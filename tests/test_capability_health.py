"""能力接入体检：静态列出"声明了但执行器不认/不会生效"的约束。"""

from __future__ import annotations

import asyncio
import json

from yai_core import (
    AgentCore,
    ApprovalPolicy,
    ApprovalRule,
    CapabilityHealth,
    ScriptedModel,
    ToolGovernance,
    ToolSpec,
    build_composite_spec,
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
