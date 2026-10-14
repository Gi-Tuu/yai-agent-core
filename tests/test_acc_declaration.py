"""ACC v1 声明校验器的边界测试：官方向量之外的字段、类型与长度组合。

官方 26 条向量只覆盖声明层的 5 条；这一文件把 schema 里每一条约束（必填、枚举、
正则、区间、长度）都钉住，并确认"失败只出诊断、绝不抛异常、未知字段不产生安全含义"。
"""

from __future__ import annotations

from typing import Any

import pytest

from yai_core.integrations.acc.declaration import (
    DIAGNOSTIC_INVALID_DECLARATION,
    DIAGNOSTIC_UNSUPPORTED_VERSION,
    parse_declaration,
)


def _raw(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"version": 1, "enabled": True, "scope": "order.read"}
    base.update(overrides)
    return base


def _errors(raw: dict[str, Any]) -> tuple[str, ...]:
    result = parse_declaration(raw)
    assert result.valid is False
    assert result.declaration is None
    return result.errors


# --- 必填三件套 ---------------------------------------------------------------


def test_minimal_declaration_is_valid_and_fills_defaults() -> None:
    result = parse_declaration(_raw())
    assert result.valid and result.diagnostic is None
    assert result.errors == ()
    declaration = result.declaration
    assert declaration is not None
    assert (declaration.version, declaration.enabled, declaration.scope) == (1, True, "order.read")
    assert declaration.risk_level is None
    assert declaration.subject_required is False
    assert declaration.approval_when == ()
    assert declaration.readonly is None
    assert declaration.extensions == {}


@pytest.mark.parametrize("missing", ["version", "enabled", "scope"])
def test_each_required_field_is_named_in_errors(missing: str) -> None:
    raw = _raw()
    del raw[missing]
    errors = _errors(raw)
    assert any(missing in error for error in errors)
    assert parse_declaration(raw).diagnostic == DIAGNOSTIC_INVALID_DECLARATION


def test_version_one_as_zero_fraction_float_is_still_integer_one() -> None:
    result = parse_declaration(_raw(version=1.0))
    assert result.valid is True
    assert result.declaration is not None
    assert result.declaration.version == 1


@pytest.mark.parametrize("value", [2, 7, 3.0])
def test_other_integer_versions_are_unsupported_major(value: Any) -> None:
    result = parse_declaration(_raw(version=value))
    assert result.valid is False
    assert result.diagnostic == DIAGNOSTIC_UNSUPPORTED_VERSION


@pytest.mark.parametrize("value", ["1", 2.5, True, None, [1], {}])
def test_non_integer_or_missing_version_is_invalid_declaration(value: Any) -> None:
    result = parse_declaration(_raw(version=value))
    assert result.valid is False
    assert result.diagnostic == DIAGNOSTIC_INVALID_DECLARATION


def test_unsupported_version_wins_over_other_errors() -> None:
    raw = _raw(version=2, scope="!!!")
    result = parse_declaration(raw)
    assert result.diagnostic == DIAGNOSTIC_UNSUPPORTED_VERSION
    assert len(result.errors) == 2


@pytest.mark.parametrize("value", [1, "true", None])
def test_enabled_must_be_boolean(value: Any) -> None:
    assert parse_declaration(_raw(enabled=value)).valid is False


# --- scope 格式 ---------------------------------------------------------------


@pytest.mark.parametrize(
    "scope",
    ["order.read", "Order-1", "staff.search:dept", "a", "refund_request.create", "x" * 160],
)
def test_scope_accepts_documented_shape(scope: str) -> None:
    assert parse_declaration(_raw(scope=scope)).valid is True


@pytest.mark.parametrize("scope", ["", ".order", "-order", ":order", "order read", "x" * 161, 1])
def test_scope_rejects_empty_lead_and_oversize(scope: Any) -> None:
    assert parse_declaration(_raw(scope=scope)).valid is False


# --- 嵌套对象类型 -------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        _raw(risk="low"),
        _raw(risk={"level": "critical"}),
        _raw(risk={"level": 1}),
        _raw(subject={"required": "yes"}),
        _raw(audit={"sensitive": 1}),
        _raw(approval="required"),
        _raw(approval={"prompt": "x" * 501}),
        _raw(approval={"when": {"param": "amount"}}),
        _raw(execution={"timeout_ms": 0}),
        _raw(execution={"timeout_ms": 600_001}),
        _raw(execution={"timeout_ms": "1000"}),
        _raw(execution={"readonly": "true"}),
        _raw(execution={"rate_limit": {"count": 30}}),
        _raw(execution={"rate_limit": {"count": 0, "window": "1m"}}),
        _raw(execution={"rate_limit": {"count": 30, "window": "0s"}}),
        _raw(execution={"rate_limit": {"count": 30, "window": "1.5m"}}),
        _raw(execution={"rate_limit": {"count": 30, "window": "1M"}}),
        _raw(guidance={"when_to_use": "x" * 2001}),
        _raw(guidance={"examples": [1, 2]}),
        _raw(guidance={"context": ["a" * 121]}),
        _raw(guidance={"context": "customer-service"}),
    ],
)
def test_documented_constraints_reject(raw: dict[str, Any]) -> None:
    result = parse_declaration(raw)
    assert result.valid is False
    assert result.diagnostic == DIAGNOSTIC_INVALID_DECLARATION
    assert result.errors


def test_accepted_nested_values() -> None:
    declaration = parse_declaration(
        _raw(
            risk={"level": "high"},
            subject={"required": True},
            audit={"sensitive": True},
            approval={"required": True, "prompt": "Approve?", "when": []},
            execution={
                "readonly": False,
                "idempotent": True,
                "timeout_ms": 600_000,
                "rate_limit": {"count": 30, "window": "1h"},
            },
            guidance={
                "when_to_use": "use it",
                "returns": "a thing",
                "examples": [{"id": "SO1"}],
                "context": ["customer-service"],
            },
        )
    ).declaration
    assert declaration is not None
    assert declaration.risk_level == "high"
    assert declaration.subject_required is True
    assert declaration.audit_sensitive is True
    assert declaration.approval_required is True
    assert declaration.approval_prompt == "Approve?"
    assert declaration.approval_when == ()
    assert declaration.readonly is False
    assert declaration.idempotent is True
    assert declaration.timeout_ms == 600_000
    assert declaration.rate_limit is not None
    assert (declaration.rate_limit.count, declaration.rate_limit.window) == (30, "1h")
    assert declaration.guidance is not None
    assert declaration.guidance.examples == ({"id": "SO1"},)
    assert declaration.guidance.context == ("customer-service",)


# --- approval.when -----------------------------------------------------------


def test_approval_conditions_keep_json_types() -> None:
    declaration = parse_declaration(
        _raw(
            scope="refund.create",
            approval={
                "when": [
                    {"param": "amount", "op": ">", "value": 1000, "label": "超千元"},
                    {"param": "urgent", "op": "==", "value": True},
                    {"param": "reason", "op": "exists"},
                    {"param": "team", "op": "in", "value": ["support", "finance"]},
                ]
            },
        )
    ).declaration
    assert declaration is not None
    conditions = declaration.approval_when
    assert [c.op for c in conditions] == [">", "==", "exists", "in"]
    assert conditions[0].label == "超千元"
    assert conditions[1].value is True
    assert conditions[2].has_value is False
    assert conditions[3].value == ["support", "finance"]


def test_nested_unknown_keys_are_dropped_not_rejected() -> None:
    declaration = parse_declaration(_raw(risk={"level": "low", "x_future": 1})).declaration
    assert declaration is not None
    assert declaration.risk_level == "low"
    assert declaration.extensions == {}


def test_root_unknown_keys_land_in_extension_bag() -> None:
    declaration = parse_declaration(_raw(x_business_owner="ops", x_future_hint=True)).declaration
    assert declaration is not None
    assert declaration.extensions == {"x_business_owner": "ops", "x_future_hint": True}


@pytest.mark.parametrize(
    "item",
    [
        {"op": ">"},
        {"param": "amount"},
        {"param": "", "op": ">"},
        {"param": "amount", "op": "=~"},
        {"param": "amount", "op": ">", "label": "x" * 301},
        "amount>1000",
    ],
)
def test_approval_condition_items_are_validated(item: Any) -> None:
    assert parse_declaration(_raw(approval={"when": [item]})).valid is False


# --- 输入形态 -----------------------------------------------------------------


@pytest.mark.parametrize("raw", [None, "version: 1", 1, [], ["version"], True])
def test_non_object_root_gives_diagnostic_without_raising(raw: Any) -> None:
    result = parse_declaration(raw)
    assert result.valid is False
    assert result.diagnostic == DIAGNOSTIC_INVALID_DECLARATION
    assert len(result.errors) == 1
