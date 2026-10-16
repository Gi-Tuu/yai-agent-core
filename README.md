# YAI Agent Core

[English](README.en.md) · **简体中文**

[![CI](https://github.com/Gi-Tuu/yai-agent-core/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Gi-Tuu/yai-agent-core/actions/workflows/ci.yml)
[![OpenSSF Scorecard](https://api.securityscorecards.dev/projects/github.com/Gi-Tuu/yai-agent-core/badge)](https://scorecard.dev/viewer/?uri=github.com/Gi-Tuu/yai-agent-core)
[![OpenSSF Best Practices](https://www.bestpractices.dev/projects/15360/badge)](https://www.bestpractices.dev/projects/15360)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![Version](https://img.shields.io/badge/version-v0.11.0-blue.svg)](CHANGELOG.md)
[![Kernel](https://img.shields.io/badge/kernel-0%20third%20party%20deps-c0392b)](pyproject.toml)

<p align="center"><b>给每个软件，一颗会思考的心脏。</b></p>

> 进程内嵌入式、**自适应**的 Agent 内核 —— **Agent 世界的 SQLite**。
> 不必部署一个智能体平台，也不必写 Agent Loop / Planner / 工具选择代码：把内核 `import`
> 进你**已有的软件**，宿主只声明“我有什么能力”，Core 自动发现能力、选策略、调工具、完成任务。

<p align="center">
  <img src="docs/assets/desktop-panel.png" alt="YAI 灵动岛员工面板：对话为主体，思考链与调用链折叠成过程步，含税任务从发现工具到归档完整闭环" width="520">
</p>

<p align="center"><b>一个浮窗，就是一个数字员工。</b> 对话为主体，思考链与工具调用折叠成“过程 · N 步”；
路由、能力缺口、新工具飞入、授权卡全部由真实内核事件驱动。截图数据均为虚构。</p>

## 这是什么

YAI 不是又一个聊天机器人，也不是让你照着教程拼装的 Agent 框架。它是一个**跑在你软件进程里的内核**：

- 你把软件里**本来就存在的普通函数**交给它（或接入 MCP / OpenAPI）；
- 它内省这些能力、自动生成工具规格，在收到任务时**自适应**地决定直接回答、调用工具、分步规划，还是向你澄清；
- 能力不足时能**感知缺口、按需发现新工具**，高危操作默认**先请求授权**——每一步都以事件对外可见。

## 适合谁 / 不适合谁

| ✅ 适合你，如果…… | ⛔ 不适合你，如果…… |
|---|---|
| 已有一个能跑的软件，想**低成本加上 Agent 能力**，且不被平台锁定 | 想在网页上**拖拽搭 Agent**、完全不写代码（用可视化 Agent 平台） |
| 想让 AI 调用你软件的**真实功能**，而不是通用闲聊 | 想要开箱即用的**云端托管 / 团队 SaaS** 智能体 |
| 需要可**自托管、可内嵌**、各部件可替换的 Agent 运行时 | 想要无人值守、全自动的 **coding agent**（YAI 刻意默认要授权） |
| 想边做边学 Agent 内部原理（每个决策可审计、有逐行讲义） | 期望零配置的通用助手——YAI 的价值恰恰在**绑定宿主能力** |

## 和你听过的东西有何不同

|  | 平台<br>可视化 Agent 平台 | 框架<br>编排框架 · 多智能体框架 | **嵌入式内核 · YAI** |
|---|---|---|---|
| 形态 | 注册账号，在平台里搭 | 自己定义 Agent / 工具 / 编排 | **`import` 进现有软件** |
| 要写的 Agent 代码 | — | 多 | **无** |
| 部署方式 | 平台托管 | 自己起一个服务 | **随宿主进程，无独立服务** |
| 绑定宿主能力 | 弱、需手动接 | 手动接 | **自动内省发现** |
| 内核第三方硬依赖 | — | 通常较多 | **0**（全部可选、懒加载） |

> 数据库类比：平台是云数据库控制台，框架是 ORM，**YAI 是 SQLite** —— 不抢主角，安静地嵌进你的程序。

## 30 秒跑起来

**路线 A · 只装库、零 Key 验证嵌入**（不必克隆、不必申请模型）。内核本体零第三方硬依赖：

```bash
pip install yai-agent-core
```

> 想尝鲜 main 上尚未发版的最新提交，再用源码安装：
> `pip install "yai-agent-core @ git+https://github.com/Gi-Tuu/yai-agent-core.git"`

```python
import asyncio
from yai_core import AgentCore, ScriptedModel, ModelResponse, ToolCallRequest, build_spec

def search_notes(keyword: str) -> list[str]:
    """搜索宿主笔记库。"""                # 你自己的普通业务函数，没有任何 Agent 代码
    return {"周报": ["Core 骨架", "工具调用"]}.get(keyword, [])

# 离线确定性模型（真实上线整体换成 OpenAICompatProvider）：先调工具，再给结论
model = ScriptedModel([
    ModelResponse(content="", tool_calls=[ToolCallRequest(id="c1", name="search_notes",
                 arguments={"keyword": "周报"})]),
    ModelResponse(content="本周周报 2 条。"),
])
core = AgentCore(model)
core.register_tools([build_spec(search_notes)])      # 内省 type hints / docstring 生成工具
print(asyncio.run(core.run("搜索周报并总结")).final_text)   # 本周周报 2 条。
```

接真实模型只需换一行（`[llm]` extra，OpenAI 兼容 DeepSeek / 通义 / OpenAI / Agnes）：

```python
from yai_core import OpenAICompatProvider
model = OpenAICompatProvider()      # 读 OPENAI_API_KEY / OPENAI_BASE_URL / LLM_MODEL
```

用 Claude 系列则换 `[anthropic]` extra（同一套 `ModelProvider` 契约，翻译在适配器里做完）：

```python
from yai_core import AnthropicProvider
model = AnthropicProvider()         # 读 ANTHROPIC_API_KEY / ANTHROPIC_MODEL / ANTHROPIC_STRONG_MODEL
```

**路线 B · 克隆仓库，跑全部宿主与测试**（离线，无需 Key）：

```bash
uv venv
uv pip install -e ".[dev,llm,server]"
uv run python scripts/smoke_test.py   # 同一 Core 自适应多个不同宿主
uv run pytest                         # 全量测试，不需要 API Key
```

第一次读代码：[`docs/reading-guide.md`](docs/reading-guide.md) 是学习路线，[`docs/walkthrough/00-index.md`](docs/walkthrough/00-index.md) 是逐行讲解。

## 核心能力

| 能力 | 一句话 |
|---|---|
| **能力自发现** | Python 函数 type hints + docstring 自动生成 JSON Schema；外部 **MCP** 工具经 MCP Client、**OpenAPI** REST 操作经规范解析，同构接入注册表 |
| **自适应路由** | 任务路由到 `direct / react / plan / clarify`；LLM 分类异常自动回退规则，工具不足时发出 `capability_missing`，决策全程可审计 |
| **自校准学习** | 上下文 bandit 从成败中学习路由，Thompson Sampling 选策略；离线 benchmark 末段 **89.6% vs 规则 73.3%（约 +16pp）** |
| **按需发现闭环** | 缺口 → 向发现源要候选 → 注册 → **本轮即可调用**；词法 + 本地 bge-m3 / 云端 embedding 双通道召回，远程 MCP/OpenAPI 源也按缺口保守收窄（空命中回退全量） |
| **长上下文压缩** | 超预算时把旧轮压成摘要、最近完整轮原样保留，模型失败回退硬裁剪——长对话不丢早期目标与事实 |
| **动态重规划** | 多步计划执行中走不通时，模型调 `replan` 显式回到规划、保留已完成步骤产出修正计划（`plan_revised`）；仅 PLAN 注册、只重排内部计划无副作用、不走授权 |
| **成本感知选档** | 默认走便宜快档 standard，仅规划 / 工具失败纠偏 / 后段卡住才升 strong；选档是确定性规则、不额外花钱，DONE 汇总各档调用数，`cost_aware=False` 可关 |
| **结构化长期记忆** | `remember` / `recall` 主动把事实 / 偏好 / 画像存档并召回；高相似去重合并、重要度加权、落盘 JSON；不内置向量记忆，语义召回走 Embedding SPI |
| **安全边界** | 权限三档（全部 / 部分 / 无需审批）；组合工具只编排已注册工具、不执行生成代码；代码生成仅定义 `ToolSandbox` 契约、沙箱由宿主提供 |
| **子员工委派** | 根员工可把子任务派给短命子员工：工具白名单 ⊆ 根、权限单调收窄、独立小上下文、带预算与全局并发闸（并行 ≤ 5、嵌套 ≤ 2），结果只回流根、完成即销毁；灵动岛一座岛切换多个彼此隔离的专员 |
| **全程可观测** | 每次策略选择、能力缺口、工具发现 / 组合 / 调用、权限确认都通过统一事件流对外发出 |
| **结构化 tracing** | 可选 `[otel]` extra 在**外侧订阅**事件流，把一次 run 转成一条 OpenTelemetry trace：root span 串起每个工具的子 span（成功 / 失败 / 耗时），自适应决策落为 span event；不改内核、不初始化 exporter，是否接 Jaeger / Tempo / OTLP 由宿主决定 |
| **端到端评测** | 内核自带离线 eval harness：ScriptedModel 真跑完整 Core，量化完成率、工具精确 / 召回、越权与无支撑断言；golden 回归基线 + 非门禁 CI |

<details>
<summary>展开：十个可替换 SPI 契约</summary>

`Model · Channel · Memory · Policy · Discovery · Sandbox · RouteSelector · Embedding · StorageCipher · Resource`
——宿主可替换任意一个，Core 提供零配置默认实现（`StorageCipher` 为可选的落盘加密插槽；`Resource`
是文件/资源访问插槽，内核本体不碰磁盘，内置实现见 `batteries/filesystem.py`）。内核本体始终零第三方硬依赖。

</details>

## 两种用法

### 1）作为库嵌入（三步）

```python
from yai_core import AgentCore, OpenAICompatProvider
import my_app_capabilities as cap                       # 宿主自己的普通业务函数

core = AgentCore.auto(cap, OpenAICompatProvider())      # ① 内省能力，自动注册工具
result = await core.run("搜索本周记录并整理成报告")        # ② 自适应 direct/react/plan/clarify
print(result.final_text)                                # ③ 可交付结果 + 全程事件
```

### 2）灵动岛员工薄壳（`shell/desktop`）

需要一张“脸”的宿主**不必自己写交互窗口**：官方薄壳提供一个常驻浮窗，Qt Quick 真原生渲染、
与各应用内置的 Core 跑在**同一进程**（无本地端口）。

<p align="left">
  <img src="docs/assets/desktop-ball.png" alt="灵动岛引导球：圆形球加紫色呼吸环，球内点我提示" width="180">
</p>

- **三态形变**：圆形球（56）→ 悬停向右展开胶囊（340×52）→ 点击向下展开员工面板（470×620），全程可拖动；
- **位置记忆**：拖动后关闭重开，浮窗回到原处；
- **首次引导**：球内“点我” + 紫色呼吸环，引导一次后不再出现；
- **全局快捷键**：在任意软件按 `Ctrl+Alt+Y` 立刻呼出并展开；
- **离线 / 真实双入口**：桌面快捷方式分别启动离线演示与接真实模型的员工；
- **多专员切换**：一座岛管理销售 / 仓库 / 笔记 / 陪伴多个**彼此隔离**的专员，时间线不丢 ——
  灵动岛只做前台 / 交换机，**不做超级 Agent**。

```bash
uv sync --extra desktop
.\.venv\Scripts\python.exe -m shell.desktop            # 离线演示：无 Key 看全部动效
.\.venv\Scripts\python.exe -m shell.desktop --live     # 接真实模型
```

薄壳只做呈现与编排；`shell/employee` 把子员工实现为“收窄工具 + 收窄权限 + 独立上下文 + 预算”的
一次 sub-run（并行 ≤ 5、嵌套 ≤ 2、结果只回流根），**内核不感知界面、也不做多 Agent 框架**。

## 截图墙

<p align="center">
  <img src="docs/assets/hoste-native.png" alt="销售 CRM 原生六视图，不依赖 Core 完整可用" width="45%">
&nbsp;&nbsp;
  <img src="docs/assets/hoste-discovery.png" alt="CRM 嵌入 Core，能力不足时自动发现天气工具并请求授权" width="45%">
</p>

<p align="center"><b>左：</b>软件本来的样子（六个原生视图，无 Core 也完整可用）；
<b>右：</b>嵌入 Core 后，能力不足时自动发现工具、首次调用仍弹授权卡（发现 ≠ 授权）。</p>

<p align="center">
  <img src="docs/assets/router-learning-curve.svg" alt="自校准路由学习曲线：bandit 从规则基线爬升到约 90%" width="60%">
</p>

## 示例（由浅入深）

| # | 宿主 | 看什么 |
|---|---|---|
| 1 | `host_a_notes` | 最小接入：普通函数自动变工具 |
| 2 | `host_c_companion` | 换领域即换岗位（AMBRACE 预演） |
| 3 | `host_d_mcp` | 作为 Client 接入外部 MCP Server |
| 4 | `host_f_discovery` | 能力缺口 → 发现 → 本轮可用（静态 / 语义） |
| 5 | `host_g_sandbox` | 教学沙箱：现场造代码工具并执行 |
| 6 | `host_e_sales_crm` | 旗舰：独立 CRM，一个开关嵌入 Core（终端 + 网页，端口 8200） |
| 7 | `host_i_mealie` | 适配真实第三方开源项目（Mealie，REST） |
| 8 | `host_j_actual` | 第三方播种五拍标准线：记账应用 Actual Budget（发现补能力 / 派子员工，网页 8240） |

> 目录名保留历史编号（a/c/d/e/f/g/i/j），不随学习顺序重排，以便与各版材料、讲义路径一致。

## 在线 HTTP API

线上实例：**https://yai-agent-core.onrender.com**（免费层冷启动约 30–90s）

```bash
GET  /health                  # {"status":"ok", ...}
GET  /v1/tools                # 当前宿主自动发现的工具清单
POST /v1/agent/run            # {"task": "..."} -> 事件流 + 最终结果
```

可选环境变量（持久化记忆、历史裁剪 / TTL、限流等）见 [`.env.example`](.env.example) 与 [`docs/architecture.md`](docs/architecture.md)。

## 版本路线

- **v0.1 – v0.3（已完成）**：函数内省、规则 / LLM 路由、Agent Loop、SPI 默认实现、MCP Client、SQLite 持久化、OpenAPI 发现、历史裁剪与限流、容器化部署
- **v0.4 – v0.6（已完成）**：能力缺口感知与 ToolDiscovery 闭环、host_e CRM 零改造嵌入、权限三档、组合工具、双通道语义发现、执行中动态发现
- **v0.7（已完成）**：自校准路由 contextual bandit（离线 +16pp）、代码工具注册 / TTL / 授权闸、子进程教学沙箱、灵动岛员工薄壳
- **v0.8（已完成）**：上下文摘要压缩、MCP/OpenAPI 动态发现源与多源聚合、SQLite KV TTL、Docker 沙箱适配器、API Key 鉴权 / 限流 / 响应守卫、发布 PyPI、CI 安全加固（CodeQL、OpenSSF Scorecard、mypy、覆盖率门禁）
- **v0.9（开发中，已在 main 落地）**：动态重规划 `replan`、成本·延迟感知选档 `tiering`（standard/strong 自动换挡 + DONE 成本汇总）；长期记忆语义召回与任务起始主动注入、落盘加密第九 SPI `StorageCipher`；结构化 tracing（OpenTelemetry）与 metrics / readiness、任务级预算熔断；子员工预算与全局并发闸；第三方播种五拍标准线（`host_j` 零改造嵌入真实记账应用 Actual Budget）
- **v1.0（规划中）**：作为 AI Companion 项目 [AMBRACE](https://github.com/Gi-Tuu/AMBRACE) 的 Agent 内核回流嵌入

## 许可证

MIT · [Gi-Tuu](https://github.com/Gi-Tuu)
