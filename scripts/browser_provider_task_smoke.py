"""Browser Provider 真实任务 E2E（受控浏览器，非当前用户标签页）。

本地 HTTP 页面 → 观察 → 定位输入/按钮 → 审批 → 输入/点击 → 再观察 → 真实页面文本验证
→ 执行收据链校验。使用真实 Playwright Chromium；不是扩展当前标签页写操作
（当前标签页写操作按 ADR 0006 暂不实现）。

运行：.venv\\Scripts\\python.exe scripts\\browser_provider_task_smoke.py
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from yai_core.channels import CollectChannel
from yai_core.integrations.browser import (
    BrowserPolicy,
    BrowserProvider,
    build_browser_tools,
)
from yai_core.integrations.browser.playwright_backend import PlaywrightBrowserBackend
from yai_core.policy import AllowlistPolicy
from yai_core.tools import ToolExecutor, ToolRegistry
from yai_core.types import Evidence, VerificationResult, verify_receipt_chain

_PAGE = (
    '<!doctype html><html><head><meta charset="utf-8"><title>YAI E2E</title></head>'
    "<body><h1>YAI Browser E2E</h1>"
    '<label>名字 <input id="name" placeholder="请输入名字"></label>'
    "<button id=\"go\" onclick=\"document.getElementById('result')"
    ".textContent='完成：'+document.getElementById('name').value\">提交</button>"
    '<div id="result"></div></body></html>'
)


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = _PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        return


class _ApprovingChannel(CollectChannel):
    def __init__(self) -> None:
        super().__init__()
        self.approvals = 0

    async def confirm(self, tool_name: str, arguments: dict, context: Any = None) -> bool:
        self.approvals += 1
        return True

    async def ask(self, question: str) -> str:
        return "yes"


async def _run() -> dict[str, Any]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_address[1]}"
    channel = _ApprovingChannel()
    backend = PlaywrightBrowserBackend(headless=True)
    await backend.start()
    report: dict[str, Any] = {"origin": origin, "operations": [], "approvals": 0}
    try:
        provider = BrowserProvider(
            backend, policy=BrowserPolicy.secure([origin])
        )
        registry = ToolRegistry()
        registry.register_many(build_browser_tools(provider))
        executor = ToolExecutor(
            registry, AllowlistPolicy(mode="allow_all"), channel
        )

        async def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            events, ok, text = await executor.execute(name, arguments)
            receipts = [
                event.data["receipt"]
                for event in events
                if "receipt" in event.data
            ]
            report["operations"].append(
                {
                    "tool": name,
                    "ok": ok,
                    "text": text[:300],
                    "receipt": receipts[-1] if receipts else None,
                }
            )
            return {"ok": ok, "text": text, "events": events}

        await call("browser_open", {"url": origin + "/"})
        observed = await call("browser_observe", {})
        data = json.loads(observed["text"])
        elements = data.get("elements", [])
        input_el = next(
            (e for e in elements if e.get("role") == "textbox" or e.get("tag") == "input"),
            None,
        )
        button_el = next(
            (e for e in elements if e.get("role") == "button" or e.get("tag") == "button"),
            None,
        )
        if input_el is None or button_el is None:
            report["ok"] = False
            report["error"] = "未找到输入框或按钮"
            return report
        await call("browser_type", {"ref": input_el["ref"], "text": "YAI"})
        # type 动作会做一次再观察并生成新快照；旧按钮 ref 已过期，必须重新观察。
        refreshed = await call("browser_observe", {})
        refreshed_data = json.loads(refreshed["text"])
        button_el = next(
            (
                e
                for e in refreshed_data.get("elements", [])
                if e.get("role") == "button" or e.get("tag") == "button"
            ),
            None,
        )
        if button_el is None:
            report["ok"] = False
            report["error"] = "刷新后未找到按钮"
            return report
        await call("browser_click", {"ref": button_el["ref"]})
        observed = await call("browser_observe", {})
        page = json.loads(observed["text"])
        text = str(page.get("text", ""))
        report["page_text"] = text
        report["approvals"] = channel.approvals
        report["captured_events"] = [
            event.type.value for event in observed["events"]
        ]
        receipts = [
            item["receipt"]
            for item in report["operations"]
            if item.get("receipt")
        ]
        report["receipt_chain_ok"] = verify_receipt_chain(receipts)
        report["verification"] = VerificationResult(
            status="verified_success" if "完成：YAI" in text else "verified_failure",
            goal="在受控浏览器中填写名字并提交",
            confidence=0.9 if "完成：YAI" in text else 0.8,
            evidence=(
                Evidence(
                    kind="page_text",
                    summary="页面真实文本包含完成结果" if "完成：YAI" in text else "未找到完成结果",
                    ok="完成：YAI" in text,
                    detail={"text": text[:500]},
                ),
            ),
        ).to_dict()
        report["ok"] = bool(
            report.get("receipt_chain_ok")
            and "完成：YAI" in text
            and report["verification"]["status"] == "verified_success"
        )
    finally:
        await backend.close()
        server.shutdown()
        server.server_close()
    return report


def main() -> int:
    report = asyncio.run(_run())
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
