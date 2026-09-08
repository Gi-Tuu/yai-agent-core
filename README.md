# YAI Agent Core

> 进程内嵌入式、自适应的 Agent 内核（Embeddable Self-Adaptive Agent Kernel）。
> 宿主软件只声明"我有什么能力"，Core 自动发现能力、自适应选择策略并完成任务——**宿主不写一行 Agent Loop / Planner / 工具选择代码**。

## 定位：不是又一个 Agent 框架

| 形态 | 数据库类比 | Agent 世界 | 你要做什么 |
|---|---|---|---|
| 平台/SaaS | 云数据库控制台 | Dify、Coze | 注册账号在平台里搭 |
| 框架/SDK | ORM | LangGraph、CrewAI、OpenAI/Claude Agents SDK | 自己定义 Agent、工具、编排 |
| **嵌入式内核（本项目）** | **SQLite** | YAI Agent Core | **把现有软件接进来，能力自动长出来** |

## 30 秒快速开始

```bash
uv venv
uv pip install -e ".[dev,llm,server]"
python scripts/smoke_test.py     # 离线冒烟：同一 Core 自适应三个不同宿主
pytest                           # 单元 + 端到端测试（不需要 API Key）
```

**第一次读代码**：[`docs/reading-guide.md`](docs/reading-guide.md) 是学习路线；[`docs/walkthrough/00-index.md`](docs/walkthrough/00-index.md) 是每个源码文件的逐行讲解。

宿主接入只有三步：

```python
from yai_core import AgentCore
import my_app_capabilities as cap          # 宿主自己的普通业务函数

core = AgentCore.auto(cap, model)          # ① 内省宿主能力，自动注册工具
result = await core.run("搜索本周记录并整理成报告")  # ② 自适应：direct/react/plan/clarify
print(result.final_text)                   # ③ 可交付结果 + 全程事件可观测
```

## 自适应机制（v0.1）

1. **能力自发现**：Python 函数 type hints + docstring 自动生成 JSON Schema 工具规格（v0.2：OpenAPI / MCP tools/list）
2. **策略自适应**：Adaptive Router 将任务路由到 `direct / react / plan / clarify`，规则实现零成本可测，v0.2 加 LLM 分类器并回退规则
3. **模型自适应**：标准任务/规划任务可路由到不同模型（tier: standard/strong），失败可回退；OpenAI 兼容（DeepSeek、通义千问等）
4. **宿主自适应（SPI）**：Model / Channel / Memory / Policy 四个契约宿主可替换，Core 提供零配置默认实现
5. **过程可观测**：每次策略选择、工具调用、权限确认都通过 Observer 事件流对外发出

## 目录结构

```
src/yai_core/
├── core.py              # AgentCore 门面（auto / run / astream）
├── types.py             # ToolSpec / ChatMessage / AgentEvent / Strategy
├── spi/                 # 宿主可替换契约：model / channel / memory / policy
├── discovery/           # 能力自发现（函数内省；v0.2 OpenAPI/MCP）
├── tools/               # ToolRegistry + ToolExecutor（Tool Bus）
├── kernel/              # AdaptiveRouter + AgentLoop + Context
├── llm/                 # OpenAI 兼容模型后端（可选依赖）
├── memory/ policy/ channels/   # 默认实现（内存记忆 / 白名单权限 / CLI·收集通道）
└── batteries/
    └── fastapi_server/  # 在线 API + /health + X-Agent 验证端点
examples/
├── host_a_notes/        # 宿主 A：笔记应用（只有业务函数，零 Agent 代码）
├── host_b_data/         # 宿主 B：销售数据应用（同一 Core 零修改适配）
└── host_c_companion/    # 宿主 C：AI 陪伴应用（AMBRACE 回流形态预演）
tests/                   # 离线 ScriptedModel 端到端测试
docs/                    # 架构设计、代码学习导览、三个比赛的提交清单
```

## 在线 API（X-Agent 部署要求）

```bash
uvicorn 启动示例见 docs/architecture.md
GET  /health                                # {"status":"ok","commit":"..."}
GET  /.well-known/xagent-verification.json  # {"schemaVersion":1,"slug":...,"commit":...}
GET  /v1/tools                              # 当前宿主自动发现的工具清单
POST /v1/agent/run                          # {"task": "..."} -> 事件流 + 最终结果
```

## 版本路线

- **v0.1（当前）**：函数内省、规则路由、Agent Loop、SPI 默认实现、FastAPI Battery、双宿主 demo
- v0.2：LLM 路由器（规则兜底）、OpenAPI 发现、MCP Client、SQLite 记忆
- v0.3：上下文/记忆自适应、检查点与失败恢复、Flutter Channel
- v1.0：作为 AMBRACE 的 Agent 内核回流嵌入

## 许可证

MIT
