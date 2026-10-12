# 路线图（Roadmap）

> 最后更新：2026-10-11。本路线图只记录**方向与边界**，不承诺具体日期；每个版本的实际范围以 Release notes 为准。

## 愿景

让"给现有软件加 Agent 能力"像"给应用嵌入数据库"一样简单：宿主软件只声明自己的业务能力（普通函数 + 一行描述），YAI 内核在进程内完成能力发现、意图理解、策略选择、工具调用、记忆与可审计交付，**宿主不写一行 Agent Loop / Planner / 工具选择代码**。

## 设计原则（所有版本不变）

1. **Library-first**：YAI 是被 import 的内核，不是需要单独部署的平台；内核本体 `dependencies = []`，第三方能力全部走可选 extra 懒加载。
2. **一切外部能力走 SPI**：模型（Model）、通道（Channel）、记忆（Memory）、权限（Policy）、发现（Discovery）、沙箱（Sandbox）、路由学习（RouteSelector）、文本嵌入（Embedding）、存储加密（StorageCipher）九个契约宿主可替换。
3. **事件流唯一出口**：每个自适应决策都必须发出 `AgentEvent`，禁止静默决策。
4. **可离线测试**：全部测试不依赖网络与 API Key（假模型 / MockTransport / 注入时钟）。

## 已完成

### v0.1 — 内核最小闭环

- 函数内省自动生成工具（type hints + docstring → JSON Schema）
- ToolRegistry / ToolExecutor（校验、权限、sync/async）
- Adaptive Router：direct / react / plan / clarify 四策略（规则路由，零成本可测）
- Agent Loop：ReAct 循环、先规划后执行、澄清交互、迭代上限
- SPI 四契约 + 默认实现（内存记忆、白名单权限、CLI/收集通道）
- FastAPI Battery（/health、/v1/tools、POST 事件流）、Dockerfile / render.yaml
- 三个宿主示例（笔记、销售数据、AI 陪伴）

### v0.2 — 工具来源扩展与 LLM 路由

- MCP Client（stdio / Streamable HTTP / 内存实例，同构接入工具注册表）
- OpenAPI 发现（OpenAPI 3 描述 → 工具，$ref 内联、只读模式、鉴权）
- LLM 路由器（规则兜底，决策来源可审计）
- SQLite 持久化记忆（opt-in，`YAI_DB_PATH`）
- OpenAI 兼容模型后端（DeepSeek / 通义千问等）

### v0.3 — 长期运行可信度

- 历史保留策略：条数上限 + TTL，轮边界对齐裁剪（opt-in）
- 在线 API 限流 Battery：每 IP 滑动窗口、429 + Retry-After、GET 放行
- 宿主 E：零 AI 依赖可独立运行的销售 CRM，同一业务对象零改造嵌入（终端 + 网页工作台）
- 澄清循环修复（轮数上限、上下文累积、空回答体面收尾）

### v0.6 — 路由演进（当前 main，上海开源赛候选）

- `llm_router="auto"`：按模型后端能力标记自动开关 LLM 路由（真实模型开、离线脚本模型关）
- **能力缺口感知**：LLM 路由发现现有工具不足时发出 `capability_missing` 事件，宿主可据此引导安装插件/接入 MCP——从"只会用给定工具"到"知道自己缺什么工具"的第一步
- **能力发现最小闭环（ToolDiscovery SPI）**：缺口 → 发现候选 → 注册 → 本轮即可用；内置进程内 `StaticCatalog`（关键词匹配、零依赖、离线可测），发现源异常不致命，"能被发现不等于被授权执行"；宿主 F 离线确定性演示
- 规则路由量词回归（"多少/几个/有没有"等问题不再误判）

> 版本号说明：比赛专用 tag `v0.5.0-hangzhou` 之后，下一个正式 tag 建议为 v0.6.0（release notes 源稿见 `docs/releases/v0.6.0.md`）；打 tag 前需同步 `pyproject.toml` 的 `version`。

## 下一步

### v0.7 — 自适应能力扩展（开发中）

- **自校准路由（已落地，opt-in）**：把一次性关键词路由升级为会从执行反馈学习的**上下文老虎机（contextual bandit）**，坐实项目名 self-adaptive。四个路由策略各是一个 arm，按 9 维任务特征分桶，每桶维护 Beta(α,β) 后验，用 Thompson Sampling 选策略；冷启动注入规则先验，且某上下文桶零真实反馈时确定性取先验最强臂（第一次建议=规则，避免弱先验随机采样把任务带偏），首条真实反馈后才放开采样；任务结束后从事件流抽成败一次性回灌（fractional update，流式 astream 正常结束也回灌、取消不回灌），不影响当次稳定性。纯标准库、零依赖、状态可 JSON 持久化；默认关闭，空任务/无工具等硬规则区域学习器不表态。新增第七个 SPI `RouteSelector`，学习算法可整体替换。
  - **离线可复现证据**：`scripts/benchmark_router.py` 用 60 条标注任务做确定性仿真（奖励仍由真实 `extract_route_outcome` 计算），五线对比 + 消融。8 seeds × 3 epochs：自校准末段成功率 **89.6%** vs 规则基线 73.3%（**约 +16pp**），Oracle 100%；消融"无规则先验"88.5%（整体 75.6%，冷启动更颠）、消融"无上下文"70.8%（整体 64.0%，证明按上下文分桶是关键）。学习曲线见 `docs/assets/router-learning-curve.svg`，脚本内置"bandit 必须显著优于 rules"的硬校验。见讲义第 19 篇。
- **权限三档产品化**：全部审批 / 部分审批（读白名单自动放行、写操作询问）/ 无需审批，宿主可在应用内切换；宿主 E 网页工作台提供 Core 开关，直观对比"有无 Core"。
- **两层工具目录（已接线）**：系统提示只放 `catalog_text()`（名称 + 一句话摘要），完整 JSON Schema 走 function-calling；工具数超 `full_schema_budget`（默认 24）后先让模型按目录选工具、再用 `schemas_for(names)` 只注入所选 schema，聚焦失败回退全量，宁多勿漏。
- **执行中动态发现（已落地）**：react 中途模型可调用 meta-tool `request_capability(need)` 显式声明能力缺口，内核授权后发 `capability_missing`（`phase="react"`）→ 发现注册 → 下一轮直接调用，`tool_discovered` 事件让新工具 schema 立即可见。
- **每轮反思（已落地）**：工具失败后把失败原因回灌模型，引导其改用现有工具、请求发现新能力或如实说明能力缺口，不再反复撞同一失败调用。
- **上下文摘要压缩（已落地，opt-in）**：超 `hard_max_chars` 高水位时，把较早的完整轮交给模型压成一条 system 摘要（保留目标 / 约束 / 事实 / 工具关键结果 / 未决问题），最近 `keep_recent_rounds` 个完整轮原样保留；区间两端对齐 user 轮首，不切断工具配对。模型异常或摘要为空自动回退硬裁剪 `enforce_budget()`，摘要为增强而非依赖。`AgentCore(context_compaction=True)` 开启，也可传入自定义 `CompactionPolicy`；灵动岛真机工厂已开启。见讲义第 25 篇。
- **双通道语义发现（已落地，opt-in）**：把"关键词子串"升级为可排序相关度。第一通道 `discovery/scoring.py` 纯标准库（NFKC 规范化、中文单字+bigram、小同义词表、关键词强信号+token 重叠），零依赖、永远可用；第二通道是新增的第八个 SPI `EmbeddingProvider`，`SemanticCatalog` 用标准库 `math` 算余弦，配两层摘要（轻量摘要检索、命中才内省完整 Schema）。召回用词法 gate OR 语义 gate（绝对门槛 + 多候选 top1 边际 margin 防误召回），融合分仅排序。真实后端两个：本地 bge-m3 ONNX（`[local-embed]`，CPU、离线、免费，与 AMBRACE 同模型同后处理）与 OpenAI 兼容云端（`[llm]`，按 index 保序）；不配置或报错自动降级纯词法。阈值用真实 bge-m3 校准（无关≈0.30、语义相关≥0.48、词法≈0.68）。宿主 F 支持 `--local` 真实向量演示。见讲义第 20 篇。
- **组合工具（已落地）**：模型可通过 `compose_tool` 把宿主已注册的工具编排成新工具（`AgentCore(composition=True)` 开启）。组合工具只能引用已注册工具、执行时每步仍走权限，因此**能力上限 = 被组合工具的并集，物理不越界**，且不执行任何模型生成的代码。
- **代码工具注册表 + 生命周期（已落地，执行走宿主沙箱）**：宿主提供 `ToolSandbox` 后，模型可经 meta-tool `create_code_tool` 生成代码工具；内核负责注册、48h TTL、被调用刷新、后台永久保留（`retain_code_tool` / `sweep_code_tools` / `code_tools_status`），创建与首次执行都过权限闸；**内核仍不内置任何代码执行器**，真正执行在宿主沙箱。见下文"代码生成工具"。
- **宿主沙箱示例 host_g（已落地）**：`examples/host_g_sandbox/` 给出最小可运行的 `ToolSandbox`（标准库子进程 + 内置白名单 + 超时；print 4k 字符 / 结果 64KB 上界；Unix 另加内存 256MB / CPU 15s rlimit），离线演示"AI 现场 create_code_tool 造加权评分工具并在沙箱执行"，把代码工具的 SPI 契约真正跑通；定位为教学级隔离（**Windows 无标准库内存/CPU rlimit，仅靠超时兜底、不做 OS 级网络隔离**），生产级用容器/微 VM 替换同一 SPI。
- 网页工作台渲染 `capability_missing` / `tool_discovered` / `tool_composed` / `code_tool_created` 事件。
- **运维向（已落地）**：KV TTL（`MemoryStore.put` 可选 `ttl_seconds`，SQLite schema v1→v2 自动迁移、旧数据保留、过期边界时刻保留）；响应体大小守卫（final / 事件数 / 整体三级截断、头+尾保留、附 truncated 与 events_total 标记，不静默删改）；API Key 鉴权（`YAI_API_KEY` / `YAI_API_KEYS`，Bearer / X-API-Key、常量时间比较，硬门槛路径公开、未带 key 在花模型钱前返回 401）。

- **结构化长期记忆（已落地，opt-in）**：在会被裁剪的对话历史之外，新增 `memory/vault.py` 的 `MemoryVault` 与 `remember` / `recall` 两个普通工具，模型可主动把事实 / 偏好 / 画像 / 事件 / 笔记存档、并在需要时召回。写入对高相似条目去重合并（标签并集、重要度取大、访问计数 +1）；召回按"词法相似度 ×（0.75 + 0.25·重要度/5）"排序、支持 kinds/tags 过滤与 top-k，命中刷新最近使用时间与计数；落盘为 JSON（原子写、损坏冷启动容忍）。`AgentCore(long_memory=True, memory_storage=...)` 开启，另提供 `long_memory_status()` / `forget_memory()`；落盘可经第九 SPI `StorageCipher` 透明加密（`[crypto]` extra 提供 Fernet 电池）。见讲义第 28 / 35 篇。
- **语义召回 + 任务起始主动召回（已落地，opt-in）**：`MemoryVault.arecall` 在词法之外叠加可选语义通道（宿主注入第八 SPI `EmbeddingProvider`，复用标准库余弦、阈值 0.45，词法 OR 语义取并集融合），跨语言 / 换说法也能召回；记忆向量懒批量计算并缓存、增删即失效，embedder 故障 / 数量不符自动降级纯词法并记 `semantic_last_error`。`AgentCore(auto_memory_recall=True, memory_embedder=..., auto_recall_k=3)` 开启后，loop 在任务开始自动检索相关记忆注入 system 上下文（发 `memories_recalled` 事件、声明"当前指令优先"），模型无需主动调 recall 即可用上画像；默认关闭、内核零硬依赖。见讲义第 37 篇。
- **端到端离线评测（已落地）**：新增零硬依赖内核包 `evaluation/`（`GoldenTask` / `collect_trace` / `score_task` / `aggregate`）与 `scripts/eval_agent.py`，用 ScriptedModel 真跑完整 AgentCore，量化任务完成率、工具调用精确率 / 召回率、越权与无支撑断言；`scripts/eval_tasks.py` 的 5 条 golden cases 作为回归基线（应全通过），非门禁 CI `eval` job 产出评分 JSON。把 ScriptedModel 换成真实模型即可量化真机多步完成表现。见讲义第 29 篇。

### v0.8 — Tool Discovery（发现工具，动态发现源）

> 最小切片已在 v0.6 落地：`ToolDiscovery` SPI + 进程内 `StaticCatalog` + 缺口→注册→本轮可用闭环（见上）。
> 本阶段把"进程内静态目录"扩展为"运行时远程发现源"。

- **远程发现源（已落地）**：`discovery/remote_sources.py` 的 `RemoteSourceDiscovery` 复用 MCP / OpenAPI bridge，懒连接一组外部源（首次 discover 才连接、单源失败跳过、连接常驻以保持 handler、缓存 specs），`mcp_discovery(configs)` / `openapi_discovery(configs)` 工厂构造；配置即白名单，注册去重与执行授权仍归内核 / PermissionPolicy。
- **多源聚合（已落地）**：`discovery/aggregate.py` 的 `CompositeDiscovery` 把进程内 StaticCatalog 与远程源串成单个 discovery（内核只接受一个），跨源去重、把已返回工具名并入后续源的 available，并聚合 `list_discoverable` 声明。
- **远程源按 need 筛选（已落地）**：`RemoteSourceDiscovery` 对已构造 ToolSpec 做保守双通道收窄——词法（name+description 双向重叠取大，零依赖，阈值 0.20 / margin 0.08 / top_k 8，按实测分布标定）始终可用，宿主再提供 embedder 时叠加语义 gate（跨概念高召回）；任一通道识别即收窄、两通道都不中回退全量，`filter_by_need=False` 关闭。底层检索（`SemanticCatalog` + 第八 SPI，本地 bge-m3 / 云端双后端）见上。
- 每轮反思中按需再次发现（与 v0.7 反思循环联动）。
- 宿主插件市场发现源（AMBRACE）待 v1.0 回流时接入。

### v0.9 — 动态重规划与成本选档（开发中）

- **动态重规划 replan（已落地，PLAN 专属）**：新增 meta-tool `replan`，多步计划执行中因新结果发现原计划走不通（漏前置 / 顺序要调 / 前提不成立）时显式触发；进入 PLAN 分支幂等注册（空宿主 / direct / 简单 react 不注册），`_handle_replan` 保留已完成步骤、基于当前观察产出修正计划，发 `plan_revised`（序号 / 原因 / 步骤，解析不出回退原任务），回灌后按新计划继续。只重排内部计划、无外部副作用，不走授权闸，业务工具仍逐个授权；meta-tool schema 经 `_with_meta_schemas` 在聚焦裁剪下始终注入（同时修复 request_capability / create_code_tool 可见性）。见讲义第 31 篇。
- **成本·延迟感知选档 tiering（已落地）**：新增零硬依赖 `kernel/tiering.py`，默认 standard（便宜快），仅在规划 / 工具失败纠偏 / 执行到第 3 轮仍未完成时才升 strong；选档是确定性规则、不额外调模型，档位相对上一轮变化才发 `tier_changed`（首轮声明起始档），DONE 新增 `standard_calls` / `strong_calls` / `model_calls` 成本汇总。`AgentCore(cost_aware=False)` 关闭、执行轮恒 standard、行为与旧版一致，宿主可自定义规则松紧。见讲义第 32 篇。
- **结构化 tracing（OpenTelemetry，已落地，opt-in）**：新增可选 `[otel]` extra 与 `integrations/tracing/`，在**外侧订阅**事件流（不改 kernel/loop、库内不初始化 TracerProvider/Exporter、opentelemetry 不进硬依赖）：一次 run 映射为一个 root span，每次工具调用是其下子 span（成功 / 失败 / 耗时，无配对 CALL 的组合 / meta 工具结果补即时 span、流中断关闭未闭合 span），自适应决策落为 span event；`traced_astream` / `traced_run` 两个入口，宿主配好 OTLP 即可对接 Jaeger / Tempo。见讲义第 33 篇与 `docs/tracing.md`（中英）。
- **运行指标与就绪探针（metrics / readiness，已落地）**：零依赖 `metrics.py` 的 `MetricsRecorder` 在 `astream` 唯一出口旁路 tap，统计 runs（completed/failed/cancelled 分类）、策略、工具 total/errors、模型档位 standard/strong、事件计数与 run 时延直方图，`render_prometheus()` 直接吐 Prometheus 文本；FastAPI Battery 新增 `GET /readyz`（探针并发、5s 超时、逐探针隔离，失败只摘流量不重启；liveness `/health` 仍不查下游）与 `GET /metrics`（默认文本、`?format=json`，配 Key 时鉴权、`YAI_METRICS_PUBLIC=1` 可公开，GET 探针不限流）。可选 `[otel]` extra 的 `integrations/metrics.register_otel_meter` 用 observable 仪器采集时 pull 快照、库内不初始化 MeterProvider。多实例指标为 per-process、由 Prometheus 按 instance 抓取后查询层聚合。见讲义第 38 篇与 `docs/deployment-scaling.md` §7。

### v1.0 — 回流 AMBRACE

- 作为开源 AI 陪伴项目 [AMBRACE](https://github.com/Gi-Tuu/AMBRACE) 的 Agent 内核内嵌发布。
- 陪伴场景的主动行为、事件响应、记忆与工具权限在 YAI 上收敛，替换 AMBRACE 内硬编码的仲裁逻辑。
- 稳定的 SPI 兼容性承诺与语义化版本。

### v1.x 之后（方向，不排序）

- **代码生成工具（Code Tool）**：在组合工具之上，允许模型在沙箱里生成并执行新代码，产生宿主原本没有的能力。**内核侧已落地**：`ToolSandbox` SPI、`CodeToolManager`（注册/48h TTL/调用刷新/永久保留）、`create_code_tool` meta-tool、创建与首次执行双重授权、过期回收事件。**宿主侧已有教学级示例**：`examples/host_g_sandbox/` 用标准库子进程实现 `ToolSandbox`（`-I -S` 干净解释器 + 内置白名单 + 一次性进程 + 超时，无 import/open/反射；print 4k 字符、结果 64KB 上界；Unix 另加内存 256MB / CPU 15s rlimit），`tests/test_sandbox_example.py` 用真实子进程覆盖安全边界与端到端闭环。**生产级仍待宿主替换**：跑不可信代码应改用一次性容器 / gVisor / 微 VM（Firecracker）实现同一 SPI，内核无需改动；不提供沙箱则优雅降级为"无沙箱不可用"。
  - 授权（已落地）：除"无需审批"档外，创建与首次执行代码工具都必须经 `PermissionPolicy` 取得用户授权；
  - 生命周期（已落地）：代码工具默认临时保留 48h，期间被多次调用则刷新 TTL；宿主可在后台把某个工具置为永久保留（`retain_code_tool`），定时 `sweep_code_tools()` 回收过期工具；
  - 隔离（教学级示例已落地，生产级待宿主）：host_g 子进程沙箱默认无 import（无网络）、无文件、强制超时（`code_timeout`，默认 10s）、print 与结果大小上界；Unix 额外限内存/CPU，**Windows 无标准库 rlimit、内存不硬限、不做 OS 级网络隔离**；容器/资源上限等强隔离由生产宿主实现；
  - 与组合工具的关系：能用现有工具编排解决的优先走组合工具（零沙箱风险），确需新原始能力时才进入代码生成。
  - **生产级沙箱适配器（Docker 已落地，宿主侧，不进内核）**：`shell/desktop/docker_sandbox.py` 的 `DockerSandbox` 复用同一白名单 worker（源码剥到 `from __future__`、经 `python -c` 作为单个 argv 传入，纵深防御第二层），外层一次性容器做 OS 级硬隔离：`--rm`、`--network none`、`--read-only` + noexec tmpfs、`--cap-drop ALL`、`no-new-privileges`、`--memory`/`--cpus` cgroup（跨平台、补齐 Windows 无 rlimit）、`--pids-limit`；payload 走 stdin、不挂载代码卷；超时按唯一容器名 `docker rm -f` 兜底。真机端到端需启动 Docker Desktop（测试在无 Docker 时自动 skip）。按隔离强度递进的后续可选：②gVisor（runsc，系统调用拦截）；③微 VM Firecracker（最强隔离，冷启动稍重）；④远程 e2b / Daytona（执行移出本机）。每个适配器独立可选，内核零改动。
- 组合工具的可视化编排与版本管理（宿主侧）。

## 明确不做（防止内核膨胀）

- **不做 MCP Server**：YAI 是工具的使用方，不对外提供 MCP。
- **不做 Multi-Agent**：多智能体编排属于宿主层应用，不进内核。
- **不做向量记忆**：记忆后端是 SPI，向量方案由宿主/插件提供。
- **不做内置 UI**：界面（网页/桌面/Flutter）是宿主的事，内核只保证事件流足够驱动 UI。
- **不做 coding agent**：不以内核形态实现自主改代码的 Agent。

## 维护节奏

- 主干保护：main 始终保持全量测试 + ruff 绿色；CI 跑 Ubuntu 3.11/3.12/3.13 与 Windows 3.13 四矩阵。
- 小步提交：Conventional Commits，一个 PR 一件事；行为变化必须同步测试与 `docs/walkthrough/` 逐行讲义。
- 比赛验证后的成熟能力回流内核，内核保持通用、比赛分化留在各自材料与示例中。
