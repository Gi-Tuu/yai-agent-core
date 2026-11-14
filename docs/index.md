# YAI Agent Core

[English](index.en.md) · **简体中文**

> 进程内嵌入式、**自适应**的 Agent 内核 —— **Agent 世界的 SQLite**。
> 不必部署智能体平台，也不必写 Agent Loop / Planner / 工具选择代码：把内核 `import`
> 进你**已有的软件**，宿主只声明"我有什么能力"，Core 自动发现能力、选策略、调工具、完成任务。

- **仓库**：[github.com/Gi-Tuu/yai-agent-core](https://github.com/Gi-Tuu/yai-agent-core)（Gitee 镜像同步）
- **安装**：`pip install yai-agent-core` · **许可**：MIT · **版本**：v0.51.0
- **零第三方硬依赖**：模型、MCP、OpenAPI、浏览器/桌面、OTel 全部是可选项，懒加载

## 30 秒跑起来

```bash
pip install yai-agent-core
```

```python
import asyncio
from yai_core import AgentCore, ScriptedModel, ModelResponse, ToolCallRequest, build_spec

def search_notes(keyword: str) -> list[str]:
    """搜索宿主笔记库。"""            # 你自己的普通业务函数，没有任何 Agent 代码
    return {"周报": ["Core 骨架", "工具调用"]}.get(keyword, [])

model = ScriptedModel([                      # 离线确定性模型；上线换 OpenAICompatProvider()
    ModelResponse(content="", tool_calls=[ToolCallRequest(id="c1", name="search_notes",
                 arguments={"keyword": "周报"})]),
    ModelResponse(content="本周周报 2 条。"),
])
core = AgentCore(model)
core.register_tools([build_spec(search_notes)])   # 内省 type hints / docstring 生成工具
print(asyncio.run(core.run("搜索周报并总结")).final_text)   # 本周周报 2 条。
```

仓库里还有一个带事件流的完整示例：`python examples/quickstart.py`。

## 接下来看什么

| 你想…… | 读这篇 |
|---|---|
| 弄清全部文档、以哪份为准 | [文档总索引](DOCUMENT_MAP.md) |
| 知道"做到了什么程度、证据在哪" | [能力与证据索引](IMPLEMENTATION_STATUS.md) |
| 理解内核分层与数据流 | [架构设计](architecture.md) |
| 按顺序读代码（配逐行讲义） | [学习路线](reading-guide.md) → [讲义总览](walkthrough/00-index.md) |
| 上生产：权限、审批、部署 | [生产权限指南](guides/production-permissions.md) · [部署手册](guides/demo-deployment.md) |
| 做浏览器 / 桌面自动化 | [浏览器扩展与 Bridge](guides/browser-extension-bridge.md) |
| 看实测数字（而非口号） | [评测与证据](evaluations/memory-evaluation.md) |
| 看版本与变更 | [版本记录](https://github.com/Gi-Tuu/yai-agent-core/blob/main/CHANGELOG.md) |
| 与平台 / 框架对比 | [和同类有何不同](comparison.md) |

## 这是什么（一分钟版）

- **能力自发现**：普通 Python 函数的 type hints + docstring 自动变成 JSON Schema 工具；
  外部 MCP / OpenAPI 同构接入。
- **自适应路由**：任务自动走 `direct / react / plan / clarify`，决策全部是可见事件，
  可回放、可审计。
- **按需补能力**：感知能力缺口 → 向发现源要候选 → 注册 → 本轮即可调用。
- **治理**：权限三档、参数级审批、作用域闸、执行收据哈希链、可信变更预览。
- **可嵌入**：随宿主进程，无独立服务；十个 SPI 契约，任何部件都能换。

更多细节读 [README](https://github.com/Gi-Tuu/yai-agent-core#readme) 与
[白皮书](whitepaper.md)。
