"""零依赖 MetricsRecorder 单测：计数口径、失败/取消分类、直方图、Prometheus 渲染。"""

from __future__ import annotations

import asyncio

from yai_core.metrics import MetricsRecorder
from yai_core.types import AgentEvent, EventType


def _completed_run(recorder: MetricsRecorder, *, standard: int = 1, strong: int = 0) -> None:
    """模拟一次正常跑完（见到 DONE）的 run。"""
    recorder.on_run_start()
    recorder.record_event(
        AgentEvent(EventType.STRATEGY_SELECTED, {"strategy": "direct"})
    )
    recorder.record_event(
        AgentEvent(
            EventType.DONE,
            {"standard_calls": standard, "strong_calls": strong},
        )
    )
    recorder.on_run_finish(None)


def test_completed_run_counts_and_model_tiers() -> None:
    rec = MetricsRecorder()
    _completed_run(rec, standard=2, strong=1)
    snap = rec.snapshot()
    assert snap["runs"] == {"started": 1, "completed": 1, "failed": 0, "cancelled": 0}
    assert snap["runs_by_strategy"] == {"direct": 1}
    # 模型调用按 DONE 事件里的真实次数累加（成本归因口径）。
    assert snap["model_calls"] == {"standard": 2, "strong": 1}
    assert snap["events"]["done"] == 1
    assert snap["events"]["strategy_selected"] == 1


def test_tool_calls_split_ok_and_error() -> None:
    rec = MetricsRecorder()
    rec.on_run_start()
    rec.record_event(AgentEvent(EventType.TOOL_CALL, {"tool": "list_notes"}))
    rec.record_event(AgentEvent(EventType.TOOL_RESULT, {"tool": "list_notes", "ok": True}))
    rec.record_event(AgentEvent(EventType.TOOL_CALL, {"tool": "weather"}))
    rec.record_event(AgentEvent(EventType.TOOL_RESULT, {"tool": "weather", "ok": False}))
    rec.record_event(AgentEvent(EventType.DONE, {}))
    rec.on_run_finish(None)
    tools = rec.snapshot()["tool_calls"]
    assert tools["list_notes"] == {"total": 1, "errors": 0}
    assert tools["weather"] == {"total": 1, "errors": 1}


def test_failed_run_without_done_counts_as_failed() -> None:
    rec = MetricsRecorder()
    rec.on_run_start()
    rec.on_run_finish(RuntimeError("model blew up"))
    snap = rec.snapshot()
    assert snap["runs"] == {"started": 1, "completed": 0, "failed": 1, "cancelled": 0}


def test_cancelled_run_counts_as_cancelled() -> None:
    rec = MetricsRecorder()
    rec.on_run_start()
    rec.on_run_finish(asyncio.CancelledError())
    assert rec.snapshot()["runs"]["cancelled"] == 1
    assert rec.snapshot()["runs"]["failed"] == 0


def test_done_then_finish_does_not_double_count() -> None:
    rec = MetricsRecorder()
    _completed_run(rec)
    # 已见 DONE 的正常 run，finally 里再调 finish(None) 不得重复计 completed。
    rec.on_run_finish(None)
    assert rec.snapshot()["runs"]["completed"] == 1


def test_histogram_accumulates_and_snapshot_is_isolated() -> None:
    rec = MetricsRecorder()
    _completed_run(rec)
    _completed_run(rec)
    hist = rec.snapshot()["run_duration_seconds"]
    assert hist["count"] == 2
    assert hist["buckets"]["+Inf"] == 2  # +Inf 桶等于总样本数
    # 两次都极快，应全部落入最小桶，且累计桶单调不减。
    assert hist["buckets"]["0.5"] == 2

    snap = rec.snapshot()
    snap["runs"]["completed"] = 999  # 篡改快照不得回流到收集器。
    assert rec.snapshot()["runs"]["completed"] == 2


def test_dirty_event_data_is_tolerated() -> None:
    rec = MetricsRecorder()
    rec.on_run_start()
    # 缺字段 / 非法计数都不应抛异常。
    rec.record_event(AgentEvent(EventType.TOOL_CALL, {}))
    rec.record_event(AgentEvent(EventType.TOOL_RESULT, {"tool": "x"}))  # 无 ok 视为成功
    rec.record_event(AgentEvent(EventType.DONE, {"standard_calls": "oops"}))
    rec.on_run_finish(None)
    snap = rec.snapshot()
    assert snap["runs"]["completed"] == 1
    assert snap["model_calls"]["standard"] == 0
    assert snap["tool_calls"]["unknown"]["total"] == 1


def test_prometheus_render_contains_core_series() -> None:
    rec = MetricsRecorder()
    _completed_run(rec, standard=3)
    text = rec.render_prometheus()
    assert "# TYPE yai_agent_runs_total counter" in text
    assert 'yai_agent_runs_total{outcome="completed"} 1' in text
    assert 'yai_agent_model_calls_total{tier="standard"} 3' in text
    assert 'yai_run_duration_seconds_bucket{le="+Inf"} 1' in text
    assert "yai_run_duration_seconds_count 1" in text
    assert "yai_run_duration_seconds_sum" in text
    assert "# TYPE yai_uptime_seconds gauge" in text
    assert text.endswith("\n")
