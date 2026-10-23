"""通道契约：Core 不假设 UI 形态（CLI / Web / Flutter 都实现同一接口）。

基础 :class:`Channel` 只要求 ``emit`` / ``ask`` / 两参数 ``confirm``——这是所有
宿主都能实现的**稳定下限**。审批上下文（``reason / prompt / risk / scope / target``）
属于可选扩展 :class:`ApprovalContextChannel`：内核在"要问人"的那条路上会把这些
描述性字段一起递出来，让审批人看得见"这次到底要动哪个资源、为什么问"。

要不要接：**通道自己声明**。带上 ``yai_approval_context_aware = True`` 才会收到
第三个参数，没带的实现一字不改照样跑。内核不用"探签名"来判断——宿主写的通道
可能有 ``**kwargs``，探签名会把"能收"猜成"不能收"，猜错的那一次就是审批人少看
了一个字段。

为什么不在基础契约里直接写第三个参数：``CollectChannel``、宿主手写的两参数通道
在运行时一直合法，但静态类型检查会判它们不满足 Protocol。把扩展单独拆出来，
"旧实现零改动"才是类型层面也成立的承诺，而不是靠一层 ``cast`` 遮住不兼容。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from yai_core.types import AgentEvent


@runtime_checkable
class Channel(Protocol):
    async def emit(self, event: AgentEvent) -> None:
        """接收 Observer 事件流（思考过程、工具状态、错误等）。"""
        ...

    async def ask(self, question: str) -> str:
        """策略为 clarify 时，向宿主/用户反问并取回回答。"""
        ...

    async def confirm(self, tool_name: str, arguments: dict[str, Any]) -> bool:
        """权限策略为 ask（或契约/原生审批要求）时，请求宿主确认是否执行该工具。

        ``arguments`` 永远是**已按声明脱敏的对外副本**。
        """
        ...


@runtime_checkable
class ApprovalContextChannel(Protocol):
    """可选扩展：愿意接收审批上下文的通道。

    实现方必须同时声明 ``yai_approval_context_aware = True``。第三个参数全部是
    描述性字段，参数值仍走第二个位置，且已经脱敏。
    """

    yai_approval_context_aware: bool

    async def emit(self, event: AgentEvent) -> None:
        ...

    async def ask(self, question: str) -> str:
        ...

    async def confirm(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: dict[str, Any],
    ) -> bool:
        ...
