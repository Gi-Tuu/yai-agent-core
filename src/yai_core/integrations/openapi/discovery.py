"""发现阶段：把 OpenAPI paths/operations 翻译成工具执行计划（OperationPlan）。

本模块纯函数、零 IO、零第三方依赖，输出交给 client.py 闭包执行。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from yai_core.integrations.acc.evaluate import to_execution_policy
from yai_core.integrations.acc.gate import build_gate
from yai_core.integrations.acc.openapi import bind_operation, misplaced_declarations
from yai_core.integrations.openapi.spec import (
    _HTTP_METHODS,
    _READ_ONLY_METHODS,
    OpenApiSpecConfig,
)
from yai_core.tools.schema import EMPTY_OBJECT_SCHEMA, sanitize_schema
from yai_core.types import ToolGovernance


@dataclass
class OperationPlan:
    """一个 REST 操作翻译成工具的中间表示。"""

    name: str
    description: str
    input_schema: dict[str, Any]
    method: str
    path_template: str
    path_params: tuple[str, ...]
    query_params: tuple[str, ...]
    body_fields: tuple[str, ...]
    body_wrapped: bool
    body_required: bool
    security: list[dict] = field(default_factory=list)
    # 带 ACC 扩展的 operation 才有：治理元数据快照，交由 client 装进 ToolSpec。
    governance: ToolGovernance | None = None


def _camel_to_snake(segment: str) -> str:
    """petId → pet_id；listPets → list_pets（小写边界前插下划线）。"""
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", segment).lower()


def _clean_operation_id(operation_id: str) -> str:
    """operationId 合法化：驼峰转蛇形、非法字符变下划线、截断 64 字符。"""
    name = _camel_to_snake(operation_id)
    name = re.sub(r"[^a-z0-9_-]+", "_", name).strip("_")
    return name[:64].strip("_")


def _synthetic_name(method: str, path: str) -> str:
    """没有 operationId 时合成：GET /pets/{petId} → get_pets_pet_id。"""
    parts = [method.lower()]
    for segment in path.strip("/").split("/"):
        if not segment:
            continue
        segment = segment.strip("{}").replace("-", "_")
        parts.append(_camel_to_snake(segment))
    return re.sub(r"[^a-z0-9_]+", "_", "_".join(parts))[:64].strip("_")


def _dedupe_name(name: str, used: set[str], notes: list[str]) -> str:
    """同一 spec 内重名追加 __2/__3（跨来源重名由 ToolRegistry 直接报错，不在这里吞）。"""
    if name not in used:
        used.add(name)
        return name
    suffix = 2
    while f"{name}__{suffix}" in used:
        suffix += 1
    final = f"{name}__{suffix}"
    used.add(final)
    notes.append(f"同一 spec 内工具名冲突，{name} 的第 {suffix - 1} 个重名操作改名为 {final}")
    return final


def _parameter_property(parameter: dict) -> tuple[str, dict, bool]:
    name = parameter["name"]
    schema = sanitize_schema(parameter.get("schema") or {})
    if parameter.get("description") and isinstance(schema, dict):
        schema.setdefault("description", parameter["description"])
    return name, schema, bool(parameter.get("required", False))


def _merge_body(
    properties: dict, required: list[str], body_schema: dict, notes: list[str]
) -> tuple[tuple[str, ...], bool]:
    """requestBody 的 schema 并入入参：object 拍平合并；其他类型包成 {"body": ...}。"""
    body_schema = sanitize_schema(body_schema)
    if isinstance(body_schema, dict) and body_schema.get("type") == "object":
        body_props = body_schema.get("properties") or {}
        for name, sub in body_props.items():
            if name in properties:
                notes.append(f"body 字段与路径/查询参数同名 {name}，body 侧覆盖")
            properties[name] = sub
        for name in body_schema.get("required") or []:
            if name not in required:
                required.append(name)
        return tuple(body_props.keys()), False
    properties["body"] = body_schema
    return ("body",), True


def _path_allowed(path: str, config: OpenApiSpecConfig) -> bool:
    """先 include 白名单（前缀匹配），再 exclude 黑名单。"""
    if config.include_paths and not any(
        path.startswith(prefix) for prefix in config.include_paths
    ):
        return False
    if config.exclude_paths and any(path.startswith(prefix) for prefix in config.exclude_paths):
        return False
    return True


def _collect_parameters(
    shared_parameters: list[Any], operation: dict
) -> tuple[dict[str, Any], list[str], list[str], list[str]]:
    """合并 pathItem 级与 operation 级 parameters，拆成 (properties, required, path, query)。

    header / cookie 参数不暴露给模型（v0.2 边界）；path 参数按 OpenAPI 规定恒为 required。
    """
    properties: dict[str, Any] = {}
    required: list[str] = []
    path_params: list[str] = []
    query_params: list[str] = []
    for parameter in [*shared_parameters, *(operation.get("parameters") or [])]:
        if not isinstance(parameter, dict):
            continue
        where = parameter.get("in")
        if where not in {"path", "query"}:
            continue
        name, sub_schema, is_required = _parameter_property(parameter)
        properties[name] = sub_schema
        if where == "path":
            path_params.append(name)
            if name not in required:
                required.append(name)  # OpenAPI 规定 path 参数恒为 required
        else:
            query_params.append(name)
            if is_required and name not in required:
                required.append(name)
    return properties, required, path_params, query_params


def _resolve_request_body(
    operation: dict, properties: dict[str, Any], required: list[str], notes: list[str]
) -> tuple[tuple[str, ...], bool, bool]:
    """解析 requestBody 的 JSON schema 并并入入参，返回 (body_fields, body_wrapped, body_required)。

    object 类型由 :func:`_merge_body` 拍平；其他类型包成单个 ``body`` 字段。
    """
    body_fields: tuple[str, ...] = ()
    body_wrapped = False
    body_required = False
    request_body = operation.get("requestBody")
    if isinstance(request_body, dict):
        content = request_body.get("content") or {}
        json_entry = content.get("application/json")
        if json_entry and isinstance(json_entry.get("schema"), dict):
            body_fields, body_wrapped = _merge_body(
                properties, required, json_entry["schema"], notes
            )
            body_required = bool(request_body.get("required", False))
            if body_required and body_wrapped and "body" not in required:
                required.append("body")
    return body_fields, body_wrapped, body_required


def _plan_one_operation(
    method_l: str,
    path: str,
    operation: dict,
    spec: dict,
    shared_parameters: list[Any],
    config: OpenApiSpecConfig,
    used_names: set[str],
    notes: list[str],
) -> OperationPlan | None:
    """把单个 HTTP operation 翻译成一个 :class:`OperationPlan`（参数 / body / 命名 / schema）。

    带 ACC 扩展时多走一道绑定：声明不可用或不可暴露的操作**返回 None 并留诊断**，
    绝不退化成"一个没有治理的同名工具"。
    """
    properties, required, path_params, query_params = _collect_parameters(
        shared_parameters, operation
    )
    body_fields, body_wrapped, body_required = _resolve_request_body(
        operation, properties, required, notes
    )

    operation_id = operation.get("operationId")
    raw_name = (
        _clean_operation_id(operation_id)
        if operation_id
        else _synthetic_name(method_l, path)
    )
    name = _dedupe_name(raw_name, used_names, notes)
    if config.prefix:
        name = f"{config.prefix}_{name}"

    summary = (operation.get("summary") or "").strip()
    description = (operation.get("description") or "").strip()
    tail = f"{summary} {description}".strip()
    tool_description = f"[{method_l.upper()} {path}] {tail}".strip()[:1000]

    input_schema = sanitize_schema(
        {"type": "object", "properties": properties, "required": required}
    )
    if not properties:
        input_schema = dict(EMPTY_OBJECT_SCHEMA)

    binding = bind_operation(
        operation,
        method=method_l.upper(),
        tool_name=name,
        input_schema=input_schema,
        query_params=tuple(query_params),
        body_fields=body_fields,
        scope_policy=config.acc_scope_allowed,
        trusted_subject=config.trusted_subject,
    )
    if binding.declared:
        notes.extend(binding.diagnostics)
        if not binding.exposed:
            # 被跳过不该顺手吞掉一个工具名：否则后面的同名操作会被改名成 __2。
            used_names.discard(name)
            return None
        if config.include_guidance:
            # 默认**不**把 guidance 喂给模型（来源可能是第三方 spec）；宿主要展示
            # 可自行从 ToolSpec.governance.acc 里取原文。
            tool_description = _with_guidance(tool_description, binding)

    governance: ToolGovernance | None = None
    if binding.declared and binding.declaration is not None:
        timeout_ms, rate_limit = to_execution_policy(binding.declaration)
        governance = ToolGovernance(
            audit_sensitive=binding.audit_sensitive,
            acc=binding.raw,
            # 声明参与准入的方式：编译成一个中性判定函数挂到 governance.gate 上，
            # 执行器只认 GateDecision，不认识 ACC。
            gate=build_gate(binding.declaration, input_schema),
            # 执行提示（超时 / 频率）翻译成内核原生的执行策略，由执行器真的强制。
            timeout_ms=timeout_ms,
            rate_limit=rate_limit,
        )

    return OperationPlan(
        name=name,
        description=tool_description,
        input_schema=input_schema,
        method=method_l.upper(),
        path_template=path,
        path_params=tuple(path_params),
        query_params=tuple(query_params),
        body_fields=body_fields,
        body_wrapped=body_wrapped,
        body_required=body_required,
        # operation 级 security 缺省回退全局 security。
        security=operation.get("security", spec.get("security")) or [],
        governance=governance,
    )


def _sanitize_guidance(text: str) -> str:
    """把声明里的 guidance 文本清成"可安全放进模型提示"的样子。

    ACC 的 ``guidance`` 本来就是**给模型看的**（when_to_use / returns），所以它一定会
    进入提示词。问题在于它的来源可能不可信：OpenAPI 描述常来自第三方 API——一份被
    污染的 spec 就能借 guidance 往模型提示里塞任意指令文本。

    这里做的是**净化**而不是"禁止"（ACC 明确要求 guidance 模型可读）：去掉控制字符
    （换行与制表保留，它们是正常排版）、零宽字符与双向覆盖符（后者能在界面上骗过
    人眼），其余字符**逐字保留**——净化不得改写声明本身的意思。
    """
    cleaned = []
    for char in text:
        code = ord(char)
        if char in ("\n", "\t"):
            cleaned.append(char)
            continue
        if code < 0x20 or code == 0x7F:      # 其余控制字符
            continue
        if code in (0x200B, 0x200C, 0x200D, 0xFEFF):   # 零宽字符
            continue
        if 0x202A <= code <= 0x202E or 0x2066 <= code <= 0x2069:  # 双向覆盖
            continue
        cleaned.append(char)
    return "".join(cleaned)


def _with_guidance(description: str, binding: Any) -> str:
    """把 ACC 的 guidance 追加到工具描述尾部（声明"什么时候用""返回什么"）。"""
    declaration = binding.declaration
    if declaration is None or declaration.guidance is None:
        return description
    parts = [
        f"使用时机：{_sanitize_guidance(declaration.guidance.when_to_use)}"
        if declaration.guidance.when_to_use
        else "",
        f"返回：{_sanitize_guidance(declaration.guidance.returns)}"
        if declaration.guidance.returns
        else "",
    ]
    extra = " ".join(part for part in parts if part)
    if not extra:
        return description
    return f"{description} {extra}".strip()[:1000]



def _plans_for_path(
    path: str,
    path_item: Any,
    spec: dict,
    config: OpenApiSpecConfig,
    used_names: set[str],
    notes: list[str],
) -> list[OperationPlan]:
    """处理单个 pathItem：校验 / 过滤后，对每个 HTTP method 产出一个计划。"""
    if not isinstance(path_item, dict):
        return []
    if "$ref" in path_item:
        notes.append(f"路径级 $ref 不展开，跳过：{path}")
        return []
    if not _path_allowed(path, config):
        return []

    plans: list[OperationPlan] = []
    shared_parameters = path_item.get("parameters") or []
    for method, operation in path_item.items():
        method_l = method.lower()
        if method_l not in _HTTP_METHODS or not isinstance(operation, dict):
            continue
        if config.read_only and method_l not in _READ_ONLY_METHODS:
            continue
        plan = _plan_one_operation(
            method_l,
            path,
            operation,
            spec,
            shared_parameters,
            config,
            used_names,
            notes,
        )
        if plan is not None:
            plans.append(plan)
    return plans


def plan_operations(
    spec: dict, config: OpenApiSpecConfig
) -> tuple[list[OperationPlan], list[str]]:
    """遍历 paths，产出操作计划列表与 notes（read_only 过滤、ACC 跳过、截断都在这里）。"""
    notes: list[str] = []
    notes.extend(misplaced_declarations(spec))
    plans: list[OperationPlan] = []
    used_names: set[str] = set()

    for path, path_item in (spec.get("paths") or {}).items():
        plans.extend(_plans_for_path(path, path_item, spec, config, used_names, notes))

    if len(plans) > config.max_operations:
        notes.append(
            f"操作数 {len(plans)} 超过 max_operations={config.max_operations}，已截断"
        )
        plans = plans[: config.max_operations]
    return plans, notes
