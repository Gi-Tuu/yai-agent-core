"""A2A host adapter demo: remote A2A agent -> ToolSpec -> AgentCore.

Run::

    uv run python examples/host_a2a_adapter/demo.py

Starts an in-process fake A2A server, wraps it as an ordinary tool, and lets a ScriptedModel
call it through the kernel. No network access, no key, no kernel change.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

from examples.host_a2a_adapter.a2a_client import A2AClient, A2AConfig  # noqa: E402
from examples.host_a2a_adapter.fake_a2a_server import serve  # noqa: E402

from yai_core import AgentCore, ModelResponse, ScriptedModel, ToolCallRequest  # noqa: E402


async def main() -> None:
    server, base_url = serve()
    try:
        client = A2AClient(A2AConfig(base_url=base_url, poll_interval=0.05, max_wait=5))
        card = client.fetch_card()
        tool = client.as_tool(name="a2a_echo")
        print(f"agent card: {card.name} ({card.version}) -> {card.url}")

        model = ScriptedModel(
            [
                ModelResponse(
                    content="",
                    tool_calls=[
                        ToolCallRequest(
                            id="c1", name="a2a_echo", arguments={"task": "hello from YAI"}
                        )
                    ],
                ),
                ModelResponse(content="A2A delegate finished."),
            ]
        )
        core = AgentCore(model)
        core.register_tools([tool])
        result = await core.run("delegate a hello to the remote A2A agent")
        print(f"tool result: {await tool.handler(task='hello from YAI')}")
        print(f"final: {result.final_text}")
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    asyncio.run(main())
