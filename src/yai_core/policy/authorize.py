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

**参数快照契约**：调用方在进入权限判定之前用 :func:`snapshot_arguments` 复制一份参数，
此后"展示给用户审批的""写进审计事件的""真正传给 handler 的"必须是**同一个对象**。
否则宿主或通道在 ``confirm()`` 期间改写这个 dict，就会出现"用户批准的是 A、
实际执行的是 B"这类审批与执行不一致的问题。
"""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING

from yai_core.policy.redact import redact_arguments
from yai_core.spi.policy import PermissionDecision
from yai_core.types import AgentEvent, EventType

if TYPE_CHECKING:
    from yai_core.spi.channel import Channel
    from yai_core.spi.policy import PermissionPolicy


def snapshot_arguments(arguments: dict) -> dict:
    """深拷贝模型给出的参数，作为审批-执行-审计共用的唯一快照。

    模型侧参数本就是 JSON 值（dict/list/标量），深拷贝代价低且无自定义对象；
    拷贝失败（罕见：宿主塞进了不可拷贝的值）时退回原对象，宁可继续也不崩执行链。
    """
    try:
        return deepcopy(arguments)
    except Exception:  # noqa: BLE001 - 快照是加固手段，不应成为新的失败源
        return arguments


async def authorize_tool_call(
    policy: PermissionPolicy,
    channel: Channel,
    name: str,
    arguments: dict,
    *,
    sensitive_paths: tuple[str, ...] = (),
) -> tuple[list[AgentEvent], bool, str]:
    """对一次工具调用做权限判定，返回（要追加的事件，是否放行，拒绝原因）。

    ``arguments`` 是**权威参数**（调用方进入闸口前已快照），策略判定与实际执行都用它；
    给用户看的审批事件、:meth:`Channel.confirm` 的入参、以及 ``TOOL_CALL`` 事件
    各自持有独立副本 —— 宿主或通道在确认期间改写的只是它拿到那份，改不动执行。

    ``sensitive_paths`` 来自宿主在 :class:`ToolSpec.governance` 上的声明：对外三份
    副本里的这些路径会被替换成占位符，而执行仍拿原值（脱敏不得改变业务行为）。
    """
    events: list[AgentEvent] = []
    decision = await policy.check(name, arguments)
    if decision == PermissionDecision.ASK:
        shown = redact_arguments(arguments, sensitive_paths)
        events.append(
            AgentEvent(
                EventType.PERMISSION_ASKED,
                {"tool": name, "arguments": shown},
            )
        )
        approved = await channel.confirm(name, shown)
        if not approved:
            return events, False, f"用户/宿主拒绝执行工具 {name}"
    if decision == PermissionDecision.DENY:
        return events, False, f"权限策略拒绝执行工具 {name}"

    events.append(
        AgentEvent(
            EventType.TOOL_CALL,
            {"tool": name, "arguments": redact_arguments(
                arguments, sensitive_paths
            )},
        )
    )
    return events, True, ""
