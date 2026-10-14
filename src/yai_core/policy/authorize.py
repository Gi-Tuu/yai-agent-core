"""统一的工具调用授权闸：普通工具与内核 meta-tool 共用同一套权限语义。

执行器（普通工具）与 Agent Loop（request_capability / create_code_tool 等
由内核直接处理的 meta-tool）都通过这里做权限判定，保证"询问 / 拒绝 / 放行"
的事件顺序完全一致，避免两套实现漂移。

事件顺序与 :class:`yai_core.tools.executor.ToolExecutor` 严格对齐：

- ``ASK``：先发 ``PERMISSION_ASKED`` 等宿主确认，**通过后才发** ``TOOL_CALL``；
  拒绝则不发 ``TOOL_CALL``。
- ``DENY``：不发 ``TOOL_CALL``，直接判定不通过。
- ``ALLOW``：发 ``TOOL_CALL`` 后放行。
- ``require_approval``（能力契约的审批意图）只能把闸门**往上抬**：策略 ALLOW 也走 ASK
  的分支，但策略 DENY 永远优先——外部声明不能替宿主放行。

返回 ``(events, approved, reason)``。

**参数快照契约**：调用方在进入权限判定之前用 :func:`snapshot_arguments` 复制一份参数，
此后"策略判定用的""真正传给 handler 的"都必须是**这同一份权威对象**，而"给用户看的"
和"写进审计事件的"是它派生出的**只读副本**。否则宿主或通道在 ``confirm()`` 期间改写
这个 dict，就会出现"用户批准的是 A、实际执行的是 B"这类审批与执行不一致的问题。
"""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any, cast

from yai_core.policy.redact import redact_arguments
from yai_core.spi.policy import PermissionDecision
from yai_core.types import AgentEvent, EventType

if TYPE_CHECKING:
    from yai_core.spi.channel import Channel
    from yai_core.spi.policy import PermissionPolicy, SubjectAwarePermissionPolicy


def snapshot_arguments(arguments: dict) -> dict:
    """深拷贝模型给出的参数，作为审批-执行-审计共用的唯一快照。

    模型侧参数本就是 JSON 值（dict/list/标量），深拷贝代价低且无自定义对象；
    拷贝失败（罕见：宿主塞进了不可拷贝的值）时退回原对象，宁可继续也不崩执行链。
    """
    try:
        return deepcopy(arguments)
    except Exception:  # noqa: BLE001 - 快照是加固手段，不应成为新的失败源
        return arguments


def _fill_prompt(template: str, shown: dict) -> str:
    """把 ``{参数名}`` 占位符填进审批话术，**只用对外副本里的值**。

    未知占位符原样留着（模板与参数不匹配是宿主的事，不该把审批整条吞掉）。
    """
    text = template
    for key, value in (shown or {}).items():
        token = "{" + str(key) + "}"
        if token in text:
            text = text.replace(token, str(value))
    return text


async def _check(
    policy: PermissionPolicy, name: str, arguments: dict, subject: Any
) -> PermissionDecision:
    """策略判定：只有策略自己声明 ``yai_subject_aware`` 时才多传主体。

    这样旧的 PermissionPolicy 实现一字不改照样跑；主体感知的宿主也不用接受
    一个它读不懂的入参——**内核不替策略假装它懂身份**。
    """
    if subject is not None and getattr(policy, "yai_subject_aware", False):
        aware = cast("SubjectAwarePermissionPolicy", policy)
        return await aware.check(name, arguments, subject=subject)
    return await policy.check(name, arguments)


async def authorize_tool_call(
    policy: PermissionPolicy,
    channel: Channel,
    name: str,
    arguments: dict,
    *,
    sensitive_paths: tuple[str, ...] = (),
    audit_sensitive: bool = False,
    subject: Any = None,
    require_approval: bool = False,
    approval_reason: str = "",
    approval_prompt: str = "",
) -> tuple[list[AgentEvent], bool, str]:
    """对一次工具调用做权限判定，返回（要追加的事件，是否放行，拒绝原因）。

    ``arguments`` 是**权威参数**（调用方进入闸口前已快照），策略判定与实际执行都用它；
    给用户看的审批事件、:meth:`Channel.confirm` 的入参、以及 ``TOOL_CALL`` 事件
    各持独立副本 —— 宿主或通道在确认期间改写的只是它拿到的那份，改不动执行，
    也改不动已经发出的事件载荷。

    ``sensitive_paths`` 来自宿主在 :class:`ToolSpec.governance` 上的声明：对外
    副本里的这些路径会被替换成占位符，而执行仍拿原值（脱敏不得改变业务行为）。
    ``audit_sensitive`` 是宽口径审计提示：只把**审计事件**那份的顶层值整体收口，
    给审批人的副本仍按路径脱敏（全糊掉会让人工批准失去判断依据）。

    ``subject`` 是宿主注入的可信操作主体，只交给声明了 ``yai_subject_aware`` 的策略。

    ``require_approval`` 来自能力契约的审批意图（``approval.required`` / 命中的
    ``approval.when``）：**它只能把闸门往上抬**——策略 DENY 仍然优先，策略 ALLOW
    也会被抬成人工确认；反过来说，契约永远不能替策略放行。
    """
    events: list[AgentEvent] = []
    decision = await _check(policy, name, arguments, subject)
    if decision == PermissionDecision.DENY:
        return events, False, f"权限策略拒绝执行工具 {name}"

    if decision == PermissionDecision.ASK or require_approval:
        asked = redact_arguments(arguments, sensitive_paths)
        payload: dict[str, Any] = {"tool": name, "arguments": asked}
        if require_approval:
            payload["approval_required"] = True
            payload["reason"] = approval_reason
            if approval_prompt:
                payload["prompt"] = _fill_prompt(approval_prompt, asked)
        events.append(AgentEvent(EventType.PERMISSION_ASKED, payload))
        approved = await channel.confirm(
            name, redact_arguments(arguments, sensitive_paths)
        )
        if not approved:
            return events, False, f"用户/宿主拒绝执行工具 {name}"

    events.append(
        AgentEvent(
            EventType.TOOL_CALL,
            {
                "tool": name,
                "arguments": redact_arguments(
                    arguments, sensitive_paths, broad=audit_sensitive
                ),
            },
        )
    )
    return events, True, ""
