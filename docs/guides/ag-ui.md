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
