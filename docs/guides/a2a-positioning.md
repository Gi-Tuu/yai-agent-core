# A2A 1.0 与 YAI 的定位

[English](a2a-positioning.en.md) · **简体中文**

> 结论先说：**A2A 不是内核特性，YAI 不在 `src/yai_core/` 里实现 Multi-Agent。**
> 有向父子核（ADR 0008）是"父产品调用子产品能力"的层级组合，不是 A2A 的 peer-to-peer 协作。
> 如果宿主需要 A2A，应该在**宿主层**做一个适配器，把远端 A2A Agent 当成一个工具；内核不感知。

## 1. A2A 1.0 是什么

[A2A（Agent-to-Agent）1.0](https://a2a-protocol.org/latest/announcing-1.0/) 是 Linux Foundation
下的开放协议，用于**多个 Agent 之间**发现、委派任务和交换结果；核心概念包括 Agent Card（能力描述）、
Task（长任务生命周期）与消息/制品。它与 MCP（Agent↔工具/数据）、AG-UI（Agent↔用户界面）是不同层：

| 层 | 协议 | 解决什么 |
|---|---|---|
| Agent ↔ 工具/数据 | **MCP** | 把外部能力变成可调用工具 |
| Agent ↔ 用户界面 | **AG-UI** | 把事件流变成前端可消费的标准事件 |
| Agent ↔ Agent | **A2A** | 跨 Agent 的任务委派与协作 |

## 2. YAI 的位置与红线

YAI 是**进程内嵌入式内核**：一个宿主软件 `import` 它，声明自己的能力，内核负责路由、循环、
工具执行与治理。它的设计目标不是"再做一个 Agent 平台"，因此：

- **不做 Multi-Agent 编排**：没有 peer 发现、投票、群聊、A2A server；
- **不做 A2A 实现**：不内置 Agent Card、Task 生命周期、远端 Agent 注册表；
- **有向父子核**（`shell/federation/`）是例外但不同层：父核调用**自己的子产品**的能力，
  有向、不可反调、无 peer 发现；见 [federation 指南](federation.md) 与
  [ADR 0008](../adr/0008-directed-federation.md)。

## 3. 有向父子核 ≠ A2A

| 维度 | 有向父子核（YAI federation） | A2A 1.0 |
|---|---|---|
| 拓扑 | 父 → 子，单向；子不能反调 | 对等 Agent 之间可互相委派 |
| 发现 | 父核显式配置/启动子核 | Agent Card / 注册表发现 |
| 身份 | 父子产品边界、grant、manifest | 跨组织 Agent 身份与鉴权 |
| 目标 | 一个产品组合另一个产品的能力 | 跨团队/跨厂商的任务协作 |
| 失败语义 | 父核负责子核生命周期与限额 | Task 状态机、远端重试/取消 |

把两者混为一谈会导致两个错误：给内核塞 peer 编排，或把父子核说成 A2A 兼容。

## 4. 如果宿主需要 A2A：放在宿主层

推荐形态是一个**宿主适配器**，不是内核改动：

```
远端 A2A Agent  ──A2A client──▶  宿主适配器  ──ToolSpec──▶  YAI AgentCore
```

- 适配器负责：Agent Card 解析、鉴权、Task 创建/轮询/取消、超时与重试；
- 对内核只暴露一个普通工具（例如 `a2a_delegate(agent, task)`），返回文本/结构化结果；
- 权限、审批、审计、预算仍由 YAI 内核统一治理；
- 子 Agent 的失败转成工具失败，走同一条 `tool_result` 与反思回灌路径。

这样既保留 A2A 的互操作价值，也不破坏"内核不做 Multi-Agent"的红线。

## 5. 决策与重访条件

**当前决策**：A2A 不进内核；文档明确边界；不提供官方 A2A server/client 依赖。

**何时重访**：

1. 有真实宿主提出"必须由内核直接做 A2A 发现/编排"的需求，且无法在宿主层满足；
2. 需要跨进程/跨组织的 Agent 身份成为内核一等能力（而不是工具参数）；
3. A2A 的 Task 语义与 YAI 的事件流/预算/审批模型出现不可调和的冲突，需要专门 ADR。

在此之前，A2A 的正确位置是**宿主集成示例**，不是 `src/yai_core/`。

## 6. 参考

- [A2A 1.0 announcement](https://a2a-protocol.org/latest/announcing-1.0/)
- [Linux Foundation: A2A surpasses 150 organizations](https://www.linuxfoundation.org/press/a2a-protocol-surpasses-150-organizations-lands-in-major-cloud-platforms-and-sees-enterprise-production-use-in-first-year)
- 本仓库：[federation 指南](federation.md) · [ADR 0008](../adr/0008-directed-federation.md) ·
  [AG-UI 适配](ag-ui.md) · [MCP Registry](mcp-registry.md)
