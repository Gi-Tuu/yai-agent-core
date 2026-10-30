"""外发文本的 URL 来源闸。

规则是确定性的，不依赖模型是否“加了一句警示”：

- 工具声明 ``ToolGovernance.egress_text``（要外发的文本参数路径）；
- 当前任务原文里的 URL 是可信来源；
- 其余工具输出的 URL 是不可信来源；
- 要外发的文本里出现了一个**只在不可信工具输出出现、任务原文没有**的 URL，
  本次外发直接拒绝（``PERMISSION_DENIED``，source=``egress``）。

不声明 ``egress_text`` 的工具完全不走这条规则——内核不按字段名猜。
URL 按解析后的主机、端口和百分号编码规范化，保留路径大小写。
可信来源仅匹配完整 URL；不可信来源匹配同主机路径，避免添加 query 绕过。
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

from yai_core.paths import value_candidates
from yai_core.types import ToolSpec, validate_egress_paths

# 裸域名仅识别常见后缀；ASCII 边界允许中文正文紧邻，不匹配小数或文件名。
_URL_RE = re.compile(
    r"(?:https?://|www\.)[^\s<>'\"\[\]()（），。；！？、]+"
    r"|(?<![a-z0-9_@./-])(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"(?:com|org|net|edu|gov|io|co|cn|example)(?![a-z0-9_-]|\.[a-z0-9])"
    r"(?::[0-9]+)?(?:[/?#][^\s<>'\"\[\]()（），。；！？、]*)?",
    re.IGNORECASE,
)
_TRAILING = ".,;:!?)]}，。；：！？、）】」』"
_UNRESERVED = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~"


def _percent(value: str) -> str:
    def normalize(match: re.Match[str]) -> str:
        encoded = match.group(0)
        decoded = unquote(encoded, errors="strict")
        return "".join(
            char if char in _UNRESERVED or ord(char) > 127
            else "".join(f"%{byte:02X}" for byte in char.encode("utf-8"))
            for char in decoded
        )

    return re.sub(r"(?:%[0-9a-fA-F]{2})+", normalize, value)


def _canonical(url: str) -> str:
    value = url.strip().rstrip(_TRAILING)
    parsed = urlsplit(value if re.match(r"https?://", value, re.I) else "https://" + value)
    host = unquote(parsed.hostname or "", errors="strict").encode("idna").decode().lower()
    if not host or parsed.username is not None or parsed.password is not None:
        return ""
    # ASCII 形式的 xn-- 标签也必须能解码，后续来源比较才可安全还原中文。
    host.encode("ascii").decode("idna")
    port = parsed.port
    authority = f"[{host}]" if ":" in host else host
    if port is not None and (parsed.scheme, port) not in {("http", 80), ("https", 443)}:
        authority += f":{port}"
    return urlunsplit((
        parsed.scheme.lower(), authority, _percent(parsed.path or "/"),
        _percent(parsed.query), _percent(parsed.fragment),
    ))


def find_urls(text: str) -> list[tuple[str, str]]:
    """返回 ``(原文, 规范化)``；保留国际化主机与路径，不按非 ASCII 截断。"""
    found: list[tuple[str, str]] = []
    for match in _URL_RE.finditer(text or ""):
        original = match.group(0).rstrip(_TRAILING)
        try:
            canonical = _canonical(original)
        except (ValueError, UnicodeError):
            continue
        if canonical:
            found.append((original, canonical))
    return found


def _same_source(candidate: str, source: str) -> bool:
    target, known = urlsplit(candidate), urlsplit(source)
    target_host = (target.hostname or "").encode("ascii").decode("idna").removeprefix("www.")
    known_host = (known.hostname or "").encode("ascii").decode("idna").removeprefix("www.")
    if target.port != known.port:
        return False
    if target_host != known_host:
        suffix = target_host.removeprefix(known_host)
        if not (
            target_host.startswith(known_host)
            and suffix
            and all("\u3400" <= char <= "\u9fff" for char in suffix)
        ):
            return False
    if target.path == known.path:
        return True
    # 只在已知源路径后识别紧邻中文正文；绝不裁掉 ASCII 路径。
    suffix = target.path.removeprefix(known.path)
    return (
        target.path.startswith(known.path)
        and bool(suffix)
        and all("\u3400" <= char <= "\u9fff" for char in suffix)
    )


@dataclass
class EgressContext:
    """一次任务的外发来源上下文；任务串行，ContextVar 随 astream 作用域。"""

    task_urls: set[str]
    untrusted_urls: set[str] = field(default_factory=set)


_CURRENT: ContextVar[EgressContext | None] = ContextVar("yai_egress", default=None)


@contextmanager
def egress_scope(task: str | EgressContext) -> Iterator[EgressContext]:
    """建立或重新进入任务上下文；token 必须在本次推进内复位。"""
    context = task if isinstance(task, EgressContext) else EgressContext(
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
    def walk(node: Any) -> Iterator[str]:
        if isinstance(node, str):
            yield node
        elif isinstance(node, dict):
            for item in node.values():
                yield from walk(item)
        elif isinstance(node, (list, tuple)):
            for item in node:
                yield from walk(item)

    for candidate in value_candidates(value, path):
        yield from walk(candidate)


def check_egress(spec: ToolSpec, arguments: dict[str, Any]) -> str | None:
    """返回拒绝原因；``None`` 表示这次外发允许通过。"""
    if not spec.governance.egress_text:
        return None
    try:
        validate_egress_paths(spec.name, spec.governance.egress_text, spec.input_schema)
    except ValueError as exc:
        return f"外发被拒绝：治理声明无效：{exc}"
    context = _CURRENT.get()
    if context is None:
        return "外发被拒绝：缺少任务来源上下文，请通过 AgentCore 或宿主 egress_scope 执行"

    offending: list[str] = []
    seen: set[tuple[str, str]] = set()
    for path in spec.governance.egress_text:
        for text in _strings_at_path(arguments, path):
            for original, canonical in find_urls(text):
                if canonical in context.task_urls:
                    continue
                if not any(_same_source(canonical, source) for source in context.untrusted_urls):
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
