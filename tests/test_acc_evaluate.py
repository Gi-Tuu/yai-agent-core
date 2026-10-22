"""ACC 治理决策原语的边界测试：暴露 / 有效风险 / 审计提示 / 参数级审批。

官方语料的 21 条 runtime 向量给的是"抽样值"，这里补上游 COVERAGE.md 点名要的
"边界与非法输入"组合：算子全覆盖、严格 JSON 类型不转换、判不出来时失败关闭。
"""

from __future__ import annotations

from typing import Any

import pytest

from yai_core.integrations.acc.declaration import AccDeclaration, parse_declaration
from yai_core.integrations.acc.evaluate import (
    DECISION_ALLOW,
    DECISION_APPROVAL_REQUIRED,
    DECISION_REJECT_INVALID_ARGUMENTS,
    approval_outcome,
    audit_sensitive,
    effective_risk,
    invocation_errors,
    is_exposed,
    same_json_value,
    schema_at_path,
    value_at_path,
)

_AMOUNT_SCHEMA = {
    "type": "object",
    "properties": {"amount": {"type": "number"}},
    "required": ["amount"],
}


def _decl(**raw: Any) -> AccDeclaration:
    base: dict[str, Any] = {"version": 1, "enabled": True, "scope": "order.read"}
    base.update(raw)
    result = parse_declaration(base)
    assert result.valid and result.declaration is not None
    return result.declaration


# --- 暴露 ---------------------------------------------------------------------


def test_exposure_needs_every_condition() -> None:
    declaration = _decl(subject={"required": True})
    assert is_exposed(declaration, scope_allowed=True, trusted_subject=True) is True
    assert is_exposed(declaration, scope_allowed=False, trusted_subject=True) is False
    assert is_exposed(declaration, scope_allowed=True, trusted_subject=False) is False


def test_disabled_capability_is_never_exposed() -> None:
    declaration = _decl(enabled=False)
    assert is_exposed(declaration, scope_allowed=True, trusted_subject=True) is False


def test_missing_binding_input_schema_hides_the_capability() -> None:
    """绑定要求"入参 schema 足够"才暴露：没有 schema 就不能把操作当成可调能力。"""
    hidden = is_exposed(_decl(), scope_allowed=True, trusted_subject=True, has_input_schema=False)
    assert hidden is False


def test_subject_not_required_ignores_trusted_subject() -> None:
    assert is_exposed(_decl(), scope_allowed=True, trusted_subject=False) is True


# --- 有效风险 -----------------------------------------------------------------


def test_explicit_risk_level_beats_every_default() -> None:
    declaration = _decl(risk={"level": "high"}, execution={"readonly": True})
    assert effective_risk(declaration, http_method="GET") == "high"


@pytest.mark.parametrize(
    "raw,method,expected",
    [
        ({}, "GET", "low"),
        ({}, "POST", "medium"),
        ({"execution": {"readonly": True}}, "POST", "low"),
        ({"execution": {"idempotent": True}}, "POST", "medium"),
        # 文档化的刻意偏离：显式声明非只读的 GET 取 medium（参考 oracle 给 low）。
        ({"execution": {"readonly": False}}, "GET", "medium"),
    ],
)
def test_risk_defaults(raw: dict[str, Any], method: str, expected: str) -> None:
    assert effective_risk(_decl(**raw), http_method=method) == expected


def test_head_is_not_an_acc_native_readonly_signal() -> None:
    """绑定只把 GET 当作原生只读信号；HEAD 是 YAI 自己的只读集合，不影响 ACC 默认风险。"""
    assert effective_risk(_decl(), http_method="HEAD") == "medium"


def test_risk_default_without_http_method() -> None:
    assert effective_risk(_decl()) == "medium"


# --- 审计提示 -----------------------------------------------------------------


def test_audit_hint_only_true_when_declared() -> None:
    assert audit_sensitive(_decl()) is False
    assert audit_sensitive(_decl(audit={"sensitive": False})) is False
    assert audit_sensitive(_decl(audit={"sensitive": True})) is True


# --- 严格 JSON 值模型 ---------------------------------------------------------


@pytest.mark.parametrize(
    "left,right",
    [
        (True, "true"),
        (1, "1"),
        (0, False),
        (None, False),
        (None, "null"),
        ([1], ["1"]),
        ([1], [True]),
        ({"a": 1}, {"a": "1"}),
        ({"a": 1}, {"a": 1, "b": None}),
        ("x", ["x"]),
    ],
)
def test_no_implicit_coercion(left: Any, right: Any) -> None:
    assert same_json_value(left, right) is False
    assert same_json_value(right, left) is False


def test_strict_equality_positive_cases() -> None:
    assert same_json_value(1, 1.0) is True
    assert same_json_value(None, None) is True
    assert same_json_value(True, True) is True
    assert same_json_value([1, "a"], [1, "a"]) is True
    assert same_json_value({"a": 1, "b": 2}, {"b": 2, "a": 1}) is True
    assert same_json_value([1, 2], [2, 1]) is False


def test_value_at_path_only_addresses_object_members() -> None:
    args = {"billing": {"tier": "gold"}, "roles": [{"name": "admin"}]}
    assert value_at_path(args, "billing.tier") == "gold"
    # 点号路径不按下标寻址数组：与参考 oracle 一致，取不到就是同一个缺失标记。
    missing = value_at_path(args, "roles.0")
    assert missing is value_at_path(args, "billing.missing") is value_at_path(args, "nope.deep")


def test_schema_at_path_descends_into_array_items() -> None:
    role_schema = {"type": "object", "properties": {"name": {"type": "string"}}}
    schema = {
        "type": "object",
        "properties": {"roles": {"type": "array", "items": role_schema}},
    }
    assert schema_at_path(schema, "roles.name") == {"type": "string"}
    assert schema_at_path(schema, "roles.nickname") is None
    assert schema_at_path(schema, "nope") is None


# --- 审批：要求与 ANY ---------------------------------------------------------


def test_unconditional_requirement_is_dominant() -> None:
    raw = {"approval": {"required": True, "when": [{"param": "amount", "op": ">", "value": 1000}]}}
    outcome = approval_outcome(_decl(scope="refund.create", **raw), {"amount": 1}, _AMOUNT_SCHEMA)
    assert outcome.decision == DECISION_APPROVAL_REQUIRED
    # 不命中任何 when，也必须照样要问人。
    assert outcome.matched == ()
    assert "人工确认" in outcome.reason


def test_empty_when_creates_no_intent() -> None:
    outcome = approval_outcome(
        _decl(scope="refund.create", approval={"when": []}), {"amount": 1500}, _AMOUNT_SCHEMA
    )
    assert outcome.decision == DECISION_ALLOW


def test_any_semantics_reports_every_matched_condition() -> None:
    declaration = _decl(
        scope="refund.create",
        approval={
            "when": [
                {"param": "amount", "op": ">", "value": 1000, "label": "金额超限"},
                {"param": "priority", "op": "==", "value": "urgent", "label": "加急"},
            ]
        },
    )
    schema = {
        "type": "object",
        "properties": {"amount": {"type": "number"}, "priority": {"type": "string"}},
    }
    outcome = approval_outcome(declaration, {"amount": 2000, "priority": "urgent"}, schema)
    assert outcome.decision == DECISION_APPROVAL_REQUIRED
    assert [c.label for c in outcome.matched] == ["金额超限", "加急"]


# --- 审批：九个算子 -----------------------------------------------------------


@pytest.mark.parametrize(
    "op,value,args,expected",
    [
        (">", 1000, {"amount": 1001}, DECISION_APPROVAL_REQUIRED),
        (">", 1000, {"amount": 1000}, DECISION_ALLOW),
        (">=", 1000, {"amount": 1000}, DECISION_APPROVAL_REQUIRED),
        ("<", 1000, {"amount": 999}, DECISION_APPROVAL_REQUIRED),
        ("<=", 1000, {"amount": 1000}, DECISION_APPROVAL_REQUIRED),
        ("==", "urgent", {"priority": "urgent"}, DECISION_APPROVAL_REQUIRED),
        ("==", "urgent", {"priority": "normal"}, DECISION_ALLOW),
        ("!=", "urgent", {"priority": "normal"}, DECISION_APPROVAL_REQUIRED),
        ("in", ["support", "finance"], {"team": "finance"}, DECISION_APPROVAL_REQUIRED),
        ("in", ["support", "finance"], {"team": "legal"}, DECISION_ALLOW),
        ("contains", "secret", {"message": "a secret here"}, DECISION_APPROVAL_REQUIRED),
        ("contains", "secret", {"message": "clean"}, DECISION_ALLOW),
        ("exists", None, {"reason": "manual"}, DECISION_APPROVAL_REQUIRED),
        ("exists", None, {"reason": None}, DECISION_ALLOW),
    ],
)
def test_operator_matrix(op: str, value: Any, args: dict[str, Any], expected: str) -> None:
    param = next(iter(args))
    condition: dict[str, Any] = {"param": param, "op": op}
    if op != "exists":
        condition["value"] = value
    # 类型声明放宽成并集：矩阵钉的是算子语义，不是入参类型校验。
    schema = {
        "type": "object",
        "properties": {param: {"type": ["number", "string", "array", "null"]}},
    }
    outcome = approval_outcome(_decl(scope="x.y", approval={"when": [condition]}), args, schema)
    assert outcome.decision == expected


def test_in_does_not_coerce_number_into_string() -> None:
    declaration = _decl(
        scope="case.assign", approval={"when": [{"param": "code", "op": "in", "value": ["1", "2"]}]}
    )
    schema = {"type": "object", "properties": {"code": {"type": ["number", "string"]}}}
    assert approval_outcome(declaration, {"code": 1}, schema).decision == DECISION_ALLOW


def test_contains_on_array_uses_strict_equality() -> None:
    declaration = _decl(
        scope="role.update",
        approval={
            "when": [
                {
                    "param": "roles",
                    "op": "contains",
                    "value": {"name": "admin", "active": True},
                }
            ]
        },
    )
    schema = {
        "type": "object",
        "properties": {"roles": {"type": "array", "items": {"type": "object"}}},
    }
    outcome = approval_outcome(declaration, {"roles": [{"active": True, "name": "admin"}]}, schema)
    assert outcome.decision == DECISION_APPROVAL_REQUIRED


def test_nested_param_path_matches_object_member() -> None:
    declaration = _decl(
        scope="order.update",
        approval={"when": [{"param": "billing.tier", "op": "==", "value": "gold"}]},
    )
    schema = {
        "type": "object",
        "properties": {
            "billing": {"type": "object", "properties": {"tier": {"type": "string"}}}
        },
    }
    outcome = approval_outcome(declaration, {"billing": {"tier": "gold"}}, schema)
    assert outcome.decision == DECISION_APPROVAL_REQUIRED


def test_absent_path_with_not_equal_still_creates_intent() -> None:
    """`!=` 对"未提供的参数"成立：宁可多问一次，不静默放行。"""
    declaration = _decl(
        scope="order.update", approval={"when": [{"param": "reason", "op": "!=", "value": "auto"}]}
    )
    schema = {"type": "object", "properties": {"reason": {"type": "string"}}}
    assert approval_outcome(declaration, {}, schema).decision == DECISION_APPROVAL_REQUIRED


# --- 审批：失败关闭 -----------------------------------------------------------


_REJECT = DECISION_REJECT_INVALID_ARGUMENTS
_ASK = DECISION_APPROVAL_REQUIRED

_FAIL_CLOSED: list[tuple[dict[str, Any], dict[str, Any], str]] = [
    # 数值比较的 value 不是 number：判不出来 = 拒绝，绝不静默 allow。
    ({"param": "amount", "op": ">", "value": "1000"}, {"amount": 1500}, _REJECT),
    # 调用值是字符串而算子要求 number：入参校验先拦下。
    ({"param": "amount", "op": ">", "value": 1000}, {"amount": "1500"}, _REJECT),
    # in 的 value 必须是 array。
    ({"param": "team", "op": "in", "value": "support"}, {"team": "support"}, _REJECT),
    # contains 只适用于 string / array 参数。
    ({"param": "amount", "op": "contains", "value": "x"}, {"amount": 1500}, _REJECT),
    # NaN 不是有限 JSON number：入参校验先拒（Infinity 同理）。
    ({"param": "amount", "op": ">", "value": 1000}, {"amount": float("nan")}, _REJECT),
    # 对照组：同一 schema 下合法命中，证明上面几条不是"一律拒绝"。
    ({"param": "team", "op": "==", "value": "support"}, {"team": "support"}, _ASK),
]


@pytest.mark.parametrize("condition,args,expected", _FAIL_CLOSED)
def test_undecidable_conditions_fail_closed(
    condition: dict[str, Any], args: dict[str, Any], expected: str
) -> None:
    declaration = _decl(scope="refund.create", approval={"when": [condition]})
    schema = {
        "type": "object",
        "properties": {"amount": {"type": "number"}, "team": {"type": "string"}},
    }
    outcome = approval_outcome(declaration, args, schema)
    assert outcome.decision == expected
    assert outcome.reason


def test_type_incompatible_invocation_is_rejected_before_conditions() -> None:
    declaration = _decl(
        scope="refund.create",
        approval={"when": [{"param": "amount", "op": ">", "value": 1000}]},
    )
    outcome = approval_outcome(declaration, {"amount": "1500"}, _AMOUNT_SCHEMA)
    assert outcome.decision == DECISION_REJECT_INVALID_ARGUMENTS
    assert outcome.argument_errors


def test_missing_required_argument_is_rejected() -> None:
    declaration = _decl(
        scope="refund.create",
        approval={"when": [{"param": "amount", "op": ">", "value": 1000}]},
    )
    outcome = approval_outcome(declaration, {}, _AMOUNT_SCHEMA)
    assert outcome.decision == DECISION_REJECT_INVALID_ARGUMENTS
    assert "amount" in outcome.reason


def test_condition_path_absent_from_schema_is_rejected() -> None:
    declaration = _decl(
        scope="refund.create",
        approval={"when": [{"param": "not.declared", "op": "exists"}]},
    )
    outcome = approval_outcome(declaration, {"amount": 1}, _AMOUNT_SCHEMA)
    assert outcome.decision == DECISION_REJECT_INVALID_ARGUMENTS
    assert "not.declared" in outcome.reason


def test_conditions_without_binding_schema_fail_closed() -> None:
    declaration = _decl(
        scope="refund.create", approval={"when": [{"param": "amount", "op": ">", "value": 1}]}
    )
    assert approval_outcome(declaration, {"amount": 2}, None).decision == (
        DECISION_REJECT_INVALID_ARGUMENTS
    )


def test_unconditional_requirement_without_schema_still_asks() -> None:
    declaration = _decl(scope="refund.create", approval={"required": True})
    assert approval_outcome(declaration, {"amount": 2}, None).decision == (
        DECISION_APPROVAL_REQUIRED
    )


# --- 入参子集校验器 -----------------------------------------------------------


def test_invocation_subset_validator_covers_structural_and_domain_keywords() -> None:
    """入参校验现在同时覆盖**结构关键字**与**取值域关键字**。

    结构：type / required / properties / items；
    取值域：enum / pattern / minimum / maximum / minLength / maxLength /
    minItems / maxItems / format(date-time, date)。
    """
    schema = {
        "type": "object",
        "properties": {
            "ids": {"type": "array", "items": {"type": "integer"}},
            "note": {"type": "string", "maxLength": 3},
            "kind": {"type": "string", "enum": ["a", "b"]},
        },
        "required": ["ids"],
    }
    # 结构问题：类型不符
    errors = invocation_errors({"ids": [1, "two"], "kind": "zzz"}, schema)
    assert len(errors) == 2
    assert "ids[1]" in errors[0]
    # 取值域问题：enum 不符（"zzz" 不在 ["a","b"] 里）
    assert "kind" in errors[1]
    # 取值域问题：长度超限
    length_errors = invocation_errors({"ids": [1], "note": "hello"}, schema)
    assert length_errors and "长度" in length_errors[0]
    # 必填缺失
    assert invocation_errors({"note": "x"}, schema)


def test_unknown_type_name_is_a_validation_error_not_a_pass() -> None:
    errors = invocation_errors({"a": 1}, {"type": "object", "properties": {"a": {"type": "int"}}})
    assert errors and "int" in errors[0]


def test_no_conditions_means_acc_does_not_gate_business_arguments() -> None:
    """没有 approval.when 时，ACC 层不代替宿主做出参校验（只做审批判定）。"""
    declaration = _decl(scope="refund.create")
    outcome = approval_outcome(declaration, {"amount": "1500"}, _AMOUNT_SCHEMA)
    assert outcome.decision == DECISION_ALLOW
    assert outcome.argument_errors == ()


def test_unconditional_requirement_still_gates_invalid_arguments() -> None:
    """required=true 且带条件时，入参不合法先拒绝——与参考 oracle 的判定顺序一致。"""
    declaration = _decl(
        scope="refund.create",
        approval={"required": True, "when": [{"param": "amount", "op": ">", "value": 1000}]},
    )
    outcome = approval_outcome(declaration, {"amount": "1500"}, _AMOUNT_SCHEMA)
    assert outcome.decision == DECISION_REJECT_INVALID_ARGUMENTS
