"""测试专用本地 HTTP 客户端：环回 fail-closed + 标准库 http.client 直连。

为什么存在：这批测试要起真实 HTTP 服务再打真实请求（离线端到端），而通用
HTTP 客户端（urllib.urlopen 等）对"动态 URL"天然命中 SSRF 静态规则——测试
恰恰**必须**打环回，两条诉求永远冲突。本模块把矛盾收进一处：

- **fail-closed 环回闸**：scheme 必须 http、host 必须环回；base 被改成真实
  地址时测试自身立刻拒绝，不发出任何请求；
- **连接层确定目标**：http.client 显式 host/port 直连，请求目标不经
  URL 字符串拼接进入通用 fetch 通道。

仅供 tests/ 使用；生产代码的本地客户端请各自内置同款环回闸（见
shell/desktop/crm_client.py 的构造期校验）。
"""

from __future__ import annotations

import http.client
import json
import urllib.parse

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def require_loopback(url: str) -> tuple[str, int]:
    """校验 URL 指向环回 http，返回解析出的 (host, port)；否则 AssertionError。"""
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    assert parts.scheme == "http", f"本地测试只允许 http，收到：{parts.scheme!r}"
    assert host in _LOOPBACK_HOSTS, f"本地测试只允许环回地址，收到：{host!r}"
    return host, parts.port or 80


def local_request(
    base: str,
    path: str,
    *,
    method: str = "GET",
    payload: object | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> tuple[int, bytes]:
    """对本地测试服务发一次请求，返回 (status, 原始响应字节)。

    非 2xx 也正常返回状态码（由调用方断言），连接失败才抛异常。
    """
    host, port = require_loopback(base + path)
    body: bytes | None = None
    merged = dict(headers or {})
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        merged.setdefault("Content-Type", "application/json")
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers=merged)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def local_json(
    base: str,
    path: str,
    *,
    method: str = "GET",
    payload: object | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
    encoding: str = "utf-8",
) -> tuple[int, object]:
    """local_request 的 JSON 便捷层：返回 (status, 解析后的 JSON)。"""
    status, raw = local_request(
        base, path, method=method, payload=payload, headers=headers, timeout=timeout
    )
    return status, json.loads(raw.decode(encoding))
