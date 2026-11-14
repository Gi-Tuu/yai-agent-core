"""工具执行器的纯结果/超时辅助。

从 ``tools/executor.py`` 拆出；``executor`` 继续 re-export，旧导入路径保持可用。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any


def _fingerprint_arguments(arguments: dict) -> str:
    """参数指纹：同一个幂等键只认同一组参数，避免撞键时返回别人的结果。"""
    raw = json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _composite_timeout_message(name: str, declared_ms: int) -> str:
    """组合超时的统一话术：只停止等待，不承诺终止同步步骤的副作用。"""
    return (
        f"组合工具 {name} 超过声明的 {declared_ms}ms 未完成，已停止等待（不是强制终止）；"
        "已完成步骤的副作用不会回滚，异步步骤会被取消，"
        "同步步骤无法中断、可能仍在后台跑完"
    )


def bound_tool_result(
    text: str, limit: int | None
) -> tuple[str, bool, int]:
    """把单个工具结果限制在 ``limit`` 字符内；返回 ``(text, truncated, original_chars)``。

    - ``limit=None`` 或未超限时原样返回，``truncated=False``；
    - 超限时保留"头 + 尾"，中间插入显式省略标记，让模型明确知道结果被截断
      （与 payload_guard 的有界裁剪理念一致，绝不静默删改）；
    - 保证返回文本长度严格不超过 ``limit``。
    """
    original_chars = len(text)
    if limit is None or original_chars <= limit:
        return text, False, original_chars

    head_budget = int(limit * 0.7)
    tail_budget = limit - head_budget
    head = text[:head_budget]
    tail = text[original_chars - tail_budget :]

    def _join(head_part: str) -> str:
        omitted = original_chars - len(head_part) - tail_budget
        marker = f"\n…[工具结果过长，已省略 {omitted} 字符，以下为结尾部分]…\n"
        return head_part + marker + tail

    bounded = _join(head)
    # marker 长度随 omitted 位数变化，必要时从头部再收紧，确保严格不越上界。
    while len(bounded) > limit and head_budget > 0:
        head_budget -= 1
        head = text[:head_budget]
        bounded = _join(head)
    return bounded, True, original_chars


class ToolTimeoutError(RuntimeError):
    """工具 handler 超过 ``handler_timeout``。"""


def _consume_abandoned_result(future: asyncio.Future[Any]) -> None:
    """被超时放弃的线程任务：把结果/异常取出来丢掉。

    不取的话，后台线程之后抛错会留下 "exception was never retrieved" 的噪音，
    运维看到会以为还有一个没处理的故障。
    """
    if not future.cancelled():
        future.exception()


def _check_timeout(value: Any, label: str) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"{label} 必须是正数或 None")
