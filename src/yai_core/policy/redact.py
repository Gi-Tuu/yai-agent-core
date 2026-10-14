"""参数脱敏：按宿主声明的路径替换敏感值，**只作用于对外副本**。

设计口径：

- 脱敏对象是"离开可信边界之前"的副本（审批事件、``channel.confirm`` 入参、
  ``TOOL_CALL`` 审计事件、追踪 span），实际执行永远拿权威参数；
- 只脱敏**宿主显式声明过**的路径。内核不按字段名关键词猜密钥——猜出来的规则
  既会漏（换个名字就绕过）也会误伤（把业务字段抹掉让审批失去意义），
  所以"未声明即不脱敏"是**有意的边界**，不是缺陷；
- 路径用 dot-notation（``headers.authorization``、``rows.0.token``）；
  解析不到（键不存在、类型不符、列表越界）时**跳过该路径**，不报错、不因此拒绝执行；
- 宽口径提示（``broad=True``，来自外部能力契约的 ``audit.sensitive``）只把顶层值整体
  收口，用于**审计/日志副本**；给审批人的那份仍只按声明路径脱敏，避免"全糊掉导致
  人工批准无从判断"。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

#: 脱敏占位前缀；保留长度便于审批人判断"这是不是一把我认得的长令牌"。
REDACTED_PREFIX = "[redacted"


def _placeholder(value: Any) -> str:
    if isinstance(value, (str, bytes)):
        return f"{REDACTED_PREFIX}:len={len(value)}]"
    if value is None:
        return f"{REDACTED_PREFIX}]"
    return f"{REDACTED_PREFIX}:{type(value).__name__}]"


def _redact_at(root: Any, parts: list[str]) -> None:
    """把 root 上 parts 指向的值换成占位符；途中走不通就静默返回。"""
    current = root
    for index, part in enumerate(parts):
        last = index == len(parts) - 1
        if isinstance(current, dict):
            if part not in current:
                return
            if last:
                current[part] = _placeholder(current[part])
                return
            current = current[part]
        elif isinstance(current, list):
            if not part.isdigit():
                return
            position = int(part)
            if position >= len(current):
                return
            if last:
                current[position] = _placeholder(current[position])
                return
            current = current[position]
        else:
            return


def redact_arguments(arguments: dict, paths: Any, *, broad: bool = False) -> dict:
    """返回 ``arguments`` 的脱敏深拷贝；``paths`` 为声明的敏感参数路径序列。

    无声明时仍返回一份**拷贝**——调用方拿到的永远不该是可写回执行链的对象。

    ``broad=True`` 是宽口径审计提示（外部契约的 ``audit.sensitive``）：把所有**顶层**
    参数值整体收口。它只该用在进审计/日志的那一份上——给审批人的副本如果全糊掉，
    人工批准就没有判断依据了。
    """
    try:
        copy = deepcopy(arguments)
    except Exception:  # noqa: BLE001 - 含不可拷贝值时退回原对象，宁可不脱敏也不崩链路
        copy = arguments
    if not isinstance(copy, dict):
        return copy
    for path in paths or ():
        parts = [piece for piece in str(path).split(".") if piece]
        if parts:
            _redact_at(copy, parts)
    if broad:
        for key in list(copy):
            copy[key] = _placeholder(copy[key])
    return copy
