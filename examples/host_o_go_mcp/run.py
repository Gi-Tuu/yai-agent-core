"""Go stdio MCP Server 的 Python 宿主示例。

前置：Go 1.22+。没有 Go 时脚本给出安装指引并以退出码 2 结束。
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from yai_core import AgentCore, ModelResponse, ScriptedModel, ToolCallRequest  # noqa: E402
from yai_core.integrations.mcp import McpServerConfig, McpToolBridge  # noqa: E402

GO_INSTALL = "https://go.dev/dl/"


def find_go() -> str | None:
    return shutil.which("go")


async def main() -> int:
    go = find_go()
    if go is None:
        print(
            "未找到 go 命令。这个示例需要 Go 1.22+；安装指引：" + GO_INSTALL + "\n"
            "不装 Go 不影响核心库使用，这只是可选的多语言示例。",
            file=sys.stderr,
        )
        return 2

    bridge = McpToolBridge(
        McpServerConfig(
            alias="go_demo",
            command=go,
            args=["-C", str(ROOT / "examples" / "host_o_go_mcp"), "run", "."],
        )
    )
    specs = await bridge.connect()
    try:
        print("tools:", [spec.name for spec in specs])
        model = ScriptedModel(
            [
                ModelResponse(
                    content="",
                    tool_calls=[
                        ToolCallRequest(
                            id="c1",
                            name="lookup_order",
                            arguments={"order_id": "SO-1001"},
                        )
                    ],
                ),
                ModelResponse(content="Go MCP 订单查询完成。"),
            ]
        )
        core = AgentCore(model)
        core.register_tools(specs)
        result = await core.run("查一下订单 SO-1001")
        print("final:", result.final_text)
        return 0
    finally:
        await bridge.aclose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
