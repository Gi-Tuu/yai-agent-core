# 逐行讲解 00 · 导读：一次请求穿过整个 Core

> 这套文档对 `src/yai_core/` 每个文件做逐块、逐行解释，面向"边学边造"的你。
> 读法：先读本篇建立全局地图，再按编号逐篇对照源码看。每篇末尾有自检问题，答得上来再往下走。

## 0. 先记住一句话

**宿主只提供普通函数；Core 把函数变成工具，自己决定怎么用工具完成任务，并把过程一步步喊出来。**

## 1. 一张全景图（数字就是执行顺序）

```
你的任务 "先查看状态，然后检索约定记忆，给出关怀建议"
  │
  │ ① AgentCore.auto(宿主模块, 模型)            【core.py】
  │    └─ discover(宿主)                         【discovery/introspect.py】
  │         读函数签名/docstring → ToolSpec 列表 → 注册进 ToolRegistry【tools/registry.py】
  │    └─ （可选）attach_mcp_tools 接入外部 MCP 工具，同样注册进 ToolRegistry
  │                                            【integrations/mcp/client.py】
  │
  │ ② await core.run(任务)                       【core.py】
  │    └─ AgentLoop.astream(任务)                【kernel/loop.py】
  │
  │ ③ Router.classify(任务, 注册表) → 策略        【kernel/router.py】
  │    没有工具→direct / 行动词→react / 多步+行动→plan / 太模糊→clarify
  │    （可选）自校准学习器按"同类任务历史成败"建议策略【learning/，第 19 篇】
  │    工具不足且模型报缺口 → capability_missing 事件
  │      └─（可选）ToolDiscovery 发现新工具 → 注册 → 本轮即可用【discovery/catalog.py】
  │           发现匹配：词法打分（零依赖）+ 可选 embedding 语义双通道【discovery/scoring.py、semantic.py，第 20 篇】
  │
  │ ④ Context 组装消息列表（含刚发现的工具）      【kernel/context.py】
  │    超预算时旧轮压成摘要、recent 完整保留（第 25 篇）【kernel/compaction.py】
  │
  │ ⑤（plan 时）强模型拆步骤，再补"按计划执行"指令；执行中计划走不通可
  │     调 replan 显式重规划（第 31 篇）；每轮按成本·延迟自动选 standard/strong（第 32 篇）
  │
  │ ⑥ ReAct 循环                                 【kernel/loop.py】
  │    模型.achat(消息, 工具清单)                 【llm/openai_compat.py / 任何 SPI 模型】
  │      ├─ 模型不调工具 → 最终答案，结束
  │      ├─ 模型调 meta-tool（内核自己拦截，不进执行器）
  │      │     ├─ request_capability → 发现注册新工具（第 15 篇）
  │      │     ├─ replan → 计划走不通时显式重规划（第 31 篇，仅 PLAN）
  │      │     ├─ compose_tool → 组合已有工具（第 16 篇）
  │      │     └─ create_code_tool → 注册代码工具（第 18 篇，需宿主沙箱）
  │      └─ 模型要调普通工具 → ToolExecutor.execute  【tools/executor.py】
  │             ├─ PermissionPolicy.check 权限   【policy/allowlist.py】
  │             ├─ 普通函数直接执行 / composite 逐步编排 / code 转交宿主沙箱
  │             └─ 单结果先按 16000 字符有界收口（头+尾+省略标记，第 41 篇），
  │                再包成 tool 消息塞回 Context，继续循环
  │
  │ ⑦ 全过程的每个动作变成 AgentEvent，经 Channel 往外发  【channels/】
  │    └─（可选）外侧订阅者 traced_astream 把同一条事件流转成一条 OTel trace（第 33 篇）【integrations/tracing/】
  │    └─ 旁路 MetricsRecorder 在唯一出口累计 runs/工具/模型档位/时延（第 38 篇）【metrics.py】
  │         └─ GET /metrics 吐 Prometheus 文本；（可选）OTel meter 采集时 pull【integrations/metrics/】
  │         └─ GET /readyz 就绪探针（失败只摘流量不重启）【batteries/fastapi_server/readiness.py】
  │ ⑧ 历史写进 Memory（InMemoryStore / SqliteStore）【memory/】
  │ ⑨（可选）任务结束后从事件流抽成败，回灌 learning 学习器  【learning/】
  └ 返回 RunResult（策略 + 事件列表 + 最终文本）
```

## 2. 文件分工速查

| 文件 | 角色 | 类比 |
|---|---|---|
| `types.py` | 全项目通用的数据结构 | 普通话词典 |
| `spi/*.py` | 十个"插槽"的接口约定（model/channel/memory/policy/discovery/sandbox/learning/embedding/cipher/resource） | 插座标准 |
| `discovery/introspect.py` | 把函数读成工具规格 | 自动翻译官 |
| `discovery/catalog.py` | 按需能力目录：缺口出现时补工具（最小发现源，关键词） | 备用零件柜 |
| `discovery/scoring.py` | 词法相关度打分（规范化、中文 bigram、同义词，零依赖） | 字面匹配的标尺 |
| `discovery/semantic.py` | 双通道语义发现 `SemanticCatalog`（词法 + embedding，标准库余弦） | 懂同义改写的检索员 |
| `integrations/embedding/` | 真实嵌入后端：本地 bge-m3 ONNX / OpenAI 兼容云端（可选 extra） | 语义向量的发动机 |
| `tools/registry.py` | 工具花名册（喂模型的目录 / schema 按名确定性排序，第 41 篇） | 电话簿 |
| `tools/executor.py` | 真正调用工具的地方（组合工具在此分流；单结果有界收口，第 41 篇） | 总机 |
| `tools/health.py` | 能力接入体检（不执行 handler，列出声明的约束与执行能力不匹配处，第 50 篇） | 接入前质检员 |
| `tools/executor.py` + `types.py` | 可信变更预览（宿主渲染 diff、审批展示、执行前 token 复核，第 51 篇） | 审批前的验算员 |
| `tools/composer.py` | 组合工具：把已有工具编排成新工具（不越界、逐步授权） | 可复用的流水线模板 |
| `tools/code_tools.py` | 代码工具注册表：48h TTL、调用刷新、永久保留、过期回收 | 临时工的考勤与合同 |
| `tools/manifest.py` | 能力清单（manifest）批量打包 + JSON 文件 IO | 能力的快递箱 |
| `tools/meta.py` | meta-tool：`request_capability`（发现）、`replan`（重规划）、`create_code_tool`（造代码工具） | 向内核自己提需求的按钮 |
| `kernel/tiering.py` | 成本·延迟感知选档（standard/strong，规划/失败/后段才升强档，零依赖） | 自动换挡器 |
| `spi/sandbox.py` | 第六个插槽：代码工具沙箱（只定义契约，执行由宿主实现） | 高危车间的安全规程 |
| `kernel/router.py` | 决定用哪种打法 | 作战参谋 |
| `learning/` | 自校准路由：特征/反馈/上下文老虎机（从执行结果学习，opt-in） | 会复盘的作战参谋 |
| `kernel/context.py` | 管理发给模型的消息 | 剪贴板 |
| `kernel/compaction.py` | 超预算时把旧轮压成摘要（失败回退硬裁剪） | 会做会议纪要的秘书 |
| `kernel/loop.py` | 主循环（心脏） | 发动机 |
| `memory/inmemory.py` | 默认记忆 | 短期便签本 |
| `memory/sqlite_store.py` | 持久化记忆（opt-in） | 带锁的笔记本 |
| `memory/vault.py` | 结构化长期记忆：remember/recall 事实档案（opt-in） | 可主动翻的档案柜 |
| `memory/retention.py` | 历史裁剪纯函数（条数/TTL，轮边界对齐） | 会碎纸的碎纸机 |
| `policy/allowlist.py` | 默认权限 | 门禁 |
| `channels/*.py` | 默认输入输出 | 显示屏/话筒 |
| `llm/openai_compat.py` | 真实模型适配 | 翻译给 DeepSeek 听 |
| `core.py` | 对外唯一门面 | 前台 |
| `integrations/mcp/client.py` | 接入外部 MCP Server 的工具 | 外接设备转接头 |
| `integrations/openapi/` | OpenAPI 发现（opt-in） | REST API 的自动翻译官 |
| `batteries/fastapi_server/app.py` | 包成 HTTP 服务 | 对外营业窗口 |
| `batteries/fastapi_server/ratelimit.py` | 评审期限流（每 IP 滑动窗口） | 门口的取号机 |
| `batteries/fastapi_server/payload_guard.py` | 响应体大小守卫（三级截断、标记不静默） | 行李限重秤 |
| `batteries/fastapi_server/auth.py` | API Key 鉴权（Bearer / X-API-Key，常量时间比较） | 门卫查证件 |
| `discovery/remote_sources.py` | 远程发现源：MCP / OpenAPI 懒连接、按 need 双通道收窄（空命中回退全量） | 外部供应商名录 |
| `discovery/aggregate.py` | `CompositeDiscovery`：多个发现源串成一个、跨源去重 | 总台汇总窗口 |
| `shell/desktop/docker_sandbox.py` | 生产级沙箱：一次性容器 OS 级隔离（无网/只读/限资源） | 加固的隔离车间 |
| `evaluation/` | 端到端离线评测：完成率、工具精确/召回、越权、无支撑断言（golden 回归基线） | 考完打分的判卷人 |
| `integrations/tracing/otel.py` | 结构化 tracing：外侧订阅事件流转成一条 OpenTelemetry trace（可选 `[otel]` extra，不改内核） | 给整场手术录像并标注每一刀 |
| `metrics.py` | 零依赖运行指标收集器：runs/工具/模型档位/事件计数 + 时延直方图 + Prometheus 文本（第 38 篇） | 店铺的每日流水账 |
| `batteries/fastapi_server/readiness.py` | 就绪探针：并发执行、超时/异常隔离，失败只摘流量不重启（第 38 篇） | 开门营业前的逐项点检表 |
| `integrations/metrics/otel_meter.py` | OTel 指标桥接：observable 仪器采集时从快照拉值（可选 `[otel]` extra，第 38 篇） | 把流水账自动誊抄到总部报表系统 |
| `examples/five_beats.py` | 第三方播种五拍标准线：把事件流归纳为 唤醒/长尾/缺口/补能力·派子员工/授权/完成 并断言顺序（第 39 篇） | 一次成功接管的验收工序卡 |

## 3. 贯穿全项目的 5 个 Python 语法（先混个脸熟，后面逐个细讲）

1. `from __future__ import annotations`：让类型注解延迟成字符串，写新式注解不报错。
2. `async def / await / async for / yield`：异步与异步生成器，loop.py 的核心。
3. `@dataclass`：几行代码写出"只装数据的类"。
4. `Protocol`：不继承也能算"符合接口"（鸭子类型的书面版）。
5. `X | None`、`list[dict]`：新式类型注解。

## 4. 分篇目录

- 01 · `types.py`：数据词汇表
- 02 · `spi/`：可替换插槽（model/channel/memory/policy；Discovery 见第 15 篇、Sandbox 见第 17 篇）
- 03 · `discovery/introspect.py`：能力自发现
- 04 · `tools/`：注册表 + 执行总线
- 05 · `kernel/router.py` + `context.py`：决策与上下文
- 06 · `kernel/loop.py`：主循环逐行（最重要）
- 07 · 默认实现：memory / policy / channels
- 08 · `llm/openai_compat.py` + `core.py`：真实模型与门面
- 09 · `batteries/fastapi_server` + `Dockerfile`：变成在线 API 并容器化部署
- 10 · `integrations/mcp/client.py`：MCP Client，把外部工具接进注册表
- 11 · `memory/sqlite_store.py`：SQLite 持久化记忆（scope、迁移钩子、WAL）
- 12 · `integrations/openapi/`：OpenAPI 发现（REST API 零适配变工具，$ref/鉴权/只读闸）
- 13 · `memory/retention.py` + 两个 Store：历史保留策略（条数裁剪/TTL，轮边界对齐，opt-in）
- 14 · `batteries/fastapi_server/ratelimit.py`：限流 Battery（滑动窗口、429 + Retry-After、GET 放行）
- 15 · `spi/discovery.py` + `discovery/catalog.py`：能力缺口的最小闭环（缺口 → 发现 → 注册 → 本轮可用）
- 16 · `tools/composer.py`：组合工具（不越界地造工具：占位符解析、逐步授权、能力并集）
- 17 · `spi/sandbox.py`：第六个插槽 ToolSandbox（代码工具沙箱，只定义契约，执行由宿主实现）
- 18 · `tools/code_tools.py` + `meta.py` + 执行器接线：代码工具注册表、48h TTL、调用刷新、双重授权（执行仍走宿主沙箱）
- 19 · `learning/` + `spi/learning.py`：自校准路由（上下文老虎机，从执行反馈学习，opt-in；附离线 benchmark 五线）
- 20 · `discovery/scoring.py` + `spi/embedding.py` + `discovery/semantic.py`：双通道语义发现（词法打分 + 可选 embedding，本地 bge-m3 / 云端双后端，margin 防误召回）
- 21 · `types.py`（ToolSpec 序列化）+ `tools/manifest.py`：能力清单交换（语言中立 manifest，composite / code 跨实例移植，治理声明随清单走但 `gate` 永不随清单走，不造调用通道）
- 22 · `shell/desktop/`：原生桌面端（PySide6 + QML 灵动岛，内核与界面同进程，事件驱动动效与线程纪律）
- 23 · `shell/desktop/workbench.py` + `qml/`：多专员工作台与三态交互（球→胶囊→面板、共享循环、隔离与未读、真挂起的演示脚本）
- 24 · `learning_vault.py` + `host.py`：真机自适应全开（路由学习 × 沙箱造工具，闭包跨任务、容错读写、仓库专员样板间）
- 25 · `kernel/compaction.py` + `context.py`：上下文摘要压缩（旧轮压摘要、recent 完整保留、失败回退硬裁剪）
- 26 · `discovery/remote_sources.py` + `aggregate.py`：远程动态发现源（MCP / OpenAPI 懒连接）与多源聚合去重
- 27 · KV TTL、响应体守卫、API Key 鉴权 + `docker_sandbox.py`：运维三件套与 Docker 生产沙箱
- 28 · `memory/vault.py` + `core.py`：结构化长期记忆（remember/recall、去重合并、重要度加权，opt-in）
- 29 · `evaluation/` + `scripts/eval_*.py`：端到端评测（golden 回归基线：完成率 / 工具正确性 / 越权 / 无支撑断言，非门禁 CI）
- 30 · `discovery/scoring.py` + `remote_sources.py`：远程源按 need 筛选（双向词法 + 可选语义，保守收窄、空命中回退全量）
- 31 · `tools/meta.py` + `loop.py`：动态重规划 replan（计划走不通显式回到规划，仅 PLAN 注册、不走授权）
- 32 · `kernel/tiering.py` + `core.py`：成本·延迟感知选档（standard/strong 自动换挡，DONE 成本汇总）
- 33 · `integrations/tracing/otel.py`：结构化 tracing（外侧订阅事件流 → OpenTelemetry trace，root + 工具子 span，可选 `[otel]` extra，不改内核）
- 34 · `core.py` + `kernel/loop.py` + `diagnostics.py`：生产加固三件套（实例级任务串行锁、模型硬超时与取消传播、零依赖本地诊断 SPI，含沙箱取消即 kill）
- 35 · `kernel/budget.py` + `spi/cipher.py` + `config.py` + `memory/sqlite_store.py`：生产加固 P1（任务级预算硬上限与熔断、vault 落盘静态加密 SPI + Fernet 电池、CoreConfig 集中校验与 from_config、SQLite 在线热备份/恢复）
- 36 · `shell/employee/worker.py` + `delegate.py`：子员工预算兜底与跨层级共享并发闸（default_budget、budget_exceeded 回流、DelegationSupervisor 全局信号量、per-task 预算覆盖）
- 37 · `memory/vault.py` + `kernel/loop.py` + `config.py` + `core.py`：长期记忆语义召回与任务起始主动召回（异步 arecall 词法 OR 语义、embedder 注入与向量懒缓存、失败降级纯词法、loop 任务开始自动注入 system 记忆并发 memories_recalled 事件）
- 38 · `metrics.py` + `batteries/fastapi_server/readiness.py` + `app.py` + `integrations/metrics/`：运行指标与就绪探针（零依赖计数器 + run 时延直方图、astream 唯一出口旁路 tap、completed/failed/cancelled 分类、`/readyz` 与 `/metrics`、OTel observable meter 采集时 pull，可选 `[otel]` extra）
- 39 · `examples/host_j_actual/` + `examples/five_beats.py`：第三方播种端到端（记账应用 Actual Budget；五拍标准线 唤醒→长尾→缺口→补能力/派子员工→授权→完成；发现补分类与委派只读两条离线链路；整数分与官方 CLI argv 锁定；网页壳 8240）
- 40 · `memory/sqlite_store.py` + `memory/vault.py`：长期运行的记忆规模治理（SQLite 裁剪先 COUNT/MIN 索引聚合短路、未达阈值不取回全表；vault 词法召回改懒构建倒排索引、与全扫逐字节等价；可选 `max_notes` 容量上限默认不删；语义通道不做 ANN 的取舍）
- 41 · `tools/executor.py` + `tools/registry.py`：上下文轻量治理（三类工具结果回灌前统一按 16000 字符"头+尾"有界收口并打 `truncated` 标记；喂模型的工具目录 / schema 按名确定性排序以稳定提示缓存前缀；内部存储顺序不变）
- 42 · `kernel/compaction.py` + `kernel/loop.py` + `core.py`：摘要压缩省钱与水位便捷构造（C2 `compaction_model` 让摘要改走便宜小模型、`None` 完全回归主模型、不进 CoreConfig 也不计预算；C3 `CompactionPolicy.from_context_window(chars)` 按 0.6/0.4 比例算高/低水位，`from_context_window(40000)` 恰等于默认策略，字符口径不引 tokenizer）
- 43 · `policy/redact.py` + `policy/authorize.py` + `policy/approval.py` + `host_e tool_auth.py`：治理三件套（令牌鉴权、审批-执行-审计参数快照、声明式脱敏）＋ 原生参数级审批
- 44 · `integrations/acc/` + `openapi/discovery.py` + `policy/authorize.py`：ACC v1 互操作层（零依赖声明校验器 + 声明→原生审批的翻译 + 官方 26 条参考向量 + OpenAPI 绑定按声明筛暴露面 + 能力闸与宿主注入的可信主体）
- 45 · `llm/anthropic.py`：Claude 模型接口（system 走顶层、max_tokens 必填、tool_use/tool_result 块翻译、相邻同角色合并；同一 ModelProvider 契约，内核零改动）
- 46 · `spi/resources.py` + `batteries/filesystem.py`：第 10 个 SPI 资源契约（路径策略纯函数两道闸、原子写与硬链接独占创建、`expected_sha256` 乐观并发、四个 async 工具）
- 47 · `paths.py` + `policy/approval.py` + `batteries/filesystem.py` + `tools/executor.py`：安全加固第二批（路径语义收敛成单一真源、空前缀失败关闭、结果侧也脱敏、manifest 治理语义与版本号绑定）
- 48 · `types.py` + `llm/anthropic.py` + `memory/sqlite_store.py`：多模态消息契约（`ChatMessage.parts` 可选扩展、图片内联/引用两条路径、落盘只存引用不存字节、摘要与降级渲染）
- 49 · policy/approval.py + kernel/context.py + tools/composer.py + spi/resources.py：第一层收口（入参 schema 子集补全、图片按真实成本计入预算、后端能力探测与自动降级、组合工具版本仓库、跨层对抗性证据、O_NOFOLLOW 关竞态窗口）
- 50 · tools/health.py + tools/schema.py + policy/approval.py：能力接入体检与 schema 兼容性（静态检查未实现键/审批路径/频控/组合步骤；`anyOf` / `oneOf` / `$ref` 宁可跳过也不误拒；OpenAPI nullable 归一）
- 51 · types.py + policy/authorize.py + tools/executor.py：可信变更预览（宿主生成 `ChangePreview`、审批上下文展示、确认后 token 复核，失效则拒绝执行；组合工具同规则）

## 5. 自检（读完本篇应能回答）

1. 宿主写的函数，是在哪一步变成工具的？
2. 策略是在哪决定的？四种策略分别什么意思？
3. 工具结果通过什么回到模型那里？
4. 为什么说 Channel 是"显示屏"而 Core 本身不打印东西？
