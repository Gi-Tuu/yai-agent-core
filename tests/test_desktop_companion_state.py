"""桌面陪伴专员的状态源接线：真实 _live_factory 里注册并注入。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "examples"))

# shell.desktop 包会导入 PySide6；最小 CI（只装 dev）没有桌面 extra。
pytest.importorskip("PySide6")

from shell.desktop.host import DemoCompanion, _companion_state, _live_factory  # noqa: E402

import runner_common  # noqa: E402
from yai_core import EventType, ModelResponse, ScriptedModel  # noqa: E402


class Channel:
    permission_mode = "partial"

    async def emit(self, event) -> None:  # noqa: ANN001, ARG002
        return None

    async def confirm(self, *args, **kwargs) -> bool:  # noqa: ANN002, ANN003
        return True

    async def ask(self, question: str) -> str:  # noqa: ARG002
        return ""


def test_companion_state_helper_returns_recent_moods() -> None:
    host = DemoCompanion()
    host.log_mood("开心", "测试")
    state = _companion_state(host)
    assert state["total"] == 3
    assert state["recent_moods"][-1]["note"] == "测试"


def test_live_factory_registers_and_injects_companion_state(monkeypatch) -> None:
    monkeypatch.setattr(
        runner_common,
        "build_model",
        lambda: (ScriptedModel([ModelResponse(content="我在。")]), "test"),
    )
    monkeypatch.setattr(runner_common, "load_dotenv", lambda: None)

    host = DemoCompanion()
    factory = _live_factory(
        host,
        (),
        state_provider=("companion", lambda: _companion_state(host)),
    )

    async def run():
        return [event async for event in factory("你好", Channel())]

    events = asyncio.run(run())
    state_events = [event for event in events if event.type == EventType.HOST_STATE]
    assert state_events
    providers = state_events[0].data["providers"]
    assert providers[0]["name"] == "companion"
    assert any(event.type == EventType.DONE for event in events)
