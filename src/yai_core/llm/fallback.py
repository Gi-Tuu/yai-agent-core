"""多模型兜底后端：按优先级尝试多个 ModelProvider，免费模型高峰过载时自动降级。

设计目标：
- 免费共享模型（如 GLM-4.7-Flash）在高峰期会返回 429/1305 或超时；把"能力强但偶发
  过载"的模型放前面、"稳定但较弱"的模型放后面，即可在不改变内核的前提下保证可用性。
- 仅对**可重试错误**（限流 429、服务端 5xx、超时、连接失败、空响应）降级；
  对 400/401/403 等请求或配置错误立即抛出，不浪费后续模型。
- 内置轻量断路器：主模型连续失败后进入冷却，冷却期内直接用兜底模型，冷却结束再试探
  主模型（半开），避免每次调用都先在过载模型上等待。
- 内核零第三方硬依赖：不 import openai，仅凭异常的 status_code / 类名判定可重试性。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from yai_core.types import ModelResponse

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
    """

    # 真实联网后端：让 llm_router="auto" 启用 LLM 分类。
    yai_live_router = True

    def __init__(
        self,
        providers: list[Any],
        *,
        retries: int = 0,
        backoff: float = 0.6,
        cooldown: float = 45.0,
        per_call_timeout: float = 30.0,
    ) -> None:
        if not providers:
            raise ValueError("FallbackModelProvider 至少需要一个模型后端")
        self.providers = list(providers)
        self.retries = max(0, retries)
        self.backoff = backoff
        self.cooldown = cooldown
        self.per_call_timeout = per_call_timeout
        # 每个模型的"短路到何时"时间戳；0 表示健康。
        self._unavailable_until: list[float] = [0.0] * len(self.providers)
        self._active_index = 0

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
            out.append({
                "model": getattr(p, "model", f"provider-{i}"),
                "healthy": until <= now,
                "retry_in": round(max(0.0, until - now), 1),
                "active": i == self._active_index,
            })
        return out

    # ---------- 主入口 ----------

    async def achat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        tier: str = "standard",
    ) -> ModelResponse:
        now = time.monotonic()
        # 健康模型优先；冷却中的模型排到后面（冷却结束后自然回到候选）。
        order = [
            i for i in range(len(self.providers))
            if self._unavailable_until[i] <= now
        ] + [
            i for i in range(len(self.providers))
            if self._unavailable_until[i] > now
        ]

        last_exc: Exception | None = None
        for idx in order:
            provider = self.providers[idx]
            try:
                resp = await self._attempt(provider, messages, tools, tier)
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
                continue

        # 所有模型都失败：抛出最后一个（通常是过载/超时），由上层决定如何兜底。
        assert last_exc is not None
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
                resp = await asyncio.wait_for(
                    provider.achat(messages, tools, tier=tier),
                    timeout=self.per_call_timeout,
                )
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
        assert last_exc is not None
        raise last_exc
