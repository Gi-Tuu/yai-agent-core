"""结构化长期记忆 remember/recall 的端到端离线测试（ScriptedModel，无 API Key）。"""

import asyncio
from pathlib import Path

from yai_core import AgentCore, ModelResponse, ScriptedModel, ToolCallRequest
from yai_core.types import EventType


def _build_model() -> ScriptedModel:
    return ScriptedModel(
        [
            ModelResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="c1",
                        name="remember",
                        arguments={
                            "content": "用户对芒果严重过敏",
                            "kind": "fact",
                            "tags": ["过敏", "饮食"],
                            "importance": 5,
                        },
                    )
                ],
            ),
            ModelResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="c2",
                        name="recall",
                        arguments={"query": "芒果过敏", "k": 3},
                    )
                ],
            ),
            ModelResponse(content="已记住：你对芒果严重过敏，我会避免相关食物。"),
        ]
    )


def test_long_memory_tools_run_and_persist(tmp_path: Path) -> None:
    storage = tmp_path / "memory.json"
    core = AgentCore(
        _build_model(), long_memory=True, memory_storage=storage
    )

    # 任务命中"记录"动作进入 react（不用"然后"，避免被判 PLAN 而由规划步消耗脚本响应）。
    result = asyncio.run(core.run("请记录我对芒果过敏"))

    tool_calls = [
        event.data["tool"]
        for event in result.events
        if event.type == EventType.TOOL_CALL
    ]
    assert "remember" in tool_calls
    assert "recall" in tool_calls
    assert result.events[-1].type == EventType.DONE
    assert "芒果" in result.final_text

    # 记忆已落盘且 vault 状态可观测。
    assert storage.exists()
    status = core.long_memory_status()
    assert status["enabled"] is True
    assert status["total"] == 1
    assert status["by_kind"]["fact"] == 1


def test_long_memory_survives_core_restart(tmp_path: Path) -> None:
    storage = tmp_path / "memory.json"
    first = AgentCore(_build_model(), long_memory=True, memory_storage=storage)
    asyncio.run(first.run("请记录我对芒果过敏"))

    # 新 core 复用同一存储：历史记忆仍可回忆。
    second = AgentCore(_build_model(), long_memory=True, memory_storage=storage)
    status = second.long_memory_status()
    assert status["enabled"] is True
    assert status["total"] == 1


def test_long_memory_disabled_by_default() -> None:
    core = AgentCore(_build_model())
    assert core.long_memory_status()["enabled"] is False
    assert "remember" not in {s.name for s in core.registry.all()}
