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


def _idna_host(host: str) -> str:
    """主机名的 IDNA 映射；有损映射（如 ß→ss）返回原样而不是合并后的形状。

    Python 内置 idna 编解码是 IDNA 2003 语义，会把 ``faß`` 映射成 ``fass``——
    浏览器（IDNA 2008/UTS46）视二者为**不同域名**。若按映射结果归一，
    用户授权 ``faß.example`` 后 ``fass.example`` 也会被放行（信任被扩大）。
    判据：编码再解码能还原原样 → 无损，用映射结果；还原不回 → 有损，
    保留原主机名作为独立信任身份（与其他形状互不匹配，宁窄勿宽）。
    """
    try:
        ascii_form = host.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        return host
    try:
        if ascii_form.encode("ascii").decode("idna").lower() == host.lower():
            return ascii_form.lower()
    except (UnicodeError, ValueError):
        pass
    return host


def _canonical(url: str) -> str:
    value = url.strip().rstrip(_TRAILING)
    parsed = urlsplit(value if re.match(r"https?://", value, re.I) else "https://" + value)
    # userinfo（reader@example.com 里的 reader@）不影响连接目标：按 hostname
    # 归一即可。此前对带 userinfo 的 URL 整体返回空，导致这类 URL 既不被
    # 记为不可信来源也无法匹配任务原文，外发检查直接漏过。
    host = _idna_host(unquote(parsed.hostname or "", errors="strict"))
    if not host:
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


def _trim_glued_cjk(original: str) -> str:
    """主机名区域里 ASCII 字母后紧粘的 CJK 视为主机名终止，截断匹配。

    ``https://evil.example即可`` 这类"中文正文紧贴 URL"的写法，若让 CJK 进主机名，
    IDNA 会把它编成另一个 xn-- 主机，与不可信来源记录对不上 → 漏拦。与裸域名
    分支的语义一致（紧邻中文按正文后缀处理），这里把同样的规则用于 scheme 分支；
    路径区域（/ 之后）的 CJK 是合法路径，不截断；纯 IDN 主机（例子.中国）不触发。
    """
    scheme_at = original.find("://")
    start = scheme_at + 3 if scheme_at != -1 else 0
    for i in range(start, len(original)):
        ch = original[i]
        if ch in "/?#":
            break
        prev = original[i - 1] if i > start else ""
        if "\u3400" <= ch <= "\u9fff" and prev.isascii() and prev.isalnum():
            return original[:i]
    return original


def find_urls(text: str) -> list[tuple[str, str]]:
    """返回 ``(原文, 规范化)``；保留国际化主机与路径，不按非 ASCII 截断。"""
    found: list[tuple[str, str]] = []
    for match in _URL_RE.finditer(text or ""):
        original = _trim_glued_cjk(match.group(0)).rstrip(_TRAILING)
        if not original:
            continue
        try:
            canonical = _canonical(original)
        except (ValueError, UnicodeError):
            continue
        if canonical:
            found.append((original, canonical))
    return found


def _same_source(candidate: str, source: str) -> bool:
    target, known = urlsplit(candidate), urlsplit(source)
    # 主机名统一经 _idna_host：ASCII 形取映射结果；有损映射（如 faß）保留原样
    # 的非 ASCII 主机名也安全比较——不能直接 .encode("ascii")，否则外发检查
    # 遇到这类 URL 会抛 UnicodeEncodeError（2026-10-31 B1 审计发现）。
    target_host = _idna_host(target.hostname or "").removeprefix("www.")
    known_host = _idna_host(known.hostname or "").removeprefix("www.")
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
    # 路径大小写不敏感：服务器路径虽区分大小写，但攻击者控制工具输出与外发
    # 两侧，/X 与 /x 的差异是"简单格式变换"，不能成为绕过同源匹配的手段。
    if target.path.lower() == known.path.lower():
        return True
    # 只在已知源路径后识别紧邻中文正文；绝不裁掉 ASCII 路径。
    known_lower = known.path.lower()
    target_lower = target.path.lower()
    suffix = target_lower.removeprefix(known_lower)
    return (
        target_lower.startswith(known_lower)
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


def _iter_value_strings(value: Any) -> Iterator[str]:
    """递归抽出结构化结果里的所有字符串（dict 值 / list 项 / 标量 str）。"""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_value_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_value_strings(item)


def record_tool_output(result: Any) -> None:
    """把工具输出里的 URL 记入不可信集合（在返回给模型之前调用）。

    接受**结构化结果**（dict/list/str）：必须在 JSON 序列化之前记录——序列化会把
    字符串里的换行/引号转义成 ``\n``、``\\"`` 等形状，find_urls 在转义文本上
    记下的规范形与外发文本（真实字符）对不上，同一 URL 就会漏拦。
    """
    context = _CURRENT.get()
    if context is None:
        return
    for text in _iter_value_strings(result):
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
