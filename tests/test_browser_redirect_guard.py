"""重定向安全：未授权跳转必须在**加载前**被拦截，不能等最终 URL 再判断。

使用真实本地 HTTP 服务器 + 真实 Playwright 后端；未安装 playwright 时跳过。
"""

from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


def _serve(handler_cls: type[BaseHTTPRequestHandler]) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def test_redirect_to_disallowed_origin_is_blocked_before_content_loads() -> None:
    pytest.importorskip("playwright.async_api")

    class SecretHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server 契约
            body = b"TOP_SECRET_SHOULD_NOT_LOAD"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args) -> None:
            return

    secret = _serve(SecretHandler)
    evil_port = secret.server_address[1]

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{evil_port}/secret")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_args) -> None:
            return

    redirect = _serve(RedirectHandler)
    allowed_port = redirect.server_address[1]

    try:
        from yai_core.integrations.browser import BrowserPolicy, BrowserProvider
        from yai_core.integrations.browser.playwright_backend import (
            PlaywrightBrowserBackend,
        )

        async def main() -> None:
            backend = PlaywrightBrowserBackend(headless=True)
            await backend.start()
            try:
                provider = BrowserProvider(
                    backend,
                    policy=BrowserPolicy.secure(
                        [f"http://127.0.0.1:{allowed_port}"]
                    ),
                )
                result = await provider.open(
                    f"http://127.0.0.1:{allowed_port}/redirect"
                )
                assert result["ok"] is False, result
                assert "TOP_SECRET_SHOULD_NOT_LOAD" not in str(result)
                # 直接打开未授权目标也要在导航前被拒绝。
                denied = await provider.open(
                    f"http://127.0.0.1:{evil_port}/secret"
                )
                assert denied["ok"] is False
                assert "TOP_SECRET_SHOULD_NOT_LOAD" not in str(denied)
            finally:
                await backend.close()

        asyncio.run(main())
    finally:
        redirect.shutdown()
        redirect.server_close()
        secret.shutdown()
        secret.server_close()
