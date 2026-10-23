"""宿主实时状态（现状锚）：每轮瞬态注入、不持久化、失败降级、容量上限。"""

from __future__ import annotations

import asyncio
import json

import pytest

from yai_core import (
    AgentCore,
    DiagnosticKind,
    EventType,
    HostStateRegistry,
    ModelResponse,
    ToolCallRequest,
    build_spec,
)
from yai_core.kernel.state import _FRAME


class RecordingModel:
    """按脚本返回响应，并保存每次模型调用收到的消息。"""

    yai_multimodal = False

    def __init__(self, responses: list[ModelResponse]) -> None:
        self._responses = responses
        self.calls: list[list[dict]] = []

    async def achat(self, messages, tools=None, *, tier="standard"):
        self.calls.append([dict(message) for message in messages])
        return self._responses[min(len(self.calls) - 1, len(self._responses) - 1)]


def _system_text(messages: list[dict]) -> str:
    return "\n".join(
        str(message.get("content", ""))
        for message in messages
        if message.get("role") == "system"
    )


def lookup(name: str = "") -> dict:
    """按姓名查询。"""
    return {"name": name}


def test_context_transient_block_is_second_and_not_persisted() -> None:
    from yai_core import ChatMessage, Context

    ctx = Context("基础提示")
    ctx.add(ChatMessage(role="user", content="任务"))

    out = ctx.llm_messages(transient_system="状态快照")

    assert out[0] == {"role": "system", "content": "基础提示"}
    assert out[1] == {"role": "system", "content": "状态快照"}
    assert [m.content for m in ctx.messages] == ["基础提示", "任务"]


def test_state_is_injected_but_not_persisted() -> None:
    model = RecordingModel([ModelResponse(content="好的")])
    core = AgentCore(model)
    core.register_state_provider(
        "workbench",
        lambda: {"selected_customer": "王敏", "tab": "followups"},
    )

    result = asyncio.run(core.run("继续处理"))

    text = _system_text(model.calls[0])
    assert _FRAME in text
    assert "王敏" in text and "followups" in text
    # 瞬态块不写回对话历史（否则会过期、占 token、被压缩进摘要）。
    history = "\n".join(message.content for message in core.memory.history())
    assert "王敏" not in history
    # 事件只报大小与 provider 名，不复制可能含 PII 的状态内容。
    events = [e for e in result.events if e.type == EventType.HOST_STATE]
    assert len(events) == 1
    payload = events[0].data
    assert payload["providers"][0]["name"] == "workbench"
    assert "王敏" not in json.dumps(payload, ensure_ascii=False)


def test_state_refreshes_on_every_model_call() -> None:
    counter = {"value": 0}

    def provider() -> dict:
        counter["value"] += 1
        return {"step": counter["value"]}

    model = RecordingModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCallRequest(id="1", name="lookup", arguments={"name": "王敏"})
                ]
            ),
            ModelResponse(content="完成"),
        ]
    )
    core = AgentCore(model, auto_approve_tools=True)
    core.register_tools([build_spec(lookup)])
    core.register_state_provider("counter", provider)

    result = asyncio.run(core.run("查一下客户王敏"))

    assert len(model.calls) == 2
    assert '"step": 1' in _system_text(model.calls[0])
    assert '"step": 2' in _system_text(model.calls[1])
    assert len([e for e in result.events if e.type == EventType.HOST_STATE]) == 2


def test_unchanged_state_emits_one_event() -> None:
    model = RecordingModel(
        [
            ModelResponse(
                tool_calls=[
                    ToolCallRequest(id="1", name="lookup", arguments={"name": "王敏"})
                ]
            ),
            ModelResponse(content="完成"),
        ]
    )
    core = AgentCore(model, auto_approve_tools=True)
    core.register_tools([build_spec(lookup)])
    core.register_state_provider("static", lambda: {"mode": "edit"})

    result = asyncio.run(core.run("查一下客户王敏"))

    assert len(model.calls) == 2
    assert len([e for e in result.events if e.type == EventType.HOST_STATE]) == 1


def test_async_provider_and_failure_degrades() -> None:
    async def slowish() -> str:
        await asyncio.sleep(0)
        return "异步状态"

    def broken() -> str:
        raise RuntimeError("host bug")

    diagnostics: list = []
    model = RecordingModel([ModelResponse(content="ok")])
    core = AgentCore(model, on_diagnostic=diagnostics.append)
    core.register_state_provider("async", slowish)
    core.register_state_provider("broken", broken)

    result = asyncio.run(core.run("你好"))

    text = _system_text(model.calls[0])
    assert "异步状态" in text
    assert "broken" not in text
    assert any(d.kind == DiagnosticKind.HOST_STATE_FAILED for d in diagnostics)
    assert [e for e in result.events if e.type == EventType.HOST_STATE]


def test_state_is_not_reused_after_unregister() -> None:
    model = RecordingModel([ModelResponse(content="ok")])
    core = AgentCore(model)
    core.register_state_provider("temp", lambda: {"secret": "old-state"})
    asyncio.run(core.run("第一次"))

    assert core.unregister_state_provider("temp") is True
    assert core.unregister_state_provider("temp") is False
    asyncio.run(core.run("第二次"))

    assert "old-state" not in _system_text(model.calls[1])


def test_registration_validation() -> None:
    core = AgentCore(RecordingModel([ModelResponse(content="ok")]))

    with pytest.raises(ValueError, match="不能为空"):
        core.register_state_provider("  ", lambda: "")
    with pytest.raises(TypeError, match="可调用"):
        core.register_state_provider("bad", "not-callable")  # type: ignore[arg-type]

    core.register_state_provider("x", lambda: "1")
    with pytest.raises(ValueError, match="已注册"):
        core.register_state_provider("x", lambda: "2")
    core.register_state_provider("x", lambda: "3", replace=True)
    assert core.state_provider_names() == ("x",)


# ---------- 注册表本身：容量、截断、渲染 ----------

def test_registry_clips_per_provider_and_total() -> None:
    registry = HostStateRegistry(per_provider_chars=10, total_chars=80)
    registry.register("a", lambda: "A" * 50)
    registry.register("b", lambda: "B" * 50)

    body, payload = asyncio.run(registry.render())

    assert "已截断" in body
    assert payload is not None
    assert payload["truncated"] is True
    assert len(payload["providers"]) == 2

    tight = HostStateRegistry(per_provider_chars=100, total_chars=5)
    tight.register("a", lambda: "A" * 50)
    tight.register("b", lambda: "B" * 50)
    tight_body, tight_payload = asyncio.run(tight.render())
    assert "总长度已截断" in tight_body
    assert tight_payload is not None and len(tight_payload["providers"]) == 1


def test_registry_skips_empty_and_renders_json_stably() -> None:
    registry = HostStateRegistry()
    registry.register("none", lambda: None)
    registry.register("blank", lambda: "   ")
    registry.register("mapping", lambda: {"b": 2, "a": 1})

    body, payload = asyncio.run(registry.render())

    assert payload is not None
    assert [item["name"] for item in payload["providers"]] == ["mapping"]
    assert '{"a": 1, "b": 2}' in body


def test_registry_name_limits_and_provider_cap() -> None:
    registry = HostStateRegistry(max_providers=1)
    with pytest.raises(ValueError, match="48"):
        registry.register("x" * 49, lambda: "")
    registry.register("ok", lambda: "")
    with pytest.raises(ValueError, match="数量"):
        registry.register("second", lambda: "")


def test_registry_fingerprint_changes_only_with_content() -> None:
    state = {"value": "a"}
    registry = HostStateRegistry()
    registry.register("s", lambda: state)

    first = asyncio.run(registry.render())
    second = asyncio.run(registry.render())
    state["value"] = "b"
    third = asyncio.run(registry.render())

    assert first[1] is not None and second[1] is not None and third[1] is not None
    assert first[1]["fingerprint"] == second[1]["fingerprint"]
    assert third[1]["fingerprint"] != first[1]["fingerprint"]


def test_registry_rejects_invalid_settings_and_name_types() -> None:
    with pytest.raises(ValueError, match="正数"):
        HostStateRegistry(total_chars=0)
    registry = HostStateRegistry()
    with pytest.raises(TypeError, match="名称"):
        registry.register(None, lambda: "")  # type: ignore[arg-type]
    registry.register("x", lambda: "")
    registry.clear()
    assert registry.names() == ()


def test_registry_survives_unserializable_state() -> None:
    circular: dict = {}
    circular["self"] = circular
    registry = HostStateRegistry()
    registry.register("circular", lambda: circular)

    body, payload = asyncio.run(registry.render())

    assert payload is not None and "self" in body

    class Exploding:
        def __str__(self) -> str:
            raise RuntimeError("nope")

    broken = HostStateRegistry()
    broken.register("bad", lambda: Exploding())
    broken_body, broken_payload = asyncio.run(broken.render())
    assert broken_body == "" and broken_payload is None


def test_registry_timeout_skips_provider() -> None:
    async def hangs() -> str:
        await asyncio.sleep(1)
        return "too late"

    registry = HostStateRegistry(timeout=0.01)
    registry.register("slow", hangs)

    body, payload = asyncio.run(registry.render())

    assert body == "" and payload is None
