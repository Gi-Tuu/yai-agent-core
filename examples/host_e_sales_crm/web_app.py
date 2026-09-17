"""宿主 E 网页工作台：销售 CRM 数字员工的"应用形态"演示（纯标准库）。

- 业务本体仍是 crm_app.py（零 yai_core 导入）；本文件只把 AgentCore 接到网页。
- 读工具自动放行；写工具经 SSE 推送 permission_request，页面点"允许/拒绝"后继续
  （Channel SPI 的 Web 实现：confirm/ask 等待浏览器回传决策）。
- 事件流走 SSE（GET /api/stream），启动任务走 POST /api/run，只监听 127.0.0.1。
- 不引入任何第三方依赖，uv run 或 venv python 直接可跑。

用法：
    .\\.venv\\Scripts\\python.exe examples\\host_e_sales_crm\\web_app.py
    uv run python examples/host_e_sales_crm\\web_app.py --port 8200
然后浏览器打开 http://127.0.0.1:8200 （默认启动 1 秒后自动打开）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import queue
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # examples/

from runner_common import bootstrap, build_model, load_dotenv  # noqa: E402

bootstrap()

from host_e_sales_crm.crm_app import SalesCrm  # noqa: E402
from host_e_sales_crm.run_agent import READ_TOOLS  # noqa: E402
from yai_core import AgentCore, EventType, discover  # noqa: E402
from yai_core.policy import AllowlistPolicy  # noqa: E402

HERE = Path(__file__).resolve().parent
WEB_DIR = HERE / "web"

# 交互式授权/澄清的等待上限：超时按"拒绝/空回答"处理，避免任务永久挂起。
PROMPT_TIMEOUT_SECONDS = 600
# 内存中最多保留的历史会话（SSE 重连/取结果用），单用户本地演示足够。
MAX_RUNS = 10


class WebChannel:
    """Channel SPI 的网页实现：emit 不做事（事件统一由 astream 推送），
    confirm/ask 挂起等待浏览器经 /api/decision、/api/answer 回传。"""

    def __init__(self, run: dict, loop: asyncio.AbstractEventLoop) -> None:
        self._run = run
        self._loop = loop

    async def emit(self, event) -> None:  # noqa: ANN001 - 事件统一走 astream
        return None

    async def ask(self, question: str) -> str:
        return await self._prompt("ask", {"question": question}, "")

    async def confirm(self, tool_name: str, arguments: dict) -> bool:
        payload = {"tool": tool_name, "arguments": arguments}
        return bool(await self._prompt("confirm", payload, False))

    async def _prompt(self, kind: str, data: dict, default):
        future = self._loop.create_future()
        self._run["pending"] = (kind, future)
        event_type = "clarify_request" if kind == "ask" else "permission_request"
        self._run["queue"].put({"type": event_type, "data": data})
        try:
            return await asyncio.wait_for(future, timeout=PROMPT_TIMEOUT_SECONDS)
        except TimeoutError:
            return default
        finally:
            self._run["pending"] = None


def resolve_pending(run: dict, kind: str, value) -> bool:
    """从 HTTP 线程安全地兑现浏览器的授权/回答决策。"""
    pending = run.get("pending")
    if not pending or pending[0] != kind or pending[1].done():
        return False
    pending[1].get_loop().call_soon_threadsafe(pending[1].set_result, value)
    return True


def _event_type(event) -> str:
    event_type = event.type
    return event_type.value if hasattr(event_type, "value") else str(event_type)


class Workbench:
    """持有共享 CRM、后台 asyncio 循环与全部会话状态。"""

    def __init__(self, model_factory, model_label: str = "", crm: SalesCrm | None = None) -> None:
        self.crm = crm or SalesCrm()
        self._model_factory = model_factory
        self.model_label = model_label
        self._loop = asyncio.new_event_loop()
        self._runs: dict[str, dict] = {}
        self._lock = threading.Lock()
        threading.Thread(target=self._run_loop, name="yai-web-loop", daemon=True).start()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    # ---- 只读状态 ----
    def tools(self) -> list[dict]:
        result = []
        for spec in discover(self.crm):
            source = getattr(spec, "source", "native")
            source = source.value if hasattr(source, "value") else str(source)
            result.append({
                "name": spec.name,
                "description": spec.description,
                "source": source,
                "access": "read" if spec.name in READ_TOOLS else "write",
            })
        return result

    def state(self) -> dict:
        with self._lock:
            active = self._active_id_locked()
        return {"model_label": self.model_label, "tools": self.tools(), "active": active}

    def snapshot(self) -> dict:
        """原生 CRM 界面的全部数据：与 Agent 读的是同一个 SalesCrm 对象。"""
        crm = self.crm
        return {
            "customers": crm.list_customers(),
            "due_followup": crm.customers_due_followup(3),
            "orders": crm.list_orders(),
            "followups": crm.list_followups(),
            "todos": crm.list_todos("all"),
            "brief": crm.daily_brief(),
            "total_amount": crm.sum_amount(),
        }

    def native_action(self, op: str, payload: dict) -> tuple[int, dict]:
        """软件原生写操作（不经过 Agent）：Agent 执行中拒绝，避免两边同时改数据。"""
        with self._lock:
            if self._active_id_locked():
                return 409, {"error": "busy", "detail": "数字员工正在执行，请等待完成或先终止"}
        crm = self.crm
        if op == "followup":
            return 200, crm.add_followup(
                str(payload.get("customer", "")), str(payload.get("content", ""))
            )
        if op == "todo":
            return 200, crm.create_todo(
                str(payload.get("title", "")), str(payload.get("due", ""))
            )
        if op == "complete":
            return 200, crm.complete_todo(str(payload.get("title", "")))
        return 404, {"error": "unknown_action", "detail": op}

    def reset(self) -> bool:
        # 任务卡住（如澄清/授权等待）时允许"先终止再重置"，避免重置被 409 永久卡死。
        with self._lock:
            active_id = self._active_id_locked()
        if active_id:
            self.cancel(active_id)
            for _ in range(20):  # 最多等 1 秒让 CancelledError 落盘
                time.sleep(0.05)
                with self._lock:
                    if not self._active_id_locked():
                        break
        with self._lock:
            if self._active_id_locked():
                return False
            self.crm._reset()  # noqa: SLF001 - 演示数据重置本就是宿主公开的菜单能力
            return True

    def cancel(self, run_id: str) -> bool:
        """终止指定会话：兑现挂起的授权/澄清，并取消后台 Agent 任务。"""
        run = self._runs.get(run_id)
        if not run or not run["active"]:
            return False
        task = run.get("task")
        pending = run.get("pending")

        def _do_cancel() -> None:
            if pending and not pending[1].done():
                kind, future = pending
                if not future.done():
                    future.set_result(False if kind == "confirm" else "")
            if task is not None:
                task.cancel()

        self._loop.call_soon_threadsafe(_do_cancel)
        return True

    # ---- 会话生命周期 ----
    def start_run(self, task: str) -> str | None:
        with self._lock:
            if self._active_id_locked():
                return None
            run_id = uuid.uuid4().hex[:12]
            run: dict = {
                "queue": queue.Queue(), "pending": None, "active": True,
                "task_text": task, "task": None,
            }
            self._runs[run_id] = run
            self._prune_locked()
        channel = WebChannel(run, self._loop)
        core = AgentCore.auto(
            self.crm,
            self._model_factory(),
            channel=channel,
            policy=AllowlistPolicy(READ_TOOLS, mode="auto"),
            llm_router=True,
        )
        asyncio.run_coroutine_threadsafe(self._run_guarded(core, task, run), self._loop)
        return run_id

    def get_run(self, run_id: str) -> dict | None:
        return self._runs.get(run_id)

    def resolve(self, run_id: str, kind: str, value) -> bool:
        run = self._runs.get(run_id)
        if not run:
            return False
        return resolve_pending(run, kind, value)

    def _active_id_locked(self) -> str | None:
        for run_id, run in self._runs.items():
            if run["active"]:
                return run_id
        return None

    def _prune_locked(self) -> None:
        finished = [r for r in self._runs.values() if not r["active"]]
        for run in finished[:-MAX_RUNS]:
            self._runs = {k: v for k, v in self._runs.items() if v is not run}

    async def _run_guarded(self, core: AgentCore, task: str, run: dict) -> None:
        run["task"] = asyncio.current_task()
        cancelled = False
        final_text = ""
        strategy = None
        try:
            async for event in core.astream(task):
                run["queue"].put({"type": _event_type(event), "data": event.data})
                if event.type == EventType.DONE:
                    final_text = event.data.get("final_text", "")
                    strategy = event.data.get("strategy")
        except asyncio.CancelledError:
            cancelled = True
            run["queue"].put({
                "type": "cancelled",
                "data": {"reason": "任务已被用户终止"},
            })
            raise
        except Exception as exc:  # noqa: BLE001 - 网页端必须看到错误而不是白屏
            run["queue"].put({"type": "error", "data": {"error": f"{type(exc).__name__}: {exc}"}})
        finally:
            run["active"] = False
            run["task"] = None
            if not cancelled:
                run["queue"].put({
                    "type": "done",
                    "data": {"final_text": final_text, "strategy": strategy},
                })


def make_handler(workbench: Workbench):
    class Handler(BaseHTTPRequestHandler):
        server_version = "YaiHostEWeb/0.1"

        def log_message(self, fmt: str, *args) -> None:
            sys.stdout.write("[web] " + fmt % args + "\n")

        # ---- 基础工具 ----
        def _send_json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
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

        # ---- 路由 ----
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = parsed.path
            if path in ("/", "/index.html"):
                page = WEB_DIR / "index.html"
                body = page.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/api/state":
                self._send_json(200, workbench.state())
                return
            if path == "/api/snapshot":
                self._send_json(200, workbench.snapshot())
                return
            if path == "/api/stream":
                query = parse_qs(parsed.query)
                run_id = (query.get("run_id") or [""])[0]
                run = workbench.get_run(run_id)
                if not run:
                    self._send_json(404, {"error": "run_not_found"})
                    return
                self._serve_stream(run)
                return
            self._send_json(404, {"error": "not_found", "detail": path})

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            payload = self._read_json()
            if payload is None:
                self._send_json(400, {"error": "bad_json"})
                return
            if path == "/api/run":
                task = str(payload.get("task", "")).strip()
                if not task:
                    self._send_json(400, {"error": "bad_request", "detail": "task 不能为空"})
                    return
                run_id = workbench.start_run(task)
                if run_id is None:
                    self._send_json(409, {"error": "busy", "detail": "已有任务在执行"})
                else:
                    self._send_json(200, {"run_id": run_id})
                return
            if path == "/api/decision":
                ok = workbench.resolve(str(payload.get("run_id", "")), "confirm",
                                       bool(payload.get("approved")))
                self._send_json(200 if ok else 404, {"ok": ok})
                return
            if path == "/api/answer":
                ok = workbench.resolve(str(payload.get("run_id", "")), "ask",
                                       str(payload.get("answer", "")))
                self._send_json(200 if ok else 404, {"ok": ok})
                return
            if path == "/api/cancel":
                ok = workbench.cancel(str(payload.get("run_id", "")))
                self._send_json(200 if ok else 404, {"ok": ok})
                return
            if path in ("/api/crm/followup", "/api/crm/todo", "/api/crm/complete"):
                op = path.rsplit("/", 1)[-1]
                status, result = workbench.native_action(op, payload)
                self._send_json(status, result)
                return
            if path == "/api/reset":
                ok = workbench.reset()
                if ok:
                    self._send_json(200, {"ok": True})
                else:
                    self._send_json(409, {"error": "busy", "detail": "任务执行中不能重置"})
                return
            self._send_json(404, {"error": "not_found", "detail": path})

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
                if event["type"] in ("done", "error", "cancelled"):
                    return

    return Handler


def create_server(workbench: Workbench, host: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(workbench))
    server.daemon_threads = True
    return server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="host_e 销售 CRM 数字员工网页工作台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.getenv("YAI_WEB_PORT", "8200")))
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args(argv)

    load_dotenv()
    model, backend = build_model()
    workbench = Workbench(model_factory=lambda: model, model_label=backend, crm=SalesCrm())
    httpd = create_server(workbench, args.host, args.port)
    url = f"http://{args.host}:{httpd.server_port}"
    print("=" * 60)
    print("host_e 销售 CRM · 数字员工网页工作台")
    print(f"  本地地址 : {url}")
    print(f"  模型后端 : {backend}")
    print("  读工具自动放行；写工具会在网页内请求授权。Ctrl+C 停止。")
    print("=" * 60)
    if not args.no_open:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
