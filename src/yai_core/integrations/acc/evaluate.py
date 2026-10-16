"""ACC 声明 → 治理决策的翻译层：暴露、有效风险、审计提示，以及把审批要求交回内核判定。

**这里没有参数级审批的判定逻辑**——那套语义（严格 JSON 值、失败关闭、判定顺序）是
YAI 原生的，住在 :mod:`yai_core.policy.approval`。本模块只做两件 ACC 自己的事：

1. 把已校验的 :class:`~yai_core.integrations.acc.declaration.AccDeclaration` 里的
   ``approval.required`` / ``approval.prompt`` / ``approval.when`` 组装成原生
   :class:`~yai_core.types.ApprovalPolicy`，交给内核判；
2. 算 ACC 独有的三个派生量：能力是否暴露（:func:`is_exposed`）、有效风险
   （:func:`effective_risk`）、审计宽口径提示（:func:`audit_sensitive`）。

这样宿主接不接 ACC 得到的**审批行为是同一套**：ACC 只是另一种写法的清单。

保留的严格 JSON 取值规则（SPEC §4.6，实际执行在内核模块里）：

- 比较不做任何跨类型隐式转换；
- 调用值与绑定输入 schema 不兼容 → ``reject_invalid_arguments``，在业务操作之前；
- 条件本身无法判定（路径未声明、没有可校验的绑定 schema、数值比较遇到非有限数、
  ``contains`` 遇到标量）同样**失败关闭**为拒绝，绝不静默当作"条件不匹配"放行。

与参考 oracle 的一处刻意为之的差异：``GET`` + 显式 ``execution.readonly: false`` 时
oracle 给 ``low``，本实现按绑定文本的"显式声明优先"给 ``medium``（更保守）；
官方向量未覆盖该组合，见 ``tests/test_acc_conformance.py`` 的限制清单。
"""

from __future__ import annotations

from typing import Any

from yai_core.integrations.acc.declaration import AccDeclaration
from yai_core.policy.approval import (
    DECISION_ALLOW,
    DECISION_APPROVAL_REQUIRED,
    DECISION_REJECT_INVALID_ARGUMENTS,
    ApprovalError,
    ApprovalOutcome,
    argument_errors,
    evaluate_approval,
    same_json_value,
    schema_at_path,
    value_at_path,
)
from yai_core.types import ApprovalPolicy
from yai_core.types import ApprovalRule as AccApprovalCondition

RISK_LOW = "low"
RISK_MEDIUM = "medium"
RISK_HIGH = "high"

#: 兼容别名：判定原语搬进内核后，这里的错误类型就是内核的错误类型。
AccConditionError = ApprovalError

__all__ = [
    "AccApprovalCondition",
    "AccConditionError",
    "ApprovalOutcome",
    "DECISION_ALLOW",
    "DECISION_APPROVAL_REQUIRED",
    "DECISION_REJECT_INVALID_ARGUMENTS",
    "RISK_HIGH",
    "RISK_LOW",
    "RISK_MEDIUM",
    "approval_outcome",
    "audit_sensitive",
    "effective_risk",
    "invocation_errors",
    "is_exposed",
    "same_json_value",
    "schema_at_path",
    "to_approval_policy",
    "value_at_path",
]


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


def to_approval_policy(declaration: AccDeclaration) -> ApprovalPolicy:
    """把声明里的审批意图搬进原生策略；条件条目本来就是 :class:`ApprovalRule`，无需逐条转换。"""
    return ApprovalPolicy(
        required=declaration.approval_required,
        prompt=declaration.approval_prompt,
        when=declaration.approval_when,
    )


def invocation_errors(args: Any, parameter_schema: Any) -> list[str]:
    """按绑定输入 schema 校验调用值，返回人类可读的错误清单（空 = 通过）。"""
    return argument_errors(args, parameter_schema)


def approval_outcome(
    declaration: AccDeclaration,
    args: Any,
    parameter_schema: Any = None,
) -> ApprovalOutcome:
    """把声明翻译成原生策略后由内核判定；结论里的 ``matched`` 是 :class:`ApprovalRule`。

    判定顺序（入参先于条件、``required`` 占优、``when`` 取 ANY）由内核定义，
    ACC 不再自带一套"看起来一样"的实现——两份判定迟早漂移，而漂移的那份没人测试。
    """
    return evaluate_approval(to_approval_policy(declaration), args, parameter_schema)
