"""ACC（Agent Capability Contract）v1 兼容层：纯标准库，声明校验 + 治理决策原语。

本包只做两件事，都不碰 IO、不做运行时强制：

- :mod:`.declaration` 校验一份 ACC v1 声明，失败给 ``unsupported_version`` /
  ``invalid_declaration`` 诊断，未知字段进扩展袋且无安全含义；
- :mod:`.evaluate` 从合法声明算出"能力可达 / 有效风险 / 审计提示 / 审批意图"四类结论，
  条件比较严格遵循 ACC 的 JSON 值模型，判不出来就失败关闭。

对外声明的能力范围（自评口径见 ``docs/walkthrough/44-acc-compat.md``）：
**只声称 OpenAPI Binding Parser Profile**；把决策原语接进 ToolExecutor 之后才谈 Runtime Profile，
且 ACC 永远是 reach（可达性），最终业务授权仍归宿主。
"""

from __future__ import annotations

from yai_core.integrations.acc.declaration import (
    ACC_EXTENSION_KEY,
    ACC_MAJOR_VERSION,
    DIAGNOSTIC_INVALID_DECLARATION,
    DIAGNOSTIC_UNSUPPORTED_VERSION,
    AccApprovalCondition,
    AccDeclaration,
    AccGuidance,
    AccParseResult,
    AccRateLimit,
    parse_declaration,
)
from yai_core.integrations.acc.evaluate import (
    DECISION_ALLOW,
    DECISION_APPROVAL_REQUIRED,
    DECISION_REJECT_INVALID_ARGUMENTS,
    AccConditionError,
    ApprovalOutcome,
    approval_outcome,
    audit_sensitive,
    effective_risk,
    invocation_errors,
    is_exposed,
    same_json_value,
    schema_at_path,
    value_at_path,
)
from yai_core.integrations.acc.gate import build_gate
from yai_core.integrations.acc.openapi import (
    AccOperationBinding,
    bind_operation,
    misplaced_declarations,
)

__all__ = [
    "ACC_EXTENSION_KEY",
    "ACC_MAJOR_VERSION",
    "DECISION_ALLOW",
    "DECISION_APPROVAL_REQUIRED",
    "DECISION_REJECT_INVALID_ARGUMENTS",
    "DIAGNOSTIC_INVALID_DECLARATION",
    "DIAGNOSTIC_UNSUPPORTED_VERSION",
    "AccApprovalCondition",
    "AccConditionError",
    "AccDeclaration",
    "AccGuidance",
    "AccOperationBinding",
    "AccParseResult",
    "AccRateLimit",
    "ApprovalOutcome",
    "approval_outcome",
    "audit_sensitive",
    "build_gate",
    "bind_operation",
    "effective_risk",
    "invocation_errors",
    "is_exposed",
    "misplaced_declarations",
    "parse_declaration",
    "same_json_value",
    "schema_at_path",
    "value_at_path",
]
