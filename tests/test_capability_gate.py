"""能力闸（gate）与可信主体（Subject）的回归：契约声明怎么变成每次调用的准入。

钉住的语义：

- 主体只能由宿主注入，**只能透传到策略与闸**，模型侧无从伪造；
- 闸只能把闸门**往上抬**：``require_approval`` 能把策略的 ALLOW 抬成人工确认，
  但永远抬不动策略的 DENY；``reject`` 让调用根本到不了业务操作；
- 没有声明 / 没有闸的工具行为与从前一致（渐进兼容）。
"""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError

import pytest

from yai_core import (
    AgentCore,
    GateDecision,
    ModelResponse,
    Subject,
    ToolCallRequest,
    ToolGovernance,
    build_spec,
)
from yai_core.integrations.acc.declaration import parse_declaration
from yai_core.integrations.acc.gate import build_gate
from yai_core.spi.policy import PermissionDecision
from yai_core.tools import ToolExecutor, ToolRegistry
from yai_core.types import EventType

AMOUNT_SCHEMA = {
    "type": "object",
    "properties": {"amount": {"type": "number"}},
    "required": ["amount"],
}


def _gate(declaration: dict, schema: dict | None = None):
    parsed = parse_declaration(declaration)
    assert parsed.valid and parsed.declaration is not None
    return build_gate(parsed.declaration, schema or AMOUNT_SCHEMA)


class _Channel:
    def __init__(self, *, approve: bool = True) -> None:
        self.approve = approve
        self.confirms: list[tuple[str, dict]] = []

    async def emit(self, event) -> None:  # pragma: no cover
        return None

    async def ask(self, question: str) -> str:  # pragma: no cover
        return ""

    async def confirm(self, tool_name: str, arguments: dict) -> bool:
        self.confirms.append((tool_name, dict(arguments)))
        return self.approve


class _Policy:
    """固定档位策略；``record`` 记录它看到的调用形式（用来证明没被多塞参数）。"""

    def __init__(self, decision: PermissionDecision = PermissionDecision.ALLOW) -> None:
        self.decision = decision
        self.seen: list[dict] = []

    async def check(self, tool_name: str, arguments: dict) -> PermissionDecision:
        self.seen.append({"tool": tool_name, "arguments": dict(arguments)})
        return self.decision


class _SubjectPolicy(_Policy):
    yai_subject_aware = True

    async def check(self, tool_name: str, arguments: dict, *, subject=None):  # type: ignore[override]
        self.seen.append({"tool": tool_name, "subject": subject})
        return self.decision


def _spec(fn, governance: ToolGovernance):
    spec = build_spec(fn)
    spec.governance = governance
    return spec


def _executor(spec, policy, channel):
    registry = ToolRegistry()
    registry.register(spec)
    return ToolExecutor(registry, policy, channel)


def refund(amount: float) -> dict:
    """退款。"""
    return {"refunded": amount}


# --- 主体类型本身 -------------------------------------------------------------


def test_subject_rejects_blank_identity() -> None:
    for bad in ("", "   "):
        try:
            Subject(subject_id=bad)
        except ValueError as exc:
            assert "非空" in str(exc)
        else:  # pragma: no cover - 明确不允许
            raise AssertionError("空主体 ID 应当被拒绝")


def test_subject_is_immutable_snapshot() -> None:
    subject = Subject(subject_id="u-1", display_name="张三")
    with pytest.raises(FrozenInstanceError):
        subject.display_name = "李四"  # type: ignore[misc]


# --- 闸的三种话 ---------------------------------------------------------------


def test_gate_silent_when_declaration_has_no_requirements() -> None:
    gate = _gate({"version": 1, "enabled": True, "scope": "order.read"})
    decision = gate({"amount": 1}, Subject(subject_id="u-1"))
    assert decision == GateDecision(risk="medium", scope="order.read")
    assert decision.reject == "" and decision.require_approval is False


def test_gate_requires_subject_before_anything_else() -> None:
    gate = _gate(
        {"version": 1, "enabled": True, "scope": "refund.create", "subject": {"required": True}}
    )
    hidden = gate({"amount": 10}, None)
    assert "可信操作主体" in hidden.reject
    assert gate({"amount": 10}, Subject(subject_id="u-1")).reject == ""


def test_gate_escalates_on_declared_approval_intent() -> None:
    gate = _gate(
        {
            "version": 1,
            "enabled": True,
            "scope": "refund.create",
            "approval": {
                "required": False,
                "prompt": "为订单 {order_id} 退款 {amount} 元？",
                "when": [{"param": "amount", "op": ">", "value": 1000, "label": "超千元"}],
            },
        },
        {
            "type": "object",
            "properties": {"amount": {"type": "number"}, "order_id": {"type": "string"}},
        },
    )
    big = gate({"amount": 2000, "order_id": "A-1"}, Subject(subject_id="u-1"))
    assert big.require_approval is True
    assert "超千元" in big.reason
    assert "{order_id}" in big.prompt  # 闸给模板，填充在授权闸里做
    small = gate({"amount": 10, "order_id": "A-2"}, Subject(subject_id="u-1"))
    assert small.require_approval is False


def test_gate_rejects_arguments_outside_bound_schema() -> None:
    gate = _gate(
        {
            "version": 1,
            "enabled": True,
            "scope": "refund.create",
            "approval": {"when": [{"param": "amount", "op": ">", "value": 1000}]},
        }
    )
    decision = gate({"amount": "2000"}, Subject(subject_id="u-1"))
    assert "拒绝" in decision.reject
    assert decision.require_approval is False


# --- 执行器集成：闸怎么改事件顺序 ---------------------------------------------


def _run(executor, name, arguments, subject=None):
    return asyncio.run(executor.execute(name, arguments, subject=subject))


def test_gate_approval_escalates_an_allow_policy() -> None:
    channel = _Channel(approve=True)
    policy = _Policy(PermissionDecision.ALLOW)
    spec = _spec(
        refund,
        ToolGovernance(
            gate=_gate(
                {
                    "version": 1,
                    "enabled": True,
                    "scope": "refund.create",
                    "approval": {"required": True},
                }
            )
        ),
    )
    events, ok, _ = _run(_executor(spec, policy, channel), "refund", {"amount": 10})
    assert ok is True
    types = [e.type for e in events]
    # 抬闸后的事件顺序与 ASK 档完全一致：先问、批了才发 TOOL_CALL，最后回结果
    assert types == [EventType.PERMISSION_ASKED, EventType.TOOL_CALL, EventType.TOOL_RESULT]
    asked = events[0]
    assert asked.data["approval_required"] is True
    assert "approval.required" in asked.data["reason"]
    assert channel.confirms and channel.confirms[0][0] == "refund"


def test_gate_approval_declined_runs_nothing() -> None:
    channel = _Channel(approve=False)
    spec = _spec(
        refund,
        ToolGovernance(
            gate=_gate(
                {
                    "version": 1,
                    "enabled": True,
                    "scope": "refund.create",
                    "approval": {"required": True},
                }
            )
        ),
    )
    events, ok, reason = _run(
        _executor(spec, _Policy(PermissionDecision.ALLOW), channel), "refund", {"amount": 10}
    )
    assert ok is False and "拒绝" in reason
    assert [e.type for e in events] == [EventType.PERMISSION_ASKED]


def test_policy_deny_beats_gate_approval() -> None:
    channel = _Channel(approve=True)
    spec = _spec(
        refund,
        ToolGovernance(
            gate=_gate(
                {
                    "version": 1,
                    "enabled": True,
                    "scope": "refund.create",
                    "approval": {"required": True},
                }
            )
        ),
    )
    events, ok, reason = _run(
        _executor(spec, _Policy(PermissionDecision.DENY), channel), "refund", {"amount": 10}
    )
    assert ok is False and "权限策略拒绝" in reason
    assert events == [] and channel.confirms == []


def test_gate_reject_happens_before_policy_and_handler() -> None:
    calls: list[float] = []

    def refund(amount: float) -> dict:
        """退款。"""
        calls.append(amount)
        return {"refunded": amount}

    channel = _Channel()
    policy = _Policy(PermissionDecision.ALLOW)
    spec = _spec(
        refund,
        ToolGovernance(
            gate=_gate(
                {
                    "version": 1,
                    "enabled": True,
                    "scope": "refund.create",
                    "subject": {"required": True},
                }
            )
        ),
    )
    events, ok, reason = _run(_executor(spec, policy, channel), "refund", {"amount": 10})
    assert ok is False and "可信操作主体" in reason
    assert calls == [] and policy.seen == [] and events == []


def test_prompt_placeholder_uses_the_redacted_copy() -> None:
    def refund_with_order(amount: float, order_id: str) -> dict:
        """带订单退款。"""
        return {"refunded": amount, "order_id": order_id}

    spec = _spec(
        refund_with_order,
        ToolGovernance(
            sensitive=("order_id",),
            gate=_gate(
                {
                    "version": 1,
                    "enabled": True,
                    "scope": "refund.create",
                    "approval": {
                        "required": True,
                        "prompt": "确认给 {order_id} 退款 {amount}？",
                    },
                },
                {
                    "type": "object",
                    "properties": {"amount": {"type": "number"}, "order_id": {"type": "string"}},
                },
            ),
        ),
    )
    events, ok, _ = _run(
        _executor(spec, _Policy(PermissionDecision.ALLOW), _Channel()),
        "refund_with_order",
        {"amount": 50, "order_id": "SECRET-1"},
    )
    assert ok is True
    prompt = events[0].data["prompt"]
    assert "SECRET-1" not in prompt and "[redacted" in prompt
    assert "50" in prompt


def test_unknown_placeholder_survives_in_prompt() -> None:
    spec = _spec(
        refund,
        ToolGovernance(
            gate=_gate(
                {
                    "version": 1,
                    "enabled": True,
                    "scope": "refund.create",
                    "approval": {"required": True, "prompt": "退款 {missing} 元？"},
                }
            )
        ),
    )
    events, _ok, _ = _run(
        _executor(spec, _Policy(PermissionDecision.ALLOW), _Channel()), "refund", {"amount": 5}
    )
    assert "{missing}" in events[0].data["prompt"]


# --- 主体透传：只给声明了旗标的策略 -------------------------------------------


def test_subject_reaches_subject_aware_policy_only() -> None:
    plain = _Policy()
    spec = _spec(refund, ToolGovernance())
    _run(_executor(spec, plain, _Channel()), "refund", {"amount": 1}, Subject(subject_id="u-9"))
    assert "subject" not in plain.seen[0]  # 老签名照旧，不会被多塞参数打挂

    aware = _SubjectPolicy()
    _run(_executor(spec, aware, _Channel()), "refund", {"amount": 1}, Subject(subject_id="u-9"))
    assert aware.seen[0]["subject"].subject_id == "u-9"


def test_no_subject_means_no_subject_passed() -> None:
    aware = _SubjectPolicy()
    spec = _spec(refund, ToolGovernance())
    _run(_executor(spec, aware, _Channel()), "refund", {"amount": 1})
    assert aware.seen[0]["subject"] is None


def test_composite_steps_inherit_the_subject() -> None:
    from yai_core.tools.composer import build_composite_spec

    seen: list[dict] = []

    def peek(amount: float) -> dict:
        """看一眼。"""
        seen.append({"amount": amount})
        return {"ok": True}

    registry = ToolRegistry()
    aware = _SubjectPolicy()
    registry.register(_spec(peek, ToolGovernance()))
    registry.register(
        build_composite_spec(
            "peek_twice",
            "看两次",
            [{"tool": "peek", "args": {"amount": "{{$input.amount}}"}}],
        )
    )
    executor = ToolExecutor(registry, aware, _Channel())
    subject = Subject(subject_id="u-7")
    events, ok, _ = _run(executor, "peek_twice", {"amount": 3}, subject)
    assert ok is True and seen == [{"amount": 3}]
    # 每一步都按同一个主体过闸，而不是丢掉主体后按"无主"执行
    # 组合名本身不过闸（用户看到的是黑盒名字），权限精确落到每一步
    assert [row["subject"].subject_id for row in aware.seen] == ["u-7"]


# --- 端到端：主体怎么穿过一次运行 ---------------------------------------------


class ScriptedModel:
    """按预设顺序返回响应的假模型，并记录每次看到的消息（离线）。"""

    def __init__(self, responses: list[ModelResponse]) -> None:
        self._responses = responses
        self.calls = 0
        self.seen_messages: list[list] = []

    async def achat(self, messages, tools=None, *, tier="standard"):
        self.seen_messages.append(list(messages))
        resp = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        return resp


def _tool_call() -> ToolCallRequest:
    return ToolCallRequest(id="c1", name="refund", arguments={"amount": 10})


def _subject_core(
    policy: _SubjectPolicy, rounds: int = 1
) -> tuple[AgentCore, ScriptedModel]:
    """装配一个"退款要可信主体"的 core。

    任务文本刻意带行动信号（"调用"）：规则路由只认行动词，
    "退 10 元"会被判成 direct，测试想钉的闸行为就不会发生。
    """
    responses: list[ModelResponse] = []
    for _ in range(rounds):
        responses.append(ModelResponse(content="", tool_calls=[_tool_call()]))
        responses.append(ModelResponse(content="完成"))
    model = ScriptedModel(responses)
    core = AgentCore(
        model,
        channel=_Channel(),
        policy=policy,
    )
    core.register_tools(
        [
            _spec(
                refund,
                ToolGovernance(
                    gate=_gate(
                        {
                            "version": 1,
                            "enabled": True,
                            "scope": "refund.create",
                            "subject": {"required": True},
                        }
                    )
                ),
            )
        ]
    )
    return core, model


TASK = "调用退款接口退 10 元"


def test_subject_threads_through_each_run_and_never_sticks() -> None:
    policy = _SubjectPolicy()
    core, _model = _subject_core(policy, rounds=3)
    asyncio.run(core.run(TASK, subject=Subject(subject_id="u-1")))
    asyncio.run(core.run(TASK, subject=Subject(subject_id="u-2")))
    assert [row["subject"].subject_id for row in policy.seen] == ["u-1", "u-2"]

    # 第三次不带主体：闸在策略之前拒，身份不沿用上一位
    asyncio.run(core.run(TASK))
    assert [row["subject"].subject_id for row in policy.seen] == ["u-1", "u-2"]


def test_gate_rejection_is_fed_back_to_the_model_not_swallowed() -> None:
    """拒绝没有事件（与权限 DENY 同形），但理由必须作为 tool 消息回灌给模型。"""
    policy = _SubjectPolicy()
    core, model = _subject_core(policy, rounds=1)
    result = asyncio.run(core.run(TASK))
    fed_back = [
        str(message.get("content", ""))
        for turn in model.seen_messages
        for message in turn
        if isinstance(message, dict) and message.get("role") == "tool"
    ]
    assert any("可信操作主体" in text for text in fed_back), fed_back
    assert result.final_text  # 模型拿到拒绝理由后自己收尾，不是一声不响地卡住
    assert policy.seen == []  # 闸先于策略：策略连这次调用都不该看见


def test_concurrent_cores_keep_their_own_subjects() -> None:
    policy_a, policy_b = _SubjectPolicy(), _SubjectPolicy()
    core_a, _ = _subject_core(policy_a, rounds=1)
    core_b, _ = _subject_core(policy_b, rounds=1)

    async def main() -> None:
        await asyncio.gather(
            core_a.run(TASK, subject=Subject(subject_id="张三")),
            core_b.run(TASK, subject=Subject(subject_id="李四")),
        )

    asyncio.run(main())
    assert [row["subject"].subject_id for row in policy_a.seen] == ["张三"]
    assert [row["subject"].subject_id for row in policy_b.seen] == ["李四"]


# --- 决策的描述性字段必须落到审计上 -----------------------------------------


def test_risk_and_scope_reach_the_audit_events() -> None:
    """risk / scope 此前算出来就扔了：审计链看不出"这是高影响写、作用域是什么"。"""
    gate = _gate(
        {
            "version": 1,
            "enabled": True,
            "scope": "refund.create",
            "risk": {"level": "high"},
            "approval": {"required": True, "prompt": "确认退款 {amount} 元？"},
        },
        {
            "type": "object",
            "properties": {"amount": {"type": "number"}},
            "required": ["amount"],
        },
    )
    executor = _executor(
        _spec(refund, ToolGovernance(gate=gate)), _Policy(), _Channel()
    )
    events, ok, _text = asyncio.run(
        executor.execute("refund", {"amount": 50}, subject=Subject(subject_id="u-1"))
    )
    assert ok is True
    asked = next(e for e in events if e.type == EventType.PERMISSION_ASKED)
    called = next(e for e in events if e.type == EventType.TOOL_CALL)
    # 审批人那份与审计那份都要带：前者让人判断该不该批，后者让事后能按风险检索
    assert (asked.data["risk"], asked.data["scope"]) == ("high", "refund.create")
    assert (called.data["risk"], called.data["scope"]) == ("high", "refund.create")


def test_ungoverned_tool_events_keep_their_old_shape() -> None:
    """没声明就不许多出键——未治理工具的事件形状必须与从前逐字一致。"""
    executor = _executor(_spec(refund, ToolGovernance()), _Policy(), _Channel())
    events, ok, _ = asyncio.run(executor.execute("refund", {"amount": 1}))
    assert ok is True
    called = next(e for e in events if e.type == EventType.TOOL_CALL)
    assert set(called.data) == {"tool", "arguments"}
