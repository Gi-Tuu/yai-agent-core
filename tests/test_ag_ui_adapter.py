"""AG-UI 1.0 适配器：事件映射、SSE 编码与示例宿主（离线）。"""

from __future__ import annotations

import json

import pytest

from yai_core.integrations.ag_ui import AgUiTranslator, encode_sse, stream_sse
from yai_core.types import AgentEvent, EventType


def _events(translator: AgUiTranslator, *events: AgentEvent) -> list[dict]:
    out = translator.start()
    for event in events:
        out.extend(translator.translate(event))
    out.extend(translator.finish())
    return out


def test_run_lifecycle_and_text_message() -> None:
    translator = AgUiTranslator(thread_id="t-1", run_id="r-1")
    events = _events(
        translator,
        AgentEvent(EventType.MODEL_MESSAGE, {"text": "hello"}),
        AgentEvent(EventType.DONE, {"final_text": "hello"}),
    )
    assert [event["type"] for event in events] == [
        "RUN_STARTED",
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
        "RUN_FINISHED",
    ]
    assert events[0] == {"type": "RUN_STARTED", "threadId": "t-1", "runId": "r-1"}
    assert events[2]["delta"] == "hello"
    assert events[4]["result"] == "hello"


def test_tool_call_events_are_correlated() -> None:
    translator = AgUiTranslator(thread_id="t", run_id="r")
    events = _events(
        translator,
        AgentEvent(EventType.TOOL_CALL, {"tool": "echo", "arguments": {"text": "hi"}}),
        AgentEvent(EventType.TOOL_RESULT, {"tool": "echo", "ok": True, "preview": "echo: hi"}),
        AgentEvent(EventType.DONE, {"final_text": "done"}),
    )
    types = [event["type"] for event in events]
    assert types.count("TOOL_CALL_START") == 1
    assert types.count("TOOL_CALL_ARGS") == 1
    assert types.count("TOOL_CALL_END") == 1
    assert types.count("TOOL_CALL_RESULT") == 1
    call_id = next(e["toolCallId"] for e in events if e["type"] == "TOOL_CALL_START")
    result = next(e for e in events if e["type"] == "TOOL_CALL_RESULT")
    assert result["toolCallId"] == call_id
    assert result["content"] == "echo: hi"
    assert result["metadata"] == {"ok": True}


def test_tool_args_are_json_and_single_line() -> None:
    translator = AgUiTranslator(thread_id="t", run_id="r")
    events = _events(
        translator,
        AgentEvent(EventType.TOOL_CALL, {"tool": "echo", "arguments": {"text": "你好"}}),
        AgentEvent(EventType.DONE, {}),
    )
    args = next(e for e in events if e["type"] == "TOOL_CALL_ARGS")
    assert json.loads(args["delta"]) == {"text": "你好"}
    assert "\n" not in args["delta"]


def test_failed_tool_result_uses_error_text() -> None:
    translator = AgUiTranslator(thread_id="t", run_id="r")
    events = _events(
        translator,
        AgentEvent(EventType.TOOL_CALL, {"tool": "echo", "arguments": {}}),
        AgentEvent(EventType.TOOL_RESULT, {"tool": "echo", "ok": False, "error": "boom"}),
        AgentEvent(EventType.DONE, {}),
    )
    result = next(e for e in events if e["type"] == "TOOL_CALL_RESULT")
    assert result["content"] == "boom"
    assert result["metadata"] == {"ok": False}


def test_unknown_yai_events_become_custom() -> None:
    translator = AgUiTranslator(thread_id="t", run_id="r")
    events = _events(
        translator,
        AgentEvent(EventType.STRATEGY_SELECTED, {"strategy": "react"}),
        AgentEvent(EventType.DONE, {}),
    )
    custom = next(e for e in events if e["type"] == "CUSTOM")
    assert custom["name"] == "yai.strategy_selected"
    assert custom["value"] == {"strategy": "react"}


def test_error_is_terminal_and_finish_is_idempotent() -> None:
    translator = AgUiTranslator(thread_id="t", run_id="r")
    events = translator.start()
    events += translator.translate(
        AgentEvent(EventType.ERROR, {"stage": "model", "error": "boom"})
    )
    assert events[-1]["type"] == "RUN_ERROR"
    assert events[-1]["code"] == "model"
    assert translator.finish() == []
    assert translator.translate(AgentEvent(EventType.MODEL_MESSAGE, {"text": "late"})) == []


def test_schema_focus_messages_are_not_user_facing() -> None:
    translator = AgUiTranslator(thread_id="t", run_id="r")
    assert translator.translate(
        AgentEvent(EventType.MODEL_MESSAGE, {"text": "schema", "kind": "schema_focus"})
    ) == []


def test_encode_sse_is_one_data_frame_with_lf() -> None:
    frame = encode_sse({"type": "RUN_STARTED", "threadId": "t", "runId": "r"})
    assert frame.startswith("data: {")
    assert frame.endswith("\n\n")
    assert frame.count("\n") == 2
    assert json.loads(frame[len("data: ") : -2])["type"] == "RUN_STARTED"


def test_stream_sse_concatenates_frames() -> None:
    frames = list(stream_sse([{"type": "RUN_STARTED"}, {"type": "RUN_FINISHED"}]))
    assert len(frames) == 2
    assert all(frame.endswith("\n\n") for frame in frames)


# ---------- 示例宿主（FastAPI TestClient；未装 server extra 时跳过） ----------


def test_example_app_streams_ag_ui_events() -> None:
    fastapi = pytest.importorskip("fastapi")
    assert fastapi is not None
    from examples.host_ag_ui_sse.app import app
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        response = client.post(
            "/agent",
            json={
                "threadId": "t-demo",
                "runId": "r-demo",
                "messages": [{"id": "m1", "role": "user", "content": "你好"}],
            },
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = response.text

    frames = [
        json.loads(line[len("data: ") :])
        for line in body.splitlines()
        if line.startswith("data: ")
    ]
    types = [frame["type"] for frame in frames]
    assert types[0] == "RUN_STARTED"
    assert "TEXT_MESSAGE_START" in types
    assert "TEXT_MESSAGE_CONTENT" in types
    assert types[-1] == "RUN_FINISHED"
    assert frames[-1]["threadId"] == "t-demo"
    assert frames[-1]["runId"] == "r-demo"
