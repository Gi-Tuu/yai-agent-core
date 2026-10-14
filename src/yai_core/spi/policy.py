"""权限策略契约：高风险工具调用必须可被宿主拦截/确认。

**主体感知的可选扩展**（向后兼容，不改变已有实现的调用方式）：需要看"谁在做"的策略，
除了实现 :meth:`PermissionPolicy.check` 之外，在类上声明 ``yai_subject_aware = True``，
并把 ``check`` 写成接受 ``subject=`` 关键字的形式::

    class MyPolicy:
        yai_subject_aware = True

        async def check(self, tool_name, arguments, *, subject=None): ...

内核只在**宿主注入了主体**时才多传这个关键字；没声明 ``yai_subject_aware`` 的策略
一律按老签名调用，不会被突然多出来的参数打挂。主体永远来自宿主注入，
模型输出与工具参数里的身份声明都不算。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class PermissionDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


@runtime_checkable
class PermissionPolicy(Protocol):
    async def check(self, tool_name: str, arguments: dict[str, Any]) -> PermissionDecision: ...


class SubjectAwarePermissionPolicy(Protocol):
    """**可选扩展形态**：策略想看见"谁在做"就长成这样，并声明 ``yai_subject_aware = True``。

    内核用能力旗标而不是鸭子探测来决定要不要多传 ``subject=``——签名猜探会在宿主
    策略用了 ``**kwargs`` 或位置参数时猜错，宁可不传。
    """

    yai_subject_aware: bool

    async def check(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        subject: Any = None,
    ) -> PermissionDecision: ...
