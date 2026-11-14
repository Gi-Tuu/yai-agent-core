"""A2A host adapter: offline fake server regression."""

from __future__ import annotations

import asyncio
import json

import pytest
from examples.host_a2a_adapter.a2a_client import A2AClient, A2AConfig, _validate_endpoint
from examples.host_a2a_adapter.fake_a2a_server import serve


@pytest.fixture()
def server():
    srv, base = serve()
    try:
        yield srv, base
    finally:
        srv.shutdown()
        srv.server_close()


def test_fetch_card_and_delegate_as_tool(server) -> None:
    _srv, base = server
    client = A2AClient(A2AConfig(base_url=base, poll_interval=0.05, max_wait=5))
    card = client.fetch_card()
    assert card.name == "Fake Echo Agent"
    assert card.url.startswith("http://127.0.0.1:")
    tool = client.as_tool(name="a2a_echo")
    assert tool.name == "a2a_echo"
    assert tool.source == "a2a"
    result = asyncio.run(tool.handler(task="hello"))
    assert "echo: hello" in result


def test_polling_working_task(server) -> None:
    _srv, base = server
    client = A2AClient(A2AConfig(base_url=base, poll_interval=0.01, max_wait=5))
    result = client.send("slow task")
    assert result.state == "COMPLETED"
    assert "done after polling" in result.text


def test_bearer_token_is_sent(server, monkeypatch: pytest.MonkeyPatch) -> None:
    srv, base = server
    monkeypatch.setenv("A2A_TOKEN", "test-token")
    client = A2AClient(
        A2AConfig(base_url=base, token_env="A2A_TOKEN", poll_interval=0.01, max_wait=5)
    )
    assert client.send("hello").text
    assert srv.last_auth == "Bearer test-token"


def test_rejects_non_loopback_http() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        _validate_endpoint("http://example.com/a2a")
    assert _validate_endpoint("https://example.com/a2a")
    assert _validate_endpoint("http://127.0.0.1:1/a2a")


def test_message_result_variant(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, _size=-1):
            payload = {"result": {"message": {"parts": [{"text": "direct message"}]}}}
            return json.dumps(payload).encode()

    monkeypatch.setattr(
        "examples.host_a2a_adapter.a2a_client.urlopen", lambda *a, **k: FakeResponse()
    )
    client = A2AClient(A2AConfig(base_url="https://example.com"))
    # fetch_card also goes through urlopen, so stub card separately
    from examples.host_a2a_adapter import a2a_client as mod

    monkeypatch.setattr(
        mod.A2AClient,
        "fetch_card",
        lambda self: mod.A2AAgentCard(
            name="x", description="", version="1", url="https://example.com/a2a"
        ),
    )
    result = client.send("hi")
    assert result.text == "direct message"
