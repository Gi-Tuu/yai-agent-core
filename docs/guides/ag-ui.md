# AG-UI 适配器（Agent ↔ 前端）

[English](ag-ui.en.md) · **简体中文**

YAI 的事件流只有唯一出口（`AgentCore.astream`）。本适配器把这条事件流翻译成
[AG-UI 1.0](https://docs.ag-ui.com/spec/1.0) 的 SSE 事件，让 React / CopilotKit 等
标准前端可以直接接一个 YAI Agent，不需要写 QML 或私有协议。

它是一个**外侧订阅者**，不是内核的一部分：

- 不修改 `kernel/` / `core.py`，也不新增事件流；
- 只依赖标准库（事件是 dict，SSE 是字符串）；
- 不内置 UI：它只输出标准事件，渲染是前端的事。

## 1. 映射表

| YAI 事件 | AG-UI 事件 | 字段 |
|---|---|---|
| run 开始 | `RUN_STARTED` | `threadId` / `runId` |
| `model_message` | `TEXT_MESSAGE_START` / `TEXT_MESSAGE_CONTENT` / `TEXT_MESSAGE_END` | `messageId`、`delta`、`role="assistant"` |
| `tool_call` | `TOOL_CALL_START` / `TOOL_CALL_ARGS` / `TOOL_CALL_END` | `toolCallId`、`toolCallName`、`delta`=JSON 参数 |
| `tool_result` | `TOOL_CALL_RESULT` | `messageId`、`toolCallId`、`content`、`role="tool"`、`metadata.ok` |
| `done` | `RUN_FINISHED` | `threadId` / `runId` / 可选 `result`（最终文本） |
| `error` | `RUN_ERROR` | `message`、`code`=stage |
| 其他（策略/计划/审批/澄清等） | `CUSTOM` | `name="yai.<event_type>"`、`value`=原始载荷 |

口径：能一一对应的走标准事件；**不能映射的 YAI 事件保留为 `CUSTOM`**，不丢信息，也不假装语义。
`permission_asked` / `clarify_requested` 目前就是 `CUSTOM`（AG-UI 的 interrupt/resume 生命周期尚未接）。

## 2. SSE 格式

AG-UI 1.0 的 HTTP+SSE binding：客户端 `POST` 一个 `RunAgentInput` JSON，响应
`text/event-stream`，每个 `data:` 恰好是一个完整事件（LF 行尾）：

```
data: {"type":"RUN_STARTED","threadId":"thr-1","runId":"run-1"}

data: {"type":"TEXT_MESSAGE_START","messageId":"msg-1","role":"assistant"}

data: {"type":"TEXT_MESSAGE_CONTENT","messageId":"msg-1","delta":"你好"}

data: {"type":"TEXT_MESSAGE_END","messageId":"msg-1"}

data: {"type":"RUN_FINISHED","threadId":"thr-1","runId":"run-1"}
```

`AgUiTranslator` 负责状态机（消息 id、tool call 关联、终止），`encode_sse()` 负责单帧编码，
`stream_sse()` 可直接喂给 FastAPI `StreamingResponse`。

## 3. 运行示例

```bash
uv sync --extra server
uv run uvicorn examples.host_ag_ui_sse.app:app --port 8000
```

```bash
curl -N -X POST http://127.0.0.1:8000/agent \
  -H 'Content-Type: application/json' \
  -H 'Accept: text/event-stream' \
  -d '{"threadId":"thr-1","runId":"run-1","messages":[{"id":"m1","role":"user","content":"你好"}]}'
```

示例用 `ScriptedModel`（离线、零 Key）；换成 `OpenAICompatProvider` 即可接真实模型。
前端侧用 `@ag-ui/client` 的 `HttpAgent` 指向 `http://127.0.0.1:8000/agent` 即可。

## 4. 代码入口

```python
from yai_core.integrations.ag_ui import AgUiTranslator, encode_sse

translator = AgUiTranslator(thread_id="thr-1", run_id="run-1")
for event in translator.start():
    yield encode_sse(event)
async for agent_event in core.astream("你好"):
    for event in translator.translate(agent_event):
        yield encode_sse(event)
for event in translator.finish():
    yield encode_sse(event)
```

## 5. 诚实边界

- **不是 token 级流式**：YAI 的 `model_message` 是完整文本，适配器一次 `CONTENT` 发出；
  真正的逐 token 流式要等内核模型层暴露增量事件（当前不在范围）。
- **tool result 是受限预览**：`preview` 是内核已截断的结果（默认上限），不是完整输出。
- **approval / clarify 未接 AG-UI interrupt**：目前是 `CUSTOM`；接 interrupt/resume 需要
  前端 ↔ 适配器的 resume 契约，留作后续。
- **不发 `STATE_SNAPSHOT` / `MESSAGES_SNAPSHOT`**：YAI 的会话历史由宿主管理，适配器不复制一份。
- 适配器只验证了「事件映射 + SSE 帧 + 示例宿主」；未在真实 CopilotKit 前端做端到端验收。

## 6. 测试

```bash
uv run pytest tests/test_ag_ui_adapter.py -q
```

覆盖：生命周期、文本消息、tool call 关联、失败 tool result、CUSTOM 映射、错误终止幂等、
SSE 单帧格式、FastAPI 示例宿主（TestClient 离线）。
