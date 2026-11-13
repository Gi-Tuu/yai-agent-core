> English translation of [中文](README.md) · The Chinese version is canonical; if they disagree, the Chinese wins.

# host_ag_ui_sse · AG-UI SSE host example

**English** · [简体中文](README.md)

A FastAPI host that translates YAI's event stream into [AG-UI 1.0](https://docs.ag-ui.com/spec/1.0)
SSE, so frontends (CopilotKit / `@ag-ui/client`) can `POST /agent` and consume `text/event-stream`.

```bash
uv sync --extra server
uv run uvicorn examples.host_ag_ui_sse.app:app --port 8000

curl -N -X POST http://127.0.0.1:8000/agent \
  -H 'Content-Type: application/json' \
  -H 'Accept: text/event-stream' \
  -d '{"threadId":"thr-1","runId":"run-1","messages":[{"id":"m1","role":"user","content":"hello"}]}'
```

- The example uses `ScriptedModel` (offline, no key); swap in `OpenAICompatProvider` for a real model;
- Mapping rules, SSE framing and boundaries: [`docs/guides/ag-ui.md`](../../docs/guides/ag-ui.md);
- Boundaries: not token-level streaming, tool results are bounded previews, approval/clarify are
  currently `CUSTOM` events.
