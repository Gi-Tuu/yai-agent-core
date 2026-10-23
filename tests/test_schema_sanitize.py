"""JSON Schema 清洗的 nullable 归一：OpenAPI 3.0 的 nullable 不能一丢了之。"""

from __future__ import annotations

from yai_core.integrations.openapi import OpenApiSpecConfig, plan_operations
from yai_core.policy.approval import argument_errors
from yai_core.tools.schema import sanitize_schema


def test_nullable_becomes_a_standard_type_union() -> None:
    cleaned = sanitize_schema(
        {
            "type": "object",
            "properties": {
                "note": {"type": "string", "nullable": True},
                "kind": {"type": "string", "nullable": True, "enum": ["a", "b"]},
            },
        }
    )
    note = cleaned["properties"]["note"]
    assert note["type"] == ["string", "null"]
    assert "nullable" not in note
    kind = cleaned["properties"]["kind"]
    assert kind["type"] == ["string", "null"]
    assert None in kind["enum"]


def test_nullable_value_passes_global_argument_validation() -> None:
    schema = sanitize_schema(
        {
            "type": "object",
            "properties": {"note": {"type": "string", "nullable": True}},
        }
    )
    assert argument_errors({"note": None}, schema) == []
    assert argument_errors({"note": "ok"}, schema) == []
    assert argument_errors({"note": 1}, schema)


def test_openapi_nullable_parameter_reaches_the_plan_as_type_union() -> None:
    spec = {
        "openapi": "3.0.3",
        "info": {"title": "nullable", "version": "1"},
        "paths": {
            "/notes": {
                "get": {
                    "operationId": "listNotes",
                    "parameters": [
                        {
                            "name": "note",
                            "in": "query",
                            "schema": {"type": "string", "nullable": True},
                        }
                    ],
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    plan = plan_operations(spec, OpenApiSpecConfig(spec=spec))[0][0]
    assert plan.input_schema["properties"]["note"]["type"] == ["string", "null"]
    assert argument_errors({"note": None}, plan.input_schema) == []


def test_nullable_marker_without_a_type_and_non_dict_values_are_harmless() -> None:
    assert sanitize_schema({"nullable": True}) == {"type": "object", "properties": {}}
    assert sanitize_schema("not-a-schema") == "not-a-schema"
