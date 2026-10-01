"""灵动岛真机 bandit 接线测试：跨任务共享选择器、任务后落盘、观测累积。

需要 PySide6（host.py 连带导入 specialist）；未装 ``[desktop]`` extra 时整体跳过。
不依赖网络 / API Key：monkeypatch ``runner_common.build_model`` 返回脚本模型；
``llm_router="auto"`` 对无 ``yai_live_router`` 标记的脚本模型走规则路由，
脚本模型只在 react 循环里被调用，不会被当成分类器。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

pytest.importorskip("PySide6.QtCore", reason="需要 PySide6（uv sync --extra desktop）")

from PySide6.QtCore import QCoreApplication  # noqa: E402
from shell.desktop.host import (  # noqa: E402
    _READ_TOOLS,
    DemoNotes,
    DemoSales,
    DemoWarehouse,
    _live_factory,
    prepare_paths,
)
from shell.desktop.learning_vault import summarize  # noqa: E402

from yai_core import ModelResponse, ToolCallRequest  # noqa: E402

prepare_paths()  # 与真机启动一致：补齐 src 与 examples 导入路径
import runner_common  # noqa: E402


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    return QCoreApplication.instance() or QCoreApplication([])


class _Channel:
    # auto 挡位：读白名单全放行；脚本化 run 不触发 ask/confirm。
    permission_mode = "auto"

    async def emit(self, event) -> None:
        return None


class _ScriptedNotesModel:
    """奇数次回复给 search_notes 工具调用，偶数次给最终总结（可跨任务循环）。"""

    def __init__(self) -> None:
        self.calls = 0

    async def achat(self, messages, tools=None, *, tier="standard"):
        self.calls += 1
        if self.calls % 2 == 1:
            return ModelResponse(
                content="",
                tool_calls=[ToolCallRequest(
                    id=f"c{self.calls}", name="search_notes",
                    arguments={"keyword": "周报"})],
            )
        return ModelResponse(content="周报里有 Core 骨架、薄壳原生端两条。")


def _patch(monkeypatch, model: _ScriptedNotesModel) -> None:
    monkeypatch.setattr(runner_common, "build_model",
                        lambda: (model, "scripted"))
    monkeypatch.setattr(runner_common, "load_dotenv",
                        lambda *a, **k: False)


async def _collect(factory, task: str) -> list[str]:
    agen = factory(task, _Channel())
    names = []
    async for event in agen:
        names.append(event.type.value)
    await agen.aclose()
    return names


def test_live_factory_persists_learning(qapp, monkeypatch, tmp_path) -> None:
    model = _ScriptedNotesModel()
    _patch(monkeypatch, model)
    path = tmp_path / "notes_learning.json"
    factory = _live_factory(DemoNotes(), ("search_notes",), learning_path=path)

    names = asyncio.run(_collect(factory, "搜一下周报"))

    assert "tool_call" in names and "tool_result" in names
    assert names[-1] == "done"
    assert path.exists()
    status = summarize(path)
    assert status["enabled"] is True
    assert status["learned_tasks"] == 1


def test_learning_accumulates_across_tasks(qapp, monkeypatch, tmp_path) -> None:
    model = _ScriptedNotesModel()
    _patch(monkeypatch, model)
    path = tmp_path / "notes_learning.json"
    factory = _live_factory(DemoNotes(), ("search_notes",), learning_path=path)

    asyncio.run(_collect(factory, "搜一下周报"))
    # 灵动岛第二个任务：同一个 factory 闭包再调用一次，学习必须累积而非清零。
    asyncio.run(_collect(factory, "再搜一次周报"))

    assert summarize(path)["learned_tasks"] == 2


def test_no_learning_path_keeps_old_behavior(qapp, monkeypatch, tmp_path) -> None:
    model = _ScriptedNotesModel()
    _patch(monkeypatch, model)
    factory = _live_factory(DemoNotes(), ("search_notes",))

    names = asyncio.run(_collect(factory, "搜周报"))

    assert names[-1] == "done"
    assert list(tmp_path.glob("*.json")) == []


class _ScriptedSalesModel:
    """奇数次给 list_customers 工具调用，偶数次给客户总结（可跨任务循环）。"""

    def __init__(self) -> None:
        self.calls = 0

    async def achat(self, messages, tools=None, *, tier="standard"):
        self.calls += 1
        if self.calls % 2 == 1:
            return ModelResponse(
                content="",
                tool_calls=[ToolCallRequest(
                    id=f"c{self.calls}", name="list_customers", arguments={})],
            )
        return ModelResponse(content="客户有星河科技（A）、云海贸易（B）两家。")


def test_demo_sales_host_and_discovery(qapp) -> None:
    from yai_core import discover

    names = {s.name for s in discover(DemoSales())}
    assert {"list_customers", "get_customer",
            "list_opportunities", "add_order"} <= names

    crm = DemoSales()
    assert "星河科技" in crm.list_customers()
    assert crm.get_customer("云海贸易")["contact"] == "李总"
    assert len(crm.list_opportunities()) == 2
    assert crm.add_order("星河科技", "许可", 999)["order_no"] == 1


def test_sales_live_factory_runs_with_sandbox(qapp, monkeypatch, tmp_path) -> None:
    _patch(monkeypatch, _ScriptedSalesModel())
    learning = tmp_path / "sales_learning.json"
    factory = _live_factory(
        DemoSales(),
        _READ_TOOLS["sales"],
        with_sandbox=True,
        storage_path=tmp_path / "sales_tools.json",
        learning_path=learning,
    )

    names = asyncio.run(_collect(factory, "列一下客户"))

    assert "tool_call" in names and names[-1] == "done"
    assert summarize(learning)["learned_tasks"] == 1


class _ScriptedWarehouseModel:
    """奇数次给 get_stock 工具调用，偶数次给库存结论（可跨任务循环）。"""

    def __init__(self) -> None:
        self.calls = 0

    async def achat(self, messages, tools=None, *, tier="standard"):
        self.calls += 1
        if self.calls % 2 == 1:
            return ModelResponse(
                content="",
                tool_calls=[ToolCallRequest(
                    id=f"c{self.calls}", name="get_stock",
                    arguments={"name": "轴承"})],
            )
        return ModelResponse(content="轴承当前库存 8 件，低于安全线，建议补货。")


def test_warehouse_showcase_full_adaptive(qapp, monkeypatch, tmp_path) -> None:
    # 仓库专员 = 自适应全开样板间：路由学习 + 沙箱造工具 + 工具持久化同进程协同。
    _patch(monkeypatch, _ScriptedWarehouseModel())
    learning = tmp_path / "wh_learning.json"
    factory = _live_factory(
        DemoWarehouse(),
        _READ_TOOLS["warehouse"],
        with_sandbox=True,
        storage_path=tmp_path / "wh_tools.json",
        learning_path=learning,
    )

    first = asyncio.run(_collect(factory, "查一下轴承库存"))
    assert "strategy_selected" in first
    assert "tool_call" in first and "tool_result" in first
    assert first[-1] == "done"

    # 第二个任务：同一专员再跑，路由学习必须累积（"越用越准"的可执行证据）。
    asyncio.run(_collect(factory, "再查一下螺丝库存"))
    assert summarize(learning)["learned_tasks"] == 2
