"""通道契约：Core 不假设 UI 形态（CLI / Web / Flutter 都实现同一接口）。

`confirm` 的第三个参数是**可选的审批上下文**：内核在"要问人"的那条路上，会把
`reason / prompt / risk / scope / target` 一起递出来，让审批人看得见"这次到底要动
哪个资源、为什么问"——这些数据也进了 `PERMISSION_ASKED` 事件，但**事件是在
`confirm()` 返回之后才由 loop 投出去的**，正在弹的卡片读不到它，所以只能走参数这条路。

要不要接：**通道自己声明**。带上 `yai_approval_context_aware = True` 才会收到第三个
参数，没带的实现一字不改照样跑。内核不用"探签名"来判断——宿主写的通道可能有
`**kwargs`，探签名会把"能收"猜成"不能收"，猜错的那一次就是审批人少看了一个字段。
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

    async def confirm(
        self, tool_name: str, arguments: dict[str, Any], context: dict[str, Any] | None = None
    ) -> bool:
        """权限策略为 ask（或契约/原生审批要求）时，请求宿主确认是否执行该工具。

        `context` 只在实现方带 `yai_approval_context_aware = True` 时才会被传入，
        内容全是描述性字段；`arguments` 永远是**已按声明脱敏的对外副本**。
        """
        ...
