"""工作台（多专员）离线测试：隔离、切换、事件带 id、未读角标。

同样不依赖网络 / API Key / 显示器；未装 ``[desktop]`` extra 时整体跳过。
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

pytest.importorskip("PySide6.QtCore", reason="需要 PySide6（uv sync --extra desktop）")

from PySide6.QtCore import QCoreApplication  # noqa: E402
from shell.demo_script import DemoEvent  # noqa: E402
from shell.desktop.specialist import SpecialistSpec  # noqa: E402
from shell.desktop.workbench import WorkbenchRuntime  # noqa: E402


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    return QCoreApplication.instance() or QCoreApplication([])


def _pump(app: QCoreApplication, predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    app.processEvents()
    return bool(predicate())


async def _script(events):
    for event in events:
        yield event


async def _slow_then_done(seconds: float):
    await asyncio.sleep(seconds)
    yield DemoEvent("done", final_text="完成了")


def _spec(sid: str, factory) -> SpecialistSpec:
    return SpecialistSpec(id=sid, name=f"{sid}专员", glyph=sid[0],
                          demo_factory=factory)


def _workbench(*pairs) -> WorkbenchRuntime:
    return WorkbenchRuntime(
        [_spec(sid, factory) for sid, factory in pairs], mode="demo")


def test_registration_and_list_model_roles(qapp) -> None:
    workbench = _workbench(("a", lambda t, c: _script([])),
                           ("b", lambda t, c: _script([])))
    assert [s.sid for s in workbench.specialists] == ["a", "b"]
    assert workbench.active_id == "a"
    assert workbench.model.rowCount() == 2

    roles = workbench.model.roleNames()
    name_by_role = {role: key.decode() for role, key in roles.items()}
    first = workbench.model.index(0, 0)
    row = {
        name_by_role[role]: workbench.model.data(first, role) for role in name_by_role
    }
    assert row["specialistId"] == "a"
    assert row["name"] == "a专员"
    assert row["glyph"] == "a"
    assert row["active"] is True
    assert row["busy"] is False
    assert row["runMode"] == "demo"
    workbench.shutdown()


def test_switch_changes_aggregate_properties(qapp) -> None:
    workbench = _workbench(
        ("a", lambda t, c: _script([DemoEvent("done")])),
        ("b", lambda t, c: _script([DemoEvent("done")])),
    )
    workbench.setPermissionMode("manual")
    assert workbench.activePermissionMode == "manual"

    workbench.setActiveSpecialist("b")
    assert workbench.activeSpecialistId == "b"
    # 挡位按专员独立：切过去读的是 b 自己的默认挡
    assert workbench.activePermissionMode == "partial"
    # 只允许一行处于选中态（旧行不刷新会双高亮）
    active_flags = [
        bool(workbench.model.data(workbench.model.index(row, 0),
                                  _role(workbench, "active")))
        for row in range(workbench.model.rowCount())
    ]
    assert active_flags == [False, True]
    workbench.shutdown()


def _role(workbench: WorkbenchRuntime, key: str) -> int:
    for role, name in workbench.model.roleNames().items():
        if name.decode() == key:
            return role
    raise AssertionError(f"未知角色 {key}")


def test_tasks_are_isolated_per_specialist(qapp) -> None:
    workbench = _workbench(
        ("slow", lambda t, c: _slow_then_done(0.8)),
        ("quick", lambda t, c: _script([DemoEvent("done", final_text="x")])),
    )
    events: list[tuple[str, str, str]] = []
    workbench.specialistEvent.connect(
        lambda sid, name, payload: events.append((sid, name, payload)))

    assert workbench.startTask("A 的任务") is True     # 当前专员 = slow
    assert _pump(qapp, lambda: workbench.activeBusy)

    workbench.setActiveSpecialist("quick")
    assert workbench.activeBusy is False              # 画面不被 slow 打断
    assert _pump(qapp, lambda: workbench.startTask("B 的任务") is True)

    # slow 在后台完成：只记未读，不改当前画面
    assert _pump(qapp, lambda: workbench.get("slow").unread >= 1)
    assert workbench.activeSpecialistId == "quick"
    assert any(sid == "slow" and name == "done" for sid, name, _ in events)
    assert any(sid == "quick" for sid, name, _ in events)
    workbench.shutdown()


def test_pending_is_per_specialist(qapp) -> None:
    seen: dict[str, object] = {}

    async def asks(task, channel):
        seen["a"] = await channel.confirm("write_a", {"n": 1})
        yield DemoEvent("done")

    async def idle(task, channel):
        await asyncio.sleep(0.4)
        yield DemoEvent("done")

    workbench = _workbench(("a", asks), ("b", idle))
    workbench.startTask("写一点东西")
    assert _pump(qapp, lambda: workbench.activePendingKind == "confirm")
    assert "write_a" in workbench.activePendingPayload

    workbench.setActiveSpecialist("b")
    assert workbench.activePendingKind == ""          # 挂起跟着 a 走，不污染 b
    assert workbench.get("a").pending_kind == "confirm"
    assert workbench.startTask("b 自己的任务") is True

    workbench.setActiveSpecialist("a")
    assert workbench.activePendingKind == "confirm"
    workbench.decide(True)
    assert _pump(qapp, lambda: seen.get("a") is True)
    workbench.shutdown()


def test_shutdown_settles_every_specialist(qapp) -> None:
    async def long(task, channel):
        await asyncio.sleep(30)
        yield DemoEvent("done")

    workbench = _workbench(("a", long), ("b", long))
    workbench.startTask("跑很久")
    assert _pump(qapp, lambda: workbench.get("a").busy)
    workbench.shutdown()
    assert _pump(qapp, lambda: not workbench.get("a").busy)
    assert not workbench._thread.is_alive()


def test_code_vault_roundtrip_on_worker_loop(qapp, tmp_path) -> None:
    """空闲也能管工具：读写派发到常驻 loop，结果经信号回 GUI。"""
    from shell.desktop.specialist import SpecialistSpec

    from yai_core import CodeToolManager, ToolRegistry

    storage = tmp_path / "warehouse_code_tools.json"
    CodeToolManager(ToolRegistry(), storage_path=storage).create(
        name="double", description="把输入数字翻倍",
        input_schema={"type": "object", "properties": {"x": {"type": "number"}}},
        code="def run(inputs):\n    return {'value': inputs['x'] * 2}")

    spec = SpecialistSpec(
        id="warehouse", name="仓库专员", glyph="仓",
        demo_factory=lambda task, channel: _script([]), code_storage=storage)
    workbench = WorkbenchRuntime([spec], mode="demo")
    updates: list[str] = []
    workbench.codeVaultUpdated.connect(updates.append)

    assert workbench.codeVaultAvailable is True
    workbench.openCodeVault()
    assert _pump(qapp, lambda: "double" in workbench.codeVault)
    assert '"description": "把输入数字翻倍"' in workbench.codeVault

    workbench.setCodeToolPermanent("double", True)
    assert _pump(qapp, lambda: '"permanent": true' in workbench.codeVault)
    assert '"permanent": true' in storage.read_text(encoding="utf-8")

    workbench.removeCodeTool("double")
    assert _pump(qapp, lambda: '"total": 0' in workbench.codeVault)
    assert len(updates) >= 3
    workbench.shutdown()


def test_code_vault_unavailable_without_storage(qapp) -> None:
    workbench = _workbench(("a", lambda task, channel: _script([])))
    assert workbench.codeVaultAvailable is False
    workbench.openCodeVault()          # 没有仓库路径时静默不动作，不抛
    assert workbench.codeVault == "{}"
    workbench.shutdown()


def _feed_learning(path: Path, task: str = "帮我查一下这个 key 的值",
                   reward: float = 0.95) -> None:
    """跑一次 suggest + record 并落盘，构造一个有数据的学习文件。"""
    from shell.desktop.learning_vault import load_selector, save_selector

    from yai_core import Strategy, ToolRegistry, discover
    from yai_core.learning import RouteOutcome

    class _Host:
        def get_value(self, key: str) -> str:
            """按键取值。"""
            return key

    reg = ToolRegistry()
    reg.register_many(discover(_Host()))
    selector = load_selector(path)
    selector.suggest(task, reg)
    selector.record(task, reg, Strategy.REACT,
                    RouteOutcome(success=True, reward=reward))
    save_selector(selector, path)


def test_learning_roundtrip_on_worker_loop(qapp, tmp_path) -> None:
    """空闲也能看学习：读取派发到常驻 loop，结果经信号回 GUI。"""
    learning = tmp_path / "warehouse_route_learning.json"
    _feed_learning(learning)

    spec = SpecialistSpec(
        id="warehouse", name="仓库专员", glyph="仓",
        demo_factory=lambda task, channel: _script([]), learning_path=learning)
    workbench = WorkbenchRuntime([spec], mode="demo")
    updates: list[str] = []
    workbench.learningUpdated.connect(updates.append)

    assert workbench.learningAvailable is True
    workbench.openLearning()
    assert _pump(qapp, lambda: '"enabled": true' in workbench.learningVault)
    assert '"learned_tasks": 1' in workbench.learningVault
    assert '"context_buckets": 1' in workbench.learningVault
    assert '"direct"' in workbench.learningVault and '"react"' in workbench.learningVault
    assert len(updates) >= 1
    workbench.shutdown()


def test_learning_unavailable_without_path(qapp) -> None:
    workbench = _workbench(("a", lambda task, channel: _script([])))
    assert workbench.learningAvailable is False
    workbench.openLearning()          # 没有学习路径时静默不动作，不抛
    assert workbench.learningVault == "{}"
    workbench.shutdown()
