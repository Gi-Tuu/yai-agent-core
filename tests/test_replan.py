"""动态重规划 re-plan 端到端离线测试。

验证：多步计划执行中模型发现原计划走不通 -> 调用 replan meta-tool ->
内核产出修正计划、发 plan_revised 事件 -> 模型按新计划继续执行工具 -> 收尾，
并核对成本·档位事件与 DONE 的调用计数。全程离线、不需要 API Key。
"""

import asyncio
from copy import deepcopy
from typing import Any

import pytest

from yai_core import (
    AgentCore,
    Budget,
    ModelResponse,
    ToolCallRequest,
    build_spec,
)


class ScriptedModel:
    """按预设顺序返回响应的假模型；超长按最后一条重复。"""

    def __init__(self, responses: list[ModelResponse]) -> None:
        self._responses = responses
        self.calls = 0
        self.requests: list[list[dict[str, Any]]] = []

    async def achat(
        self, messages: list[dict[str, Any]], tools: Any = None, *, tier: str = "standard"
    ) -> ModelResponse:
        pending: set[str] = set()
        for message in messages:
            if message["role"] == "tool":
                assert message["tool_call_id"] in pending, "孤立或重复的工具结果"
                pending.remove(message["tool_call_id"])
            else:
                assert not pending, "工具结果返回前插入了普通消息"
                calls = message.get("tool_calls", [])
                pending = {call["id"] for call in calls}
                assert len(pending) == len(calls), "重复的工具调用 ID"
        assert not pending, "存在尚未返回结果的工具调用"
        self.requests.append(deepcopy(messages))
        resp = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        return resp


NOTES = {"项目": ["版本 0.22.1 已发布"]}


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
                content="1. 搜索项目笔记\n2. 整理结果\n3. 汇报"
            ),
            ModelResponse(  # 4 react 第1轮：按新计划调搜索
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="s1",
                        name="search_notes",
                        arguments={"keyword": "项目"},
                    )
                ],
            ),
            ModelResponse(content="已找到项目笔记并完成汇报。"),        # 5 react 第2轮收尾
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
    assert data["steps"] == ["搜索项目笔记", "整理结果", "汇报"]

    # 重规划后工具仍真正执行
    assert "tool_call" in event_types
    assert "tool_result" in event_types
    assert "项目" in result.final_text

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
                        id="s1", name="search_notes", arguments={"keyword": "项目"}
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
                        id="s1", name="search_notes", arguments={"keyword": "项目"}
                    )
                ],
            ),
            ModelResponse(content="完成。"),                              # 2 收尾
        ]
    )
    core = AgentCore(model, cost_aware=False)
    core.register_tools([build_spec(search_notes)])

    result = asyncio.run(core.run("搜索笔记里的项目内容"))

    assert result.strategy.value == "react"
    tier_sequence = [
        e.data["tier"] for e in result.events if e.type.value == "tier_changed"
    ]
    assert "strong" not in tier_sequence  # 只有起始 standard，不动态升档
    done = result.events[-1].data
    assert done["strong_calls"] == 0
    assert done["model_calls"] == 2


@pytest.mark.parametrize("content", ["", "根据已有观察调整计划"])
@pytest.mark.parametrize(
    "order",
    [
        ("r1",),
        ("r1", "b1", "b2"),
        ("b1", "r1", "b2"),
        ("b1", "b2", "r1"),
        ("r1", "b1", "r2", "b2"),
        ("r1", "r2"),
    ],
)
def test_replan_preserves_batch_protocol_and_execution_order(
    order: tuple[str, ...], content: str
) -> None:
    executed: list[str] = []
    observed_execution: list[list[str]] = []

    def record(value: str) -> str:
        executed.append(value)
        return "已执行:" + value

    revisions = [item for item in order if item.startswith("r")]
    batch = [
        ToolCallRequest(
            id=item,
            name="replan" if item.startswith("r") else "record",
            arguments={"reason": item} if item.startswith("r") else {"value": item},
        )
        for item in order
    ]

    class RecordingModel(ScriptedModel):
        async def achat(
            self, messages: list[dict[str, Any]], tools: Any = None,
            *, tier: str = "standard",
        ) -> ModelResponse:
            observed_execution.append(list(executed))
            return await super().achat(messages, tools, tier=tier)

    model = RecordingModel(
        [
            ModelResponse(content="1. 收集记录\n2. 汇报"),
            ModelResponse(tool_calls=[ToolCallRequest(
                id="history", name="record", arguments={"value": "history"},
            )]),
            ModelResponse(content=content, tool_calls=batch),
            *[
                ModelResponse(content=f"1. 继续执行{i}\n2. 汇报{i}")
                for i in range(1, len(revisions) + 1)
            ],
            ModelResponse(content="完成。"),
        ]
    )
    core = AgentCore(model)
    core.register_tools([build_spec(record)])
    result = asyncio.run(core.run("先收集记录，然后汇报"))

    assert executed == ["history", *[item for item in order if item.startswith("b")]]
    for index, replan_id in enumerate(revisions):
        completed = list(order[:order.index(replan_id)])
        request = model.requests[3 + index]
        replies = [message for message in request if message["role"] == "tool"]
        assert [message["tool_call_id"] for message in replies] == ["history", *completed]
        assert "已执行:history" in replies[0]["content"]
        assert observed_execution[3 + index] == [
            "history", *[item for item in completed if item.startswith("b")],
        ]
        call_ids = [
            call["id"] for message in request for call in message.get("tool_calls", [])
        ]
        assert call_ids == ["history", *completed]
        if content:
            assert any(message.get("content") == content for message in request)
        assert request[-1]["role"] == "user"
        assert f"因“{replan_id}”" in request[-1]["content"]
        for message in replies:
            if message["tool_call_id"].startswith("r"):
                assert "1. 继续执行1\n2. 汇报1" in message["content"]

    final = model.requests[-1]
    assert [
        call["id"] for message in final for call in message.get("tool_calls", [])
    ] == ["history", *order]
    assert [
        message["tool_call_id"] for message in final if message["role"] == "tool"
    ] == ["history", *order]
    for revision, replan_id in enumerate(revisions, 1):
        reply = next(message for message in final if message.get("tool_call_id") == replan_id)
        assert f"1. 继续执行{revision}\n2. 汇报{revision}" in reply["content"]
    assert not any("【系统重规划】" in message.get("content", "") for message in final)
    revised = [event for event in result.events if event.type.value == "plan_revised"]
    assert [event.data["revision"] for event in revised] == list(range(1, len(revisions) + 1))
    assert result.events[-1].data["model_calls"] == 4 + len(revisions)
    assert result.events[-1].data["strong_calls"] == 1 + len(revisions)


def test_replan_successive_rounds_keep_previous_plan() -> None:
    model = ScriptedModel([
        ModelResponse(content="1. 搜索\n2. 汇报"),
        ModelResponse(tool_calls=[ToolCallRequest(id="r1", name="replan", arguments={})]),
        ModelResponse(content="1. 第一次调整\n2. 汇报"),
        ModelResponse(tool_calls=[ToolCallRequest(id="r2", name="replan", arguments={})]),
        ModelResponse(content="1. 第二次调整\n2. 汇报"),
        ModelResponse(content="完成。"),
    ])
    core = AgentCore(model)
    core.register_tools([build_spec(search_notes)])
    result = asyncio.run(core.run("先搜索笔记，然后汇报"))

    previous = next(message for message in model.requests[4] if message.get("tool_call_id") == "r1")
    assert "1. 第一次调整\n2. 汇报" in previous["content"]
    revisions = [
        event.data["revision"] for event in result.events if event.type.value == "plan_revised"
    ]
    assert revisions == [1, 2]
    assert result.events[-1].data["model_calls"] == 6


@pytest.mark.parametrize("failure", ["budget", "error", "timeout", "cancel"])
def test_replan_failure_does_not_run_remaining_tools(failure: str) -> None:
    executed: list[str] = []
    cancelled: list[bool] = []

    def record() -> str:
        executed.append("ran")
        return "不应执行"

    class FailingModel(ScriptedModel):
        async def achat(
            self, messages: list[dict[str, Any]], tools: Any = None,
            *, tier: str = "standard",
        ) -> ModelResponse:
            replanning = self.calls == 2
            response = await super().achat(messages, tools, tier=tier)
            if replanning:
                if failure == "error":
                    raise RuntimeError("重规划模型失败")
                if failure == "cancel":
                    raise asyncio.CancelledError
                if failure == "timeout":
                    try:
                        await asyncio.Future()
                    finally:
                        cancelled.append(True)
            return response

    model = FailingModel([
        ModelResponse(content="1. 收集\n2. 汇报"),
        ModelResponse(tool_calls=[
            ToolCallRequest(id="r1", name="replan", arguments={}),
            ToolCallRequest(id="b1", name="record", arguments={}),
        ]),
        ModelResponse(content="1. 继续\n2. 汇报"),
    ])
    core = AgentCore(
        model,
        budget=Budget(max_model_calls=2) if failure == "budget" else None,
        model_timeout=0.05 if failure == "timeout" else None,
    )
    core.register_tools([build_spec(record)])
    events = []

    async def run() -> None:
        async for event in core.astream("先收集记录，然后汇报"):
            events.append(event)

    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(run())
    else:
        asyncio.run(run())
        expected = "budget_exceeded" if failure == "budget" else "error"
        assert any(event.type.value == expected for event in events)
        assert events[-1].type.value == "done"
    assert not executed
    assert not any(event.type.value == "plan_revised" for event in events)
    assert model.calls == (2 if failure == "budget" else 3)
    if failure == "timeout":
        assert cancelled == [True]


def test_replan_refreshes_transient_host_state() -> None:
    states: list[str] = []

    def provider() -> str:
        state = f"host-snapshot-{len(states) + 1}"
        states.append(state)
        return state

    model = ScriptedModel([
        ModelResponse(content="1. 搜索\n2. 汇报"),
        ModelResponse(tool_calls=[ToolCallRequest(id="r1", name="replan", arguments={})]),
        ModelResponse(content="1. 继续搜索\n2. 汇报"),
        ModelResponse(content="完成。"),
    ])
    core = AgentCore(model)
    core.register_tools([build_spec(search_notes)])
    core.register_state_provider("test-state", provider)
    result = asyncio.run(core.run("先搜索笔记，然后汇报"))

    assert len(model.requests) == len(states) == 4
    for request, state in zip(model.requests, states, strict=True):
        assert request[1]["role"] == "system"
        assert state in request[1]["content"]
        assert sum("host-snapshot-" in message.get("content", "") for message in request) == 1
    assert len([event for event in result.events if event.type.value == "host_state"]) == 4
    assert all("host-snapshot-" not in message.content for message in core.memory.history())
