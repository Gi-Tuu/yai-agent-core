> English translation of [中文](README.md) · The Chinese version is canonical; if they disagree, the Chinese wins.

# A2A host adapter example

**English** · [简体中文](README.md)

Wrap a remote **A2A 1.0** agent as an ordinary `ToolSpec` and hand it to `AgentCore`; the kernel
never knows A2A exists. This is the runnable form of the "host-layer adapter" in
[`a2a-positioning.en.md`](../../docs/guides/a2a-positioning.en.md).

```bash
uv run python examples/host_a2a_adapter/demo.py
```

```text
agent card: Fake Echo Agent (1.0.0) -> http://127.0.0.1:58126/a2a
tool result: echo: hello from YAI
final: A2A delegate finished.
```

## Layout

| File | Purpose |
|---|---|
| `a2a_client.py` | Minimal A2A JSON-RPC client + `as_tool()` |
| `fake_a2a_server.py` | Offline fake A2A server (Agent Card / SendMessage / GetTask) |
| `demo.py` | fake server → ToolSpec → `AgentCore` + `ScriptedModel` real tool call |

## Real remote

```python
from examples.host_a2a_adapter.a2a_client import A2AClient, A2AConfig

client = A2AClient(
    A2AConfig(base_url="https://agent.example.com", token_env="A2A_TOKEN")
)
core.register_tools([client.as_tool(name="a2a_research")])
```

Supported: Agent Card, `SendMessage` / `GetTask` (non-streaming), `SendStreamingMessage` (SSE),
text/structured parts, Bearer token. Streaming: `client.stream(task, on_text=...)` receives chunks;
with `A2AConfig(streaming=True)` the `as_tool()` path uses streaming too.

### OAuth2 (client credentials)

```python
from examples.host_a2a_adapter.a2a_client import A2AClient, A2AConfig, A2AOAuth2

client = A2AClient(A2AConfig(
    base_url="https://agent.example.com",
    oauth2=A2AOAuth2(
        token_url="https://auth.example.com/oauth/token",
        client_id="yai",
        client_secret_env="A2A_CLIENT_SECRET",
        scope="a2a",
    ),
))
```

### Authorization Code / Device Code

```python
from examples.host_a2a_adapter.a2a_client import (
    A2AClient, A2AConfig, A2AOAuth2AuthorizationCode, A2AOAuth2DeviceCode,
)

# 1) authorization code: host opens the URL and receives the callback code
client = A2AClient(A2AConfig(
    base_url="https://agent.example.com",
    oauth2=A2AOAuth2AuthorizationCode(
        authorization_url="https://auth.example.com/authorize",
        token_url="https://auth.example.com/oauth/token",
        client_id="yai",
        redirect_uri="http://127.0.0.1:8765/callback",
        scope="a2a",
    ),
))
print(client.authorization_url("state-123"))
client.exchange_code(code, state="state-123")  # PKCE S256 + state check

# 2) device code: the user visits verification_uri and enters user_code
client = A2AClient(A2AConfig(
    base_url="https://agent.example.com",
    oauth2=A2AOAuth2DeviceCode(
        device_authorization_url="https://auth.example.com/device",
        token_url="https://auth.example.com/oauth/token",
        client_id="yai",
    ),
))
flow = client.start_device_flow()
print(flow["verification_uri"], flow["user_code"])
client.poll_device_token(flow["device_code"], interval=flow.get("interval", 5))
```

Both flows only **obtain and cache a token**; user interaction (browser/device) remains host-owned.
The authorization-code flow generates a PKCE S256 challenge and `exchange_code(state=...)` validates
state; the callback server and device page remain host-owned.

### Push configuration

`create_push_config(task_id, webhook_url, token=...)` / `list_push_configs` /
`delete_push_config` call A2A push-config methods; **the webhook receiver is host-owned**; the
example only registers/lists/deletes configs.

Not supported: a push receiver, file parts, or Agent Card signature verification;
authorization-code/device flows only exchange tokens (no browser/callback server).

## Boundaries

- **HTTPS only**; `http` is allowed on loopback for the local fake server;
- Remote output is untrusted: never treat it as system instructions; permissions/approvals/budgets
  remain governed by the kernel;
- Timeouts and max wait are configurable; remote failures become normal tool failures;
- This is host-layer code and **does not enter `src/yai_core/`**.
