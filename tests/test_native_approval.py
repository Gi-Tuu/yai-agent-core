"""YAI 原生参数级审批（``governance.approval``）的回归：不接 ACC 也能"超阈值要人确认"。

钉住的语义：

- **判定住在内核**：``policy/approval.py`` 与执行器都不 import ACC 层，宿主只写数据
  就能得到与 ACC 声明完全一致的审批行为；
- **严格 JSON 值**：不做隐式转换；判不出来（算子不认识、路径没声明、没绑定 schema）
  一律失败关闭成"拒绝"，绝不静默放行；
- **只能抬不能放**：审批要求把策略的 ALLOW 抬成人工确认，但永远压不过 DENY；
  ``gate``（函数）与 ``approval``（数据）同时存在时取更严的那个，两份理由都不丢；
- **是数据就得能随清单走**：导出/导入往返一致，未知字段与非法算子拒绝加载。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from yai_core import (
    ApprovalPolicy,
    ApprovalRule,
    GateDecision,
    ToolGovernance,
    build_composite_spec,
    build_spec,
)
from yai_core.integrations.acc.declaration import parse_declaration
from yai_core.integrations.acc.evaluate import approval_outcome, to_approval_policy
from yai_core.policy.approval import (
    DECISION_ALLOW,
    DECISION_APPROVAL_REQUIRED,
    DECISION_REJECT_INVALID_ARGUMENTS,
    ApprovalError,
    argument_errors,
    evaluate_approval,
    rule_matches,
    schema_at_path,
)
from yai_core.spi.policy import PermissionDecision
from yai_core.tools import ToolExecutor, ToolRegistry
from yai_core.types import (
    EventType,
    ToolSpec,
    governance_from_manifest,
    governance_to_manifest,
)

AMOUNT_SCHEMA = {
    "type": "object",
    "properties": {"amount": {"type": "number"}},
    "required": ["amount"],
}


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
    def __init__(self, decision: PermissionDecision = PermissionDecision.ALLOW) -> None:
        self.decision = decision

    async def check(self, tool_name: str, arguments: dict) -> PermissionDecision:
        return self.decision


class _Recorder:
    """记录 handler 到底被没被调用——"没执行"必须由执行侧证明，不能只看返回值。"""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def refund(self, amount: float) -> dict:
        """退款。"""
        self.calls.append(amount)
        return {"refunded": amount}


def _spec(fn, governance: ToolGovernance) -> ToolSpec:
    spec = build_spec(fn)
    spec.governance = governance
    return spec


def _executor(specs, policy, channel) -> ToolExecutor:
    registry = ToolRegistry()
    for spec in specs:
        registry.register(spec)
    return ToolExecutor(registry, policy, channel)


def _run(executor: ToolExecutor, name: str, arguments: dict):
    return asyncio.run(executor.execute(name, arguments))


# --- 内核不认识 ACC（依赖方向） ------------------------------------------------


def test_kernel_approval_path_never_imports_the_acc_layer() -> None:
    """子进程里只导入内核模块，``sys.modules`` 不该出现任何 ``yai_core.integrations``。

    在测试进程里查 ``sys.modules`` 没有意义：别的测试早就导入过 ACC。
    只有干净解释器才能证明"内核不依赖兼容层"这条方向没被人反过来写。
    """
    src = Path(__file__).resolve().parents[1] / "src"
    env = dict(os.environ, PYTHONPATH=str(src))
    probe = (
        "import sys, yai_core, yai_core.policy.approval, yai_core.tools.executor;"
        "leaks=[m for m in sys.modules if m.startswith('yai_core.integrations')];"
        "print(','.join(leaks))"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, env=env, check=True
    )
    assert out.stdout.strip() == "", f"内核拉进了兼容层：{out.stdout}"


# --- 判定语义（纯函数） --------------------------------------------------------


def test_no_policy_is_silent() -> None:
    assert evaluate_approval(None, {"amount": 1}, AMOUNT_SCHEMA).decision == DECISION_ALLOW


def test_required_dominates_unmatched_conditions() -> None:
    policy = ApprovalPolicy(
        required=True, when=(ApprovalRule(param="amount", op=">", value=1000),)
    )
    outcome = evaluate_approval(policy, {"amount": 1}, AMOUNT_SCHEMA)
    assert outcome.decision == DECISION_APPROVAL_REQUIRED
    assert outcome.matched == ()
    assert "人工确认" in outcome.reason


def test_any_matched_rule_is_reported_with_labels() -> None:
    schema = {
        "type": "object",
        "properties": {"amount": {"type": "number"}, "priority": {"type": "string"}},
    }
    policy = ApprovalPolicy(
        when=(
            ApprovalRule(param="amount", op=">", value=1000, label="金额超限"),
            ApprovalRule(param="priority", op="==", value="urgent", label="加急"),
        )
    )
    outcome = evaluate_approval(policy, {"amount": 2000, "priority": "urgent"}, schema)
    assert outcome.decision == DECISION_APPROVAL_REQUIRED
    assert [rule.label for rule in outcome.matched] == ["金额超限", "加急"]
    assert "金额超限" in outcome.reason and "加急" in outcome.reason


def test_comparison_never_coerces_types() -> None:
    """字符串 "1" 不等于数字 1：隐式转换等于给"参数写成字符串"开一条绕过金额门槛的路。"""
    schema = {"type": "object", "properties": {"code": {"type": "string"}}}
    policy = ApprovalPolicy(when=(ApprovalRule(param="code", op="==", value=1),))
    assert evaluate_approval(policy, {"code": "1"}, schema).decision == DECISION_ALLOW
    same = ApprovalPolicy(when=(ApprovalRule(param="code", op="==", value="1"),))
    assert evaluate_approval(same, {"code": "1"}, schema).decision == DECISION_APPROVAL_REQUIRED


#: 每个用例配一个"参数合法、路径已声明"的 schema，让失败关闭**只因它该只因的原因**。
_JUDGEMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "amount": {"type": "number"},
        "name": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
}


@pytest.mark.parametrize(
    ("rule", "args", "cause"),
    [
        (ApprovalRule(param="amount", op="=>", value=1), {"amount": 5}, "算子不被支持"),
        (ApprovalRule(param="amount", op=">", value="x"), {"amount": 5}, "有限 number"),
        (
            ApprovalRule(param="amount", op=">", value=float("nan")),
            {"amount": 5},
            "有限 number",
        ),
        (
            ApprovalRule(param="amount", op=">", value=float("inf")),
            {"amount": 5},
            "有限 number",
        ),
        (ApprovalRule(param="amount", op="in", value=5), {"amount": 5}, "必须是 array"),
        (
            ApprovalRule(param="amount", op="contains", value="x"),
            {"amount": 5},
            "contains 只适用于",
        ),
        (ApprovalRule(param="nope", op="exists"), {"amount": 5}, "未在绑定 schema 中声明"),
    ],
)
def test_undecidable_conditions_fail_closed_for_the_stated_reason(
    rule: ApprovalRule, args: dict, cause: str
) -> None:
    """判不出来就拒，而且**理由必须说清是哪一个判不出来**。

    只断言"结果是拒"会假绿：入参校验那一步也能拒。这里每个用例的 args 都是合法值、
    路径都在 schema 里，拒绝只能来自被钉的那一条。
    """
    outcome = evaluate_approval(ApprovalPolicy(when=(rule,)), args, _JUDGEMENT_SCHEMA)
    assert outcome.decision == DECISION_REJECT_INVALID_ARGUMENTS
    assert cause in outcome.reason


def test_matched_and_unmatched_rules_mix() -> None:
    """ANY 语义下只报命中的那些；全不中才是 allow。"""
    policy = ApprovalPolicy(
        when=(
            ApprovalRule(param="amount", op=">", value=1000, label="超限"),
            ApprovalRule(param="name", op="contains", value="vip"),
        )
    )
    hit = evaluate_approval(policy, {"amount": 5, "name": "the-vip-list"}, _JUDGEMENT_SCHEMA)
    assert hit.decision == DECISION_APPROVAL_REQUIRED
    assert [rule.param for rule in hit.matched] == ["name"]
    miss = evaluate_approval(policy, {"amount": 5, "name": "guest"}, _JUDGEMENT_SCHEMA)
    assert miss.decision == DECISION_ALLOW
    assert "没有命中" in miss.reason


def test_condition_without_binding_schema_fails_closed() -> None:
    """没有绑定 schema 就没有"这个参数是什么"的依据；放行会让拼错的路径永远不命中。"""
    policy = ApprovalPolicy(when=(ApprovalRule(param="amount", op=">", value=1),))
    outcome = evaluate_approval(policy, {"amount": 2}, None)
    assert outcome.decision == DECISION_REJECT_INVALID_ARGUMENTS
    assert "绑定" in outcome.reason


def test_required_without_schema_still_asks() -> None:
    """无条件要求不依赖任何参数：没有 schema 也照样要问人。"""
    outcome = evaluate_approval(ApprovalPolicy(required=True), {"amount": 2}, None)
    assert outcome.decision == DECISION_APPROVAL_REQUIRED


def test_numeric_compare_on_a_declared_string_fails_closed() -> None:
    """schema 把参数声明成 string，规则却要按大小比：判定不能"把它当数字试试"。

    这条走的是"值本身没法比"而不是"入参类型不符"——前面几步都得放行，才能证明
    失败关闭来自比较那一层。
    """
    schema = {"type": "object", "properties": {"amount": {"type": "string"}}}
    policy = ApprovalPolicy(when=(ApprovalRule(param="amount", op=">", value=1),))
    outcome = evaluate_approval(policy, {"amount": "5"}, schema)
    assert outcome.decision == DECISION_REJECT_INVALID_ARGUMENTS
    assert "数值比较要求 JSON number" in outcome.reason


def test_rule_matches_rejects_unknown_operator_directly() -> None:
    """`rule_matches` 是公开的，可能被宿主单独拿去用：不认识算子时必须抛错，不能返回 False。

    `evaluate_approval` 在更早一步就把非法算子拦掉了，所以这条路只能直接测——
    返回 False 等于"条件不匹配 → 放行"，正是失败关闭要挡的那个方向。
    """
    with pytest.raises(ApprovalError, match="不支持的比较算子"):
        rule_matches(ApprovalRule(param="amount", op="≈", value=1), {"amount": 1})


def test_argument_subset_validator_edges() -> None:
    """入参子集校验的几条边角：boolean、bool 不是 integer、schema 形状不对、路径断在中途。"""
    schema = {
        "type": "object",
        "properties": {"flag": {"type": "boolean"}, "count": {"type": "integer"}},
    }
    assert argument_errors({"flag": True, "count": 2}, schema) == []
    assert any("flag" in e for e in argument_errors({"flag": "yes"}, schema))
    # Python 里 True == 1，JSON 里布尔不是整数——放过去就会让 {"count": true} 当成 1 过闸。
    assert any("count" in e for e in argument_errors({"count": True}, schema))
    # 整份 schema 不是 object：不猜、不报错，交回"这层没意见"。
    assert argument_errors({"a": 1}, ["不是 object"]) == []
    # 中间节点没有 properties：路径解析不出来，条件那一步会失败关闭。
    assert schema_at_path({"type": "object"}, "a") is None


def test_exists_treats_json_null_as_absent() -> None:
    policy = ApprovalPolicy(when=(ApprovalRule(param="memo", op="exists"),))
    schema = {"type": "object", "properties": {"memo": {"type": ["string", "null"]}}}
    assert evaluate_approval(policy, {"memo": None}, schema).decision == DECISION_ALLOW
    assert evaluate_approval(policy, {"memo": "x"}, schema).decision == (
        DECISION_APPROVAL_REQUIRED
    )


def test_equal_to_null_is_a_real_value_not_a_missing_path() -> None:
    """``== null`` 与"路径不存在"必须能区分：靠 has_value，不靠 value is None。"""
    schema = {"type": "object", "properties": {"assignee": {"type": ["string", "null"]}}}
    to_null = ApprovalPolicy(
        when=(ApprovalRule(param="assignee", op="==", value=None, has_value=True),)
    )
    assert evaluate_approval(to_null, {"assignee": None}, schema).decision == (
        DECISION_APPROVAL_REQUIRED
    )
    missing_value_rule = ApprovalPolicy(
        when=(ApprovalRule(param="assignee", op="==", value=None, has_value=False),)
    )
    # has_value 只影响清单形状；判定仍按 value 走，null == null 成立。
    assert evaluate_approval(missing_value_rule, {"assignee": None}, schema).decision == (
        DECISION_APPROVAL_REQUIRED
    )


# --- 与 ACC 判定一字不差（parity） --------------------------------------------


@pytest.mark.parametrize(
    "op,value,args,expected",
    [
        (">", 1000, {"amount": 2000}, DECISION_APPROVAL_REQUIRED),
        (">", 1000, {"amount": 500}, DECISION_ALLOW),
        (">=", 1000, {"amount": 1000}, DECISION_APPROVAL_REQUIRED),
        ("<", 10, {"amount": 20}, DECISION_ALLOW),
        ("<=", 10, {"amount": 10}, DECISION_APPROVAL_REQUIRED),
        ("==", 1000, {"amount": 1000}, DECISION_APPROVAL_REQUIRED),
        ("==", 1000, {"amount": "1000"}, DECISION_REJECT_INVALID_ARGUMENTS),
        ("!=", 1000, {"amount": 900}, DECISION_APPROVAL_REQUIRED),
        ("in", [1, 2], {"amount": 2}, DECISION_APPROVAL_REQUIRED),
        ("exists", None, {"amount": 1}, DECISION_APPROVAL_REQUIRED),
        ("contains", "abc", {"amount": 1}, DECISION_REJECT_INVALID_ARGUMENTS),
        (">", "1000", {"amount": 1}, DECISION_REJECT_INVALID_ARGUMENTS),
    ],
)
def test_native_and_acc_reach_the_same_decision(
    op: str, value: Any, args: dict, expected: str
) -> None:
    """同一份声明、同一个入参：ACC 路径与原生路径的结论必须完全相同。

    这条测试的意义在于"只有一份判定"：如果哪天有人把 ACC 层再拆出一套自己的比较逻辑，
    这里会先红，而不是在生产上出现"接了 ACC 的宿主行为不一样"。
    """
    raw: dict[str, Any] = {"param": "amount", "op": op}
    if value is not None:
        raw["value"] = value
    declaration = parse_declaration(
        {"version": 1, "enabled": True, "scope": "refund.create", "approval": {"when": [raw]}}
    )
    assert declaration.valid and declaration.declaration is not None
    acc_side = approval_outcome(declaration.declaration, args, AMOUNT_SCHEMA)
    native_side = evaluate_approval(
        to_approval_policy(declaration.declaration), args, AMOUNT_SCHEMA
    )
    assert acc_side.decision == expected
    assert native_side.decision == acc_side.decision
    assert native_side.reason == acc_side.reason


def test_translator_maps_every_approval_field() -> None:
    declaration = parse_declaration(
        {
            "version": 1,
            "enabled": True,
            "scope": "refund.create",
            "approval": {
                "required": True,
                "prompt": "退款 {amount} 元，确认吗？",
                "when": [{"param": "amount", "op": ">", "value": 1000, "label": "超限"}],
            },
        }
    )
    assert declaration.valid and declaration.declaration is not None
    policy = to_approval_policy(declaration.declaration)
    assert policy.required is True
    assert policy.prompt == "退款 {amount} 元，确认吗？"
    assert policy.when == (ApprovalRule(param="amount", op=">", value=1000, label="超限"),)


# --- 清单往返（数据就得能走） --------------------------------------------------


def _manifest_round_trip(governance: ToolGovernance) -> dict[str, Any]:
    spec = ToolSpec(
        name="refund",
        description="退款",
        input_schema=AMOUNT_SCHEMA,
        handler=None,
        governance=governance,
    )
    return ToolSpec.from_manifest_dict(spec.to_manifest_dict()).governance


def test_approval_survives_manifest_round_trip() -> None:
    policy = ApprovalPolicy(
        required=True,
        prompt="确认退款？",
        when=(
            ApprovalRule(param="amount", op=">", value=1000, label="超限"),
            ApprovalRule(param="memo", op="exists"),
            ApprovalRule(param="assignee", op="==", value=None, has_value=True),
        ),
    )
    restored = _manifest_round_trip(ToolGovernance(approval=policy))
    assert restored.approval == policy


def test_trivial_approval_writes_no_key() -> None:
    """空审批声明不该在清单里留下噪声字段，也不该在导入端产生"审批要求"。"""
    spec = ToolSpec(
        name="refund",
        description="退款",
        input_schema=AMOUNT_SCHEMA,
        handler=None,
        governance=ToolGovernance(approval=ApprovalPolicy()),
    )
    assert "approval" not in spec.to_manifest_dict().get("governance", {})
    assert _manifest_round_trip(ToolGovernance(approval=ApprovalPolicy())).approval is None


def test_manifest_rejects_unknown_approval_fields_and_ops() -> None:
    with pytest.raises(ValueError, match="不认识的字段"):
        governance_from_manifest("refund", {"approval": {"requird": True}})
    with pytest.raises(ValueError, match="算子非法"):
        governance_from_manifest(
            "refund", {"approval": {"when": [{"param": "amount", "op": "=>"}]}}
        )
    with pytest.raises(ValueError, match="未知字段"):
        governance_from_manifest(
            "refund", {"approval": {"when": [{"param": "a", "op": "exists", "vale": 1}]}}
        )


@pytest.mark.parametrize(
    ("raw", "cause"),
    [
        ("不是 object", "必须是 object"),
        ({"required": "yes"}, "必须是布尔值"),
        ({"prompt": 3}, "必须是字符串"),
        ({"when": {"param": "a", "op": "exists"}}, "必须是数组"),
        ({"when": ["不是对象"]}, "必须是 object"),
        ({"when": [{"op": "exists"}]}, "缺少合法 param"),
        ({"when": [{"param": "  ", "op": "exists"}]}, "缺少合法 param"),
        ({"when": [{"param": "a", "op": "exists", "label": 3}]}, "label 必须是字符串"),
    ],
)
def test_inbound_approval_validation_rejects_each_malformed_shape(
    raw: Any, cause: str
) -> None:
    """入站审批块逐条形状都要拒——**每条都要报出它自己的原因**。

    这份数据来自别的进程/别的语言，而它会真的参与准入：让 `required: "yes"` 混进来，
    真值判定就变成"非空字符串为真"，一个拼错类型的清单能把闸门悄悄改掉。
    """
    with pytest.raises(ValueError, match=cause):
        governance_from_manifest("refund", {"approval": raw})


def test_manifest_value_key_presence_is_the_missing_marker() -> None:
    """``exists``（无值）与 ``== null``（值是 null）在清单里靠"有没有 value 键"区分。"""

    exists_only = governance_to_manifest(
        ToolGovernance(approval=ApprovalPolicy(when=(ApprovalRule(param="memo", op="exists"),)))
    )
    null_value = governance_to_manifest(
        ToolGovernance(
            approval=ApprovalPolicy(
                when=(ApprovalRule(param="memo", op="==", value=None, has_value=True),)
            )
        )
    )
    assert exists_only["approval"]["when"][0] == {"param": "memo", "op": "exists"}
    assert null_value["approval"]["when"][0] == {"param": "memo", "op": "==", "value": None}


# --- 执行器接线 ---------------------------------------------------------------


def test_future_annotations_do_not_poison_the_bound_schema() -> None:
    """本文件开头就写了 ``from __future__ import annotations``——这正是宿主最常见的写法。

    内省若把 ``amount: float`` 看成字符串 ``"float"`` 而退化成 ``string``，下面的审批
    用例全会变成"合法调用被判非法"。这条测试钉住那个真实踩过的坑。
    """
    spec = build_spec(_Recorder().refund)
    assert spec.input_schema["properties"]["amount"]["type"] == "number"


def test_native_approval_asks_under_an_allow_policy() -> None:
    rec = _Recorder()
    channel = _Channel(approve=True)
    policy = ApprovalPolicy(
        when=(ApprovalRule(param="amount", op=">", value=1000),),
        prompt="退款 {amount} 元要人确认，批吗？",
    )
    executor = _executor(
        [_spec(rec.refund, ToolGovernance(approval=policy))], _Policy(), channel
    )
    events, ok, _ = _run(executor, "refund", {"amount": 2000})

    assert ok is True
    assert [e.type for e in events] == [
        EventType.PERMISSION_ASKED,
        EventType.TOOL_CALL,
        EventType.TOOL_RESULT,
    ]
    asked = events[0].data
    assert asked["approval_required"] is True
    assert "命中审批条件" in asked["reason"]
    assert asked["prompt"] == "退款 2000 元要人确认，批吗？"
    assert rec.calls == [2000]


def test_below_the_threshold_runs_without_asking() -> None:
    rec = _Recorder()
    channel = _Channel(approve=True)
    policy = ApprovalPolicy(when=(ApprovalRule(param="amount", op=">", value=1000),))
    executor = _executor(
        [_spec(rec.refund, ToolGovernance(approval=policy))], _Policy(), channel
    )
    events, ok, _ = _run(executor, "refund", {"amount": 500})

    assert ok is True
    assert EventType.PERMISSION_ASKED not in [e.type for e in events]
    assert channel.confirms == []


def test_control_without_the_declaration_the_same_call_just_runs() -> None:
    """反假绿：同一个工具、同一个参数，不声明 approval 时既不问人也不被拒。

    没有这条，上面"问了一次"的绿可能只是工具本来就跑不起来。
    """
    rec = _Recorder()
    executor = _executor([_spec(rec.refund, ToolGovernance())], _Policy(), _Channel())
    events, ok, _ = _run(executor, "refund", {"amount": 2000})
    assert ok is True
    assert [e.type for e in events] == [EventType.TOOL_CALL, EventType.TOOL_RESULT]
    assert rec.calls == [2000]


def test_permission_deny_still_beats_the_approval_requirement() -> None:
    """审批只会抬闸门：策略 DENY 时，required 再硬也到不了问人那一步。"""
    rec = _Recorder()
    channel = _Channel(approve=True)
    executor = _executor(
        [_spec(rec.refund, ToolGovernance(approval=ApprovalPolicy(required=True)))],
        _Policy(PermissionDecision.DENY),
        channel,
    )
    events, ok, text = _run(executor, "refund", {"amount": 1})
    assert ok is False
    assert "权限策略拒绝" in text
    assert channel.confirms == []
    assert rec.calls == []


def test_declined_approval_runs_nothing() -> None:
    rec = _Recorder()
    channel = _Channel(approve=False)
    executor = _executor(
        [_spec(rec.refund, ToolGovernance(approval=ApprovalPolicy(required=True)))],
        _Policy(),
        channel,
    )
    events, ok, text = _run(executor, "refund", {"amount": 1})
    assert ok is False
    assert "拒绝执行" in text
    assert EventType.TOOL_CALL not in [e.type for e in events]
    assert rec.calls == []


def test_invalid_arguments_are_rejected_before_the_handler() -> None:
    """条件要用参数，而参数类型不对 → 拒。判定顺序是"入参先于条件"，业务操作不许跑。"""
    rec = _Recorder()
    channel = _Channel(approve=True)
    policy = ApprovalPolicy(when=(ApprovalRule(param="amount", op=">", value=1000),))
    executor = _executor(
        [_spec(rec.refund, ToolGovernance(approval=policy))], _Policy(), channel
    )
    events, ok, text = _run(executor, "refund", {"amount": "1500"})
    assert ok is False
    assert "审批闸拒绝" in text
    assert channel.confirms == []
    assert rec.calls == []


def test_positional_governance_still_binds_the_gate() -> None:
    """历史位置写法里第 4 个参数是 ``gate``；新字段必须排在它**后面**。

    插在中间不会报错，只会把宿主绑好的闸门静默挪到 ``approval`` 上、``gate`` 变 None——
    闸门消失得无声无息，正是最难查的那类回归。
    """

    def gate(arguments: dict, subject: Any) -> GateDecision:
        return GateDecision(require_approval=True)

    governance = ToolGovernance(("api_key",), False, None, gate)
    assert governance.gate is gate
    assert governance.approval is None


def test_gate_and_approval_are_merged_not_overwritten() -> None:
    """函数闸与数据闸同时存在时取更严的：两个来源的理由都必须出现在审批事件里。"""

    def gate(arguments: dict, subject: Any) -> GateDecision:
        return GateDecision(require_approval=True, reason="函数闸说：先看一眼", risk="high")

    policy = ApprovalPolicy(when=(ApprovalRule(param="amount", op=">", value=1000),))
    rec = _Recorder()
    channel = _Channel(approve=True)
    executor = _executor(
        [
            _spec(
                rec.refund,
                ToolGovernance(gate=gate, approval=policy),
            )
        ],
        _Policy(),
        channel,
    )
    events, ok, _ = _run(executor, "refund", {"amount": 2000})
    assert ok is True
    asked = next(e for e in events if e.type == EventType.PERMISSION_ASKED)
    assert asked.data["risk"] == "high"
    assert "函数闸说" in asked.data["reason"]
    assert "命中审批条件" in asked.data["reason"]
    # 只问一次：合并的是判定，不是把审批跑两遍。
    assert len([e for e in events if e.type == EventType.PERMISSION_ASKED]) == 1


def test_gate_rejection_wins_over_an_approval_requirement() -> None:
    rec = _Recorder()
    channel = _Channel(approve=True)

    def gate(arguments: dict, subject: Any) -> GateDecision:
        return GateDecision(reject="函数闸直接拒绝")

    executor = _executor(
        [
            _spec(
                rec.refund,
                ToolGovernance(gate=gate, approval=ApprovalPolicy(required=True)),
            )
        ],
        _Policy(),
        channel,
    )
    events, ok, text = _run(executor, "refund", {"amount": 1})
    assert ok is False
    assert "函数闸直接拒绝" in text
    assert channel.confirms == []
    assert rec.calls == []


def test_composite_can_declare_its_own_approval() -> None:
    """组合工具自己写了审批要求 → 开工前真的问一次；静默丢掉这份声明就是骗人。"""
    rec = _Recorder()
    channel = _Channel(approve=True)
    composite = build_composite_spec(
        "flow",
        "退款一遍",
        [{"tool": "refund", "args": {"amount": "{{$input.amount}}"}}],
    )
    composite.governance = ToolGovernance(approval=ApprovalPolicy(required=True))
    executor = _executor([build_spec(rec.refund), composite], _Policy(), channel)

    events, ok, _ = _run(executor, "flow", {"amount": 3})
    assert ok is True
    types = [e.type for e in events]
    assert types[0] == EventType.PERMISSION_ASKED
    assert types[1] == EventType.TOOL_COMPOSED
    asked = events[0].data
    assert asked["tool"] == "flow"
    assert asked["approval_required"] is True
    assert asked["steps"] == ["refund"]


def test_composite_approval_declined_runs_no_steps() -> None:
    rec = _Recorder()

    def refund(amount: float) -> dict:
        """退款。"""
        rec.calls.append(amount)
        return {"refunded": amount}

    composite = build_composite_spec(
        "flow",
        "退款一遍",
        [{"tool": "refund", "args": {"amount": "{{$input.amount}}"}}],
    )
    composite.governance = ToolGovernance(approval=ApprovalPolicy(required=True))
    channel = _Channel(approve=False)
    executor = _executor([build_spec(refund), composite], _Policy(), channel)

    events, ok, text = _run(executor, "flow", {"amount": 3})
    assert ok is False
    assert "拒绝执行" in text
    assert rec.calls == []
    assert EventType.TOOL_COMPOSED not in [e.type for e in events]
