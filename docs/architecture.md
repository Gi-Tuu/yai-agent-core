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
- 边界：压缩只重排本次上下文、不改写 MemoryStore 中的持久历史；跨请求历史裁剪仍走 retention（见 2.2）。

### 2.6 并发模型与多租户隔离

YAI 是进程内内核，运行在 asyncio 单线程事件循环上，并发契约刻意收窄、默认安全：

- **一个 `AgentCore` 实例 = 一条串行会话**。实例内任意时刻只运行一个任务：`astream`/`run` 全程持有一把实例级 `asyncio.Lock`，并发调用会排队，而不会让两条任务在 `await` 点交错、互相污染共享的上下文、记忆、工具注册表与路由学习态。
- **要并发，就多实例**。多租户、多专员、多子员工等相互独立的会话各自实例化独立的 `AgentCore`；无状态的模型客户端、发现源、沙箱可作为单例被多个 core 共享，而 `registry / memory / learning / code_tool_manager` 等有状态部件必须随实例各自独立。子员工（`shell/employee`）与灵动岛的每个专员正各自持有独立 core，因此彼此仍可并行（受各自并发上限约束）。
- **流式提前关闭会释放锁**。异步生成器被 `aclose()`（或宿主 `break`/丢弃）时，`async with` 自动释放实例锁；宿主应正常耗尽或显式关闭事件流，否则下一个任务会一直等待。
- **后台维护方法是同步的**。`sweep_code_tools()` 与记忆 retention 等同步方法在事件循环线程内原子执行（无 `await` 让出点），写入采用快照迭代与原子替换，建议在任务间隙调用，无需也不应与任务锁混用。

这条契约把水平扩展简化为：**单机多会话 = 一进程多实例；跨机器 = 多副本 + 共享无状态的模型/发现后端**，内核自身不引入线程池、消息队列或分布式锁。

## 3. SPI 契约

| 契约 | 方法 | 默认实现 | 宿主何时替换 |
|---|---|---|---|
| ModelProvider | `achat(messages, tools, tier)` | OpenAICompatProvider（DeepSeek 等） | 接私有模型/本地模型 |
| Channel | `emit / ask / confirm` | CollectChannel、CliChannel | Web SSE、Flutter UI |
| MemoryStore | `history / put / get / clear` | InMemoryStore | SQLite、向量记忆 |
| PermissionPolicy | `check(tool, args) → allow/deny/ask` | AllowlistPolicy | 高危工具人工确认 |
| ToolDiscovery | `discover(need, *, task, available) → list[ToolSpec]` | StaticCatalog（进程内目录） | 接 MCP/OpenAPI/插件市场动态发现 |
| ToolSandbox | `execute(code, inputs, *, timeout=None) → SandboxResult` | 内核不提供（宿主实现；壳层/示例有 `SubprocessSandbox`、`DockerSandbox`） | 在隔离环境中执行生成代码 |
| RouteSelector | 选策略 + 观察反馈（上下文 bandit） | 内置自校准 bandit | 自定义路由/学习策略 |
| EmbeddingProvider | `embed_texts(list[str]) → list[vector]` | 无（可选；本地 bge-m3 或 OpenAI 兼容云端） | 用自有嵌入器做语义工具发现 |

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

- Claude/OpenAI Agents SDK、LangGraph：developer-builds-agent，工具/编排需显式定义，且绑定自家生态；
- agentic-kernel（TS）：嵌入式运行时理念相近，但仅 TypeScript 且无能力自发现/策略自适应；
- MCP：工具互操作**协议**，不是运行时；YAI 是 MCP 的消费者（v0.2 Client）而非竞争者；
- YAI：host-declares-capability、Python 进程内、模型/通道/存储全无关、自适应过程可观测。

## 7. 明确不做（内核边界）

以下能力**不进入内核本体**（`src/yai_core/`），以保持零硬依赖与可嵌入性；部分能力已由壳层或可选 extra 提供，在括注中说明。

- **MCP Server**：内核只做 MCP Client（连接外部 Server），自身不暴露为 Server。
- **对等 Multi-Agent 编排**：不做角色对等的多智能体协商；层级式"子员工委派"已在壳层落地（`shell/employee/`：单一根对用户负责，子员工权限单调收窄、带预算、完成即销毁）。
- **内核内执行生成代码**：组合工具与沙箱代码工具均已支持，但生成代码一律在宿主提供的 `ToolSandbox` 内执行（内核不内置执行器），并经授权闸与 TTL 生命周期管理。
- **向量长期记忆**：内核记忆不绑定向量库；本地 bge-m3 嵌入仅作为可选 extra（`[local-embed]`）用于语义工具发现，不充当长期记忆。
- **内置 UI**：内核不提供任何界面；灵动岛 / 浮窗位于壳层（`shell/desktop/`），经唯一嵌入点与内核通信。
- **coding agent 场景、队列 / 中间件、K8s**：不做无人值守编码代理，不引入消息中间件，部署不依赖 K8s（水平扩展见 `docs/deployment-scaling.md`）。
