# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 与语义化版本。
**内核本体始终保持零第三方硬依赖**（`pyproject.toml` 的 `dependencies = []`），真实模型、Web 服务、MCP、OpenAPI 等能力全部走可选 extras 与 SPI 契约懒加载。

每个版本的逐条变更以本文件对应小节为准；[`docs/releases/`](docs/releases/) 放**GitHub Release 正文源稿**（人话版，有则写，无则 Release 正文由工作流从 PR 标题自动生成）。版本号与 GitHub Release / tag 由维护者拍板后发布。

## [v0.24.0] - 2026-10-27

本版把"可信"往前推了三步：**成本**（提示缓存）、**注入**（工具输出按不可信数据处理 +
离线回归集）、**审计**（执行收据哈希链 + 权限拒绝成为可见事件），外加两项长会话优化
（并行压缩、任务锚点）。零新增运行时依赖，内核 `dependencies` 仍为空。

### Added

- **提示缓存**（`AnthropicProvider(prompt_cache=True)`）：system 与工具清单打上
  `cache_control: {"type": "ephemeral"}`，这两段是每轮完全相同的前缀，命中后按官方计价
  输入 token 可省一个数量级。块状 system 不被某些兼容代理接受时，传 `prompt_cache=False`
  退回纯字符串。配套 3 例测试（默认形态 / 关闭 / 只在最后一个工具打断点）。
- **工具输出当"不可信数据"处理**（`AgentCore(tool_output_guard=True)`，默认开）：工具返回值
  回灌上下文前包成 `<tool_output untrusted="true">…</tool_output>`，把间接提示注入的成功率
  往下压（Spotlighting 类防御的最小实现）。宿主可关掉；事件流与审计收据里的原始值不受影响。
- **执行收据哈希链**：每条收据带 `prev_hash` 与 `receipt_hash`（SHA-256，含前一条哈希），
  用 `verify_receipt_chain(receipts)` 一条命令校验整串有没有被改动 / 抽条 / 换序。
  对应 IETF Agent Audit Trail 草案里"prev_hash 串链"的思路，不引入任何新依赖。
- **执行后验证（后置条件）**：`ToolGovernance.postcondition` 可声明
  `(arguments) -> bool | Awaitable[bool]`。只在该次调用结果为 `unknown`（超时 /
  同步 handler 被放弃 / handler 抛错 / 沙箱异常）时运行：确认副作用已发生 → 收据改判
  `succeeded` 并重新接链，模型看到成功；确认没发生或检查自身出错 → 保持 `unknown`，
  只记录 `postcondition=rejected|error`，绝不替宿主猜。
- **权限拒绝成为可见事件**（`PERMISSION_DENIED`，第 21 类事件）：此前被策略 / 能力闸 /
  频率上限 / 审批拒绝的调用只把原因回给模型，**事件流里完全看不见**，宿主没法审计
  "这次调用为什么没发生"。现在这四条路径各发一条 `permission_denied`
  （`tool` / `source` / `reason` / 按声明脱敏后的 `arguments`）。事件类型守卫同步到 21 类。
- **OpenTelemetry GenAI 语义约定**：trace 上补 `gen_ai.operation.name` / `gen_ai.agent.name`
  （root）与 `gen_ai.tool.name` / `gen_ai.tool.type` / `gen_ai.tool.call.id`（工具 span），
  Jaeger / Grafana 一类后端可按标准识别 agent 与工具调用。
- **并行摘要压缩**：`CompactionPolicy(parallel=N)` 把待压缩的旧轮按轮首切成至多 N 段
  并发摘要、按序拼接；墙钟随段数下降，保留策略与消息结构完全不变（默认 1 = 旧行为）。
  `CONTEXT_COMPACTED` 事件新增 `parallel_segments` 字段。
- **任务锚点（上下文腐化防护）**：长任务被裁剪 / 压缩后，若原始任务已不在消息里，
  下一次模型调用会把「当前任务」重新锚到瞬态 system 位；任务仍在上下文时不重复注入。
- **注入型工具输出的离线回归集**（`tests/test_injection_regression.py`）：假设模型
  **完全照注入的话去做**，验证内核的确定性防线仍然拦得住——策略 DENY、资源作用域闸、
  审批拒绝三条路径各一例，断言业务 handler 一次都没被执行、模型收到拒绝原因、
  事件流里有 `permission_denied`；另断言注入文本进入模型前已被包成不可信数据块。

## [v0.23.0] - 2026-10-27

本版把"**能不能当场验证**"做成仓库的一等公民：一条命令跑完治理闭环、一条命令跑完全部门禁、
一份能力与证据索引；桌面星云加了粒子数偏好；仓库内容整理为通用口径
（部署手册与配图迁到通用位置、移除过期清单与提交目录）。

**升级注意**：部署验证端点改名为 `/.well-known/yai-verification.json`
（原路径不再提供）。其余为新增能力与文档整理，无内核 API 变更。

### Added

- **一条命令跑完治理闭环**（`scripts/demo_governance_loop.py`）：离线脚本模型发起一次退款调用 →
  内核在参数级审批处拦下并冻结参数 → 批准才执行、拒绝零副作用 → 全过程写成可回查的
  JSONL trace。带 `--refuse`（验证零副作用）与 `--trace`（落盘路径）；退出码即断言。
  参数取值只进 trace，不打到终端。配套 `tests/test_demo_governance_loop.py` 两例回归。
- **一条命令跑完全部门禁**（`scripts/check_all.py`）：锁定文件 → lint → 类型检查 → 版本与打包守卫
  → 全量离线测试 + 覆盖率门禁 → 两宿主冒烟，任何一步失败立即停下并给出退出码；`--fast` 跳过全量测试。
- **能力与证据索引**（`docs/IMPLEMENTATION_STATUS.md`）：把"已验证 / 已实现·未真机验证 / 明确不做"
  分三档列出，每条能力都给出对应测试或实测证据与边界，避免把"支持"读成"验证过"。
- **桌面星云粒子数可配置**（`ui/nebulaParticles`，默认 260）：`NebulaOrbCanvas` 的粒子数改由
  `particleCount` 属性驱动，胶囊与面板顶栏两处都绑到 `workbench.nebulaParticles`；写入范围收敛
  24–1200。实测 260 → 520 仍是 60fps，但单核 CPU 约 13.8% → 20.3%——粒子数是真成本，
  默认值保持与设计稿同源的 260。

### Changed

- **仓库内容整理为通用口径**：原材料目录里只有两类内容值得留下，已迁到通用位置——
  部署手册 → `docs/demo-deployment.md`，白皮书配图与绘图脚本 → `docs/figs/` + `scripts/draw_figs.py`；
  过期清单、视频录制脚本、提交目录（`submissions/`）与对应构建脚本一并移出仓库
  （内容仍在 git 历史里，可按需取回）。代码与文档里的措辞统一为"公网演示 / 探活"。
- **部署验证端点更名**：改为 `/.well-known/yai-verification.json`，鉴权白名单与三条测试同步。
- **构建脚本改名**：`build_competition_docx.py` → `build_docx.py`，`build_submission_form.py` → `build_form_pdf.py`。

### Fixed

- **链接守卫只检查被跟踪的 markdown**：此前它把本地过程稿也扫进去，而且 `docs/exports` 的跳过条件
  用 `in parts` 比较多段路径，永远匹配不上。现在有 git 时按 `git ls-files` 取文件，
  解包 sdist（无 git）时退回遍历。
- **治理闭环演示不把参数取值打到终端**：参数属于审计数据，只写进 JSONL trace，
  终端只报"几项"与工具名（CodeQL `py/clear-text-logging-sensitive-data`）。

## [v0.22.1] - 2026-10-25

本版收口 **v0.22.0 之后审计整改的第二批次**（PR #102，合并提交 `16d3b82`）：13 个提交全部是
修复 / 测试 / 文档 / CI，**没有新增对外能力，也没有破坏性改动**——可以直接升级。
同时补上两处"上一版遗留、只有把包发出去才会暴露"的缺口：Release 资产缺 provenance、sdist 包内文档断链。

### Added

- **Release 开始上传 provenance 资产**（`publish.yml`）：Scorecard 的 Signed-Releases 检查要求
  attestation 作为 **Release 资产**上传，只带 `.sigstore` / `.sig` / `.pem` 不算。
  现在构建后导出 `dist/provenance/*.intoto.jsonl` 并加入 Release 的 `files:`。
  v0.22.0 的 14 个资产里没有它，正是那个缺口——**本版起生效**。
- **sidecar 服务端端到端测试**（`tests/test_sidecar_server.py`，29 例）：覆盖服务端装配与失败边界
  （默认模型工厂拒绝未知 provider、策略默认与校验、工具接入的依赖缺失与桥接清理、initialize 入参校验、
  关闭后拒绝调用、取消与审批超时的拒绝语义、宿主失联与待处理请求清理、stdio 逐行处理与读失败、
  退出码与模块入口）。全离线：不联网、不需要 API Key，MCP / OpenAPI 用假模块注入。
  sidecar 五个模块覆盖率由 0% / 74% / 89% **全部升到 100%**。

### Fixed

- **Python 3.11 上外部取消会被悄悄吞掉（CI 在 3.11 变红）**：
  3.11 的 `asyncio.wait_for` 会把协程包进一个额外 Task，再等一个内部 waiter；
  外层取消恰好落在"内层协程同时完成"时，它走 `if fut.done(): return fut.result()`
  **把 `CancelledError` 吞掉并返回结果**。内核恰恰靠取消生效：
  `shell/employee` 委派里"一个子任务失败要中断另一个子任务"就是 `task.cancel()`——
  一旦被吞，子任务就没人中断，工具会一直跑下去，整次委派挂死。
  实测在 3.11 上 `tests/test_shell_employee.py::test_delegate_exception_cancels_and_joins_siblings`
  稳定超时挂死，3.12/3.13 正常。
  现把内核与 `shell/employee` 路径上所有"包住内联 await 的超时"统一换成
  `asyncio.timeout`（3.11+ 都有，且与 3.12+ 的 `wait_for` 是同一套语义）：
  `kernel/loop.py`（模型调用入口）、`kernel/router.py`（路由分类）、
  `kernel/compaction.py`（上下文压缩）、`kernel/state.py`（宿主状态）、
  `llm/fallback.py`（多模型降级）、`tools/executor.py`（异步 handler 与组合工具）、
  `sidecar/channel.py` 与 `sidecar/server.py`（审批 / 追问往返）、
  `batteries/fastapi_server/readiness.py`（就绪探针）、`shell/employee/worker.py`
  （子员工墙钟超时）。宿主外壳（桌面 / 示例）不在内核取消契约内，保持原样。
  新增回归用例 `tests/test_model_timeout.py::test_cancel_at_model_completion_boundary_is_not_swallowed`
  把取消精确打在"模型调用刚返回"的竞态窗口上，断言取消没有被吞、在飞的工具被真正中断；
  已实测去掉内核改动后该用例必红。

- **工具执行语义三处收紧**：模型吐出坏 JSON 时按 `{}` 处理，等于把一次模型错误升级成一次"成功"的
  空参调用——Anthropic 与 OpenAI 兼容层现在在 parse 边界直接拒绝；幂等缓存命中原本直接回放，等于把
  首次调用时的权限当成"现在仍然成立"——缓存条目现在记录首次调用的权限依赖（含组合工具的嵌套子调用），
  重放前逐个复核当前准入，**只复核、不审批、不执行工具**；组合工具把剩余 timeout 预算下发给每个步骤
  （同步 handler 默认在事件循环线程里直接跑，外包超时在它返回前根本没机会触发，声明的 50ms 会等同
  0.4s 跑完还报成功）。
- **上下文三处顺序错位**：启用摘要压缩时 `Context.add` 仍在追加时就硬裁剪，旧轮在压缩器看到它之前
  就被删掉，压缩永远等不到高水位——改为压缩启用时推迟裁剪，每次模型调用前"先压缩、再硬裁剪兜底"；
  重规划把尚未回齐结果的工具调用一并带进规划请求——当前批次只保留已完成的工具配对；`AgentCore`
  关闭后再 `astream` 会静默走空循环，宿主看到的是"没有事件"而不是"内核已关"——现在直接抛
  `RuntimeError`，且关闭检查移进实例锁内，避免与运行中的任务交错。
- **子员工权限逐层叠加，失败如实回流**：过去"权限只能收窄"只对白名单本身生效，父级 DENY / ASK 的
  工具仍可能被子员工执行——现在子员工策略叠加父级当前 `executor.policy`，两层都 ALLOW 才执行，
  父级运行期撤权立即生效；准入拒绝可能不发事件，失败改按执行器真实返回值记录（失败列表同时并入
  unknown 与实际失败名）；`delegate` 传可选 `subject`，父任务失败时取消所有 pending 子任务；
  `tools` 用 `is not None` 判断，空列表不再被当成"未指定"。
- **SQLite 备份 / 恢复改为暂存校验后原子替换**：原来直接往目标路径写，写坏了就是坏库留在原地，
  备份文件损坏时还会先删掉目标的 `-wal` / `-shm` 再发现源不能用。现在两条路径都是
  同目录暂存 → 校验 → 关闭所有连接 → 单文件 `os.replace` 原子替换；自备份与源侧车别名一律拒绝，
  有侧车时拒绝操作（不提供多文件崩溃原子性，边界写进 docstring）。
- **OpenAPI 路径拒绝独立点段**：`quote(..., safe="")` 会保留非保留字符 `.`，只编码挡不住点段规范化
  （httpx 会把 `/pets/../foo` 变成 `/foo`，实际调用偏离注册接口）。现在参数填完模板后检查最终路径的
  每一段，出现独立 `.` / `..` 段直接拒绝；不改服务器的解码行为。
- **冒烟脚本不再"打印即通过"**：`scripts/smoke_test.py` 以前只打印事件就算过，模型说了什么、
  工具调没调都不影响退出码。现在每个宿主必须有 `DONE`、至少一次成功的宿主业务工具调用，且不能出现
  `ERROR`、失败工具结果或预算终止；失败抛异常、CLI 非零退出，并始终关闭 Core。
- **桌面：DONE 是展示终态，不是生产者的最后动作**：内核的学习回灌排在 DONE 之后，收到 done 就
  `break` / `aclose` 会跳过这次回灌——改为停止上屏、继续耗尽事件流；收尾失败不覆盖已完成的终态。
  星云画布改四态统一 60fps（粒子数取设计稿同源的 260；预置色串 + `globalAlpha` 代替逐粒子 rgba；
  未吸积时跳过轨道三角函数），窗口隐藏或最小化时停帧。
- **发出去的包，文档链接全是断的**：sdist 白名单里没有 `ROADMAP.md`、部署手册与白皮书配图目录，而白皮书与部署文档链接到它们。
  对着 **PyPI 上已发布的 0.22.0 sdist** 实测：**包内断链 12 处**（白皮书 9 张赛事配图
  + 部署文档 2 处 + 白皮书引 ROADMAP 1 处）。
  这三处**都只含已被 git 跟踪的公开内容**（含个人信息的本地材料 / 更新邮件等过程稿
  全部未被跟踪，仍不进包），现补进白名单：**包内断链 12 → 0**。
- 新增 `tests/test_sdist_link_integrity.py`（19 例）把这件事钉住：
  仓库内所有 markdown 的相对链接必须可达；**进了包的文档，其链接目标也必须在包里**
  （否则包内必然断链）；另有一组断言守住白名单里的关键支点文件。
  已实测：把 `/ROADMAP.md` 从白名单摘掉，该守卫当场变红。

### Docs

- **CII 自证与门面口径**：`docs/openssf-best-practices-self-assessment.md` 刷新到实测数字
  （1 911 项 / 95.65% / 414 次提交 / 18 个 tag），并把"如何修改已有自评"改成正确入口
  （`/passing/edit`——站点并不存在 `/edit`）。**平台记录已于 2026-10-25 同步**：`maintained` 的版本号、
  `version_tags` 的示例 tag、`test_most` 的覆盖率，以及两条 `dynamic_analysis*` 由 Unmet 改 Met；
  回读公开 JSON 确认 `badge_level=passing`、Passing 完成度 100%。SECURITY 中英两份把默认策略说清楚
  （默认 Core 策略放行，宿主必须显式配置 policy 或声明 `approval.required`，才要求高危操作经批准；
  必需审批不能绕过策略拒绝，动态发现也不能绕过统一授权）；README 中英两份的桌面安装命令补 `--extra llm`，
  各自的"当前发布版本"标记同步到 v0.22.1。

## [v0.22.0] - 2026-10-24

本版是一次**全面体检后的整改批次**：把"实现已经比文档靠前 7 个版本"这件事掰回齐平，
并给最引以为傲的那条红线补上真正拦得住人的守门测试。

### Added

- **红线 1「内核本体零第三方硬依赖」补上全库守门测试**（`tests/test_kernel_dependencies.py`，12 例）：
  此前唯一的守卫是 `tests/test_mcp_bridge.py::test_no_toplevel_mcp_import_keeps_kernel_dependency_free`，
  它只检查 `integrations/mcp/client.py` **一个文件**，且整个模块顶部 `pytest.importorskip("mcp")`
  ——没装 `[mcp]` extra 的环境里整条守卫被跳过。实测把 `import httpx` 注入
  `src/yai_core/core.py` / `tools/executor.py` 顶层，全量 1 564 项测试**全绿**，红线被破坏而无人察觉。
  新测试用 AST 扫 `src/yai_core` 全量 102 个文件的**模块顶层** import（懒加载在函数体内，仍允许），
  并断言 `pyproject.toml` 的 `dependencies == []`；守卫自身有反证用例，确保注入即变红。

- **`CODE_TOOL_RETIRED` 从死事件变成真事件**：
  该事件类型定义在 `types.py`，白皮书把它列为对外可见的 20 类事件之一，
  `examples/runner_common.py` 也有对应的消费分支——但内核**从头到尾没发过**：
  `CodeToolManager.expire_stale()` 静默注销过期代码工具，不留任何痕迹。
  现在执行器在 run 内发现代码工具已过期时，除发 `TOOL_RESULT(ok=False)` 外，
  另发 `CODE_TOOL_RETIRED`（`data = {name, source: "code", reason: "expired"}`），
  宿主得以区分"工具被回收"与"工具调用失败"。新增 `tests/test_spi_surface.py::test_no_dead_event_types`
  把"每个事件类型都必须在 `src/` 里有真实发出点"钉成断言，杜绝同类死事件再出现。

- **sdist 自带的 `tests/` 在包内跑不起来**：
  `pyproject.toml` 的 sdist 白名单只含 `src/yai_core` 与 `tests`，不含 `shell/`、`examples/`、
  `scripts/`、`uv.lock`、`SECURITY.zh-CN.md`——而 `tests/` 里有 21 个模块依赖它们。
  实测从解包的 sdist 跑 `pytest --collect-only`：**21 个模块收集失败**（`ModuleNotFoundError: shell.desktop.host`
  等），只能收集到 1 276 项；`test_version_consistency.py`（读 `uv.lock`）与
  `test_security_policy_parity.py`（要 `SECURITY.zh-CN.md`）直接失败。
  现在白名单根锚定并补齐这些依赖，从 sdist 跑全量测试 **exit 0**。

- **sdist 白名单被 hatchling 的 gitignore 语义击穿**：
  `include` 里写无前导斜杠的 `"README.md"` 会匹配**任意层级**同名文件，
  已发布的 0.21.0 sdist 因此混入了 9 个白名单外的 README.md
  （`examples/*/README.md`、`models/README.md`、`fuzzing/README.md` 等）。
  现在所有模式改为 `/` 根锚定，并显式排除本机过程稿。

- **README 首屏授权措辞与真实默认值相反**：
  README 写「高危操作默认**先请求授权**」，而默认是 `auto_approve_tools=True`
  → `AllowlistPolicy(mode="allow_all")`，**所有工具调用静默放行**，也没有"高危"默认分级；
  授权卡只在宿主显式声明 `ApprovalPolicy` / ACC `approval_required` 时才出现。
  中英两份 README 均已改为如实描述默认策略与收紧方式。

- **依赖带两条已知 CVE**：`opentelemetry-api 1.44.0`（GHSA-xcq7-p3pf-p88h，无界 tracestate 解析，
  修复版 1.45.1）与 `filelock 4.0.6`（GHSA-567q-5qvw-pf92，修复版 4.2.1）。
  锁文件已升级到 `opentelemetry-api 1.45.1` 与 `filelock 4.2.4`，OSV 批量扫描 0 命中。

- **`anthropic` 可选依赖缺上界**：官方 SDK 1.x→2.x 是 breaking 区间，而本项目测试用假模块，
  CI 永远发现不了真实 SDK 的 API 漂移——`pip install yai-agent-core[anthropic]` 会拿到未测的 2.x。
  上界收紧为 `>=0.40,<2`，等真机验证过 2.x 再放开。

- **GitHub Release 正文退化成了 PR 流水账**：`publish.yml` 只有 `generate_release_notes: true`，
  从不读取 `docs/releases/<tag>.md`，仓库里写好的人话版发布说明（三条主线 / 升级指引 / 本版数字 /
  诚实边界）从未生效。现在发版时优先用 `docs/releases/<tag>.md` 作正文，文件不存在才回退自动生成。

### Added

- **三道新的守门测试**：
  - `tests/test_kernel_dependencies.py`（12 例）：内核零第三方硬依赖的全库 AST 扫描（见上）。
  - `tests/test_spi_surface.py`（7 例）：十个 SPI 契约的数量、形状、README 清单对应，
    以及"无死事件"断言。
  - `tests/test_version_consistency.py` 扩充：两份 README 的版本徽章必须与包版本同值
    （此前 `README.en.md` 徽章停在 v0.20.0、`README.md` 已是 v0.21.0，中英门面漂移而 CI 全绿），
    且 README 不得把旧版本标成"当前发布版本"。

### Changed

- **中英门面的事实与措辞校准**（README / README.en / docs/comparison(.en) / SECURITY(.zh-CN) /
  CONTRIBUTING(.en) / docs/architecture(.en) / docs/whitepaper）：
  版本徽章、版本路线（补齐 v0.15–v0.21）、示例宿主数（9→10）、测试数（1100+→1500+）、
  SECURITY 支持版本表（0.8.x→0.21.x/0.20.x）、SQLite schema 口径（v1→v3）、
  `achat` 签名、诊断类型数（8→9，补 `host_state_failed`）、能力表补"默认关闭"标注、
  竞品类别级的全称否定改为中性描述。
- **`docs/architecture.md` 补「v0.12–v0.21 新增机制」小节**（中英各一份）：
  多模态消息契约、manifest v2、schema 13 关键字、能力体检、ChangePreview + ExecutionReceipt、
  HostStateProvider、Sidecar、Office 文本抽取、ACC 执行提示真被执行。
- **讲义第 06 / 18 / 21 / 41 篇与源码重新对齐**：第 06 篇贴的系统提示词、`describe()` vs
  `catalog_text()`、`max_iters` 透传、`_model_chat` 与 `executor.execute(..., call_id=…)`
  全部换成当前源码写法，行号锚点改为按函数名定位；第 18 篇补 `CODE_TOOL_RETIRED` 的两条回收路径；
  第 21 篇补 `to_manifest_dict` 的 `version` 分支；第 41 篇补 v0.12 起的 `_event_preview` 脱敏闸。
- **Dependabot 的 pip 段加分组**并把 `open-pull-requests-limit` 从 3 提到 8：此前锁文件 90+ 个包
  却每周最多 3 个 PR 且无分组，93 个包永远追不上。

## [v0.21.0] - 2026-10-24

### Added

- **Office 文档文本抽取 Battery（`batteries/office_text.py`，新 `[office]` extra）**：
  docx / xlsx / pptx 走标准库 `zipfile` + `xml.etree`（零依赖），PDF 走可选 pypdf
  （BSD-3、纯 Python、0.4 MB，懒加载）。安全闸三条有测试钉住：只读白名单 ZIP 成员
  且逐成员流式限量（防 zip 炸弹）、全量缓冲区拒绝 `<!DOCTYPE` / `<!ENTITY`（实体
  攻击定义通道整条封死）、结果截断带 `…[truncated]`。边界如实声明：文本级抽取，
  版面/样式/公式/数字单元格不进文本层。CI 矩阵同步装 `--extra office`。

### Changed

- **星云动效对齐设计稿（web 引擎 → QML 全量移植）**：`NebulaOrbCanvas.qml`
  补齐设计稿 `nebula_engine.js` 的渲染特征——四色调色板（深橙→琥珀→象牙白）、
  核心径向光晕、虚线旋转轨道、顺时针拖尾弧、行星球三段渐变与暗环、呼吸透明度、
  纵向波动、超新星粒子爆发与双冲击波、坍缩蓄力与延迟爆发、冻结锁定虚线环，
  渐变底移回 Canvas 并以 `ctx.clip()` 裁进球/胶囊壳。修正一个真实缺陷：
  漂移相位原先按帧推进（硬编码 0.016），空闲降帧后速度只剩设计稿的 1/3，
  现全部按真实 dt 积分——降帧只损失流畅度、不损失速度。实测（harness 抓帧
  对照 web 演示）：粒子数 120→260 对 CPU 无影响，帧率才是成本杠杆
  （空闲 20fps≈10%、30fps≈14%、60fps≈19% 单核），故取 260 粒 + 空闲 30fps +
  工作 60fps；旧口径"120 粒 20fps 占 6.2%"已在讲义 22 订正。

### Fixed

- **测试与本地工具的安全卫生批量加固**（同时消除静态扫描器的 24 项误报源头）：
  新增 `tests/_local_http.py`（环回 fail-closed + `http.client` 显式 host/port），
  五个测试文件的本地 HTTP helper 收编至此；假凭据改环境注入（`YAI_TEST_API_KEY` /
  `YAI_TEST_REDACTION_KEY`）；`shell/desktop/crm_client.py` 构造期强制 http + 环回；
  发布脚本的 `http_json` 加 https 官方域白名单并修复一处
  latent `union-attr`；`docs/exports/render_mermaid.py` 改常量 host 直连 kroki.io。
  `fuzzing/fuzz_schema.py` 标量不变量改为 `is` 恒等判（`json.loads("NaN")` 的
  `float('nan')` 与自身 `==` 恒 False，fuzzer 实测撞出）。

## [v0.20.0] - 2026-10-24

### Changed

- **桌面壳视觉重构（Optical Obsidian / Braun Signal Orange）**：合并新的
  `Theme.qml`（球 52、胶囊 440×52、面板 440×536、26px 面板圆角、信号橙），
  保留原 QML 仍依赖的字体/时长/状态 token；新增 `NebulaOrbCanvas.qml`，
  球、胶囊与面板顶栏共用 idle / working / complete / frozen 四态。
  为常驻桌面控成本：空闲 20fps、工作 60fps、冻结停画，实测待命约单核 6.2%。
  胶囊改为 `01/SALES + 活动状态 + OPEN + 收起箭头`；面板改为
  `YAI.CORE · 模型 + VAULT/MEM + 编号标签 + HITL.GATE + TRACE + RUN` 结构；
  VAULT/MEM 改为全屏面板并可直接互切。壳层改为不透明，避免桌面其他窗口文字
  透进对话。`docs/assets` 的球/面板截图已按真实运行态更新，并新增胶囊、HITL、
  VAULT、MEM 四张截图；压缩包里的网页预览依赖 CDN 且为独立 mock，未并入仓库。

## [v0.19.0] - 2026-10-24

### Added

- **销售 CRM 的“当前选中客户”接入 HostStateProvider**：host_e 网页新增
  `POST/GET /api/selection` 与客户表“选中”按钮；`Workbench._agent_state()` 只把
  已选客户（姓名/公司/等级/状态）瞬态注入网页内置 Agent。桌面销售通过带令牌的
  `GET /api/agent/state` 读取同一状态（`HostECrmClient._selection` 私有方法，不会
  被自动注册成工具），以 `sales_selection` 注册；客户删除后自动清空。新增跨进程
  与网页端离线用例。

## [v0.18.0] - 2026-10-24

### Added

- **桌面陪伴专员接入 HostStateProvider**：`shell/desktop/host.py` 的 `_live_factory`
  支持 `state_provider`；陪伴专员每次任务注册 `companion` 状态源（最近三条心情 +
  总数），模型调用前瞬态注入，不写入历史/记忆。新增
  `tests/test_desktop_companion_state.py`，走真实 live factory 验证。

## [v0.17.0] - 2026-10-24

### Added

- **Node 宿主样例与 CI sidecar 强化**：新增 `examples/host_l_sidecar_node/`，零依赖
  Node 进程真实拉起 `python -m yai_core.sidecar`，工具由 Node 自己的 OpenAPI 服务
  暴露、模型走本地 mock，全程离线；演示审批反向请求与 `done` 事件。CI 新增
  `sidecar (mcp + llm)` job（含 Node 样例），不再跳过 MCP/stdio 真实路径。
- **HostStateProvider 接入真实宿主**：`examples/host_c_companion/run.py` 的
  `build_core()` 注册 `companion` 状态源，当前心情/能量/羁绊每轮瞬态注入，
  不需要先调工具、也不写入历史；新增离线测试钉住这条链路。

## [v0.16.0] - 2026-10-24

### Added

- **语言无关宿主接入（Sidecar 会话控制协议）**：新增 `python -m yai_core.sidecar`，
  在 stdin/stdout 上用 NDJSON + JSON-RPC 2.0 承载 Agent 会话：`initialize`、
  `agent/run`（流式 `agent/event`）、`agent/cancel`、`ping`、`shutdown`；审批与
  澄清由 sidecar 反向发 `approval/request` / `clarify/request`，超时按拒绝/空回答
  处理。**工具面仍走 MCP / OpenAPI**，协议里没有 `tool/call`，不新增第二套工具
  调用通道。单进程单任务；工具接入失败会关闭已建 core；运行期异常只发
  `agent/error`，不杀进程。默认真实模型走 `[llm]` extra，测试可注入模型与工具。

## [v0.15.0] - 2026-10-24

### Added

- **CI 增加留出集评测与 Windows junction 竞态测试**：`router benchmark` job 在原有
  主曲线之后同时跑 `--holdout`（冻结学习泛化）；Windows 上新增 junction 父目录
  替换测试，证明句柄真实路径复核在没有 `O_NOFOLLOW` 的平台也确实生效（此前该
  竞态测试在 Windows 只 skip）。
- **宿主实时状态（现状锚）**：`AgentCore.register_state_provider()` /
  `unregister_state_provider()` 允许宿主注册只读快照回调。内核在**每次主模型调用前**
  重新取一次状态（工具名聚焦的窄调用除外），作为瞬态 system 块插在基础提示之后、
  历史之前；不写入
  `Context.messages`，因此不进压缩摘要、不进长期记忆、下一轮不沿用旧值。
  单个 provider 默认 2000 字符、总量 4000 字符、异步 provider 2 秒上限；
  provider 抛错/超时只发 `HOST_STATE_FAILED` 诊断并跳过，其余状态照常注入。
  状态变化（或首次注入）时发 `HOST_STATE` 事件，载荷只含 provider 名、字符数、
  截断标记与指纹，**不含状态内容**。状态块明确标注"是数据不是指令"，且只能由
  宿主注册——模型参数、工具结果与用户消息都改不了它。
- **路由 benchmark 的留出集 / 冻结学习评测**：新增 `split_tasks()` 与
  `python scripts/benchmark_router.py --holdout`。按策略分层切出训练/留出任务，
  只在训练集上学习，**冻结参数后**在没见过的任务上评测，并与规则基线、Oracle
  对比；硬校验要求冻结后的成功率显著高于规则，否则非零退出。原有主曲线保留
  （同批任务边学边测），两份结果分别写入 `router_bench.json` 与
  `router_holdout.json`。

### Fixed

- **host_e 预览 token 的判定范围收窄到相关状态**：此前 token 哈希整个 CRM 快照，
  审批卡开着时去隔壁补一条待办、给别的客户记跟进，都会让这次审批被判 `preview_stale`
  ——而它其实与本次写操作无关。现在按工具圈定依据（目标客户及其跟进、目标商机、
  待办集合、订单表决定的新订单号等），无关变更不再误伤；同一目标的变化仍会失效。
  圈定范围由宿主负责，真实系统应换成自己的 revision/事务快照。
- **幂等重放不再绕过权限收紧与参数校验**：重放前仍会跑一次策略判定，当前策略
  为 DENY 时拒绝（缓存不是后门）；缓存条目绑定参数指纹，同一个幂等键换了参数
  会返回 `idempotency_conflict`（`not_started`）而不是把上次结果当成这次的。
  重放本身仍不重复执行、不重新弹人工审批。
- **父目录链接替换的竞态窗口进一步收窄**：`O_NOFOLLOW` 只保护最后一段，父目录在
  校验后被换成链接仍能把读取/落盘引到别处（Windows 连 `O_NOFOLLOW` 都没有）。
  读路径在 `open` 后用句柄真实路径复核（Linux `/proc/self/fd`、macOS `F_GETPATH`、
  Windows `GetFinalPathNameByHandleW`）；写路径在 `replace`/`link` 前再 `realpath`
  一次。平台取不到句柄路径时按未验证降级，不制造新的失败面，也不假装验证过。

## [v0.14.0] - 2026-10-23

### Added

- **多模态内核核对脚本**：新增 `scripts/check_multimodal_core.py`，把图片放进
  `AgentCore` 会话历史并走完整内核链路；与只核对出站格式的
  `check_multimodal_wire.py` 分工不同。默认使用 `docs/assets/desktop-panel.png`。
- **`strict_direct`（opt-in）**：`CoreConfig` / `AgentCore` 新增布尔开关，默认 `False`
  保持旧兼容。开启后，`direct` 策略下模型若仍返回 `tool_calls`，内核不执行任何工具，
  发 `ERROR` 并保留模型的文字回答；没有文字时给出诚实收尾。之所以不默认开启：现有
  多个宿主依赖"路由判 direct、模型自行补调工具"的兜底，默认收紧会直接破坏它们。
- **能力接入体检**：新增 `check_tool_specs()` 与 `AgentCore.capability_health()`。
  不执行 handler，静态列出未实现的 schema 键、非法/未执行的频控窗口、找不到路径的
  审批条件、组合步骤缺工具、代码工具缺沙箱等问题；`CapabilityHealth.to_text()` /
  `to_dict()` 可直接给宿主展示或落日志。
- **schema 兼容性回归**：遇到 `anyOf` / `oneOf` / `$ref` 这类可能放宽取值域的键时，
  入参校验整段跳过该子 schema（能力体检会标“未校验”），避免把合法调用误拒；
  `additionalProperties` 等只收紧的键不影响顶层类型校验。
- **可信变更预览**：`ChangePreview` + `ToolGovernance.preview`（宿主函数，不随清单
  导出）。审批前宿主生成 `summary / diff / target / token`，随 `PERMISSION_ASKED`、
  `Channel.confirm` 上下文与 `TOOL_CALL` 一起展示；确认后执行前重新生成并比较 token，
  变了就 `preview_stale` 拒绝执行，生成失败则 `preview_failed` 失败关闭。组合工具
  自己的审批走同一规则；CLI/桌面/QML 已接入展示。host_e 示例已接真实 preview
  provider：写工具审批卡展示从当前 CRM 数据渲染的 diff，token 同样用于执行前复核。
  host_e 新增只读 `/api/agent/preview`，桌面销售专员通过同一端点在确认卡展示真实
  diff；桌面端不再复制一套业务预览规则，执行前 token 复核仍由内核完成。
- **执行收据与进程内幂等**：新增 `ExecutionReceipt`（`not_started` / `succeeded` /
  `failed` / `unknown` / `partial`），随 `TOOL_RESULT` 事件流出；`call_id` 从模型
  工具调用透传，组合步骤用“父 id:下标”。超时/同步放弃/handler 异常/沙箱异常记
  `unknown`，组合前几步成功后续失败记 `partial`，不会被误当成可安全重试。
  新增宿主幂等键 `ToolGovernance.idempotency_key`（不随清单导出）；提供非空 key 时，
  执行器进程内缓存结果（默认 256 条），同一 key 重试不再进入 handler，事件标
  `replayed=True`。跨进程/多实例去重仍由宿主负责。

### Fixed

- **桌面销售的内部 preview 方法不再被注册成工具**：`AgentCore.auto()` 会内省公开
  方法，`HostECrmClient.preview` 曾因此出现在模型工具列表里，模型失败后会去调用
  一个并不存在的业务工具。现改为 `_preview`，审批链路仍然使用它，但模型不可见。
- **审批预览失效后立即终止本次任务**：此前 `preview_stale` 被当成普通工具失败，
  模型会继续调 `list_customers` / `preview` / `request_capability` 试图补救，
  在免费模型上很容易触发 429。现在失效即收尾，不再发起后续模型调用。
- **模型把工具调用写成正文时不再伪装成最终答案**：部分兼容模型会把
  `<tool_call>` / `<arg_key>` / `<function_call>` 写进普通 content，而不是返回结构化
  `tool_calls`。内核现在识别这种形状后失败关闭：不执行、不把原始 XML 放进最终答复，
  发 `ERROR(stage=model_protocol)` 并给出可读说明，避免用户看到一个"假工具调用"气泡。
- **执行约束覆盖所有真实路径**：组合工具此前在预算、频率上限与声明超时之前提前分流，
  导致"一次组合 = 多次业务操作"绕过 `max_tool_calls`、组合声明的 `rate_limit` 不生效、
  `timeout_ms` 只管到单个步骤；现在预算按组合的每个真实步骤计数，频率闸在组合分支之前，
  工作流级 `timeout_ms` 覆盖整条组合（超时文案明确"已完成步骤不会回滚"）。代码工具的
  `timeout_ms` 也会收紧传给宿主沙箱的超时；同步函数返回 awaitable 时，剩余超时额度仍然生效。
- **入参 schema 校验成为所有调用的共同下限**：此前只有挂了条件审批的调用才会校验取值域，
  普通工具声明 `minimum` 后传越界值仍会执行。现在每次调用都校验已实现的 schema 子集；
  有条件审批时仍保留更具体的"审批闸拒绝"话术。
- **`format: date` 不再放过尾部垃圾**：此前只检查前 10 位，`2026-10-23junk` 也会通过；
  现在整串必须是 `YYYY-MM-DD`。
- **泛型容器注解不再退化成 `string`**：`list[dict[str, Any]]`、`dict[str, int]`、`set` /
  `tuple` / `Literal` 等注解现在递归生成数组、对象、枚举 schema。此前所有泛型容器都被
  映射成 `string`，模型按声明传入数组时会被校验/审批误判非法——这是宿主写工具（如
  Actual 的 `categorize_transactions`）在严格校验下暴露出的真实缺陷。
- **组合版本快照真正不可变**：`save` 深拷贝步骤，`to_manifest_steps` / `restore` 返回
  独立副本；版本记录同时保存 `input_schema` 与可序列化治理（脱敏、审批、执行提示），
  恢复时不再丢失这些约束。`gate` 是函数，按设计不进快照、也不会被凭数据重建。
  `latest` 改为记录**最近一次 save**——覆盖旧版本不会改变字典插入顺序，此前重存 `v1`
  后仍会返回 `v2`。
- **OpenAPI `nullable` 归一为标准 JSON Schema**：`nullable: true` 清洗时转成
  `type: [T, "null"]`（枚举补 `null`），不再被静默丢掉；否则全局入参校验会把
  可选参数的 `null` 误判为类型不符。
- **审批卡标题不再把高风险读操作叫成写操作**：高风险且无参数级审批时显示
  “高影响操作待确认”，不再固定写“写操作”。
- **评测不再把"工具失败但声称拿到凭证"判成通过**：新增 `failed_must_tools`，工具召回也
  改按**成功结果**计算；只要任务期望工具或实际发生过工具调用，最终答复里的凭证就必须能
  在成功结果里找到。纯 direct 且不期望工具的任务仍跳过出处核验。
- **多模态降级与预算口径对齐**：故障切换包装按实际尝试的子后端逐次降级（视觉后端收
  内容数组，纯文本后端收占位字符串），不再在包装层提前丢图；上下文硬裁剪与摘要压缩
  选区也改用与 `total_chars` 相同的 `message_cost`，图片不会因只按 `content` 扣减而被
  多删，长文本片段也能触发摘要。
- **Token 预算不再重复累计**：后端只给 prompt/completion、不给 total 时，改用本次增量
  兜底（10、20、30），不会把前几次 token 反复相加（10、30、60）而提前熔断。
- **路由反馈记回最初决策**：澄清链中原任务先判 `clarify`、补充信息后才判 `react` 时，
  反馈现在奖励最初那次 `clarify`，而不是把功劳记到后续策略上。
- **桌面审批展示**：`QtChannel` 透传 `approval_required`，参数级审批不再被标题误判成
  "新能力授权请求"；`Panel.qml` 对对象/数组参数使用 JSON 渲染，不再显示 `[object Object]`。
- **通道类型契约拆分**：基础 `Channel` 恢复两参数 `confirm`，审批上下文进入可选的
  `ApprovalContextChannel` 扩展；旧的两参数通道在运行时与静态类型层面都合法，不再依赖
  `cast` 遮掩不兼容。
- **host_k 资源读取有界**：不再"先 stat 再无界 `read_text`"，改为最多读取 `max_bytes+1`
  并检查实际读取量；文件在检查后增长也突破不了上限。

### Changed

- `ToolExecutor.execute` 新增可选 `budget` 参数；普通执行错误仍回灌给模型，唯一向上冒泡的
  是任务级 `BudgetExceeded`（硬熔断必须由 Agent Loop 统一收尾）。
- `Context` 新增 `message_cost()`，上下文总量、硬裁剪与摘要压缩共用同一成本函数。

## [v0.13.0] - 2026-10-22

本版主线是**把"还差点"的五处补到能上线**，另加一批**从外部契约到内核原生**的
治理收紧。全部分支的默认行为不变；新增能力一律 opt-in。

### Added

- **入参校验的 schema 子集从 4 个关键字扩到 13 个**：此前只校验结构（`type` / `required` / `properties` / `items`），取值域一律不看——声明写 `minimum: 100`，调用传 `1` 照样通过。现在支持 `enum` / `pattern` / `minimum` / `maximum` / `minLength` / `maxLength` / `minItems` / `maxItems` / `format`（只实现 `date-time` 与 `date`）。`enum` 用严格 JSON 相等（`"1" != 1`），`pattern` 用 `re.search`，模式本身非法时跳过而不拒绝调用；仍未实现的键（`anyOf` / `$ref` / `additionalProperties` / `exclusive*` / `multipleOf` / `uniqueItems` / `const`）**记录在册**——多实现几个键就多几分"看起来很严"，但没实现的键会让人误以为已经校验过。
- **多模态的三处落地**：① 图片按**真实成本口径**计入上下文预算（此前按占位文本长度计，等于"图片不占预算"），新增 `IMAGE_CONTEXT_COST_CHARS`（保守代理，宿主可调）；② 新增可选能力标记 `yai_multimodal`（沿用 `yai_live_router` 的约定：类属性、不进协议强制结构），**没声明的后端按"不支持多模态"处理**，片段自动降级成纯文本占位（不丢信息、不发后端不认的数组）；③ 新增示例宿主 `examples/host_k_multimodal/`：**模型只能选句柄，宿主负责解析、判作用域、判有效期、判大小**（授权与生命周期都在宿主，内核不抓取任何外部资源）。
- **组合工具的版本管理**：新增 `CompositeWorkflowStore`（纯数据、标准库）——`save` 存步骤快照、`versions`/`latest`/`get`、`diff` 按**位置**比对（工作流顺序敏感）、`restore` 还原成可注册的 `ToolSpec`。版本号是 `ToolSpec.version`（可选字段），随能力清单跨进程走，老读者不认识就当没有。
- **跨层对抗性证据**：补到三层——spec 里的 `guidance` 默认进不了模型提示、即使打开也改不动闸门判定；模型参数里的 `subject` 不算可信主体（主体只来自宿主运行期上下文）；模型参数里的 `approval`/`governance` 不被采纳；清单里的 `gate` 不会被重建。

### Fixed

- **符号链接竞态窗口在 POSIX 上关进内核层**：读路径改用 `O_NOFOLLOW` 打开（新增 `spi/resources.py::open_no_follow`），"路径校验 → 真正打开"之间被换成链接时内核直接拒绝（`ELOOP`）。**平台差异如实写明**：`O_NOFOLLOW` 是 POSIX 扩展，**Windows 没有**——那边窗口只能收窄、不能根除（对应测试在 Windows 上是 skip，不是"通过"）。
- **清单新增字段不改变安全语义**：`ToolSpec.version` 等新增字段与 `governance` 声明一起进清单时，旧读者会**静默忽略**不认识的键——这与"清单治理语义与版本号绑定"的既有约定一致（新字段不改变安全语义），故不升版本号。

### Changed

- **清除杭州相关材料与引用**：早期过程材料目录已从仓库删除，示例演示数据、演示脚本、`.gitignore` 规则与发布说明里的相关引用一并清理；GitHub 上对应的 Release 也已删除（**tag 仍留在 git 历史里**——历史不可改，也不该改）。

## [v0.12.0] - 2026-10-22

本版主线是**把"看起来有护栏、实际没生效"的那几处补上**，顺带把外部能力契约的
**执行提示**从"只解析不执行"变成真约束，并给消息契约加上**可选的多模态**扩展。
三批内容彼此独立，都不改变既有宿主的默认行为。

### Added

- **多模态消息契约（可选扩展）**：`ChatMessage` 新增 `parts: list[ContentPart] | None`，三种片段——`TextPart` / `ImagePart` / `ResourcePart`。**默认 None = 纯文本消息，`to_llm_dict()` 输出与旧版逐字节相同**（有测试钉住），老宿主零改动。
  - **图片有两条路径**：`data`（宿主在信任边界上解析好的内联 base64）与 `ref`（宿主签发的引用）。只有内联的那条模型才真的看得到；只有引用的会渲染成"明说模型看不到"的文字占位——**绝不静默丢图**（模型在缺资料时照常回答，看起来成功、其实答错）。`ResourcePart` 同理：内核**不替宿主取**任何外部资源（那是授权与生命周期问题）。
  - **内联图片有硬上限**（5 MB）：图片整块进模型请求，超大载荷既烧钱又可能把请求撑爆，超限在构造时就拒。
  - **出站是内容数组**（OpenAI 兼容的原生形态，`OpenAICompatProvider` 直通零改动）；`AnthropicProvider` 现在**真的翻译**图片块（此前是压平成空文本，图片确实看不见）。
  - **落盘只存引用与元数据，不存图片字节**（`history` 新增 `parts` 列，schema v2→v3）：历史长期驻留，不该塞进一堆没人看得懂的二进制。代价是读回的图片没有内联数据、渲染成"看不到"的占位——取舍写在脸上。
  - **降级与摘要不丢信息**：`parts_to_text()` 把片段渲染成纯文本，供不支持多模态的模型与上下文摘要使用（摘要器是纯文本模型，图片引用若被丢掉，"用户发来截图，结论是…"这条事实会凭空消失）；`Context.total_chars` 也把片段计入预算。
  - **迁移逐级进行**（v1→v2→v3）并照顾两种老库：表不存在（手工造过的残缺库）→ 按 v3 建表；表在但缺列 → `ALTER TABLE` 补列。此前单次 if/elif 迁移链会把老库停在中间版本上。
  - 新增 `scripts/check_multimodal_wire.py`：离线打印内核会发出的**确切请求体**（不需要 Key），给了 Key 可 `--live` 真机核对一次。讲义新增第 48 篇。
- **ACC 的执行提示真正被执行器强制**（`execution.timeout_ms` / `execution.rate_limit` 此前**只解析、不执行**——声明里写着"每次 10 秒""每分钟 30 次"，内核却当它不存在）：
  - 新增原生数据 `ToolGovernance.timeout_ms` 与 `ToolGovernance.rate_limit`（`RateLimitHint` + 窗口串解析 30s/1m/2h/1d），都是数据，随能力清单跨进程走。
  - 执行器真的强制：超时与全局 `handler_timeout` **取更严的一档**（治理数据只能加严）；频率上限用进程内滑动窗口，超限的调用**不进 handler**、也不发 `TOOL_CALL`，且**在授权之前**判定（被频率拦下的调用既不该先问人要批准，也不该留下"发生过"的审计记录）。
  - ACC 侧只做"一跳"翻译（声明 → 原生数据），判定逻辑仍只有内核这一份。
  - **对抗性证据**：工具参数里塞 `timeout_ms` / `rate_limit` / `governance` **改不动**任何治理结论——治理只来自注册时挂在工具上的声明，参数是模型产物。
- **审批上下文随 `confirm()` 一起递给通道**：`Channel.confirm` 多了**可选**第三参数 `context`，带 `reason / prompt / risk / scope / target`；通道**自己声明**愿意收（`yai_approval_context_aware = True`）才会被传入。为什么非走这条参数：`authorize_tool_call` 必须先把"批不批"拿到手才返回，而 `PERMISSION_ASKED` 是它返回之后才由 `AgentLoop` 统一 yield 的——**卡片弹出的那一刻，那条事件还在半路上**，界面读不到。于是审批人此前只能看到"工具名（k=v）"，内核算了一年的落点与风险一个字都没露出来。
  - **既有实现零改动**：没声明旗标的 CLI / HTTP / Flutter / 宿主自写通道，仍只收到两个位置参数；用能力旗标而不是探签名——宿主的 `**kwargs` 会把"能收"猜错（与策略侧 `yai_subject_aware` 同一约定）。
  - **上下文里不许有未脱敏的值**：只带工具名与描述性字段，`arguments` 仍是按声明脱敏的对外副本；宿主自己写在 `prompt` 模板里的占位符除外（那是宿主的决定，内核不替它扩散别的字段）。有值才带：策略单纯 ASK 时上下文就只有 `{"tool": ...}`。
  - 内置 `CliChannel` 与桌面壳 `QtChannel` 都接上了：终端按 y 之前会先打印"理由 / 落点 / 风险 / 作用域"；灵动岛确认卡改成**先说为什么问、再说动哪个资源、最后才是参数串**，标题按 `approval_required` / `risk` 分档（参数级审批 / 高影响写操作待确认 / 新能力授权请求）。
  - 9 例测试（`tests/test_approval_context_channel.py`），含一条**时序探针**：`confirm` 被调用时通道已收到的事件数为 0——这条就是"为什么必须走参数"的证据；若哪天有人把界面改成从事件流读落点，卡片会安静地拿到空值。另含老通道只收两参（含内置 `CollectChannel`）、字段不多带、脱敏不透传三条边界。讲义第 43 篇新增第 5 节。

### Fixed


- **外来能力清单的审批条件路径在导入期就与 `input_schema` 交叉校验**。此前拼错的参数（如 `totl` 其实是 `amount`）能顺利导入，随后**每一次调用**都被判"参数不合法"，工具 100% 不可用——而错误发生在别的进程里，排查成本极高（本地看着像"工具坏了"，远端日志只有一行拒绝理由）。现在导入期就拒，并列出"已声明的参数"清单。**这是既有契约的收紧**（此前清单刻意不校验路径，因为 schema 可能后绑定）：只在 schema 带 `properties` 时才校验，没有可校验 schema 的（如尚未绑定后端的 native 工具）仍放行，交给运行期失败关闭。
- **ACC 的"要求可信主体"不再在注册期隐藏能力**，强制点统一交给**执行期**的能力闸。主体是宿主在**运行期**注入的，注册期无从知道这次运行会不会有主体；用一个静态布尔提前隐藏，等于让宿主注入的主体在注册期就失效（能力缺失却看不出为什么）。现在这类能力照样暴露（模型看得见），但没有可信主体的那次调用会被能力闸当场拒绝，并在诊断里说清"当前宿主未声明能提供主体"。
- **ACC 声明里的 `guidance` 默认不再进入给模型看的工具描述**。guidance 的来源是 OpenAPI 描述，而描述常常来自第三方 API——一份被污染的 spec 就能借它往模型提示里塞任意指令文本。声明原文仍完整保留在 `ToolSpec.governance.acc` 里（宿主要展示自己取），宿主显式打开 `include_guidance=True` 才喂给模型，打开时仍先净化（去控制字符 / 零宽 / 双向覆盖符，净化不改写声明本身的意思）。
- **参数级审批的数组内嵌路径曾"永远不命中"，护栏等于没装**。根因是同一个概念写了两遍：`schema_at_path` 会下钻数组 `items`（`rows.amount` 认得），`value_at_path` 只认对象成员（取不到）——于是路径在 schema 侧"已声明"、取值侧"取不到"，条件**静默不命中**，每一次调用都放行。修法不是给取值侧打补丁，而是把路径语法**收敛到一处**（新增 `src/yai_core/paths.py` 作为唯一定义处）：`rows.0.amount`（显式下标）与 `rows.amount`（自动下钻）都能寻址，判定语义是 ANY（命中任一候选即命中，偏保守）。既有窄解析器 `value_at_path` 的历史契约逐字保留（它只寻址对象成员，有既有测试钉住），审批判定改走新的候选集解析器。**"值缺失"时各算子的口径刻意不变**：数值比较与 `contains` 失败关闭（拒绝），`==`/`in`/`exists` 不命中，`!=` 命中（"不等于期望安全值"却拿不到值本身就值得问人）。
- **作用域闸的空字符串前缀曾等于"整个根目录可写"**，与文档承诺的"失败关闭"直接矛盾——`prefixes=("",)` 这一个字符的笔误，就把根目录整片变成可写区。现在构造期就拒空串前缀并说人话，且 `_in_scope` 遇空前缀一律不放行（双保险）。
- **工具返回值未脱敏，密钥可绕开入参那一侧进入事件流**。入参侧（`PERMISSION_ASKED` / `channel.confirm` / `TOOL_CALL`）早就按声明脱敏，但 `TOOL_RESULT` 的 `preview` 是原文——而"把入参原样回显"是极常见的工具形状（调试工具、HTTP 客户端回显请求头）。执行器新增 `_event_preview()`：`audit_sensitive` → 结果整体折叠成"已折叠 + 字符数"；声明了 `sensitive` 路径且结果是对象 → 按同一批路径脱敏后再取前 200 字符；未声明 → 照常展示（内核不按字段名猜密钥，"未声明即不脱敏"是刻意边界）。**返回给模型的正文仍是真值**，业务行为不变。
- **文件列举三处小毛病**：断链符号链接会让整次列举失败（现在按 0 计，不中断）；条目大小走 `stat()` 会**跟随链接并泄漏根目录外文件的真实大小**（现在走 `lstat`）；条目数**恰好等于**上限时也会被标记成"已截断"（现在多取一条判定，边界不再误报）。`ResourceProvider.list()` 的契约（返回 ≤ 上限的条目）逐字不变，"是否截断"另走 `list_with_flag()`。
- **作用域闸会静默覆盖调用方声明的风险等级**（写操作一律改写成 `high`），审计链上的风险因此失真。现在取**更严的一档**：显式声明的 `risk` 不丢，写操作的默认 `high` 仍然兜底。
- **脱敏在深拷贝失败时退回原对象**，破坏"对外副本"不变式——通道改一下就等于改了执行参数。现在顶层必定是新 dict（逐值安全拷贝），不可拷贝的值保留原值但不再共享顶层。
- **能力闸未复核声明的 `enabled`**：宿主手工用 `enabled=false` 的声明造闸时，闸仍会按审批要求放行或拦截。闸层现在直接拒——暴露层过滤是另一道闸，闸层要有自己的兜底。
- **能力清单的治理语义与版本号脱钩**：v0.10 起清单里多了 `governance` 块（脱敏路径 / 宽口径审计 / 审批要求），而 `manifest_version` 仍是 1——**老读者版本号对得上，会静默忽略不认识的键**，跨进程导入后脱敏与审批要求全部消失，而新环境以为护栏还在。现在 `manifest_version` 升到 **2**（老读者拿到 v2 直接拒，失败关闭），本实现仍能读 v1 历史文件。
- 新增 `tests/test_security_regressions.py`（21 例，逐条钉住上面八条缺陷，含"修复前是什么样"的说明），讲义新增第 47 篇《安全加固第二批：把"静默失效"的那几处补上》。

## [v0.11.0] - 2026-10-16

本版主线是**把"外部契约的治理"翻译回 YAI 原生治理**：参数级审批不再需要先学会一个外部契约，准入判定算出来的风险 / 作用域 / 实际落点第一次真的进人工确认与审计事件，文件能力补上"根目录之内还要分块"的授权闸。路线来源是一份外部工程任务书（要求"移除 ACC 兼容层、建原生治理"）——逐条核实现状后的结论是：内核早就不依赖 ACC，真缺的是**判定归属**与**资源作用域授权**两件事，三步都已落地。

**没有破坏性变更**：`approval` 不声明即无行为，`filesystem_gate` 由宿主自己挂载，未治理工具的 `TOOL_CALL` / `PERMISSION_ASKED` 事件形状逐字不变；`import` 方式与 v0.10.0 一致。一处**审批话术**变化见 Fixed。

### Fixed

- **`from __future__ import annotations` 让工具 schema 骗人，进而把每次合法调用判成非法**。函数内省过去只查"注解对象"，而 PEP 563 下注解全是字符串：`amount: float` 被看成 `"float"`、查表不中，于是**所有参数一律退化成 JSON `string`**。表面看只是文档难看，实际后果是审批按绑定 schema 校验入参时"期望 string、实到 number"——合法调用全被拒在业务操作之前。`build_spec` 现在先走 `get_type_hints` 解析字符串注解，解不开（前向引用指向不存在的名字）才退回原来的"认不出就 string"兜底。
  - **为什么以前没人发现**：ACC 的既有测试都手交了正确的 schema 给 `build_gate`，绕过了 `build_spec`；而真实宿主拿到的正是这份退化 schema。新增 `test_build_spec_resolves_string_annotation` 与 `test_future_annotations_do_not_poison_the_bound_schema` 两条钉住。
- **能力清单导出不再会悄悄丢掉审批阈值**。手写 `ApprovalRule("amount", ">", 1000)` 时 `has_value` 默认 `False`，而导出按"没给值就不写 `value` 键"处理——导出的清单里阈值消失，导入端变成"`>` 遇到没有值的条件"，于是**每一次调用都被拒**。`ApprovalRule.__post_init__` 现在见到非 None 的值就把 `has_value` 补成真：忘填这个布尔只是省事，不该因此改变语义。
- **SECURITY 漏洞联系邮箱纠错**：中英两份此前写的 Gmail 尾号与仓库 git 身份差一位（疑为手误），任何按它投递的报告都到不了维护者手里。现改为维护者确认的 163 地址，并新增 `tests/test_security_policy_parity.py` 钉住中英两份的事实性字段不漂移。
- **一处对外话术变化（不改变判定，只改变措辞）**：审批结论的 reason 不再输出 `approval.required` / `approval.when` 这类协议术语，改为内核中文口径（"该能力要求每次调用都人工确认"、"命中审批条件：…"）。判定语义逐条不变，官方向量与全部既有测试的 decision 断言仍然全绿；原本有两条测试钉的是术语字符串而非行为，已改为钉行为。依赖日志文案做正则匹配的宿主需要注意这一处。

### Added

- **原生参数级审批 `ToolGovernance.approval`：不接任何外部协议也能说"超过这条线就要人确认"**。参数级审批的语义（严格 JSON 值比较、判不出来失败关闭、四步判定顺序）本来跟 ACC 一点关系都没有，却一直住在 `integrations/acc/evaluate.py` 里——结果是宿主只想给自家工具加个金额门槛，也得先学会一个外部契约、或者自己手写一个 `gate` 函数（函数进不了清单、跨不了语言、每家还各抄一遍"超过"怎么算）。现在判定整块搬进内核 `policy/approval.py`，宿主只写数据：
  ```python
  ToolGovernance(approval=ApprovalPolicy(
      prompt="退款 {amount} 元，确认吗？",
      when=(ApprovalRule("amount", ">", 1000, label="金额超限"),),
  ))
  ```
  - **`approval` 是数据，所以跟着能力清单走**：`governance_to_manifest` / `governance_from_manifest` 逐字段严校验（算子必须落在 `APPROVAL_OPS`、未知键拒绝加载），导入即生效。它敢导入即生效是因为**只会加严**——"要多问一个人"和"这次参数不合法别碰业务"，做不到替宿主放行；而放松的那条路（函数级 `gate`）依旧绝不凭清单重建。清单形状上"没给值"与"值就是 null"靠 **`value` 键的存在与否**区分（`exists` 不出键，`== null` 出 `"value": null`）。
  - **执行器两条新接线**：① 没有 `gate` 时用 `approval` 自己判；② 两者都在时**取更严地合并**（两边理由都进审批事件、只问一次人）——"有 gate 就当 approval 不存在"等于声明了的保护内核没做。组合工具分支不过 `authorize_tool_call`，但**它自己声明**的审批要求会真的问一次（只 `confirm`、不替黑盒名字跑策略），人不批一步都不跑。
  - **`integrations/acc` 就此降级为互操作层（shim）**：`evaluate.py` 只剩 ACC 自己的三个派生量（`is_exposed` / `effective_risk` / `audit_sensitive`）加一个 `to_approval_policy` 翻译器；`AccApprovalCondition` 变成 `ApprovalRule` 的别名（九个算子一字不差，再养一个同构数据类只会制造"两个模型谁说了算"）。判定只剩一份，由 `test_native_and_acc_reach_the_same_decision`（12 组含失败关闭）钉住两条路径结论一字不差。
  - **踩出来的两个真坑**：① `from __future__ import annotations` 让函数注解变成字符串，而内省把解不开的字符串一律当成 `string` ——审批要按绑定 schema 校验入参，于是**每次合法调用都被判成非法**。`build_spec` 现在先走 `get_type_hints`，解不开才退回原兜底。② 手写 `ApprovalRule("amount", ">", 1000)` 时 `has_value` 默认 `False`，导出的清单会把阈值丢掉、导入后变成"每次都拒"——`__post_init__` 见到非 None 的值就把它补上。
  - **一处话术变更要说明**：审批结论的 reason 文案改用内核中文口径（"该能力要求每次调用都人工确认"），不再带 `approval.required` 这种协议术语。有两条测试原本钉的是术语而不是行为，已改成断言行为；官方向量与 `test_acc_*` 判定结果全绿。新增 `tests/test_native_approval.py`（53 例，含依赖方向探针：子进程里只 import 内核模块，`sys.modules` 不许出现 `yai_core.integrations`）与 `tests/test_introspect.py` 增 2 例。讲义第 21 篇第 5 节、第 43 篇第 4 节、第 44 篇第 1/4/7/8/9 节同步改写。
- **资源作用域闸 `filesystem_gate`：把授权点从"模型写来的字符串"移到"解析后的落点"**。文件系统两道路径闸只管"**出不出根目录**"，管不了"根目录内的哪一块"；而宿主想说的恰恰是"只能碰 `notes/`"。按原始字符串匹配白名单是假的——`notes/a.txt`、`./notes/a.txt`、`notes//a.txt`、`notes/./a.txt` 是同一目标的四种写法，而且人工审批看到的正是这个原始串。
  - **归一化用与 handler 完全相同的 `normalize_relative`**，所以"批准目标 == 执行目标"是构造上成立、不靠约定；`write=True` 而 `prefixes` 为空 = 拒绝一切写（失败关闭）；非法形状（`..`、绝对路径、盘符冒号、空、非字符串）在闸口就拒并把理由回灌给模型。
  - **`GateDecision` 新增 `target` 字段**，与上一步的 `risk`/`scope` 同一条路落进 `PERMISSION_ASKED` 与 `TOOL_CALL`：审批人第一次能看到"这次究竟要写哪个文件"，而不是模型原样写来的串。
  - **三条边界照实写**：前缀匹配是**字面**的（`NOTES/a.txt` 在 `notes/` 下会被误拒——宁可误拒，要放宽宿主自己规范化）；它**不替代**逐段拦链接与 realpath 复核（批准与写入之间被换成链接仍是 fail closed）；它**不做授权**（`PermissionPolicy` 的 DENY 永远优先）。
  - 11 例测试，含一条**反"假绿"对照**：`test_without_the_gate_the_same_write_succeeds`——不挂闸时同一次越界写会成功落盘，证明"作用域"确实是闸在管而不是电池碰巧拦住。写这条之前我先踩了一遍：闸只挂在 `file_patch` 上、测试却调 `file_create`，"拒绝"那条就假绿了。讲义第 46 篇新增第 5 节。
- **准入判定的描述性字段终于落到审计上（`risk` / `scope` 进事件）**：`GateDecision` 一直带着 `effective_risk` 与声明的 `scope`，但执行器只用了 `reject / require_approval / reason / prompt`——**算出来就扔了**，结果审计链上看不出"这次是高影响写、作用域是什么"，事后无法按风险检索。现在两者随 `PERMISSION_ASKED`（审批人要判断该不该批）与 `TOOL_CALL`（事后审计）一起发出。
  - **不改变决策语义**：`risk` / `scope` 仍是描述性信息，放行与否只看策略的 DENY/ASK/ALLOW 与 `require_approval`；内核依旧不按风险自动升降档（那是宿主的权限模型）。
  - **未治理工具的事件形状逐字不变**：空值不写键，`test_ungoverned_tool_events_keep_their_old_shape` 钉住 `set(data) == {"tool", "arguments"}`。
- **上游 ACC 一致性语料不再随包分发**：`tests/fixtures/acc/`（Apache-2.0 的 26 条官方向量 + `SOURCE.md`）此前随 sdist 一起发布——**合规**（出处与许可证都在），但那是别人的测试数据集，我们没有对外分发它的必要。现在 `pyproject.toml` 的 sdist `exclude` 排掉该目录，缺文件时 `tests/test_acc_conformance.py` 整模块 `skip`。
  - 验证过两条：构建出的 sdist 里 `fixtures/acc` 命中 **0**、`tests/` 仍有 93 个文件（发版前对 `0.11.0` 产物重测）；把 sdist 解包后跑那个测试文件得到 **`1 skipped`**（不是报错）。源码仓库里跑仍全量执行，CI 就在源码仓库里跑。讲义第 44 篇第 5 节同步说明。

## [v0.10.0] - 2026-10-14

本版主线是**"让内核能被安全地嵌进真实软件"**：能力声明能跨进程走了，超时与收尾有了统一入口，多了第二个真实模型后端，文件能力有了第 10 个契约，公网端点多了两道闸。全部新增默认不改变既有行为。

- **能力声明跨进程**：`ToolGovernance` 的数据部分随 manifest 清单导出/导入，脱敏与审计收口在另一台机器上照样生效；`gate` 作为函数永不导出也永不凭快照重建——外来清单既加不了闸门，也没有摘闸门的通道。
- **运行时保障补齐两处空白**：工具 handler 硬超时（`tool_timeout`，异步真取消、同步如实告知"可能仍在后台跑完"）与 `AgentCore.aclose()` 统一收尾（谁创建谁负责关，幂等）。
- **第二个真实模型后端**：Anthropic 消息接口（`[anthropic]` extra），`ModelProvider` 契约与内核零改动。
- **第 10 个 SPI**：资源契约 + 安全文件后端（两道路径闸、原子写、乐观并发、超限报错不截断）。
- **外部能力契约（ACC）兼容层**：只声称 OpenAPI Binding Parser Profile，26 条官方向量逐字节入库校验；能力闸 + 宿主注入的可信主体让声明真正参与每一次调用，且**只能把闸门往上抬**。
- **安全两条**：沙箱 AST 静态闸门深嵌套改为失败关闭（并加模糊测试 target）；host_e 跨进程工具口加本地令牌鉴权。
- **限流加第二道闸**：`global_per_minute` 跨 IP 总配额（默认关闭），补上"每 IP 都没超但总量刷爆"的盲区。

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

- **对外文档改为"按类别"对比，不再点名同类项目**：`docs/comparison.md` / `.en.md` 重写为**类别描述**（通用框架 / 图编排运行时 / 多智能体框架 / 自托管平台 / 托管无代码平台），同时删除他方的自我宣传文案、星标数与指向他站的链接；README、`docs/architecture`、v0.6.0 发布说明、对外材料里的同类项目名字一并改为中性类别说法。YAI 一侧的事实（零硬依赖、九契约、benchmark 数字、测试数、宿主数）逐条保留并复核，中英两份同步。**刻意保留**两类名字：我们实现/依赖的接口与库名（模型兼容接口、MCP、OpenAPI、PySide6 等），以及作为类比对象的嵌入式数据库名。已生成的 PDF 与示意图 PNG 不在本轮重导（文本层已改，图里仍有旧名字，重导需另行确认）。

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
- **英文门面文档**：新增 `README.en.md`、`CONTRIBUTING.en.md`、`docs/architecture.en.md`、`docs/comparison.en.md`，`SECURITY.md` 英文化（中文另存 `SECURITY.zh-CN.md`），各份顶部双向互链并约定事实性内容中英同步、防漂移；逐行讲义与过程文档不翻译。
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

## [v0.5.0 早期赛事快照]

- 早期赛事快照版本（tag 指向该快照，版本内容保持原样）：MCP Client、LLM 路由器（规则兜底）、SQLite 持久化记忆（opt-in）、OpenAPI 发现、FastAPI 在线 API、多宿主 demo、容器化与 PaaS 部署。

## [v0.1]

- 函数内省、规则路由、Agent Loop、五个 SPI 默认实现、FastAPI Battery、三宿主 demo、容器化与 PaaS 部署。
