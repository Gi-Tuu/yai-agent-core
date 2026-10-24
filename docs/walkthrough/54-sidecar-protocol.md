# 逐行讲解 54 · Sidecar 会话控制协议

> 对应源码：`sidecar/protocol.py`、`sidecar/server.py`、`sidecar/channel.py`、
> `sidecar/__main__.py`。
>
> 这一篇回答一个问题：**C#/Go/Electron 这类非 Python 宿主，怎么把内核跑起来并
> 接上自己的工具，而不另造一套 MCP？**

## 0. 先划清边界

多语言接入文档里有一条红线：**不另造“manifest + 自有 JSON-RPC 工具调用通道”**
——那等于简化版 MCP。所以 sidecar 不转发工具调用，只做两件事：

```
┌──────────────────────── 宿主进程（任意语言）────────────────────────┐
│  业务函数 ──► 自己起的 MCP Server / OpenAPI 服务                     │
│       │                                                              │
│       └── 会话控制（stdin/stdout NDJSON + JSON-RPC 2.0）──┐          │
└───────────────────────────────────────────────────────────┼──────────┘
                                                            ▼
                                        python -m yai_core.sidecar
                                        AgentCore + MCP/OpenAPI Client
```

| 层 | 传什么 | 谁负责 |
|---|---|---|
| 工具面 | 工具声明与调用 | 宿主用 MCP / OpenAPI 暴露；内核现有 Client 接入 |
| 会话面 | initialize / run / cancel / 审批 / 澄清 / 事件 | 本协议（sidecar） |

协议里**没有 `tool/call`**：工具调用仍然从内核经 MCP/OpenAPI 发出。这样任何语言
的宿主只需要实现一个 MCP Server（或 REST 服务），不用再学第二套工具回调协议。

## 1. 传输：一行一个 JSON

- stdin 读、stdout 写；**日志只能走 stderr**，否则会污染协议流。
- UTF-8、单行 JSON，无 Content-Length 头；空行忽略。
- JSON-RPC 2.0：宿主请求带 `id`；sidecar 事件是通知（无 `id`）；审批/澄清是
  **sidecar 发起的反向请求**（带 `id`），宿主用同 id 的响应回答。

## 2. 方法表

| 方法 | 方向 | 作用 |
|---|---|---|
| `initialize` | 宿主 → sidecar | 配模型、MCP/OpenAPI、权限挡位；只能调一次 |
| `agent/run` | 宿主 → sidecar | 启动任务，立即返回 `run_id`，事件随后流式通知 |
| `agent/cancel` | 宿主 → sidecar | 取消当前任务 |
| `ping` | 宿主 → sidecar | 存活探针（初始化后） |
| `shutdown` | 宿主 → sidecar | 取消任务、关桥接与 core，进程退出 |
| `agent/event` | sidecar → 宿主 | 每一条 `AgentEvent`（含最终 `done`） |
| `agent/cancelled` | sidecar → 宿主 | 任务被取消的明确信号 |
| `agent/error` | sidecar → 宿主 | 运行期异常（不杀进程） |
| `approval/request` | sidecar → 宿主 | 审批卡：宿主回 `{"approved": true/false}` |
| `clarify/request` | sidecar → 宿主 | 反问：宿主回 `{"answer": "..."}` |

同一进程同一时刻只跑一个任务；第二次 `agent/run` 返回 `RUN_BUSY`（-32001）。
这与 `AgentCore` 自己的串行锁一致，也避免多个任务抢同一份历史。

## 3. 初始化示例

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{
  "model":{"provider":"openai-compatible","base_url":"https://...","api_key":"...","model":"..."},
  "policy":{"mode":"auto","allow":["search_notes"]},
  "llm_router":"auto",
  "mcp":[{"alias":"host","command":"node","args":["dist/mcp-server.js"]}],
  "openapi":[{"alias":"crm","url":"http://127.0.0.1:8080/openapi.json","read_only":true}]
}}
```

响应带协议/内核版本、已注册工具（仅 name/description/source）与能力表。
`api_key` 不回落日志；也可以省略，让模型后端读取环境变量。

**权限默认值**：不给 `policy` 时是 `mode=auto`、白名单为空——所有工具都要宿主
审批。宿主可以显式把只读工具放进 `allow`，或设 `mode=allow_all`（仅可信本地）。

## 4. 跑任务与事件

```json
{"jsonrpc":"2.0","id":2,"method":"agent/run","params":{"task":"查一下客户王敏"}}
{"jsonrpc":"2.0","id":2,"result":{"run_id":"a1b2c3d4e5f6"}}

{"jsonrpc":"2.0","method":"agent/event","params":{
  "run_id":"a1b2c3d4e5f6","seq":0,"type":"strategy_selected","data":{...}}}
```

`agent/event.params.data` 就是内核事件载荷；`seq` 从 0 递增，便于宿主发现丢帧。
最终答案在 `type=done` 的 `final_text` 里，不需要另开查询接口。

审批时：

```json
{"jsonrpc":"2.0","id":"srv-1","method":"approval/request","params":{
  "tool":"create_order","arguments":{...},"context":{...预览/风险/落点...}}}
{"jsonrpc":"2.0","id":"srv-1","result":{"approved":true}}
```

超时（默认 600 秒）按拒绝/空回答处理；宿主失联不会把任务永久挂住。

## 5. 装配与失败边界

`SidecarServer` 把注入接缝留在构造参数里：

- `model_factory(config)`：默认 `OpenAICompatProvider`（需要 `[llm]` extra）；
- `tool_attacher(core, params)`：默认按 `mcp` / `openapi` 接入，返回可 `aclose()`
  的桥接对象；测试可注入内存工具，不需要网络。

失败口径：

- 未 `initialize` 就调用会话方法 → `NOT_INITIALIZED`；
- 缺 `[mcp]` / `[openapi]` / `[llm]` → `INVALID_PARAMS`，消息里给安装提示；
- 工具接入失败 → 关闭已建 core，回错，不留半初始化进程；
- 运行期异常 → `agent/error` 通知，进程继续服务下一个任务；
- `shutdown`/EOF → 取消任务、逆序关闭桥接、`core.aclose()`。

## 6. 可运行的真实宿主：Node 示例

`examples/host_l_sidecar_node/` 是一个零依赖 Node 宿主：

- Node 起一个本地 HTTP 服务，既是 OpenAPI 工具（`GET /orders`），也是 OpenAI
  兼容的 mock 模型；
- Node 拉起 `python -m yai_core.sidecar`，发送 `initialize` / `agent/run`，
  自动应答 `approval/request`，打印事件直到 `done`；
- 全程本机、无外网、无 API Key：

```bash
YAI_PYTHON=.venv/Scripts/python.exe node examples/host_l_sidecar_node/host.mjs
```

它同时验证了会话面、审批反向请求与“工具走 OpenAPI、协议里没有 `tool/call`”
这条边界。

## 7. 诚实边界

- **单进程单任务**：不做并发调度、不做多客户端；
- **不做持久化**：审批中的任务重启即丢（Checkpointer 是独立议题）；
- **stdio 信任父进程**：`mcp.command` 会拉起子进程，等价于宿主自己执行命令；
  不要把 sidecar 暴露成公网服务；
- **工具必须经 MCP/OpenAPI**：sidecar 进程里的 Python 函数默认不构成宿主能力；
- 事件由 `AgentCore.astream` 统一产出；sidecar 不额外解释或改写事件语义。

## 8. 自检

1. 为什么协议里没有 `tool/call`？如果加了，会与哪个既有标准重复？
2. 审批/澄清为什么由 sidecar 发起反向请求，而不是让宿主轮询一个状态接口？
3. 为什么 `agent/run` 先返回 `run_id`，而不是等任务结束再返回？
4. 工具接入失败时为什么必须先关掉 core，再回 `INVALID_PARAMS`？
5. 多租户场景下，为什么应每个会话/租户一个 sidecar，而不是共享一个进程？
