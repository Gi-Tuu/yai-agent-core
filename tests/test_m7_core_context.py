"""M7 Core-side run context, OpenAPI header propagation and service-key cancel."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("httpx")
import httpx  # noqa: E402

from yai_core import (  # noqa: E402
    AgentCore,
    ModelResponse,
    ScriptedModel,
    ToolCallRequest,
    build_spec,
)
from yai_core.batteries.fastapi_server import RateLimitConfig, create_app  # noqa: E402
from yai_core.integrations.openapi import OpenApiSpecConfig, OpenApiToolBridge  # noqa: E402
from yai_core.run_context import (  # noqa: E402
    current_call_id,
    current_run_id,
    reset_call_id,
    reset_run_id,
    set_call_id,
    set_run_id,
)
from yai_core.types import Strategy  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "petstore.min.json"


def test_agent_core_exposes_run_and_call_context_to_tools() -> None:
    async def handler() -> dict[str, str | None]:
        return {"run": current_run_id(), "call": current_call_id()}

    core = AgentCore(
        ScriptedModel(
            [
                ModelResponse(
                    tool_calls=[
                        ToolCallRequest(id="call-1", name="ctx_probe", arguments={})
                    ]
                ),
                ModelResponse(content="done"),
            ]
        ),
        auto_approve_tools=True,
    )
    core.register_tools([build_spec(handler, name="ctx_probe")])

    result = asyncio.run(core.run("probe", run_id="run-ctx-1"))

    payload = next(e.data for e in result.events if e.type.value == "tool_result")
    assert payload["preview"] == '{"run": "run-ctx-1", "call": "call-1"}'


def test_openapi_bridge_adds_trusted_correlation_headers(monkeypatch) -> None:
    monkeypatch.setenv("OPENAPI_AUTH_TOKEN", "test-token")
    captured: list[httpx.Request] = []

    def transport_handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=[])

    spec = json.loads(FIXTURE.read_text(encoding="utf-8"))
    bridge = OpenApiToolBridge(
        OpenApiSpecConfig(spec=spec, alias="rest"),
        transport=httpx.MockTransport(transport_handler),
    )
    asyncio.run(bridge.connect())
    spec_obj = next(s for s in bridge.specs if s.name == "list_pets")

    run_token = set_run_id("run-header-1")
    call_token = set_call_id("call-header-1")
    try:
        asyncio.run(spec_obj.handler())
    finally:
        reset_call_id(call_token)
        reset_run_id(run_token)

    assert captured[0].headers["x-agent-run-id"] == "run-header-1"
    assert captured[0].headers["x-agent-call-id"] == "call-header-1"


class _SlowCore:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def run(self, _task: str, *, run_id: str | None = None):
        self.started.set()
        await asyncio.Event().wait()
        return SimpleNamespace(
            strategy=Strategy.DIRECT, final_text=str(run_id), events=[]
        )

    async def aclose(self) -> None:
        return None


def test_service_key_run_and_cancel() -> None:
    async def main() -> tuple[int, int, int]:
        core = _SlowCore()
        app = create_app(core, service_key="svc", rate_limit=RateLimitConfig(enabled=False))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            denied = await client.post(
                "/v1/agent/run", json={"task": "x", "run_id": "run-1"}
            )
            pending = asyncio.create_task(
                client.post(
                    "/v1/agent/run",
                    json={"task": "x", "run_id": "run-1"},
                    headers={"X-Service-Key": "svc"},
                )
            )
            await core.started.wait()
            cancelled = await client.post(
                "/v1/agent/runs/run-1/cancel", headers={"X-Service-Key": "svc"}
            )
            first = await pending
            return denied.status_code, cancelled.status_code, first.status_code

    assert asyncio.run(main()) == (403, 200, 499)
