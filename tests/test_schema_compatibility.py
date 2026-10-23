"""OpenAPI/MCP schema 兼容性：未实现的组合关键字不能造成"合法调用被误拒"。

全局入参校验只实现一个子集。遇到可能**放宽**取值域的 ``anyOf`` / ``oneOf`` /
``$ref`` 时，宁可跳过这一层（能力体检会提示未校验），也不能按顶层 ``type`` 把
另一种合法分支拒绝。``additionalProperties`` 等只收紧/补充的键则不影响类型校验。
"""

from __future__ import annotations

import pytest

from yai_core import CapabilityHealth, ToolSpec, check_tool_specs
from yai_core.policy.approval import argument_errors
from yai_core.tools.schema import sanitize_schema


@pytest.mark.parametrize(
    "member_schema,value",
    [
        ({"type": "string", "anyOf": [{"type": "integer"}]}, 3),
        ({"type": "string", "oneOf": [{"type": "integer"}]}, 3),
        ({"type": "string", "$ref": "#/$defs/Maybe"}, 3),
    ],
)
def test_broadening_combinators_do_not_false_reject(
    member_schema: dict, value: object
) -> None:
    schema = {
        "type": "object",
        "properties": {"x": member_schema},
    }
    assert argument_errors({"x": value}, schema) == []


def test_supported_type_still_checked_when_no_broadening_key() -> None:
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    assert argument_errors({"x": 3}, schema)


def test_additional_properties_does_not_false_reject_object_payload() -> None:
    schema = {
        "type": "object",
        "properties": {
            "x": {
                "type": "object",
                "additionalProperties": {"type": "string"},
            }
        },
    }
    assert argument_errors({"x": {"a": "b"}}, schema) == []


def test_openapi_sanitizer_preserves_combinators_and_refs() -> None:
    cleaned = sanitize_schema(
        {
            "type": "object",
            "properties": {
                "a": {"anyOf": [{"type": "string"}, {"type": "integer"}]},
                "b": {"$ref": "#/$defs/Thing"},
            },
            "$defs": {"Thing": {"type": "string"}},
        }
    )
    assert "anyOf" in cleaned["properties"]["a"]
    assert cleaned["properties"]["b"]["$ref"] == "#/$defs/Thing"


def test_health_check_flags_unvalidated_combinators() -> None:
    spec = ToolSpec(
        name="compat",
        description="兼容性",
        input_schema={
            "type": "object",
            "properties": {"x": {"anyOf": [{"type": "string"}]}},
        },
        handler=lambda **_: {},
    )
    report: CapabilityHealth = check_tool_specs([spec])
    assert report.ok is True  # 只是未校验，不是接入错误
    assert "schema_unsupported_key" in {f.code for f in report.findings}
