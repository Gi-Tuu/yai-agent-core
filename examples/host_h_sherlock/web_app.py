"""host_h 极简网页壳：用 Python 标准库起本地服务，零第三方依赖。

运行：
    .venv/Scripts/python.exe examples/host_h_sherlock/web_app.py
然后浏览器访问 http://127.0.0.1:8202 （启动时自动打开）。

设计：
- 左侧"原生应用"：sherlock 原本是命令行工具，你得记命令、读文档、处理 JSON；
- 右侧"嵌入 YAI Agent Core 后"：用自然语言说"查 torvalds 的社交账号"，
  Core 自动调用 lookup_username，事件流逐条播放，命中站点渲染成链接卡片。

有 OPENAI_API_KEY 时自动走真实模型；否则用离线脚本模型（确定、不联网）。
只监听 127.0.0.1。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

_HERE = Path(__file__).resolve().parent
_EXAMPLES = _HERE.parent
_SRC = _EXAMPLES.parent / "src"
for _p in (str(_EXAMPLES), str(_SRC)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from host_h_sherlock import capabilities as cap  # noqa: E402
from host_h_sherlock.demo_agent import DEFAULT_TASK, build_core  # noqa: E402
from runner_common import build_model, load_dotenv  # noqa: E402
from yai_core import AgentCore, build_spec  # noqa: E402

WEB_DIR = _HERE / "web"
HOST = "127.0.0.1"
PORT = 8202

load_dotenv()


def _make_core() -> tuple[AgentCore, str]:
    """有 Key 用真实模型，否则离线脚本模型。"""
    if os.getenv("OPENAI_API_KEY"):
        model, label = build_model()
        core = AgentCore(model, auto_approve_tools=True)
        core.register_tools([
            build_spec(cap.list_available_sites),
            build_spec(cap.lookup_username),
        ])
        return core, label
    return build_core(), "离线脚本模型"


def _extract_found(events: list[dict]) -> list[dict]:
    """从工具结果事件里提取命中站点列表（用于前端渲染链接卡片）。"""
    sites: list[dict] = []
    for ev in events:
        if ev["type"] != "tool_result":
            continue
        data = ev["data"]
        preview = data.get("preview") or ""
        # preview 可能是 dict 或 str
        if isinstance(preview, dict):
            found = preview.get("found") or []
        else:
            try:
                found = (json.loads(preview).get("found")) if preview.startswith("{") else []
            except (json.JSONDecodeError, AttributeError):
                found = []
        for item in found:
            if isinstance(item, dict) and item.get("url"):
                sites.append({"site": item.get("site", "?"), "url": item["url"]})
    return sites


def _run_core(task: str) -> dict:
    core, label = _make_core()
    result = asyncio.run(core.run(task))
    events = [{"type": e.type.value, "data": e.data} for e in result.events]
    return {
        "task": task,
        "strategy": result.strategy.value if result.strategy else None,
        "events": events,
        "final_text": result.final_text,
        "found_sites": _extract_found(events),
        "backend": label,
    }


class _Handler(BaseHTTPRequestHandler):
    server_version = "YAIHostH/0.1"

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

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        pass

    def do_GET(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        if route in ("/", "/index.html"):
            self._send_file(WEB_DIR / "index.html", "text/html; charset=utf-8")
        elif route == "/api/health":
            self._send_json({
                "ok": True,
                "mode": "real-llm" if os.getenv("OPENAI_API_KEY") else "offline-scripted",
                "port": PORT,
            })
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
        except Exception as exc:  # noqa: BLE001
            self._send_json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)


def main() -> None:
    if not (WEB_DIR / "index.html").is_file():
        print(f"[错误] 找不到前端页面：{WEB_DIR / 'index.html'}")
        sys.exit(1)
    server = ThreadingHTTPServer((HOST, PORT), _Handler)
    url = f"http://{HOST}:{PORT}"
    mode = "真实模型" if os.getenv("OPENAI_API_KEY") else "离线脚本模型"
    print("=" * 60)
    print("host_h sherlock 嵌入演示网页（标准库零依赖，仅监听本机）")
    print(f"  地址：{url}    模式：{mode}")
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
