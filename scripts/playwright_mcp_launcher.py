"""MCP 浏览器启动器：自动拉起 CDP Chrome，失败时回退到 MCP 自管浏览器。

为什么需要它：Playwright MCP 只会在 initialize 时连接 ``--cdp-endpoint``；
如果 9222 没有监听，所有浏览器工具会直接 ECONNREFUSED，而且不会自动重试。
本启动器在把参数交给 ``npx @playwright/mcp`` 之前：

1. 检测 ``--cdp-endpoint``；
2. 端点不通时调用 ``scripts/chrome_cdp.py start`` 启动独立调试 Chrome；
3. 等待端点就绪；
4. 仍失败则移除 ``--cdp-endpoint``，让 MCP 使用自己的受管浏览器（保证工具可用）。

用法（由 bridge.json 的 initialize.mcp 调用）::

    python scripts/playwright_mcp_launcher.py \
      -y @playwright/mcp@0.0.84 --cdp-endpoint http://127.0.0.1:9222
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHROME_CDP = ROOT / "scripts" / "chrome_cdp.py"
WAIT_SECONDS = 15.0


def _healthy(endpoint: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(f"{endpoint}/json/version", timeout=timeout) as resp:
            return resp.status == 200 and bool(json.loads(resp.read().decode("utf-8")))
    except Exception:  # noqa: BLE001 - 端点不可用就是 false
        return False


def _ensure_cdp(endpoint: str) -> bool:
    if _healthy(endpoint):
        return True
    try:
        port = endpoint.rsplit(":", 1)[1].strip("/")
    except IndexError:
        return False
    if not Path(CHROME_CDP).is_file():
        return False
    try:
        subprocess.run(
            [sys.executable, str(CHROME_CDP), "start", "--port", port],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception:  # noqa: BLE001
        return False
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        if _healthy(endpoint):
            return True
        time.sleep(0.5)
    return False


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    endpoint = ""
    if "--cdp-endpoint" in args:
        index = args.index("--cdp-endpoint")
        if index + 1 < len(args):
            endpoint = args[index + 1]
    if endpoint and _ensure_cdp(endpoint):
        pass
    elif endpoint:
        # 自动启动失败：去掉 CDP 参数，让 MCP 自管浏览器，保证 browser_* 仍可用。
        print(
            f"YAI MCP launcher: CDP endpoint {endpoint} 不可用，回退到 MCP 自管浏览器",
            file=sys.stderr,
            flush=True,
        )
        index = args.index("--cdp-endpoint")
        del args[index : index + 2]
        if "--browser" not in args and "--headless" not in args:
            args.extend(["--browser", "chrome"])
    npx = shutil.which("npx") or shutil.which("npx.cmd")
    if not npx:
        print("YAI MCP launcher: 找不到 npx，请安装 Node.js", file=sys.stderr)
        return 127
    return subprocess.run([npx, *args], cwd=ROOT).returncode


if __name__ == "__main__":
    raise SystemExit(main())
