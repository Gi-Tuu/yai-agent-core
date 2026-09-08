# YAI Agent Core 架构设计

## 1. 设计原则

1. **Library-first（进程内库）**：`pip install` 后运行在宿主进程内，不独立起服务、不要求云端。
2. **Host-declares-capability**：宿主只声明能力（普通函数/OpenAPI/MCP），Core 负责一切 Agent 逻辑。
3. **内核最小**：判断标准——"是不是任何宿主都需要？"否则进 batteries/ 或 adapter。
4. **零硬依赖**：内核只用标准库；openai / fastapi 为可选依赖、懒加载。
5. **自适应必须可解释**：每个决策产出 Observer 事件，禁止黑盒。
6. **模型/通道/存储无关**：通过 SPI 契约解耦，Core 给零配置默认实现。

## 2. 分层

```
宿主应用（AMBRACE / host_a / host_b）      只声明能力，零 Agent 代码
        │ 内省 / 注册
能力自发现 Auto-Discovery（函数 / OpenAPI[v0.2] / MCP[v0.2]）
        ▼
YAI Kernel
  ├─ Adaptive Router   任务分类 → direct/react/plan/clarify + 模型 tier
  ├─ Agent Loop        按策略执行（工具循环、计划拆解、澄清重入）
  ├─ Context           消息组装与 token 预算
  └─ Tool Bus          权限 → 执行（sync/async）→ 结构化结果
        ▲
SPI 契约环（可替换 + 默认实现）
  ModelProvider(OpenAI 兼容) / Channel(CLI·FastAPI·Flutter)
  MemoryStore(内存→SQLite→向量) / PermissionPolicy(auto/ask/deny)
        ▲
Batteries（可选）：FastAPI Server、MCP Client、SQLite Memory
```

## 3. SPI 契约

| 契约 | 方法 | 默认实现 | 宿主何时替换 |
|---|---|---|---|
| ModelProvider | `achat(messages, tools, tier)` | OpenAICompatProvider（DeepSeek 等） | 接私有模型/本地模型 |
| Channel | `emit / ask / confirm` | CollectChannel、CliChannel | Web SSE、Flutter UI |
| MemoryStore | `history / put / get / clear` | InMemoryStore | SQLite、向量记忆 |
| PermissionPolicy | `check(tool, args) → allow/deny/ask` | AllowlistPolicy | 高危工具人工确认 |

## 4. Adaptive Router 状态机

```
任务文本 + 可用工具集
   ├─ 无工具                → direct（模型直接答）
   ├─ 模糊短句              → clarify（Channel.ask 后重入）
   ├─ 含行动词 + 多步骤信号 → plan（先拆解，再 react）
   └─ 含行动词              → react（工具循环）
```

v0.2：LLM 一次性分类输出 `{strategy, reason, tier}`，异常/不确定时回退规则路由。

## 5. 启动在线 API（batteries）

```python
import os, uvicorn
from yai_core import AgentCore, OpenAICompatProvider
from yai_core.batteries.fastapi_server import create_app
import my_app_capabilities as cap

core = AgentCore.auto(cap, OpenAICompatProvider())
app = create_app(core)
# 环境变量：YAI_GIT_COMMIT=<40位 commit>、YAI_PROJECT_SLUG=<slug>
# uvicorn module:app --host 0.0.0.0 --port 8000
```

## 6. 与生态的差异化（诚实对比，用于比赛材料）

- Claude/OpenAI Agents SDK、LangGraph：developer-builds-agent，工具/编排需显式定义，且绑定自家生态；
- agentic-kernel（TS）：嵌入式运行时理念相近，但仅 TypeScript 且无能力自发现/策略自适应；
- MCP：工具互操作**协议**，不是运行时；YAI 是 MCP 的消费者（v0.2 Client）而非竞争者；
- YAI：host-declares-capability、Python 进程内、模型/通道/存储全无关、自适应过程可观测。

## 7. 明确不做（v0.1 红线）

MCP Server、Multi-Agent 编排、自进化写工具、向量长期记忆、内置 UI、coding agent 场景、队列/中间件、K8s。
