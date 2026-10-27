"""治理闭环演示脚本的离线回归：批准即执行一次，拒绝即零副作用，trace 两种都留痕。"""

from __future__ import annotations

import asyncio
import json

from scripts import demo_governance_loop as demo


def _trace_types(path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line)["type"] for line in lines]


def _run(tmp_path, approve: bool) -> dict:
    trace = tmp_path / "trace.jsonl"
    return asyncio.run(demo.run(approve, trace))


def test_approve_executes_exactly_once(tmp_path) -> None:
    payload = _run(tmp_path, approve=True)

    assert payload["approval_count"] == 1
    assert payload["executed"] == 1
    assert payload["ledger"] == [{"order_id": demo.DEMO_ORDER, "amount": demo.DEMO_AMOUNT}]

    types = _trace_types(tmp_path / "trace.jsonl")
    assert "strategy_selected" in types
    assert "permission_asked" in types
    assert "tool_result" in types
    assert types[-1] == "done"


def test_refuse_has_no_side_effect(tmp_path) -> None:
    payload = _run(tmp_path, approve=False)

    assert payload["approval_count"] == 1
    assert payload["executed"] == 0
    assert payload["ledger"] == []

    types = _trace_types(tmp_path / "trace.jsonl")
    assert "permission_asked" in types
    assert "tool_result" not in types
    assert types[-1] == "done"
