"""FastAPI 多会话隔离：同一进程内按 session_id 拿独立 core。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from yai_core import AgentCore, ModelResponse, ScriptedModel
from yai_core.batteries.fastapi_server import create_app


def _factory(session_id: str) -> AgentCore:
    return AgentCore(
        ScriptedModel([ModelResponse(content=f"session={session_id}")])
    )


def test_sessions_get_distinct_cores(monkeypatch) -> None:
    monkeypatch.setenv("YAI_GIT_COMMIT", "0" * 40)
    monkeypatch.setenv("YAI_PROJECT_SLUG", "yai-test")
    core = AgentCore(ScriptedModel([ModelResponse(content="default")]))
    with TestClient(
        create_app(core, core_factory=_factory)
    ) as client:
        first = client.post(
            "/v1/agent/run", json={"task": "hi", "session_id": "a"}
        )
        second = client.post(
            "/v1/agent/run", json={"task": "hi", "session_id": "b"}
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["final_text"] == "session=a"
    assert second.json()["final_text"] == "session=b"


def test_session_id_is_required_with_factory(monkeypatch) -> None:
    monkeypatch.setenv("YAI_GIT_COMMIT", "0" * 40)
    core = AgentCore(ScriptedModel([ModelResponse(content="default")]))
    with TestClient(create_app(core, core_factory=_factory)) as client:
        resp = client.post("/v1/agent/run", json={"task": "hi"})

    assert resp.status_code == 400
    assert "session_id" in resp.json()["detail"]


def test_session_id_rejected_without_factory(monkeypatch) -> None:
    monkeypatch.setenv("YAI_GIT_COMMIT", "0" * 40)
    core = AgentCore(ScriptedModel([ModelResponse(content="default")]))
    with TestClient(create_app(core)) as client:
        resp = client.post(
            "/v1/agent/run", json={"task": "hi", "session_id": "a"}
        )

    assert resp.status_code == 400
    assert "core_factory" in resp.json()["detail"]


def test_invalid_session_id_rejected(monkeypatch) -> None:
    monkeypatch.setenv("YAI_GIT_COMMIT", "0" * 40)
    core = AgentCore(ScriptedModel([ModelResponse(content="default")]))
    with TestClient(create_app(core, core_factory=_factory)) as client:
        resp = client.post(
            "/v1/agent/run", json={"task": "hi", "session_id": "../escape"}
        )

    assert resp.status_code == 400
