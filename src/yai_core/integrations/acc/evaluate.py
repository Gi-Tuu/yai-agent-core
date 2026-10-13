"""ACC 声明 → 治理决策的纯函数层：暴露、有效风险、审计提示、参数级审批。

只消费 :func:`yai_core.integrations.acc.declaration.parse_declaration` 产出的合法声明，
不做任何 IO、不读环境变量、不碰事件流；宿主如何执行这些决策属于运行时集成层。

严格 JSON 取值规则（SPEC §4.6）：

- 比较一律用 :func:`same_json_value`，**绝不把字符串隐式转成数字或布尔**；
- 调用值与绑定输入 schema 不兼容 → ``reject_invalid_arguments``，在业务操作之前；
- 条件本身无法判定（路径未声明、数值比较遇到非有限数、``contains`` 遇到标量）同样
  **失败关闭**为拒绝，绝不静默当作"条件不匹配"放行。

与参考 oracle 的一处刻意为之的差异：``GET`` + 显式 ``execution.readonly: false`` 时
oracle 给 ``low``，本实现按绑定文本的"显式声明优先"给 ``medium``（更保守）；
官方向量未覆盖该组合，见 ``tests/test_acc_conformance.py`` 的限制清单。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from yai_core.integrations.acc.declaration import AccApprovalCondition, AccDeclaration

DECISION_ALLOW = "allow"
DECISION_APPROVAL_REQUIRED = "approval_required"
DECISION_REJECT_INVALID_ARGUMENTS = "reject_invalid_arguments"

RISK_LOW = "low"
RISK_MEDIUM = "medium"
RISK_HIGH = "high"

_NUMERIC_OPS = frozenset({">", ">=", "<", "<="})
_MISSING = object()


class AccConditionError(ValueError):
    """条件无法按 ACC JSON 值模型判定（失败关闭为拒绝）。"""


def value_at_path(args: Any, path: str) -> Any:
    """按点号路径取调用值；只寻址 JSON object 成员，取不到返回 ``None`` 语义的缺失标记。"""
    current: Any = args
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return _MISSING
        current = current[part]
    return current


def schema_at_path(schema: Any, path: str) -> dict[str, Any] | None:
    """按点号路径在输入 schema 里找对应子 schema；数组路径自动下钻到 ``items``。"""
    current: Any = schema
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        if current.get("type") == "array" and isinstance(current.get("items"), dict):
            current = current["items"]
        properties = current.get("properties")
        if not isinstance(properties, dict):
            return None
        current = properties.get(part)
    return current if isinstance(current, dict) else None


def same_json_value(left: Any, right: Any) -> bool:
    """严格 JSON 相等：``true != "true"``、``0 != "0"``、对象按键集合与元素顺序无关比较。"""
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return bool(left == right)
    if isinstance(left, str) and isinstance(right, str):
        return left == right
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            same_json_value(a, b) for a, b in zip(left, right, strict=True)
        )
    if isinstance(left, dict) and isinstance(right, dict):
        return set(left) == set(right) and all(
            same_json_value(left[key], right[key]) for key in left
        )
    return False


def _is_json_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if isinstance(value, int):
        return True
    return math.isfinite(value)


def _matches_json_type(value: Any, type_name: str, path: str) -> bool:
    if type_name == "object":
        return isinstance(value, dict)
    if type_name == "array":
        return isinstance(value, list)
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "boolean":
        return isinstance(value, bool)
    if type_name == "number":
        return _is_json_number(value)
    if type_name == "integer":
        if isinstance(value, bool):
            return False
        return isinstance(value, int) or (isinstance(value, float) and value.is_integer())
    if type_name == "null":
        return value is None
    raise AccConditionError(f"{path} 使用了 ACC 子集校验器不认识的 type：{type_name!r}")


def _check_value(value: Any, schema: Any, path: str, errors: list[str]) -> None:
    """最小 JSON Schema 子集校验：``type`` / ``required`` / ``properties`` / ``items``。

    其余关键字（enum、pattern、min/max……）被忽略——这是记录在册的限制：
    审批决策只依赖类型与结构，更强的入参校验仍由宿主业务层负责。
    """
    if not isinstance(schema, dict):
        return
    declared = schema.get("type")
    if declared is not None:
        names = declared if isinstance(declared, list) else [declared]
        if not any(
            isinstance(name, str) and _matches_json_type(value, name, path) for name in names
        ):
            errors.append(f"{path} 类型不符：期望 {declared}")
            return
    if isinstance(value, dict):
        required = schema.get("required")
        if isinstance(required, list):
            for name in required:
                if isinstance(name, str) and name not in value:
                    errors.append(f"{path}.{name} 必填")
        properties = schema.get("properties")
        if isinstance(properties, dict):
            for name, sub_schema in properties.items():
                if name in value:
                    _check_value(value[name], sub_schema, f"{path}.{name}", errors)
    elif isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(value):
                _check_value(item, items, f"{path}[{index}]", errors)


def invocation_errors(args: Any, parameter_schema: Any) -> list[str]:
    """按绑定输入 schema 校验调用值，返回人类可读的错误清单（空 = 通过）。"""
    errors: list[str] = []
    try:
        _check_value(args, parameter_schema, "$", errors)
    except AccConditionError as exc:
        errors.append(str(exc))
    return errors


@dataclass(frozen=True)
class ApprovalOutcome:
    """一次调用在 ACC 层面的审批结论（``allow`` 只代表"不产生审批意图"，不等于放行）。"""

    decision: str
    reason: str = ""
    matched: tuple[AccApprovalCondition, ...] = ()
    argument_errors: tuple[str, ...] = field(default_factory=tuple)


def is_exposed(
    declaration: AccDeclaration,
    *,
    scope_allowed: bool,
    trusted_subject: bool,
    has_input_schema: bool = True,
) -> bool:
    """能力可达性：enabled、非空 scope、路由允许、需要主体时有可信主体、绑定给了入参 schema。"""
    return (
        declaration.enabled
        and bool(declaration.scope)
        and scope_allowed
        and has_input_schema
        and (not declaration.subject_required or trusted_subject)
    )


def effective_risk(declaration: AccDeclaration, *, http_method: str | None = None) -> str:
    """有效风险：显式 ``risk.level`` 优先，其次声明式只读，最后绑定原生 GET 信号，兜底 medium。"""
    if declaration.risk_level:
        return declaration.risk_level
    if declaration.readonly is True:
        return RISK_LOW
    if declaration.readonly is None and (http_method or "").upper() == "GET":
        return RISK_LOW
    return RISK_MEDIUM


def audit_sensitive(declaration: AccDeclaration) -> bool:
    """审计敏感提示：只有显式 ``audit.sensitive: true`` 才为真。"""
    return declaration.audit_sensitive is True


def _condition_matches(
    condition: AccApprovalCondition, args: Any, parameter_schema: Any
) -> bool:
    actual = value_at_path(args, condition.param)
    if condition.op == "exists":
        return actual is not _MISSING and actual is not None
    if condition.op in _NUMERIC_OPS:
        # 路径缺失 = 无从比较：oracle 同样抛错，这里失败关闭为拒绝。
        if not _is_json_number(actual):
            raise AccConditionError(f"数值比较要求 JSON number：{condition.param}")
        if not _is_json_number(condition.value):
            raise AccConditionError(f"数值比较的 value 必须是有限 number：{condition.param}")
        if condition.op == ">":
            return bool(actual > condition.value)
        if condition.op == ">=":
            return bool(actual >= condition.value)
        if condition.op == "<":
            return bool(actual < condition.value)
        return bool(actual <= condition.value)
    if condition.op == "==":
        return same_json_value(actual, condition.value)
    if condition.op == "!=":
        return not same_json_value(actual, condition.value)
    if condition.op == "in":
        if not isinstance(condition.value, list):
            raise AccConditionError(f"in 条件的 value 必须是 array：{condition.param}")
        return any(same_json_value(actual, candidate) for candidate in condition.value)
    if condition.op == "contains":
        if isinstance(actual, str) and isinstance(condition.value, str):
            return condition.value in actual
        if isinstance(actual, list):
            return any(same_json_value(item, condition.value) for item in actual)
        raise AccConditionError(f"contains 只适用于 string 或 array 参数：{condition.param}")
    raise AccConditionError(f"不支持的比较算子：{condition.op}")


def approval_outcome(
    declaration: AccDeclaration,
    args: Any,
    parameter_schema: Any = None,
) -> ApprovalOutcome:
    """按 SPEC §4.6 顺序判定审批意图：先校验入参，再校验条件路径，再无条件要求，最后 ANY。

    入参校验只在**存在 `approval.when` 条件**时进行：SPEC 把拒绝义务绑定在"条件参数与
    schema 类型不符"上，没有条件的能力不由 ACC 层代做业务入参校验（那是宿主与绑定层的活）。
    """
    conditions = declaration.approval_when

    if conditions and parameter_schema is not None:
        errors = invocation_errors(args, parameter_schema)
        if errors:
            return ApprovalOutcome(
                decision=DECISION_REJECT_INVALID_ARGUMENTS,
                reason="调用输入与绑定 input schema 不兼容：" + "；".join(errors),
                argument_errors=tuple(errors),
            )
    for condition in conditions:
        if schema_at_path(parameter_schema, condition.param) is None:
            return ApprovalOutcome(
                decision=DECISION_REJECT_INVALID_ARGUMENTS,
                reason=f"approval 条件路径未在绑定 schema 中声明：{condition.param}",
            )

    if declaration.approval_required:
        return ApprovalOutcome(
            decision=DECISION_APPROVAL_REQUIRED,
            reason="approval.required 为真，每次调用都要人工确认（条件规则不得削弱它）",
        )
    if not conditions:
        return ApprovalOutcome(decision=DECISION_ALLOW, reason="声明里没有任何审批要求")

    matched: list[AccApprovalCondition] = []
    for condition in declaration.approval_when:
        try:
            hit = _condition_matches(condition, args, parameter_schema)
        except AccConditionError as exc:
            return ApprovalOutcome(
                decision=DECISION_REJECT_INVALID_ARGUMENTS,
                reason=f"approval 条件无法按 JSON 值模型判定，已失败关闭：{exc}",
            )
        if hit:
            matched.append(condition)
    if matched:
        return ApprovalOutcome(
            decision=DECISION_APPROVAL_REQUIRED,
            reason="命中 approval.when：" + "；".join(
                condition.label or f"{condition.param} {condition.op}" for condition in matched
            ),
            matched=tuple(matched),
        )
    return ApprovalOutcome(decision=DECISION_ALLOW, reason="没有命中的审批条件")
