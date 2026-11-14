"""A2A host adapter: offline fake server regression."""

from __future__ import annotations

import asyncio
import json

import pytest
from examples.host_a2a_adapter.a2a_client import (
    A2AClient,
    A2AConfig,
    A2AOAuth2,
    A2AOAuth2AuthorizationCode,
    A2AOAuth2DeviceCode,
    _validate_endpoint,
)
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
    assert tool.source == "native"
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

def test_streaming_collects_artifact_chunks(server) -> None:
    _srv, base = server
    client = A2AClient(A2AConfig(base_url=base, timeout=5))
    chunks: list[str] = []
    result = client.stream("hello stream", on_text=chunks.append)
    assert result.state == "COMPLETED"
    assert "stream part 1" in result.text
    assert "stream part 2" in result.text
    assert chunks == ["stream part 1", "stream part 2"]


def test_streaming_config_is_used_by_tool(server) -> None:
    _srv, base = server
    client = A2AClient(A2AConfig(base_url=base, timeout=5, streaming=True))
    tool = client.as_tool(name="a2a_stream")
    result = asyncio.run(tool.handler(task="hello"))
    assert "stream part 1" in result
    assert "stream part 2" in result

def test_oauth_client_credentials(server, monkeypatch: pytest.MonkeyPatch) -> None:
    srv, base = server
    monkeypatch.setenv("A2A_CLIENT_SECRET", "s3cret")
    client = A2AClient(
        A2AConfig(
            base_url=base,
            oauth2=A2AOAuth2(
                token_url=f"{base}/oauth/token",
                client_id="demo",
                client_secret_env="A2A_CLIENT_SECRET",
                scope="a2a",
            ),
            poll_interval=0.01,
            max_wait=5,
        )
    )
    assert client.send("hello").text
    assert srv.last_auth == "Bearer token-123"
    assert "grant_type=client_credentials" in (srv.last_oauth_form or "")
    assert "scope=a2a" in (srv.last_oauth_form or "")


def test_push_notification_config_lifecycle(server) -> None:
    _srv, base = server
    client = A2AClient(A2AConfig(base_url=base, poll_interval=0.01, max_wait=5))
    created = client.create_push_config("task-1", f"{base}/webhook", token="hook-token")
    config = created["pushNotificationConfig"]
    assert config["url"].endswith("/webhook")
    assert config["token"] == "hook-token"
    listed = client.list_push_configs("task-1")
    assert listed["configs"][0]["id"] == config["id"]
    client.delete_push_config("task-1", config["id"])
    assert client.list_push_configs("task-1")["configs"] == []

def test_authorization_code_flow(server, monkeypatch: pytest.MonkeyPatch) -> None:
    _srv, base = server
    client = A2AClient(
        A2AConfig(
            base_url=base,
            oauth2=A2AOAuth2AuthorizationCode(
                authorization_url=f"{base}/authorize",
                token_url=f"{base}/oauth/token",
                client_id="demo",
                redirect_uri="http://127.0.0.1/callback",
                scope="a2a",
            ),
        )
    )
    url = client.authorization_url("state-1")
    assert "response_type=code" in url
    assert "state=state-1" in url
    client.exchange_code("code-1")
    assert client.send("hello").text
    assert client._oauth_token == "token-authcode"  # noqa: SLF001


def test_device_code_flow(server) -> None:
    _srv, base = server
    client = A2AClient(
        A2AConfig(
            base_url=base,
            oauth2=A2AOAuth2DeviceCode(
                device_authorization_url=f"{base}/oauth/device",
                token_url=f"{base}/oauth/token",
                client_id="demo",
            ),
        )
    )
    flow = client.start_device_flow()
    assert flow["user_code"] == "ABCD-EFGH"
    client.poll_device_token(flow["device_code"], interval=0.01, timeout=5)
    assert client.send("hello").text
    assert client._oauth_token == "token-device"  # noqa: SLF001
