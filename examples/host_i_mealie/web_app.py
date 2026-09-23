"""host_i Mealie 极简网页壳：标准库零依赖。

运行：
    .venv/Scripts/python.exe examples/host_i_mealie/web_app.py
然后浏览器访问 http://127.0.0.1:8203 （启动时自动打开）。

设计：
- 左侧"原生 Mealie"：食谱卡片网格（得自己点搜索、翻详情、手动加购物清单）；
- 右侧"嵌入 YAI Core 后"：说一句"今晚做番茄炒蛋"，
  Core 自己串起搜菜 -> 看食材 -> 加购物清单 三步，事件流逐条播放。

有 MEALIE_URL+TOKEN 走真实 Mealie；否则离线脚本模型。只监听 127.0.0.1。
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

from host_i_mealie import capabilities as cap  # noqa: E402
from host_i_mealie.demo_agent import DEFAULT_TASK, build_core  # noqa: E402
from runner_common import build_model, load_dotenv  # noqa: E402
from yai_core import AgentCore, build_spec  # noqa: E402

WEB_DIR = _HERE / "web"
HOST = "127.0.0.1"
PORT = 8203

load_dotenv()


def _make_core() -> tuple[AgentCore, str]:
    if os.getenv("MEALIE_URL") and os.getenv("MEALIE_TOKEN"):
        model, label = build_model()
        core = AgentCore(model, auto_approve_tools=True)
        core.register_tools([
            build_spec(cap.search_recipes),
            build_spec(cap.get_recipe),
            build_spec(cap.get_mealplan),
            build_spec(cap.add_mealplan),
            build_spec(cap.get_shopping_list),
            build_spec(cap.add_shopping_item),
        ])
        return core, label
    return build_core(), "离线脚本模型"


def _run_core(task: str) -> dict:
    core, label = _make_core()
    result = asyncio.run(core.run(task))
    events = [{"type": e.type.value, "data": e.data} for e in result.events]
    return {
        "task": task,
        "strategy": result.strategy.value if result.strategy else None,
        "events": events,
        "final_text": result.final_text,
        "backend": label,
    }


class _Handler(BaseHTTPRequestHandler):
    server_version = "YAIHostI/0.1"

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
            real = bool(os.getenv("MEALIE_URL") and os.getenv("MEALIE_TOKEN"))
            self._send_json({
                "ok": True,
                "mode": "real-mealie" if real else "offline",
                "port": PORT,
            })
        else:
            self.send_error(404, "Not Found")

    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/api/run":
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
        print(f"[错误] 找不到前端：{WEB_DIR / 'index.html'}")
        sys.exit(1)
    server = ThreadingHTTPServer((HOST, PORT), _Handler)
    url = f"http://{HOST}:{PORT}"
    mode = "真实 Mealie" if (os.getenv("MEALIE_URL") and os.getenv("MEALIE_TOKEN")) else "离线脚本"
    print("=" * 60)
    print("host_i Mealie 嵌入演示网页（标准库零依赖）")
    print(f"  地址：{url}    模式：{mode}")
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
