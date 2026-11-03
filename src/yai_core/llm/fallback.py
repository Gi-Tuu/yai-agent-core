"""多模型兜底后端：按优先级尝试多个 ModelProvider，免费模型高峰过载时自动降级。

设计目标：
- 免费共享模型（如 GLM-4.7-Flash）在高峰期会返回 429/1305 或超时；把"能力强但偶发
  过载"的模型放前面、"稳定但较弱"的模型放后面，即可在不改变内核的前提下保证可用性。
- 仅对**可重试错误**（限流 429、服务端 5xx、超时、连接失败、空响应）降级；
  对 400/401/403 等请求或配置错误立即抛出，不浪费后续模型。
- 内置轻量断路器：主模型连续失败后进入冷却，冷却期内直接用兜底模型，冷却结束再试探
  主模型（半开），避免每次调用都先在过载模型上等待。
- 内核零第三方硬依赖：不 import openai，仅凭异常的 status_code / 类名判定可重试性。
- **路由治理（v0.26 P0）**：宿主可声明允许使用的 provider 集合、敏感等级、锁定目标；
  未授权的后端在发起请求之前就被筛掉，一个都不会被调用；无授权目标时明确抛
  :class:`NoAuthorizedProviderError` 停止。声明只能来自宿主，模型无法修改。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from yai_core.diagnostics import (
    DiagnosticEvent,
    DiagnosticKind,
    DiagnosticSink,
    emit,
)
from yai_core.llm.routing import (
    NoAuthorizedProviderError,
    ProviderPolicy,
    ProviderRoutingPolicy,
    RoutingDecision,
    current_routing,
)
from yai_core.types import ModelResponse, flatten_content_blocks

# 异常类名里包含这些关键字时视为可重试（覆盖 openai SDK 的 Timeout/Connection/RateLimit）。
_RETRYABLE_NAME_KEYS = (
    "timeout",
    "timedout",
    "connection",
    "ratelimit",
    "apiconnection",
    "internalserver",
    "serviceunavailable",
    "gateway",
)


class _EmptyResponseError(RuntimeError):
    """模型返回空内容且无工具调用（思考模型偶发），视为可重试。"""


def _messages_for(provider: Any, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按**实际尝试的后端**决定消息形态，而不是按整条链的声明决定。

    包装层若只转发链级的 ``yai_multimodal``，主视觉后端故障后切到纯文本后端，
    后者会收到它不认的内容数组；反过来整条链都降级又会让视觉后端看不到图。
    这里逐后端降级：文本后端拿字符串，视觉后端原样接数组。
    """
    if bool(getattr(provider, "yai_multimodal", False)):
        return messages
    out: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            out.append({**message, "content": flatten_content_blocks(content)})
        else:
            out.append(message)
    return out


def _is_retryable(exc: BaseException) -> bool:
    """判断异常是否值得换模型重试：429 / 5xx / 超时 / 连接问题 / 空响应。"""
    if isinstance(exc, _EmptyResponseError):
        return True
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and (status == 429 or 500 <= status < 600):
        return True
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    name = type(exc).__name__.lower()
    return any(key in name for key in _RETRYABLE_NAME_KEYS)


class FallbackModelProvider:
    """按优先级串行尝试多个模型后端的 ModelProvider。

    Args:
        providers: 按优先级排列的模型后端（都实现 ModelProvider 协议）。
        retries: 每个模型在切换到下一个之前的额外重试次数（0 = 失败即切换）。
        backoff: 同模型重试的基础退避秒数（线性倍增）。
        cooldown: 某模型失败后被短路的冷却秒数，冷却结束才会再次试探。
        per_call_timeout: 单次模型调用的硬超时秒数，防止挂死拖住整条链。
        on_diagnostic: 本地诊断回调；被治理挡下的后端会发 ``provider_route_blocked``。
        provider_ids: 与 ``providers`` 一一对应的稳定标识；不传时依次取 provider 的
            ``provider_id`` / ``model`` 属性，最后回退 ``provider-<序号>``。
        routing: 宿主的路由授权声明；None 表示不做额外限制（保持旧行为）。
    """

    # 真实联网后端：让 llm_router="auto" 启用 LLM 分类。
    yai_live_router = True

    @property
    def yai_multimodal(self) -> bool:
        """只要链上有视觉后端，就不能在包装层提前把图片降级成文字。"""
        return any(
            bool(getattr(provider, "yai_multimodal", False))
            for provider in self.providers
        )

    def __init__(
        self,
        providers: list[Any],
        *,
        retries: int = 0,
        backoff: float = 0.6,
        cooldown: float = 45.0,
        per_call_timeout: float = 30.0,
        on_diagnostic: DiagnosticSink | None = None,
        provider_ids: list[str] | None = None,
        routing: ProviderRoutingPolicy | None = None,
    ) -> None:
        if not providers:
            raise ValueError("FallbackModelProvider 至少需要一个模型后端")
        if provider_ids is not None and len(provider_ids) != len(providers):
            raise ValueError("provider_ids 必须与 providers 一一对应（长度相同）")
        if routing is not None and not isinstance(routing, ProviderRoutingPolicy):
            raise ValueError("routing 必须是 ProviderRoutingPolicy 实例或 None")
        self.providers = list(providers)
        self.retries = max(0, retries)
        self.backoff = backoff
        self.cooldown = cooldown
        self.per_call_timeout = per_call_timeout
        self.routing = routing
        if provider_ids is None:
            self.provider_ids = [
                str(
                    getattr(provider, "provider_id", None)
                    or getattr(provider, "model", None)
                    or f"provider-{index}"
                )
                for index, provider in enumerate(self.providers)
            ]
        else:
            self.provider_ids = [str(item) for item in provider_ids]
        # 每个模型的"短路到何时"时间戳；0 表示健康。
        self._unavailable_until: list[float] = [0.0] * len(self.providers)
        self._active_index = 0
        # 本地诊断回调：某模型可重试失败、降级到下一后端时发一条 warning。
        # AgentLoop 会把自己的回调鸭子注入到这里；宿主自建时也可显式传入。
        self.on_diagnostic = on_diagnostic
        # 最近一次选择结果（可审计）：eligible 列表 + 被挡下的 provider 与原因。
        self.last_routing: dict[str, Any] = {}

    def _provider_id(self, index: int) -> str:
        return self.provider_ids[index]

    def _effective_policy(self) -> ProviderRoutingPolicy:
        """静态声明叠加当前任务上下文；上下文只能收紧，不能放宽。"""
        policy = self.routing or ProviderRoutingPolicy()
        ctx = current_routing()
        if ctx is not None:
            policy = policy.narrowed(
                allowed_providers=ctx.allowed_providers,
                sensitivity=ctx.sensitivity,
                locked_provider=ctx.locked_provider,
                allow_fallback=ctx.allow_fallback,
            )
        return policy

    def _decide(self, policy: ProviderRoutingPolicy) -> tuple[list[int], list[RoutingDecision]]:
        """按治理声明筛出可尝试的下标；被挡下的每一项都留下原因。"""
        eligible: list[int] = []
        blocked: list[RoutingDecision] = []
        for index, _provider in enumerate(self.providers):
            provider_id = self._provider_id(index)
            entry = policy.provider(provider_id) or ProviderPolicy(provider_id)
            reason = ""
            detail: dict[str, Any] = {"provider_id": provider_id}
            if policy.allowed_providers is not None and provider_id not in policy.allowed_providers:
                reason = "not_authorized"
            elif not entry.enabled:
                reason = "disabled"
            elif not entry.has_credentials:
                reason = "no_credentials"
            elif not entry.allows(policy.sensitivity):
                reason = "sensitivity"
                detail["sensitivity"] = policy.sensitivity
            elif (
                policy.locked_provider is not None
                and provider_id != policy.locked_provider
            ):
                reason = "locked_to_other_provider"
                detail["locked_provider"] = policy.locked_provider
            if reason:
                blocked.append(RoutingDecision(provider_id=provider_id, eligible=False,
                                               reason=reason, detail=detail))
            else:
                eligible.append(index)
        if not policy.allow_fallback and eligible:
            blocked.extend(
                RoutingDecision(
                    provider_id=self._provider_id(index),
                    eligible=False,
                    reason="fallback_disabled",
                    detail={"kept": self._provider_id(eligible[0])},
                )
                for index in eligible[1:]
            )
            eligible = eligible[:1]
        return eligible, blocked

    def _record_routing(
        self, eligible: list[int], blocked: list[RoutingDecision], policy: ProviderRoutingPolicy
    ) -> None:
        self.last_routing = {
            "eligible": [self._provider_id(i) for i in eligible],
            "blocked": [item.to_dict() for item in blocked],
            "sensitivity": policy.sensitivity,
            "locked_provider": policy.locked_provider,
            "allow_fallback": policy.allow_fallback,
        }
        for item in blocked:
            emit(
                self.on_diagnostic,
                DiagnosticEvent(
                    kind=str(DiagnosticKind.PROVIDER_ROUTE_BLOCKED),
                    message=(
                        f"方案后端 {item.provider_id} 被路由治理挡下（{item.reason}），"
                        "不会发起请求"
                    ),
                    data=item.to_dict(),
                ),
            )

    # ---------- 供日志/标签使用的属性 ----------

    @property
    def model(self) -> str:
        return getattr(self.providers[self._active_index], "model", "fallback")

    @property
    def strong_model(self) -> str:
        return getattr(self.providers[self._active_index], "strong_model", self.model)

    def status(self) -> list[dict[str, Any]]:
        """返回各模型当前健康状态（供诊断/工作台展示）。"""
        now = time.monotonic()
        out = []
        for i, p in enumerate(self.providers):
            until = self._unavailable_until[i]
            out.append(
                {
                    "provider_id": self._provider_id(i),
                    "model": getattr(p, "model", f"provider-{i}"),
                    "healthy": until <= now,
                    "retry_in": round(max(0.0, until - now), 1),
                    "active": i == self._active_index,
                }
            )
        return out

    # ---------- 主入口 ----------

    async def achat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        tier: str = "standard",
    ) -> ModelResponse:
        # 路由治理先于一切：未经授权的后端连一次请求都不会收到。
        policy = self._effective_policy()
        eligible, blocked = self._decide(policy)
        self._record_routing(eligible, blocked, policy)
        if not eligible:
            raise NoAuthorizedProviderError(
                "白名单 / 凭据 / 敏感等级约束下没有可用后端",
                blocked=[item.to_dict() for item in blocked],
            )

        now = time.monotonic()
        # 健康模型优先；冷却中的模型排到后面（冷却结束后自然回到候选）。
        order = [i for i in eligible if self._unavailable_until[i] <= now] + [
            i for i in eligible if self._unavailable_until[i] > now
        ]

        last_exc: Exception | None = None
        for idx in order:
            provider = self.providers[idx]
            try:
                resp = await self._attempt(
                    provider, _messages_for(provider, messages), tools, tier
                )
                # 成功：恢复健康并记住当前模型（粘性）。
                self._unavailable_until[idx] = 0.0
                self._active_index = idx
                return resp
            except Exception as exc:  # noqa: BLE001 - 需要区分可重试/致命并决定是否降级
                last_exc = exc
                if not _is_retryable(exc):
                    # 致命错误（400/401/403 等）：换模型也不会好，立即上抛。
                    raise
                # 可重试错误：标记该模型短路，继续尝试下一个。
                self._unavailable_until[idx] = time.monotonic() + self.cooldown
                emit(
                    self.on_diagnostic,
                    DiagnosticEvent(
                        kind=str(DiagnosticKind.MODEL_FALLBACK),
                        message=(
                            f"模型 {getattr(provider, 'model', f'provider-{idx}')} "
                            f"可重试失败（{type(exc).__name__}），降级到下一后端"
                        ),
                        data={
                            "failed_model": getattr(provider, "model", f"provider-{idx}"),
                            "error": f"{type(exc).__name__}: {exc}",
                            "cooldown": self.cooldown,
                        },
                    ),
                )
                continue

        # 所有模型都失败：抛出最后一个（通常是过载/超时），由上层决定如何兜底。
        if last_exc is None:
            # 不应发生：循环结束却无异常；显式 raise（python -O 会剥离 assert）
            raise RuntimeError("所有尝试均失败，但未捕获到具体异常")
        raise last_exc

    async def _attempt(
        self,
        provider: Any,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        tier: str,
    ) -> ModelResponse:
        """单个模型上的有限重试 + 硬超时 + 空响应判定。"""
        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                async with asyncio.timeout(self.per_call_timeout):
                    resp = await provider.achat(messages, tools, tier=tier)
                if not (resp.content or resp.tool_calls):
                    raise _EmptyResponseError("模型返回空内容且无工具调用")
                return resp
            except TimeoutError as exc:
                last_exc = exc
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if not _is_retryable(exc):
                    raise
            if attempt < self.retries:
                await asyncio.sleep(self.backoff * (attempt + 1))
        if last_exc is None:
            # 不应发生：循环结束却无异常；显式 raise（python -O 会剥离 assert）
            raise RuntimeError("所有尝试均失败，但未捕获到具体异常")
        raise last_exc
