# 路线图（Roadmap）

> 最后更新：2026-09-20。本路线图只记录**方向与边界**，不承诺具体日期；每个版本的实际范围以 Release notes 为准。

## 愿景

让"给现有软件加 Agent 能力"像"给应用嵌入数据库"一样简单：宿主软件只声明自己的业务能力（普通函数 + 一行描述），YAI 内核在进程内完成能力发现、意图理解、策略选择、工具调用、记忆与可审计交付，**宿主不写一行 Agent Loop / Planner / 工具选择代码**。

## 设计原则（所有版本不变）

1. **Library-first**：YAI 是被 import 的内核，不是需要单独部署的平台；内核本体 `dependencies = []`，第三方能力全部走可选 extra 懒加载。
2. **一切外部能力走 SPI**：模型（Model）、通道（Channel）、记忆（Memory）、权限（Policy）、发现（Discovery）五个契约宿主可替换。
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

### v0.7 — 大规模工具集与反思

- **两层工具目录**：注册表提供 `catalog()`（名称 + 一句话摘要）与 `schemas_for(names)`（按需返回完整 schema），系统提示只放目录，工具数量上百也不爆上下文。
- **每轮反思**：工具结果回灌后重新判断"继续 / 换策略 / 申请新能力"，路由不再是一次性分类。
- 网页工作台渲染 `capability_missing` 事件。
- 运维向：KV TTL（SQLite schema v2 迁移）、响应体大小守卫、API 鉴权。

### v0.8 — Tool Discovery（发现工具，动态发现源）

> 最小切片已在 v0.6 落地：`ToolDiscovery` SPI + 进程内 `StaticCatalog` + 缺口→注册→本轮可用闭环（见上）。
> 本阶段把"进程内静态目录"换成"运行时动态发现源"。

- 发现源扩展：MCP 目录（按语义找到公共 MCP Server 并 list_tools）、OpenAPI 服务目录、宿主插件市场。
- 语义匹配（关键词之外的向量/嵌入检索），用于大规模工具目录。
- 每轮反思中按需再次发现（与 v0.7 反思循环联动）。
- 发现的工具注册前经权限策略确认（高危能力显式授权）——该约束在静态目录阶段已由权限闸保证。

### v1.0 — 回流 AMBRACE

- 作为开源 AI 陪伴项目 [AMBRACE](https://github.com/Gi-Tuu/AMBRACE) 的 Agent 内核内嵌发布。
- 陪伴场景的主动行为、事件响应、记忆与工具权限在 YAI 上收敛，替换 AMBRACE 内硬编码的仲裁逻辑。
- 稳定的 SPI 兼容性承诺与语义化版本。

### v1.x 之后（方向，不排序）

- 组合工具（把多个工具编排为工作流，零代码、无沙箱风险）。
- 代码生成工具（沙箱作为 SPI 由宿主提供，内核不内置沙箱）。

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
