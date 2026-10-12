"""host_j（Actual Budget 记账宿主）极简网页壳：Python 标准库起本地服务。

运行：
    .venv/Scripts/python.exe examples/host_j_actual/web_app.py
然后浏览器访问 http://127.0.0.1:8240 （启动时会自动打开）。

左栏是"原生记账应用"：账户、本月交易（标出未分类）、预算对比，全是宿主自己
的 CRUD，没有任何智能体。右栏是"嵌入 YAI Agent Core 后"：同一个宿主，点一下
即可走两条第三方播种标准线——

- 发现链路：Core 识别"自动分类建议"能力缺口 → 现场发现 suggest_category →
  经授权补分类 → 核对预算超支；
- 委派链路：根员工一次派两个白名单收窄的只读子员工，并行汇总账户与对账概览。

只监听 127.0.0.1，不对外暴露；演示用离线脚本模型（无需 API Key、结果确定）。
原生面板读模块内一份固定假账；每次"嵌入 Core"演示都用一份全新假账，保证可
重复点击而不会把账改"干净"。
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

# 允许直接运行：补 examples/ 与 src/ 到 sys.path。
_HERE = Path(__file__).resolve().parent
_EXAMPLES = _HERE.parent
_SRC = _EXAMPLES.parent / "src"
for _p in (str(_EXAMPLES), str(_SRC)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from host_j_actual import capabilities as cap  # noqa: E402
from host_j_actual.backend import FakeActualBackend  # noqa: E402
from host_j_actual.demo_agent import (  # noqa: E402
    DEFAULT_DELEGATE_TASK,
    DEFAULT_TASK,
    build_delegate_core,
    build_discovery_core,
)

WEB_DIR = _HERE / "web"
HOST = "127.0.0.1"
PORT = 8240

#: 原生面板展示用的固定假账（始终保留 3 笔未分类，便于对照"嵌入前"的状态）。
_PANEL_BACKEND = FakeActualBackend()
_PANEL_TOOLS = cap.make_tools(_PANEL_BACKEND)


def _run_core(mode: str, task: str | None) -> dict:
    """每次演示用一份全新假账新建 Core，跑完把事件序列化成可 JSON 化结构。"""
    backend = FakeActualBackend()
    if mode == "delegate":
        core = build_delegate_core(backend)
        task = task or DEFAULT_DELEGATE_TASK
    else:
        core = build_discovery_core(backend, auto_confirm=True)
        task = task or DEFAULT_TASK
    result = asyncio.run(core.run(task))
    events = [
        {"type": getattr(e.type, "value", str(e.type)), "data": e.data}
        for e in result.events
    ]
    return {
        "mode": mode,
        "task": task,
        "strategy": result.strategy.value if result.strategy else None,
        "events": events,
        "final_text": result.final_text,
        # 把这份一次性假账的最终状态带回前端，展示"补分类之后"的账。
        "ledger_after": cap.make_tools(backend).list_recent_transactions(),
        "overview_after": cap.make_tools(backend).reconcile_overview(),
    }


class _Handler(BaseHTTPRequestHandler):
    server_version = "YAIHostJ/0.1"

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str) -> None:
        if not path.is_file():
            self.send_error(404, "Not Found")
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        if route in ("/", "/index.html"):
            self._send_file(WEB_DIR / "index.html", "text/html; charset=utf-8")
        elif route == "/api/health":
            self._send_json({"ok": True, "mode": "offline-scripted", "port": PORT})
        elif route == "/api/accounts":
            self._send_json(_PANEL_TOOLS.list_accounts())
        elif route == "/api/categories":
            self._send_json(_PANEL_TOOLS.list_categories())
        elif route == "/api/transactions":
            self._send_json(_PANEL_TOOLS.list_recent_transactions())
        elif route == "/api/budget":
            self._send_json(_PANEL_TOOLS.get_month_budget())
        elif route == "/api/overview":
            self._send_json(_PANEL_TOOLS.reconcile_overview())
        else:
            self.send_error(404, "Not Found")

    def do_POST(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        if route != "/api/run":
            self.send_error(404, "Not Found")
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {}
        mode = "delegate" if payload.get("mode") == "delegate" else "discover"
        task = str(payload.get("task") or "").strip() or None
        try:
            self._send_json(_run_core(mode, task))
        except Exception as exc:  # noqa: BLE001 - 演示页面要看到错误而不是崩掉
            self._send_json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)


def main() -> None:
    if not (WEB_DIR / "index.html").is_file():
        print(f"[错误] 找不到前端页面：{WEB_DIR / 'index.html'}")
        sys.exit(1)
    server = ThreadingHTTPServer((HOST, PORT), _Handler)
    url = f"http://{HOST}:{PORT}"
    print("=" * 60)
    print("host_j Actual Budget 记账宿主演示（标准库零依赖，仅监听本机）")
    print(f"  地址：{url}")
    print("  关闭：Ctrl+C 或直接关闭本窗口")
    print("=" * 60)
    try:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
