# YAI Agent Core 架构设计

[English](architecture.en.md) · **简体中文**

## 1. 设计原则

1. **Library-first（进程内库）**：`pip install` 后运行在宿主进程内，不独立起服务、不要求云端。
2. **Host-declares-capability**：宿主只声明能力（普通函数/OpenAPI/MCP），Core 负责一切 Agent 逻辑。
3. **内核最小**：判断标准——"是不是任何宿主都需要？"否则进 batteries/ 或 adapter。
4. **零硬依赖**：内核只用标准库；openai / fastapi 为可选依赖、懒加载。
5. **自适应必须可解释**：每个决策产出 Observer 事件，禁止黑盒。
6. **模型/通道/存储无关**：通过 SPI 契约解耦，Core 给零配置默认实现。

## 2. 分层

```
宿主应用（AMBRACE / host_a / host_c）      只声明能力，零 Agent 代码
        │ 内省 / 注册
能力自发现 Auto-Discovery（函数 / OpenAPI[v0.3] / MCP Client[v0.2 已落地]）
        ▼
YAI Kernel
  ├─ Adaptive Router   任务分类 → direct/react/plan/clarify + 模型 tier
  │                     工具不足 → capability_missing → ToolDiscovery 补工具（本轮可用）
  ├─ Agent Loop        按策略执行（工具循环、计划拆解、澄清重入、缺口发现）
  ├─ Context           消息组装与 token 预算
  └─ Tool Bus          权限 → 执行（sync/async）→ 结构化结果
        ▲
SPI 契约环（可替换 + 默认实现）
  ModelProvider(OpenAI 兼容) / Channel(CLI·FastAPI·Flutter)
  MemoryStore(内存→SQLite→向量) / PermissionPolicy(auto/ask/deny)
  ToolDiscovery(StaticCatalog 进程内目录 → MCP/OpenAPI/插件市场)
        ▲
Integrations（可选，懒加载）：MCP Client（integrations/mcp/，[mcp] extra）、OpenTelemetry tracing（integrations/tracing/，[otel] extra，在外侧订阅事件流、不改 kernel）
Batteries（可选）：FastAPI Server、SQLite Memory
```

### 2.1 MCP Client 集成（v0.2 已落地）

```
MCP Server（HTTP / stdio 子进程 / 内存实例）
   │  list_tools / call_tool（MCP Python SDK v2，可选依赖、懒加载）
   ▼
McpToolBridge（integrations/mcp/client.py）
   ├─ sanitize_schema      清洗 SDK 生成 schema 里的私有键
   ├─ _to_spec             每个远端工具 → ToolSpec(source="mcp")，handler 为异步闭包
   └─ _flatten_call_result content 文本 / structured_content / is_error → 返回值或异常
   ▼
ToolRegistry（与 Native 工具同构，Router/Loop/Executor 零感知）
```
- 内核本体不 import mcp（AST 测试守红线）；`uv sync --extra mcp` 才启用。
- 连接三态：`McpServerConfig.url`（Streamable HTTP）/ `command+args`（stdio 子进程）/
  `server`（内存实例，离线测试用）；`prefix` 解决多 Server 工具重名。
- 生命周期：`async with McpToolBridge(...)` 或 `connect()/aclose()` 手动管理；
  stdio 形态必须 aclose，否则子进程成为孤儿。

### 2.2 持久化记忆（SQLite，v0.2 已落地）

`memory/sqlite_store.py` 提供 `SqliteStore`：MemoryStore 契约的 SQLite 实现（标准库、
零新依赖）。history/kv 两张表，`scope` 列做会话隔离，`PRAGMA user_version` 做迁移钩子，
WAL + 单锁覆盖单进程并发；`history()` 保持契约的同步签名。装配走 `YAI_DB_PATH`
（`SqliteStore.from_env()`），未设置时回退 InMemoryStore，线上默认不开启（免费 PaaS 临时盘）。
Loop/Core/宿主示例零改动。边界（历史裁剪、多进程写、临时盘持久性）见 walkthrough 第 11 章 H 节。
历史保留策略（v0.3，opt-in）：`memory/retention.py` 提供纯函数 count_cut/ttl_cut，
两种 Store 以构造参数 max_messages / ttl_seconds / clock 对等支持；裁剪边界按"轮"
对齐（一条 user 到下一条 user 之前为一轮），保证 assistant(tool_calls)/tool 配对
不被拆散；SQLite 构造时自动 prune 一次覆盖重启场景。MemoryStore 契约与 schema
版本（仍为 v1）不变；KV 的 TTL 需 schema v2 迁移，留待 v0.4。`Context` 的字符预算
硬裁剪（原私有 `_compact`，现公共方法 `enforce_budget`）同为轮边界丢弃（kernel 内同源实现）；
v0.7 起在其之上新增摘要压缩，见 2.5 节。

### 2.3 OpenAPI 发现（v0.2 已落地）

`integrations/openapi/` 把任意 OpenAPI 3 描述自动翻译成 ToolSpec(source="openapi")（alias 仅用于日志与 notes）：
spec.py 负责载入/版本闸/内部 $ref 内联（循环与外部引用保留并记 notes），discovery.py
纯函数地把 operations 规划成工具（operationId/合成命名、path/query/body 入参合并、
include/exclude、read_only 只留 GET/HEAD、max_operations=40 截断），client.py 用懒加载的
httpx 执行（bearer/apiKey 鉴权令牌走环境变量、调用期校验、204/非2xx/JSON/文本截断归一）。
httpx/pyyaml 在 [openapi] extra，内核本体零硬依赖；测试全部走 httpx.MockTransport 离线。
与 MCP 桥同构，在 serve_example 的 app_lifespan 中顺序挂载、共用关闭列表。

### 2.4 能力按需发现闭环（ToolDiscovery，已落地最小切片）

```
LLM 路由判定现有工具不足（missing_capability）
   │  发 capability_missing 事件（含缺口、可用工具清单）
   ▼
ToolDiscovery.discover(need, task, available) → list[ToolSpec]
   │  内置实现 StaticCatalog：进程内候选 + 显式关键词匹配（零依赖、离线可测）
   │  未来实现：MCP 目录 / OpenAPI 服务目录 / 插件市场
   ▼
内核统一去重（候选间同名 + 已注册）→ ToolRegistry.register_many
   │  发 tool_discovered 事件（missing / source / registered）
   ▼
本轮 ReAct：新工具已在系统提示词与工具清单中，模型本轮即可调用
   └─ 执行仍走 PermissionPolicy（发现≠授权；发现源异常仅发 error 事件，不致命）
```

- 发现源只交候选规格，**注册/去重/权限归内核**，外部来源无法绕过审计。
- 发现发生在系统提示词构建之前，因此新工具在**同一轮**就对模型可见，不必等下一次任务。
- 安全默认：`discovery=None` 时只发缺口事件、不动态注册任何工具，老宿主行为不变。
- 边界：当前只在"路由判定缺口"时发现一次；执行中途反思、语义检索、动态换工具属 v0.7/v0.8。

### 2.5 上下文摘要压缩（Context Compaction，v0.7 已落地，opt-in）

```
ReAct 每轮模型调用前：Context.total_chars() 超 hard_max_chars 高水位？
   │  否 → 照常进入模型
   ▼ 是
select_compaction_range：start=最早旧 user 轮首，end=倒数 keep_recent_rounds 个 user 轮首
   │  render_for_summary：旧轮转纯文本（用户 / 助手[调用工具] / 工具结果）
   ▼
摘要模型 achat([摘要指令, 旧轮文本], tools=None)
   │  成功 → replace_range 把区间换成一条 system「早期对话摘要」，发 context_compacted 事件
   └─ 异常 / 空摘要 → 回退 enforce_budget() 硬裁剪（轮边界、不拆工具对），不致命
   ▼
最近 keep_recent_rounds 个完整轮原样保留，长对话不丢早期目标与事实
```

- 三段式窗口：system（index 0）永留 / 旧轮压成一条摘要 / 最近完整轮全保留；`replace_range` 强制 `start≥1`、两端对齐 user 轮首，消息结构对模型永远合法。
- 摘像是增强不是依赖：模型异常、超时、空摘要一律退回已验证的硬裁剪 `enforce_budget()`。
- 开关：`AgentCore(context_compaction=True)` 用默认 `CompactionPolicy`（hard 24000 / target 16000 / keep 2 轮），也可传自定义 policy；默认关闭，老宿主行为不变。灵动岛真机工厂已开启。
- 摘要可走专用小模型（省钱）：`AgentCore(compaction_model=小模型)` 后，压缩这一次额外调用改走它、主模型专心做任务；不传（`None`）则完全回归主模型。摘要模型是对象参数、不进 `CoreConfig`，且压缩调用与路由分类一样不计入任务预算。
- 水位便捷构造：`CompactionPolicy.from_context_window(chars, *, hard_ratio=0.6, target_ratio=0.4)` 按"会话字符工作区"比例推算高/低水位，`from_context_window(40000)` 恰等于默认 24000/16000；坚持字符口径、不引入 tokenizer，非法比例 / 过小窗口在装配期抛错。
- 边界：压缩只重排本次上下文、不改写 MemoryStore 中的持久历史；跨请求历史裁剪仍走 retention（见 2.2）。

### 2.6 并发模型与多租户隔离

YAI 是进程内内核，运行在 asyncio 单线程事件循环上，并发契约刻意收窄、默认安全：

- **一个 `AgentCore` 实例 = 一条串行会话**。实例内任意时刻只运行一个任务：`astream`/`run` 全程持有一把实例级 `asyncio.Lock`，并发调用会排队，而不会让两条任务在 `await` 点交错、互相污染共享的上下文、记忆、工具注册表与路由学习态。
- **要并发，就多实例**。多租户、多专员、多子员工等相互独立的会话各自实例化独立的 `AgentCore`；无状态的模型客户端、发现源、沙箱可作为单例被多个 core 共享，而 `registry / memory / learning / code_tool_manager` 等有状态部件必须随实例各自独立。子员工（`shell/employee`）与灵动岛的每个专员正各自持有独立 core，因此彼此仍可并行（受各自并发上限约束）。
- **流式提前关闭会释放锁**。异步生成器被 `aclose()`（或宿主 `break`/丢弃）时，`async with` 自动释放实例锁；宿主应正常耗尽或显式关闭事件流，否则下一个任务会一直等待。
- **后台维护方法是同步的**。`sweep_code_tools()` 与记忆 retention 等同步方法在事件循环线程内原子执行（无 `await` 让出点），写入采用快照迭代与原子替换，建议在任务间隙调用，无需也不应与任务锁混用。

这条契约把水平扩展简化为：**单机多会话 = 一进程多实例；跨机器 = 多副本 + 共享无状态的模型/发现后端**，内核自身不引入线程池、消息队列或分布式锁。

### 2.7 本地诊断事件（Diagnostics，零依赖旁路）

内核在"已静默兜底、主流程不失败、但运维排障必须看得见"的瞬间，会通过一个**同步、零依赖、永不反噬主流程**的回调发出 `DiagnosticEvent`。它与另外三套可观测手段分工明确，互不替代：

| 通道 | 面向 | 回答什么 | 触发方 |
|---|---|---|---|
| `AgentEvent`（事件流） | 产品 / 终端用户 | Agent **正在做什么决策与动作**（路由、工具调用、发现、授权…） | 内核主流程，必经、可回放 |
| `DiagnosticEvent`（本小节） | 本地运维 / 排障 | 内核**在哪些增强环节悄悄降级了**（模型超时、分类回退、发现失败、压缩回退…） | 仅当宿主传入 `on_diagnostic` 回调 |
| Metrics（零依赖收集器，见下） | 运维 / 成本看板 | 这段时间**整体跑得怎么样**（run 成败计数、工具失败率、强档模型调用量、时延分布） | `astream` 唯一出口旁路 tap，默认开启 |
| OpenTelemetry tracing（可选 extra） | 分布式 / 跨服务 | 一次请求跨进程的**链路、耗时、归因** | 宿主在外侧订阅事件流，内核不初始化 exporter |

- **数据结构**：`DiagnosticEvent(kind, message, level, data)`。`level ∈ debug/info/warning/error`；`kind` 当前覆盖 8 类：`model_error`、`model_timeout`、`model_fallback`、`router_llm_fallback`、`discovery_failed`、`discoverable_list_failed`、`compaction_fallback`、`schema_focus_fallback`。
- **安全语义**：回调是同步的，宿主应让它快速、非阻塞（写一行日志、推一个监控计数即可）；回调内部抛出的任何异常都会被内核吞掉，诊断是旁路，绝不能因为"日志系统挂了"而拖垮 Agent 主流程。内核不依赖 `logging`，不把任何日志库变成硬依赖。
- **接线方式**：`AgentCore(model, on_diagnostic=fn)` 一处传入，内核会把同一回调鸭子接线给内部的 `AdaptiveRouter` 与 `FallbackModelProvider`（它们暴露可写的 `on_diagnostic` 属性；宿主自建、不认识该属性的自定义组件不受影响）。不传回调时内核完全安静，行为与旧版本一致。
- **刻意不纳入诊断的情况**：沙箱执行失败会冒泡成 `tool_result(ok=False)`、权限拒绝会发 `permission_asked`——这些本就是用户可见的业务事件，不属于"静默兜底"，因此不重复发诊断。

**Metrics（运行指标，零依赖）**：`metrics.py` 的 `MetricsRecorder` 在 `astream` 唯一出口做旁路 tap——run 开始打点、逐事件累计、`finally` 按结局结算，统计 runs（started/completed/failed/cancelled，取消类与真实异常分开）、按策略计数、工具 total/errors、模型档位 standard/strong（DONE 一次性结算）、事件计数与 run 墙钟时延直方图（`monotonic` 测时）。它只用标准库（一把 `threading.Lock` 兼容跨线程抓取，`snapshot()` 深拷贝），`render_prometheus()` 直接吐 Prometheus 文本，FastAPI Battery 经 `GET /metrics`（默认文本、`?format=json` 快照）暴露，配了 API Key 时默认鉴权、`YAI_METRICS_PUBLIC=1` 可显式公开；另有 `GET /readyz` 就绪探针（并发、5s 超时、逐探针隔离，失败只摘流量不重启）。想走 OTel 指标管线时，用可选 `[otel]` extra 的 `integrations/metrics.register_otel_meter`——全部 observable 仪器、采集时才从快照 pull（run 热路径零回调），库内不初始化 MeterProvider。与 diagnostics 相同，指标回调里的任何异常都被吞掉，绝不反噬主流程。详见逐行讲义第 38 篇与 `docs/deployment-scaling.md` §7。

### 2.8 生产加固 P1：预算熔断、落盘加密、集中配置、在线热备份

在 P0（并发契约 / 硬超时 / 诊断）之上，P1 收敛四类与**成本和数据安全**相关的生产风险；四项均默认关闭或默认与旧版等价、内核零第三方硬依赖、全部离线可测。

- **任务级预算硬上限与熔断（防失控烧钱）**：`AgentCore(budget=Budget(max_model_calls=, max_tool_calls=, max_tokens=))`，`None` 表示不限制、`0` 表示一次都不允许。loop 在顶层任务建一个 `BudgetTracker`（澄清递归复用同一账本）：模型调用在统一入口 `_model_chat` 计数（**不含**路由分类与上下文压缩），普通工具在执行分支计数（meta-tool 与组合编排不重复计、组合工具对外算 1 次），token 仅在模型回传真实 `usage` 时累计（缺失不估算）。任一上限触顶立即终止，发 `budget_exceeded` 事件（带 scope/used/limit 与账本快照），且熔断异常在两处容错回退中被显式重抛，不会被"降级"误吞。
- **长期记忆落盘静态加密（第九个 SPI，opt-in）**：`spi/cipher.py` 定义同步 `StorageCipher`（`encrypt/decrypt(bytes)`）；`MemoryVault(path, cipher=...)` 落盘透明加密、读取解密，原子 `os.replace` 写盘；文件缺失、JSON 损坏或**解密失败（密钥不符 / 密文被篡改）一律冷启动为空库并重写**，保证进程永远起得来。不传 cipher 时仍为明文 JSON（向后兼容，文档明确告知）。算法不进内核：`[crypto]` extra 提供基于 Fernet（认证加密）的 `FernetStorageCipher`，支持 `generate_key()` 与 `from_env("YAI_VAULT_KEY")`（缺环境变量直接报错，不静默生成一次性密钥）。
- **集中配置 `CoreConfig`（防错配、易复用）**：`config.py` 的零依赖 dataclass 收敛 `AgentCore` 全部与模型无关的可选项，默认值与 kwargs 一致；`validate()` 在构造早期拒绝非法取值（次数非正、bool 冒充整数、`llm_router` / `context_compaction` / `budget` / `model_timeout` 类型错误）。`AgentCore.from_config(model, config)` 让多专员 / 多子员工复用同一份预算与开关；公共 kwargs 签名与默认行为不变。
- **SQLite 在线热备份与恢复（防停服备份 / 数据损坏）**：`SqliteStore.backup_to(dest)` 用标准库 `Connection.backup` 在实例锁内做页级一致快照，无需停服，文件库与 `:memory:` 均可；`restore_to(backup, dest)` 离线恢复，备份损坏时直接抛 `sqlite3.DatabaseError` 而非静默起空库。崩溃恢复由 WAL 与原子写保证（close 后同路径重开数据不丢）。

### 2.9 长期记忆语义召回与任务起始主动召回（opt-in）

结构化长期记忆（§2.2 的 `MemoryVault`）在词法召回之外，新增两条**默认关闭、内核零硬依赖**的增强：

- **词法 OR 语义双通道召回**：`MemoryVault.arecall(query, *, k, kinds, tags)` 是异步入口。词法通道沿用 token 相关度；当宿主经 `AgentCore(memory_embedder=...)` 注入第八个 SPI `EmbeddingProvider` 时，语义通道把 query 与记忆内容编码后做余弦相似度（复用 `discovery/semantic.py` 的标准库实现，阈值 0.45）。两通道按记忆 id 取**并集**、融合分取 `max(词法, 语义)` 再叠重要度加权，跨语言 / 换说法（中文记忆 vs 英文 query）也能召回。同步 `recall` 保留为纯词法、行为不变；`recall` 工具 handler 改异步并统一走 `arecall`。记忆向量按需懒批量计算并按 id 缓存，`remember` 合并 / `forget` 删除即令旧向量失效；embedder 抛错或返回数量不符时**降级纯词法**并记录 `semantic_last_error`（`status()` 暴露 `semantic_enabled` / `semantic_last_error`），召回不因向量服务故障而失败。
- **任务起始主动召回**：`AgentCore(auto_memory_recall=True, auto_recall_k=3)` 时，loop 在历史之后、user 任务之前自动用任务文本调一次召回，命中则作为一条 `system` 消息注入（文案明确"若与用户本次明确指令冲突，以本次指令为准"，避免陈旧画像压过当前指令），并发 `memories_recalled` 事件（条数 + 每条 id/kind/摘要）。信息不足的 clarify 反问路径不召回；召回回调任何异常只发本地诊断、绝不阻断主任务。loop 只依赖一个 `async (task) → list[dict]` 回调（Core 用 `functools.partial(vault.arecall, k=...)` 注入），不反向依赖 vault / embedder。`CoreConfig.validate()` 在构造期拒绝"开召回但未开长期记忆""k 非正整数""embedder 非 async `embed_texts`"三类非法组合。

### 2.10 工具结果有界收口与工具目录确定性排序（上下文轻量治理）

在 §2.5 的整轮压缩之外，再补齐两处"喂给模型的字节"的轻量化；均内核零硬依赖、全部离线可测，且默认不改变小结果行为：

- **单个工具结果统一有界收口（C4）**：`ToolExecutor` 对原生函数、代码工具（沙箱输出）、组合工具（`{"steps":[...]}`）三类成功结果，在 `json.dumps` 之后、写回对话上下文之前，统一经纯函数 `bound_tool_result(text, limit)` 收口：默认上限 **16000 字符**（与 OpenAPI 集成的 `max_result_chars` 对齐），超限保留**头 70% + 尾 30%**，中间插入显式省略标记（写明省略字符数），并以收紧循环保证结果严格不越上界，绝不静默截断。`AgentCore(tool_result_char_limit=...)` 可覆盖，传 `None` 关闭；`CoreConfig.validate()` 在装配期拒绝非正整数与 bool。`TOOL_RESULT` 成功事件新增 `truncated` 与 `result_chars`（收口前原始字符数），事件 `preview` 仍只取前 200 字；未超限的小结果逐字不变。它位于"工具结果 → 上下文"最靠近源头的一层，与 §2.5（整轮 24000 字符压缩）和在线 API 的响应体守卫 `payload_guard`（HTTP 出口 256KB）构成三道互不替代的上界。
- **工具目录确定性排序（C1）**：`ToolRegistry` 喂给模型的 `catalog()` / `catalog_text()` / `describe()` / `llm_schemas()` 一律经内部 `_ordered()` 按工具名排序；内部存储视图 `all()` 仍保持注册顺序，按需取完整 schema 的 `schemas_for(names)` 仍遵循调用方给定顺序。这样系统提示里的工具清单字节只在工具集**真正变化**（增删 / 改名）时才改变，避免宿主注册顺序的偶然波动无谓击穿提示缓存的稳定前缀，也让快照测试确定。

## 3. SPI 契约

| 契约 | 方法 | 默认实现 | 宿主何时替换 |
|---|---|---|---|
| ModelProvider | `achat(messages, tools, tier)` | OpenAICompatProvider（DeepSeek 等）、AnthropicProvider（Claude） | 接私有模型/本地模型 |
| Channel | `emit / ask / confirm` | CollectChannel、CliChannel | Web SSE、Flutter UI |
| MemoryStore | `history / put / get / clear` | InMemoryStore | SQLite、向量记忆 |
| PermissionPolicy | `check(tool, args) → allow/deny/ask` | AllowlistPolicy | 高危工具人工确认 |
| ToolDiscovery | `discover(need, *, task, available) → list[ToolSpec]` | StaticCatalog（进程内目录） | 接 MCP/OpenAPI/插件市场动态发现 |
| ToolSandbox | `execute(code, inputs, *, timeout=None) → SandboxResult` | 内核不提供（宿主实现；壳层/示例有 `SubprocessSandbox`、`DockerSandbox`） | 在隔离环境中执行生成代码 |
| RouteSelector | 选策略 + 观察反馈（上下文 bandit） | 内置自校准 bandit | 自定义路由/学习策略 |
| EmbeddingProvider | `embed_texts(list[str]) → list[vector]` | 无（可选；本地 bge-m3 或 OpenAI 兼容云端） | 语义工具发现；长期记忆语义召回（`memory_embedder`） |
| StorageCipher | `encrypt / decrypt(bytes) → bytes` | 无（可选；`[crypto]` extra 提供 FernetStorageCipher） | 长期记忆 vault 落盘静态加密（KMS / 密钥链 / 国密） |

## 4. Adaptive Router 状态机

```
任务文本 + 可用工具集
   ├─ 无工具                → direct（模型直接答）
   ├─ 模糊短句              → clarify（Channel.ask 后重入）
   ├─ 含行动词 + 多步骤信号 → plan（先拆解，再 react）
   └─ 含行动词              → react（工具循环）
```

v0.2（已落地）：LLM 一次性分类输出 `{strategy, reason, tier, missing_capability}`，异常/超时/非法输出回退规则路由；
两条路径统一返回 RouteDecision，`strategy_selected` 事件带 `source=llm|rules` 可审计。
LLM 路由还会做能力边界感知：现有工具不足以完成任务时，在 `strategy_selected` 之后发出
`capability_missing` 事件（含缺口描述与可用工具清单）；若注入了 `ToolDiscovery` 发现源，
内核随即把缺口闭环成新工具（见 §2.4），否则只发事件、由宿主决定后续引导；规则路径、
澄清与无工具部署不产生该事件。`llm_router="auto"` 时按模型后端的 `yai_live_router`
能力标记决定是否启用 LLM 分类（真实后端声明、离线脚本模型不声明）。

## 5. 启动在线 API（batteries）

```python
import os, uvicorn
from yai_core import AgentCore, OpenAICompatProvider
from yai_core.batteries.fastapi_server import create_app
import my_app_capabilities as cap

core = AgentCore.auto(cap, OpenAICompatProvider())
app = create_app(core)
# 环境变量：YAI_GIT_COMMIT=<40位 commit>、YAI_PROJECT_SLUG=<slug>
# 容器内监听 8000；宿主机端口由部署侧映射（本项目约定 8001:8000）
# uvicorn module:app --host 0.0.0.0 --port 8000
```

v0.3 评审期限流（默认开启）：`rate_limit.py` 提供每 IP 滑动窗口（`enabled=True`、
`per_minute=30`、`window_seconds=60`，可用 `YAI_RATE_LIMIT_*` 调整或关闭）。中间件只拦
`POST /v1/agent/run`，超限返回 429 + `Retry-After`（与响应体同源），被限请求不触达
模型；三个 GET 验证端点永不限流（评审硬门槛）。客户端身份取 XFF 最左非空、回退对端
地址；状态只在内存，重启清零；全局限流兜底不做（单实例）。

## 6. 与生态的差异化（诚实对比，用于比赛材料）

- 模型厂商自带的 Agent SDK、图编排框架：developer-builds-agent，工具/编排需显式定义，且常绑定自家生态；
- agentic-kernel（TS）：嵌入式运行时理念相近，但仅 TypeScript 且无能力自发现/策略自适应；
- MCP：工具互操作**协议**，不是运行时；YAI 是 MCP 的消费者（v0.2 Client）而非竞争者；
- YAI：host-declares-capability、Python 进程内、模型/通道/存储全无关、自适应过程可观测。

## 7. 明确不做（内核边界）

以下能力**不进入内核本体**（`src/yai_core/`），以保持零硬依赖与可嵌入性；部分能力已由壳层或可选 extra 提供，在括注中说明。

- **MCP Server**：内核只做 MCP Client（连接外部 Server），自身不暴露为 Server。
- **对等 Multi-Agent 编排**：不做角色对等的多智能体协商；层级式"子员工委派"已在壳层落地（`shell/employee/`：单一根对用户负责，子员工权限单调收窄、带预算、完成即销毁）。
- **内核内执行生成代码**：组合工具与沙箱代码工具均已支持，但生成代码一律在宿主提供的 `ToolSandbox` 内执行（内核不内置执行器），并经授权闸与 TTL 生命周期管理。
- **不内置向量数据库 / 向量索引**：内核记忆不绑定任何向量库或 ANN 索引。长期记忆的语义召回（§2.9）复用可选的 `EmbeddingProvider`（本地 bge-m3 经 `[local-embed]` extra，或 OpenAI 兼容云端）对记忆内容**即时 / 懒缓存编码**后做余弦比对，规模面向单宿主的结构化记忆（默认 top-k 少量条目），不引入常驻向量数据库；需要大规模向量检索的宿主可自行实现 `MemoryStore` / 召回回调接入。
- **内置 UI**：内核不提供任何界面；灵动岛 / 浮窗位于壳层（`shell/desktop/`），经唯一嵌入点与内核通信。
- **coding agent 场景、队列 / 中间件、K8s**：不做无人值守编码代理，不引入消息中间件，部署不依赖 K8s（水平扩展见 `docs/deployment-scaling.md`）。
