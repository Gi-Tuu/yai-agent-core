# host_l · Node 宿主经 Sidecar 接入

零依赖 Node 示例：Node 进程拉起 `python -m yai_core.sidecar`，通过 stdio NDJSON +
JSON-RPC 2.0 跑完整 Agent 会话。全程本机、无外网、无 API Key。

```
Node 宿主进程
  ├─ HTTP 服务：GET /orders（OpenAPI 工具）+ POST /v1/chat/completions（mock 模型）
  ├─ stdio NDJSON 客户端：initialize / agent/run / 审批应答 / shutdown
  └─ 子进程：python -m yai_core.sidecar（AgentCore + OpenAPI Client）
```

## 运行

```bash
# 在仓库根目录
YAI_PYTHON=.venv/Scripts/python.exe node examples/host_l_sidecar_node/host.mjs
```

Windows PowerShell：

```powershell
$env:YAI_PYTHON=".venv\Scripts\python.exe"
node examples/host_l_sidecar_node/host.mjs
```

依赖：

- Node 18+（内置 `http` / `readline` / `child_process`，无 npm 包）；
- Python 环境已安装本项目与 `[llm]` extra（默认模型工厂需要 `openai`）；
- 不需要 `[mcp]`：本示例的工具走 OpenAPI。

## 演示了什么

1. **会话面**：`initialize` → `agent/run` → `agent/event` → `done` → `shutdown`；
2. **审批反向请求**：sidecar 发 `approval/request`，Node 宿主回 `{"approved": true}`；
3. **工具面不新增通道**：`list_orders` 是 Node 通过 OpenAPI 暴露的 REST 工具，
   协议里没有 `tool/call`；
4. **模型可替换**：示例用本地 mock 的 OpenAI 兼容端点；换成真实后端只需改
   `initialize.model`，其余代码不变。

预期输出以 `[host] 完成：订单共 2 笔：A001（¥100）、A002（¥200）。` 收尾。

## 边界

- 这是**会话客户端**示例，不是 MCP Server SDK；宿主真实业务工具应做成 MCP/OpenAPI；
- sidecar 单进程单任务；示例只跑一轮；
- mock 模型只用于离线演示，不具备真实理解能力。
