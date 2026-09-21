"""统一的工具调用授权闸：普通工具与内核 meta-tool 共用同一套权限语义。

执行器（普通工具）与 Agent Loop（request_capability / create_code_tool 等
由内核直接处理的 meta-tool）都通过这里做权限判定，保证"询问 / 拒绝 / 放行"
的事件顺序完全一致，避免两套实现漂移。

事件顺序与 :class:`yai_core.tools.executor.ToolExecutor` 严格对齐：

- ``ASK``：先发 ``PERMISSION_ASKED`` 等宿主确认，**通过后才发** ``TOOL_CALL``；
  拒绝则不发 ``TOOL_CALL``。
- ``DENY``：不发 ``TOOL_CALL``，直接判定不通过。
- ``ALLOW``：发 ``TOOL_CALL`` 后放行。

返回 ``(events, approved, reason)``。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from yai_core.spi.policy import PermissionDecision
from yai_core.types import AgentEvent, EventType

if TYPE_CHECKING:
    from yai_core.spi.channel import Channel
    from yai_core.spi.policy import PermissionPolicy


async def authorize_tool_call(
    policy: PermissionPolicy,
    channel: Channel,
    name: str,
    arguments: dict,
) -> tuple[list[AgentEvent], bool, str]:
    """对一次工具调用做权限判定，返回（要追加的事件，是否放行，拒绝原因）。"""
    events: list[AgentEvent] = []
    decision = await policy.check(name, arguments)
    if decision == PermissionDecision.ASK:
        events.append(
            AgentEvent(
                EventType.PERMISSION_ASKED,
                {"tool": name, "arguments": arguments},
            )
        )
        approved = await channel.confirm(name, arguments)
        if not approved:
            return events, False, f"用户/宿主拒绝执行工具 {name}"
    if decision == PermissionDecision.DENY:
        return events, False, f"权限策略拒绝执行工具 {name}"

    events.append(
        AgentEvent(EventType.TOOL_CALL, {"tool": name, "arguments": arguments})
    )
    return events, True, ""
