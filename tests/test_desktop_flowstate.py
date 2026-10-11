"""组合工具流程状态机（flowstate.js）离线测试。

用 QJSEngine 直接 evaluate 纯 JS 状态机，不加载 QML/Window、不依赖显示器，
覆盖真机（内部工具逐个发事件）与离线 demo（仅最终结果或 done 收尾）两类序列。
未装 ``[desktop]`` extra 时整体跳过。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

pytest.importorskip("PySide6.QtCore", reason="需要 PySide6（uv sync --extra desktop）")

from PySide6.QtCore import QCoreApplication  # noqa: E402
from PySide6.QtQml import QJSEngine  # noqa: E402


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    return QCoreApplication.instance() or QCoreApplication([])


@pytest.fixture(scope="module")
def calc():
    src = (ROOT / "shell" / "desktop" / "qml" / "flowstate.js").read_text(
        encoding="utf-8")
    engine = QJSEngine()
    engine.evaluate(src)

    def run(statements: str, result: str):
        expr = ("JSON.stringify((function(){ " + statements
                + " return " + result + "; })())")
        value = engine.evaluate(expr)
        if value.isError():
            raise AssertionError(f"JS 错误：{value.toString()}")
        return json.loads(value.toString())

    return run


def test_live_sequence_all_steps_done(qapp, calc) -> None:
    """真机：内部工具 call/result 逐个到达，最后组合结果收尾。"""
    out = calc(
        "var s = startFlow('combo', ['t1', 't2']);"
        "s = markCall(s, 't1');"
        "s = markResult(s, 't1', true);"
        "s = markCall(s, 't2');"
        "s = markResult(s, 't2', true);"
        "s = markResult(s, 'combo', true);",
        "{states: statesOf(s), finished: s.finished, ok: s.ok}")
    assert out["states"] == ["done", "done"]
    assert out["finished"] is True
    assert out["ok"] is True


def test_demo_sales_closed_by_done(qapp, calc) -> None:
    """demo 销售组合无最终 tool_result，任务 done 时统一收尾为成功。"""
    out = calc(
        "var s = startFlow('tax_quote_brief', "
        "['calc_with_tax', 'add_followup']);"
        "s = finishFlow(s, true);",
        "{states: statesOf(s), finished: s.finished, ok: s.ok}")
    assert out["states"] == ["done", "done"]
    assert out["finished"] is True
    assert out["ok"] is True


def test_demo_companion_closed_by_final_result(qapp, calc) -> None:
    """demo 陪伴组合由同名最终 tool_result 收尾。"""
    out = calc(
        "var s = startFlow('mood_trend_3d', "
        "['recall_moods', 'summarize_trend']);"
        "s = markResult(s, 'mood_trend_3d', true);",
        "{states: statesOf(s), finished: s.finished, ok: s.ok}")
    assert out["states"] == ["done", "done"]
    assert out["finished"] is True


def test_live_midstep_failure_marks_fail_and_keeps_pending(qapp, calc) -> None:
    """真机中途失败：失败节点标 fail，未执行节点保持 wait，组合收尾为失败。"""
    out = calc(
        "var s = startFlow('combo', ['t1', 't2']);"
        "s = markCall(s, 't1');"
        "s = markResult(s, 't1', false);"
        "s = markResult(s, 'combo', false);",
        "{states: statesOf(s), finished: s.finished, ok: s.ok}")
    assert out["states"] == ["fail", "wait"]
    assert out["finished"] is True
    assert out["ok"] is False


def test_empty_steps_immediately_finished(qapp, calc) -> None:
    """空步骤组合直接视为完成。"""
    out = calc(
        "var s = startFlow('empty', []);",
        "{states: statesOf(s), finished: s.finished, ok: s.ok}")
    assert out["states"] == []
    assert out["finished"] is True
    assert out["ok"] is True


def test_unknown_call_leaves_state_unchanged(qapp, calc) -> None:
    """未知工具的 call 不改动节点状态。"""
    out = calc(
        "var s = startFlow('combo', ['t1']);"
        "s = markCall(s, 'nope');",
        "{states: statesOf(s)}")
    assert out["states"] == ["run"]


def test_events_after_finish_are_ignored(qapp, calc) -> None:
    """完成后再喂结果不再改变状态（返回同一对象）。"""
    out = calc(
        "var s = startFlow('combo', ['t1']);"
        "s = markResult(s, 'combo', true);"
        "var s2 = markResult(s, 't1', true);",
        "{states: statesOf(s2), same: s2 === s}")
    assert out["states"] == ["done"]
    assert out["same"] is True
