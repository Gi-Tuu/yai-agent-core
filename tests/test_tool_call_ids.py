"""工具调用 id 的协议防护：空 id 合成、重复 id 失败关闭。"""

from __future__ import annotations

import asyncio

from yai_core import AgentCore, ModelResponse, ScriptedModel, ToolCallRequest, ToolSpec


def _core(ids: list[str]) -> tuple[AgentCore, list[dict]]:
    calls: list[dict] = []

    async def handler(**kwargs):
        calls.append(kwargs)
        return "ok"

    tool = ToolSpec(
        name="ping",
        description="ping",
        input_schema={"type": "object"},
        handler=handler,
    )
    model = ScriptedModel(
        [
            ModelResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(id=call_id, name="ping", arguments={})
                    for call_id in ids
                ],
            ),
            ModelResponse(content="done"),
        ]
    )
    core = AgentCore(model)
    core.register_tools([tool])
    return core, calls


def test_duplicate_tool_call_id_executes_nothing() -> None:
    core, calls = _core(["call_1", "call_1"])
    result = asyncio.run(core.run("go"))

    assert result.status == "error"
    assert calls == []
    assert any("重复" in str(event.data) for event in result.events)


def test_blank_tool_call_ids_are_synthesized_unique() -> None:
    core, calls = _core(["", ""])
    result = asyncio.run(core.run("go"))

    assert result.status == "ok"
    assert len(calls) == 2
    tool_messages = [
        event.data
        for event in result.events
        if event.type.value == "tool_result"
    ]
    assert len(tool_messages) == 2
