"""每轮反思（工具失败后的自我修正）端到端测试，全部离线、不需要 API Key。"""

import asyncio

from yai_core import AgentCore, ModelResponse, ToolCallRequest, build_spec

NOTES = {"周报": ["周一完成 Core 骨架", "周三跑通工具调用"]}


def search_notes(keyword: str) -> list:
    """搜索宿主笔记库。"""
    return NOTES.get(keyword, [])


def test_failed_tool_injects_reflection_hint_and_model_recovers() -> None:
    """工具失败（含模型幻觉出不存在的工具）后，下一轮上下文带反思提示，模型可改用现有工具。"""

    class RecordingModel:
        def __init__(self) -> None:
            self.recorded: list[list[dict]] = []

        async def achat(self, messages, tools=None, *, tier="standard"):
            self.recorded.append(messages)
            n = len(self.recorded)
            if n == 1:
                # 第一轮误调一个不存在的工具 -> executor 失败
                return ModelResponse(
                    content="",
                    tool_calls=[ToolCallRequest(id="g1", name="ghost_tool", arguments={})],
                )
            if n == 2:
                # 看到反思提示后改用宿主真实工具
                return ModelResponse(
                    content="",
                    tool_calls=[
                        ToolCallRequest(
                            id="s1", name="search_notes", arguments={"keyword": "周报"}
                        )
                    ],
                )
            return ModelResponse(content="已改用现有工具完成。")

    model = RecordingModel()
    core = AgentCore(model)
    core.register_tools([build_spec(search_notes)])

    result = asyncio.run(core.run("搜索笔记里关于周报的内容"))

    # 第二轮（失败之后）的上下文里必须包含反思提示，并点名失败工具
    joined = "\n".join(
        m.get("content", "") for m in model.recorded[1] if m["role"] in ("user", "tool")
    )
    assert "系统反思提示" in joined
    assert "ghost_tool" in joined
    # 最终没有卡死，改用真实工具后正常收尾
    assert result.final_text == "已改用现有工具完成。"
    assert result.events[-1].type.value == "done"
