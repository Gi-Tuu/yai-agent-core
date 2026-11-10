"""OpenAPI 3.1 / JSON Schema 2020-12 兼容矩阵（工具入参子集）。

覆盖：版本接受、const→enum、type 联合中的 null、$ref 兄弟键覆盖、数值型
exclusiveMinimum 保留、未翻译构造只记 notes 不整份拒绝、webhooks 不注册工具。
"""

from __future__ import annotations

from yai_core.integrations.openapi import (
    OpenApiSpecConfig,
    load_spec,
    plan_operations,
    resolve_refs,
)
from yai_core.policy.approval import argument_errors
from yai_core.tools.schema import sanitize_schema


def _spec(version: str = "3.1.0", paths: dict | None = None, **extra) -> dict:
    spec = {
        "openapi": version,
        "info": {"title": "compat", "version": "1"},
        "paths": paths or {},
    }
    spec.update(extra)
    return spec


def _query_param(name: str, schema: dict, *, required: bool = False) -> dict:
    return {"name": name, "in": "query", "required": required, "schema": schema}


def _one_get(param: dict, *, version: str = "3.1.0", **extra) -> dict:
    spec = _spec(
        version,
        {
            "/items": {
                "get": {
                    "operationId": "listItems",
                    "parameters": [param],
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
        **extra,
    )
    return spec


# ---------- 版本 ----------


def test_accepts_openapi_31() -> None:
    spec = _one_get(_query_param("q", {"type": "string"}))
    loaded = load_spec(OpenApiSpecConfig(spec=spec))
    assert loaded["openapi"] == "3.1.0"
    plans, notes = plan_operations(loaded, OpenApiSpecConfig(spec=spec))
    assert len(plans) == 1 and notes == []


# ---------- 2020-12 关键字 ----------


def test_const_becomes_single_value_enum() -> None:
    cleaned = sanitize_schema(
        {"type": "object", "properties": {"mode": {"type": "string", "const": "atm"}}}
    )
    mode = cleaned["properties"]["mode"]
    assert mode["enum"] == ["atm"]
    assert "const" not in mode


def test_const_survives_into_plan_as_enum() -> None:
    spec = _one_get(_query_param("mode", {"type": "string", "const": "atm"}))
    plan = plan_operations(spec, OpenApiSpecConfig(spec=spec))[0][0]
    assert plan.input_schema["properties"]["mode"]["enum"] == ["atm"]


def test_type_union_with_null_is_preserved_and_null_is_accepted() -> None:
    spec = _one_get(_query_param("note", {"type": ["string", "null"]}))
    plan = plan_operations(spec, OpenApiSpecConfig(spec=spec))[0][0]
    member = plan.input_schema["properties"]["note"]
    assert member["type"] == ["string", "null"]
    assert argument_errors({"note": None}, plan.input_schema) == []


def test_numeric_exclusive_bounds_are_kept() -> None:
    spec = _one_get(
        _query_param(
            "amount", {"type": "number", "exclusiveMinimum": 0.5, "exclusiveMaximum": 9.5}
        )
    )
    plan = plan_operations(spec, OpenApiSpecConfig(spec=spec))[0][0]
    member = plan.input_schema["properties"]["amount"]
    assert member["exclusiveMinimum"] == 0.5
    assert member["exclusiveMaximum"] == 9.5


def test_ref_siblings_override_the_target() -> None:
    spec = _spec(
        paths={
            "/items": {
                "get": {
                    "operationId": "listItems",
                    "parameters": [
                        {
                            "name": "id",
                            "in": "query",
                            "schema": {
                                "$ref": "#/components/schemas/Id",
                                "description": "覆盖说明",
                                "minimum": 1,
                            },
                        }
                    ],
                }
            }
        },
        components={"schemas": {"Id": {"type": "integer", "description": "原始说明"}}},
    )
    resolved, notes = resolve_refs(spec)
    assert notes == []
    plan = plan_operations(resolved, OpenApiSpecConfig(spec=spec))[0][0]
    member = plan.input_schema["properties"]["id"]
    assert member["type"] == "integer"
    assert member["description"] == "覆盖说明"
    assert member["minimum"] == 1


# ---------- 未翻译构造：只记 notes，不整份拒绝 ----------


def test_unsupported_31_constructs_are_reported_not_fatal() -> None:
    spec = _one_get(
        _query_param(
            "tags",
            {
                "type": "array",
                "prefixItems": [{"type": "string"}],
                "items": {"type": "string"},
            },
        ),
        webhooks={"/hooks": {"post": {"responses": {"200": {"description": "ok"}}}}},
    )
    plans, notes = plan_operations(spec, OpenApiSpecConfig(spec=spec))
    assert len(plans) == 1  # 仍能注册工具
    joined = "\n".join(notes)
    assert "prefixItems" in joined
    assert "webhooks" in joined
    # 未翻译的键不会喂给模型
    tags = plans[0].input_schema["properties"]["tags"]
    assert "prefixItems" not in tags
    assert tags["type"] == "array"


def test_path_level_servers_are_reported() -> None:
    spec = _spec(
        paths={
            "/items": {
                "servers": [{"url": "https://other.example.com"}],
                "get": {"operationId": "listItems", "responses": {"200": {"description": "ok"}}},
            }
        }
    )
    plans, notes = plan_operations(spec, OpenApiSpecConfig(spec=spec))
    assert len(plans) == 1
    assert any("路径级 servers" in note for note in notes)


def test_webhooks_are_not_registered_as_tools() -> None:
    spec = _spec(
        paths={},
        webhooks={"/hooks": {"post": {"responses": {"200": {"description": "ok"}}}}},
    )
    plans, _notes = plan_operations(spec, OpenApiSpecConfig(spec=spec))
    assert plans == []


# ---------- 3.0 回归 ----------


def test_openapi_30_nullable_still_normalized() -> None:
    spec = _one_get(
        _query_param("note", {"type": "string", "nullable": True}),
        version="3.0.3",
    )
    plan = plan_operations(spec, OpenApiSpecConfig(spec=spec))[0][0]
    assert plan.input_schema["properties"]["note"]["type"] == ["string", "null"]
