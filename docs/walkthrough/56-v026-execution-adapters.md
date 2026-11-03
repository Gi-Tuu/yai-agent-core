# 第 56 篇：v0.26 —— 执行适配器、验收契约与 P0 安全

> 对应代码：`memory/vault.py`、`llm/routing.py`、`llm/fallback.py`、
> `integrations/browser/`、`integrations/desktop/`、`verification.py`、
> `sidecar/native_bridge.py`、`extension/`、`benchmarks/memory/`。
> 本篇讲"为什么这样切边界、每个决策落在哪一行"，不重复前 55 篇已讲过的内核机制。

## 1. 先回答边界问题

v0.26 新增的每一项都先回答过同一个问题：**它属于 Core、Integration、Provider、Battery
还是 Host/Shell？** 结论：

| 新增能力 | 归属 | 判据 |
|---|---|---|
| Browser Provider | Integration（可选适配器） | 只依赖 `BrowserBackend` Protocol，Playwright 走可选 extra |
| Windows Desktop Provider | Integration（可选、Windows-only） | 同样只依赖 Protocol，pywinauto 走可选 extra |
| 验收契约（Evidence / VerificationResult） | Core 类型 + 宿主 verifier | 契约在内核，领域规则在宿主 |
| 浏览器扩展 | Host | 只做 UI 与连接，不跑 Agent 循环 |
| Native Messaging Bridge | Sidecar 的宿主侧适配 | 只做帧 ↔ NDJSON 转换，不改 Sidecar 协议 |
| 记忆质量基准 | 工程/评测（benchmarks） | 不进运行时依赖 |

判据不是"实现方便"，而是三条硬约束：内核 `dependencies = []`、公开 API/SPI/事件契约
兼容、平台能力不污染 Agent Loop。

## 2. P0：记忆落盘失败不再静默丢档

旧 `MemoryVault._load()` 是一个大 `except Exception: self._notes = []`——一次坏文件
换来"空库启动成功"，下一次 `remember()` 的 `_save()` 就把原始档案覆盖了。

现在的分层（`src/yai_core/memory/vault.py`）：

1. **分类**：`unreadable / corrupt / decrypt_failed / version_incompatible /
   owner_mismatch` 五种状态，写进 `load_state` 与 `status()["storage"]`；
2. **处置**（`on_corruption`）：
   - `quarantine`（默认）：先把原始字节复制成 `<name>.corrupt-<时间戳>`，再允许空库写入；
     复制失败自动转只读；
   - `read_only`：读可用、写抛 `MemoryVaultReadOnlyError`；
   - `raise`：构造器抛 `MemoryVaultLoadError`（带分类与路径）；
3. **两条额外收紧**：解密失败 / 版本不兼容即便选了 quarantine 也只读——原始档案可能
   仍然有效，覆盖等于丢数据；
4. **owner 绑定**：`MemoryVault(owner=...)` 写入并校验；不一致即只读，连一条记忆都不读进来；
5. **可观测**：`memory_load_failed` 诊断（ERROR）带 status / detail / policy。

对应测试：`tests/test_memory_vault_corruption.py`（含"原文件字节不被改动"断言）。

## 3. P0：Provider 路由治理（未授权零调用）

`FallbackModelProvider` 原来的循环会对**所有**后端尝试，只要它是可重试错误。
新增 `src/yai_core/llm/routing.py`：

- `ProviderPolicy`：单 provider 的 `allowed_sensitivities` / `enabled` / `has_credentials`；
- `ProviderRoutingPolicy`：白名单、敏感等级、锁定、是否允许降级，`narrowed()` 单调收窄
  （白名单取交、等级取高、锁定不可解除、禁用降级不可重开）；
- `routing_context()`：ContextVar 按任务注入，子任务继承并只能收紧；
- `FallbackModelProvider._decide()` 在**发请求之前**筛掉未授权后端，无目标抛
  `NoAuthorizedProviderError`；`last_routing` + `provider_route_blocked` 诊断可审计。

关键不变量：**模型输出无法修改授权**（声明只来自宿主代码），取消 / 超时 / 重试只改变
"尝试顺序"，不改变"允许集合"。

## 4. Browser Provider：快照引用与"不宣称成功"

`integrations/browser/provider.py` 的三条设计：

1. **引用带版本**：每次 `observe()` 生成 `s<N>`，元素引用 `s<N>:<i>`；`_resolve()` 只接受
   最新快照，旧引用返回 `stale_ref` 并提示重新观察——避免页面重渲染后点错元素；
2. **动作后必然再观察**：`_settle()` 比较前后指纹，返回 `executed + changed /
   no_observable_change / unknown`，**从不返回 `success`**；"是否完成"由验收契约回答；
3. **治理不另起一套**：`build_browser_tools()` 只造 `ToolSpec`，注册进 `ToolRegistry` 后
   由 `ToolExecutor` 做参数校验 / 权限 / 审批 / 收据 / 事件；来源白名单、密码框拒填、
   导航 gate 是**额外收紧**，不是替代。真实 Chromium / 系统 Chrome 已用
   `scripts/browser_smoke.py` 跑通一条完整任务（含来源闸与旧引用过期）。

## 5. Windows Desktop Provider：语义优先、焦点保护、用户接管

`integrations/desktop/provider.py` 与 Browser 同构，差异在平台语义：

- UIA 语义 Pattern（Invoke / Toggle / Select、ValuePattern）优先；几何点击默认关闭；
- 控件引用 `d<N>:<i>`，`runtime_id` 解析失败即按过期处理；
- 高风险控件名（删除 / 卸载 / 格式化 / 关机…）默认拒绝；敏感控件拒绝自动填写；
- 物理输入前 `is_foreground()` 检查，焦点漂移返回 `not_foreground`；
- **进程身份白名单**：`DesktopPolicy` 用 PID（**精确匹配**，`123` 不放行 `1234`）/ 映像名 /
  完整路径三者之一收窄可操作进程；窗口标题只作附加过滤，**不算安全边界**（v0.27.1 收紧）；
- `pause()` 后**写动作**全拒绝（只读观察仍允许），`resume()` 恢复——对应"用户接管"；
- 不跨 UAC / 安全桌面：后端直接报错，Provider 不改写成成功。

真实后端 `PywinautoDesktopBackend` 的阻塞调用统一 `asyncio.to_thread`；
`scripts/windows_uia_smoke.py` 用自建 WinForms 窗口做真机 UIA 冒烟。

## 6. Outcome Verification：把"调用成功"和"目标达成"拆开

`src/yai_core/verification.py` + `types.py`：

- `Evidence`：kind / summary（上限 500）/ ok / detail；
- `VerificationResult`：五种状态 + confidence（夹取 0–1）+ evidence；
- `OutcomeVerifier`：宿主实现的 `async verify(goal, evidence, metadata)`；
- `CompositeVerifier`：确定性合并（失败优先 → 全成功 → partial → unknown → not_verifiable）；
- `evidence_from_events()`：从 `TOOL_RESULT` 收据与工具显式 `evidence` 收集；
- `verify_run()`：异常降级 `not_verifiable` + `verification_failed` 诊断。

接线：`AgentCore.astream` 在 `DONE` 事件**发出前**跑验收，把结论写进事件 data 与
`RunResult.verification`；未配置 verifier 时保持 `None`。**验收失败不重试**（重复副作用
比"未确认"更糟）。

## 7. 浏览器扩展与 Native Messaging Bridge

- `sidecar/native_bridge.py`：4 字节小端长度帧 ↔ NDJSON 的独立转换层；扩展来源
  allowlist（fail closed）、操作白名单、入站 1 MiB 上限、出站超限替换为截断标记；
  `initialize` 参数只来自本地配置——**浏览器不保管凭据**；
- `extension/`：MV3 Side Panel；Service Worker 只做连接/转发，断连把状态标 `unknown`
  且**不自动重放**任务；"读取当前页面"必须用户点击，走 `activeTab` + `chrome.scripting`
  一次性采集；
- 生命周期诚实声明：Bridge 与一次扩展连接绑定，不提供跨进程任务恢复；
- 真机验收（2026-11-03）：Bridge ↔ 真实 Sidecar 握手/状态/关闭通过；扩展在
  Chrome 156 / Edge 156 加载出 MV3 service worker（`scripts/bridge_smoke.py` /
  `scripts/extension_smoke.py`），侧边栏点击与 native host 注册仍需人工验收。

## 8. 记忆质量基准

`benchmarks/memory/run_bench.py` + `evaluation/memory_metrics.py`：Recall@5、MRR@5、
无关召回率、P50/P95、词法-语义互补、嵌入故障降级一致性、更新删除后索引一致性。
假嵌入器（概念轴）只证明通道接线；真实 bge-m3 需本地模型文件，数字不进 CI。

本机实测（18 条 / 12 query / k=5）：纯词法 Recall@5 0.750；真实 bge-m3 双通道
Recall@5 1.000 / MRR@5 0.917 / P50 12.27 ms；嵌入故障降级结果与纯词法逐条一致。

## 9. 自检

1. 为什么解密失败不能"隔离后继续写"？写出会导致什么数据后果。
2. Provider 白名单在哪个函数里生效？为什么必须在 `achat()` 里而不是外层？
3. 浏览器元素引用为什么会过期？动作结果里的 `outcome` 与验收状态有什么区别？
4. 桌面 Provider 的 `pause()` 为什么允许只读观察、却拒绝 `focus`？
5. 验收器抛错时，为什么只能降级 `not_verifiable`，不能把任务判失败？
