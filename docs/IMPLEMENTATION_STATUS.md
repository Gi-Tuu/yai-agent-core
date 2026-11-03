# 能力与证据索引（IMPLEMENTATION_STATUS）

> README 回答"能做什么"，本文件回答**"做到了什么程度、证据在哪、边界在哪"**。
> 口径：**v0.25.0 已发布（2026-10-31）；v0.26 Unreleased 增量**（Batch 1–6：P0 安全、
> Browser Provider、Outcome Verification）已在本文件内标注，其余以证据列为准。三档状态：
> **已验证**（有离线测试或实测证据） / **已实现·未真机验证**（代码在、缺真实环境） / **不做**（明确排除）。
>
> 一条命令自查：`python scripts/check_all.py`（全量门禁，离线）
> 一条命令看治理闭环：`python scripts/demo_governance_loop.py`

## 一、内核与执行

| 能力 | 状态 | 证据 | 边界 |
|---|---|---|---|
| 自适应路由（规则 / LLM / 在线学习） | 已验证 | `tests/test_router.py`、`test_bandit_selector.py`、`test_route_outcomes.py`、`test_router_benchmark.py` | LLM 分类需宿主注入模型；离线只验证规则层与学习层 |
| direct / react / plan / clarify 四策略 | 已验证 | `tests/test_loop.py`、`test_replan.py` | `strict_direct` 需显式开启；默认 direct 仍执行结构化 tool_calls |
| 并发串行契约 | 已验证 | `tests/test_core_concurrency.py` | 同一实例任务串行；要并发请多实例 |
| 超时与取消（模型 / 工具 / 组合 / 子员工） | 已验证 | `tests/test_model_timeout.py`、`test_timeout_and_shutdown.py`、`test_shell_employee.py` | 同步 handler 无法被中断，只能止损；3.11+ 统一用 `asyncio.timeout` |
| 预算熔断（模型 / 工具 / token 三墙） | 已验证 | `tests/test_budget.py` | token 计数依赖后端返回 usage，部分后端不回 |
| 组合工具（只编排已注册工具） | 已验证 | `tests/test_composite.py`、`test_composite_discovery.py` | 不生成代码、不做可视化编排 |
| 幂等重放 + 权限依赖复核 | 已验证 | `tests/test_execution_receipts.py` | 只对声明幂等的调用生效 |
| 任务内重规划 | 已验证 | `tests/test_replan.py` | 只带已完成的工具配对 |
| 任务结果验收契约（v0.26，Unreleased） | 已验证（契约 / 组合器 / 事件接入） | `tests/test_outcome_verification.py` | 验收器由宿主提供；未配置时 `verification=None`；验收失败降级 `not_verifiable`，不重试、不改最终文本 |
| Provider 路由治理（v0.26，Unreleased） | 已验证（离线假后端） | `tests/test_provider_routing.py`、`test_fallback_model.py` | 授权声明来自宿主；未授权后端零调用；无授权目标明确抛错；子任务只能收窄 |
| 提示缓存（Anthropic 适配层） | 已验证 | `tests/test_anthropic_provider.py`（默认 / 关闭 / 断点位置 3 例） | 厂商专有扩展，只在适配器里；兼容代理可 `prompt_cache=False` |

## 二、工具接入与发现

| 能力 | 状态 | 证据 | 边界 |
|---|---|---|---|
| native 函数 / OpenAPI 3 / MCP 三源统一 | 已验证 | `tests/test_openapi_bridge.py`、`test_mcp_bridge.py` | MCP 需安装 `[mcp]` extra |
| 浏览器 MCP 扩展端到端（v0.26，扩展宿主） | 已验证（本机真实 Edge；含 CDP 模式） | `scripts/register_native_host.py --browser-mcp playwright [--cdp-endpoint]` + 侧边栏任务「打开百度」；CDP 模式经 `scripts/chrome_cdp.py` 启动的 Chrome 验证（`/json/list` 当前页 = baidu） | 属于宿主扩展能力：MCP 可选、懒加载，内核 `dependencies` 仍为空；CDP 连接的是 chrome_cdp 启动的独立 profile Chrome，不能接管已有默认窗口 |
| 本地 LLM 配置（v0.26，扩展宿主） | 已验证（面板入口 + CLI + 单测） | 侧边栏“LLM 设置”读取/保存（Key 只显示末4位、不存浏览器）；`scripts/configure_bridge_llm.py`；`tests/test_bridge_llm_config.py`、`tests/test_native_bridge.py` configure/get_config | Key 只写本地 bridge.json、读回永远脱敏；保存后需重开侧边栏生效；CLI 与面板共用同一配置文件 |
| 能力缺口自发现 + 远程发现源 | 已验证 | `tests/test_discovery*.py`、`test_remote_discovery.py` | 发现不等于授权，仍走统一策略 |
| 能力清单（manifest）导出 / 导入 | 已验证 | `tests/test_manifest.py` | `handler` 与 `gate` 是函数，不导出也不凭快照重建 |
| 参数严格校验 | 已验证 | `tests/test_openai_compat.py`、`test_anthropic_provider.py` | 历史上下文里的坏参数只做渲染，不转发 |
| ACC 声明与 OpenAPI 绑定 | 已验证 | `tests/test_acc_*.py`（4 个模块） | **可选 translator**：内核原生治理为主，ACC 不是依赖 |
| Office 文本抽取（docx/xlsx/pptx 零依赖，PDF 走 `[office]` extra pypdf） | 已验证 | `tests/test_office_text.py`、`tests/test_agentdojo_aggregate.py` | 文本级抽取：版面/样式/公式/数字单元格不进文本层；zip 炸弹与实体攻击有闸 |
| Browser Provider（v0.26，Unreleased，可选适配器） | **已验证（真实 Chromium + 系统 Chrome）** | `scripts/browser_smoke.py`（2026-11-03 通过：来源闸 / 输入 / 下拉 / 点击 / 等待 / 再观察 / 旧引用过期，11 条收据）；`tests/test_browser_provider.py`（假后端边界） | 只验证了一条本地任务；嵌套 frame、下载/上传、CAPTCHA、真实站点多样性与视觉定位未验证 |

## 三、治理与安全

| 能力 | 状态 | 证据 | 边界 |
|---|---|---|---|
| 权限策略（allowlist / 自定义 SPI） | 已验证 | `tests/test_execution_policy.py`、`test_execution_boundaries.py` | 未声明即不保护——安全语义只来自声明 |
| 生产安全默认值（v0.25.0，B2） | 已验证 | `tests/test_production_defaults.py`、`examples/production_policy.py` | 默认 allow_all 零变化但发 ProductionSafetyWarning；生产配置模板见 docs/production-permissions.md |
| 参数级审批 + 审批上下文 | 已验证 | `tests/test_native_approval.py`、`test_approval_context_channel.py` | 必需审批不能被策略拒绝绕过 |
| 能力闸（gate） | 已验证 | `tests/test_capability_gate.py` | 判不出来一律失败关闭 |
| 资源作用域（文件前缀等） | 已验证 | `tests/test_resource_scope_gate.py`、`test_file_provider_security.py` | 作用域闸在 handler 之前生效 |
| 可信变更预览 + 脱敏 | 已验证 | `tests/test_trusted_preview.py`、`test_redaction.py` | 预览是私有数据，不上送模型 |
| 子员工权限逐层叠加 | 已验证 | `tests/test_shell_employee.py` | 父级运行期撤权立即生效 |
| 沙箱契约 | 已验证（契约层） | `tests/test_sandbox_spi.py`、`test_sandbox_static_gate.py` | 内核只定义契约，**不内置执行器** |
| 注入型工具输出的离线回归集 | 已验证 | `tests/test_injection_regression.py`（策略拒绝 / 作用域闸 / 审批拒绝 + 包裹生效，4 例） | **只测内核防线，不测模型鲁棒性**；模型是脚本化的"最坏情况" |
| 工具输出按不可信数据处理 | 已验证 | `tests/test_loop.py`（默认包裹 / 可关两例） | 只改 prompt 形态，不改事件与收据里的原值 |
| 执行后验证（后置条件） | 已验证 | `tests/test_execution_receipts.py`（确认 / 否认 / 检查出错 / 异步 / 链完整 / 非 unknown 不跑 6 例） | 只作用于 `unknown`；检查自身失败也只记 `error`，不替宿主猜 |
| 执行收据哈希链 | 已验证 | `tests/test_execution_receipts.py::test_receipt_chain_links_and_detects_tampering`；`verify_receipt_chain()` | 进程内串链；跨进程续链由宿主决定 |
| 记忆落盘损坏安全（v0.26，Unreleased） | 已验证 | `tests/test_memory_vault_corruption.py`、`tests/test_storage_cipher.py` | 分类（不可读 / 损坏 / 解密失败 / 版本不兼容 / owner 不一致）；quarantine / read_only / raise 三种处置；解密失败与版本不兼容强制只读；绝不静默覆盖原档 |
| 记忆 owner 绑定（v0.26，Unreleased，最小隔离） | 已验证 | `tests/test_memory_vault_corruption.py`（owner_mismatch / 同 owner / 旧文件兼容） | 完整多租户仍由宿主用独立实例 / 独立路径；本项只防止误读误覆盖 |
| 外发 URL 来源闸（v0.25.0） | 已验证 | `tests/test_egress_guard.py` | 声明路径按 schema 校验；声明保护但缺任务上下文则拒绝；未知 URL 不因此拒绝，不做跨任务污点追踪 |
| 外发来源闸边界加固（v0.25.0，B1） | 已验证 | `tests/test_egress_guard.py`（Markdown/HTML/JSON 包裹、大小写与 www 变体、并发隔离、事件字段） | 主机名区域 ASCII 后紧粘 CJK 视为主机名终止；路径比较大小写不敏感；IDNA 有损映射保留独立身份 |
| 红线：内核零第三方硬依赖 | 已验证 | `tests/test_kernel_dependencies.py`（AST 全库扫描，注入即红） | 懒加载在函数体内仍允许 |

## 四、记忆与上下文

| 能力 | 状态 | 证据 | 边界 |
|---|---|---|---|
| 会话记忆 + SQLite 长期记忆 | 已验证 | `tests/test_sqlite_memory.py`、`test_long_memory_e2e.py` | 存储由宿主选，内核不强制 |
| 备份 / 恢复（暂存校验 + 原子替换） | 已验证 | `tests/test_sqlite_backup.py` | 有侧车（`-wal`/`-shm`）时拒绝操作，不提供多文件崩溃原子性 |
| 摘要压缩 + 硬裁剪兜底 | 已验证 | `tests/test_context_compaction.py`、`test_context_compact.py` | 摘要超时/失败即回退硬裁剪 |
| 存储加密 | 已验证 | `tests/test_storage_cipher.py` | 加解密走 SPI，默认不加密 |
| 保留策略 / TTL | 已验证 | `tests/test_memory_retention.py`、`test_kv_ttl.py` | 按宿主配置执行 |
| 并行摘要压缩 | 已验证 | `tests/test_context_compaction.py`（分段铺满 / 段首对齐 / 并发墙钟 / 事件字段） | 只影响墙钟；保留策略与消息结构不变，默认 1 |
| 任务锚点（上下文腐化防护） | 已验证 | `tests/test_context_compaction.py` 两例 | 仅在原任务已不在上下文时注入，不重复花 token |
| 嵌入召回（可选） | 已验证（假实现） | `tests/test_integrations_embedding.py` | 本地 ONNX 真机用例需自备模型文件，CI 跳过 |
| 记忆质量基准（v0.26，Unreleased） | 已验证（离线脚本 + 回归） | `benchmarks/memory/run_bench.py`、`tests/test_memory_metrics.py`；本机真实 bge-m3 实测 Recall@5 1.000 / MRR@5 0.917 / P50 12.27ms（18 条 / 12 query） | 语料小、标注非穷尽；真实 BGE 数字不随仓库分发、不进 CI；假嵌入器分数不代表真实语义效果 |

## 五、宿主与协议

| 能力 | 状态 | 证据 | 边界 |
|---|---|---|---|
| 11 个示例宿主（含 1 个生产形态）+ 原生桌面壳 | 已验证 | `tests/test_host_*.py`、`tests/test_host_production.py`、`tests/test_desktop_*.py` | 桌面壳仅 Windows 真机验证；真实第三方应用行为仍由各示例自己的假后端测试界定 |
| Windows Desktop Provider（v0.26，Unreleased，可选适配器） | 已验证（假后端 + **本机真机 UIA 冒烟**） | `tests/test_desktop_provider.py`；`scripts/windows_uia_smoke.py`（2026-11-03 通过：自建 WinForms 窗口观察 → Invoke → 按钮回调验证） | 仅本机一次冒烟；其它真实应用、截图、UAC 提权窗口未验收；坐标点击默认关闭；非 Windows 构造即报错 |
| 两个真实开源软件零改造接入 | 已验证 | `tests/test_host_i_mealie.py`、`test_host_j_actual.py` | 走假后端，不连真实实例 |
| Sidecar 协议（stdio NDJSON + JSON-RPC 2.0） | 已验证 | `tests/test_sidecar_protocol.py`、`test_sidecar_server.py` | 只承载会话面，工具面仍走 MCP / OpenAPI |
| Native Messaging Bridge（v0.26，Unreleased） | **已验证（真实进程 + 真实 Sidecar + 真实 Edge 端到端）** | `scripts/register_native_host.py`（HKCU 注册，可逆）；`scripts/bridge_smoke.py --executable`（冻结 exe + 未授权退出码 2 + hello/status/shutdown）；真实 Edge 侧边栏显示"协议握手完成（bridge v1）"，`bridge.log` 记录 frozen=True / exit ok | 未在 Chrome 品牌版做端到端（同一注册表/同一 exe，Edge 已证）；未跑真实任务（需模型凭据环境）；Bridge 与一次扩展连接绑定，不提供跨进程任务恢复 |
| 浏览器扩展宿主（v0.26，Unreleased，MV3 Side Panel） | **已验证加载 + 侧边栏页 + 点击行为 + 端到端任务（Playwright Chromium / 真实 Edge）** | `scripts/extension_smoke.py`：MV3 service worker 出现、`sidepanel.html` 渲染无页错误、`sidePanel.getPanelBehavior().openPanelOnActionClick=true`；`tests/test_extension_assets.py` 静态守卫（manifest / 点击行为 / 标签页兜底）；系统 Chrome 的 CLI `--load-extension` 受策略限制，UI 手动加载路径需人工确认；本机真实 Edge 端到端：任务「打开百度」经 Native Bridge → Sidecar → MCP 浏览器工具完成并返回标题（`docs/exports/extension-e2e.png`）；CDP 模式（`scripts/chrome_cdp.py` + `--cdp-endpoint`）已验证操作该 Chrome 窗口（`docs/exports/extension-cdp-e2e.png`） | 真实鼠标点击工具栏图标→侧边栏出现、Native Messaging 注册、安装包分发与 Service Worker 回收仍未自动化验收 |
| 宿主实时状态 | 已验证 | `tests/test_host_state_provider.py` | 状态是增强，失败只降级 |
| 通道（CLI / 收集 / 桌面 / sidecar） | 已验证 | `tests/test_cli_channel.py`、`test_sidecar_server.py` | 审批与澄清必须经通道回宿主 |

## 六、可观测与运维

| 能力 | 状态 | 证据 | 边界 |
|---|---|---|---|
| 21 类结构化事件、单一出口 | 已验证 | `tests/test_spi_surface.py`（含"无死事件"断言） | 事件是唯一对外流 |
| 本地诊断回调 | 已验证 | `tests/test_diagnostics.py` | 回调自身抛错被吞，绝不反噬任务 |
| GenAI 语义约定属性（agent / tool） | 已验证 | `tests/test_tracing_otel.py` | 只发稳定字段；usage / model 不臆造 |
| OpenTelemetry 追踪 / 指标 | 已验证（假导出器） | `tests/test_tracing_otel.py`、`test_metrics_recorder.py` | 需装 `[otel]` extra |
| 就绪/指标 HTTP 端点 | 已验证 | `tests/test_readiness_metrics_api.py`、`test_battery_api.py` | 属 batteries，不是内核必需 |

## 七、打包、供应链与发布

| 能力 | 状态 | 证据 | 边界 |
|---|---|---|---|
| 版本串一致（pyproject / `__version__` / lock / 双 README） | 已验证 | `tests/test_version_consistency.py` | 发版必跑 |
| sdist 链接完整 + 包内可跑测试 | 已验证 | `tests/test_sdist_link_integrity.py`（19 例）；实测包内全量 exit 0 | 仅带已跟踪的公开内容 |
| SLSA provenance + cosign 签名 | 已验证 | v0.25.0 Release 15 个资产，含 `slsa-provenance.intoto.jsonl` | 第三方扫描识别有延迟 |
| OpenSSF 徽章 / Scorecard | 外部评分 | CII Passing（#15360）；Scorecard 7.7/10（2026-10-28 复扫，Signed-Releases 9/10） | 以第三方复扫为准 |

## 八、明确不做

MCP Server · Multi-Agent 编排 · 内置向量数据库 · 内置 UI（桌面壳在 `shell/`，不在内核） ·
代码生成工具的沙箱执行 · coding agent · 跨实例分布式限流 · gVisor / 微 VM 级隔离。
理由与取舍见 `ROADMAP.md` 的"不做什么"。

## 九、已知边界（没验证的，不假装验证过）

- **真实模型只做抽样验证**：CI 全部用脚本模型，不依赖网络与 API Key。
- **Playwright 后端已在真实 Chromium / 系统 Chrome 验证一条完整任务**（`scripts/browser_smoke.py`：
  导航 / 观察 / 输入 / 下拉 / 点击 / 等待 / 再观察 + 来源闸 + 旧引用过期，11 条收据）；
  仍不覆盖嵌套 frame、下载 / 上传、CAPTCHA、真实站点多样性与视觉定位。
- **浏览器扩展已在真实 Chrome / Edge 加载出 MV3 service worker**（`scripts/extension_smoke.py`）；
  但**侧边栏点击流、Native Messaging 注册、安装包分发**仍需人工按
  `docs/browser-extension-bridge.md` 验收，不能把"加载成功"当成"交互链路已验证"。
- **Windows Desktop Provider**：本机只完成一次自建 WinForms 窗口的 UIA 冒烟
  （观察 / 语义 Invoke / 回调验证）；对第三方真实应用、复杂控件 Pattern、截图、
  多显示器与 UAC 提权窗口**没有**验证，不能据此宣称"通用桌面自动化已验证"。
  Playwright 浏览器后端同理保持「已实现·未真机验证」。
- **公网演示实例已停用**：Render 上的 `yai-agent-core` 服务已于 2026-11-02 删除；今后任何公网部署都应显式配置 `YAI_API_KEY`（见 ADR 0002 D3）。
- **Windows 符号链接竞态**只能收窄、不能根除（POSIX 有 `O_NOFOLLOW`）；对应测试在 Windows 跳过。
- **桌面壳只在 Windows 真机验证**；60fps 数据仅来自 2560×1600 / dpr 1.5 一次采样。
- **演示视频为旧版录制**，界面与当前桌面壳不同（能力点一致）。
