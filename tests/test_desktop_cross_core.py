"""跨核调用可视化：edge 聚合 + 工作台接线（离线，不依赖显示器/网络）。"""

from __future__ import annotations

import pytest
from shell.desktop.federation_view import CrossCoreView, split_cross_core_tool


def test_split_cross_core_tool() -> None:
    assert split_cross_core_tool("notes__add_note") == ("notes", "add_note")
    # 前缀里允许下划线；只按第一个 __ 切
    assert split_cross_core_tool("notes_app__search_notes") == (
        "notes_app",
        "search_notes",
    )
    assert split_cross_core_tool("search_notes") is None
    assert split_cross_core_tool("__notes") is None
    assert split_cross_core_tool("notes__") is None
    assert split_cross_core_tool(None) is None


def test_cross_core_view_aggregates_calls_results_and_usage() -> None:
    view = CrossCoreView()
    assert view.observe("tool_call", {"tool": "notes__add_note"})
    view.observe(
        "tool_result",
        {"tool": "notes__add_note", "ok": True, "usage": {"total_tokens": 40}},
    )
    view.observe("tool_call", {"tool": "notes__search_notes"})
    view.observe("tool_result", {"tool": "notes__search_notes", "ok": False})
    view.observe("tool_call", {"tool": "calc__add"})

    rows = view.edges()
    assert [row["prefix"] for row in rows] == ["notes", "calc"]
    notes = next(row for row in rows if row["prefix"] == "notes")
    assert notes["calls"] == 2
    assert notes["failures"] == 1
    assert notes["tokens"] == 40
    assert notes["last_tool"] == "search_notes"
    assert notes["last_ok"] is False
    assert view.summary() == {"edges": 2, "calls": 3, "failures": 1, "tokens": 40}


def test_non_cross_core_events_are_ignored() -> None:
    view = CrossCoreView()
    assert view.observe("strategy_selected", {"strategy": "direct"}) == []
    assert view.observe("tool_call", {"tool": "search_notes"}) == []
    assert view.observe("tool_call", "not-json") == []
    assert view.observe("tool_result", {"tool": "notes__add_note", "ok": True}) == []
    assert view.edges() == []
    assert view.summary() == {"edges": 0, "calls": 0, "failures": 0, "tokens": 0}


def test_usage_without_total_tokens_sums_numeric_fields() -> None:
    view = CrossCoreView()
    view.observe("tool_call", {"tool": "notes__x"})
    view.observe(
        "tool_result",
        {"tool": "notes__x", "ok": True, "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
    )
    assert view.edges()[0]["tokens"] == 15


# ---------- 工作台接线（QML 读的就是这个 Property） ----------


def _workbench_with_sales():
    PySide6 = pytest.importorskip("PySide6.QtCore", reason="需要 PySide6")
    from shell.desktop.specialist import SpecialistSpec
    from shell.desktop.workbench import WorkbenchRuntime

    async def _empty(task, channel):
        if False:  # pragma: no cover - 只为满足异步生成器签名
            yield None

    spec = SpecialistSpec(
        id="sales", name="销售专员", glyph="销", demo_factory=_empty
    )
    return WorkbenchRuntime([spec], mode="demo"), PySide6


@pytest.fixture(scope="module")
def qapp():
    PySide6 = pytest.importorskip("PySide6.QtCore", reason="需要 PySide6")
    return PySide6.QCoreApplication.instance() or PySide6.QCoreApplication([])


def test_workbench_exposes_cross_core_edges(qapp) -> None:
    workbench, _ = _workbench_with_sales()
    changed: list[int] = []
    workbench.crossCoreChanged.connect(lambda: changed.append(1))

    workbench._relay("sales", "tool_call", '{"tool": "notes__add_note"}')  # noqa: SLF001
    workbench._relay(  # noqa: SLF001
        "sales",
        "tool_result",
        '{"tool": "notes__add_note", "ok": true, "usage": {"total_tokens": 12}}',
    )
    workbench._relay("sales", "tool_call", '{"tool": "search_notes"}')  # noqa: SLF001

    rows = workbench.crossCoreEdges
    assert len(rows) == 1
    assert rows[0]["prefix"] == "notes"
    assert rows[0]["calls"] == 1
    assert rows[0]["tokens"] == 12
    assert changed  # 至少两次变化（call + result）
