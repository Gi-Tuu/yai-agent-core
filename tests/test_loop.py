"""端到端测试：用脚本化假模型离线验证 react 循环，不需要 API Key。"""

import asyncio

from yai_core import AgentCore, ModelResponse, ToolCallRequest, build_spec


class ScriptedModel:
    """按预设顺序返回响应的假模型，用于离线测试。"""

    def __init__(self, responses: list[ModelResponse]) -> None:
        self._responses = responses
        self.calls = 0

    async def achat(self, messages, tools=None, *, tier="standard"):
        resp = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        return resp


NOTES = {"周报": ["周一完成 Core 骨架", "周三跑通工具调用"], "其他": ["买菜"]}


def search_notes(keyword: str) -> list:
    """搜索宿主笔记库。"""
    return NOTES.get(keyword, [])


def test_react_loop_calls_tool_and_finishes() -> None:
    model = ScriptedModel(
        [
            ModelResponse(
                content="",
                tool_calls=[ToolCallRequest(id="c1", name="search_notes",
                                            arguments={"keyword": "周报"})],
            ),
            ModelResponse(content="本周周报包含 2 条记录：Core 骨架、工具调用。"),
        ]
    )
    core = AgentCore(model)
    core.register_tools([build_spec(search_notes)])

    result = asyncio.run(core.run("搜索笔记里关于周报的内容并总结"))

    event_types = [e.type.value for e in result.events]
    assert "strategy_selected" in event_types
    assert "tool_call" in event_types
    assert "tool_result" in event_types
    assert event_types[-1] == "done"
    assert "周报" in result.final_text
    assert model.calls == 2


def test_direct_answer_without_tools() -> None:
    model = ScriptedModel([ModelResponse(content="你好，我是内嵌助手。")])
    core = AgentCore(model)
    result = asyncio.run(core.run("你好"))
    assert result.strategy.value == "direct"
    assert result.final_text.startswith("你好")
