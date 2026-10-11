"""动态重规划 re-plan 端到端离线测试。

验证：多步计划执行中模型发现原计划走不通 -> 调用 replan meta-tool ->
内核产出修正计划、发 plan_revised 事件 -> 模型按新计划继续执行工具 -> 收尾，
并核对成本·档位事件与 DONE 的调用计数。全程离线、不需要 API Key。
"""

import asyncio
from typing import Any

from yai_core import (
    AgentCore,
    ModelResponse,
    ToolCallRequest,
    build_spec,
)


class ScriptedModel:
    """按预设顺序返回响应的假模型；超长按最后一条重复。"""

    def __init__(self, responses: list[ModelResponse]) -> None:
        self._responses = responses
        self.calls = 0

    async def achat(
        self, messages: list[dict[str, Any]], tools: Any = None, *, tier: str = "standard"
    ) -> ModelResponse:
        resp = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        return resp


NOTES = {"比赛": ["X-Agent 开发窗口 0919 关闭"]}


def search_notes(keyword: str) -> list:
    """搜索宿主笔记库。"""
    return NOTES.get(keyword, [])


def test_replan_emits_revised_plan_and_continues() -> None:
    """计划走不通时显式重规划：发修订事件、保留步骤、按新计划完成、计数正确。"""
    model = ScriptedModel(
        [
            ModelResponse(content="1. 搜索笔记\n2. 汇报"),                # 1 规划(strong)
            ModelResponse(  # 2 react 第0轮：发现原计划漏前置，请求重规划
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="r1",
                        name="replan",
                        arguments={"reason": "原计划漏了先确认关键词"},
                    )
                ],
            ),
            ModelResponse(  # 3 重规划产出修正计划(strong)
                content="1. 搜索比赛笔记\n2. 整理结果\n3. 汇报"
            ),
            ModelResponse(  # 4 react 第1轮：按新计划调搜索
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="s1",
                        name="search_notes",
                        arguments={"keyword": "比赛"},
                    )
                ],
            ),
            ModelResponse(content="已找到比赛笔记并完成汇报。"),        # 5 react 第2轮收尾
        ]
    )
    core = AgentCore(model)
    core.register_tools([build_spec(search_notes)])

    result = asyncio.run(core.run("先搜索笔记，然后汇报"))

    event_types = [e.type.value for e in result.events]
    assert result.strategy.value == "plan"

    # plan_revised 事件：修订序号、原因、解析出的新步骤
    revised = [e for e in result.events if e.type.value == "plan_revised"]
    assert len(revised) == 1
    data = revised[0].data
    assert data["revision"] == 1
    assert data["reason"] == "原计划漏了先确认关键词"
    assert data["steps"] == ["搜索比赛笔记", "整理结果", "汇报"]

    # 重规划后工具仍真正执行
    assert "tool_call" in event_types
    assert "tool_result" in event_types
    assert "比赛" in result.final_text

    # 档位事件：只有起始一次 standard（重规划 achat 不经过档位变化检测）
    tier_sequence = [
        e.data["tier"] for e in result.events if e.type.value == "tier_changed"
    ]
    assert tier_sequence == ["standard"]

    # DONE 成本汇总：规划 strong + 重规划 strong = 2；执行 3 轮 standard
    done = result.events[-1].data
    assert done["model_calls"] == 5
    assert done["strong_calls"] == 2
    assert done["standard_calls"] == 3


def test_replan_without_reason_uses_default() -> None:
    """replan 未带 reason 时用默认理由，且解析不出步骤时回退为原任务，不崩溃。"""
    model = ScriptedModel(
        [
            ModelResponse(content="1. 搜索笔记\n2. 汇报"),                # 1 规划
            ModelResponse(  # 2 请求重规划（不带 reason）
                content="",
                tool_calls=[ToolCallRequest(id="r1", name="replan", arguments={})],
            ),
            ModelResponse(content="想不出编号步骤"),                      # 3 修正计划无法解析
            ModelResponse(  # 4 直接调搜索
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="s1", name="search_notes", arguments={"keyword": "比赛"}
                    )
                ],
            ),
            ModelResponse(content="完成。"),                              # 5 收尾
        ]
    )
    core = AgentCore(model)
    core.register_tools([build_spec(search_notes)])

    result = asyncio.run(core.run("先搜索笔记，然后汇报"))

    revised = next(e for e in result.events if e.type.value == "plan_revised")
    assert revised.data["reason"] == "执行中发现原计划需要调整"
    # 解析不出编号步骤 -> steps 回退为原始任务，保证事件始终携带可执行内容
    assert revised.data["steps"] == ["先搜索笔记，然后汇报"]


def test_cost_aware_disabled_keeps_standard() -> None:
    """cost_aware=False 回归：不接入 TierPolicy，简单 react 全程 standard。"""
    model = ScriptedModel(
        [
            ModelResponse(  # 1 react 第0轮：调搜索
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="s1", name="search_notes", arguments={"keyword": "比赛"}
                    )
                ],
            ),
            ModelResponse(content="完成。"),                              # 2 收尾
        ]
    )
    core = AgentCore(model, cost_aware=False)
    core.register_tools([build_spec(search_notes)])

    result = asyncio.run(core.run("搜索笔记里的比赛内容"))

    assert result.strategy.value == "react"
    tier_sequence = [
        e.data["tier"] for e in result.events if e.type.value == "tier_changed"
    ]
    assert "strong" not in tier_sequence  # 只有起始 standard，不动态升档
    done = result.events[-1].data
    assert done["strong_calls"] == 0
    assert done["model_calls"] == 2
