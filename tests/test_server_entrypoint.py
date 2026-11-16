"""Sidecar entrypoint: health, 0-tool default and service-key shutdown."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from yai_core.server import build_app


def test_sidecar_health_zero_tools_and_guarded_shutdown(monkeypatch) -> None:
    monkeypatch.setenv("YAI_SIDECAR_OFFLINE", "1")
    monkeypatch.setenv("YAI_SERVICE_KEY", "svc-test")
    monkeypatch.delenv("OPENAPI_SPEC_URL", raising=False)
    monkeypatch.delenv("OPENAPI_SPEC_PATH", raising=False)
    monkeypatch.setenv("OPENAPI_ALLOW_WRITES", "false")

    app, _core = build_app()

    class _Server:
        should_exit = False

    server = _Server()
    app.state.yai_server = server

    async def run() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            health = await client.get("/health")
            assert health.status_code == 200
            ready = await client.get("/readyz")
            assert ready.status_code == 200
            tools = await client.get("/v1/tools")
            assert tools.status_code == 200
            assert tools.json() == []
            assert (await client.post("/v1/shutdown")).status_code == 403
            shutdown = await client.post(
                "/v1/shutdown", headers={"X-Service-Key": "svc-test"}
            )
            assert shutdown.status_code == 202
            assert server.should_exit is True

    import asyncio

    asyncio.run(run())


def test_sidecar_requires_model_without_explicit_offline(monkeypatch) -> None:
    monkeypatch.delenv("YAI_SIDECAR_OFFLINE", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        build_app()


def test_sidecar_opt_in_mounts_writable_openapi_tools(monkeypatch) -> None:
    monkeypatch.setenv("YAI_SIDECAR_OFFLINE", "1")
    monkeypatch.setenv("YAI_SERVICE_KEY", "svc-test")
    monkeypatch.setenv(
        "OPENAPI_SPEC_PATH",
        str(Path(__file__).parent / "fixtures" / "petstore.min.json"),
    )
    monkeypatch.setenv("OPENAPI_ALLOW_WRITES", "true")
    monkeypatch.setenv("OPENAPI_AUTH_TOKEN", "test-token")

    app, _core = build_app()

    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        tools = client.get("/v1/tools")
        assert tools.status_code == 200
        assert any(t["source"] == "openapi" for t in tools.json())
