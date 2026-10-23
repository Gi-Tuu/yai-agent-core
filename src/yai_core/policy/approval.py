"""YAI 原生的参数级审批判定：纯函数、零依赖、严格 JSON 值语义、判不出来就失败关闭。

这块逻辑**属于内核**，不属于任何外部协议：宿主只想说"退款超过 1000 元要人确认"，
不该因此被迫去实现一个能力契约解析器。外部契约（``integrations/acc``）反过来
把它的声明**翻译进**这里的模型，而不是这里去依赖那边。

三条不可让步的语义：

1. **严格 JSON 值比较**：``"100" != 100``、``true != 1``、``0 != "0"``。绝不为了"看起来
   能用"把字符串悄悄转成数字——静默转换会让"参数是字符串 100"这种输入绕过金额门槛。
2. **失败关闭**：条件路径没在绑定 schema 里声明、数值比较遇到非有限数、``contains``
   遇到标量、算子不认识 —— 一律判成"拒绝执行"，绝不静默当作"条件不匹配"放行。
3. **只能抬不能放**：本模块的产出是"要不要问人 / 要不要拒"，永远不会返回"跳过宿主
   权限策略"。放行与否仍由 :class:`yai_core.spi.policy.PermissionPolicy` 说了算。

入参校验只实现 ``type`` / ``required`` / ``properties`` / ``items`` 四个关键字，
``enum`` / ``pattern`` / ``min-max`` 被忽略——这是记录在册的限制，更强的业务入参
校验归宿主。与 :mod:`yai_core.tools.schema` 的清洗器不是一回事：那个管 schema 能不能
安全地交给模型，这个管调用值能不能安全地进入决策。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from yai_core.paths import schema_at_path, value_candidates
from yai_core.types import APPROVAL_OPS, ApprovalPolicy, ApprovalRule

#: 三种结论。``ALLOW`` 只代表"这一层不产生审批意图"，不等于放行——放行看权限策略。
DECISION_ALLOW = "allow"
DECISION_APPROVAL_REQUIRED = "approval_required"
DECISION_REJECT_INVALID_ARGUMENTS = "reject_invalid_arguments"

_NUMERIC_OPS = frozenset({">", ">=", "<", "<="})


class _Missing:
    """"路径上没有这个值"的哨兵。不能用 ``None``：JSON 里 null 是一个**合法值**。"""

    def __repr__(self) -> str:  # pragma: no cover - 只用于调试输出
        return "<MISSING>"


MISSING = _Missing()


class ApprovalError(ValueError):
    """条件无法按 JSON 值语义判定（调用方必须把它当作"拒绝"，不是"跳过"）。"""


def value_at_path(args: Any, path: str) -> Any:
    """按点号路径取**单个**调用值；只寻址 JSON object 成员，取不到返回 :data:`MISSING`。

    这是"取单值"的窄解析器（历史契约，保留给只需要一个值的调用方）。**审批判定走
    :func:`yai_core.paths.value_candidates`**：候选集解析与 schema 侧下钻语义一致，
    数组元素（``rows.0.amount`` / ``rows.amount``）也能寻址——两套解析器各说各话时，
    条件会"schema 说已声明、取值说取不到"，于是永远不命中，护栏静默失效。
    """
    current: Any = args
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return MISSING
        current = current[part]
    return current


def same_json_value(left: Any, right: Any) -> bool:
    """严格 JSON 相等：不做任何跨类型隐式转换；数组按序、对象按键集合比较。"""
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


def is_json_number(value: Any) -> bool:
    """JSON number：bool 不是数字，NaN / Infinity 不是数字（它们会让比较失去意义）。"""
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
        return is_json_number(value)
    if type_name == "integer":
        if isinstance(value, bool):
            return False
        return isinstance(value, int) or (isinstance(value, float) and value.is_integer())
    if type_name == "null":
        return value is None
    raise ApprovalError(f"{path} 使用了本实现不认识的 type：{type_name!r}")


def _check_value(value: Any, schema: Any, path: str, errors: list[str]) -> None:
    """最小 JSON Schema 子集。

    实现的键：``type`` / ``required`` / ``properties`` / ``items``（结构与类型），
    以及 ``enum`` / ``pattern`` / ``minimum`` / ``maximum`` / ``minLength`` /
    ``maxLength`` / ``minItems`` / ``maxItems`` / ``format``（取值域）。

    范围是**故意有限**的：多实现几个键就多几分"看起来很严"，但没实现的键会让人
    误以为已经校验过。仍未实现（记录在册的限制）：``anyOf`` / ``oneOf`` /
    ``allOf`` / ``$ref`` / ``additionalProperties`` / ``exclusiveMinimum`` /
    ``exclusiveMaximum`` / ``multipleOf`` / ``uniqueItems`` / ``const``。

    ``anyOf`` / ``oneOf`` / ``$ref`` 可能给出比顶层 ``type`` 更宽的合法分支；
    遇到它们时整段跳过校验，避免把合法调用误判为非法。能力体检会把这些键
    标成"未校验"，宿主可据此决定是否改用可执行的 schema 子集。
    """
    if not isinstance(schema, dict):
        return
    # 这些键可能**放宽**取值域（anyOf/oneOf/$ref 可以给出与顶层 type 不同的
    # 合法分支）。只实现子集时继续按 type 拒绝，会把 JSON Schema 判定合法的调用
    # 误判为非法——兼容性优先：整段跳过校验，由能力体检提示"这层没校验"。
    if any(key in schema for key in ("anyOf", "oneOf", "$ref")):
        return
    declared = schema.get("type")
    if declared is not None:
        names = declared if isinstance(declared, list) else [declared]
        if not any(
            isinstance(name, str) and _matches_json_type(value, name, path) for name in names
        ):
            errors.append(f"{path} 类型不符：期望 {declared}")
            return
    _check_value_domain(value, schema, path, errors)
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


def _check_value_domain(value: Any, schema: dict, path: str, errors: list[str]) -> None:
    """校验**取值域**（与结构校验分开）：enum / 范围 / 长度 / 格式。

    口径：

    - ``enum``：严格 JSON 相等（``"1" != 1``，``True != 1``）——沿用
      :func:`same_json_value`，不为了"宽容"而做跨类型相等；
    - ``pattern``：用 ``re.search``（JSON Schema 语义是"部分匹配"，不是整串匹配）；
      模式本身非法时**跳过**（不因此拒绝调用——声明写错不该让业务操作被拦，但
      也不该假装校验过，所以只在错误清单里留一句提示？→ 这里选择跳过并保持静默，
      与"未实现的键一律不参与判定"的口径一致）；
    - ``minimum`` / ``maximum``：仅当值是 JSON number 时才比（字符串不比大小）；
    - ``format``：只实现 ``date-time`` 与 ``date`` 两种（能可靠判定的那两种），
      其余格式**不参与判定**（JSON Schema 的 format 默认是注解而非断言）。
    """
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        if not any(same_json_value(value, item) for item in enum):
            errors.append(f"{path} 不在允许取值范围内：{enum}")

    pattern = schema.get("pattern")
    if isinstance(pattern, str) and pattern and isinstance(value, str):
        try:
            if re.search(pattern, value) is None:
                errors.append(f"{path} 不满足格式约束 {pattern!r}")
        except re.error:
            pass  # 声明里的模式本身非法：不参与判定，也不因此拒绝调用

    minimum = schema.get("minimum")
    if isinstance(minimum, (int, float)) and not isinstance(minimum, bool):
        if is_json_number(value) and value < minimum:
            errors.append(f"{path} 小于下限 {minimum}")
    maximum = schema.get("maximum")
    if isinstance(maximum, (int, float)) and not isinstance(maximum, bool):
        if is_json_number(value) and value > maximum:
            errors.append(f"{path} 大于上限 {maximum}")

    if isinstance(value, str):
        min_length = schema.get("minLength")
        if isinstance(min_length, int) and not isinstance(min_length, bool):
            if len(value) < min_length:
                errors.append(f"{path} 长度小于 {min_length}")
        max_length = schema.get("maxLength")
        if isinstance(max_length, int) and not isinstance(max_length, bool):
            if len(value) > max_length:
                errors.append(f"{path} 长度大于 {max_length}")
    elif isinstance(value, list):
        min_items = schema.get("minItems")
        if isinstance(min_items, int) and not isinstance(min_items, bool):
            if len(value) < min_items:
                errors.append(f"{path} 元素少于 {min_items}")
        max_items = schema.get("maxItems")
        if isinstance(max_items, int) and not isinstance(max_items, bool):
            if len(value) > max_items:
                errors.append(f"{path} 元素多于 {max_items}")

    fmt = schema.get("format")
    if isinstance(fmt, str) and isinstance(value, str):
        if fmt == "date-time" and not _looks_like_datetime(value):
            errors.append(f"{path} 不是 ISO 8601 日期时间")
        elif fmt == "date" and not _looks_like_date(value):
            errors.append(f"{path} 不是 ISO 8601 日期")


def _looks_like_datetime(text: str) -> bool:
    """粗判 ISO 8601 日期时间（``YYYY-MM-DDTHH:MM`` 开头，允许 Z / 偏移 / 秒）。"""
    if len(text) < 16 or text[10] not in ("T", "t", " "):
        return False
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _looks_like_date(text: str) -> bool:
    """粗判 ISO 8601 日期（``YYYY-MM-DD``）：整串都必须是日期，不能只看前 10 位。"""
    if len(text) != 10:
        return False
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def argument_errors(args: Any, parameter_schema: Any) -> list[str]:
    """按绑定输入 schema 校验调用值，返回人类可读错误清单（空 = 通过）。"""
    errors: list[str] = []
    try:
        _check_value(args, parameter_schema, "$", errors)
    except ApprovalError as exc:
        errors.append(str(exc))
    return errors


def rule_matches(rule: ApprovalRule, args: Any) -> bool:
    """判定单条规则是否命中；语义上判不出来时抛 :class:`ApprovalError`。

    取值走**候选集**（:func:`yai_core.paths.value_candidates`）：数组内嵌参数同样能寻址
    （``rows.0.amount`` 与 ``rows.amount`` 都行），判定语义是 ANY——命中任一候选即命中，
    宁可多问一次人，不可漏问。

    "这次调用没给这个值"时，各算子的处理**沿用既有口径不变**（不放松也不收紧）：
    数值比较与 ``contains`` 判不出来 → 失败关闭（拒绝）；``==`` / ``in`` / ``exists``
    不命中；``!=`` 命中（"不等于期望安全值"却拿不到值，本身就该问人）。
    """
    candidates = value_candidates(args, rule.param)
    if rule.op == "exists":
        return any(candidate is not None for candidate in candidates)
    if rule.op in _NUMERIC_OPS:
        if not is_json_number(rule.value):
            raise ApprovalError(f"数值比较的 value 必须是有限 number：{rule.param}")
        if not candidates:
            raise ApprovalError(f"数值比较要求 JSON number：{rule.param}")
        for actual in candidates:
            if not is_json_number(actual):
                raise ApprovalError(f"数值比较要求 JSON number：{rule.param}")
            if rule.op == ">" and actual > rule.value:
                return True
            if rule.op == ">=" and actual >= rule.value:
                return True
            if rule.op == "<" and actual < rule.value:
                return True
            if rule.op == "<=" and actual <= rule.value:
                return True
        return False
    if rule.op == "==":
        return any(same_json_value(candidate, rule.value) for candidate in candidates)
    if rule.op == "!=":
        # 拿不到值也算"不等于期望值"：条件形同虚设比多问一次人危险。
        if not candidates:
            return True
        return any(not same_json_value(candidate, rule.value) for candidate in candidates)
    if rule.op == "in":
        if not isinstance(rule.value, list):
            raise ApprovalError(f"in 条件的 value 必须是 array：{rule.param}")
        return any(
            same_json_value(candidate, item)
            for candidate in candidates
            for item in rule.value
        )
    if rule.op == "contains":
        if not candidates:
            raise ApprovalError(f"contains 只适用于 string 或 array 参数：{rule.param}")
        for actual in candidates:
            if isinstance(actual, str) and isinstance(rule.value, str):
                if rule.value in actual:
                    return True
                continue
            if isinstance(actual, list):
                if any(same_json_value(item, rule.value) for item in actual):
                    return True
                continue
            raise ApprovalError(f"contains 只适用于 string 或 array 参数：{rule.param}")
        return False
    raise ApprovalError(f"不支持的比较算子：{rule.op}")


@dataclass(frozen=True)
class ApprovalOutcome:
    """一次调用的审批结论。``allow`` 只代表"这层不要求问人"，不代表放行。"""

    decision: str
    reason: str = ""
    matched: tuple[ApprovalRule, ...] = ()
    prompt: str = ""
    argument_errors: tuple[str, ...] = field(default_factory=tuple)


def evaluate_approval(
    policy: ApprovalPolicy | None,
    args: Any,
    input_schema: Any = None,
) -> ApprovalOutcome:
    """按固定顺序判定审批意图。

    顺序是语义的一部分，不能换：

    1. **入参先于条件**：调用值与绑定 schema 不兼容时直接拒——拿一个类型都不对的值去
       比大小，结果没有意义（这一步只在存在 ``when`` 条件时做：没有条件就没有由它
       驱动的判定，业务入参校验是宿主的事）；
    2. **条件路径必须已声明**：路径不在 schema 里、或压根没绑定 schema = 我们根本不知道
       这个参数是什么，判定结果没有意义，失败关闭；
    3. **无条件要求占优**：``required`` 为真时每次都要问，命中的规则不得削弱它；
    4. **ANY 语义**：多条 ``when`` 只要命中一条就要问；一条都不中才是"不产生审批意图"。
    """
    if policy is None:
        return ApprovalOutcome(decision=DECISION_ALLOW, reason="未声明审批要求")

    rules = tuple(policy.when)
    if rules and input_schema is not None:
        errors = argument_errors(args, input_schema)
        if errors:
            return ApprovalOutcome(
                decision=DECISION_REJECT_INVALID_ARGUMENTS,
                reason="调用输入与绑定 input schema 不兼容：" + "；".join(errors),
                argument_errors=tuple(errors),
            )
    for rule in rules:
        if rule.op not in APPROVAL_OPS:
            return ApprovalOutcome(
                decision=DECISION_REJECT_INVALID_ARGUMENTS,
                reason=f"审批算子不被支持，已失败关闭：{rule.op!r}",
            )
        if input_schema is None:
            # 有条件却没有绑定 schema = 我们不知道这些参数长什么样。放过去的话，
            # 拼错的路径在 ==/!= 下会"永远不命中"，条件静默失效——比报错危险得多。
            return ApprovalOutcome(
                decision=DECISION_REJECT_INVALID_ARGUMENTS,
                reason=f"审批条件没有可校验的绑定 input schema，已失败关闭：{rule.param}",
            )
        if schema_at_path(input_schema, rule.param) is None:
            declared = sorted((input_schema.get("properties") or {}).keys())
            return ApprovalOutcome(
                decision=DECISION_REJECT_INVALID_ARGUMENTS,
                reason=(
                    f"审批条件路径未在绑定 schema 中声明：{rule.param}；"
                    f"已声明的参数：{', '.join(declared) or '（无）'}。"
                    "路径写错会让这次调用被永久拒绝，请核对拼写"
                ),
            )

    if policy.required:
        return ApprovalOutcome(
            decision=DECISION_APPROVAL_REQUIRED,
            reason="该能力要求每次调用都人工确认（条件规则不得削弱它）",
            prompt=policy.prompt,
        )
    if not rules:
        return ApprovalOutcome(decision=DECISION_ALLOW, reason="没有任何审批要求")

    matched: list[ApprovalRule] = []
    for rule in rules:
        try:
            hit = rule_matches(rule, args)
        except ApprovalError as exc:
            return ApprovalOutcome(
                decision=DECISION_REJECT_INVALID_ARGUMENTS,
                reason=f"审批条件无法按 JSON 值语义判定，已失败关闭：{exc}",
            )
        if hit:
            matched.append(rule)
    if matched:
        return ApprovalOutcome(
            decision=DECISION_APPROVAL_REQUIRED,
            reason="命中审批条件："
            + "；".join(rule.label or f"{rule.param} {rule.op}" for rule in matched),
            matched=tuple(matched),
            prompt=policy.prompt,
        )
    return ApprovalOutcome(decision=DECISION_ALLOW, reason="没有命中的审批条件")
