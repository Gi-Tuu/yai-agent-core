# 有向父子核（Directed Federation）设计稿

[English](federation.en.md) · **简体中文**

> 状态：**Phase 1（进程内）、Phase 2（本机 IPC）、Phase 3（生态运维）已实现。**
> 决策背景与边界见 [ADR 0008](../adr/0008-directed-federation.md)。
> 一句话：**每个产品里嵌一个独立核；父产品里的核可以调用子产品里的核，反之无路可走。**

## 0. 已实现范围（Phase 1 / Phase 2 / Phase 3）

代码入口：`shell/federation/`；示例：`examples/host_m_federation/`；测试：`tests/test_federation.py`、
`tests/test_federation_ipc.py`。

- **Phase 1（进程内）**：子核工具子集注册为父核工具（`<prefix>__<tool>`）；grant 校验子核身份 /
  工具子集 / 过期；父核策略 + 子核策略双防线；深度、扇出、环检测；超时；失败以工具错误回流。
- **Phase 2（本机 IPC）**：`ChildServer`（loopback TCP + NDJSON JSON-RPC + 发现文件 0600 + token）与
  `LinkChild`（父核侧长连接、请求多路复用）；握手签发 **HMAC grant**（密钥只在子核进程内）；
  支持 `health` / `tools.list` / `tools.call`；跨进程调用把调用链注入环检测；超时 / 断连 / 非法 grant
  一律失败关闭。
- **Phase 3（生态运维）**：父核 `refresh_child()` 支持 manifest 热更新（IPC 侧配合
  `federation.tools.list` 的 `manifest_version`）；`ChildServer(edge_quota_per_minute=...)` 每 edge
  滑动窗口限流；父核 `register_child(max_calls=...)` + `edge_stats()` 统计调用/失败/耗时；
  `ManagedChild` 托管子进程（拉起 → 等发现文件 → 连接 → 重连 / 停止）。
- **Phase 3 尾项**：
  - **专员桥**：`shell/federation/specialists.py` 的 `build_specialist_child()` /
    `attach_specialists()` 把"应用对象"（桌面灵动岛专员的形态）的公开方法内省成子核工具，
    一行接入父核；Qt 面板本身的接线仍属宿主侧。
  - **usage / token 预算**：子核工具结果可带 `{"usage": {...}}`，父核按 edge 聚合到
    `edge_stats()` 的 `tokens` / `usage`；`register_child(max_tokens=...)` 超限即
    `edge_token_budget_exceeded`（失败关闭）。
  - **两个可安装子核示例**：`examples/child_cores/`（笔记 / 计算器），
    `hub_demo.py` 同时托管两个进程并跨核调用。
- **未实现**：跨核事务、守护式自动重启（进程死后需宿主决定 restart）、
  manifest 服务端主动推送（当前由父核主动 `refresh_child` 拉取）。
- **安全前提**：同 OS 用户；发现文件 0600 + token 防同机其他用户/进程；**不防 root / 调试器 / 内存抓取**。

### Phase 2 协议实现要点

| 项 | 实现 |
|---|---|
| 传输 | `127.0.0.1` 随机端口，NDJSON 每行一条 JSON-RPC 2.0 消息，单条 ≤ 1 MiB |
| 发现文件 | `<data>/yai-federation/<node_id>.json`：protocol / node_id / child_id / host / port / token / pid，权限 0600（Windows 上 chmod 语义受限，见 `_noop_chmod`） |
| 握手 | `federation.handshake`：校验协议版本 + token + 父核允许列表 → 返回 manifest 子集 + HMAC grant |
| 调用 | `federation.tools.call`：校验 grant 签名 / 过期 / 工具子集 / 子核身份；`deadline_ms` 可选 |
| 健康 | `federation.health`：进程存活 + 子核身份 + 协议版本 + 工具数 |
| 错误 | `grant_invalid` / `protocol_error` / `deadline_exceeded` / 连接断开 → 父核侧 `ChildUnavailable` 或工具失败回流 |

## 1. 角色与拓扑

```
父产品（主产品 A）                        次产品 1（文档）
┌─────────────────────────┐              ┌─────────────────────────┐
│ 宿主 UI / 业务逻辑        │              │ 宿主 UI / 业务逻辑        │
│  ┌───────────────────┐  │   grant A→1  │  ┌───────────────────┐  │
│  │ 父核 AgentCore     │──┼─────────────▶│  │ 子核 AgentCore     │  │
│  │ tools ⊆ 自己的+子核 │  │  JSON-RPC    │  │ tools = 文档能力    │  │
│  └───────────────────┘  │  (loopback)  │  └───────────────────┘  │
└─────────────────────────┘              └─────────────────────────┘
        ▲                                           ▲
        │ 另一个父产品 B 也可配置 A→1 的独立 grant    │ 子核独立可用：
        └───────────────────────────────────────────┘ 用户直接打开文档产品照常工作
```

规则：

1. **有向无环**：边 = 父 → 子调用权。一个核可同时是父与子（生态必然如此）。
2. **m:n 显式配置**：同一子核可被多个父核调用，每条边一份独立 grant / 审计 / 配额。
3. **默认禁止兄弟直连**：兄弟核的协作经共同父核路由（父核编排），不自动建立兄弟通道。
4. **独立优先**：子核在父核离线时仍完整可用；父核只"借用"子核声明的能力。

## 2. 组件与代码落点（宿主层，内核零改动）

| 组件 | 位置 | 职责 |
|---|---|---|
| `CoreNode` | `shell/federation/node.py` | 包一个 `AgentCore` + 自己的 `policy` / `manifest` / `subject`；独立生命周期 |
| `Federation` | `shell/federation/parent.py` | 父核侧：注册子核、拉 manifest、选工具子集、签发 grant、深度/扇出/环检测、预算聚合 |
| `ChildLink` | `shell/federation/link.py` | 父核侧长连接客户端（重连、关联 ID、超时、取消） |
| `ChildServer` | `shell/federation/server.py` | 子核侧：loopback JSON-RPC server + 发现文件 + grant 校验 + 调用子核工具 |
| 发现文件 | `%LOCALAPPDATA%/yai-federation/<product>.json` | pid / 端口 / 协议版本 / token（权限 0600） |

复用：`shell/employee` 的权限收窄、预算、并发闸；`manifest` 的能力清单格式；
sidecar 的 JSON-RPC 形状；Bridge 的发现文件 + token 模式。

## 3. 能力交换（manifest 子集）

- 子核启动后通过 `federation.handshake` 返回自己的 manifest（工具名、JSON Schema、governance 摘要、
  manifest 版本、协议版本）。
- 父核**显式选择子集**注册为工具（默认不暴露子核全部能力）：
  `doc.append_entry`、`doc.search` 这类名字前缀由父核命名，避免与自有工具冲突。
- 版本协商：子核 manifest 升级后父核可热更新；工具被移除/改名时父核标记 `child_tool_gone` 并停止路由。

## 4. Grant（调用凭证）

```json
{
  "v": 1,
  "edge": "atm-main:doc-app",
  "child_id": "doc-app@0.27.2",
  "tools": ["doc.append_entry", "doc.search"],
  "subject": {"id": "user-42", "tenant": "default"},
  "issued_at": "2026-11-09T14:00:00Z",
  "expires_at": "2026-11-09T15:00:00Z",
  "nonce": "b7f1…",
  "sig": "hmac-sha256(...)"
}
```

- 签发：子核侧生成**对称密钥**（写入发现文件，仅同用户可读）；父核通过本机发现文件获得 token 后，
  用自己的 `grant` 请求签名？——**不**。简化：发现文件里的 token 是"连接凭据"，grant 由子核在
  handshake 时按"父核身份 + 工具子集"签发，父核只持有 grant，不持有子核密钥。
- 校验：子核对每个 `tools.call` 校验 grant 签名 / 过期 / edge / 工具名 / subject 是否匹配；
  任一不符 → `grant_invalid` / `tool_not_granted`，失败关闭。
- 撤销：子核可随时 `child_policy_denied`（数据所有者否决）；父核侧也有自己的审批闸（双防线）。

## 5. 协议 v1（本机 IPC）

**传输**：`127.0.0.1` 随机端口 + NDJSON（每行一个 JSON-RPC 2.0 消息），长连接复用。
第一阶段用 loopback；协议与传输解耦，后续可替换为 POSIX UDS / Windows 命名管道，不改消息。

**方法**：

| 方法 | 方向 | 说明 |
|---|---|---|
| `federation.handshake` | 父 → 子 | 交换协议版本、父核身份、请求工具子集 → 返回 manifest 摘要 + grant |
| `federation.tools.list` | 父 → 子 | 当前可用的工具（按 grant 过滤） |
| `federation.tools.call` | 父 → 子 | 调一个子核工具（主体方法） |
| `federation.tools.cancel` | 父 → 子 | 取消进行中的调用（关联 ID） |
| `federation.health` | 父 → 子 | 心跳 / 版本 / 负载（轻量，不触发模型） |

**调用信封**：

```json
{
  "jsonrpc": "2.0", "id": "c-42",
  "method": "federation.tools.call",
  "params": {
    "grant": "…",
    "tool": "doc.append_entry",
    "arguments": {"title": "ATM 流水", "body": "…"},
    "subject": {"id": "user-42"},
    "idempotency_key": "atm-2026-11-09-0001",
    "deadline_ms": 5000,
    "budget_left": {"tokens": 8000, "tool_calls": 12},
    "trace_id": "0af7651916cd43dd8448eb211c80319c"
  }
}
```

**响应**：`{ok, result, evidence, receipts, usage}`；错误统一 `code/message/data`。
关键错误码：

| code | 含义 | 父核行为 |
|---|---|---|
| `child_unavailable` | 子核未运行 / 断连 | 降级，不重试写操作 |
| `grant_invalid` | grant 过期 / 签名错 / 不匹配 | 停止路由并告警，重签 |
| `tool_not_granted` | 工具不在 grant 子集 | 不重试 |
| `child_policy_denied` | 子核策略拒绝（含人工拒绝） | 把拒绝作为可见事件回传 |
| `deadline_exceeded` | 超时 | 取消并回报 |
| `cancelled` | 父核取消 | 幂等清理 |
| `idempotency_conflict` | 同一幂等键但参数不同 | 失败关闭 |

**效率与背压**：单连接并发上限默认 8；单消息 ≤ 256 KB（大结果走引用/分段，v2）；
子核侧有界队列，满则 `busy`；父核侧指数退避仅用于**只读**调用。

## 6. 治理继承

- **工具集**：父核只注册 grant 里的子集；模型看不到未授予能力。
- **主体**：`subject` 由父核注入（不来自模型参数），子核把它当作"此次调用代表谁"。
- **预算**：父核把剩余预算随请求下发；子核不得超用；树级预算由父核汇总（子核回报 usage）。
- **审批**：父核侧策略先判（可要求人工确认）；子核侧再判（数据所有者可拒绝）。
  两边都通过才执行；任一侧拒绝即失败关闭。
- **频率**：每 edge 独立限流；子核侧默认 60 次/分钟，可配置。

## 7. 审计与证据

- 子核返回 `receipts`（执行收据摘要：工具、参数哈希、结果哈希、时间、收据链头）。
- 父核把摘要并入本次工具调用的 `evidence`；`trace_id` 贯穿两层，OTel 侧可拼成一条 trace。
- **不承诺跨核事务**：写操作靠幂等键；部分成功必须显式回传（`partial: true` + 已完成步骤）。

## 8. 失败与恢复

- 子核重启 → 父核重连、重新 handshake、重新拉 manifest；旧 grant 失效需重签。
- 父核崩溃 → 子核无状态（连接断开即清理进行中调用），独立使用不受影响。
- 取消传播：父核取消 → `federation.tools.cancel` → 子核取消工具执行（同步 handler 只能止损）。
- 不自动重放写操作；只读调用可带幂等键安全重试。

## 9. 安全边界（必须写进文档）

- 前提：父子核在**同一 OS 用户**下；发现文件 0600、端口仅绑定 127.0.0.1。
- 防的是：同机**其他用户/其他进程**冒用连接（token + 文件权限）。**不防** root、调试器、内存抓取。
- 日志脱敏：grant / token / 参数中的敏感字段按既有的 `sensitive` 声明处理。
- 子核的独立策略永远优先：父核的 grant 不是"绕过子核审批"的通行证。

## 10. 性能目标与验证方式

| 指标 | 目标 | 验证 |
|---|---|---|
| 同进程直调 | p50 < 0.1 ms | 单元基准 |
| 本机 IPC（心跳/只读工具） | p50 < 2 ms、p95 < 5 ms | 离线基准（loopback） |
| 断连恢复 | < 1 s 重连并重新 handshake | 集成测试（杀子核进程） |
| 写操作幂等 | 同键重放不产生第二次写入 | 集成测试 |

## 11. 分期与验收

| 阶段 | 内容 | 验收 |
|---|---|---|
| Phase 1 | 进程内 `CoreNode` + `Federation` + 深度/扇出/环检测 + grant 校验 | 单测 + `examples/host_m_federation` 离线 demo |
| Phase 2 | 本机 IPC：`ChildServer` + `ChildLink` + 发现文件 + 两个进程 e2e | e2e（文档产品模拟 ↔ ATM 模拟）、延迟基准达标 |
| Phase 3 | 生态运维：manifest 热更新、每 edge 配额/审计面板、桌面灵动岛接入 | 2 个可安装子核示例 |
| Phase 4 | 跨机（可选）：TLS + 复用 FastAPI sidecar 协议 | 视需求 |

## 12. 非目标

- 不做对等 Multi-Agent 协商 / 自动发现 / 自动信任。
- 不做 MCP Server、不做内核级编排、不新增 SPI。
- 不做跨核事务回滚、分布式一致性。
- 不做公网暴露（Phase 4 另议，且必须走既有鉴权边界）。

## 13. 评审待定（默认值已给，可改）

1. **传输 MVP**：默认 loopback（跨平台、可调试）；UDS/命名管道作为 Phase 2 优化项。
2. **兄弟边**：默认禁止，兄弟协作经共同父核；如要允许，需显式配置并加环检测。
3. **审批归属**：默认双防线（父核先判 + 子核再判），任一侧拒绝即失败关闭。
