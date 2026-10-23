"""JSON Schema 清洗：喂给模型的工具入参 schema 只保留标准关键字。

MCP SDK 自动生成的 input_schema 带 title 等实现私有键，OpenAPI 描述里也常混入
xml/example/deprecated 等 OpenAPI 专有注解；个别 OpenAI 兼容接口对多余键敏感，
注册进 ToolRegistry 前统一递归清洗一遍。MCP 桥与 OpenAPI 桥共用本模块。
"""

from __future__ import annotations

from typing import Any


def _allow_null(schema: dict[str, Any]) -> None:
    """把 OpenAPI 3.0 的 ``nullable: true`` 归一成标准 JSON Schema 允许 null。

    ``nullable`` 不是 JSON Schema 关键字，清洗时会被丢掉；如果只丢不转换，
    ``type: string, nullable: true`` 会变成"不允许 null 的 string"，模型给
    可选参数传 null 时反而被全局入参校验拒绝。归一后既保留语义，也让模型
    看到的是标准 ``type: ["string", "null"]``。
    """
    value = schema.get("type")
    if isinstance(value, str) and value != "null":
        schema["type"] = [value, "null"]
    elif isinstance(value, list) and "null" not in value:
        schema["type"] = [*value, "null"]
    enum = schema.get("enum")
    if isinstance(enum, list) and None not in enum:
        schema["enum"] = [*enum, None]


# JSON Schema 标准关键字白名单（draft 2020-12 常用子集，覆盖工具入参所需）。
_SCHEMA_KEYS = frozenset(
    {
        "type",
        "properties",
        "required",
        "items",
        "enum",
        "description",
        "default",
        "const",
        "anyOf",
        "oneOf",
        "allOf",
        "not",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minLength",
        "maxLength",
        "pattern",
        "minItems",
        "maxItems",
        "uniqueItems",
        "multipleOf",
        "format",
        "additionalProperties",
        "$defs",
        "$ref",
    }
)

# 空对象 schema 的公开常量（操作没有任何入参时使用）。
EMPTY_OBJECT_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}


def sanitize_schema(schema: Any) -> Any:
    """递归只保留 JSON Schema 标准关键字；dict/list 之外的原值原样返回。

    注意 properties / $defs 的键是"属性名/定义名"而不是 schema 关键字，
    必须原样保留，只清洗它们的值。
    """
    if isinstance(schema, dict):
        cleaned: dict[str, Any] = {}
        for key, value in schema.items():
            if key in ("properties", "$defs") and isinstance(value, dict):
                cleaned[key] = {name: sanitize_schema(sub) for name, sub in value.items()}
            elif key == "nullable":
                # 由下方统一转换，不把 OpenAPI 私有键喂给模型。
                continue
            elif key in _SCHEMA_KEYS:
                cleaned[key] = sanitize_schema(value)
        if schema.get("nullable") is True:
            _allow_null(cleaned)
        return cleaned or dict(EMPTY_OBJECT_SCHEMA)
    if isinstance(schema, list):
        return [sanitize_schema(item) for item in schema]
    return schema
