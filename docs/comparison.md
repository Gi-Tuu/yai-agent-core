# YAI Agent Core 与同类方案对比（诚实版）

> 最后更新：2026-09-22。本文只写可验证事实：YAI 一侧以本仓库代码/配置为准，同类方案以其官方文档表述为准；不做贬低式比较，文末给出"什么场景该选谁"。

## 1. 一句话定位

| 方案 | 官方自我定位（事实） | 形态 |
|---|---|---|
| **YAI Agent Core（本项目）** | 进程内嵌入式、自适应的 Agent **内核**：宿主只声明能力，内核自动发现工具、自适应路由并执行 | Python 库（`pip install`），内核本体零第三方硬依赖 |
| LangChain | "构建 Agent 最快的方式"，提供标准工具调用架构、厂商无关设计与中间件 | Python/JS 框架 |
| LangGraph | LangChain 官方的**低层编排框架/运行时**，面向需要精确控制、生产级长运行 Agent | Python/JS 框架 |
| CrewAI | 基于**角色的多智能体编排框架**，快速搭建角色分工的 agent 工作流 | Python 框架 |
| AutoGen / Microsoft Agent Framework | 微软的**多智能体对话框架**（Agent Framework 是 AutoGen + Semantic Kernel 的统一后继） | Python/.NET 框架 |
| Dify | 开源 LLM 应用**平台**：Backend-as-a-Service + LLMOps，可视化 Workflow/Chatflow 编辑器，Docker 自托管 | 自托管平台（服务端 + Web 控制台） |
| Coze（扣子） | 面向所有技能水平用户的 AI 应用开发**平台**，可视化无代码/低代码编排，托管运营 | 托管 SaaS（国内扣子 / 海外 Coze） |

## 2. 关键维度对比

| 维度 | YAI Agent Core | LangChain / LangGraph | CrewAI / AutoGen | Dify | Coze |
|---|---|---|---|---|---|
| 交付形态 | 被 import 的库，随宿主进程运行 | 库/框架 | 库/框架 | 自托管服务端平台 | 托管 SaaS |
| 开发者要做什么 | 写普通业务函数并声明能力；**不写 Agent Loop/Planner/工具选择** | 自己定义 agent、工具、链/图、状态与边 | 自己定义角色、任务、协作流程 | 在平台里配置提示词、工具、Workflow | 网页上拖拽配置 Bot/插件 |
| 嵌入已有软件 | 进程内 import，一行 `AgentCore.auto`；不引入服务端 | 可嵌入，但需自行实现 Agent 装配与循环 | 可嵌入，多智能体范式需应用配合 | 应用成为 Dify 的客户端/外部调用方 | 无法嵌入，应用运行在平台内 |
| 内核运行时依赖 | **`dependencies = []`**（openai/fastapi/mcp/httpx 均为可选 extra 且懒加载） | 一组分层依赖包 | 框架依赖 | 完整服务端栈（Docker Compose 多容器） | 无（平台托管） |
| 工具接入 | 函数内省自动注册；**MCP Client**；**OpenAPI 自动发现**；**ToolDiscovery 按需发现闭环** | 工具抽象 + 大量集成包 | 工具抽象 | 平台内工具/插件市场 | 平台插件生态 |
| 编排范式 | 自适应路由（direct/react/plan/clarify）+ 能力缺口最小闭环（感知→发现→注册→本轮可用），宿主无需预设流程 | 链/中间件（LangChain）；图与状态机（LangGraph） | 角色团队/对话驱动 | 可视化 Workflow 图 | 可视化编排 |
| 路由自校准 | **从执行反馈学习的上下文老虎机**（按任务特征分桶的 Beta/Thompson，规则先验冷启动），同类任务越用越准；离线 benchmark 可复现（末段 91.3% vs 规则 73.3%），默认关闭 | 无内置，需自行实现评估/微调回路 | 无内置 | 无内置 | 无内置 |
| 多智能体 | **明确不做**（属宿主层） | 可自行实现 | **原生强项** | 支持多 Agent | 平台原生多 Agent 协作 |
| 决策可观测 | 事件流唯一出口，每次策略选择/工具调用/权限/能力缺口/工具发现都有事件 | 靠 LangSmith 等外部可观测服务 | 框架日志/外部服务 | 平台运行日志 | 平台内置日志 |
| 权限控制 | 宿主提供 PermissionPolicy SPI；读写分级、写操作可人工授权 | 应用自行实现 | 应用自行实现 | 平台权限模型 | 平台模型 |
| 部署要求 | 随宿主；另提供可选 FastAPI Battery 与 Docker 示例 | 应用自行部署 | 应用自行部署 | 需部署并运维平台 | 仅注册账号 |
| 离线可测 | 全量测试离线（假模型/MockTransport/注入时钟），不依赖网络与 Key | 可测，需自行 mock | 可测，需自行 mock | 依赖平台运行 | 依赖平台 |
| 成熟度 | 单人新项目，346 项测试、7 个示例宿主 | 成熟大生态 | 成熟社区 | 成熟平台（156K+ Star） | 商业产品 |

## 3. YAI 的差异化主张（对应"技术创新"评分）

1. **范式反转：从"拼装框架"到"嵌入式内核"。** 现有框架的共同前提是"开发者先有一个 Agent 要做，再去定义它"；YAI 回答的是另一个问题——"一个已经存在的软件（笔记、CRM、陪伴 App……）如何不写 Agent 代码就获得 Agent 能力"。类比不是 ORM，而是 SQLite：能力像数据一样长在应用里。
2. **能力自发现而非手工注册。** 宿主普通 Python 函数经 type hints + docstring 自动生成 JSON Schema 工具；外部 MCP Server 与 OpenAPI 服务同构接入，执行侧零感知。
3. **自适应路由 + 能力缺口闭环。** 内核按任务在 direct/react/plan/clarify 间选择（规则零成本兜底、LLM 增强、决策来源可审计）；当工具不足以完成任务时，先发出 `capability_missing` 事件，若注入了 `ToolDiscovery` 发现源则在本轮把缺失能力发现、注册并调用（发 `tool_discovered` 事件，发现≠授权）——把"我缺什么能力"从一句提示变成可运行的闭环，而不是假装工具存在或盲目调用。
4. **路由会从反馈里自校准（self-adaptive 坐实）。** 关键词/LLM 路由是开环的，判一次就结束；YAI 可选地在任务结束后从事件流抽取成败，按任务特征分桶累计 Beta(α,β) 后验，用 Thompson Sampling 为同类任务调整策略，冷启动以规则为先验。纯标准库、零依赖、离线可复现：内置 benchmark 五线（含"无先验""无上下文"消融）显示末段成功率 91.3% vs 规则 73.3%。学习默认关闭、硬规则区域不表态，学习算法本身也是第七个 SPI（`RouteSelector`）可替换。
5. **内核零硬依赖 + SPI 七契约。** 内核本体 `dependencies = []`，模型/通道/记忆/权限/发现源/沙箱/路由学习全部可替换；这让内核可以进入移动端、桌面端、受限网络等装不下框架全家桶的环境。

## 4. 什么场景该选谁（诚实建议）

- 你要**可视化拖拽、让非技术同学搭 Bot/知识库** → 选 **Dify / Coze**，这是它们的主场，YAI 不竞争。
- 你要**多角色分工、多智能体协作** → 选 **CrewAI / AutoGen（Microsoft Agent Framework）**，YAI 明确不做多智能体。
- 你要**精细的图编排、长运行有状态流程、人工审批节点** → 选 **LangGraph**，图是它的抽象强项。
- 你要**快速原型、用最丰富的集成生态** → 选 **LangChain**。
- 你要**把 Agent 能力内嵌进一个已有软件、保持进程内运行、不引入平台、不自己写 Agent 循环** → 这正是 **YAI Agent Core** 的位置。

## 5. 事实来源

- YAI 事实：本仓库 `pyproject.toml`（dependencies/extras）、`src/yai_core/`、`README.md`、`ROADMAP.md`（2026-09-22 复核）。
- LangChain / LangGraph：官方文档 https://docs.langchain.com/oss/python/concepts/products ；1.0 发布说明 https://www.langchain.com/blog/langchain-langgraph-1dot0
- CrewAI / AutoGen：LangChain 官方框架综述（2026-06）https://www.langchain.com/resources/ai-agent-frameworks
- Dify：官网 https://dify.ai/zh ；文档 https://docs.dify.ai/
- Coze / 扣子：https://docs.coze.com/guides ；https://docs.coze.cn/what_is_coze

> 同类项目迭代很快，以上为 2026-09-20 官方页面表述；提交前如有版本/定位变化，以各项目官网最新文档为准。
