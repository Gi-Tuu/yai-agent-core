"""Provider 路由治理：故障转移只能发生在宿主授权的服务商集合内。

为什么需要它：``FallbackModelProvider`` 的目标是"主模型过载时自动降级"。但如果
只要某个后端还活着就允许切换，敏感上下文可能被送到宿主根本没授权的服务商——
"能连上"不等于"被允许"。本模块把"谁能收到这次请求"变成宿主的显式声明，
并保证**未授权后端连一次网络请求都不会收到**。

分层：

- :class:`ProviderPolicy`：单个 provider 的授权声明（允许接收的敏感等级、
  是否启用、是否具备凭据）；
- :class:`ProviderRoutingPolicy`：整条链的声明（白名单 / 锁定 / 是否允许降级）；
- :func:`routing_context`：按任务注入更严格约束的上下文管理器。子任务只能
  与父约束取交集（等级取更高、白名单取交、锁定不可解除），**绝不能扩大授权**。

约束来自宿主代码，不来自模型输出；模型没有任何工具能修改这里的状态。
内核零第三方硬依赖，全部用标准库（``dataclasses`` / ``contextvars``）。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

#: 数据敏感等级（从低到高）。宿主可只用其中一部分；未知取值直接报错。
SENSITIVITY_LEVELS = ("public", "internal", "confidential", "restricted")

_SENSITIVITY_RANK = {level: index for index, level in enumerate(SENSITIVITY_LEVELS)}


def _check_sensitivity(level: str | None, *, label: str = "sensitivity") -> None:
    if level is not None and level not in _SENSITIVITY_RANK:
        raise ValueError(
            f"{label} 必须是 {SENSITIVITY_LEVELS} 之一或 None，收到 {level!r}"
        )


def _stricter_sensitivity(left: str | None, right: str | None) -> str | None:
    """两个敏感等级取更严格（更高）的那个；None 表示未声明。"""
    if left is None:
        return right
    if right is None:
        return left
    return left if _SENSITIVITY_RANK[left] >= _SENSITIVITY_RANK[right] else right


class ProviderRoutingError(RuntimeError):
    """Provider 路由治理错误基类。"""


class NoAuthorizedProviderError(ProviderRoutingError):
    """本次任务没有任何 provider 获得授权。

    必须在发出任何网络请求之前抛出——"没有可用授权目标时明确停止"，而不是
    悄悄降级到某个未授权的后端。
    """

    def __init__(self, detail: str, *, blocked: list[dict[str, str]] | None = None) -> None:
        self.blocked = list(blocked or [])
        super().__init__(f"没有可用的授权模型后端：{detail}")


@dataclass(frozen=True)
class ProviderPolicy:
    """单个 provider 的宿主授权声明。

    Args:
        provider_id: 与 ``FallbackModelProvider(provider_ids=...)`` 或 provider 的
            ``provider_id`` / ``model`` 属性对应的稳定标识。
        allowed_sensitivities: 该 provider 允许接收的敏感等级集合；默认全部。
        enabled: 宿主显式禁用（例如临时下线）时置 False，内核不会尝试。
        has_credentials: 宿主确认该后端具备可用凭据；False 表示"不要尝试"，
            避免拿一个没配密钥的后端去撞 401 或空跑。
    """

    provider_id: str
    allowed_sensitivities: frozenset[str] = frozenset(SENSITIVITY_LEVELS)
    enabled: bool = True
    has_credentials: bool = True

    def __post_init__(self) -> None:
        if not str(self.provider_id).strip():
            raise ValueError("ProviderPolicy.provider_id 必须是非空字符串")
        unknown = set(self.allowed_sensitivities) - set(SENSITIVITY_LEVELS)
        if unknown:
            raise ValueError(f"未知的敏感等级：{sorted(unknown)}")

    def allows(self, sensitivity: str | None) -> bool:
        """未声明任务等级时不过滤；声明了就看是否在该 provider 的授权集合内。"""
        if sensitivity is None:
            return True
        return sensitivity in self.allowed_sensitivities


@dataclass(frozen=True)
class ProviderRoutingPolicy:
    """整条 fallback 链的路由声明（宿主构造，模型不可见）。

    Args:
        providers: 逐 provider 的授权声明；未列出的 provider 按默认（全允许）处理。
        allowed_providers: 白名单；``None`` 表示不额外限制，空集合表示全部禁止。
        sensitivity: 静态任务敏感等级（按任务注入时以更严格者为准）。
        locked_provider: 锁定到某个 provider；锁定目标未授权时不回退。
        allow_fallback: False 时只尝试第一个授权目标，失败即停（不跨后端降级）。
    """

    providers: tuple[ProviderPolicy, ...] = ()
    allowed_providers: frozenset[str] | None = None
    sensitivity: str | None = None
    locked_provider: str | None = None
    allow_fallback: bool = True

    def __post_init__(self) -> None:
        _check_sensitivity(self.sensitivity)
        ids = [p.provider_id for p in self.providers]
        if len(ids) != len(set(ids)):
            raise ValueError("ProviderRoutingPolicy.providers 中存在重复的 provider_id")
        if self.locked_provider is not None and not str(self.locked_provider).strip():
            raise ValueError("locked_provider 必须是非空字符串或 None")

    def provider(self, provider_id: str) -> ProviderPolicy | None:
        """返回某个 provider 的声明（未声明返回 None）。"""
        for item in self.providers:
            if item.provider_id == provider_id:
                return item
        return None

    def narrowed(
        self,
        *,
        allowed_providers: frozenset[str] | None = None,
        sensitivity: str | None = None,
        locked_provider: str | None = None,
        allow_fallback: bool | None = None,
    ) -> ProviderRoutingPolicy:
        """派生子任务策略：所有维度只能收紧，不能放宽。

        白名单取交集、敏感等级取更高、父级锁定不可解除、父级禁用降级则子级也
        不能重新打开。这是"子 Agent 权限单调收窄"在路由层的实现。
        """
        _check_sensitivity(sensitivity)
        if self.allowed_providers is None:
            merged_allowed = allowed_providers
        elif allowed_providers is None:
            merged_allowed = self.allowed_providers
        else:
            merged_allowed = self.allowed_providers & allowed_providers
        if self.locked_provider is not None:
            # 父级已锁定：子级只能继续锁同一个目标，不能换、也不能解除。
            merged_locked: str | None = self.locked_provider
        else:
            merged_locked = locked_provider
        return ProviderRoutingPolicy(
            providers=self.providers,
            allowed_providers=merged_allowed,
            sensitivity=_stricter_sensitivity(self.sensitivity, sensitivity),
            locked_provider=merged_locked,
            allow_fallback=self.allow_fallback if allow_fallback is None else (
                self.allow_fallback and allow_fallback
            ),
        )


@dataclass(frozen=True)
class RoutingContext:
    """按任务（而非按实例）注入的更严格路由约束。"""

    allowed_providers: frozenset[str] | None = None
    sensitivity: str | None = None
    locked_provider: str | None = None
    allow_fallback: bool | None = None

    def narrowed_with(self, parent: RoutingContext | None) -> RoutingContext:
        """把本上下文叠加在父上下文上：全部维度只取更严格一侧。"""
        if parent is None:
            return self
        parent_allowed = parent.allowed_providers
        if parent_allowed is None:
            allowed = self.allowed_providers
        elif self.allowed_providers is None:
            allowed = parent_allowed
        else:
            allowed = parent_allowed & self.allowed_providers
        locked = parent.locked_provider or self.locked_provider
        if parent.allow_fallback is False or self.allow_fallback is False:
            fallback: bool | None = False
        else:
            fallback = self.allow_fallback if self.allow_fallback is not None else (
                parent.allow_fallback
            )
        return RoutingContext(
            allowed_providers=allowed,
            sensitivity=_stricter_sensitivity(parent.sensitivity, self.sensitivity),
            locked_provider=locked,
            allow_fallback=fallback,
        )


_routing_context: ContextVar[RoutingContext | None] = ContextVar(
    "yai_provider_routing_context", default=None
)


def current_routing() -> RoutingContext | None:
    """返回当前任务上下文里的路由约束（None 表示宿主未按任务注入）。"""
    return _routing_context.get()


@contextmanager
def routing_context(
    *,
    allowed_providers: frozenset[str] | None = None,
    sensitivity: str | None = None,
    locked_provider: str | None = None,
    allow_fallback: bool | None = None,
) -> Iterator[RoutingContext]:
    """在当前任务（含其子任务）内施加更严格的路由约束。

    用法::

        with routing_context(sensitivity="confidential", allowed_providers=frozenset({"a"})):
            await core.run("把这份内部报告转成摘要")

    约束只作用于模型 provider 选择，不会发送给模型；子任务继承本上下文并只能
    进一步收紧。退出时恢复父上下文。
    """
    _check_sensitivity(sensitivity)
    child = RoutingContext(
        allowed_providers=allowed_providers,
        sensitivity=sensitivity,
        locked_provider=locked_provider,
        allow_fallback=allow_fallback,
    ).narrowed_with(_routing_context.get())
    token = _routing_context.set(child)
    try:
        yield child
    finally:
        _routing_context.reset(token)


@dataclass(frozen=True)
class RoutingDecision:
    """一次 provider 选择的可审计结果（供宿主日志 / 工作台展示）。"""

    provider_id: str
    eligible: bool
    reason: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "eligible": self.eligible,
            "reason": self.reason,
            "detail": dict(self.detail),
        }
