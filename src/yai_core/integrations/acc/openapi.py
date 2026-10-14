"""OpenAPI 绑定：读 operation 级 ``x-agent-capability``，产出"能不能暴露"的治理视图。

规范出处：ACC 的 OpenAPI binding（Normative）。四条落地铁律：

- 只认 **operation 对象**上的扩展；根节点 / pathItem / 参数 / 响应层出现的都不算声明，
  但会作为"贴错位置"的诊断报出来（``misplaced_declarations``）；
- 声明格式错误、主版本不认识、绑定输入 schema 不足以安全判定 → **整条 operation 跳过并出
  诊断**，MUST NOT 回退成"一个没有治理的同名工具"暴露给模型；
- **没有该扩展的 operation 完全不受影响**，未采用 ACC 的宿主零行为变化；
- ``approval.when.param`` 必须能在归一化后的绑定视图里解析出来；落在"同名参数同时来自
  query 与 body"的归一化歧义上时，整条不暴露——绑定允许"拒绝该条件"，但被静默拒掉的条件
  等于把声明里的审批承诺削薄，削的方向是放行更多，所以取更保守的那一档。

本模块不执行任何东西：暴露与否、有效风险、审批意图都在这里算清楚，
交给 OpenAPI 桥决定注册与注册成什么样的 :class:`ToolSpec`。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from yai_core.integrations.acc.declaration import (
    ACC_EXTENSION_KEY,
    AccDeclaration,
    parse_declaration,
)
from yai_core.integrations.acc.evaluate import (
    audit_sensitive as _audit_sensitive,
)
from yai_core.integrations.acc.evaluate import (
    effective_risk as _effective_risk,
)
from yai_core.integrations.acc.evaluate import (
    is_exposed as _is_exposed,
)
from yai_core.integrations.acc.evaluate import (
    schema_at_path,
)


@dataclass(frozen=True)
class AccOperationBinding:
    """一个 operation 的 ACC 绑定结果。"""

    #: operation 上有没有 ACC 扩展；False 表示这条与 ACC 无关，调用方应原样处理。
    declared: bool
    #: 是否允许暴露给模型（declared=False 时恒为 True，交回原逻辑）。
    exposed: bool
    tool_name: str = ""
    scope: str = ""
    declaration: AccDeclaration | None = None
    #: 供决策用的绑定输入视图（YAI 归一化后的 JSON Schema）。
    input_schema: dict[str, Any] = field(default_factory=dict)
    effective_risk: str = ""
    audit_sensitive: bool = False
    diagnostics: tuple[str, ...] = ()
    #: 已校验声明的原样快照，透传给 ToolSpec.governance.acc（内核不解释）。
    raw: dict[str, Any] = field(default_factory=dict)


def _skip(
    tool_name: str,
    raw: dict[str, Any],
    diagnostics: tuple[str, ...],
    *,
    declared: bool = True,
) -> AccOperationBinding:
    return AccOperationBinding(
        declared=declared,
        exposed=False,
        tool_name=tool_name,
        raw=raw,
        diagnostics=diagnostics,
    )


def _ambiguous_names(query_params: tuple[str, ...], body_fields: tuple[str, ...]) -> set[str]:
    """同名参数跨位置出现 = 归一化路径歧义（body 会覆盖 query，绑定禁止静默二选一）。"""
    return {str(name) for name in query_params} & {str(name) for name in body_fields}


def bind_operation(
    operation: dict[str, Any],
    *,
    method: str,
    tool_name: str,
    input_schema: dict[str, Any],
    query_params: tuple[str, ...] = (),
    body_fields: tuple[str, ...] = (),
    scope_policy: Callable[[str], bool] | None = None,
    trusted_subject: bool = False,
) -> AccOperationBinding:
    """把一个 operation 的 ACC 扩展算成暴露/风险/审计绑定。"""
    raw = operation.get(ACC_EXTENSION_KEY) if isinstance(operation, dict) else None
    if raw is None:
        return AccOperationBinding(
            declared=False, exposed=True, tool_name=tool_name, input_schema=input_schema
        )
    if not isinstance(raw, dict):
        return _skip(
            tool_name, {}, (f"{tool_name}：{ACC_EXTENSION_KEY} 不是 object，已跳过该操作",)
        )

    result = parse_declaration(raw)
    if not result.valid or result.declaration is None:
        detail = "；".join(result.errors[:3])
        return _skip(
            tool_name,
            dict(raw),
            (
                f"{tool_name}：ACC 声明不可用（{result.diagnostic}）——{detail}；"
                "按绑定不回退成无治理工具，已跳过该操作",
            ),
        )
    declaration = result.declaration

    ambiguous = _ambiguous_names(query_params, body_fields)
    diagnostics: list[str] = []
    for condition in declaration.approval_when:
        head = condition.param.split(".")[0]
        if head in ambiguous:
            diagnostics.append(
                f"{tool_name}：approval 条件参数 {condition.param} 同时来自 query 与 body，"
                "归一化路径有歧义，已跳过该操作"
            )
        elif schema_at_path(input_schema, condition.param) is None:
            diagnostics.append(
                f"{tool_name}：approval 条件参数 {condition.param} 不在绑定输入 schema 里，"
                "输入不足以安全判定，已跳过该操作"
            )
    if diagnostics:
        return _skip(tool_name, dict(raw), tuple(diagnostics), declared=True)

    scope_allowed = scope_policy is None or bool(scope_policy(declaration.scope))
    exposed = _is_exposed(
        declaration,
        scope_allowed=scope_allowed,
        trusted_subject=trusted_subject,
        has_input_schema=True,
    )
    if not exposed:
        if not declaration.enabled:
            reason = "enabled=false"
        elif declaration.subject_required and not trusted_subject:
            reason = "要求可信主体但当前上下文没有"
        elif not scope_allowed:
            reason = f"宿主路由未允许 scope {declaration.scope}"
        else:
            reason = "暴露条件不满足"
        return AccOperationBinding(
            declared=True,
            exposed=False,
            tool_name=tool_name,
            scope=declaration.scope,
            declaration=declaration,
            input_schema=input_schema,
            raw=dict(raw),
            diagnostics=(f"{tool_name}：ACC 声明有效但不暴露（{reason}）",),
        )

    risk = _effective_risk(declaration, http_method=method)
    return AccOperationBinding(
        declared=True,
        exposed=True,
        tool_name=tool_name,
        scope=declaration.scope,
        declaration=declaration,
        input_schema=input_schema,
        effective_risk=risk,
        audit_sensitive=_audit_sensitive(declaration),
        diagnostics=(),
        raw=dict(raw),
    )


def misplaced_declarations(spec: dict[str, Any]) -> list[str]:
    """找出贴错位置的 ACC 扩展：规范要求它只能挂在 operation 对象上。"""
    notes: list[str] = []
    if not isinstance(spec, dict):
        return notes
    if ACC_EXTENSION_KEY in spec:
        notes.append(f"根节点上的 {ACC_EXTENSION_KEY} 不被绑定读取（只认 operation 级）")
    paths = spec.get("paths")
    if isinstance(paths, dict):
        for path, item in paths.items():
            if isinstance(item, dict) and ACC_EXTENSION_KEY in item:
                notes.append(
                    f"pathItem {path} 上的 {ACC_EXTENSION_KEY} 不被绑定读取（只认 operation 级）"
                )
    return notes
