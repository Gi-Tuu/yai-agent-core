"""Phase 3 尾项回归：灵动岛专员 → 联邦子核、usage 聚合/预算、两个可安装子核 demo。"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
from shell.federation import (
    ChildCore,
    Federation,
    FederationError,
    attach_specialists,
    build_specialist_child,
)

from yai_core import (
    AgentCore,
    ModelResponse,
    ScriptedModel,
    ToolCallRequest,
    build_spec,
)

ROOT = Path(__file__).resolve().parents[1]
HUB_DEMO = ROOT / "examples" / "child_cores" / "hub_demo.py"


class NotesApp:
    """桌面灵动岛"笔记专员"的最小替身。"""

    def __init__(self) -> None:
        self.notes: list[str] = []

    def add_note(self, text: str) -> dict:
        """新增一条笔记。"""
        self.notes.append(text)
        return {"count": len(self.notes)}

    def search_notes(self, keyword: str) -> list[str]:
        """按关键词搜索笔记。"""
        return [note for note in self.notes if keyword in note]


class WarehouseApp:
    """"仓库专员"的最小替身。"""

    def __init__(self) -> None:
        self.items = {"螺丝": 120}

    def get_stock(self, name: str) -> dict:
        """查询库存。"""
        return {"name": name, "stock": self.items.get(name, 0)}


def _parent(*calls, final: str = "完成") -> AgentCore:
    responses = [
        ModelResponse(
            content="",
            tool_calls=[ToolCallRequest(id=f"c{i}", name=name, arguments=args)],
        )
        for i, (name, args) in enumerate(calls)
    ]
    responses.append(ModelResponse(content=final))
    return AgentCore(ScriptedModel(responses))


# ---------- 专员对象 → 子核 ----------


def test_build_specialist_child_introspects_public_methods() -> None:
    child = build_specialist_child(NotesApp(), node_id="notes", product="notes")
    assert set(child.manifest()) == {"add_note", "search_notes"}
    assert child.child_id == "notes"


def test_specialist_without_public_methods_is_rejected() -> None:
    class Empty:
        def _hidden(self) -> None:
            pass

    with pytest.raises(ValueError):
        build_specialist_child(Empty(), node_id="empty")


def test_attach_specialists_and_parent_calls_both() -> None:
    notes, warehouse = NotesApp(), WarehouseApp()
    parent = _parent(
        ("notes__add_note", {"text": "重要的事"}),
        ("warehouse__get_stock", {"name": "螺丝"}),
        final="都办好了。",
    )
    federation = Federation(parent, node_id="island")
    grants = attach_specialists(federation, {"notes": notes, "warehouse": warehouse})

    result = asyncio.run(parent.run("记一条并查库存"))

    assert notes.notes == ["重要的事"]
    assert result.final_text == "都办好了。"
    assert set(grants) == {"notes", "warehouse"}
    exposed = {tool["name"] for tool in parent.list_tools()}
    assert {"notes__add_note", "warehouse__get_stock"} <= exposed


# ---------- usage 聚合与 token 预算 ----------


def test_usage_is_aggregated_and_token_budget_fails_closed() -> None:
    def charge() -> dict:
        """返回一次带 usage 的子核结果。"""
        return {"ok": True, "usage": {"total_tokens": 60}}

    child = ChildCore(
        _sync_core([charge]),
        node_id="billing",
        child_id="billing-app",
        product="billing",
    )
    federation = Federation(_parent(), node_id="hub")
    federation.register_child(child, max_tokens=100)

    async def main() -> None:
        await federation.invoke("billing", "charge")
        assert federation.edge_stats("billing")["tokens"] == 60
        with pytest.raises(FederationError) as excinfo:
            await federation.invoke("billing", "charge")
        assert "edge_token_budget_exceeded" in str(excinfo.value)

    asyncio.run(main())
    stats = federation.edge_stats("billing")
    assert stats["tokens"] == 120
    assert stats["usage"]["total_tokens"] == 120
    assert stats["failures"] == 1


def _sync_core(functions):
    core = AgentCore(ScriptedModel([ModelResponse(content="idle")]))
    core.register_tools([build_spec(fn) for fn in functions])
    return core


# ---------- 两个可安装子核的端到端 demo ----------


def test_two_installable_child_cores_hub_demo() -> None:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    completed = subprocess.run(
        [sys.executable, "-X", "utf8", str(HUB_DEMO)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    output = completed.stdout
    assert "notes__add_note" in output
    assert "calc__add" in output
    assert "生态 demo 已跑通" in output
    assert "12+30=42" in output
