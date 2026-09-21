"""宿主 E 网页工作台：销售 CRM 的"应用形态"演示（纯标准库 HTTP 壳）。

架构边界（演示"应用 AI 化只需一个嵌入点"）：
- 业务本体 ``crm_app.py``：纯销售 CRM，**零 yai_core 依赖**，没有 Core 也能独立运行；
- 唯一嵌入点 ``agent_bridge.py``：全项目只有它直接 import yai_core，负责装配数字员工；
- 本文件：只做 HTTP / SSE 与 CRM 原生数据，不出现任何 yai_core 符号，
  AI 请求一律转发给 :class:`AgentBridge`。

Core 开关（POST /api/core）：关闭后回到"纯 CRM"，不启动任何 Agent 任务；
权限挡位（POST /api/permission）：manual / partial / auto，对下一次任务生效。

用法：
    .\\.venv\\Scripts\\python.exe examples\\host_e_sales_crm\\web_app.py
    uv run python examples\\host_e_sales_crm\\web_app.py --port 8200
然后浏览器打开 http://127.0.0.1:8200 （默认启动 1 秒后自动打开）。
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # examples/

from runner_common import bootstrap, build_model, load_dotenv  # noqa: E402

bootstrap()

# 兼容历史导入：测试/脚本仍可从 web_app 取这些符号；唯一实现都在 agent_bridge。
from host_e_sales_crm.agent_bridge import (  # noqa: E402,F401
    PERMISSION_MODES,
    AgentBridge,
    WebChannel,
    resolve_pending,
)
from host_e_sales_crm.crm_app import SalesCrm  # noqa: E402

HERE = Path(__file__).resolve().parent
WEB_DIR = HERE / "web"


class Workbench:
    """持有共享 CRM、唯一嵌入点（AgentBridge）与原生业务互斥锁。"""

    def __init__(
        self,
        model_factory,
        model_label: str = "",
        crm: SalesCrm | None = None,
        *,
        core_enabled: bool = True,
        permission_mode: str = "partial",
    ) -> None:
        self.crm = crm or SalesCrm()
        self.bridge = AgentBridge(
            self.crm,
            model_factory,
            model_label,
            enabled=core_enabled,
            permission_mode=permission_mode,
        )
        # 原生写操作与 Agent 写操作的互斥锁（避免两边同时改数据）。
        self._lock = threading.Lock()

    # ---- 状态（AI 状态全部来自嵌入点）----
    def state(self) -> dict:
        return self.bridge.ai_state()

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

    # ---- 原生业务（不经过 Agent）----
    def native_action(self, op: str, payload: dict) -> tuple[int, dict]:
        """软件原生写操作：Agent 执行中拒绝，避免两边同时改数据。"""
        with self._lock:
            if self.bridge.active_id():
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
        if op == "customer":
            return 200, crm.add_customer(
                str(payload.get("name", "")),
                str(payload.get("company", "")),
                str(payload.get("level", "") or "普通"),
            )
        return 404, {"error": "unknown_action", "detail": op}

    def reset(self) -> bool:
        # 任务卡住（如澄清/授权等待）时允许"先终止再重置"，避免重置被 409 永久卡死。
        active_id = self.bridge.active_id()
        if active_id:
            self.bridge.cancel(active_id)
            for _ in range(20):  # 最多等 1 秒让 CancelledError 落盘
                time.sleep(0.05)
                if not self.bridge.active_id():
                    break
        if self.bridge.active_id():
            return False
        with self._lock:
            self.crm._reset()  # noqa: SLF001 - 演示数据重置本就是宿主公开的菜单能力
        return True

    # ---- Core 开关 / 权限挡位（转发嵌入点）----
    def set_core_enabled(self, on: bool) -> bool:
        return self.bridge.set_enabled(on)

    def set_permission_mode(self, mode: str) -> str:
        return self.bridge.set_permission_mode(mode)

    # ---- 会话生命周期（转发嵌入点）----
    def start_run(self, task: str) -> tuple[str | None, str | None]:
        """返回 (run_id, 错误码)；错误码为 core_disabled / busy / None。"""
        if not self.bridge.enabled:
            return None, "core_disabled"
        run_id = self.bridge.start_run(task)
        if run_id is None:
            return None, "busy"
        return run_id, None

    def get_run(self, run_id: str) -> dict | None:
        return self.bridge.get_run(run_id)

    def resolve(self, run_id: str, kind: str, value) -> bool:
        return self.bridge.resolve(run_id, kind, value)

    def cancel(self, run_id: str) -> bool:
        return self.bridge.cancel(run_id)


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
            if path == "/api/core":
                on = bool(payload.get("enabled"))
                workbench.set_core_enabled(on)
                self._send_json(200, {"ok": True, "core_enabled": on})
                return
            if path == "/api/permission":
                mode = str(payload.get("mode", ""))
                if mode not in PERMISSION_MODES:
                    self._send_json(
                        400, {"error": "bad_mode", "detail": f"可选 {PERMISSION_MODES}"}
                    )
                    return
                workbench.set_permission_mode(mode)
                self._send_json(200, {"ok": True, "permission_mode": mode})
                return
            if path == "/api/run":
                task = str(payload.get("task", "")).strip()
                if not task:
                    self._send_json(400, {"error": "bad_request", "detail": "task 不能为空"})
                    return
                run_id, err = workbench.start_run(task)
                if err == "core_disabled":
                    self._send_json(
                        503, {"error": "core_disabled", "detail": "Core 已关闭，请先启用数字员工"}
                    )
                elif err == "busy":
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
            if path in (
                "/api/crm/followup",
                "/api/crm/todo",
                "/api/crm/complete",
                "/api/crm/customer",
            ):
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
    parser.add_argument(
        "--core-off", action="store_true",
        help="启动时关闭 Core（纯 CRM 形态），用于演示有无 Core 的对比",
    )
    parser.add_argument(
        "--permission", choices=PERMISSION_MODES, default="partial",
        help="初始权限挡位（默认 partial：读放行、写/新能力询问）",
    )
    args = parser.parse_args(argv)

    load_dotenv()
    model, backend = build_model()
    workbench = Workbench(
        model_factory=lambda: model,
        model_label=backend,
        crm=SalesCrm(),
        core_enabled=not args.core_off,
        permission_mode=args.permission,
    )
    httpd = create_server(workbench, args.host, args.port)
    url = f"http://{args.host}:{httpd.server_port}"
    print("=" * 60)
    print("host_e 销售 CRM · 网页工作台（业务系统 + 唯一嵌入点）")
    print(f"  本地地址 : {url}")
    print(f"  模型后端 : {backend}")
    print(f"  Core 状态: {'已启用（数字员工在线）' if not args.core_off else '已关闭（纯 CRM）'}")
    print("  权限挡位 : " + workbench.bridge.permission_mode)
    print("  页面右上角可一键开关 Core、切换权限挡位。Ctrl+C 停止。")
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
