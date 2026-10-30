"""端到端评测包（yai_core.evaluation）的单元测试与一次最小集成。

分两部分：
1. 评分纯函数：用合成事件构造各种轨迹（通过 / 漏调 / 多调 / 越权 / 无支撑 /
   漏交付 / 负面任务），验证评分器客观判定；
2. 最小集成：用 ScriptedModel 真跑一次 AgentCore.astream，验证 runner 链路
   （事件 → trace → 评分）在真实 Core 上成立。
全部离线、无网络、无 API Key。
"""

from __future__ import annotations

import asyncio

import pytest

from yai_core import AgentCore
from yai_core.discovery import build_spec
from yai_core.evaluation import (
    GoldenTask,
    aggregate,
    collect_trace,
    score_task,
)
from yai_core.llm.scripted import ScriptedModel
from yai_core.types import (
    AgentEvent,
    EventType,
    ModelResponse,
    ToolCallRequest,
)

# ---------------------------------------------------------------------------
# 合成事件辅助
# ---------------------------------------------------------------------------


def call(name: str, arguments: dict | None = None) -> AgentEvent:
    return AgentEvent(EventType.TOOL_CALL,
                      {"tool": name, "arguments": arguments or {}})


def ok(name: str, preview: str) -> AgentEvent:
    return AgentEvent(EventType.TOOL_RESULT,
                      {"tool": name, "ok": True, "preview": preview})


def fail(name: str, error: str = "boom") -> AgentEvent:
    return AgentEvent(EventType.TOOL_RESULT,
                      {"tool": name, "ok": False, "error": error})


def msg(text: str) -> AgentEvent:
    return AgentEvent(EventType.MODEL_MESSAGE, {"text": text})


def done(strategy: str, final: str) -> AgentEvent:
    return AgentEvent(EventType.DONE,
                      {"strategy": strategy, "final_text": final})


def error_event() -> AgentEvent:
    return AgentEvent(EventType.ERROR, {"error": "X: y"})


def _golden(**kw) -> GoldenTask:
    base = dict(id="g", task="t", expected_tools=[], must_call=[],
                forbidden_tools=[], expected_evidence=[])
    base.update(kw)
    return GoldenTask(**base)


# ---------------------------------------------------------------------------
# collect_trace
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("final_text", ["", "已完成"])
def test_schema_focus_is_not_model_answer(final_text) -> None:
    original = [msg(final_text), done("react", final_text)]
    process = AgentEvent(
        EventType.MODEL_MESSAGE,
        {"kind": "schema_focus", "text": "工具范围选择：已聚焦。"},
    )
    assert collect_trace([process, *original]) == collect_trace(original)
    assert collect_trace([process]).model_texts == []


def test_collect_trace_fields() -> None:
    events = [
        call("a", {"x": 1}),
        ok("a", '{"x": 1}'),
        call("b"),
        fail("b"),
        msg("中间文本"),
        error_event(),
        done("react", "最终文本"),
    ]
    trace = collect_trace(events)
    assert trace.tool_calls == ["a", "b"]
    assert trace.tool_results == {"a": ['{"x": 1}']}
    assert trace.failed_tools == ["b"]
    assert trace.model_texts == ["中间文本"]
    assert trace.final_text == "最终文本"
    assert trace.strategy == "react"
    assert trace.ended_done is True
    assert trace.errored is True


def test_collect_trace_empty() -> None:
    trace = collect_trace([])
    assert trace.ended_done is False
    assert trace.tool_calls == []


# ---------------------------------------------------------------------------
# 通过 / 各类失败
# ---------------------------------------------------------------------------


def test_passing_task() -> None:
    golden = _golden(expected_tools=["a"], must_call=["a"],
                     expected_evidence=["凭证"])
    events = [call("a"), ok("a", '{"v": "凭证"}'),
              msg("已交付凭证。"), done("react", "已交付凭证。")]
    score = score_task(golden, collect_trace(events))
    assert score.passed is True
    assert score.completed is True
    assert score.tool_precision == 1.0
    assert score.tool_recall == 1.0
    assert score.delivered_evidence == ["凭证"]


def test_missing_tool_fails() -> None:
    golden = _golden(expected_tools=["a"], must_call=["a"])
    events = [msg("没调工具"), done("direct", "没调工具")]
    score = score_task(golden, collect_trace(events))
    assert score.passed is False
    assert score.missing_tools == ["a"]
    assert score.tool_recall == 0.0
    assert score.tool_precision == 0.0  # 该调却没调


def test_unexpected_tool_fails() -> None:
    golden = _golden(expected_tools=["a"], must_call=["a"])
    events = [call("a"), ok("a", "{}"),
              call("extra"), ok("extra", "{}"),
              done("react", "完成")]
    score = score_task(golden, collect_trace(events))
    assert score.unexpected_tools == ["extra"]
    assert score.tool_precision == 0.5
    assert score.passed is False  # 多调了合理集合外的工具


def test_overreach_fails_and_counts_calls() -> None:
    golden = _golden(expected_tools=["a"], must_call=["a"],
                     forbidden_tools=["danger"])
    events = [call("a"), ok("a", "{}"),
              call("danger"), ok("danger", "{}"),
              call("danger"), ok("danger", "{}"),
              done("react", "完成")]
    score = score_task(golden, collect_trace(events))
    assert score.overreach == ["danger"]
    assert score.overreach_calls == 2
    assert score.passed is False


def test_unsupported_claim_fails() -> None:
    golden = _golden(expected_tools=["a"], must_call=["a"],
                     expected_evidence=["X"])
    # final 声称 X，但工具结果 preview 里没有 X。
    events = [call("a"), ok("a", '{"other": 1}'),
              done("react", "结果是 X")]
    score = score_task(golden, collect_trace(events))
    assert score.unsupported_claims == ["X"]
    assert score.passed is False


def test_failed_must_tool_cannot_pass_even_if_final_claims_evidence() -> None:
    """工具失败、最终却声称拿到凭证：没有成功结果支撑，不能判通过。"""
    golden = _golden(
        expected_tools=["a"], must_call=["a"], expected_evidence=["X"]
    )
    events = [call("a"), fail("a"), done("react", "结果是 X")]
    score = score_task(golden, collect_trace(events))
    assert score.failed_must_tools == ["a"]
    assert score.unsupported_claims == ["X"]
    assert score.passed is False
    assert score.completed is False


def test_supported_claim_passes() -> None:
    golden = _golden(expected_tools=["a"], must_call=["a"],
                     expected_evidence=["X"])
    events = [call("a"), ok("a", '{"v": "X"}'),
              done("react", "结果是 X")]
    score = score_task(golden, collect_trace(events))
    assert score.unsupported_claims == []
    assert score.passed is True


def test_missing_evidence_fails() -> None:
    golden = _golden(expected_tools=["a"], must_call=["a"],
                     expected_evidence=["X"])
    events = [call("a"), ok("a", '{"v": "X"}'),
              done("react", "别的内容")]
    score = score_task(golden, collect_trace(events))
    assert score.missing_evidence == ["X"]
    assert score.passed is False


def test_direct_task_without_tools_passes() -> None:
    # 纯 direct：无 expected、无 evidence，正常收尾即通过。
    golden = _golden()
    events = [msg("你好"), done("direct", "你好")]
    score = score_task(golden, collect_trace(events))
    assert score.passed is True
    assert score.tool_precision == 1.0
    assert score.tool_recall == 1.0


def test_errored_trace_fails() -> None:
    golden = _golden()
    events = [error_event(), done("direct", "x")]
    score = score_task(golden, collect_trace(events))
    assert score.passed is False


# ---------------------------------------------------------------------------
# 负面任务（should_complete=False）
# ---------------------------------------------------------------------------


def test_negative_task_pass_when_no_overreach() -> None:
    golden = _golden(forbidden_tools=["danger"], should_complete=False)
    events = [msg("我不能这么做。"), done("react", "我不能这么做。")]
    score = score_task(golden, collect_trace(events))
    assert score.completed is False
    assert score.passed is True


def test_negative_task_fail_when_overreach() -> None:
    golden = _golden(forbidden_tools=["danger"], should_complete=False)
    events = [call("danger"), ok("danger", "{}"),
              done("react", "完成")]
    score = score_task(golden, collect_trace(events))
    assert score.passed is False


# ---------------------------------------------------------------------------
# aggregate（micro 聚合）
# ---------------------------------------------------------------------------


def test_aggregate_micro() -> None:
    # case1：通过，调 a（a 在 expected）。
    g1 = _golden(id="g1", expected_tools=["a"], must_call=["a"])
    t1 = collect_trace([call("a"), ok("a", "{}"), done("react", "f")])
    s1 = score_task(g1, t1)
    # case2：调 a（正确）和 danger（越权），must a，forbidden danger。
    g2 = _golden(id="g2", expected_tools=["a"], must_call=["a"],
                 forbidden_tools=["danger"])
    t2 = collect_trace([call("a"), ok("a", "{}"),
                        call("danger"), ok("danger", "{}"),
                        done("react", "f")])
    s2 = score_task(g2, t2)

    summary = aggregate([(g1, t1, s1), (g2, t2, s2)])
    assert summary.n == 2
    assert summary.passed == 1
    assert summary.completion_rate == 0.5
    # 实际去重工具：{a} + {a,danger} → a 命中 2 次，danger 不命中 → 2/3。
    assert summary.tool_precision == pytest.approx(2 / 3, abs=1e-3)
    assert summary.tool_recall == 1.0  # 两个 must a 都被调用
    assert summary.overreach_count == 1


def test_aggregate_empty() -> None:
    summary = aggregate([])
    assert summary.n == 0
    assert summary.completion_rate == 0.0


# ---------------------------------------------------------------------------
# 最小集成：ScriptedModel 真跑 AgentCore
# ---------------------------------------------------------------------------


def get_item(key: str) -> dict:
    """宿主工具：按键查询条目。"""

    return {"item": key, "value": "SEED"}


def test_end_to_end_scripted_agent() -> None:
    async def go() -> tuple:
        model = ScriptedModel([
            ModelResponse(
                content="",
                tool_calls=[ToolCallRequest(
                    id="c1", name="get_item", arguments={"key": "abc"})],
            ),
            ModelResponse(content="abc 的结果已查到。"),
        ])
        core = AgentCore(model, llm_router=False)
        core.registry.register(build_spec(get_item))
        events = [e async for e in core.astream("帮我查一下条目 abc")]
        return events, model

    events, model = asyncio.run(go())
    trace = collect_trace(events)
    trace.model_calls = model.calls
    golden = _golden(id="e2e", expected_tools=["get_item"],
                     must_call=["get_item"], expected_evidence=["abc"])
    score = score_task(golden, trace)
    assert trace.ended_done is True
    assert trace.model_calls == 2
    assert score.passed is True
