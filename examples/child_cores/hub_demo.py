"""两个可安装子核 + 一个父核：本机 IPC 生态 demo（零 Key、离线）。

    python examples/child_cores/hub_demo.py

父核（hub）通过 `ManagedChild` 拉起两个子产品进程（notes / calc），各自暴露本机 IPC；
父核把它们声明的工具注册成自己的工具，模型依次调用两个子核完成任务。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if (_ROOT / "src" / "yai_core").is_dir():
    sys.path.insert(0, str(_ROOT / "src"))
if (_ROOT / "shell").is_dir():
    sys.path.insert(0, str(_ROOT))

from shell.federation import Federation, ManagedChild  # noqa: E402

from yai_core import (  # noqa: E402
    AgentCore,
    ModelResponse,
    ScriptedModel,
    ToolCallRequest,
)

HERE = Path(__file__).resolve().parent


def _child(script: Path, tmp: str, node_id: str) -> ManagedChild:
    return ManagedChild(
        [
            sys.executable,
            "-X",
            "utf8",
            str(script),
            "--discovery-dir",
            tmp,
            "--parent-id",
            "hub",
            "--node-id",
            node_id,
        ],
        discovery_dir=tmp,
        node_id=node_id,
        parent_id="hub",
        connect_timeout=25.0,
    )


async def drive(tmp: str) -> None:
    notes = _child(HERE / "notes_app.py", tmp, "notes")
    calc = _child(HERE / "calc_app.py", tmp, "calc")
    await notes.start()
    await calc.start()
    try:
        parent = AgentCore(
            ScriptedModel(
                [
                    ModelResponse(
                        content="",
                        tool_calls=[
                            ToolCallRequest(
                                id="c1",
                                name="notes__add_note",
                                arguments={"keyword": "周报", "text": "生态 demo 已跑通"},
                            )
                        ],
                    ),
                    ModelResponse(
                        content="",
                        tool_calls=[
                            ToolCallRequest(
                                id="c2",
                                name="calc__add",
                                arguments={"a": 12, "b": 30},
                            )
                        ],
                    ),
                    ModelResponse(content="已记录笔记并完成计算。"),
                ]
            )
        )
        federation = Federation(parent, node_id="hub")
        federation.register_child(notes.link, tools=["add_note", "search_notes"])
        federation.register_child(calc.link, tools=["add", "history"])
        print("父核工具：", [tool["name"] for tool in parent.list_tools()])

        result = await parent.run("记一条笔记，并计算 12+30")
        print("[完成]", result.final_text)

        notes_hit = await federation.invoke("notes", "search_notes", {"keyword": "周报"})
        print("[notes] 搜索：", notes_hit.get("result"))
        calc_history = await federation.invoke("calc", "history", {})
        print("[calc] 历史：", calc_history.get("result"))
        print("子核统计：", federation.stats())
    finally:
        await notes.stop()
        await calc.stop()


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(drive(tmp))


if __name__ == "__main__":
    main()
