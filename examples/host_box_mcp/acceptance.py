"""Box MCP 电脑操作验收：读屏 → GUI 输入 → 浏览器写操作 → 清理。

这不是 Side Panel 扩展的验收，而是「YAI 作为 MCP Client 驱动 Box 云桌面」的真机验收：

- A. 读：`box_status` + `computer_state`；
- B. GUI 写：在 Box 桌面启动终端，**用 `computer_act` 键入命令**，
  再通过 `read_file` 验证命令确实执行（屏幕输入 → 文件内容）；
- C. 浏览器写：`write_file` 建本地表单 → `browser_navigate` → `browser_snapshot`
  → `browser_type` / `browser_click` → `browser_read` 验证结果；
- D. 清理：删除验收产生的文件。

注意：Box 的浏览器窗口是**按 MCP 会话**隔离的，所以 C 的全部动作必须在同一个 bridge
连接内完成；换一次 `connect()` 就会关掉上一次的窗口。

运行::

    uv run python examples/host_box_mcp/acceptance.py
    uv run python examples/host_box_mcp/acceptance.py --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from yai_core.integrations.mcp import McpServerConfig, McpToolBridge  # noqa: E402

BOX_URL = "https://mcp.box.simular.ai/mcp"
FORM_HTML = """<!doctype html><html><body>
<h1>YAI Box form</h1>
<input id="name" placeholder="name">
<button id="go">Greet</button>
<div id="out"></div>
<script>
document.getElementById('go').addEventListener('click', () => {
  document.getElementById('out').textContent =
    'hello ' + document.getElementById('name').value;
});
</script>
</body></html>"""
REF_RE = re.compile(r"\[ref=(e\d+)\]")


def _load_env_local() -> None:
    path = ROOT / ".env.local"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


async def _run_checks() -> dict[str, Any]:
    bridge = McpToolBridge(
        McpServerConfig(alias="box", url=BOX_URL, token_env="BOX_TOKEN")
    )
    specs = {spec.name: spec for spec in await bridge.connect()}
    report: dict[str, Any] = {"tools": len(specs), "checks": {}}

    def mark(name: str, ok: bool, detail: str) -> None:
        report["checks"][name] = {"ok": ok, "detail": detail[:500]}

    try:
        # A. 读状态 + 桌面可观测
        status = json.loads(str(await specs["box_status"].handler()))
        mark("box_status", status.get("state") in {"running", "hibernated"}, str(status))
        state = str(await specs["computer_state"].handler())
        mark("computer_state", "Windows" in state, state)

        # B. GUI 输入 → 文件内容
        act = str(
            await specs["computer_act"].handler(
                actions=[
                    {"action": "launch_app", "app": "xfce4-terminal"},
                    {"action": "wait", "seconds": 2},
                    {"action": "type", "text": "echo box-gui-ok > /workspace/gui-proof.txt"},
                    {"action": "key", "key": "return"},
                    {"action": "wait", "seconds": 1},
                ]
            )
        )
        content = str(await specs["read_file"].handler(path="/workspace/gui-proof.txt"))
        mark(
            "gui_typing",
            "box-gui-ok" in content,
            f"{act[:160]} | read_file: {content.strip()[:120]}",
        )

        # C. 浏览器写：必须同一会话内完成
        await specs["write_file"].handler(path="/workspace/form.html", content=FORM_HTML)
        nav = str(await specs["browser_navigate"].handler(url="file:///workspace/form.html"))
        refs = REF_RE.findall(nav)
        if len(refs) < 2:
            snapshot = str(await specs["browser_snapshot"].handler())
            refs = REF_RE.findall(snapshot)
        if len(refs) >= 2:
            await specs["browser_type"].handler(ref=refs[0], text="YAI")
            await specs["browser_click"].handler(ref=refs[1])
            read = str(await specs["browser_read"].handler())
            mark("browser_write", "hello YAI" in read, f"refs={refs[:2]} | read: {read[:160]}")
        else:
            mark("browser_write", False, f"no refs in snapshot: {nav[:300]}")

        # D. 清理
        cleanup = str(
            await specs["shell"].handler(
                command="rm -f /workspace/gui-proof.txt /workspace/form.html && echo cleaned"
            )
        )
        mark("cleanup", "cleaned" in cleanup, cleanup)
    finally:
        await bridge.aclose()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Box MCP 电脑操作验收")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    _load_env_local()
    if not os.environ.get("BOX_TOKEN"):
        print("缺少 BOX_TOKEN（见 README）", file=sys.stderr)
        return 2
    report = asyncio.run(_run_checks())
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for name, check in report["checks"].items():
            print(f"{'PASS' if check['ok'] else 'FAIL'} {name}: {check['detail']}")
    return 0 if all(check["ok"] for check in report["checks"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
