# 能力与证据索引（IMPLEMENTATION_STATUS）

> README 回答"能做什么"，本文件回答**"做到了什么程度、证据在哪、边界在哪"**。
> 口径：v0.22.1（2026-10-25 实测）。三档状态：
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
| 提示缓存（Anthropic 适配层） | 已验证 | `tests/test_anthropic_provider.py`（默认 / 关闭 / 断点位置 3 例） | 厂商专有扩展，只在适配器里；兼容代理可 `prompt_cache=False` |

## 二、工具接入与发现

| 能力 | 状态 | 证据 | 边界 |
|---|---|---|---|
| native 函数 / OpenAPI 3 / MCP 三源统一 | 已验证 | `tests/test_openapi_bridge.py`、`test_mcp_bridge.py` | MCP 需安装 `[mcp]` extra |
| 能力缺口自发现 + 远程发现源 | 已验证 | `tests/test_discovery*.py`、`test_remote_discovery.py` | 发现不等于授权，仍走统一策略 |
| 能力清单（manifest）导出 / 导入 | 已验证 | `tests/test_manifest.py` | `handler` 与 `gate` 是函数，不导出也不凭快照重建 |
| 参数严格校验 | 已验证 | `tests/test_openai_compat.py`、`test_anthropic_provider.py` | 历史上下文里的坏参数只做渲染，不转发 |
| ACC 声明与 OpenAPI 绑定 | 已验证 | `tests/test_acc_*.py`（4 个模块） | **可选 translator**：内核原生治理为主，ACC 不是依赖 |

## 三、治理与安全

| 能力 | 状态 | 证据 | 边界 |
|---|---|---|---|
| 权限策略（allowlist / 自定义 SPI） | 已验证 | `tests/test_execution_policy.py`、`test_execution_boundaries.py` | 未声明即不保护——安全语义只来自声明 |
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

## 五、宿主与协议

| 能力 | 状态 | 证据 | 边界 |
|---|---|---|---|
| 10 个示例宿主 + 原生桌面壳 | 已验证 | `tests/test_host_*.py`、`tests/test_desktop_*.py` | 桌面壳仅 Windows 真机验证 |
| 两个真实开源软件零改造接入 | 已验证 | `tests/test_host_i_mealie.py`、`test_host_j_actual.py` | 走假后端，不连真实实例 |
| Sidecar 协议（stdio NDJSON + JSON-RPC 2.0） | 已验证 | `tests/test_sidecar_protocol.py`、`test_sidecar_server.py` | 只承载会话面，工具面仍走 MCP / OpenAPI |
| 宿主实时状态 | 已验证 | `tests/test_host_state_provider.py` | 状态是增强，失败只降级 |
| 通道（CLI / 收集 / 桌面 / sidecar） | 已验证 | `tests/test_cli_channel.py`、`test_sidecar_server.py` | 审批与澄清必须经通道回宿主 |

## 六、可观测与运维

| 能力 | 状态 | 证据 | 边界 |
|---|---|---|---|
| 20 类结构化事件、单一出口 | 已验证 | `tests/test_spi_surface.py`（含"无死事件"断言） | 事件是唯一对外流 |
| 本地诊断回调 | 已验证 | `tests/test_diagnostics.py` | 回调自身抛错被吞，绝不反噬任务 |
| GenAI 语义约定属性（agent / tool） | 已验证 | `tests/test_tracing_otel.py` | 只发稳定字段；usage / model 不臆造 |
| OpenTelemetry 追踪 / 指标 | 已验证（假导出器） | `tests/test_tracing_otel.py`、`test_metrics_recorder.py` | 需装 `[otel]` extra |
| 就绪/指标 HTTP 端点 | 已验证 | `tests/test_readiness_metrics_api.py`、`test_battery_api.py` | 属 batteries，不是内核必需 |

## 七、打包、供应链与发布

| 能力 | 状态 | 证据 | 边界 |
|---|---|---|---|
| 版本串一致（pyproject / `__version__` / lock / 双 README） | 已验证 | `tests/test_version_consistency.py` | 发版必跑 |
| sdist 链接完整 + 包内可跑测试 | 已验证 | `tests/test_sdist_link_integrity.py`（19 例）；实测包内全量 exit 0 | 仅带已跟踪的公开内容 |
| SLSA provenance + cosign 签名 | 已验证 | v0.22.1 Release 15 个资产，含 `slsa-provenance.intoto.jsonl` | 第三方扫描识别有延迟 |
| OpenSSF 徽章 / Scorecard | 外部评分 | CII Passing（#15360）；Scorecard 7.6/10 | 以第三方复扫为准 |

## 八、明确不做

MCP Server · Multi-Agent 编排 · 内置向量数据库 · 内置 UI（桌面壳在 `shell/`，不在内核） ·
代码生成工具的沙箱执行 · coding agent · 跨实例分布式限流 · gVisor / 微 VM 级隔离。
理由与取舍见 `ROADMAP.md` 的"不做什么"。

## 九、已知边界（没验证的，不假装验证过）

- **真实模型只做抽样验证**：CI 全部用脚本模型，不依赖网络与 API Key。
- **线上实例的鉴权行为**（`/v1/agent/run`）未在本轮复查，属待决项。
- **Windows 符号链接竞态**只能收窄、不能根除（POSIX 有 `O_NOFOLLOW`）；对应测试在 Windows 跳过。
- **桌面壳只在 Windows 真机验证**；60fps 数据仅来自 2560×1600 / dpr 1.5 一次采样。
- **演示视频为旧版录制**，界面与当前桌面壳不同（能力点一致）。
