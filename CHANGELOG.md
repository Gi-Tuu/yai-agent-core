# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 与语义化版本。
**内核本体始终保持零第三方硬依赖**（`pyproject.toml` 的 `dependencies = []`），真实模型、Web 服务、MCP、OpenAPI 等能力全部走可选 extras 与 SPI 契约懒加载。

详细的版本说明源稿见 [`docs/releases/`](docs/releases/)。版本号与 GitHub Release / tag 由维护者拍板后发布。

## [Unreleased]

### Added

- **随包发布零依赖确定性模型 `ScriptedModel`**（`yai_core.llm`）：`pip install` 后无需 API Key、不联网即可在 30 秒内验证内核已正确嵌入并跑通工具调用；README 新增"路线 A：零 Key 验证嵌入"，并有 `tests/test_quickstart.py` 作为可执行回归。
- **多模型兜底 `FallbackModelProvider`**：主模型 429 / 5xx / 超时 / 空响应时按序降级，带断路器冷却；只配置单个模型时默认休眠，运行时行为不变。
- **数字员工宿主 E 网页工作台成品化**：原生 CRM 界面（客户/订单/跟进/待办/日报五视图）+ 可收起的 AI 数字员工抽屉；内置按需能力目录（拜访天气、含税报价），演示"缺口感知 → 发现 → 授权 → 本轮可用"闭环。
- **数字员工宿主 E 产品化增强**：原生 CRM 扩为客户 / 订单 / 商机 / 跟进 / 待办 / 日报六个视图，内省自动发现 18 个业务工具（10 读 8 写）；新增客户搜索与等级/状态筛选、客户资料编辑、订单录入、商机看板（新建 + 阶段推进）等读写功能，全部不依赖 Core 即可使用，应用内开关一键对比有无 Core。
- **ToolDiscovery 最小闭环（第 5 个 SPI）**：模型报出 `missing_capability` 后，内核向发现源（内置 `StaticCatalog`）要候选、去重注册、发 `tool_discovered` 事件，新工具本轮 ReAct 即可调用；发现源异常不致命，"能被发现不等于被授权执行"。新增宿主 F 离线演示。
- **能力缺口感知**：LLM 路由输出 `missing_capability`，内核据此发 `capability_missing` 事件（规则路径、澄清、无工具部署不发）。
- **`llm_router="auto"`**：按模型后端 `yai_live_router` 标记自动决定是否启用 LLM 分类，真实模型开启、离线脚本模型走规则。
- **开源治理资产**：`ROADMAP.md`、`docs/comparison.md`（与主流框架的诚实对比）、Issue/PR 模板、CONTRIBUTING"新人 30 分钟跑通"、CI 四矩阵（Ubuntu 3.11/3.12/3.13 + Windows 3.13）。

### Changed

- 系统提示词由"你只能通过工具操作"改为"你可以使用工具；能力不足时请明确指出缺失能力"，同时保留"不要编造工具不存在的数据"防幻觉护栏。

### Fixed

- 澄清循环死锁：意图不清时追问最多两轮、上下文累积合并、空回答体面收尾，重置会先终止卡住的任务。

## [v0.5.0-hangzhou]

- 2026 AI 杭州·码动未来"超级智能体"赛道提交版本（tag 指向该比赛快照）：MCP Client、LLM 路由器（规则兜底）、SQLite 持久化记忆（opt-in）、OpenAPI 发现、FastAPI 在线 API、多宿主 demo、容器化与 PaaS 部署。

## [v0.1]

- 函数内省、规则路由、Agent Loop、五个 SPI 默认实现、FastAPI Battery、三宿主 demo、容器化与 PaaS 部署。
