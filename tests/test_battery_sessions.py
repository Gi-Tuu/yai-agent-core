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
    assert first.json()["final_text"] == "session=anonymous:a"
    assert second.json()["final_text"] == "session=anonymous:b"


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


def test_api_keys_are_separate_session_namespaces(monkeypatch) -> None:
    monkeypatch.setenv("YAI_GIT_COMMIT", "0" * 40)
    from yai_core.batteries.fastapi_server.auth import ApiKeyAuth

    seen: list[str] = []

    def factory(session_key: str):
        seen.append(session_key)
        return AgentCore(
            ScriptedModel([ModelResponse(content=f"session={session_key}")])
        )

    core = AgentCore(ScriptedModel([ModelResponse(content="default")]))
    app = create_app(
        core,
        core_factory=factory,
        auth=ApiKeyAuth(keys=("tenant-a", "tenant-b")),
    )
    with TestClient(app) as client:
        first = client.post(
            "/v1/agent/run",
            json={"task": "x", "session_id": "victim"},
            headers={"Authorization": "Bearer tenant-a"},
        )
        second = client.post(
            "/v1/agent/run",
            json={"task": "x", "session_id": "victim"},
            headers={"Authorization": "Bearer tenant-b"},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["final_text"] != second.json()["final_text"]
    assert len(seen) == 2


def test_active_session_is_not_evicted(monkeypatch) -> None:
    import asyncio
    from types import SimpleNamespace

    import httpx

    from yai_core.batteries.fastapi_server import RateLimitConfig
    from yai_core.types import Strategy

    class GateCore:
        def __init__(self, sid: str) -> None:
            self.sid = sid
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.closed = False

        async def run(self, _task: str):
            self.started.set()
            await self.release.wait()
            return SimpleNamespace(
                strategy=Strategy.DIRECT,
                final_text=f"ok:{self.sid}",
                events=[],
                status="ok",
            )

        async def aclose(self) -> None:
            self.closed = True

    monkeypatch.setenv("YAI_GIT_COMMIT", "0" * 40)
    cores: list[GateCore] = []

    def factory(sid: str) -> GateCore:
        core = GateCore(sid)
        cores.append(core)
        return core

    async def run() -> tuple[bool, int, int]:
        base = GateCore("default")
        base.release.set()
        app = create_app(
            base,
            core_factory=factory,
            max_sessions=1,
            rate_limit=RateLimitConfig(enabled=False),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            first = asyncio.create_task(
                client.post("/v1/agent/run", json={"task": "a", "session_id": "a"})
            )
            while not cores:
                await asyncio.sleep(0.01)
            await cores[0].started.wait()

            second = asyncio.create_task(
                client.post("/v1/agent/run", json={"task": "b", "session_id": "b"})
            )
            while len(cores) < 2:
                await asyncio.sleep(0.01)
            await cores[1].started.wait()

            first_closed = cores[0].closed
            cores[0].release.set()
            cores[1].release.set()
            one, two = await first, await second
            return first_closed, one.status_code, two.status_code

    first_closed, one, two = asyncio.run(run())
    assert first_closed is False
    assert (one, two) == (200, 200)
