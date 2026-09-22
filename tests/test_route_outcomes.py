"""extract_route_outcome 路由反馈抽取测试（纯函数、离线）。"""

import pytest

from yai_core.learning import RouteOutcome, extract_route_outcome
from yai_core.types import AgentEvent, EventType, Strategy


def ev(t: EventType, **data) -> AgentEvent:
    return AgentEvent(t, data)


def _ok_tool(tool: str, result: str = "{}") -> list[AgentEvent]:
    return [
        ev(EventType.TOOL_CALL, tool=tool, arguments={}),
        ev(EventType.TOOL_RESULT, tool=tool, ok=True, preview=result),
    ]


def _fail_tool(tool: str) -> list[AgentEvent]:
    return [
        ev(EventType.TOOL_CALL, tool=tool, arguments={}),
        ev(EventType.TOOL_RESULT, tool=tool, ok=False, error="执行出错"),
    ]


def _done(strategy: Strategy, text: str) -> list[AgentEvent]:
    return [
        ev(EventType.MODEL_MESSAGE, text=text),
        ev(EventType.DONE, strategy=strategy.value, final_text=text),
    ]


# ---------- 成功 / 失败 ----------

def test_successful_react() -> None:
    events = _ok_tool("search_notes") + _done(Strategy.REACT, "找到 3 条笔记")
    out = extract_route_outcome(events, Strategy.REACT)
    assert out.success is True
    assert out.tool_calls == 1
    assert out.tool_failures == 0
    assert out.aborted is False
    assert out.reward > 0.9


def test_tool_failure_is_unsuccessful() -> None:
    events = _fail_tool("search_notes") + _done(Strategy.REACT, "工具失败")
    out = extract_route_outcome(events, Strategy.REACT)
    assert out.success is False
    assert out.tool_failures == 1
    assert out.reward == pytest.approx(0.0, abs=1e-9)


def test_error_event_is_unsuccessful() -> None:
    events = [ev(EventType.ERROR, error="模型超时")] + _done(Strategy.REACT, "出错了")
    out = extract_route_outcome(events, Strategy.REACT)
    assert out.success is False and out.errors == 1


def test_empty_final_text_is_unsuccessful() -> None:
    events = [ev(EventType.DONE, strategy=Strategy.REACT.value, final_text="")]
    out = extract_route_outcome(events, Strategy.REACT)
    assert out.success is False and out.final_empty is True


# ---------- direct 误判（无能话术） ----------

@pytest.mark.parametrize(
    "text",
    ["我无法访问你的数据", "抱歉，我没有这个数据", "I can't access that"],
)
def test_direct_unable_hint_is_unsuccessful(text: str) -> None:
    out = extract_route_outcome(_done(Strategy.DIRECT, text), Strategy.DIRECT)
    assert out.unable_hint is True and out.success is False


def test_direct_normal_chat_is_successful() -> None:
    out = extract_route_outcome(_done(Strategy.DIRECT, "你好呀，今天过得怎么样？"), Strategy.DIRECT)
    assert out.unable_hint is False and out.success is True


def test_unable_hint_after_react_with_ok_tool_still_success() -> None:
    # react 已成功取到工具结果，最终文案哪怕带"无法"也不该由 unable_hint 判死
    # （工具失败已由 tool_failures 覆盖；这里工具 ok=True）。
    events = _ok_tool("search_notes") + _done(Strategy.REACT, "无法确认更多，但已找到笔记")
    out = extract_route_outcome(events, Strategy.REACT)
    # chosen 是 react，unable_hint 不参与 success 判定
    assert out.success is True


# ---------- 澄清 ----------

def test_clarify_abort() -> None:
    events = [
        ev(EventType.CLARIFY_REQUESTED, question="想查什么？", round=1),
        ev(EventType.DONE, strategy=Strategy.CLARIFY.value, final_text="任务信息不足，已停止。"),
    ]
    out = extract_route_outcome(events, Strategy.CLARIFY)
    assert out.aborted is True and out.clarify_rounds == 1 and out.success is False


def test_clarify_then_resolved_not_aborted() -> None:
    # 澄清后递归执行，最终 DONE 是真实策略 react，而非 clarify。
    events = (
        [ev(EventType.CLARIFY_REQUESTED, question="查什么？", round=1)]
        + _ok_tool("search_notes")
        + _done(Strategy.REACT, "找到了")
    )
    out = extract_route_outcome(events, Strategy.REACT)
    assert out.aborted is False and out.clarify_rounds == 1 and out.success is True


# ---------- 计划 / 成本 ----------

def test_plan_cost_lowers_reward_vs_react() -> None:
    tool_events = _ok_tool("a") + _ok_tool("b") + _ok_tool("c")
    plan_events = (
        [ev(EventType.PLAN_CREATED, steps=[{}, {}, {}])]
        + tool_events
        + _done(Strategy.PLAN, "已完成")
    )
    react_events = tool_events + _done(Strategy.REACT, "已完成")
    plan = extract_route_outcome(plan_events, Strategy.PLAN)
    react = extract_route_outcome(react_events, Strategy.REACT)
    assert plan.plan_steps == 3
    assert plan.reward < react.reward  # 同等结果下，计划开销更大


def test_hit_max_iters() -> None:
    events: list[AgentEvent] = []
    for _ in range(6):
        events += _ok_tool("search_notes")
    events += _done(Strategy.REACT, "结果")
    out = extract_route_outcome(events, Strategy.REACT, max_iters=6)
    assert out.hit_max_iters is True and out.success is False


# ---------- 奖励区间 & 序列化 ----------

def test_reward_always_in_unit_interval() -> None:
    cases = [
        _done(Strategy.DIRECT, "你好"),
        _fail_tool("a") + _done(Strategy.REACT, "失败"),
        [ev(EventType.PLAN_CREATED, steps=[1, 2])]
        + _ok_tool("a")
        + _ok_tool("b")
        + _done(Strategy.PLAN, "好"),
        [ev(EventType.CLARIFY_REQUESTED, question="?", round=1)] * 4
        + _done(Strategy.REACT, "好"),
    ]
    for events in cases:
        out = extract_route_outcome(events, Strategy.REACT)
        assert 0.0 <= out.reward <= 1.0


def test_outcome_to_dict_roundtrip_shape() -> None:
    out = extract_route_outcome(
        _fail_tool("a") + _done(Strategy.REACT, "失败"), Strategy.REACT
    )
    d = out.to_dict()
    assert d["tool_failures"] == 1 and d["success"] is False
    assert RouteOutcome(**d).reward == out.reward
