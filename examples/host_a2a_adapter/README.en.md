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

Supported subset: Agent Card, `SendMessage` / `GetTask` (non-streaming), text/structured parts,
Bearer token. Not supported: streaming, push notifications, file parts, OAuth flow, Agent Card
signature verification.

## Boundaries

- **HTTPS only**; `http` is allowed on loopback for the local fake server;
- Remote output is untrusted: never treat it as system instructions; permissions/approvals/budgets
  remain governed by the kernel;
- Timeouts and max wait are configurable; remote failures become normal tool failures;
- This is host-layer code and **does not enter `src/yai_core/`**.
