# 路线图（Roadmap）

> 最后更新：2026-10-24（对应已发布版本 **v0.20.0**）。本路线图只记录**方向与边界**，不承诺具体日期；每个版本的实际范围以 [`CHANGELOG.md`](CHANGELOG.md) 与 GitHub Release 为准，逐行实现细节见 `docs/walkthrough/`。

## 愿景

让"给现有软件加 Agent 能力"像"给应用嵌入数据库"一样简单：宿主软件只声明自己的业务能力（普通函数 + 一行描述），YAI 内核在进程内完成能力发现、意图理解、策略选择、工具调用、记忆与可审计交付，**宿主不写一行 Agent Loop / Planner / 工具选择代码**。

## 设计原则（所有版本不变）

1. **Library-first**：YAI 是被 import 的内核，不是需要单独部署的平台；内核本体 `dependencies = []`，第三方能力全部走可选 extra 懒加载。
2. **一切外部能力走 SPI**：模型（Model）、通道（Channel）、记忆（Memory）、权限（Policy）、发现（Discovery）、沙箱（Sandbox）、路由学习（RouteSelector）、文本嵌入（Embedding）、存储加密（StorageCipher）、文件资源（ResourceProvider）**十个契约**宿主可替换。
3. **事件流唯一出口**：每个自适应决策都必须发出 `AgentEvent`，禁止静默决策。
4. **可离线测试**：全部测试不依赖网络与 API Key（假模型 / MockTransport / 注入时钟）。
5. **安全语义只来自声明，不来自猜测**：没声明的参数不脱敏；外部能力契约只能把闸门往上抬，不能替宿主放行；跨进程清单只交换数据、不交换判定函数。

## 已完成（按真实 tag）

### v0.1 — 内核最小闭环

- 函数内省自动生成工具（type hints + docstring → JSON Schema）；ToolRegistry / ToolExecutor（校验、权限、sync/async）
- Adaptive Router 四策略（direct / react / plan / clarify，规则路由，零成本可测）；Agent Loop（ReAct、先规划后执行、澄清交互、迭代上限）
- SPI 四契约 + 默认实现；FastAPI Battery（/health、/v1/tools、POST 事件流）、Dockerfile / render.yaml；三个宿主示例

### v0.2 — 工具来源扩展与 LLM 路由

- MCP Client（stdio / Streamable HTTP / 内存实例）、OpenAPI 3 发现（$ref 内联、只读模式、鉴权）
- LLM 路由器（规则兜底、决策来源可审计）、SQLite 持久化记忆（opt-in）、OpenAI 兼容模型后端

### v0.3 — 长期运行可信度

- 历史保留策略（条数 + TTL、轮边界对齐裁剪）；在线限流 Battery（每 IP 滑动窗口、429 + Retry-After、GET 放行）
- 宿主 E：零 AI 依赖可独立运行的销售 CRM，同一业务对象零改造嵌入；澄清循环修复

### v0.6 — 路由演进

- `llm_router="auto"`：按后端能力标记自动开关 LLM 路由
- **能力缺口感知**：工具不足时发 `capability_missing`，从"只会用给定工具"到"知道自己缺什么"
- **发现最小闭环**：`ToolDiscovery` SPI + 进程内 `StaticCatalog`，缺口 → 候选 → 注册 → 本轮可用；"能被发现不等于被授权执行"
- 规则路由量词回归

### v0.7 — 自适应能力扩展（tag 2026-09-22）

> 本节按主题归组；每条到具体 tag 的归属以 `CHANGELOG.md` 与文件首次入库日期为准。

- **自校准路由（contextual bandit）**：四策略各是一臂，按 9 维任务特征分桶维护 Beta 后验、Thompson Sampling 选档；冷启动注入规则先验，某桶零真实反馈时确定性取先验最强臂；执行后从事件流抽成败回灌。纯标准库、状态可 JSON 持久化、默认关闭。新增第七个 SPI `RouteSelector`。
  - **离线可复现证据**：`scripts/benchmark_router.py` 确定性仿真，8 seeds × 3 epochs：自校准末段 **89.6%** vs 规则基线 73.3%（约 +16pp），Oracle 100%；消融"无规则先验"88.5%、"无上下文"70.8%（低于基线，证明吃的是上下文）。脚本内置"必须显著优于规则"硬校验。见讲义第 19 篇。
- 权限三档产品化；两层工具目录（目录摘要 + 按需注入 schema）；执行中动态发现 `request_capability`；每轮反思
- 双通道语义发现（词法 + 可选嵌入，第八个 SPI `EmbeddingProvider`）
- 组合工具 `compose_tool`（只编排已注册工具，能力上限＝并集，不执行模型生成的代码）
- 代码工具注册表与生命周期（48h TTL、调用刷新、永久保留、创建与首次执行双重授权）；**内核不内置执行器**
- 运维向：KV TTL 与 SQLite schema v1→v2 迁移、响应体三级守卫、API Key 鉴权

### v0.8 — 产品形态与工程门禁（tag 2026-10-10 / 0.8.1 10-11）

- **上下文摘要压缩（opt-in）**：超水位时把较早的完整轮交给模型压成一条 system 摘要，最近若干完整轮原样保留；模型异常自动回退硬裁剪，摘要是增强而非依赖
- **原生桌面端 `shell/desktop/`（PySide6 + QML）**：屏幕顶部灵动岛胶囊、三态交互形态机（圆球 / 胶囊 / 面板）、对话优先面板（思考链与调用链折叠）、多专员工作台、代码工具管理入口（查看 / 永久保留 / 删除）
- **语言中立能力清单（manifest）**：`to_manifest_dict` / `from_manifest_dict` + 文件 IO，composite 工作流与 code 工具定义可跨实例移植
- 生产加固三件套（并发契约 / 模型硬超时 / 零依赖诊断）与 P1（预算熔断 / 落盘加密 / `CoreConfig` / SQLite 热备份）；子员工预算兜底与跨层级共享并发闸
- 上下文轻量化、长期运行记忆规模治理（词法倒排索引）、多租户与水平扩展部署指南并首次英文化
- **英文门面**：`README.en` / `CONTRIBUTING.en` / `docs/architecture.en` 等中英成对维护
- **供应链与质量门禁**：发布产物 SLSA 构建证明 + cosign 签名、CodeQL、OpenSSF Scorecard、atheris 模糊测试、CI 路由基准 job、pre-commit（ruff / mypy / gitleaks）、mypy 进入 CI 门禁
- **第三方软件端到端接入示例（食谱管理类，2026-09-23 入库）**：`examples/host_i_mealie/` 不修改其源码接入，验证"已有开源软件 + 内核"这条路径可走通
- PyPI 发布就绪（classifiers、project URLs、PEP 561 `py.typed`）

### v0.9 — 真实宿主端到端与可观测（tag 2026-10-12）

- **记账类宿主端到端 + 五拍标准线（2026-10-12）**：`examples/host_j_actual/` 不修改其源码接入真实开源记账软件；`examples/five_beats.py` 把一次成功的自适应接管压成可断言的节拍（唤醒 → 长尾任务 → 缺口 → 补能力 → 授权 → 完成），断言事件下标顺序与因果，供各宿主测试复用
- **结构化长期记忆 `MemoryVault`（opt-in）**：模型可主动存档事实 / 偏好 / 画像，高相似条目去重合并，落盘 JSON 可经第九个 SPI `StorageCipher` 透明加密；并叠加语义双通道召回与**任务起始主动召回**（发 `memories_recalled`，默认关闭）
- **端到端离线评测**：零硬依赖内核包 `evaluation/` + `scripts/eval_agent.py`，用 ScriptedModel 真跑完整内核，量化完成率 / 工具精确率召回率 / 越权 / 无支撑断言，作为非门禁 CI job
- **运行指标与就绪探针**：零依赖 `MetricsRecorder`（runs / 策略 / 工具 / 档位 / 事件 / 时延直方图 + Prometheus exposition）、`GET /readyz`（探针并发、逐项隔离、失败只摘流量）、`GET /metrics`（配 Key 时默认鉴权）；可选 `[otel]` meter 桥接
- **动态重规划 `replan`** 与 **成本·延迟感知选档 `tiering`**（默认 standard，规划 / 纠偏 / 第 3 轮起升 strong，档位变化发事件、DONE 汇总模型调用数）
- **结构化 tracing**：外侧订阅事件流的 OpenTelemetry 桥接（一次 run 一个 root span，工具调用是子 span，库内不初始化 Provider）
- 远程发现源按 need 双通道收窄（词法始终可用、语义可选、不中则回退全量）

### v0.10 — 让"嵌进真实软件"这件事安全（tag 2026-10-14）

- **治理声明随能力清单跨进程**：脱敏路径 / 宽口径审计提示 / 外部契约快照随 manifest 走；`gate` 是函数，**不导出也绝不凭快照重建**
- **运行时保障补两处空白**：工具 handler 硬超时 `tool_timeout`（异步真取消、同步如实说明"中断不了、可能仍在后台产生副作用"）、`await AgentCore.aclose()` 统一收尾（谁创建谁负责关、幂等）
- **第二个真实模型后端**：Anthropic 消息接口（`[anthropic]` extra，API Key 走 `ANTHROPIC_API_KEY`），契约与内核零改动
- **第 10 个 SPI：资源契约** + 安全文件后端（两道路径闸、原子写、独占创建、乐观并发、超限报错不截断）
- **外部能力契约（ACC）兼容层**：声明校验 + 决策原语 + OpenAPI 绑定 + 能力闸与宿主注入的可信主体；上游 26 条一致性向量逐字节入库校验哈希；对外只声称 `ACC v1 OpenAPI Binding Parser Profile`
- **治理三件套**：审批-执行-审计参数快照同源、声明式参数脱敏、host_e 跨进程工具口本地令牌鉴权
- **限流第二道闸**：`global_per_minute` 跨 IP 总配额（默认关闭，进程内计数）
- **安全纠错**：沙箱 AST 静态闸门深嵌套改为失败关闭并加模糊测试 target；CI runner 钉版本与分支保护解耦
- 对外文档改为按类别对比，不再点名同类产品

### v0.11 — 把"外部契约的治理"翻译回原生（tag 2026-10-16）

- **原生参数级审批**：判定原语（严格 JSON 值、判不出来失败关闭、四步顺序）从 ACC 层搬进内核 `policy/approval.py`；宿主写数据就能要"超过这条线要人确认"，`approval` 随能力清单走且**导入即生效**（只会加严，永不替宿主放行）
- **ACC 降级为互操作层（shim）**：`integrations/acc` 只保留声明校验、三个 ACC 特有派生量与 `to_approval_policy` 翻译器；判定只剩一份，两条路径的结论由 parity 测试钉死；对外仍只声称 `ACC v1 OpenAPI Binding Parser Profile`
- **准入判定不再算出来就扔**：`risk` / `scope` / `target` 随 `PERMISSION_ASKED` 与 `TOOL_CALL` 发出（空值不写键，未治理工具的事件形状逐字不变）
- **资源作用域闸 `filesystem_gate`**：用与 handler **同一个** `normalize_relative` 规范化，"批准目标 == 执行目标"构造成立；`write=True` 配空白名单 = 拒绝一切写
- **两处真实纠错**：PEP 563 下 `build_spec` 把所有参数退化成 `string`（进而让审批把每次合法调用判成非法）改为先解析字符串注解；清单导出丢审批阈值改为 `__post_init__` 补齐
- **语料分发口径**：上游 Apache-2.0 的 26 条一致性向量不再随 sdist 分发（源码仓库与 CI 仍全量跑），缺文件时相关测试整模块 `skip`

### v0.12 — 补上"静默失效"的那几处（tag 2026-10-22，当前发布版本）

- **安全加固第二批**：八处"看起来有护栏、实际没生效"的缺陷逐条钉死，并各配回归测试——
  参数级审批的数组路径曾**永远不命中**（schema 侧与取值侧两套路径语义，条件静默失效；
  修法是把路径语法收敛到 `src/yai_core/paths.py` 单一真源）；作用域闸的空字符串前缀曾等于
  "整个根目录可写"；工具返回值未脱敏（密钥从结果侧绕回事件流）；文件列举三处（断链链接、
  外部文件大小泄漏、截断标志误报）；闸的风险等级被静默覆盖；脱敏失败退回原对象；
  能力闸未复核 `enabled=false`；**清单的治理语义与版本号脱钩**（老读者会静默丢掉脱敏与审批
  要求 → `manifest_version` 升到 2，老读者失败关闭）。
- **ACC 的执行提示真正被执行器强制**：`execution.timeout_ms` / `execution.rate_limit` 此前
  只解析不执行；现在翻译成内核原生数据（`ToolGovernance.timeout_ms` / `rate_limit`），由
  执行器强制（超时取更严一档；频率超限不进 handler、不发 `TOOL_CALL`、在授权之前判定），
  并附**对抗性证据**：模型参数改不动任何治理结论。
- **多模态消息契约（可选扩展）**：`ChatMessage.parts`（`TextPart` / `ImagePart` /
  `ResourcePart`），默认 None = 纯文本消息（输出逐字不变）；图片内联才"模型看得见"，
  引用一律渲染成"明说看不到"的占位（绝不静默丢图）；内联载荷 5 MB 硬上限；落盘只存引用
  不存图片字节（schema v2→v3，迁移逐级）；`scripts/check_multimodal_wire.py` 可离线核对
  出站形态、真机可 `--live`。
- **三处治理语义收紧**：外来清单的审批条件路径在**导入期**就与 `input_schema` 交叉校验；
  "要求可信主体"改由**调用期**能力闸强制（不再靠注册期静态布尔隐藏能力）；ACC 的 `guidance`
  **默认不再进模型描述**（来源可能是第三方 spec），原文仍留在 `governance.acc` 供宿主取用。
- **依赖**：certifi 升到 2026.10.19，修掉 emSign 中间 CA 私钥泄露（Critical / CVSS 9.1）。

### v0.13 — 把"还差点"的补到能上线（tag 2026-10-22）

- **入参校验的 schema 子集从 4 个关键字扩到 13 个**：此前只看结构、不看取值域
  （声明写 `minimum: 100`，传 `1` 照样通过）。新增 `enum` / `pattern` / `minimum` /
  `maximum` / 长度 / 元素数 / `format`（只实现 date-time 与 date）；`enum` 走严格 JSON
  相等，未实现的键**记录在册**。
- **多模态三处落地**：图片按**真实成本口径**计入上下文预算（此前按占位文本长度计，
  等于"图片不占预算"）；新增 `yai_multimodal` 能力标记，**没声明的后端按不支持多模态
  处理**并自动降级成纯文本占位；新增示例宿主 `examples/host_k_multimodal/`——
  **模型只能选句柄，宿主负责解析、判作用域、判有效期、判大小**。
- **组合工具的版本管理**：`CompositeWorkflowStore` 存步骤快照、列版本、**按位置**比对
  （工作流顺序敏感）、还原成可注册的 `ToolSpec`；版本号随能力清单跨进程走。
- **跨层对抗性证据**：spec 的 guidance 默认进不了模型提示、即使打开也改不动闸门判定；
  模型参数里的 subject 不算可信主体；参数里的 approval/governance 不被采纳；
  清单里的 gate 不会被重建。
- **符号链接竞态窗口在 POSIX 上关进内核层**：读路径改用 `O_NOFOLLOW` 打开
  （**Windows 无此标志，窗口只能收窄**，对应测试在 Windows 上是 skip，不是"通过"）。
- **清除杭州相关材料与引用**（赛事材料 5 个文件、示例演示数据、文档引用；GitHub 上
  同名 Release 已删，**tag 留在 git 历史里**）。

### v0.14 — 把执行边界与审批可信度补到可运营（tag 2026-10-23）

- **执行约束覆盖全部路径**：组合工具按真实步骤计入预算，频控在组合分支前判定，
  工作流级超时覆盖整条组合；代码工具声明超时收紧沙箱；同步 handler 返回 awaitable
  仍受剩余超时约束。
- **入参校验成为所有调用的共同下限**：schema 子集对每次调用生效；泛型容器注解递归
  生成 schema；`anyOf` / `oneOf` / `$ref` 遇到时跳过以免误拒；OpenAPI `nullable`
  归一为标准 type union。
- **direct 严格模式（opt-in）**：`strict_direct=True` 时 direct 下拒绝模型自带的结构化
  tool_calls；模型把 XML tool-call 写进正文时失败关闭，不再伪装成最终答案。
- **能力接入体检**：`check_tool_specs()` / `AgentCore.capability_health()` 静态检查
  未实现 schema 键、非法频控、审批路径、组合步骤、代码工具沙箱等。
- **可信变更预览**：`ChangePreview` + `ToolGovernance.preview`；审批前展示宿主渲染的
  diff，确认后执行前复核 token，失效即拒绝；host_e 与桌面销售通过只读 HTTP 端点共用
  同一预览逻辑；stale 后立即终止，不再让模型补救。
- **执行收据与进程内幂等**：`ExecutionReceipt` 区分 not_started / succeeded / failed /
  unknown / partial；`call_id` 透传；宿主 `idempotency_key` 存在时同 key 重试不再进
  handler（进程内缓存，跨进程仍归宿主）。

### v0.15 — 宿主现状锚与执行治理加固（tag 2026-10-24）

- **宿主实时状态（现状锚）**：`register_state_provider()` 注册只读快照，每次主模型
  调用前作为瞬态 system 块注入；不进历史、压缩与长期记忆；有单条/总量上限，provider
  失败只降级并记诊断；`HOST_STATE` 事件只报大小与指纹，不复制状态内容。
- **执行治理加固**：幂等重放仍受当前策略 DENY 约束，缓存绑定参数指纹；host_e 预览
  token 收窄到工具相关状态，无关变更不再误判失效。
- **证据链补齐**：路由 benchmark 增加留出集/冻结学习评测并接入 CI；Windows 增加
  junction 父目录替换测试；能力体检、执行收据、预览与状态注入的离线用例补齐。

### v0.16 — 语言无关宿主接入（tag 2026-10-24）

- **Sidecar 会话控制协议**：`python -m yai_core.sidecar` 用 stdio NDJSON +
  JSON-RPC 2.0 承载初始化、运行、取消、审批/澄清与事件流；工具面仍走
  MCP/OpenAPI，不新增 `tool/call` 通道；单进程单任务，桥接失败回滚，关闭时
  取消任务并释放 core。
- **证据**：16 个离线用例覆盖审批/澄清/取消/错误码/stdio/MCP/OpenAPI，另有
  第 54 篇讲义与多语言接入文档同步。

### v0.17 — 跨语言证据与宿主状态接线（tag 2026-10-24）

- **Node 宿主样例**：零依赖 Node 进程真实拉起 sidecar，工具走 Node 自己的
  OpenAPI 服务、模型走本地 mock；演示审批反向请求与 `done` 事件。
- **CI 强化**：新增 `sidecar (mcp + llm)` job，安装真实可选依赖并执行 Node
  样例，不再跳过 MCP/stdio 路径。
- **HostStateProvider 真实接线**：陪伴示例注册 `companion` 状态源，当前档案
  每轮瞬态注入，不需要先调工具、也不写入历史。

### v0.18 — 桌面端状态锚（tag 2026-10-24）

- **陪伴专员状态源**：`_live_factory` 支持 `state_provider`；陪伴专员每次任务
  注册最近心情/总数，模型调用前瞬态注入，不写入历史/记忆。
- **证据**：真实 live factory 的离线用例验证 `HOST_STATE` 注入；CI 的
  PySide6 缺失环境跳过该用例，本地完整环境照常运行。

### v0.19 — 销售选中态（tag 2026-10-24）

- **网页选中客户**：客户表新增“选中”按钮与 `/api/selection`；选中态随快照/状态
  返回，客户删除后自动清空。
- **Agent 现状锚**：`Workbench._agent_state()` 只注入已选客户；每次任务注册
  `sales_selection`，不写历史/记忆。
- **桌面跨进程**：销售专员经带令牌的 `/api/agent/state` 读取同一状态
  （`_selection` 私有，不注册成工具）。

### v0.20 — 桌面灵动岛视觉重设计（tag 2026-10-24，当前发布版本）

- **胶囊/面板结构**：`01/SALES + 状态 + OPEN`；`YAI.CORE + VAULT/MEM +
  编号标签 + HITL.GATE + TRACE + RUN`；VAULT/MEM 改全屏面板。
- **四态星云**：球/胶囊/面板顶栏共用 idle/working/complete/frozen；空闲 20fps、
  工作 60fps、冻结停画，待命实测约单核 6.2%。
- **截图与文档**：`docs/assets` 更新并新增胶囊/HITL/VAULT/MEM；README、第 22/23
  篇讲义与 CHANGELOG 同步。

## 下一步

### v0.21 — 候选（未排序，等维护者拍板）

- **跨实例分布式限流**：`global_per_minute` 与工具级 `rate_limit` 都只是本进程总数；多实例部署要跨实例总闸，需先解决状态共享。
- **生产级沙箱适配器递进**：Docker 已落地（宿主侧），后续按隔离强度是 gVisor / 微 VM / 远程执行，内核零改动。
- **组合工具的可视化编排**（宿主侧；版本管理已在 v0.13 落地内核侧）。
- **公网实例的暴露面收口**：线上实例目前无鉴权 + 每 IP 限流（身份取 `X-Forwarded-For` 最左项，可伪造）+ 全局总闸默认关 ⇒ 事实上是开放的免费额度中转；收口方案（开 `YAI_API_KEY` / 只读演示模式 / 打开全局总闸）待定。
- **多模态的落地跟进**：**真机**验证（当前只有离线形态校验与一个 `--live` 脚本），以及资源引用的宿主侧解析器推广到更多宿主；真实 token 成本口径与后端能力探测已在 v0.13 落地。

### v1.0 — 回流 AMBRACE

- 作为开源 AI 陪伴项目 [AMBRACE](https://github.com/Gi-Tuu/AMBRACE) 的 Agent 内核内嵌发布；稳定的 SPI 兼容性承诺与语义化版本。

## 明确不做（防止内核膨胀）

- **不做 MCP Server**：YAI 是工具的使用方，不对外提供 MCP。
- **不做 Multi-Agent**：多智能体编排属于宿主层应用，不进内核。
- **不做向量记忆**：记忆后端是 SPI，向量方案由宿主/插件提供。
- **不做内置 UI**：界面（网页/桌面/Flutter）是宿主的事，内核只保证事件流足够驱动 UI。
- **不做 coding agent**：不以内核形态实现自主改代码的 Agent。

## 维护节奏

- 主干保护：main 始终保持全量测试 + ruff + mypy 绿色；CI 跑 Ubuntu 3.11/3.12/3.13 与 Windows 3.13 四矩阵（另有 CodeQL、Scorecard、模糊测试、路由基准与离线评测）。
- 小步提交：Conventional Commits，一个 PR 一件事；行为变化必须同步测试与 `docs/walkthrough/` 逐行讲义。
- 双语门面同步：README / CONTRIBUTING / SECURITY / architecture / comparison 各有中英两份，事实性内容改动必须同时改。
