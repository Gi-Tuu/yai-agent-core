"""工具准入：gate / egress / 参数 / 频率的前置闸。

从 ``tools/executor.py`` 拆出；纯函数 + 一个需要执行器状态的 check_admission。
"""

from __future__ import annotations

from typing import Any

from yai_core.policy.approval import (
    DECISION_APPROVAL_REQUIRED,
    DECISION_REJECT_INVALID_ARGUMENTS,
    argument_errors,
    evaluate_approval,
)
from yai_core.policy.egress import check_egress
from yai_core.types import AgentEvent, EventType, GateDecision, ToolSpec


def _native_approval(spec: ToolSpec, arguments: dict[str, Any]) -> GateDecision | None:
    """宿主只写了数据（``governance.approval``）时，由内核自己判一次要不要问人。

    没声明审批就返回 ``None``（"这层没意见"），事件形状与加了这块功能之前一字不差；
    ``allow`` 同样折回 ``None``，不让"声明了但没命中"污染审计载荷。
    """
    policy = spec.governance.approval
    if policy is None:
        return None
    outcome = evaluate_approval(policy, arguments, spec.input_schema)
    if outcome.decision == DECISION_REJECT_INVALID_ARGUMENTS:
        return GateDecision(reject=f"调用被审批闸拒绝：{outcome.reason}", reason=outcome.reason)
    if outcome.decision == DECISION_APPROVAL_REQUIRED:
        return GateDecision(
            require_approval=True, reason=outcome.reason, prompt=outcome.prompt
        )
    return None


def _tighten(
    gate: GateDecision | None, native: GateDecision | None
) -> GateDecision | None:
    """合并同一工具的函数级闸（``gate``）与数据级闸（``approval``），**取更严的那个**。

    两个来源都可能同时存在：``gate`` 由宿主/集成层现场算，``approval`` 是随清单走的声明。
    谁都不比谁弱，所以不能"有 gate 就当 approval 不存在"——那等于声明了的保护内核没做。
    合并只会让闸门更严：拒绝取并、要问人取或、话术与描述性字段先到先得。
    """
    if gate is None:
        return native
    if native is None:
        return gate
    rejects = [text for text in (gate.reject, native.reject) if text]
    reasons = [text for text in (gate.reason, native.reason) if text]
    return GateDecision(
        reject="；".join(dict.fromkeys(rejects)),
        require_approval=gate.require_approval or native.require_approval,
        reason="；".join(dict.fromkeys(reasons)),
        prompt=gate.prompt or native.prompt,
        risk=gate.risk or native.risk,
        scope=gate.scope or native.scope,
        target=gate.target or native.target,
    )


def check_admission(
    executor: Any,
    spec: ToolSpec,
    name: str,
    arguments: dict[str, Any],
    subject: Any,
    events: list[AgentEvent],
    started: float,
    call_id: str,
) -> tuple[GateDecision | None, str | None]:
        """工具自带闸、外发来源、参数校验三道前置闸。

        频率上限不在这里判定：它必须在授权通过之后才扣配额，否则被策略
        拒绝的调用也能耗尽配额（多租户 DoS）。
        """
        gate_fn = spec.governance.gate
        try:
            decision = gate_fn(arguments, subject) if gate_fn is not None else None
            decision = _tighten(decision, _native_approval(spec, arguments))
        except Exception as exc:  # noqa: BLE001 - 宿主回调异常必须失败关闭
            reason = f"工具 {name} 的准入闸异常，已失败关闭：{type(exc).__name__}"
            events.append(executor._audit.denied_event(spec, name, arguments, reason, "gate"))
            executor._audit.fail(
                events, spec=spec, name=name, call_id=call_id,
                started=started, message=reason,
            )
            return None, reason
        if decision is not None and decision.reject:
            events.append(
                executor._audit.denied_event(spec, name, arguments, decision.reject, "gate")
            )
            executor._audit.fail(
                events, spec=spec, name=name, call_id=call_id,
                started=started, message=decision.reject,
            )
            return decision, decision.reject

        egress_reason = check_egress(spec, arguments)
        if egress_reason is not None:
            events.append(
                executor._audit.denied_event(spec, name, arguments, egress_reason, "egress")
            )
            executor._audit.fail(
                events, spec=spec, name=name, call_id=call_id,
                started=started, message=egress_reason,
            )
            return decision, egress_reason

        errors = argument_errors(arguments, spec.input_schema)
        if errors:
            message = f"工具 {name} 的调用参数不合法：" + "；".join(errors)
            executor._audit.fail(
                events,
                spec=spec,
                name=name,
                call_id=call_id,
                started=started,
                message=message,
                flags={"invalid_arguments": True},
            )
            return decision, message

        # 频率上限先"只查不扣"：真正扣配额要等授权通过（否则被策略拒绝的调用
        # 也能耗尽配额）。这里只判定本次是否已满，满则直接失败关闭。
        rate_hint = spec.governance.rate_limit
        if rate_hint is not None and executor._rate_limit_full(name, rate_hint):
            message = (
                f"工具 {name} 超出调用频率上限"
                f"（{rate_hint.count} 次 / {rate_hint.window}），本次未执行"
            )
            events.append(
                AgentEvent(
                    EventType.TOOL_RESULT,
                    {"tool": name, "ok": False, "error": message, "rate_limited": True},
                )
            )
            events.append(
                executor._audit.denied_event(spec, name, arguments, message, "rate_limit")
            )
            executor._audit.attach(
                events,
                executor._audit.receipt(
                    call_id=call_id,
                    tool=name,
                    status="not_started",
                    started=started,
                    error=message,
                ),
            )
            return decision, message
        return decision, None
