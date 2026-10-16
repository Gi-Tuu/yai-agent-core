"""ACC v1 声明的零依赖子集校验器（Agent Capability Contract）。

出处：`schemas/acc.v1.schema.json`（draft 2020-12，Apache-2.0），规范版本 1.0.5。
本模块手写镜像该 schema 的约束，不引入 ``jsonschema`` 等第三方依赖（内核零硬依赖红线），
判定语义与官方参考 oracle（``scripts/check-conformance.mjs``）对齐：

- **未知字段一律忽略**（``additionalProperties: true``）：根层未知键进
  :attr:`AccDeclaration.extensions` 扩展袋，不产生任何 allowlist / 风险 / 审批 / 审计含义；
  嵌套对象里的未知键被丢弃，这是记录在册的实现限制；
- **诊断分两类**：``version`` 是 JSON 整数但不等于 1 → ``unsupported_version``；
  其余任何不合法（含缺 version、version 非整数）→ ``invalid_declaration``。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from yai_core.types import APPROVAL_OPS as _APPROVAL_OPS
from yai_core.types import ApprovalRule

ACC_MAJOR_VERSION = 1
ACC_EXTENSION_KEY = "x-agent-capability"

DIAGNOSTIC_UNSUPPORTED_VERSION = "unsupported_version"
DIAGNOSTIC_INVALID_DECLARATION = "invalid_declaration"

_SCOPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_WINDOW_RE = re.compile(r"^[1-9][0-9]*(s|m|h|d)$")

_RISK_LEVELS = ("low", "medium", "high")

#: ACC 的 ``approval.when`` 条目与 YAI 原生审批规则**逐字段同构**（9 个算子一字不差），
#: 所以这里不再另造一个数据类：声明解析直接产出 :class:`yai_core.types.ApprovalRule`。
#: 别名只为兼容既有 import 路径，新代码请直接使用原生类型。
AccApprovalCondition = ApprovalRule


@dataclass(frozen=True)
class AccRateLimit:
    """``execution.rate_limit`` 提示：window 窗口内最多 count 次。"""

    count: int
    window: str


@dataclass(frozen=True)
class AccGuidance:
    """``guidance``：给模型看的用法说明，永远不是安全策略。"""

    when_to_use: str = ""
    returns: str = ""
    examples: tuple[dict[str, Any], ...] = ()
    context: tuple[str, ...] = ()


@dataclass(frozen=True)
class AccDeclaration:
    """校验通过的 ACC v1 声明；省略的布尔字段按"未要求"取值。

    ``readonly`` / ``idempotent`` 保留 ``None``（未声明）与 ``False``（显式声明非只读）的区别，
    有效风险默认值要用它判断绑定原生只读信号是否可用。
    """

    version: int
    enabled: bool
    scope: str
    risk_level: str | None = None
    subject_required: bool = False
    approval_required: bool = False
    approval_prompt: str = ""
    approval_when: tuple[AccApprovalCondition, ...] = ()
    audit_sensitive: bool = False
    readonly: bool | None = None
    idempotent: bool | None = None
    timeout_ms: int | None = None
    rate_limit: AccRateLimit | None = None
    guidance: AccGuidance | None = None
    extensions: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AccParseResult:
    """解析结果：``valid`` + 单值 ``diagnostic`` + 人类可读的完整错误清单。"""

    valid: bool
    diagnostic: str | None
    errors: tuple[str, ...] = ()
    declaration: AccDeclaration | None = None


_KNOWN_KEYS = frozenset(
    {
        "version",
        "enabled",
        "scope",
        "risk",
        "subject",
        "approval",
        "audit",
        "execution",
        "guidance",
    }
)


def _as_int(value: Any) -> int | None:
    """JSON integer：排除 bool，接受零小数浮点（``1.0`` 在 JSON 里就是整数）。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _take_object(errors: list[str], raw: dict[str, Any], key: str) -> dict[str, Any] | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        errors.append(f"{key} 必须是 object")
        return None
    return value


def _take_bool(errors: list[str], holder: dict[str, Any], key: str, path: str) -> bool | None:
    if key not in holder:
        return None
    value = holder[key]
    if not isinstance(value, bool):
        errors.append(f"{path} 必须是 boolean")
        return None
    return value


def _take_string(
    errors: list[str],
    holder: dict[str, Any],
    key: str,
    path: str,
    *,
    required: bool = False,
    min_length: int = 0,
    max_length: int | None = None,
) -> str | None:
    if key not in holder:
        if required:
            errors.append(f"{path} 必填")
        return None
    value = holder[key]
    if not isinstance(value, str):
        errors.append(f"{path} 必须是 string")
        return None
    if len(value) < min_length:
        errors.append(f"{path} 长度不得小于 {min_length}")
        return None
    if max_length is not None and len(value) > max_length:
        errors.append(f"{path} 长度不得超过 {max_length}")
        return None
    return value


def _take_int(
    errors: list[str],
    holder: dict[str, Any],
    key: str,
    path: str,
    *,
    low: int | None = None,
    high: int | None = None,
) -> int | None:
    if key not in holder:
        return None
    value = _as_int(holder[key])
    if value is None:
        errors.append(f"{path} 必须是 integer")
        return None
    if low is not None and value < low:
        errors.append(f"{path} 不得小于 {low}")
        return None
    if high is not None and value > high:
        errors.append(f"{path} 不得大于 {high}")
        return None
    return value


def _parse_condition(errors: list[str], item: Any, index: int) -> AccApprovalCondition | None:
    path = f"approval.when[{index}]"
    if not isinstance(item, dict):
        errors.append(f"{path} 必须是 object")
        return None
    param = _take_string(errors, item, "param", f"{path}.param", required=True, min_length=1)
    op = _take_string(errors, item, "op", f"{path}.op", required=True)
    label = _take_string(errors, item, "label", f"{path}.label", max_length=300) or ""
    if op is not None and op not in _APPROVAL_OPS:
        errors.append(f"{path}.op 非法：{op!r}，合法值 {list(_APPROVAL_OPS)}")
        op = None
    if param is None or op is None:
        return None
    return AccApprovalCondition(
        param=param, op=op, value=item.get("value"), has_value="value" in item, label=label
    )


def _parse_approval(
    errors: list[str], raw: dict[str, Any]
) -> tuple[bool, str, tuple[AccApprovalCondition, ...]]:
    holder = _take_object(errors, raw, "approval")
    if holder is None:
        return False, "", ()
    required = _take_bool(errors, holder, "required", "approval.required") or False
    prompt = _take_string(errors, holder, "prompt", "approval.prompt", max_length=500) or ""
    conditions: list[AccApprovalCondition] = []
    if "when" in holder:
        items = holder["when"]
        if not isinstance(items, list):
            errors.append("approval.when 必须是 array")
        else:
            for index, item in enumerate(items, start=1):
                condition = _parse_condition(errors, item, index)
                if condition is not None:
                    conditions.append(condition)
    return required, prompt, tuple(conditions)


def _parse_execution(
    errors: list[str], raw: dict[str, Any]
) -> tuple[bool | None, bool | None, int | None, AccRateLimit | None]:
    holder = _take_object(errors, raw, "execution")
    if holder is None:
        return None, None, None, None
    readonly = _take_bool(errors, holder, "readonly", "execution.readonly")
    idempotent = _take_bool(errors, holder, "idempotent", "execution.idempotent")
    timeout_ms = _take_int(
        errors, holder, "timeout_ms", "execution.timeout_ms", low=1, high=600_000
    )
    limit: AccRateLimit | None = None
    if "rate_limit" in holder:
        sub = holder["rate_limit"]
        if not isinstance(sub, dict):
            errors.append("execution.rate_limit 必须是 object")
        else:
            count = _take_int(errors, sub, "count", "execution.rate_limit.count", low=1)
            window = _take_string(
                errors, sub, "window", "execution.rate_limit.window", required=True
            )
            if window is not None and not _WINDOW_RE.match(window):
                errors.append(f"execution.rate_limit.window 格式非法：{window!r}")
                window = None
            if count is not None and window is not None:
                limit = AccRateLimit(count=count, window=window)
    return readonly, idempotent, timeout_ms, limit


def _parse_guidance(errors: list[str], raw: dict[str, Any]) -> AccGuidance | None:
    holder = _take_object(errors, raw, "guidance")
    if holder is None:
        return None
    when_to_use = _take_string(
        errors, holder, "when_to_use", "guidance.when_to_use", max_length=2000
    )
    returns = _take_string(errors, holder, "returns", "guidance.returns", max_length=2000)

    examples: tuple[dict[str, Any], ...] = ()
    if "examples" in holder:
        items = holder["examples"]
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            errors.append("guidance.examples 必须是 object 数组")
        else:
            examples = tuple(items)

    context: tuple[str, ...] = ()
    if "context" in holder:
        items = holder["context"]
        if not isinstance(items, list):
            errors.append("guidance.context 必须是 array")
        else:
            checked: list[str] = []
            for index, item in enumerate(items, start=1):
                path = f"guidance.context[{index}]"
                if not isinstance(item, str):
                    errors.append(f"{path} 必须是 string")
                elif not 1 <= len(item) <= 120:
                    errors.append(f"{path} 长度必须在 [1, 120]")
                else:
                    checked.append(item)
            context = tuple(checked)

    return AccGuidance(
        when_to_use=when_to_use or "",
        returns=returns or "",
        examples=examples,
        context=context,
    )


def parse_declaration(raw: Any) -> AccParseResult:
    """校验一份 ACC v1 声明；失败只给诊断，绝不抛异常。"""
    errors: list[str] = []
    if not isinstance(raw, dict):
        return AccParseResult(
            valid=False,
            diagnostic=DIAGNOSTIC_INVALID_DECLARATION,
            errors=(f"声明根节点必须是 object，得到 {type(raw).__name__}",),
        )

    # version 是整数但不等于 1 → 归为"不支持的主版本"；其余失败一律 invalid_declaration。
    diagnostic: str | None = None
    version: int | None = None
    if "version" not in raw:
        errors.append("缺少必填字段 version")
    else:
        parsed = _as_int(raw["version"])
        if parsed is None:
            errors.append("version 必须是 JSON integer")
        elif parsed != ACC_MAJOR_VERSION:
            diagnostic = DIAGNOSTIC_UNSUPPORTED_VERSION
            errors.append(f"不支持的 ACC 主版本：{parsed}")
        else:
            version = parsed

    enabled: bool | None = None
    if "enabled" not in raw:
        errors.append("缺少必填字段 enabled")
    elif not isinstance(raw["enabled"], bool):
        errors.append("enabled 必须是 boolean")
    else:
        enabled = raw["enabled"]

    scope: str | None = None
    if "scope" not in raw:
        errors.append("缺少必填字段 scope")
    elif not isinstance(raw["scope"], str):
        errors.append("scope 必须是 string")
    elif not 1 <= len(raw["scope"]) <= 160:
        errors.append("scope 长度必须在 [1, 160]")
    elif not _SCOPE_RE.match(raw["scope"]):
        errors.append(f"scope 格式非法：{raw['scope']!r}")
    else:
        scope = raw["scope"]

    risk_level: str | None = None
    risk = _take_object(errors, raw, "risk")
    if risk is not None:
        value = _take_string(errors, risk, "level", "risk.level")
        if value is not None and value not in _RISK_LEVELS:
            errors.append(f"risk.level 非法：{value!r}，合法值 {list(_RISK_LEVELS)}")
        else:
            risk_level = value

    subject = _take_object(errors, raw, "subject")
    subject_required = False
    if subject is not None:
        subject_required = bool(_take_bool(errors, subject, "required", "subject.required"))

    audit = _take_object(errors, raw, "audit")
    audit_sensitive = False
    if audit is not None:
        audit_sensitive = bool(_take_bool(errors, audit, "sensitive", "audit.sensitive"))

    approval_required, approval_prompt, approval_when = _parse_approval(errors, raw)
    readonly, idempotent, timeout_ms, rate_limit = _parse_execution(errors, raw)
    guidance = _parse_guidance(errors, raw)

    if errors or version is None or enabled is None or scope is None:
        if not errors:
            errors.append("声明缺少必填字段")
        return AccParseResult(
            valid=False,
            diagnostic=diagnostic or DIAGNOSTIC_INVALID_DECLARATION,
            errors=tuple(errors),
        )

    declaration = AccDeclaration(
        version=version,
        enabled=enabled,
        scope=scope,
        risk_level=risk_level,
        subject_required=subject_required,
        approval_required=approval_required,
        approval_prompt=approval_prompt,
        approval_when=approval_when,
        audit_sensitive=audit_sensitive,
        readonly=readonly,
        idempotent=idempotent,
        timeout_ms=timeout_ms,
        rate_limit=rate_limit,
        guidance=guidance,
        extensions={key: value for key, value in raw.items() if key not in _KNOWN_KEYS},
    )
    return AccParseResult(valid=True, diagnostic=None, errors=(), declaration=declaration)
