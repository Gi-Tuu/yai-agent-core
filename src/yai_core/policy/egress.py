"""外发文本的 URL 来源闸。

规则是确定性的，不依赖模型是否“加了一句警示”：

- 工具声明 ``ToolGovernance.egress_text``（要外发的文本参数路径）；
- 当前任务原文里的 URL 是可信来源；
- 其余工具输出的 URL 是不可信来源；
- 要外发的文本里出现了一个**只在不可信工具输出出现、任务原文没有**的 URL，
  本次外发直接拒绝（``PERMISSION_DENIED``，source=``egress``）。

不声明 ``egress_text`` 的工具完全不走这条规则——内核不按字段名猜。
URL 只做轻量规范化（大小写、scheme、``www.`` 前缀、尾随标点），不解析
整个 URL 语法，避免把普通文本误当链接。
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from yai_core.types import ToolSpec

_URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>'\"\[\]()（）]+", re.IGNORECASE)
_TRAILING = ".,;:!?)]}，。；：！？、）】」』"


def _canonical(url: str) -> str:
    value = url.strip().rstrip(_TRAILING).lower()
    value = re.sub(r"^https?://", "", value)
    value = re.sub(r"^www\.", "", value)
    return value.rstrip("/")


def find_urls(text: str) -> list[tuple[str, str]]:
    """返回 ``(原文, 规范化)``；只认 http(s) 或 www. 开头。"""
    found: list[tuple[str, str]] = []
    for match in _URL_RE.finditer(text or ""):
        original = match.group(0).rstrip(_TRAILING)
        if original:
            found.append((original, _canonical(original)))
    return found


@dataclass
class EgressContext:
    """一次任务的外发来源上下文；任务串行，ContextVar 随 astream 作用域。"""

    task_urls: set[str]
    untrusted_urls: set[str] = field(default_factory=set)


_CURRENT: ContextVar[EgressContext | None] = ContextVar("yai_egress", default=None)


@contextmanager
def egress_scope(task: str) -> Iterator[EgressContext]:
    """任务级作用域：从任务原文建立可信 URL 集合。"""
    context = EgressContext(
        task_urls={canonical for _, canonical in find_urls(task)}
    )
    token = _CURRENT.set(context)
    try:
        yield context
    finally:
        _CURRENT.reset(token)


def record_tool_output(text: str) -> None:
    """把工具输出里的 URL 记入不可信集合（在返回给模型之前调用）。"""
    context = _CURRENT.get()
    if context is None or not text:
        return
    context.untrusted_urls.update(
        canonical for _, canonical in find_urls(text)
    )


def _strings_at_path(value: Any, path: str) -> Iterator[str]:
    parts = [part for part in path.split(".") if part]

    def walk(node: Any, index: int) -> Iterator[str]:
        if index == len(parts):
            if isinstance(node, str):
                yield node
            elif isinstance(node, dict):
                for item in node.values():
                    yield from walk(item, index)
            elif isinstance(node, (list, tuple)):
                for item in node:
                    yield from walk(item, index)
            return
        key = parts[index]
        if isinstance(node, dict):
            if key in node:
                yield from walk(node[key], index + 1)
        elif isinstance(node, (list, tuple)):
            for item in node:
                yield from walk(item, index)

    yield from walk(value, 0)


def check_egress(spec: ToolSpec, arguments: dict[str, Any]) -> str | None:
    """返回拒绝原因；``None`` 表示这次外发允许通过。"""
    context = _CURRENT.get()
    if context is None or not spec.governance.egress_text:
        return None

    offending: list[str] = []
    seen: set[tuple[str, str]] = set()
    for path in spec.governance.egress_text:
        for text in _strings_at_path(arguments, path):
            for original, canonical in find_urls(text):
                if canonical in context.task_urls:
                    continue
                if canonical not in context.untrusted_urls:
                    continue
                marker = (canonical, original)
                if marker not in seen:
                    seen.add(marker)
                    offending.append(original)
    if not offending:
        return None
    return (
        "外发被拒绝：即将发送的 URL 只出现在不可信工具输出中，用户任务原文里没有："
        + "、".join(offending)
        + "。请让用户确认，或从可信来源重新获取后再发送。"
    )
