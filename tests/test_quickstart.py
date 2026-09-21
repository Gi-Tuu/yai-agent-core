"""README 零 Key 快速嵌入示例的可执行回归。

本测试即 README「30 秒嵌入」代码片段的真身：改 quickstart 时两边必须同步。
它用随包发布的零依赖 ScriptedModel，证明 `pip install yai-agent-core`（不装任何
第三方依赖、不需要 API Key）就能跑通"注册工具 → 自适应执行 → 拿到结果"。
"""

import asyncio

from yai_core import (
    AgentCore,
    ModelResponse,
    ScriptedModel,
    ToolCallRequest,
    build_spec,
)


def search_notes(keyword: str) -> list[str]:
    """搜索宿主笔记库。"""
    db = {"周报": ["周一完成 Core 骨架", "周三跑通工具调用"]}
    return db.get(keyword, [])


def test_quickstart_zero_key_runs_tool_loop() -> None:
    # 1) 离线确定性模型：第一轮要求调用工具，第二轮给出最终答复。
    model = ScriptedModel(
        [
            ModelResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(id="c1", name="search_notes",
                                    arguments={"keyword": "周报"})
                ],
            ),
            ModelResponse(content="本周周报包含 2 条记录：Core 骨架、工具调用。"),
        ]
    )

    # 2) 宿主只声明自己的业务函数，Core 内省生成工具规格并注册。
    core = AgentCore(model)
    core.register_tools([build_spec(search_notes)])

    # 3) 自适应执行，拿到可交付结果；全程事件可观测。
    result = asyncio.run(core.run("搜索笔记里关于周报的内容并总结"))

    event_types = [e.type.value for e in result.events]
    assert "tool_call" in event_types
    assert "tool_result" in event_types
    assert event_types[-1] == "done"
    assert "周报" in result.final_text
    assert model.calls == 2


def test_scripted_model_requires_responses() -> None:
    import pytest

    with pytest.raises(ValueError):
        ScriptedModel([])
