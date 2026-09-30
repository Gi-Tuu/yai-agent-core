"""员工薄壳本地代理：纯标准库 HTTP（静态浮窗 + SSE + REST 决策）。

- 静态托管 :mod:`shell.overlay` 的悬浮球/抽屉页面；
- ``GET /api/stream?run_id=`` 以 SSE 推送内核事件（动效数据源）；
- REST：启动任务、授权、澄清回答、终止、Core 开关、权限挡位。

不引入任何第三方框架，与内核"零硬依赖"风格一致；仅绑定 127.0.0.1。
"""

from __future__ import annotations

import json
import queue
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from shell.session import PERMISSION_MODES, ShellSession

#: 浮窗静态资源目录。
OVERLAY_DIR = Path(__file__).resolve().parent / "overlay"

#: 静态文件 MIME 类型表。
_MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
}

#: SSE 终结事件。
_TERMINAL_EVENTS = ("done", "error", "cancelled")


def create_server(
    session: ShellSession, host: str = "127.0.0.1", port: int = 8300
) -> ThreadingHTTPServer:
    """构建绑定到指定会话的本地 HTTP 服务器。"""

    class ShellHandler(BaseHTTPRequestHandler):
        server_version = "YaiShell/0.1"

        def log_message(self, fmt: str, *args) -> None:
            sys.stdout.write("[shell] " + fmt % args + "\n")

        # ---------- 基础工具 ----------

        def _send_json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode(
                "utf-8"
            )
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict | None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                parsed = json.loads(raw or b"{}")
                return parsed if isinstance(parsed, dict) else None
            except json.JSONDecodeError:
                return None

        def _send_static(self, file_path: Path) -> None:
            if not file_path.is_file():
                self._send_json(404, {"error": "not_found"})
                return
            body = file_path.read_bytes()
            self.send_response(200)
            self.send_header(
                "Content-Type", _MIME.get(file_path.suffix, "application/octet-stream")
            )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        # ---------- GET ----------

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
            if path in ("/", "/index.html"):
                self._send_static(OVERLAY_DIR / "index.html")
                return
            if path == "/api/state":
                self._send_json(200, session.state())
                return
            if path == "/api/stream":
                query = parse_qs(parsed.query)
                run_id = (query.get("run_id") or [""])[0]
                run = session.get_run(run_id)
                if run is None:
                    self._send_json(404, {"error": "run_not_found"})
                    return
                self._serve_stream(run)
                return
            # 其余按静态资源处理（/overlay.css、/overlay.js 等），禁止路径逃逸。
            relative = path.lstrip("/")
            if relative and ".." not in relative:
                self._send_static(OVERLAY_DIR / relative)
                return
            self._send_json(404, {"error": "not_found", "detail": path})

        # ---------- POST ----------

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            payload = self._read_json()
            if payload is None:
                self._send_json(400, {"error": "bad_json"})
                return

            if path == "/api/run":
                run_id, err = session.start_run(str(payload.get("task", "")))
                if err == "core_disabled":
                    self._send_json(
                        503,
                        {"error": "core_disabled", "detail": "Core 已关闭，请先启用"},
                    )
                elif err == "busy":
                    self._send_json(409, {"error": "busy", "detail": "已有任务在执行"})
                elif err == "empty":
                    self._send_json(400, {"error": "empty", "detail": "task 不能为空"})
                else:
                    self._send_json(200, {"run_id": run_id})
                return

            if path == "/api/decision":
                ok = session.resolve(
                    str(payload.get("run_id", "")),
                    "confirm",
                    bool(payload.get("approved")),
                )
                self._send_json(200 if ok else 404, {"ok": ok})
                return

            if path == "/api/answer":
                ok = session.resolve(
                    str(payload.get("run_id", "")),
                    "ask",
                    str(payload.get("answer", "")),
                )
                self._send_json(200 if ok else 404, {"ok": ok})
                return

            if path == "/api/cancel":
                ok = session.cancel(str(payload.get("run_id", "")))
                self._send_json(200 if ok else 404, {"ok": ok})
                return

            if path == "/api/core":
                on = session.set_enabled(bool(payload.get("enabled")))
                self._send_json(200, {"ok": True, "core_enabled": on})
                return

            if path == "/api/permission":
                mode = str(payload.get("mode", ""))
                if mode not in PERMISSION_MODES:
                    self._send_json(
                        400,
                        {"error": "bad_mode", "detail": f"可选 {PERMISSION_MODES}"},
                    )
                    return
                session.set_permission_mode(mode)
                self._send_json(200, {"ok": True, "permission_mode": mode})
                return

            self._send_json(404, {"error": "not_found", "detail": path})

        # ---------- SSE ----------

        def _serve_stream(self, run: dict) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            event_queue: queue.Queue = run["queue"]
            while True:
                try:
                    event = event_queue.get(timeout=15)
                except queue.Empty:
                    try:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        return
                    continue
                body = json.dumps(event, ensure_ascii=False, default=str)
                try:
                    self.wfile.write(f"data: {body}\n\n".encode())
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
                if event["type"] in _TERMINAL_EVENTS:
                    return

    server = ThreadingHTTPServer((host, port), ShellHandler)
    server.daemon_threads = True
    return server
