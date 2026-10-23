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
from yai_core.types import AgentEvent, ChangePreview, EventType

if TYPE_CHECKING:
    from yai_core.spi.channel import ApprovalContextChannel, Channel
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


def fill_prompt(template: str, shown: dict) -> str:
    """把 ``{参数名}`` 占位符填进审批话术，**只用对外副本里的值**。

    未知占位符原样留着（模板与参数不匹配是宿主的事，不该把审批整条吞掉）。

    公开给执行器复用：组合工具自己声明审批时走的是 :meth:`Channel.confirm`
    而不是这里（组合名是黑盒，策略那一步不该跑），但话术口径必须一致——
    同一份声明在两条路径上不能给出两种问法。
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


async def confirm_with_context(
    channel: Channel, name: str, shown: dict, context: dict[str, Any]
) -> bool:
    """向通道要一次人工确认；**只有通道自己声明愿意收审批上下文，才多传第三个参数**。

    用能力旗标而不是探签名：宿主写的通道可能有 `**kwargs`，鸭子探测会把"能收"猜成
    "不能收"或反过来——猜错的那一次就是审批人少看了一个字段。

    基础 :class:`Channel` 契约只要求两个位置参数；第三个参数属于可选的
    :class:`ApprovalContextChannel` 扩展。这里按旗标收窄到扩展契约，执行器与
    ``authorize_tool_call`` 共用同一条调用规则，避免两处各写一份。
    """
    aware = getattr(channel, "yai_approval_context_aware", False)
    if aware:
        return bool(
            await cast("ApprovalContextChannel", channel).confirm(name, shown, context)
        )
    return bool(await channel.confirm(name, shown))


def approval_context(
    name: str,
    *,
    require_approval: bool = False,
    reason: str = "",
    prompt: str = "",
    risk: str = "",
    scope: str = "",
    target: str = "",
    preview: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """组装递给通道的审批上下文：**有值才带**，空字段不占位。

    公开给执行器复用：组合工具分支不经过 :func:`authorize_tool_call`（权限落到每一步），
    但它自己声明的审批要求走的是同一个通道口径——两条路必须给审批人看一样的东西，
    也共用同一个旗标规则（``confirm_with_context`` 是内部实现，这里显式查一次）。
    """
    context: dict[str, Any] = {"tool": name}
    for key, value in (("risk", risk), ("scope", scope), ("target", target)):
        if value:
            context[key] = value
    if preview:
        context["preview"] = preview
    if require_approval:
        context["approval_required"] = True
        if reason:
            context["reason"] = reason
        if prompt:
            context["prompt"] = prompt
    return context


def channel_wants_approval_context(channel: object) -> bool:
    """通道是否声明愿意收审批上下文（旗标缺失即视为"只要两个参数"）。"""
    return bool(getattr(channel, "yai_approval_context_aware", False))


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
    risk: str = "",
    scope: str = "",
    target: str = "",
    preview: ChangePreview | None = None,
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

    ``preview`` 是宿主生成的**实际变更预览**（不是模型描述）：审批事件、审批上下文
    和 ``TOOL_CALL`` 都会带上它；执行器在确认后还会重新生成并比较 token，变了就
    拒绝执行。
    ``risk`` / ``scope`` / ``target`` 是准入判定算出来的**描述性**信息（不参与放行
    决策，决策只看 DENY / ASK / ALLOW 与 require_approval）。它们必须落到事件里，
    否则"这次是高影响写、作用域是什么、真正要碰哪个资源"在审计链上根本看不出来——
    事后无法按风险检索，审批人也只能凭模型原样写来的参数串做判断。
    空串表示"这一层没意见"，此时**不写进载荷**，保持未治理工具的既有事件形状。

    **同一个描述性上下文还要交给审批人本身**：事件是在 ``confirm()`` 返回之后才由
    loop 投出去的，卡片弹出的那一刻那条事件还在半路上，所以界面不能靠事件读这些字段
    ——必须由这里顺通道调用一起递过去。只有声明了 ``yai_approval_context_aware``
    的通道会收到第三个参数，其余实现一字不改照样跑；送出去的**只有描述性字段**
    （理由、填好的话术、风险、作用域、落点），参数值仍走下面那份脱敏副本——
    宿主写的 prompt 模板里如果本来就有明文参数，那是宿主的决定，内核不替它扩散。
    """
    events: list[AgentEvent] = []
    decision = await _check(policy, name, arguments, subject)
    if decision == PermissionDecision.DENY:
        return events, False, f"权限策略拒绝执行工具 {name}"

    # 描述性字段：有值才带，避免给未声明治理的工具凭空多出几个键。
    descriptor = {
        k: v for k, v in (("risk", risk), ("scope", scope), ("target", target)) if v
    }
    preview_data = preview.to_dict() if preview is not None else None

    if decision == PermissionDecision.ASK or require_approval:
        asked = redact_arguments(arguments, sensitive_paths)
        payload: dict[str, Any] = {"tool": name, "arguments": asked, **descriptor}
        if preview_data:
            payload["preview"] = preview_data
        # 给审批人的上下文：与事件同源、同样"有值才带"，但不含参数值本身
        # （参数走 confirm 的第二个位置，那份是给通道的独立副本）。
        context = approval_context(
            name,
            require_approval=require_approval,
            preview=preview_data,
            **descriptor,
        )
        if require_approval:
            payload["approval_required"] = True
            payload["reason"] = approval_reason
            context["approval_required"] = True
            context["reason"] = approval_reason
            if approval_prompt:
                filled = fill_prompt(approval_prompt, asked)
                payload["prompt"] = filled
                context["prompt"] = filled
        events.append(AgentEvent(EventType.PERMISSION_ASKED, payload))
        # 通道拿的是**另一份**脱敏副本：它若在 confirm 里改自己那份，改不动
        # 已经发出的事件载荷，也改不动执行（见 tests/test_argument_snapshot.py）。
        approved = await confirm_with_context(
            channel, name, redact_arguments(arguments, sensitive_paths), context
        )
        if not approved:
            return events, False, f"用户/宿主拒绝执行工具 {name}"

    calls: dict[str, Any] = {
        "tool": name,
        "arguments": redact_arguments(arguments, sensitive_paths, broad=audit_sensitive),
        **descriptor,
    }
    if preview_data:
        calls["preview"] = preview_data
    events.append(AgentEvent(EventType.TOOL_CALL, calls))
    return events, True, ""
