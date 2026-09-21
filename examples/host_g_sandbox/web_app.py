"""host_g 极简网页壳：用 **Python 标准库** 起本地服务，零第三方依赖。

运行：
    .venv/Scripts/python.exe examples/host_g_sandbox/web_app.py
然后浏览器访问 http://127.0.0.1:8201 （启动时会自动打开）。

设计与 host_e 的 FastAPI 工作台刻意区分——这是一个**最小**演示：
- 左侧是"原生应用"：候选人列表（只有技能/经验数据，没有评分能力）；
- 右侧是"嵌入 YAI Agent Core 后"：点一下按钮，Core 发现宿主没有加权评分
  能力，现场 create_code_tool 造出 weighted_score，在隔离子进程沙箱里执行，
  事件流逐条播放，最后给出排序与代码工具注册表状态。

只监听 127.0.0.1，不对外暴露；演示用离线脚本模型（无需 API Key、结果确定）。
"""

from __future__ import annotations

import asyncio
import json
import sys
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

from host_g_sandbox import capabilities as cap  # noqa: E402
from host_g_sandbox.demo_agent import DEFAULT_TASK, build_core  # noqa: E402

WEB_DIR = _HERE / "web"
HOST = "127.0.0.1"
PORT = 8201


def _run_core(task: str) -> dict:
    """每次请求新建一个 Core，跑任务并把事件序列化成可 JSON 化的结构。"""
    core = build_core()
    result = asyncio.run(core.run(task))
    events = [{"type": e.type.value, "data": e.data} for e in result.events]
    return {
        "task": task,
        "strategy": result.strategy.value if result.strategy else None,
        "events": events,
        "final_text": result.final_text,
        "code_tools": core.code_tools_status(),
    }


class _Handler(BaseHTTPRequestHandler):
    server_version = "YAIHostG/0.1"

    # ---- 工具 ----
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

    def log_message(self, fmt: str, *args) -> None:  # 静默默认访问日志
        pass

    # ---- 路由 ----
    def do_GET(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        if route in ("/", "/index.html"):
            self._send_file(WEB_DIR / "index.html", "text/html; charset=utf-8")
        elif route == "/api/health":
            self._send_json({"ok": True, "mode": "offline-scripted", "port": PORT})
        elif route == "/api/candidates":
            self._send_json({"candidates": cap.list_candidates()})
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
        task = str(payload.get("task") or DEFAULT_TASK).strip() or DEFAULT_TASK
        try:
            self._send_json(_run_core(task))
        except Exception as exc:  # noqa: BLE001 - 演示页面要看到错误而不是崩掉
            self._send_json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)


def main() -> None:
    if not (WEB_DIR / "index.html").is_file():
        print(f"[错误] 找不到前端页面：{WEB_DIR / 'index.html'}")
        sys.exit(1)
    server = ThreadingHTTPServer((HOST, PORT), _Handler)
    url = f"http://{HOST}:{PORT}"
    print("=" * 60)
    print("host_g 沙箱演示网页（标准库零依赖，仅监听本机）")
    print(f"  地址：{url}")
    print("  关闭：Ctrl+C 或直接关闭本窗口")
    print("=" * 60)
    try:
        import threading

        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
