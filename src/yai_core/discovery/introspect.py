"""Python 函数内省：type hints + docstring -> JSON Schema 工具规格。

这是"宿主只声明能力、Core 自动长出 Agent"的第一块基石。
v0.2 计划：openapi.py（OpenAPI 文档 -> 工具）、mcp.py（MCP tools/list -> 工具）。
"""

from __future__ import annotations

import inspect
import types
from collections.abc import Mapping, Sequence, Set
from typing import Annotated, Any, Literal, Union, get_args, get_origin, get_type_hints

from yai_core.types import ToolSpec

_PY_TO_JSON: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    tuple: "array",
    set: "array",
    frozenset: "array",
    dict: "object",
}


def _annotation_to_schema(annotation: Any) -> tuple[dict[str, Any], bool]:
    """把 Python 注解映射成 JSON Schema 子集；返回 ``(schema, nullable)``。

    容器必须递归映射：``list[dict[str, Any]]`` 不是 ``string``。此前只查
    ``_PY_TO_JSON`` 的顶层类型，导致所有泛型容器都退化成 ``string``——模型
    一旦按声明传入数组，绑定 schema 的审批/校验就会把合法调用判成非法。
    """
    if annotation is inspect.Parameter.empty:
        return {"type": "string"}, False
    if annotation is Any:
        return {}, False
    if annotation is type(None):
        return {"type": "null"}, False

    origin = get_origin(annotation)
    if origin is Annotated:
        return _annotation_to_schema(get_args(annotation)[0])

    if origin is Union or isinstance(annotation, types.UnionType):
        args = list(get_args(annotation))
        nullable = type(None) in args
        members = [a for a in args if a is not type(None)]
        schemas = [_annotation_to_schema(member)[0] for member in members]
        if len(schemas) == 1:
            return schemas[0], nullable
        if schemas:
            types_out: list[str] = []
            for schema in schemas:
                value = schema.get("type")
                if isinstance(value, str):
                    types_out.append(value)
                elif isinstance(value, list) and all(isinstance(v, str) for v in value):
                    types_out.extend(value)
                else:
                    types_out = []
                    break
            if types_out:
                return {"type": list(dict.fromkeys(types_out))}, nullable
            return {"anyOf": schemas}, nullable
        return {}, nullable

    if origin in (list, set, frozenset, tuple, Sequence, Set):
        out: dict[str, Any] = {"type": "array"}
        item_args = get_args(annotation)
        item: Any = None
        if origin is tuple:
            if len(item_args) == 2 and item_args[1] is Ellipsis:
                item = item_args[0]
            elif len(item_args) == 1:
                item = item_args[0]
        elif item_args:
            item = item_args[0]
        if item is not None:
            item_schema, _ = _annotation_to_schema(item)
            if item_schema:
                out["items"] = item_schema
        return out, False

    if origin in (dict, Mapping):
        out = {"type": "object"}
        dict_args = get_args(annotation)
        if len(dict_args) == 2:
            value_schema, _ = _annotation_to_schema(dict_args[1])
            if value_schema:
                out["additionalProperties"] = value_schema
        return out, False

    if origin is Literal:
        values = list(get_args(annotation))
        out = {"enum": values}
        types_out = list(dict.fromkeys(type(v).__name__ for v in values))
        json_types = {
            "str": "string",
            "int": "integer",
            "float": "number",
            "bool": "boolean",
            "NoneType": "null",
        }
        mapped = [json_types.get(name) for name in types_out]
        if all(mapped):
            out["type"] = mapped[0] if len(mapped) == 1 else mapped
        return out, False

    direct = _PY_TO_JSON.get(annotation)
    if direct is not None:
        return {"type": direct}, False
    return {"type": "string"}, False


def _annotation_to_json_type(annotation: Any) -> tuple[str, bool]:
    """兼容旧调用的窄接口：只返回顶层 JSON 类型与是否可空。

    ``build_spec`` 需要完整 schema（容器 items / enum 等），走
    :func:`_annotation_to_schema`；这里保留给已有调用与测试，Union 取第一个
    非空成员，认不出的类型仍回退 string。
    """
    schema, nullable = _annotation_to_schema(annotation)
    value = schema.get("type")
    if isinstance(value, list):
        value = next((item for item in value if item != "null"), "string")
    if not isinstance(value, str):
        return "string", nullable
    return value, nullable


def _type_hints(fn: Any) -> dict[str, Any]:
    """解析 PEP 563 的字符串注解；解不开就交回原注解（继续走"认不出就 string"的兜底）。

    ``from __future__ import annotations`` 让 ``param.annotation`` 变成字面量 ``"float"``，
    而查表当然查不到 —— 于是**所有参数都退化成 string**。这不只影响文档好不好看：
    审批条件要按绑定 schema 校验入参，"期望 string"会把每一次合法调用都判成非法。
    """
    try:
        return dict(get_type_hints(fn))
    except Exception:  # noqa: BLE001 - 前向引用解不开时不能连累工具注册
        return {}


def build_spec(fn: Any, *, name: str | None = None, source: str = "native") -> ToolSpec:
    """把一个 Python 可调用对象转换成 ToolSpec。"""
    if not callable(fn):
        raise TypeError(f"build_spec 需要可调用对象，得到 {type(fn)!r}")

    sig = inspect.signature(fn)
    hints = _type_hints(fn)
    properties: dict[str, Any] = {}
    required: list[str] = []

    for param_name, param in sig.parameters.items():
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        prop, nullable = _annotation_to_schema(
            hints.get(param_name, param.annotation)
        )
        if param.default is inspect.Parameter.empty:
            required.append(param_name)
        else:
            prop["default"] = param.default
        if nullable:
            value = prop.get("type")
            if isinstance(value, str):
                prop["type"] = [value, "null"]
            elif isinstance(value, list) and "null" not in value:
                prop["type"] = [*value, "null"]
            if isinstance(prop.get("enum"), list) and None not in prop["enum"]:
                prop["enum"] = [*prop["enum"], None]
        properties[param_name] = prop

    doc = inspect.getdoc(fn) or ""
    description = doc.split("\n\n", 1)[0].strip() or name or fn.__name__

    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required

    return ToolSpec(
        name=name or fn.__name__,
        description=description,
        input_schema=schema,
        handler=fn,
        source=source,  # type: ignore[arg-type]
    )


def discover(host: Any) -> list[ToolSpec]:
    """从模块 / 类实例 / 对象上自动发现公开可调用能力。

    - 模块：只收该模块内定义的函数（避免把 import 进来的函数误注册）
    - 对象实例：收公开方法（不以 _ 开头）
    """
    specs: list[ToolSpec] = []

    if inspect.ismodule(host):
        module_name = host.__name__
        members = inspect.getmembers(host, inspect.isfunction)
        for member_name, fn in members:
            if member_name.startswith("_"):
                continue
            if getattr(fn, "__module__", None) != module_name:
                continue
            specs.append(build_spec(fn))
        return specs

    for member_name, method in inspect.getmembers(host, inspect.ismethod):
        if member_name.startswith("_"):
            continue
        specs.append(build_spec(method))
    return specs
