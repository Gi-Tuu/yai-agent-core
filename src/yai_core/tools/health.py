"""能力接入体检：不执行 handler，只检查声明能不能被内核如实执行。

为什么需要它：schema 里写了 ``anyOf``、限流窗口写了 ``forever``、审批路径拼错，
这些都不是"运行时才暴露"的问题——它们会让一条声明看起来有护栏、实际没生效。
体检把这些"声明与执行能力不匹配"的地方在接入期列出来，宿主可以选择修声明、
换实现，或明确接受降级。

这里只做静态检查：不调用 handler、不访问网络、不改注册表。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

from yai_core.paths import schema_at_path
from yai_core.policy.approval import is_json_number
from yai_core.types import (
    APPROVAL_OPS,
    RateLimitHint,
    ToolSpec,
    parse_window_seconds,
)

Severity = Literal["error", "warning", "info"]

_KNOWN_TYPES = {"object", "array", "string", "number", "integer", "boolean", "null"}
_KNOWN_FORMATS = {"date", "date-time"}
_UNSUPPORTED_SCHEMA_KEYS = {
    "not",
    "$ref",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "uniqueItems",
    "const",
    "contains",
    "prefixItems",
    "patternProperties",
    "propertyNames",
    "if",
    "then",
    "else",
    "minProperties",
    "maxProperties",
    "dependentRequired",
    "dependentSchemas",
    "unevaluatedProperties",
    "unevaluatedItems",
    "nullable",
}
_VALID_SOURCES = {"native", "openapi", "mcp", "composite", "code"}


@dataclass(frozen=True)
class HealthFinding:
    """一条接入期发现。``code`` 稳定，供宿主做筛选/白名单。"""

    tool: str
    code: str
    severity: Severity
    message: str
    path: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "tool": self.tool,
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "path": self.path,
        }


@dataclass(frozen=True)
class CapabilityHealth:
    """一次能力体检的结果；``ok`` 只表示没有 error。"""

    findings: tuple[HealthFinding, ...]
    checked: int

    @property
    def errors(self) -> tuple[HealthFinding, ...]:
        return tuple(f for f in self.findings if f.severity == "error")

    @property
    def warnings(self) -> tuple[HealthFinding, ...]:
        return tuple(f for f in self.findings if f.severity == "warning")

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checked": self.checked,
            "errors": len(self.errors),
            "warnings": len(self.warnings),
            "findings": [finding.to_dict() for finding in self.findings],
        }

    def to_text(self) -> str:
        """人读格式；没有发现时明确说"未发现"。"""
        if not self.findings:
            return f"能力体检通过：检查 {self.checked} 个工具，未发现问题。"
        lines = [
            f"能力体检：{self.checked} 个工具，{len(self.errors)} error / "
            f"{len(self.warnings)} warning"
        ]
        for finding in self.findings:
            where = f" {finding.path}" if finding.path else ""
            lines.append(
                f"- [{finding.severity}] {finding.tool}{where} ({finding.code}) {finding.message}"
            )
        return "\n".join(lines)


def _finding(
    tool: str, code: str, severity: Severity, message: str, path: str = ""
) -> HealthFinding:
    return HealthFinding(tool=tool, code=code, severity=severity, message=message, path=path)


def _join(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


def _check_schema(
    schema: Any,
    *,
    tool: str,
    path: str,
    findings: list[HealthFinding],
    seen: set[int],
    depth: int = 0,
) -> None:
    if depth > 32:
        findings.append(
            _finding(tool, "schema_too_deep", "error", "schema 嵌套超过 32 层，已停止检查", path)
        )
        return
    if not isinstance(schema, dict):
        findings.append(_finding(tool, "schema_not_object", "error", "schema 必须是 object", path))
        return
    identity = id(schema)
    if identity in seen:
        return
    seen.add(identity)

    declared_type = schema.get("type")
    if isinstance(declared_type, str):
        if declared_type not in _KNOWN_TYPES:
            findings.append(
                _finding(
                    tool,
                    "schema_unknown_type",
                    "error",
                    f"未知 type {declared_type!r}，运行期校验会失败关闭",
                    _join(path, "type"),
                )
            )
    elif isinstance(declared_type, list):
        bad = [
            item for item in declared_type if not isinstance(item, str) or item not in _KNOWN_TYPES
        ]
        if bad or not declared_type:
            findings.append(
                _finding(
                    tool,
                    "schema_bad_type_union",
                    "error",
                    f"type 数组含非法/未知成员：{bad or declared_type!r}",
                    _join(path, "type"),
                )
            )

    for key, value in schema.items():
        if key in _UNSUPPORTED_SCHEMA_KEYS:
            findings.append(
                _finding(
                    tool,
                    "schema_unsupported_key",
                    "warning",
                    f"{key} 不参与当前入参校验（不知道不等于校验过）",
                    _join(path, key),
                )
            )
        elif key == "format":
            if not isinstance(value, str):
                findings.append(
                    _finding(
                        tool,
                        "schema_bad_format",
                        "warning",
                        "format 必须是字符串",
                        _join(path, key),
                    )
                )
            elif value not in _KNOWN_FORMATS:
                findings.append(
                    _finding(
                        tool,
                        "schema_format_not_enforced",
                        "warning",
                        f"format={value!r} 当前只做注解，不参与校验",
                        _join(path, key),
                    )
                )
        elif key == "pattern":
            if not isinstance(value, str):
                findings.append(
                    _finding(
                        tool,
                        "schema_bad_pattern",
                        "warning",
                        "pattern 必须是字符串",
                        _join(path, key),
                    )
                )
            else:
                try:
                    re.compile(value)
                except re.error as exc:
                    findings.append(
                        _finding(
                            tool,
                            "schema_invalid_pattern",
                            "warning",
                            f"正则无法编译，运行期会跳过该约束：{exc}",
                            _join(path, key),
                        )
                    )
        elif key == "enum":
            if not isinstance(value, list):
                findings.append(
                    _finding(
                        tool, "schema_bad_enum", "warning", "enum 必须是数组", _join(path, key)
                    )
                )
            elif not value:
                findings.append(
                    _finding(
                        tool,
                        "schema_empty_enum",
                        "warning",
                        "空 enum 会让任何值都不合法",
                        _join(path, key),
                    )
                )
        elif key == "required":
            properties = schema.get("properties")
            if not isinstance(value, list):
                findings.append(
                    _finding(
                        tool,
                        "schema_bad_required",
                        "warning",
                        "required 必须是数组",
                        _join(path, key),
                    )
                )
            elif isinstance(properties, dict):
                missing = [name for name in value if name not in properties]
                if missing:
                    findings.append(
                        _finding(
                            tool,
                            "schema_required_undeclared",
                            "warning",
                            f"required 含未在 properties 声明的字段：{missing}",
                            _join(path, key),
                        )
                    )
        elif key in ("properties", "$defs"):
            if isinstance(value, dict):
                for name, sub in value.items():
                    _check_schema(
                        sub,
                        tool=tool,
                        path=_join(_join(path, key), name),
                        findings=findings,
                        seen=seen,
                        depth=depth + 1,
                    )
            elif key == "properties":
                findings.append(
                    _finding(
                        tool,
                        "schema_bad_properties",
                        "warning",
                        "properties 必须是 object",
                        _join(path, key),
                    )
                )
        elif key == "items":
            if isinstance(value, dict):
                _check_schema(
                    value,
                    tool=tool,
                    path=_join(path, "items"),
                    findings=findings,
                    seen=seen,
                    depth=depth + 1,
                )
            elif isinstance(value, list):
                findings.append(
                    _finding(
                        tool,
                        "schema_tuple_items",
                        "warning",
                        "元组形式的 items 当前不参与逐项校验",
                        _join(path, "items"),
                    )
                )
        elif key == "additionalProperties":
            findings.append(
                _finding(
                    tool,
                    "schema_unsupported_key",
                    "warning",
                    "additionalProperties 不参与当前入参校验",
                    _join(path, key),
                )
            )
            if isinstance(value, dict):
                _check_schema(
                    value,
                    tool=tool,
                    path=_join(path, key),
                    findings=findings,
                    seen=seen,
                    depth=depth + 1,
                )
        elif key in ("anyOf", "oneOf", "allOf"):
            findings.append(
                _finding(
                    tool,
                    "schema_unsupported_key",
                    "warning",
                    f"{key} 不参与当前入参校验（不知道不等于校验过）",
                    _join(path, key),
                )
            )
            if isinstance(value, list):
                for index, sub in enumerate(value):
                    _check_schema(
                        sub,
                        tool=tool,
                        path=f"{_join(path, key)}[{index}]",
                        findings=findings,
                        seen=seen,
                        depth=depth + 1,
                    )

    if declared_type == "array" and "items" not in schema:
        findings.append(
            _finding(
                tool,
                "schema_array_without_items",
                "warning",
                "array 未声明 items，元素内容不受校验",
                path,
            )
        )


def _check_approval(spec: ToolSpec, findings: list[HealthFinding]) -> None:
    policy = spec.governance.approval
    if policy is None:
        return
    if not isinstance(policy.required, bool):
        findings.append(
            _finding(
                spec.name, "approval_required_not_bool", "error", "approval.required 必须是布尔值"
            )
        )
    if not isinstance(policy.prompt, str):
        findings.append(
            _finding(spec.name, "approval_prompt_not_str", "error", "approval.prompt 必须是字符串")
        )
    if not isinstance(policy.when, (tuple, list)):
        findings.append(
            _finding(spec.name, "approval_when_not_sequence", "error", "approval.when 必须是数组")
        )
        return
    for index, rule in enumerate(policy.when, start=1):
        label = f"approval.when[{index}]"
        if not isinstance(rule.param, str) or not rule.param.strip():
            findings.append(
                _finding(spec.name, "approval_bad_param", "error", "审批条件缺少合法 param", label)
            )
            continue
        if rule.op not in APPROVAL_OPS:
            findings.append(
                _finding(
                    spec.name,
                    "approval_unknown_op",
                    "error",
                    f"未知审批算子 {rule.op!r}，运行期会失败关闭",
                    label,
                )
            )
        if schema_at_path(spec.input_schema, rule.param) is None:
            findings.append(
                _finding(
                    spec.name,
                    "approval_path_missing",
                    "error",
                    f"审批路径 {rule.param!r} 未在 input_schema 中声明，运行期会拒绝每次调用",
                    label,
                )
            )
        if rule.op in (">", ">=", "<", "<=") and not is_json_number(rule.value):
            findings.append(
                _finding(
                    spec.name,
                    "approval_numeric_value",
                    "error",
                    "数值审批条件的 value 必须是有限 number",
                    label,
                )
            )
        if rule.op == "in" and not isinstance(rule.value, list):
            findings.append(
                _finding(
                    spec.name, "approval_in_value", "error", "in 条件的 value 必须是数组", label
                )
            )
        if not isinstance(rule.label, str):
            findings.append(
                _finding(
                    spec.name, "approval_bad_label", "warning", "审批条件 label 应为字符串", label
                )
            )


def _check_governance(spec: ToolSpec, findings: list[HealthFinding]) -> None:
    governance = spec.governance
    timeout = governance.timeout_ms
    if timeout is not None and (
        isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0
    ):
        findings.append(
            _finding(spec.name, "governance_bad_timeout", "error", "timeout_ms 必须是正整数")
        )

    hint = governance.rate_limit
    if hint is not None:
        if not isinstance(hint, RateLimitHint):
            findings.append(
                _finding(
                    spec.name,
                    "governance_bad_rate_limit",
                    "error",
                    "rate_limit 必须是 RateLimitHint",
                )
            )
        else:
            if isinstance(hint.count, bool) or not isinstance(hint.count, int) or hint.count <= 0:
                findings.append(
                    _finding(
                        spec.name, "rate_limit_bad_count", "error", "rate_limit.count 必须是正整数"
                    )
                )
            if not isinstance(hint.window, str) or parse_window_seconds(hint.window) is None:
                findings.append(
                    _finding(
                        spec.name,
                        "rate_limit_bad_window",
                        "error",
                        f"窗口 {getattr(hint, 'window', None)!r} 无法解析，运行期不会拦截",
                    )
                )

    if not isinstance(governance.audit_sensitive, bool):
        findings.append(
            _finding(spec.name, "governance_audit_flag", "error", "audit_sensitive 必须是布尔值")
        )
    properties = (
        spec.input_schema.get("properties") if isinstance(spec.input_schema, dict) else None
    )
    for path in governance.sensitive:
        if not isinstance(path, str) or not path.strip():
            findings.append(
                _finding(
                    spec.name, "governance_bad_sensitive", "error", "sensitive 路径必须是非空字符串"
                )
            )
        elif (
            isinstance(properties, dict)
            and properties
            and isinstance(spec.input_schema, dict)
            and schema_at_path(spec.input_schema, path) is None
        ):
            findings.append(
                _finding(
                    spec.name,
                    "sensitive_path_missing",
                    "warning",
                    f"脱敏路径 {path!r} 不在 schema 中，实际不会脱敏",
                )
            )
    _check_approval(spec, findings)


def _check_tool(
    spec: ToolSpec,
    *,
    registry: Any | None,
    sandbox_available: bool,
    findings: list[HealthFinding],
) -> None:
    if not isinstance(spec.name, str) or not spec.name.strip():
        findings.append(
            _finding(str(spec.name), "tool_bad_name", "error", "工具名必须是非空字符串")
        )
    if spec.source not in _VALID_SOURCES:
        findings.append(
            _finding(spec.name, "tool_bad_source", "error", f"未知工具来源 {spec.source!r}")
        )
    if not spec.description.strip():
        findings.append(
            _finding(spec.name, "tool_empty_description", "warning", "工具没有描述，模型难以选择")
        )
    if not isinstance(spec.input_schema, dict):
        findings.append(
            _finding(spec.name, "tool_bad_input_schema", "error", "input_schema 必须是 object")
        )
    else:
        _check_schema(
            spec.input_schema,
            tool=spec.name,
            path="$",
            findings=findings,
            seen=set(),
        )
    _check_governance(spec, findings)

    if spec.source == "composite":
        if not spec.steps:
            findings.append(
                _finding(spec.name, "composite_without_steps", "error", "组合工具没有步骤")
            )
        for index, step in enumerate(spec.steps or (), start=1):
            if not isinstance(step.tool, str) or not step.tool.strip():
                findings.append(
                    _finding(
                        spec.name,
                        "composite_bad_step",
                        "error",
                        "组合步骤缺少工具名",
                        f"steps[{index}]",
                    )
                )
                continue
            if registry is not None:
                if not registry.has(step.tool):
                    findings.append(
                        _finding(
                            spec.name,
                            "composite_missing_tool",
                            "error",
                            f"步骤引用了未注册工具 {step.tool!r}",
                            f"steps[{index}]",
                        )
                    )
                elif getattr(registry.get(step.tool), "source", "") == "composite":
                    findings.append(
                        _finding(
                            spec.name,
                            "composite_nested",
                            "error",
                            "组合工具不能嵌套引用另一个组合工具",
                            f"steps[{index}]",
                        )
                    )
    elif spec.source == "code":
        if not spec.code:
            findings.append(
                _finding(spec.name, "code_without_source", "warning", "代码工具没有代码内容")
            )
        if not sandbox_available:
            findings.append(
                _finding(
                    spec.name,
                    "code_without_sandbox",
                    "warning",
                    "宿主未提供 ToolSandbox，代码工具不可执行",
                )
            )
        if spec.handler is not None:
            findings.append(
                _finding(spec.name, "code_has_handler", "warning", "代码工具不应绑定 handler")
            )
    elif spec.handler is None:
        findings.append(
            _finding(
                spec.name,
                "handler_unbound",
                "info",
                "声明型工具没有执行 handler；注册后调用会失败，需绑定后端或走 MCP/OpenAPI",
            )
        )


def check_tool_specs(
    specs: Iterable[ToolSpec],
    *,
    registry: Any | None = None,
    sandbox_available: bool = True,
) -> CapabilityHealth:
    """对一个能力集合做静态体检；``registry`` 用于检查组合步骤是否存在。"""
    items = list(specs)
    findings: list[HealthFinding] = []
    for spec in items:
        if not isinstance(spec, ToolSpec):
            findings.append(
                _finding(repr(spec), "tool_not_spec", "error", "集合中含非 ToolSpec 对象")
            )
            continue
        _check_tool(
            spec,
            registry=registry,
            sandbox_available=sandbox_available,
            findings=findings,
        )
    return CapabilityHealth(findings=tuple(findings), checked=len(items))
