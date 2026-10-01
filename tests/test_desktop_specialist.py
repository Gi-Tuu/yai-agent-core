"""单专员运行时的离线测试：不依赖网络 / API Key / 显示器。

覆盖：事件跨线程投递、授权与澄清的挂起-兑现、Core 开关与权限挡位、忙拒绝与终止。
未安装 ``[desktop]`` extra 时整体跳过（CI 四矩阵不含 Qt）。
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

pytest.importorskip("PySide6.QtCore", reason="需要 PySide6（uv sync --extra desktop）")

from PySide6.QtCore import QCoreApplication  # noqa: E402
from shell.demo_script import DemoEvent  # noqa: E402
from shell.desktop.specialist import SpecialistRuntime, SpecialistSpec  # noqa: E402


@pytest.fixture(scope="module")
def qapp() -> QCoreApplication:
    return QCoreApplication.instance() or QCoreApplication([])


@pytest.fixture(scope="module")
def loop() -> AsyncIterator[asyncio.AbstractEventLoop]:
    """共享后台循环（工作台负责创建，这里模拟同样的形态）。"""
    event_loop = asyncio.new_event_loop()
    thread = threading.Thread(target=_run, args=(event_loop,), daemon=True)
    thread.start()
    yield event_loop
    event_loop.call_soon_threadsafe(event_loop.stop)
    thread.join(timeout=2)
    event_loop.close()


def _run(event_loop: asyncio.AbstractEventLoop) -> None:
    asyncio.set_event_loop(event_loop)
    event_loop.run_forever()


def _spec(**overrides) -> SpecialistSpec:
    base = dict(id="t", name="测试专员", glyph="试",
                demo_factory=lambda task, channel: _script([]))
    base.update(overrides)
    return SpecialistSpec(**base)


def _runtime(loop, factory) -> SpecialistRuntime:
    return SpecialistRuntime(_spec(demo_factory=factory), loop,
                             factory=factory, run_mode="test")


async def _script(events):
    for event in events:
        yield event


def _pump(app: QCoreApplication, predicate, timeout: float = 5.0) -> bool:
    """在 GUI 线程排队派发跨线程信号，直到条件满足或超时。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    app.processEvents()
    return bool(predicate())


def test_events_are_delivered_to_qml(qapp, loop) -> None:
    events = [
        DemoEvent("strategy_selected", strategy="react", source="llm"),
        DemoEvent("model_message", text="我先看库存"),
        DemoEvent("done", final_text="螺丝 120"),
    ]
    runtime = _runtime(loop, lambda task, channel: _script(events))
    received: list[tuple[str, str]] = []
    runtime.eventReceived.connect(
        lambda name, payload: received.append((name, payload))
    )

    assert runtime.startTask("查库存") is True
    assert _pump(qapp, lambda: [n for n, _ in received] == [
        "strategy_selected", "model_message", "done"])
    # busy 复位在 done 投递之后（同一收尾函数内），跨线程需再轮询一次。
    assert _pump(qapp, lambda: runtime.busy is False)
    assert '"react"' in received[0][1]


def test_permission_prompt_blocks_until_approved(qapp, loop) -> None:
    seen: dict[str, object] = {}

    async def factory(task: str, channel):
        seen["approved"] = await channel.confirm(
            "restock", {"name": "轴承", "qty": 4})
        yield DemoEvent("done", final_text="ok")

    runtime = _runtime(loop, factory)
    assert runtime.startTask("补货") is True
    assert _pump(qapp, lambda: runtime.pending_kind == "confirm")
    assert "restock" in runtime.pending_payload

    runtime.decide(True)
    assert _pump(qapp, lambda: seen.get("approved") is True)
    assert _pump(qapp, lambda: runtime.pending_kind == "" and not runtime.busy)


def test_permission_denied_returns_false(qapp, loop) -> None:
    seen: dict[str, object] = {}

    async def factory(task: str, channel):
        seen["approved"] = await channel.confirm("restock", {"name": "轴承"})
        yield DemoEvent("done", final_text="ok")

    runtime = _runtime(loop, factory)
    runtime.startTask("补货")
    assert _pump(qapp, lambda: runtime.pending_kind == "confirm")
    runtime.decide(False)
    assert _pump(qapp, lambda: seen.get("approved") is False)


def test_clarify_answer_flows_back(qapp, loop) -> None:
    seen: dict[str, object] = {}

    async def factory(task: str, channel):
        seen["answer"] = await channel.ask("要补哪个品名？")
        yield DemoEvent("done", final_text="ok")

    runtime = _runtime(loop, factory)
    runtime.startTask("补货")
    assert _pump(qapp, lambda: runtime.pending_kind == "ask")
    assert "要补哪个品名" in runtime.pending_payload
    runtime.answer("轴承")
    assert _pump(qapp, lambda: seen.get("answer") == "轴承")


def test_core_disabled_rejects_task_with_notice(qapp, loop) -> None:
    runtime = _runtime(loop, lambda task, channel: _script([DemoEvent("done")]))
    runtime.setCoreEnabled(False)
    notices: list[str] = []
    runtime.noticeRaised.connect(notices.append)

    assert runtime.startTask("任何任务") is False
    assert notices and "停用" in notices[0]


def test_busy_rejects_second_task(qapp, loop) -> None:
    async def factory(task: str, channel):
        await asyncio.sleep(0.6)
        yield DemoEvent("done", final_text="ok")

    runtime = _runtime(loop, factory)
    notices: list[str] = []
    runtime.noticeRaised.connect(notices.append)
    assert runtime.startTask("第一个") is True
    assert _pump(qapp, lambda: runtime.busy)
    assert runtime.startTask("第二个") is False
    assert any("执行" in text for text in notices)
    assert _pump(qapp, lambda: not runtime.busy)


def test_cancel_ends_run_with_cancelled_event(qapp, loop) -> None:
    names: list[str] = []

    async def factory(task: str, channel):
        await asyncio.sleep(30)
        yield DemoEvent("done", final_text="不该到达")

    runtime = _runtime(loop, factory)
    runtime.eventReceived.connect(lambda name, payload: names.append(name))
    runtime.startTask("长跑任务")
    assert _pump(qapp, lambda: runtime.busy)
    runtime.cancel()
    assert _pump(qapp, lambda: "cancelled" in names and not runtime.busy)


def test_cancel_before_coroutine_starts(qapp, loop) -> None:
    """起跑前点终止：靠取消标记兜住，不能让界面永远停在"执行中"。"""
    names: list[str] = []

    async def factory(task: str, channel):
        await asyncio.sleep(0.4)
        yield DemoEvent("done", final_text="不该到达")

    runtime = _runtime(loop, factory)
    runtime.eventReceived.connect(lambda name, payload: names.append(name))
    runtime.startTask("慢起跑")
    runtime.cancel()
    assert _pump(qapp, lambda: "cancelled" in names and not runtime.busy)


def test_invalid_permission_mode_is_refused(qapp, loop) -> None:
    runtime = _runtime(loop, lambda task, channel: _script([DemoEvent("done")]))
    notices: list[str] = []
    runtime.noticeRaised.connect(notices.append)
    runtime.setPermissionMode("yolo")
    assert runtime.permission_mode == "partial"
    assert notices


def test_channel_exposes_permission_mode(qapp, loop) -> None:
    seen: dict[str, object] = {}

    async def factory(task: str, channel):
        seen["mode"] = channel.permission_mode
        yield DemoEvent("done")

    runtime = _runtime(loop, factory)
    runtime.setPermissionMode("manual")
    runtime.startTask("查一下")
    assert _pump(qapp, lambda: seen.get("mode") == "manual")
