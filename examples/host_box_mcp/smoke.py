"""Box by Simular MCP 真机冒烟：状态 → 唤醒 → shell → Chrome。

前置：

- `uv sync --extra mcp`
- 在 https://box.simular.ai/access 创建个人 key（`cb_...`），写入 `.env.local`：::

      BOX_TOKEN=cb_...

运行::

    uv run python examples/host_box_mcp/smoke.py
    uv run python examples/host_box_mcp/smoke.py --json

脚本只读环境变量 / 本地 `.env.local`，**不打印 token**；所有调用都走
`McpToolBridge`（stdio/HTTP 统一入口），证明 Box 只是一个普通 MCP Server。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from yai_core.integrations.mcp import McpServerConfig, McpToolBridge  # noqa: E402

BOX_URL = "https://mcp.box.simular.ai/mcp"


def _load_env_local() -> None:
    """把仓库根 `.env.local` 里的 BOX_TOKEN 读进进程环境（不覆盖已有值）。"""
    path = ROOT / ".env.local"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


async def run(*, wake: bool, json_out: bool) -> int:
    if not os.environ.get("BOX_TOKEN"):
        print(
            "缺少 BOX_TOKEN：在 https://box.simular.ai/access 创建个人 key 后，"
            "写入 .env.local（BOX_TOKEN=cb_...）或导出环境变量。",
            file=sys.stderr,
        )
        return 2

    bridge = McpToolBridge(
        McpServerConfig(
            alias="box",
            url=BOX_URL,
            token_env="BOX_TOKEN",
        )
    )
    specs = {spec.name: spec for spec in await bridge.connect()}
    result: dict[str, Any] = {"tools": len(specs)}
    try:
        status = json.loads(str(await specs["box_status"].handler()))
        result["box_id"] = status.get("box_id")
        result["state_before"] = status.get("state")
        result["image"] = status.get("latest_image")

        if wake:
            ensure = json.loads(str(await specs["box_ensure"].handler()))
            result["state_after"] = ensure.get("state")
            result["mcp_url"] = ensure.get("mcp_url")
        else:
            result["state_after"] = status.get("state")

        shell = await specs["shell"].handler(
            command="echo hello-from-box; uname -srm; pwd"
        )
        result["shell"] = str(shell).strip()

        nav = await specs["browser_navigate"].handler(url="https://example.com")
        result["browser"] = str(nav).splitlines()[0] if str(nav) else ""
    finally:
        await bridge.aclose()

    if json_out:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Box MCP tools : {result['tools']}")
        print(f"box_id        : {result['box_id']}")
        print(f"state         : {result['state_before']} -> {result['state_after']}")
        print(f"image         : {result['image']}")
        print("shell         :")
        for line in str(result["shell"]).splitlines():
            print(f"  {line}")
        print(f"browser       : {result['browser']}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Box by Simular MCP 真机冒烟")
    parser.add_argument(
        "--no-wake", action="store_true", help="只读状态，不唤醒/创建 box"
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()
    _load_env_local()
    return asyncio.run(run(wake=not args.no_wake, json_out=args.json))


if __name__ == "__main__":
    raise SystemExit(main())
