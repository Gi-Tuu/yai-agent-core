"""任务级预算硬上限与熔断的离线测试（ScriptedModel，零网络、零 API Key）。"""

import asyncio

import pytest

from yai_core import (
    AgentCore,
    Budget,
    BudgetExceeded,
    ModelResponse,
    TokenUsage,
    ToolCallRequest,
    build_spec,
)


class ScriptedModel:
    """按预设顺序返回响应的假模型（与 test_loop 同款，便于本文件自包含）。"""

    def __init__(self, responses: list[ModelResponse]) -> None:
        self._responses = responses
        self.calls = 0

    async def achat(self, messages, tools=None, *, tier="standard"):
        resp = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        return resp


def _events(result) -> list[str]:
    return [e.type.value for e in result.events]


def _done(result) -> dict:
    return result.events[-1].data


def test_no_budget_keeps_legacy_behavior_and_omits_snapshot() -> None:
    model = ScriptedModel(
        [
            ModelResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(id="c1", name="search_notes", arguments={"keyword": "周报"})
                ],
            ),
            ModelResponse(content="本周周报包含 2 条记录。"),
        ]
    )
    core = AgentCore(model)
    core.register_tools([build_spec(lambda keyword: ["a", "b"])])

    result = asyncio.run(core.run("搜索笔记里关于周报的内容并总结"))

    assert _events(result)[-1] == "done"
    assert "周报" in result.final_text
    # 未配置预算时 DONE 不附 budget 快照，也没有熔断标记。
    assert "budget" not in _done(result)
    assert _done(result).get("reason") != "budget_exceeded"


def test_max_model_calls_trips_before_second_call() -> None:
    model = ScriptedModel(
        [
            ModelResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(id="c1", name="search_notes", arguments={"keyword": "周报"})
                ],
            ),
            ModelResponse(content="不该被用到的收尾"),
        ]
    )
    core = AgentCore(model, budget=Budget(max_model_calls=1))
    core.register_tools([build_spec(lambda keyword: ["a", "b"])])

    result = asyncio.run(core.run("搜索笔记里关于周报的内容并总结"))

    types = _events(result)
    assert "budget_exceeded" in types
    ev = next(e for e in result.events if e.type.value == "budget_exceeded")
    assert ev.data["scope"] == "model_calls"
    assert ev.data["limit"] == 1
    # 第二次模型调用在发起前被拦：假模型实际只被调用 1 次。
    assert model.calls == 1
    assert ev.data["budget"]["model_calls"] == 1
    done = _done(result)
    assert done["reason"] == "budget_exceeded"
    assert "预算上限" in result.final_text


def test_max_tool_calls_zero_blocks_execution_without_side_effect() -> None:
    ran = {"n": 0}

    def search_notes(keyword: str) -> list:
        ran["n"] += 1
        return ["a"]

    model = ScriptedModel(
        [
            ModelResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(id="c1", name="search_notes", arguments={"keyword": "周报"})
                ],
            ),
            ModelResponse(content="不该被用到"),
        ]
    )
    core = AgentCore(model, budget=Budget(max_tool_calls=0))
    core.register_tools([build_spec(search_notes)])

    result = asyncio.run(core.run("搜索笔记里关于周报的内容并总结"))

    ev = next(e for e in result.events if e.type.value == "budget_exceeded")
    assert ev.data["scope"] == "tool_calls"
    # 工具在执行前被熔断：宿主函数一次都没被调用，无副作用。
    assert ran["n"] == 0
    assert ev.data["budget"]["tool_calls"] == 0


def test_max_tokens_trips_on_usage() -> None:
    model = ScriptedModel(
        [
            ModelResponse(
                content="你好，我是内嵌助手。",
                usage=TokenUsage(prompt_tokens=40, completion_tokens=60, total_tokens=100),
            )
        ]
    )
    core = AgentCore(model, budget=Budget(max_tokens=50))

    result = asyncio.run(core.run("你好"))

    ev = next(e for e in result.events if e.type.value == "budget_exceeded")
    assert ev.data["scope"] == "tokens"
    assert ev.data["used"] == 100
    assert ev.data["budget"]["total_tokens"] == 100
    assert _done(result)["reason"] == "budget_exceeded"


def test_direct_single_call_within_budget_succeeds() -> None:
    model = ScriptedModel(
        [
            ModelResponse(
                content="直接回答，无需工具。",
                usage=TokenUsage(prompt_tokens=5, completion_tokens=5, total_tokens=10),
            )
        ]
    )
    core = AgentCore(model, budget=Budget(max_model_calls=1, max_tokens=1000))

    result = asyncio.run(core.run("你好"))

    assert "budget_exceeded" not in _events(result)
    assert "直接回答" in result.final_text
    assert _done(result)["budget"]["model_calls"] == 1


def test_budget_snapshot_counts_tools_and_tokens() -> None:
    model = ScriptedModel(
        [
            ModelResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(id="c1", name="search_notes", arguments={"keyword": "周报"})
                ],
                usage=TokenUsage(prompt_tokens=20, completion_tokens=10, total_tokens=30),
            ),
            ModelResponse(
                content="总结完成。",
                usage=TokenUsage(prompt_tokens=40, completion_tokens=20, total_tokens=60),
            ),
        ]
    )
    def search_notes(keyword: str) -> list:
        """按关键词搜索笔记。"""
        return ["a", "b"]

    core = AgentCore(
        model, budget=Budget(max_model_calls=10, max_tool_calls=10, max_tokens=100000)
    )
    core.register_tools([build_spec(search_notes)])

    result = asyncio.run(core.run("搜索笔记里关于周报的内容并总结"))

    snap = _done(result)["budget"]
    assert snap["model_calls"] == 2
    assert snap["tool_calls"] == 1
    assert snap["total_tokens"] == 90
    assert snap["token_usage_seen"] is True
    assert snap["limits"]["max_tokens"] == 100000


def test_budget_validation_rejects_negative_and_bool() -> None:
    with pytest.raises(ValueError):
        Budget(max_model_calls=-1)
    with pytest.raises(ValueError):
        Budget(max_tool_calls=-2)
    with pytest.raises(ValueError):
        Budget(max_tokens=True)  # type: ignore[arg-type]
    # 0 是合法上限（表示一次都不允许），不应被拒。
    assert Budget(max_tool_calls=0).enabled is True


def test_budget_enabled_flag() -> None:
    assert Budget().enabled is False
    assert Budget(max_model_calls=1).enabled is True
    assert Budget(max_tool_calls=0).enabled is True
    assert Budget(max_tokens=0).enabled is True


def test_tracker_tool_boundary_and_missing_usage() -> None:
    tracker = Budget(max_tool_calls=1).start()
    tracker.before_tool_call()  # 第 1 次放行
    tracker.record_tool_call()
    with pytest.raises(BudgetExceeded) as exc:
        tracker.before_tool_call()  # 第 2 次熔断
    assert exc.value.scope == "tool_calls"
    assert tracker.exceeded is not None

    # 后端不返回 usage：token 不累计、不熔断，token_usage_seen 保持 False。
    t2 = Budget(max_tokens=1).start()
    t2.before_model_call()
    t2.record_model_call(ModelResponse(content="无用量"))
    assert t2.token_usage_seen is False
    assert t2.total_tokens == 0
    assert t2.exceeded is None


def test_tracker_model_call_boundary() -> None:
    tracker = Budget(max_model_calls=1).start()
    tracker.before_model_call()
    tracker.record_model_call(ModelResponse(content="ok"))
    with pytest.raises(BudgetExceeded) as exc:
        tracker.before_model_call()
    assert exc.value.scope == "model_calls"
    assert tracker.model_calls == 1
