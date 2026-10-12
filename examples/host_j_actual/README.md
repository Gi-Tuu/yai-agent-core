# host_j —— 第三方播种：嵌入真实记账应用 Actual Budget

> 这是 YAI "**第三方播种端到端**"（S3）的样板宿主：把内核放进一个**已经存在、
> 与 YAI 无关的真实开源应用**里，用一条标准线证明"长尾任务 → 能力缺口 →
> 补能力 / 派子员工 → 授权 → 完成"不是为演示硬编的，而是任意宿主都能走通的。
>
- 目标应用：[Actual Budget](https://actualbudget.org/)（自托管的零基预算 / 记账应用，
  含 sync server 与 Web UI）。我们不改它的源码，只把它的能力包成一组工具。
- 本示例代码随 YAI 以 MIT 发布；Actual Budget 自身的许可证与星数以其官方仓库为准，
  这里不替它声明。

## 为什么选 Actual Budget

月度对账是一条典型的"**长尾任务**"：不是一问一答，而是要串起"列交易 → 发现有
未分类 → 逐笔判断归类 → 征得同意批量补分类 → 对比预算 → 出月报"，而且其中
"**这笔交易该算什么分类**"并不是宿主原生就有的能力——这正好同时逼出内核的两条
自适应链路：**现场发现工具**与**派短命子员工并行处理**。

Actual Budget 官方并没有一套朴素 REST，主力是 Node 包
[`@actual-app/cli`](https://actualbudget.org/docs/api/cli/)（`actual <command>
<subcommand>`，默认 `--format json`，**金额一律整数分 cents**）。本宿主把官方
CLI 包成领域工具，正好说明内核不绑定任何特定后端协议：CLI、REST、进程内函数
对它而言都是"宿主声明的能力"。

## 五拍标准线

`../five_beats.py` 把一次成功的自适应接管压成可观测、可断言的节拍：

| 节拍 | 事件 | 含义 |
|---|---|---|
| ① 唤醒 | `STRATEGY_SELECTED` | 内核接住目标、选定策略 |
| ② 长尾任务 | ≥2 个不同 `TOOL_CALL` | 一个目标串起多步 |
| ③ 缺口 | `CAPABILITY_MISSING` | 现有工具不足以完成 |
| ④ 补能力 / 派子员工 | `TOOL_DISCOVERED` 或 `TOOL_CALL(delegate)` | 现场发现工具，或派短命子员工 |
| ⑤ 授权 | `PERMISSION_ASKED` | 新工具 / 写操作经权限闸确认 |
| ⑥ 完成 | `DONE` | 根对用户给出唯一最终结果 |

两条链路在本宿主都能离线跑通：

- **发现链路**：缺"自动分类建议" → 现场发现 `suggest_category` → 两次授权
  （新工具 + 写操作 `categorize_transactions`）→ 3 笔未分类清零 → 娱乐预算超支被点出。
- **委派链路**：根一次 `delegate` 派两个**只读**子员工（一个列账户、一个出对账
  概览），各自工具白名单收窄到单一工具、并行执行、结果只回流根、完成即销毁。

## 运行

```bash
# 离线发现链路（脚本模型，无需 Actual、Node 或 API Key，结果确定）
uv run python examples/host_j_actual/run.py

# 离线委派链路（两个只读子员工并行）
uv run python examples/host_j_actual/run.py --delegate

# 网页壳：左原生记账 / 右嵌入 Core，端口 8240
uv run python examples/host_j_actual/web_app.py
```

## 接真实 Actual Budget（可选）

内核与示例**不引入 Node**，真机通过官方 CLI 子进程对接。准备：

1. Node.js 22+，全局安装 CLI：`npm i -g @actual-app/cli`；
2. 一个可达的 [Actual sync server](https://actualbudget.org/docs/getting-started/sync)；
3. 通过环境变量配置（**口令只走环境变量，不进命令参数**）：
   - `ACTUAL_SERVER_URL`：sync server 地址
   - `ACTUAL_SYNC_ID`：Budget Sync ID
   - `ACTUAL_PASSWORD` 或 `ACTUAL_SESSION_TOKEN`：鉴权

配齐后 `get_backend()` 自动从离线 `FakeActualBackend` 切到 `CliActualBackend`，
同一条五拍链路在真机账上同样成立；未配齐则始终回放内置假账，CI 完全离线、不联网。

## 文件

| 文件 | 职责 |
|---|---|
| `backend.py` | 领域原语 + 整数分约定；`FakeActualBackend`（离线假账）与 `CliActualBackend`（官方 CLI 子进程，runner 可注入） |
| `capabilities.py` | 三层工具：默认只读 / 需授权写入 / 按需发现（`suggest_category`） |
| `demo_agent.py` | 两条链路的脚本模型与 Core 装配（发现 + 授权、委派） |
| `run.py` | 终端入口，`--delegate` 切换链路 |
| `web_app.py` + `web/index.html` | 标准库零依赖网页壳（8240），原生记账 vs 嵌入 Core 对照 |
| `../five_beats.py` | 五拍标准线的可执行定义（各宿主测试复用） |
| `../../tests/test_host_j_actual.py` | 17 个离线测试：假账 / 工具 / CLI argv 锁定 / 两条五拍链路 |

## 诚实边界

- 跨账户的对账概览 / 预算对比按官方 Tips "拉一段原始交易后在本地聚合"实现，
  没有臆造 ActualQL 端点；真机若遇 CLI 输出结构差异，以官方文档为准。
- 关键词分类建议（`suggest_category`）是宿主侧的确定性小规则，仅作"按需发现"的
  演示载体；真实分类可换成模型判断，但"发现 ≠ 授权、写入必经权限闸"的链路不变。
