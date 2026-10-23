"""能力接入体检：静态列出"声明了但执行器不认/不会生效"的约束。"""

from __future__ import annotations

import asyncio
import json

from yai_core import (
    AgentCore,
    ApprovalPolicy,
    ApprovalRule,
    CapabilityHealth,
    CompositeStep,
    ScriptedModel,
    ToolGovernance,
    ToolSpec,
    build_composite_spec,
    build_spec,
    check_tool_specs,
)
from yai_core.tools import ToolRegistry
from yai_core.types import ModelResponse, RateLimitHint


def _native(**overrides) -> ToolSpec:
    base = dict(
        name="demo",
        description="演示工具",
        input_schema={"type": "object", "properties": {"x": {"type": "string"}}},
        handler=lambda x="": {"x": x},
    )
    base.update(overrides)
    return ToolSpec(**base)


def _codes(report: CapabilityHealth) -> set[str]:
    return {finding.code for finding in report.findings}


def test_healthy_tool_has_no_findings() -> None:
    report = check_tool_specs([_native()])
    assert report.ok is True
    assert report.findings == ()
    assert "通过" in report.to_text()
    assert json.loads(json.dumps(report.to_dict()))["checked"] == 1


def test_unsupported_schema_and_formats_are_reported_as_warnings() -> None:
    spec = _native(
        input_schema={
            "type": "object",
            "properties": {
                "x": {
                    "anyOf": [{"type": "string"}],
                    "format": "uuid",
                    "pattern": "[",
                }
            },
        }
    )
    report = check_tool_specs([spec])
    assert report.ok is True  # 未实现键是警告，不让整个能力不可用
    assert {
        "schema_unsupported_key",
        "schema_format_not_enforced",
        "schema_invalid_pattern",
    } <= _codes(report)


def test_unknown_type_and_bad_rate_window_are_errors() -> None:
    bad_type = _native(
        input_schema={"type": "object", "properties": {"x": {"type": "wat"}}}
    )
    bad_rate = _native(governance=ToolGovernance(rate_limit=RateLimitHint(1, "forever")))
    report = check_tool_specs([bad_type, bad_rate])
    assert report.ok is False
    assert {"schema_unknown_type", "rate_limit_bad_window"} <= _codes(report)


def test_approval_path_must_exist_in_schema() -> None:
    spec = _native(
        governance=ToolGovernance(
            approval=ApprovalPolicy(when=(ApprovalRule(param="totl", op=">", value=1),))
        )
    )
    report = check_tool_specs([spec])
    assert report.ok is False
    assert "approval_path_missing" in _codes(report)


def test_composite_steps_are_checked_against_registry() -> None:
    registry = ToolRegistry()
    flow = build_composite_spec("flow", "流程", [{"tool": "missing"}])
    report = check_tool_specs([flow], registry=registry)
    assert report.ok is False
    assert "composite_missing_tool" in _codes(report)


def test_code_tool_without_sandbox_is_only_a_warning() -> None:
    spec = ToolSpec(
        name="code",
        description="代码",
        input_schema={"type": "object", "properties": {}},
        handler=None,
        source="code",
        code="return 1",
    )
    report = check_tool_specs([spec], sandbox_available=False)
    assert report.ok is True
    assert "code_without_sandbox" in _codes(report)


def test_health_check_does_not_call_handlers() -> None:
    called: list[int] = []

    def handler() -> dict:
        called.append(1)
        return {}

    spec = _native(handler=handler)
    check_tool_specs([spec])
    assert called == []


def test_agent_core_exposes_capability_health() -> None:
    core = AgentCore(ScriptedModel([ModelResponse(content="ok")]))
    core.register_tools([_native()])
    report = core.capability_health()
    assert report.checked == 1
    assert report.ok is True
    assert asyncio.run(core.run("你好")).final_text == "ok"


# ---------- 报告结构 ----------

def test_report_renders_findings_in_text_and_dict() -> None:
    spec = _native(input_schema={"type": "object", "properties": {"x": {"type": "wat"}}})
    report = check_tool_specs([spec])
    text = report.to_text()
    assert "1 个工具" in text and "1 error" in text
    assert "schema_unknown_type" in text
    payload = json.loads(json.dumps(report.to_dict()))
    assert payload["ok"] is False
    assert payload["errors"] == 1
    assert payload["findings"][0]["tool"] == "demo"
    assert payload["findings"][0]["path"] == "$.properties.x.type"


# ---------- schema 结构 ----------

def test_schema_structural_problems_are_errors() -> None:
    nested_not_object = _native(
        input_schema={"type": "object", "properties": {"x": "string"}}
    )
    bad_properties = _native(input_schema={"type": "object", "properties": []})
    report = check_tool_specs([nested_not_object, bad_properties])
    assert report.ok is False
    assert {"schema_not_object", "schema_bad_properties"} <= _codes(report)


def test_schema_type_union_members_are_checked() -> None:
    unknown_member = _native(input_schema={"type": ["string", "wat"]})
    empty_union = _native(input_schema={"type": []})
    report = check_tool_specs([unknown_member, empty_union])
    assert report.ok is False
    assert "schema_bad_type_union" in _codes(report)
    assert len([f for f in report.errors if f.code == "schema_bad_type_union"]) == 2


def test_schema_depth_limit_and_cycles_do_not_hang() -> None:
    deep: dict = {"type": "string"}
    for _ in range(34):
        deep = {"type": "object", "properties": {"n": deep}}
    report = check_tool_specs([_native(input_schema=deep)])
    assert "schema_too_deep" in _codes(report)

    cyclic: dict = {"type": "object"}
    cyclic["properties"] = {"self": cyclic}
    assert check_tool_specs([_native(input_schema=cyclic)]).ok is True


# ---------- schema 取值域声明 ----------

def test_schema_format_pattern_enum_required_are_checked() -> None:
    spec = _native(
        input_schema={
            "type": "object",
            "properties": {
                "a": {"format": 1},
                "b": {"pattern": 1},
                "c": {"enum": "x"},
                "d": {"enum": []},
            },
            "required": ["missing"],
        }
    )
    bad_required = _native(input_schema={"type": "object", "required": "x"})
    report = check_tool_specs([spec, bad_required])
    assert {
        "schema_bad_format",
        "schema_bad_pattern",
        "schema_bad_enum",
        "schema_empty_enum",
        "schema_required_undeclared",
        "schema_bad_required",
    } <= _codes(report)


def test_schema_items_and_additional_properties_are_checked() -> None:
    tuple_items = _native(input_schema={"type": "array", "items": [{"type": "string"}]})
    array_without_items = _native(input_schema={"type": "array"})
    nested_items = _native(input_schema={"type": "array", "items": {"type": "wat"}})
    extra = _native(
        input_schema={
            "type": "object",
            "properties": {"ref": {"$ref": "#/$defs/x"}},
            "additionalProperties": {"type": "wat"},
        }
    )
    report = check_tool_specs([tuple_items, array_without_items, nested_items, extra])
    assert {
        "schema_tuple_items",
        "schema_array_without_items",
        "schema_unsupported_key",
        "schema_unknown_type",
    } <= _codes(report)


# ---------- 审批声明 ----------

def test_approval_field_types_are_checked() -> None:
    spec = _native(
        governance=ToolGovernance(
            approval=ApprovalPolicy(required="yes", prompt=1, when="nope")  # type: ignore[arg-type]
        )
    )
    report = check_tool_specs([spec])
    assert report.ok is False
    assert {
        "approval_required_not_bool",
        "approval_prompt_not_str",
        "approval_when_not_sequence",
    } <= _codes(report)


def test_approval_rules_are_checked_one_by_one() -> None:
    spec = _native(
        governance=ToolGovernance(
            approval=ApprovalPolicy(
                when=(
                    ApprovalRule(param="", op=">", value=1),
                    ApprovalRule(param="x", op="bogus", value=1),
                    ApprovalRule(param="x", op=">", value="nope"),
                    ApprovalRule(param="x", op="in", value="nope"),
                    ApprovalRule(param="x", op="exists", label=1),  # type: ignore[arg-type]
                )
            )
        )
    )
    report = check_tool_specs([spec])
    assert report.ok is False
    assert {
        "approval_bad_param",
        "approval_unknown_op",
        "approval_numeric_value",
        "approval_in_value",
    } <= _codes(report)
    assert any(
        f.code == "approval_bad_label" and f.severity == "warning"
        for f in report.findings
    )


# ---------- 治理元数据 ----------

def test_governance_types_are_checked() -> None:
    spec = _native(
        governance=ToolGovernance(
            timeout_ms=0,
            rate_limit="fast",  # type: ignore[arg-type]
            audit_sensitive="yes",  # type: ignore[arg-type]
            preview="not-callable",  # type: ignore[arg-type]
        )
    )
    report = check_tool_specs([spec])
    assert report.ok is False
    assert {
        "governance_bad_timeout",
        "governance_bad_rate_limit",
        "governance_audit_flag",
        "governance_bad_preview",
    } <= _codes(report)


def test_rate_limit_count_and_window_are_checked() -> None:
    spec = _native(governance=ToolGovernance(rate_limit=RateLimitHint(0, "1h")))
    empty_window = _native(governance=ToolGovernance(rate_limit=RateLimitHint(1, "")))
    report = check_tool_specs([spec, empty_window])
    assert report.ok is False
    assert "rate_limit_bad_count" in _codes(report)
    assert "rate_limit_bad_window" in _codes(report)


def test_sensitive_paths_must_be_non_empty_and_declared() -> None:
    spec = _native(governance=ToolGovernance(sensitive=("", "missing")))
    report = check_tool_specs([spec])
    assert report.ok is False
    assert "governance_bad_sensitive" in _codes(report)
    assert "sensitive_path_missing" in _codes(report)


# ---------- 工具身份与来源 ----------

def test_tool_identity_and_schema_shape_are_checked() -> None:
    spec = ToolSpec(
        name=" ",
        description="",
        input_schema=[],  # type: ignore[arg-type]
        handler=None,
        source="bogus",  # type: ignore[arg-type]
    )
    report = check_tool_specs([spec])
    assert report.ok is False
    assert {
        "tool_bad_name",
        "tool_bad_source",
        "tool_empty_description",
        "tool_bad_input_schema",
    } <= _codes(report)


def test_non_spec_entry_is_reported() -> None:
    report = check_tool_specs([object()])  # type: ignore[list-item]
    assert report.ok is False
    assert "tool_not_spec" in _codes(report)
    assert report.checked == 1


# ---------- 组合 / 代码 / 声明型工具 ----------

def test_composite_steps_are_structurally_checked() -> None:
    no_steps = ToolSpec(
        name="flow", description="流程", input_schema={"type": "object"},
        handler=None, source="composite", steps=None,
    )
    bad_step = ToolSpec(
        name="flow2", description="流程", input_schema={"type": "object"},
        handler=None, source="composite", steps=[CompositeStep(tool="")],
    )
    report = check_tool_specs([no_steps, bad_step])
    assert report.ok is False
    assert {"composite_without_steps", "composite_bad_step"} <= _codes(report)


def test_composite_cannot_reference_another_composite() -> None:
    registry = ToolRegistry()
    inner = build_composite_spec("inner", "内部流程", [{"tool": "leaf"}])
    registry.register(build_spec(lambda: {"ok": True}, name="leaf"))
    registry.register(inner)
    outer = build_composite_spec("outer", "外部流程", [{"tool": "inner"}])
    report = check_tool_specs([outer], registry=registry)
    assert report.ok is False
    assert "composite_nested" in _codes(report)


def test_code_tool_source_and_handler_are_checked() -> None:
    no_source = ToolSpec(
        name="code", description="代码", input_schema={"type": "object"},
        handler=None, source="code", code=None,
    )
    with_handler = ToolSpec(
        name="code2", description="代码", input_schema={"type": "object"},
        handler=lambda: {}, source="code", code="return 1",
    )
    report = check_tool_specs([no_source, with_handler])
    assert report.ok is True
    assert "code_without_source" in _codes(report)
    assert "code_has_handler" in _codes(report)


def test_declared_tool_without_handler_is_informational() -> None:
    spec = _native(handler=None)
    report = check_tool_specs([spec])
    assert report.ok is True
    finding = next(f for f in report.findings if f.code == "handler_unbound")
    assert finding.severity == "info"
