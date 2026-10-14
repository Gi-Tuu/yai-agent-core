# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 与语义化版本。
**内核本体始终保持零第三方硬依赖**（`pyproject.toml` 的 `dependencies = []`），真实模型、Web 服务、MCP、OpenAPI 等能力全部走可选 extras 与 SPI 契约懒加载。

每个版本的说明就以本文件对应小节为准（[`docs/releases/`](docs/releases/) 只留早期长文草稿，不再逐版维护）。版本号与 GitHub Release / tag 由维护者拍板后发布；Release 正文由 `publish.yml` 从 PR 标题自动生成，人话版摘要写在下面的小节里。

## [v0.10.0] - 2026-10-14

本版主线是**"让内核能被安全地嵌进真实软件"**：能力声明能跨进程走了，超时与收尾有了统一入口，多了第二个真实模型后端，文件能力有了第 10 个契约，公网端点多了两道闸。全部新增默认不改变既有行为。

- **能力声明跨进程**：`ToolGovernance` 的数据部分随 manifest 清单导出/导入，脱敏与审计收口在另一台机器上照样生效；`gate` 作为函数永不导出也永不凭快照重建——外来清单既加不了闸门，也没有摘闸门的通道。
- **运行时保障补齐两处空白**：工具 handler 硬超时（`tool_timeout`，异步真取消、同步如实告知"可能仍在后台跑完"）与 `AgentCore.aclose()` 统一收尾（谁创建谁负责关，幂等）。
- **第二个真实模型后端**：Anthropic 消息接口（`[anthropic]` extra），`ModelProvider` 契约与内核零改动。
- **第 10 个 SPI**：资源契约 + 安全文件后端（两道路径闸、原子写、乐观并发、超限报错不截断）。
- **外部能力契约（ACC）兼容层**：只声称 OpenAPI Binding Parser Profile，26 条官方向量逐字节入库校验；能力闸 + 宿主注入的可信主体让声明真正参与每一次调用，且**只能把闸门往上抬**。
- **安全两条**：沙箱 AST 静态闸门深嵌套改为失败关闭（并加模糊测试 target）；host_e 跨进程工具口加本地令牌鉴权。
- **限流加第二道闸**：`global_per_minute` 跨 IP 总配额（默认关闭），补上"每 IP 都没超但总量刷爆"的盲区。

## [Unreleased]

### Added

- **限流全局兜底配额（`global_per_minute`，默认关闭）**：补上每 IP 维度的盲区——一万个赞不同的 IP、每个只发三次，`per_minute=30` 一次都不会触发，但 `/v1/agent/run` 每次都是真金白银的模型调用。现在 `YAI_RATE_LIMIT_GLOBAL_PER_MINUTE=120` 就给整实例加一道跨 IP 总闸；默认 `0` = 关闭，不配就等于行为与旧版逐字节一致。
  - **不写第二套算法**：全局闸复用同一个 `SlidingWindowLimiter`（只是 `per_minute` 覆盖 + key 固定 `__global__`），两套窗口语义（恢复时机、`Retry-After` 口径）必须完全一致。判定顺序是**先全局后每 IP**——全局拒绝时不消耗该 IP 配额；代价是"全局放行但 IP 超限"会白扣一个全局名额，宁可多拦不可漏放。429 响应体新增 `scope: "global" | "ip"`，运维一眼看出哪道闸命中。
  - **红线不动**：全局闸仍然只挂在 `POST /v1/agent/run` 白名单内，三个 GET 验证端点（`/health`、`/.well-known/*`、`/v1/tools`）任何超限状态下都无条件 200，有测试钉住。
  - **说清"全局"到底有多大**：计数在**本进程内**，多实例部署时每个实例各算各的——它不是分布式限流，跨实例总闸需要先解决状态共享（Redis / 一致性计数）。`ratelimit.py` 的 `TODO(v0.4)` 就此关掉。新增 6 例测试（跨 IP 触发、判定顺序、不触达模型、GET 豁免、默认关闭、env 三态），讲义第 14 篇新增 G 节。
- **治理声明随能力清单（manifest）一起走**：`to_manifest_dict` / `from_manifest_dict` 带上 `governance` 的三样数据（`sensitive` 脱敏路径、`audit_sensitive` 宽口径提示、`acc` 声明快照），跨进程 / 跨语言搬运后脱敏与审计收口照样生效；无治理声明的工具清单**逐字节不变**（整个键省略）。
  - **`gate` 与 handler 同命：不导出，也永不重建**。清单里的 `acc` 快照只是惰性记录——外来 JSON 不能给本机工具加上闸门，也不能替本机把闸门摘掉；要恢复准入必须由宿主本地重新绑定 `replace(spec.governance, gate=build_gate(...))`（示例写进 `from_manifest_dict` 的文档与讲义第 21 篇第 5 节）。两个方向各有契约测试：`test_foreign_acc_snapshot_never_builds_a_gate` / `test_host_rebinding_restores_the_gate`。
  - **入站只校验类型、不解释声明**：`sensitive` 必须是非空字符串数组、`audit_sensitive` 必须是布尔、`acc` 必须是 object 或 null；**出现内核不认识的治理键直接拒绝加载**——静默忽略一个写错的 `sensitives` 等于"宿主声明了脱敏而内核没做"，比报错危险。要加字段就升 `MANIFEST_VERSION`。
  - **顺带堵一个真漏**：组合工具分支不经过 `authorize_tool_call`，`TOOL_COMPOSED` 事件此前带的是**明文 arguments**，声明了 `sensitive` 照样漏。现在这份副本也按组合工具自己的声明脱敏（执行仍拿真值）。边界照实说：脱敏是**逐层声明**而非自动污点追踪——密钥经占位符流进内部步骤后按那一步自己的声明处理，`test_redaction_is_per_layer_not_taint_tracking` 把两侧都钉住。
- **工具 handler 硬超时（`tool_timeout` / `ToolExecutor.handler_timeout`，默认关闭）**：模型侧早有硬超时，工具侧一直裸着——一个不返回的 OpenAPI/MCP 调用能把整轮任务吊住。现在 `AgentCore(model, tool_timeout=10)` 或 `CoreConfig(tool_timeout=...)` 就给每次工具调用上墙上时限，默认 `None` 完全保持旧行为（含"同步 handler 就在事件循环线程里直接跑"，有测试钉住）。
  - **两种 handler 语义分开承诺**：`async def` 走 `wait_for`，是**真取消**（协程收到 `CancelledError`）；同步 handler 丢进线程池后改用 `asyncio.wait` 而不是 `wait_for`——后者会 await 被取消的 future，等于一直等线程跑完（实测 0.4s 的同步工具配 0.05s 超时仍要 0.4s 才回话）。超时话术也分两种：异步说"已取消"，同步说"无法中断、可能仍在后台产生副作用，先确认状态再重试"，免得模型原样重打同一个调用。
  - **不粉饰的边界**：被放弃的同步线程仍在循环默认线程池里，事件循环收尾会 join 它——同步工具的超时是"给模型的止损"，不是进程级立即脱身，可能长阻塞的能力应实现成 `async def`。被放弃 future 的结果由回调取出丢弃，不留 "exception was never retrieved" 噪音。超时以 `TOOL_RESULT(ok=False, timed_out=True)` 入事件流；组合工具的每一步同样受约束。
- **`await AgentCore.aclose(*, close_shared=False)`**：内核第一次有了统一收尾入口。规则是"谁创建谁负责关"——默认只关本实例自建的资源（未传 channel/memory 时的默认实现、`long_memory=True` 时创建的 `MemoryVault`），**宿主传进来的 model / memory / discovery / sandbox / channel 一个都不碰**（多实例共享无状态模型是官方推荐的并发用法，替别人关资源属于误伤）；要连整张图一起收才传 `close_shared=True`，顺序固定 channel → discovery → sandbox → memory → model。幂等（可放 `finally` 重复调）、持实例串行锁（不会在当前任务跑到一半时把资源抽走）、单个资源关不掉不阻止其余收尾但最后抛汇总异常（收尾故障必须被运维看见）。
- **校验补强**：`handler_timeout` / `tool_timeout` / `model_timeout` 传字符串现在抛 `ValueError`，不再是 `TypeError: '<=' not supported between instances of 'str' and 'int'`。
- **资源契约（第 10 个 SPI）与安全文件后端**：让 Agent 能读写文件，同时把可达范围钉死在一个根目录内。内核只定义**契约 + 纯路径策略**（`spi/resources.py`，零 IO），真正碰磁盘的实现在电池层（`batteries/filesystem.py`）——内核不会因为多了一个契约就获得读盘权限。
  - **两道路径闸，缺一不可**：`normalize_relative()` 拒非法形状（`..` 上跳、绝对路径与 UNC、盘符与 NTFS 数据流的冒号、`~` 展开、Windows 保留设备名、段尾点号/空格、控制字符、超深超长、混用分隔符），**报错而不是清洗后放行**；`resolve_within_root()` 逐段拦符号链接与重解析点（Windows 无 `O_NOFOLLOW`，这一道必须自己走），再对 `realpath` 结果做归属复核（root 自身可能是链接）。测试证明：放开第一道的宽松策略仍拦得住逃逸。
  - **写盘四口径**：写永远走 tmp + `fsync` + 原子替换，任何失败不留半截、临时文件清掉、原文件从未被就地改写；新建用 `os.link` 的"目标存在即失败"语义做独占创建（不支持硬链接的文件系统回退到"检查后替换"，窗口变宽但仍报冲突，回退分支有专门测试）；读/写/列举各有上限且**超限报错而非截断**（截断内容看起来是完整的，会让模型基于半份内容判断）；进程内一把写锁 + 跨进程靠指纹检测冲突，**明说不承诺跨进程严格互斥**（文件锁在 Windows/NFS 语义不一致）。
  - **乐观并发给模型用**：`file_read` 返回 `sha256`，`file_patch` 必须带 `expected_sha256`，不匹配即冲突；"重读再改，不要硬写"写进工具 docstring（那就是模型看到的说明书）。返回值只给相对路径，绝不回绝对路径。
  - **工具层零胶水**：`filesystem_capabilities(root)` 产出 `file_list/file_read/file_create/file_patch` 四个 **async** 闭包（真读写在 `asyncio.to_thread`，不卡事件循环），可直接交给 `AgentCore.auto` / `build_spec`；另有 `read_only=True` 只读挂载与非 UTF-8 明确拒绝。授权仍归权限闸与能力闸，契约本身不做授权。
  - 新增 `tests/test_file_provider_security.py`（69 例，双平台可跑：符号链接用例在无权限环境自动跳过），讲义第 46 篇；README / `docs/architecture` / `docs/comparison` 中英的 SPI 计数由九改为十并补契约行；内核 `dependencies` 仍为空。
- **Anthropic 模型接口（`llm/anthropic.py`，可选 `[anthropic]` extra）**：与 `OpenAICompatProvider` 并列的第二个真实模型后端，宿主仍然只是换一个 provider 对象，`ModelProvider` 契约与内核零改动；差异全在适配器里翻译。
  - **四处线格式差异逐条落地**：`system` 从 messages 里抽出来走顶层参数（多条合并）；`max_tokens` 必填（显式参数 → `ANTHROPIC_MAX_TOKENS` → 默认 4096）；assistant 的 `tool_calls` 翻译成 `tool_use` 内容块；`role:"tool"` 的结果翻译成 **user 消息里的 `tool_result` 块**，并把相邻同角色合并（一次调用多个工具结果 = 一条 user 消息多个块），且不生成空 text 块。工具描述形状 `{"type":"function","function":{…}}` → `{"name","description","input_schema"}`。
  - **响应解析**：`text` 块拼接、`tool_use` 块转 `ToolCallRequest`；思考块等非本层块类型忽略但保留 `raw`。`usage` 映射 `input_tokens/output_tokens`，后端不给 usage 时保持 `None`（内核不臆造 token 数，预算退回按次数兜底）。模型吐出的坏 JSON 参数按 `{}` 处理，不原样转发也不抛异常。
  - **口径一致**：声明 `yai_live_router = True` 供 `llm_router="auto"` 识别；standard/strong 两档沿用内核机制（`ANTHROPIC_MODEL` / `ANTHROPIC_STRONG_MODEL`）；Key 只从参数或环境变量读；SDK 懒加载，未安装时抛带 `uv sync --extra anthropic` 指引的错误；重试交给 SDK 或外层 `FallbackModelProvider`。
  - **边界写清**：不做流式、不做多模态输入（等消息层 `parts`）、不做厂商专有扩展（提示缓存 / thinking 参数）。新增 `tests/test_anthropic_provider.py`（17 例，假 SDK 注入，断言发出去的请求形状 + 一条端到端工具回路），逐行讲义第 45 篇；`.env.example` 与 `README` / `docs/architecture` / `CONTRIBUTING` 中英同步；内核 `dependencies` 仍为空。
- **ACC v1 兼容层（`integrations/acc/`，可选、零硬依赖）**：新增能力声明契约的解析与决策原语，宿主可以读懂外部生态给一个操作贴的风险/审批/审计声明，而不必自己重贴一遍。
  - **`declaration.py` 零依赖校验器**：手写镜像 ACC v1 schema 的约束（必填 `version`/`enabled`/`scope`、枚举、正则、区间、长度），不引入 `jsonschema`；诊断严格分两类——整数主版本不认识给 `unsupported_version`，其余不合法（含缺 `version`）给 `invalid_declaration`；**未知字段一律忽略**并只进 `extensions` 扩展袋，不参与任何 allowlist/风险/审批/限流/审计判定。
  - **`evaluate.py` 决策原语**：`is_exposed`（enabled + 非空 scope + 路由允许 + 需要主体时有可信主体 + 绑定给了入参 schema）、`effective_risk`（显式风险 > 声明只读 > 绑定原生 GET 信号 > 兜底 `medium`，永不默认成 low）、`audit_sensitive`、`approval_outcome`（先按绑定 input schema 拒非法调用值，再校验条件路径已声明，`approval.required` 恒占优，`approval.when` 走 ANY）。九个算子全部按**严格 JSON 值模型**比较，不把字符串悄悄转成数字；**判不出来即失败关闭为拒绝**，绝不静默当"条件不匹配"放行。本层只做判定，不接管执行，也不替代宿主业务授权。
  - **ACC v1 的 OpenAPI 绑定（`integrations/acc/openapi.py` + OpenAPI 桥）**：带 `x-agent-capability` 的 operation 现在按绑定语义接入——只认 **operation 级**扩展（贴到根节点或 pathItem 上不产生任何治理效果，只出"贴错位置"诊断）；声明格式错误、主版本不认识、`approval.when` 的条件参数在归一化输入 schema 里解析不出来或撞上"同名参数同时来自 query 与 body"的归一化歧义时**整条操作跳过并留诊断，绝不回退成一个没有治理的同名工具**；`enabled=false`、要求可信主体但当前上下文没有、scope 不在宿主路由白名单里的同样不暴露。没有该扩展的 operation 零行为变化。
  - **两个宿主配置口**（`OpenApiSpecConfig`）：`acc_scopes`（空 = 不因 scope 拦截；尾部 `.*` 前缀匹配）与 `trusted_subject`（**默认 False**——主体必须由宿主在自己的信任边界上给，模型或参数里抄来的身份不算，因此带 `subject.required` 的操作在主体注入落地前一律不暴露）。
  - **`ToolSpec.governance` 新增两个字段**：`audit_sensitive`（宽口径审计提示，来自 `audit.sensitive`；只把**审计事件**那份的顶层值整体收口，给审批人的副本仍按声明路径脱敏——规范义务范围是 logs，全糊掉会让人工批准失去判断依据）与 `acc`（已校验声明的原样快照，内核只透传不解释，解释在 `integrations/acc`）。`governance` 的数据部分现已随 manifest 序列化（见本节首条）。
  - ACC 的 `guidance.when_to_use` / `returns` 并进工具描述（模型可读的用法提示，不是安全策略）；被跳过不暴露的操作不占用工具名，避免连带把后面的合法操作改名成 `__2`。新增 `tests/test_acc_openapi_binding.py`（19 例，spec 全内联、唯一网络走 MockTransport），讲义第 44 篇同步扩充；内核 `dependencies` 仍为空。
- **能力闸与宿主注入的可信主体（`ToolGovernance.gate` + `Subject`）**：声明终于参与每一次调用，而且只有一个方向——**外部契约只能把闸门往上抬**。
  - 内核新增中性类型 `GateDecision`（`reject` / `require_approval` / `reason` / `prompt`）与 `ToolGovernance.gate: (arguments, subject) -> GateDecision`：判定由宿主或集成层提供（`integrations/acc.build_gate(declaration, input_schema)` 编译一个闭包挂在 OpenAPI 工具上），**执行器只认这三种话、不 import 任何协议实现**，`integrations/acc` 因此仍是可选层；`governance.acc` 那份声明快照只做透明记录，本身不产生执行效果。
  - `ToolExecutor.execute` 在参数快照之后、权限策略之前过闸：`reject` 非空时**不过策略、不发 `TOOL_CALL`、不碰 handler**，理由作为 `role="tool"` 消息回灌给模型（与权限 DENY 同形：审计链不出事件，模型不瞎）；`require_approval` 把策略的 **ALLOW 抬成人工确认**，但策略 DENY 永远优先——契约不能替宿主放行。审批话术里的 `{参数}` 占位符**用脱敏后的副本填充**，密钥不会顺着 prompt 漏进 UI。
  - `AgentCore.run/astream(task, subject=Subject(...))` 由宿主注入可信主体，沿 loop → 执行器 → 授权闸逐次透传（不落在实例属性上，并发会话不会互相粘连）；`subject.required` 而本次运行没有主体时直接拒绝，**不沿用上一位的身份**。策略侧走能力旗标 `yai_subject_aware`：只有声明了它的策略才收到 `subject=` 关键字，旧的两参数签名一字不改照样跑。
  - 新增 `tests/test_capability_gate.py`（18 例：三种闸话、抬闸后的事件顺序、DENY 占优、拒绝先于策略与 handler、prompt 走脱敏副本、组合工具逐步继承主体、同一 core 连续三次运行的主体不粘连、两个 core 并发各认各的主体）。讲义第 44 篇新增第 7 节。内核 `dependencies` 仍为空；`gate` 作为函数至今（以及此后）都不进 manifest 序列化。
- **官方一致性语料落地**：`tests/fixtures/acc/vectors.json` 逐字节收录上游 26 条参考向量（Apache-2.0，出处/commit/SHA-256 见同目录 `SOURCE.md`），`tests/test_acc_conformance.py` 把每条向量喂进上述公开接口并比对期望结果（键集也必须一致），另加语料元数据、kind 构成与"新增 kind 不许沉默通过"的栅栏；`tests/test_acc_declaration.py` + `tests/test_acc_evaluate.py` 补齐向量之外的边界（scope 正则与长度、bool 不当整数、timeout/rate_limit 区间与窗口格式、approval 条目校验、九算子矩阵、嵌套点号路径、`!=` 对缺失参数的命中、各类失败关闭）。逐行讲义第 44 篇。
  - **口径与限制写进文档**：对外只声称 `ACC v1 OpenAPI Binding Parser Profile`；入参校验只实现 `type/required/properties/items` 四个关键字，`timeout_ms`/`rate_limit` 尚未映射到执行器策略，主体身份仍由调用方以布尔传入——把这些列为记录在册的限制，而不是含糊说"兼容 ACC"。内核 `dependencies` 仍为空。
- **声明式参数脱敏（`ToolSpec.governance`）**：内核新增 `ToolGovernance` 元数据（宿主声明、模型不可改），首个字段 `sensitive` 声明需要脱敏的**参数路径**（dot-notation，如 `api_key`、`headers.Authorization`、`rows.0.token`）。声明后，审批事件、`Channel.confirm` 入参、`TOOL_CALL` 审计事件与追踪 span 里的对应值统一替换为 `[redacted:len=N]`，而**实际执行仍拿真值**（脱敏不得改变业务行为）。内核刻意不按字段名关键词猜"像不像密钥"——猜的规则既会漏也会误伤审批可读性，因此"未声明即不脱敏"是有意的边界。新增 `policy/redact.py` 与 `tests/test_redaction.py`。
- **审批-执行-审计三方一致性（参数快照）**：`ToolExecutor.execute` 与两个过闸 meta-tool 在进入权限判定前对模型参数做深拷贝，`authorize_tool_call` 再为"给用户看的那一份"和"审计事件那一份"各持独立副本——通道或宿主在 `confirm()` 期间改写的只是它拿到的副本，改不动真正执行的参数，也污染不了审计链。补 `tests/test_argument_snapshot.py`（含"篡改审批后仍按批准值执行""拒绝后 handler 零调用且不发 `TOOL_CALL`"）。见讲义第 43 篇。

### Changed

- **对外文档改为"按类别"对比，不再点名同类项目**：`docs/comparison.md` / `.en.md` 重写为**类别描述**（通用框架 / 图编排运行时 / 多智能体框架 / 自托管平台 / 托管无代码平台），同时删除他方的自我宣传文案、星标数与指向他站的链接；README、`docs/architecture`、v0.6.0 发布说明、参赛材料里的同类项目名字一并改为中性类别说法。YAI 一侧的事实（零硬依赖、九契约、benchmark 数字、测试数、宿主数）逐条保留并复核，中英两份同步。**刻意保留**两类名字：我们实现/依赖的接口与库名（模型兼容接口、MCP、OpenAPI、PySide6 等），以及作为类比对象的嵌入式数据库名。已生成的 PDF 与参赛图 PNG 不在本轮重导（文本层已改，图里仍有旧名字，重导需另行确认）。

### Security

- **沙箱 AST 静态闸门：深嵌套代码从"异常穿透"改为失败关闭**：`examples/host_g_sandbox/_worker.py` 与 `shell/desktop/_sandbox_worker.py` 的 `_static_check` 用 `ast.NodeVisitor` 递归走语法树。实测 600 段属性链（`inputs.b.b.b…`）能被 CPython 的 PEG 解析器接受，却把 visitor 的递归撑爆，`RecursionError` 直接穿透闸门。worker 主流程那道宽 `except Exception` 恰好兜住了它，**因此没有实际放行过违规代码**；但"只捕 `_SafetyError`"的调用方会把闸门故障当成内部错误而不是拒绝——安全判定不该寄存在别人的异常处理上。现在深嵌套在闸门内显式转成 `_SafetyError("代码嵌套过深，静态校验无法完成，已拒绝执行")`：宁可误拒，不可误放。新增 `tests/test_sandbox_static_gate.py`（29 例：import / 反射 / dunder 属性逐条拒、深链失败关闭、干净代码不误拒、两份实现不漂移）与模糊测试 target `fuzzing/fuzz_sandbox_gate.py`（用迭代式 `ast.walk` 做独立 oracle 比对判定、要求闸门只抛自己的异常、比对两份实现一致性；Linux 的 CI `fuzz` job 自动纳入，本机 Windows 无 atheris wheel，故由上述 pytest 钉住同一条 case）。
- **host_e 跨进程工具口加本地令牌鉴权**：`POST /api/agent/tool` 与 `GET /api/agent/tools` 此前**无任何鉴权**——由于权限三档的决策在调用方（桌面灵动岛）完成，同机任意进程都能绕过审批直接执行宿主写操作。现在两个端点只认 `Authorization: Bearer <token>`：令牌按 `--tool-token` → 环境变量 `YAI_TOOL_TOKEN` → 本地令牌文件（`data/host_e/tool_token`，首次启动自动生成）解析，比较用 `hmac.compare_digest`；**绑定非环回地址时必须显式提供令牌，否则拒绝启动**。诚实边界：能读到令牌文件的进程即被视为同一用户身份，这一层挡的是同机其他用户/服务与误接客户端，不防同一用户自己写的恶意本地进程（那需要 OS 级隔离）。新增 `examples/host_e_sales_crm/tool_auth.py`、`tests/test_host_e_tool_auth.py`（27 项，含 401 时零业务副作用、两侧令牌约定不漂移）。

## [v0.9.0] - 2026-10-12

### Added

- **第三方播种端到端（S3）：host_j 记账宿主 + 五拍标准线**：
  - **`examples/host_j_actual/`**：把内核嵌进真实开源记账应用 [Actual Budget](https://actualbudget.org/)（官方主力是 Node 包 `@actual-app/cli`、金额一律整数分 cents、无朴素 REST），不改其源码，只把能力包成领域工具。`backend.py` 定义领域原语并提供离线 `FakeActualBackend`（2 账户 / 8 笔交易 / 3 笔未分类 / 娱乐预算超支）与 `CliActualBackend`（官方 CLI 子进程，runner 可注入、口令只走环境变量不进 argv、跨账户逐账户拉取后本地聚合、非零退出 / 坏 JSON 体面抛错）；`capabilities.py` 分三层工具（默认只读 / 需授权写入 / 按需发现的 `suggest_category`）；`demo_agent.py` 用脚本模型装配两条链路；`run.py`（`--delegate` 切换）与标准库零依赖网页壳 `web_app.py` + `web/index.html`（端口 8240，左原生记账 / 右嵌入 Core 对照）。
  - **五拍标准线 `examples/five_beats.py`**：把一次成功的自适应接管压成可观测、可断言的节拍——唤醒（`STRATEGY_SELECTED`）→ 长尾任务（≥2 个不同 `TOOL_CALL`）→ 缺口（`CAPABILITY_MISSING`）→ 补能力 / 派子员工（`TOOL_DISCOVERED` 或 `delegate`）→ 授权（`PERMISSION_ASKED`）→ 完成（`DONE`），供各宿主测试复用。
  - **两条链路离线跑通**：发现链路（缺口 → 现场发现 `suggest_category` → 新工具与写操作两次授权 → 3 笔未分类清零 → 点出娱乐预算超支）与委派链路（根一次 `delegate` 派两个白名单收窄到单一只读工具的子员工，并行执行、结果只回流根、完成即销毁）；授权被拒时诚实停止、账本零副作用。新增 `tests/test_host_j_actual.py`（17 例，含 CLI argv 锁定、两条五拍断言、`run_worker` 直驱验证子员工成功），全离线、不联网、不需要 Node 或 API Key；逐行讲义第 39 篇。内核本体零改动、零新增硬依赖。
- **运行指标与就绪探针（metrics / readiness，零依赖 + 可选 OTel）**：
  - **零依赖 `MetricsRecorder`**：新增 `metrics.py`，在 `astream` 唯一出口旁路 tap（开始 / 逐事件 / `finally` 结算），统计 runs（started/completed/failed/cancelled，取消类与真实异常分开）、按策略计数、工具调用 total/errors、模型档位 standard/strong（DONE 一次性结算）、各类事件计数，以及 run 墙钟时延直方图（`monotonic` 测时、`+Inf` 桶=总数）；一把 `threading.Lock` 兼容跨线程采集，`snapshot()` 深拷贝隔离，脏数据容错。`render_prometheus()` 手拼合规 exposition（histogram `_bucket/_sum/_count`），不装客户端即可被 Prometheus 抓。指标回调任何异常自吞、绝不反噬主流程；`CoreConfig.metrics` 可注入自定义收集器并做四方法鸭子校验。
  - **`/readyz` 与 `/metrics` 端点**：新增 `batteries/fastapi_server/readiness.py`——探针为普通同步/async 函数（None/True=ok、False/str=失败、dict 取 `ok`、异常=error），`asyncio.gather` 并发、`wait_for` 超时（默认 5s）、异常逐探针隔离，任一失败 `/readyz` 返回 503（编排据此摘流量、不重启）；默认探针只报告工具数与记忆后端、不调模型不联网，宿主可经 `readiness_probes=` 追加 DB/模型探活。`GET /metrics` 默认 Prometheus 文本、`?format=json` 结构化快照；配了 `YAI_API_KEY` 时 `/metrics` 默认鉴权、`YAI_METRICS_PUBLIC=1` 可显式公开，`/health` 与 `/readyz` 恒公开；GET 探针路径不受限流影响。
  - **OTel meter 桥接（可选 `[otel]` extra）**：新增 `integrations/metrics/otel_meter.py`，全部用 observable 仪器、采集时从 `core.metrics_snapshot()` pull（run 热路径零回调），不初始化 MeterProvider/Exporter；输出 `yai.agent.runs{outcome}`、`.runs.by_strategy`、`.tool.calls{tool,result}`、`.model.calls{tier}`、`.events{type}`、`.run.duration{le}` 累计桶 + `.sum`、`.uptime` gauge。新增 `tests/test_metrics_recorder.py`（8 例）、`tests/test_readiness_metrics_api.py`（9 例）、`tests/test_otel_metrics_bridge.py`（InMemoryMetricReader 离线断言）、逐行讲义第 38 篇，并在 `docs/deployment-scaling.md` 增补健康检查与指标抓取章节；内核仍零硬依赖。
- **长期记忆语义召回与任务起始主动召回（opt-in）**：
  - **词法 OR 语义双通道召回**：`MemoryVault` 新增异步 `arecall(query, *, k, kinds, tags)`，在原词法通道（token 相关度 + 重要度加权）之外，当宿主注入 `EmbeddingProvider` 时叠加语义通道（复用 `discovery/semantic.py` 的标准库余弦相似度，阈值 0.45），两通道按记忆 id 取并集、融合分取 `max`，跨语言 / 换说法（中文记忆 vs 英文 query）也能召回；同步 `recall` 保留为纯词法、行为不变，`recall` 工具 handler 改异步并走 `arecall`。记忆向量按需懒批量计算并按 id 缓存，`remember` 合并 / `forget` 删除即失效；embedder 抛错或返回数量不符时降级纯词法并记录 `semantic_last_error`，`status()` 新增 `semantic_enabled` / `semantic_last_error`。
  - **任务起始主动召回**：`AgentLoop` 新增可选回调 `auto_recall`，在历史之后、user 任务之前自动用任务文本检索长期记忆，命中则作为一条 system 上下文注入（明确"若与本次明确指令冲突以本次为准"）并发新事件 `MEMORIES_RECALLED`（条数 + 每条 id/kind/摘要）；clarify 反问路径不召回；回调任何异常只发本地诊断、绝不阻断主任务。由 `AgentCore(long_memory=True, memory_embedder=..., auto_memory_recall=True, auto_recall_k=3)` 开启，默认关闭、行为与旧版一致；`CoreConfig.validate()` 拦截"开召回但没开长期记忆""k 非正整数""embedder 非 async embed_texts"三类非法组合。新增 `tests/test_auto_memory_recall.py`（3 例 core 级端到端）、`tests/test_memory_vault.py`（+8 语义用例）、`tests/test_core_config.py`（+3 校验）与逐行讲义第 37 篇；内核仍零硬依赖。
- **结构化长期记忆（opt-in）**：新增 `memory/vault.py` 的 `MemoryVault` 与 `remember` / `recall` 两个普通工具——模型可主动把事实 / 偏好 / 画像 / 事件 / 笔记存档，并在需要时按"词法相似度 + 重要度加权"召回；写入对高相似条目去重合并（标签并集、重要度取大），落盘为 JSON（原子写、损坏冷启动容忍）。由 `AgentCore(long_memory=True, memory_storage=...)` 开启，另提供 `long_memory_status()` / `forget_memory()` 后台方法；内核不内置向量长期记忆，更强语义召回经 Embedding SPI 由宿主提供。新增逐行讲义第 28 篇。
- **端到端离线评测 harness**：新增零第三方硬依赖的内核包 `evaluation/`（`GoldenTask` 判分标准、`collect_trace` 从事件流还原轨迹、`score_task` 打分、`aggregate` micro 汇总），量化**完整 AgentCore** 的任务完成率、工具调用精确率 / 召回率、越权（调用禁调工具）与无支撑断言（答复凭证无成功工具结果出处）；配套 `scripts/eval_tasks.py`（业务宿主 + 5 条 golden cases，含高危工具"诱惑"）与 `scripts/eval_agent.py`（ScriptedModel 真跑 `astream` 的 runner，`--gate` 闸门、写 `benchmark_out/eval_agent.json`）。golden 基线应全通过，作为"改内核 / 改配置行为不退化"的回归保障；新增非门禁 CI `eval` job 与逐行讲义第 29 篇。口径诚实：`unsupported_claims` 是幻觉的必要不充分信号、不宣传成幻觉率，纯 direct 任务跳过出处核验。
- **远程发现源按 need 筛选**：`RemoteSourceDiscovery` 此前返回配置内全部工具，现对已构造 ToolSpec（无 keywords）做保守双通道收窄：词法用 name+description 的**双向**相关度（query→工具、工具→query 取大，修正单向对长 query 的稀释），按实测分布标定阈值 0.20、margin 0.08、top_k 8；宿主提供 embedder 时叠加语义 gate（跨概念高召回）。任一通道识别即收窄、两通道都不中**回退全量**（跌破阈值只导致不收窄、不漏召回），语义通道失败不致命；`filter_by_need=False` 保持全量。新增逐行讲义第 30 篇，`tests/test_remote_discovery.py` 由 5 扩到 11。真机 bge 端到端待补 ONNX 权重。
- **动态重规划 replan（PLAN 专属）**：新增 meta-tool `replan`（`tools/meta.py`），模型在执行多步计划中、因新结果发现原计划走不通（漏前置 / 顺序要调 / 前提不成立）时可显式调用；loop 进入 PLAN 分支时幂等注册（`_ensure_replan_tool`，空宿主 / direct / 简单 react 不注册），`_handle_replan` 让模型保留已完成步骤、基于当前观察产出修正计划，发 `plan_revised` 事件（修订序号 / 原因 / 新步骤，解析不出步骤回退原任务），回灌后按新计划继续。replan 只重排内部计划、无外部副作用，因此不走用户授权闸，业务工具仍逐个授权。另修正聚焦模式：meta-tool 完整 schema 经 `_with_meta_schemas` 在大规模工具集裁剪下始终注入（同时修复 request_capability / create_code_tool 的可见性）。新增逐行讲义第 31 篇与 `tests/test_replan.py`。
- **成本·延迟感知选档 tiering**：新增零硬依赖的 `kernel/tiering.py`——默认 standard（便宜快），仅在生成 / 修订计划（`for_planning`）、工具失败后的纠偏轮、执行到第 3 轮仍未完成（`select`）时才升 strong，选档本身是确定性规则、不额外调用模型；档位相对上一轮变化才发 `tier_changed` 事件（首轮声明起始档），DONE 事件新增 `standard_calls` / `strong_calls` / `model_calls` 成本汇总。由 `AgentCore(cost_aware=True/False)` 开关，关闭时执行轮恒 standard、行为与旧版一致，宿主可自定义规则松紧。新增逐行讲义第 32 篇与 `tests/test_tiering.py`。
- **结构化 tracing（OpenTelemetry，opt-in）**：新增可选 `[otel]` extra 与 `integrations/tracing/`，在**外侧订阅** `astream` 事件流（不改 kernel/loop、不把 opentelemetry 转成硬依赖、库内不初始化 TracerProvider/Exporter）：一次 run 映射为一个 root span `yai.agent.run`（任务、最终策略、模型档位、结果长度、错误状态），每次工具调用是其下的 `yai.tool.<name>` 子 span（参数、成功预览 / 失败置 ERROR，对无配对 CALL 的组合 / meta 工具结果补即时 span，流中断时关闭未闭合 span），策略选择 / 计划 / 澄清 / 能力缺口 / 发现·组合·创建工具 / 压缩 / 重规划 / 档位切换等落为 root span event。对外提供 `traced_astream`（边渲染边埋点、事件一个不丢）与 `traced_run`（聚合为 RunResult）；宿主配好 TracerProvider / OTLP 即可对接 Jaeger / Tempo。新增 `tests/test_tracing_otel.py`（InMemory exporter 离线断言 span 树，模块 100% 覆盖）、`docs/tracing.md`（中英）与逐行讲义第 33 篇；CI lint&test 矩阵加装 `[otel]`。
- **生产加固三件套（并发契约 / 模型硬超时 / 零依赖诊断）**：
  - **并发串行契约**：`AgentCore` 持有实例级 `asyncio.Lock`，`astream`/`run` 全程（含跨 `yield` 与 finally 的学习反馈回灌）串行，杜绝两条任务在 `await` 点交错踩脏共享的上下文 / 记忆 / 注册表 / 学习态；生成器被 `aclose()` 或宿主提前关闭时锁自动释放。明确"**一个实例 = 一条串行会话，要并发就多实例**（无状态模型 / 发现 / 沙箱可共享单例，registry/memory/learning 随实例独立）"。
  - **模型调用硬超时与取消传播**：`AgentCore(model_timeout=60.0|None)` 在 loop 层用 `asyncio.wait_for` 统一包裹全部模型调用（不改 `ModelProvider.achat` SPI 签名），超时抛 `TimeoutError` 并体面收尾、`CancelledError` 正确传播不被吞；`compact_context(timeout=...)` 超时回退硬裁剪；三份沙箱重构为 asyncio 原生子进程，超时 / 取消先 `kill()` 再排空 transport（Docker 另强制删除一次性容器），不再遗留后台进程。
  - **零依赖本地诊断 SPI**：新增 `diagnostics.py` 的 `DiagnosticEvent / DiagnosticKind / DiagnosticLevel / DiagnosticSink / emit`，承接"已静默兜底、主流程不失败、但排障必须看得见"的瞬间（模型错误 / 超时 / 多模型降级 / LLM 路由回退 / 发现失败 / 可发现目录失败 / 压缩回退 / schema 聚焦回退共 8 类）；回调同步、须快速非阻塞，回调自身异常被吞、绝不反噬主流程，内核不依赖 `logging`。由 `AgentCore(on_diagnostic=fn)` 一处传入并鸭子接线给内部 router 与 FallbackModelProvider，不传则完全安静、行为与旧版一致；沙箱失败 / 权限拒绝等本就用户可见的业务事件不重复发诊断。新增 `tests/test_core_concurrency.py`、`tests/test_model_timeout.py`、`tests/test_diagnostics.py`（全离线）、`docs/architecture(.en).md` §2.6/§2.7 与逐行讲义第 34 篇。
- **生产加固 P1（预算熔断 / 落盘加密 / CoreConfig / SQLite 热备份）**：
  - **任务级预算硬上限与熔断**：新增零依赖 `kernel/budget.py` 的 `Budget(max_model_calls/max_tool_calls/max_tokens)`（`None`=不限、`0`=一次不允许，拒绝负数与 bool）、`BudgetTracker` 与 `BudgetExceeded`；loop 在顶层任务建 tracker（澄清递归复用），模型计数走统一入口 `_model_chat`（不含路由分类与压缩）、工具计数在普通工具分支（meta-tool 与组合编排不重复计、组合工具对外算 1 次）、token 仅在模型回传真实 `usage` 时累计（新增 `TokenUsage`，`openai_compat` 透传，缺失不估算）。超限立即终止、发 `BUDGET_EXCEEDED` 事件（带 scope/used/limit 与账本快照），DONE reason 为 `budget_exceeded`，两处回退路径不吞该异常。由 `AgentCore(budget=Budget(...))` 开启，不传则行为与旧版一致。新增 `tests/test_budget.py`（10 例）。
  - **长期记忆落盘静态加密（第九个 SPI，opt-in）**：新增 `spi/cipher.py` 的同步 `StorageCipher` 协议（`encrypt/decrypt(bytes)`，解密失败抛异常）；`MemoryVault(path, cipher=...)` 落盘时透明加密、读取时解密，原子 `os.replace` 写盘，文件缺失 / JSON 损坏 / **解密失败（密钥不符或被篡改）一律冷启动为空库并重写**，绝不让进程起不来；不传 cipher 仍为明文 JSON（向后兼容，docstring 明确告知）。新增可选 `[crypto]` extra 与 `integrations/crypto/fernet_cipher.py`（Fernet 认证加密，懒加载 cryptography，`generate_key()` / `from_env("YAI_VAULT_KEY")`，缺环境变量直接报错而非静默生成一次性密钥）。由 `AgentCore(memory_cipher=...)` 透传。新增 `tests/test_storage_cipher.py`（FakeCipher 验接线 + 真实 Fernet round-trip/篡改/from_env）。
  - **CoreConfig 集中配置与早期校验**：新增零依赖 `config.py` 的 `CoreConfig` dataclass，收敛 `AgentCore` 全部与模型无关的可选项（默认值与 kwargs 一致），`validate()` 在构造早期拒绝非法取值（迭代次数非正、bool 冒充整数、`llm_router`/`context_compaction`/`budget`/`model_timeout` 类型错误），`as_kwargs()` 展开为构造参数；新增 `AgentCore.from_config(model, config)`（一份配置复用于多专员 / 多子员工），公共 kwargs 签名与默认行为不变；顺手把 router 装配抽成纯静态方法 `_resolve_router`，缩短构造器。新增 `tests/test_core_config.py`。
  - **SQLite 在线热备份与恢复**：`SqliteStore.backup_to(dest)` 用标准库 `sqlite3.Connection.backup` 在实例锁内做页级一致快照（**无需停服**，文件库与 `:memory:` 均可，目标 db/-wal/-shm 先干净清掉），`@classmethod restore_to(backup, dest)` 离线恢复（备份损坏时直接抛 `sqlite3.DatabaseError`，不静默起空库）；崩溃恢复由 WAL + 原子写保证（close 后同路径重开数据不丢）。新增 `tests/test_sqlite_backup.py`（7 例）。
  - 新增逐行讲义第 35 篇与 `docs/architecture(.en).md` 对应小节；新增模块行覆盖率 budget/config/cipher/sqlite_store 均 100%、fernet_cipher 93%，全仓约 96%。
- **子员工预算兜底与跨层级共享并发闸**：
  - **子员工默认挂硬预算**：`shell/employee/worker.py` 新增 `default_budget(max_iters)`（模型调用 `max_iters+2`、工具调用 `max_iters*4`、token 不硬限），`_assemble_worker` / `run_worker` 新增 `budget` 形参（None 时自动推导），每个子员工在构造 `AgentCore` 时即带上独立预算；触发预算硬墙时 `WorkerResult.status="budget_exceeded"`、`ok=False`，已产出文本与预算快照随结果回流，根可据此缩小任务或放宽预算重派，熔断不作为异常穿透、不拖垮并行 gather。
  - **跨层级共享全局并发闸**：新增 `delegate.DelegationSupervisor(max_concurrent)`（持单一 `asyncio.Semaphore`），`attach_delegate` / `build_delegate_tool` 新增 `supervisor` 形参并返回该闸，沿根→子→孙原样透传，把"根并行 5、每个子又并行 5"的理论超额收敛为全局同时存活 ≤5；delegate schema 每个 task 新增可选 `max_model_calls` / `max_tool_calls`（容错非法值，未覆盖维度用默认推导补齐），`_normalize_tasks` 透传。
  - 新增逐行讲义第 36 篇；`tests/test_shell_employee.py` 新增默认预算推导、DONE 预算快照、budget_exceeded 熔断、跨层级并发峰值（共享时 peak=2、旧行为会得 4）、取消传播共 5 组离线用例，全离线、不依赖 API Key。
- **上下文轻量化（降本 / 缓存友好，#39 / #40）**：
  - **工具结果统一有界收口 + 目录确定性排序**：普通 / 代码 / 组合三类工具的成功结果统一在执行器出口按字符上限收口（默认 `tool_result_char_limit=16000`，超出保留头部 70% + 尾部 30%），`TOOL_RESULT` 事件附带 `truncated` / `result_chars`；喂给模型的工具目录改为按工具名确定性排序（`all()` / `schemas_for()` 的原始注册顺序不变），让无状态、无时间戳的 system 前缀在多次调用间字节稳定，利于 prompt cache 命中与可复现。新增逐行讲义第 41 篇与架构 §2.10（中英）。
  - **摘要压缩可注入专用小模型 + 上下文窗口水位工厂**：`AgentCore(compaction_model=...)` 让历史摘要走独立的便宜小模型（缺省回退主模型），主链路仍用主模型，长会话压缩成本显著下降（不为低温参数扩展 `achat` SPI）；新增 `CompactionPolicy.from_context_window(chars, *, keep_recent_rounds=2, hard_ratio=0.6, target_ratio=0.4)`，按模型上下文窗口字符口径一键推算硬触发 / 目标水位，不引入 tokenizer。新增逐行讲义第 42 篇、架构 §2.5（中英）与 `tests/test_context_compaction.py`（22 点）。
- **长期运行记忆规模治理（#38）**：`MemoryVault` 检索改词法倒排索引并支持可选 `max_notes` 容量上限，抑制长期部署下记忆无限增长导致的召回劣化；SQLite 历史裁剪 `prune` 改为先 `COUNT` / `MIN(时间戳)` 索引聚合短路，未达阈值不取回整表，降低大规模历史下的裁剪开销。新增逐行讲义第 40 篇。

### Changed

- **多租户 / 水平扩展部署指南增强并首次英文化（#41）**：`docs/deployment-scaling.md` 把 scope 明确为"租户内会话 / 专员边界"，新增跨不可信企业的硬隔离形态（SQLite-per-tenant 目录 + 每租户独立 DEK / KMS 信封加密 + `TenantCoreFactory` + 空闲 LRU 换出 + Postgres RLS 升级路线）、九维每租户隔离矩阵与 prompt cache 跨租户边界；立场写死"`tenant_id` 不进 kernel 签名、多租户是装配层责任、内核零改动"。新增全文对照英文版 `docs/deployment-scaling.en.md`，顶部双向互链，基线升 v0.9.0。
- **测试覆盖**：补齐 `llm/openai_compat.py` 与 readiness 探针的边界分支至 100%（#37）；全量行覆盖率约 96%。

### Security

- **修复 Signed-Releases 对 GitHub 自动源码包的签名覆盖**：发布工作流此前只签 `uv build` 产出的下划线名 sdist / wheel，而 GitHub Release 自动生成的连字符源码包 `yai-agent-core-<ver>.tar.gz` / `.zip` 缺少同名签名件，OpenSSF Scorecard Signed-Releases 因此仅给低分。现新增发版步骤：下载这两个 auto archives，用 cosign keyless 签成精确同名的 `.sigstore`（bundle 内含签名证书与 Rekor 透明日志），随同一 Release 上传。

## [v0.8.1] - 2026-10-11

> v0.8.0 之后的供应链安全、开源治理与工程加固版本；内核定位与 API 不变。

### Added

- 通过 **OpenSSF Best Practices 自评并获得 Passing（过境）徽章**（项目编号 [#15360](https://www.bestpractices.dev/projects/15360)，2026-10-10，Basics/Change Control/Reporting/Quality/Security/Analysis 六区全部满足）；中英 README 顶部加入 Passing 徽章。
- **英文门面文档**：新增 `README.en.md`、`CONTRIBUTING.en.md`、`docs/architecture.en.md`、`docs/comparison.en.md`，`SECURITY.md` 英文化（中文另存 `SECURITY.zh-CN.md`），各份顶部双向互链并约定事实性内容中英同步、防漂移；逐行讲义与比赛过程文档不翻译。
- **发布资产签名 + SLSA 来源证明**：`publish.yml` 发版时对 wheel / tar.gz 生成 build provenance（`attest-build-provenance`），并用 cosign keyless 对 wheel / tar.gz / SHA256SUMS 逐个签名（随附签名束 `.bundle`，含签名与证书），全部作为已签名资产随 GitHub Release 发布；补齐 PyPI `Project-URL: Documentation`。
- **SAST 与供应链安全体检**：接入 CodeQL（python，0 alerts）与 OpenSSF Scorecard（Linux 跑分、公开发布结果并加 badge），workflow 的 token 权限收敛到 job 级最小集。
- **atheris 模糊测试**：新增 `fuzzing/`（schema / 路由特征 / 上下文压缩三组 harness）与非门禁 CI fuzz job；atheris 仅有 Linux x86_64、py3.12+ 的 wheel，Windows / macOS 不跑。
- **CI 路由基准任务**：新增非门禁 `benchmark` job，离线跑路由自校准基准并上传 `router-benchmark` artifact（学习曲线 SVG + JSON，保留 90 天），为"末段 bandit 显著优于规则"提供 CI 证据链。
- **pre-commit 本地钩子**：新增 `.pre-commit-config.yaml`（ruff / mypy / gitleaks）。

### Changed

- **工程整洁**：`.gitignore` 统一收纳 `inbox/`、增长方案与赛事 / 文档过程稿，保持工作区干净。
- **降低内核复杂度**：重构 `AgentLoop` 提取 helper 方法，并补 cli / introspection / bge / openapi 等模块的边界测试。

### Fixed

- **发现目录健壮性**：发现源可选的 `list_discoverable()` 返回非列表（如 `None`）时退化为被动发现，不再让"None 不可迭代"错误穿透主循环；`kernel/loop.py` 行覆盖率达到 **100%**（全量 **535 passed、8 skipped、总覆盖率 95.22%**）。

### Security

- **依赖与镜像加固**：基础镜像 pin 完整 digest，pyjwt 升级至 2.15.1，消除若干已知 PYSEC。

## [v0.8.0] - 2026-10-10

### Added

- **代码工具仓库管理（灵动岛「工具」入口）**：内核 `CodeToolManager` 新增 `set_permanent(name, on)`（可逆开关，值未变不落盘）与 `remove(name)`（立即删除任意指定工具，区别于只批量回收过期项的 `expire_stale`），`make_permanent` 改为委托，`records()` 每项带上 `description`。薄壳侧 `shell/desktop/tool_vault.py` 用**不接模型、不接沙箱**的轻量 manager 直接读写持久化文件（复用内核同一份格式、原子写与过期判定）；工作台的 `openCodeVault / setCodeToolPermanent / removeCodeTool` 把读写**派发到常驻 worker loop**，与运行中的 Core 同线程串行，杜绝"面板改了、Core 又用旧内存态覆盖"的竞态，结果经 `codeVaultUpdated` 回 GUI。面板因此**空闲时也能打开**（Core 是每任务临时的，不能依赖当前实例）；任务执行中只可查看，写操作置灰并提示"下次任务生效"。新增 `tests/test_tool_vault.py`（4 项，按文件路径加载、不依赖 Qt）与 3 项内核回归、2 项工作台冒烟。（一个灵动岛管多个专员）**：`WorkbenchRuntime` 持有唯一一条后台 asyncio 循环，下挂多个 `SpecialistRuntime`（销售 / 仓库 / 笔记 / 陪伴），每专员**独立 busy、挂起决策、权限挡位、未读、时间线**；QML 只连工作台的聚合 Property（`activeBusy / activePendingKind / activePermissionMode / …`）与统一信号 `specialistEvent(sid, type, payload)`。定位钉死：**灵动岛是前台/交换机，专员的大脑长在应用里**——不聚合工具、不做意图自动分发。`SpecialistListModel` 供切换条显示字形/状态点/未读角标，切换后旧行一并刷新（避免双高亮）。新增 `tests/test_desktop_workbench.py` 5 项隔离测试（忙隔离、挂起隔离、事件带 id、未读、收尾唯一 loop）。
- **三态交互形态机**：待命是**圆球**（56×56，中央动态波形，执行中起伏），指针驻留 110ms **向右展开成胶囊**（340×52，状态文本 + 呼吸点），点击**向下展开成面板**（420×620）；指针离开窗口回到球，但**有授权/澄清挂起时拒绝收回**（离开判定留 320ms 宽限，避免跨子项抖动）。左上角锚定，整岛移动交给 `Window.startSystemMove()`（位移超 4px 判拖动，否则算点击），不再手算 DPI/多屏位移。
- **面板改为"对话优先"**：主体只有"你说"与"它答"两种气泡，思考链与调用链折叠成一条 `› 过程 · N 步`，点击才展开明细；壳层改为不透明（半透明在开着其他软件的桌面上会让对话看起来是空的）。列表数据按专员各存一份 ListModel，切走再切回历史不丢。
- **离线演示的授权卡改为真挂起**：`shell/demo_script.py` 走到 `permission_asked` / `clarify_requested` 时不再伪造事件，而是真的 `await channel.confirm/ask` 等待界面点击——无 Key 演示里的允许/拒绝走完整权限链路，拒绝会按"用户拒绝，未执行"收尾；并补齐仓库 / 笔记 / 陪伴三段主题脚本。
- **原生桌面端 `shell/desktop/`（PySide6 + QML）**：屏幕顶部居中的灵动岛胶囊（340×52）与员工面板（420×620）之间是**真窗口形变动画**，配系统托盘；内核与界面**同进程**（`ShellRuntime` 在专用 asyncio 线程跑 `AgentCore`，事件经 Qt 信号跨线程投递，授权/澄清由 `QtChannel` 挂起等待界面决策），不再需要本地 HTTP 端口与 SSE。动效逐条绑定真实内核事件（`strategy_selected` 涟漪、`capability_missing` 琥珀描边脉冲、`tool_discovered` 新能力飞入、`permission_asked` 自动展开授权卡、`delegate` 子员工出芽），只做小面积属性动画，不开全窗 alpha 模糊。新增 `[desktop]` 可选依赖，`python -m shell.desktop`（默认离线演示、`--live` 接真实模型）；`tests/test_desktop_runtime.py` 8 项离线测试覆盖投递/挂起兑现/忙拒绝/取消/非法挡位，未装 extra 时整体跳过、CI 四矩阵不受影响。新增讲义第 22 篇。
- **语言中立能力清单（manifest）序列化**：`ToolSpec.to_manifest_dict()` / `from_manifest_dict()` 与 `tools/manifest.py`（批量打包 + JSON 文件读写）。composite 工作流（steps）与 code 工具（code，需宿主沙箱）作为"数据即能力"可跨进程 / 跨语言完整移植并执行；native/openapi/mcp 的 handler 不可移植，只导出能力声明。内核不另造调用通道（避免做成简化版 MCP），跨语言实际调用统一走 MCP / OpenAPI。新增 `tests/test_manifest.py`（含移植后组合工具端到端执行验证）与讲义第 21 篇。
- **工程文档**：`docs/deployment-scaling.md`（部署 / 水平扩展 / 多租户）、`docs/multilang-interop.md`（多语言接入设计与 conformance 愿景）。
- **PyPI 发布就绪**：补全 classifiers / project URLs、PEP 561 `py.typed` 标记，sdist 改为显式白名单（只含库源码、测试与核心文档，排除 inbox 与过程稿）；新增 OIDC Trusted Publishing 工作流 `publish.yml`（推 `v*` tag 触发、免 API token）。

### Changed

- **CI 工程门禁**：GitHub Actions 新增 mypy 类型检查（`src/yai_core` 零错误，66 文件）与覆盖率门禁（`--cov-fail-under=90`，实测 92.58%、474 passed）；`checkout` / `setup-uv` 钉完整 commit SHA。
- **Windows CI 转绿**：真机 Docker 沙箱用例改按 `docker info` 的 OSType 判定，OSType 非 linux / 无 daemon 时 skip，修复 windows-latest（Server 2025）默认 Windows 容器模式导致的失败。

### Security

- 新增 `SECURITY.md`（支持版本表、公网部署必须配置 `YAI_API_KEY`、Subprocess/Docker 沙箱边界、发现不等于授权、私密报告渠道）与 `.github/dependabot.yml`（pip + Actions 每周更新、open-pull-requests-limit 3）。

## [v0.7.0] - 2026-09-22

### Added

- **随包发布零依赖确定性模型 `ScriptedModel`**（`yai_core.llm`）：`pip install` 后无需 API Key、不联网即可在 30 秒内验证内核已正确嵌入并跑通工具调用；README 新增"路线 A：零 Key 验证嵌入"，并有 `tests/test_quickstart.py` 作为可执行回归。
- **多模型兜底 `FallbackModelProvider`**：主模型 429 / 5xx / 超时 / 空响应时按序降级，带断路器冷却；只配置单个模型时默认休眠，运行时行为不变。
- **数字员工宿主 E 网页工作台成品化**：原生 CRM 界面（客户/订单/跟进/待办/日报五视图）+ 可收起的 AI 数字员工抽屉；内置按需能力目录（拜访天气、含税报价），演示"缺口感知 → 发现 → 授权 → 本轮可用"闭环。
- **数字员工宿主 E 产品化增强**：原生 CRM 扩为客户 / 订单 / 商机 / 跟进 / 待办 / 日报六个视图，内省自动发现 18 个业务工具（10 读 8 写）；新增客户搜索与等级/状态筛选、客户资料编辑、订单录入、商机看板（新建 + 阶段推进）等读写功能，全部不依赖 Core 即可使用，应用内开关一键对比有无 Core。
- **ToolDiscovery 最小闭环（第 5 个 SPI）**：模型报出 `missing_capability` 后，内核向发现源（内置 `StaticCatalog`）要候选、去重注册、发 `tool_discovered` 事件，新工具本轮 ReAct 即可调用；发现源异常不致命，"能被发现不等于被授权执行"。新增宿主 F 离线演示。
- **能力缺口感知**：LLM 路由输出 `missing_capability`，内核据此发 `capability_missing` 事件（规则路径、澄清、无工具部署不发）。
- **`llm_router="auto"`**：按模型后端 `yai_live_router` 标记自动决定是否启用 LLM 分类，真实模型开启、离线脚本模型走规则。
- **开源治理资产**：`ROADMAP.md`、`docs/comparison.md`（与主流框架的诚实对比）、Issue/PR 模板、CONTRIBUTING"新人 30 分钟跑通"、CI 四矩阵（Ubuntu 3.11/3.12/3.13 + Windows 3.13）。
- **自校准路由（第 8 个 SPI `learning`）**：`ContextualBanditSelector` 按 9 维任务特征在线学习路由策略选择，宿主 E 持久化反馈、网页学习面板可查看；离线 benchmark 随反馈累积末段准确率较纯规则约 +16pp；无反馈取规则先验，冷启动确定、模型不可用不影响。
- **双通道语义工具发现**：新增 `EmbeddingProvider` SPI 与 `SemanticCatalog`，词法相关度与语义向量相似度融合，支持同义改写/跨语言召回；本地 `bge-m3` ONNX 离线后端与 OpenAI 兼容云端后端可选，无 embedder 或出错时自动降级纯词法。
- **两层工具目录与执行中动态发现**：系统提示只放工具摘要目录，LLM 选定后再注入完整 Schema；ReAct 循环执行中可触发发现源、注册新工具并在本轮使用。
- **组合工具 composer 与权限三档**：模型可把已注册工具编排成新工具（只引用已注册工具、每步仍授权，能力不越界）；权限支持全部审批 / 读放行写授权 / 无需审批三档 + 白名单，运行时可切换。
- **代码工具注册 / TTL / 授权闸与子进程教学沙箱**：`ToolSandbox` 契约 + 宿主侧 `SubprocessSandbox`（白名单内置、超时杀循环、输出截断、Unix 内存上限、授权后执行、TTL 到期回收），新增 host_g 教学沙箱宿主。

### Changed

- 系统提示词由"你只能通过工具操作"改为"你可以使用工具；能力不足时请明确指出缺失能力"，同时保留"不要编造工具不存在的数据"防幻觉护栏。

### Fixed

- 澄清循环死锁：意图不清时追问最多两轮、上下文累积合并、空回答体面收尾，重置会先终止卡住的任务。
- 无 `[local-embed]` 环境下 `LocalBgeEmbedder` 缺模型时先抛 `ModuleNotFoundError: numpy` 而非设计的 `RuntimeError("bge-m3…")`：调整为先 `_load()` 完成模型/依赖检查再 `import numpy`，并用 `sys.modules['numpy']=None` 加回归测试锁定顺序，修复 CI 四矩阵失败。

## [v0.5.0-hangzhou]

- 2026 AI 杭州·码动未来"超级智能体"赛道提交版本（tag 指向该比赛快照）：MCP Client、LLM 路由器（规则兜底）、SQLite 持久化记忆（opt-in）、OpenAPI 发现、FastAPI 在线 API、多宿主 demo、容器化与 PaaS 部署。

## [v0.1]

- 函数内省、规则路由、Agent Loop、五个 SPI 默认实现、FastAPI Battery、三宿主 demo、容器化与 PaaS 部署。
