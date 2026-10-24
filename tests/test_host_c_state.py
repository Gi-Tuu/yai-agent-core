"""陪伴宿主真实接线：当前档案作为瞬态状态源，每轮注入但不进历史。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "examples"))

from host_c_companion import run as host_run  # noqa: E402
from yai_core import EventType, ModelResponse  # noqa: E402


class RecordingModel:
    yai_multimodal = False

    def __init__(self) -> None:
        self.calls: list[list[dict]] = []

    async def achat(self, messages, tools=None, *, tier="standard"):
        self.calls.append([dict(message) for message in messages])
        return ModelResponse(content="我在。")


def _system_text(messages: list[dict]) -> str:
    return "\n".join(
        str(m.get("content", "")) for m in messages if m.get("role") == "system"
    )


def test_companion_profile_is_injected_without_tool_call() -> None:
    model = RecordingModel()
    core = host_run.build_core(model)
    assert core.state_provider_names() == ("companion",)

    result = asyncio.run(core.run("你好"))

    text = _system_text(model.calls[0])
    assert "宿主实时状态" in text
    assert "energy" in text and "62" in text
    assert any(e.type == EventType.HOST_STATE for e in result.events)
    history = "\n".join(message.content for message in core.memory.history())
    assert "energy" not in history
    # 状态是瞬态注入，不替代工具：档案读取工具仍然可用。
    assert core.registry.has("get_companion_profile")
