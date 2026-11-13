# host_ag_ui_sse · AG-UI SSE 宿主示例

[English](README.en.md) · **简体中文**

用 FastAPI 把 YAI 的事件流翻译成 [AG-UI 1.0](https://docs.ag-ui.com/spec/1.0) 的 SSE，
前端（CopilotKit / `@ag-ui/client`）可以直接 `POST /agent` 并消费 `text/event-stream`。

```bash
uv sync --extra server
uv run uvicorn examples.host_ag_ui_sse.app:app --port 8000

curl -N -X POST http://127.0.0.1:8000/agent \
  -H 'Content-Type: application/json' \
  -H 'Accept: text/event-stream' \
  -d '{"threadId":"thr-1","runId":"run-1","messages":[{"id":"m1","role":"user","content":"你好"}]}'
```

- 示例模型：`ScriptedModel`（离线、零 Key）；换成 `OpenAICompatProvider` 即真实模型；
- 映射规则、SSE 帧格式与边界见 [`docs/guides/ag-ui.md`](../../docs/guides/ag-ui.md)；
- 边界：非 token 流式、tool result 为受限预览、approval/clarify 目前是 `CUSTOM` 事件。
