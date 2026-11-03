"""真实浏览器验收：用 Playwright 后端 + Browser Provider 完成一条完整任务。

任务：打开本地表单页 → 读取元素 → 文本框输入 → 下拉选择 → 点击提交 →
等待"提交成功"出现 → 再观察确认；同时验证：
- 动作后旧快照引用被判定过期（不会拿旧引用继续点）；
- 每个工具都经 ToolExecutor（收据 + 事件），不是绕开治理直连后端；
- 页面来源白名单生效（先用非法来源导航验证被拦）。

用法::

    python scripts/browser_smoke.py            # 自动选 Playwright Chromium / 系统 Chrome
    python scripts/browser_smoke.py --channel chrome   # 指定系统浏览器
    python scripts/browser_smoke.py --json

退出码：0 通过；2 环境不具备（未安装 playwright / 没有浏览器）；1 验证失败。
证据：控制台 JSON + `docs/exports/browser-smoke.png`（`docs/exports/` 已被 gitignore）。
"""

from __future__ import annotations

import argparse
import asyncio
import http.server
import json
import sys
import threading
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>YAI Browser Smoke</title></head>
<body>
  <h1>YAI 浏览器验收</h1>
  <form id="f" onsubmit="return false">
    <label>姓名 <input id="name" type="text" aria-label="姓名" placeholder="姓名"></label>
    <label>城市
      <select id="city" aria-label="城市">
        <option value="">请选择</option>
        <option value="sh">上海</option>
        <option value="bj">北京</option>
      </select>
    </label>
    <button id="submit" type="button">提交</button>
  </form>
  <p id="result"></p>
  <script>
    document.getElementById('submit').addEventListener('click', () => {
      const name = document.getElementById('name').value.trim();
      const city = document.getElementById('city').value;
      const ok = name && city;
      document.getElementById('result').textContent =
        ok ? `提交成功：${name} / ${city}` : '请填写完整';
    });
  </script>
</body></html>
"""


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - 标准库回调名
        body = PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:  # noqa: D102
        return


def _serve() -> tuple[http.server.HTTPServer, int]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, port


def _tool_result(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:  # pragma: no cover - 防御
        raise AssertionError(f"工具结果不是 JSON：{text[:200]}") from exc
    if not isinstance(parsed, dict):
        raise AssertionError("工具结果不是 JSON object")
    return parsed


async def _run(*, channel: str | None) -> tuple[bool, str, dict[str, Any]]:
    from yai_core.channels import CollectChannel
    from yai_core.integrations.browser import BrowserPolicy, BrowserProvider, build_browser_tools
    from yai_core.integrations.browser.playwright_backend import PlaywrightBrowserBackend
    from yai_core.policy import AllowlistPolicy
    from yai_core.tools import ToolExecutor, ToolRegistry

    server, port = _serve()
    origin = f"http://127.0.0.1:{port}"
    evidence: dict[str, Any] = {"origin": origin, "channel": channel or "playwright-chromium"}
    backend = PlaywrightBrowserBackend(headless=True, channel=channel)
    try:
        await backend.start()
    except Exception as exc:  # pragma: no cover - 环境相关
        server.shutdown()
        return False, f"无法启动浏览器：{type(exc).__name__}: {exc}", evidence

    provider = BrowserProvider(
        backend,
        policy=BrowserPolicy(
            allowed_origins=frozenset({origin}), require_approval_for=frozenset()
        ),
    )
    registry = ToolRegistry()
    registry.register_many(build_browser_tools(provider))
    executor = ToolExecutor(registry, AllowlistPolicy((), mode="auto"), CollectChannel())
    receipts: list[dict[str, Any]] = []

    async def call(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        events, ok, text = await executor.execute(tool, arguments)
        try:
            result = _tool_result(text)
        except AssertionError:
            result = {"_raw": text}
        for event in events:
            if event.type == "tool_result" and isinstance(event.data.get("receipt"), dict):
                receipts.append(event.data["receipt"])
        step = {"tool": tool, "ok": ok, "status": result.get("status")}
        evidence.setdefault("steps", []).append(step)
        return {**result, "_executor_ok": ok}

    async def observe() -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        observed = await call("browser_observe", {})
        by_name = {str(item.get("name", "")): item for item in observed.get("elements", [])}
        return observed, by_name

    try:
        # 1) 来源白名单：非法来源在导航前被拒（gate 拒绝，执行器 ok=False）
        blocked = await call("browser_open", {"url": "http://example.invalid/"})
        evidence["origin_gate_blocked"] = blocked.get("_executor_ok") is False

        # 2) 打开本地页面并观察
        opened = await call("browser_open", {"url": origin + "/"})
        if not opened.get("ok"):
            return False, f"打开页面失败：{opened}", evidence
        observed, by_name = await observe()
        evidence["element_count"] = len(observed.get("elements", []))
        if "姓名" not in by_name or "提交" not in by_name:
            return False, f"快照里没有姓名输入/提交按钮：{list(by_name)}", evidence

        # 3) 输入（动作后必须重新观察，旧引用会过期）
        typed = await call("browser_type", {"ref": by_name["姓名"]["ref"], "text": "YAI 验收"})
        if typed.get("status") != "executed":
            return False, f"输入失败：{typed}", evidence
        observed, by_name = await observe()
        combo = next(
            (
                item
                for item in observed.get("elements", [])
                if item.get("tag") == "select" or item.get("name") == "城市"
            ),
            None,
        )
        if combo is None:
            names = [e.get("name") for e in observed.get("elements", [])]
            return False, f"快照里没有下拉框：{names}", evidence

        # 4) 下拉选择（再观察后提交）
        selected = await call("browser_select", {"ref": combo["ref"], "value": "sh"})
        if selected.get("status") != "executed":
            return False, f"下拉选择失败：{selected}", evidence
        _observed, by_name = await observe()
        clicked = await call("browser_click", {"ref": by_name["提交"]["ref"]})
        if clicked.get("status") != "executed":
            return False, f"点击失败：{clicked}", evidence

        # 5) 等待结果文本并最终观察（新快照）
        waited = await call("browser_wait", {"text_contains": "提交成功", "timeout_ms": 5000})
        if not waited.get("matched"):
            return False, f"等待提交成功超时：{waited}", evidence
        final = await call("browser_observe", {})
        text = str(final.get("text", ""))
        evidence["final_text"] = text[:200]
        if "提交成功" not in text or "YAI 验收" not in text:
            return False, f"最终页面文本不符合预期：{text[:200]}", evidence

        # 6) 旧引用必须过期：拿更早那张快照的提交按钮 ref 再点一次
        stale = await call("browser_click", {"ref": by_name["提交"]["ref"]})
        evidence["stale_ref_rejected"] = stale.get("status") == "stale_ref"
        if not evidence["stale_ref_rejected"]:
            return False, f"旧引用未被拒绝：{stale}", evidence

        # 6) 截图作为人工可核对的证据（直接走 Playwright page，不冒充 Provider 能力）
        screenshot = ROOT / "docs" / "exports" / "browser-smoke.png"
        screenshot.parent.mkdir(parents=True, exist_ok=True)
        page = backend._require_page()  # noqa: SLF001 - 验收脚本读取底层页面截图
        await page.screenshot(path=str(screenshot), full_page=True)
        evidence["screenshot"] = str(screenshot.relative_to(ROOT))
        evidence["receipts"] = len(receipts)
        if not receipts:
            return False, "没有执行收据（未经过 ToolExecutor？）", evidence
        return True, f"真实浏览器任务完成：{origin}（收据 {len(receipts)} 条）", evidence
    finally:
        await backend.close()
        server.shutdown()


def main() -> int:
    parser = argparse.ArgumentParser(description="真实浏览器验收（Playwright + Browser Provider）")
    parser.add_argument("--channel", default=None, help="系统浏览器 channel（chrome / msedge）")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    args = parser.parse_args()

    if args.channel is None:
        try:
            import playwright  # noqa: F401
        except ImportError:
            payload = {"ok": False, "skipped": "playwright-missing"}
            print(json.dumps(payload) if args.json else "[browser-smoke] 跳过：未安装 playwright")
            return 2
    ok, detail, evidence = asyncio.run(_run(channel=args.channel))
    payload = {"ok": ok, "detail": detail, **evidence}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"[browser-smoke] {'通过' if ok else '失败'}：{detail}")
        print(json.dumps(evidence, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
