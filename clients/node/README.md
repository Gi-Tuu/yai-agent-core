# yai-agent-core-sidecar-client（Node / TypeScript）

YAI Agent Core **sidecar 会话协议**的类型化 Node 客户端：stdio NDJSON + JSON-RPC 2.0。
零运行时依赖（只用 Node 内置模块），Node ≥ 20。

> 边界：这个客户端只驱动**会话面**（initialize / run / cancel / 审批 / 澄清 / 事件）。
> 协议里没有 `tool/call`：宿主工具必须经 MCP 或 OpenAPI 暴露，sidecar 作为客户端去接。
> 详见 [`docs/guides/multilang-interop.md`](../../docs/guides/multilang-interop.md) 与
> 讲义 `docs/walkthrough/54-sidecar-protocol.md`。

## 安装与构建

仓库内（尚未发布到 npm）：

```bash
cd clients/node
npm install
npm run build      # 产物在 dist/
```

## 用法

```ts
import { SidecarClient } from "./dist/index.js"; // 或包名 yai-agent-core-sidecar-client

const client = SidecarClient.spawn({
  // 默认：YAI_PYTHON / python -m yai_core.sidecar
  env: { PYTHONPATH: "/path/to/yai-agent-core/src" },
  onApproval: (request) => {
    console.log("审批：", request.tool, request.arguments);
    return true; // 返回 false 即拒绝；不传处理器时默认拒绝（fail closed）
  },
  onEvent: (event) => {
    if (event.type === "tool_call") console.log("调用", event.data.tool);
  },
});

try {
  const init = await client.initialize({
    model: { model: "deepseek-chat" }, // 省略 api_key 时由后端读环境变量
    openapi: [{ alias: "crm", url: "http://127.0.0.1:8080/openapi.json", read_only: true }],
  });
  console.log("工具：", init.tools.map((tool) => tool.name));

  const { run_id } = await client.run("查一下客户王敏");
  const done = await client.waitForEvent((event) => event.type === "done");
  console.log(run_id, done.data.final_text);
} finally {
  await client.shutdown().catch(() => {});
  client.close();
}
```

## API

| 方法 | 说明 |
|---|---|
| `SidecarClient.spawn(options)` | 拉起 sidecar 进程（`command` 可整体覆盖，测试用） |
| `initialize(params)` | 配模型 / MCP / OpenAPI / 权限；返回工具清单 |
| `run(task)` | 启动任务，立即返回 `run_id`；事件随后流式到达 |
| `cancel()` / `ping()` / `shutdown()` | 取消当前任务 / 存活探针 / 停机 |
| `onEvent(fn)` | 订阅 `agent/event`，返回退订函数 |
| `waitForEvent(predicate, timeoutMs?)` | 等首个匹配事件（先查缓冲区） |
| `request(method, params, timeoutMs?)` | 协议扩展入口 |
| `close()` | 关闭读循环并终止子进程（幂等） |

选项：`onApproval` / `onClarify` / `onEvent` / `onNotification` / `onStderr` /
`requestTimeoutMs`（默认 60s）/ `eventBufferSize`（默认 256）。

## 失败语义

- 审批未配置处理器或处理器抛错 → 按**拒绝**回答；澄清失败 → 空回答；
- sidecar 进程退出 → 所有未决请求与等待者以 `SidecarError`（code `-32003`）失败；
- 请求超时 → `SidecarError`（code `-32000`）；
- 协议错误原样透传 sidecar 的 `code` / `message` / `data`。

## 测试

```bash
cd clients/node
npm test        # 构建 + node --test（用假 sidecar fixture，离线、零 Key）
```

CI：`.github/workflows/client-node.yml`（Node 22，`npm ci && npm test`）。
