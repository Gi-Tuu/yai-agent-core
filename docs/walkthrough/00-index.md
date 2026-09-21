# 逐行讲解 00 · 导读：一次请求穿过整个 Core

> 这套文档对 `src/yai_core/` 每个文件做逐块、逐行解释，面向"边学边造"的你。
> 读法：先读本篇建立全局地图，再按编号逐篇对照源码看。每篇末尾有自检问题，答得上来再往下走。

## 0. 先记住一句话

**宿主只提供普通函数；Core 把函数变成工具，自己决定怎么用工具完成任务，并把过程一步步喊出来。**

## 1. 一张全景图（数字就是执行顺序）

```
你的任务 "先查看状态，然后检索约定记忆，给出关怀建议"
  │
  │ ① AgentCore.auto(宿主模块, 模型)            【core.py】
  │    └─ discover(宿主)                         【discovery/introspect.py】
  │         读函数签名/docstring → ToolSpec 列表 → 注册进 ToolRegistry【tools/registry.py】
  │    └─ （可选）attach_mcp_tools 接入外部 MCP 工具，同样注册进 ToolRegistry
  │                                            【integrations/mcp/client.py】
  │
  │ ② await core.run(任务)                       【core.py】
  │    └─ AgentLoop.astream(任务)                【kernel/loop.py】
  │
  │ ③ Router.classify(任务, 注册表) → 策略        【kernel/router.py】
  │    没有工具→direct / 行动词→react / 多步+行动→plan / 太模糊→clarify
  │    工具不足且模型报缺口 → capability_missing 事件
  │      └─（可选）ToolDiscovery 发现新工具 → 注册 → 本轮即可用【discovery/catalog.py】
  │
  │ ④ Context 组装消息列表（含刚发现的工具）      【kernel/context.py】
  │
  │ ⑤（plan 时）先让强模型拆步骤，再补一条"按计划执行"的 user 指令
  │
  │ ⑥ ReAct 循环                                 【kernel/loop.py】
  │    模型.achat(消息, 工具清单)                 【llm/openai_compat.py / 任何 SPI 模型】
  │      ├─ 模型不调工具 → 最终答案，结束
  │      ├─ 模型调 meta-tool（内核自己拦截，不进执行器）
  │      │     ├─ request_capability → 发现注册新工具（第 15 篇）
  │      │     ├─ compose_tool → 组合已有工具（第 16 篇）
  │      │     └─ create_code_tool → 注册代码工具（第 18 篇，需宿主沙箱）
  │      └─ 模型要调普通工具 → ToolExecutor.execute  【tools/executor.py】
  │             ├─ PermissionPolicy.check 权限   【policy/allowlist.py】
  │             ├─ 普通函数直接执行 / composite 逐步编排 / code 转交宿主沙箱
  │             └─ 结果包成 tool 消息塞回 Context，继续循环
  │
  │ ⑦ 全过程的每个动作变成 AgentEvent，经 Channel 往外发  【channels/】
  │ ⑧ 历史写进 Memory（InMemoryStore / SqliteStore）【memory/】
  └ 返回 RunResult（策略 + 事件列表 + 最终文本）
```

## 2. 文件分工速查

| 文件 | 角色 | 类比 |
|---|---|---|
| `types.py` | 全项目通用的数据结构 | 普通话词典 |
| `spi/*.py` | 六个"插槽"的接口约定（model/channel/memory/policy/discovery/sandbox） | 插座标准 |
| `discovery/introspect.py` | 把函数读成工具规格 | 自动翻译官 |
| `discovery/catalog.py` | 按需能力目录：缺口出现时补工具（最小发现源） | 备用零件柜 |
| `tools/registry.py` | 工具花名册 | 电话簿 |
| `tools/executor.py` | 真正调用工具的地方（组合工具在此分流到逐步执行） | 总机 |
| `tools/composer.py` | 组合工具：把已有工具编排成新工具（不越界、逐步授权） | 可复用的流水线模板 |
| `tools/code_tools.py` | 代码工具注册表：48h TTL、调用刷新、永久保留、过期回收 | 临时工的考勤与合同 |
| `tools/meta.py` | meta-tool：`request_capability`（发现）、`create_code_tool`（造代码工具） | 向内核自己提需求的按钮 |
| `spi/sandbox.py` | 第六个插槽：代码工具沙箱（只定义契约，执行由宿主实现） | 高危车间的安全规程 |
| `kernel/router.py` | 决定用哪种打法 | 作战参谋 |
| `kernel/context.py` | 管理发给模型的消息 | 剪贴板 |
| `kernel/loop.py` | 主循环（心脏） | 发动机 |
| `memory/inmemory.py` | 默认记忆 | 短期便签本 |
| `memory/sqlite_store.py` | 持久化记忆（opt-in） | 带锁的笔记本 |
| `memory/retention.py` | 历史裁剪纯函数（条数/TTL，轮边界对齐） | 会碎纸的碎纸机 |
| `policy/allowlist.py` | 默认权限 | 门禁 |
| `channels/*.py` | 默认输入输出 | 显示屏/话筒 |
| `llm/openai_compat.py` | 真实模型适配 | 翻译给 DeepSeek 听 |
| `core.py` | 对外唯一门面 | 前台 |
| `integrations/mcp/client.py` | 接入外部 MCP Server 的工具 | 外接设备转接头 |
| `integrations/openapi/` | OpenAPI 发现（opt-in） | REST API 的自动翻译官 |
| `batteries/fastapi_server/app.py` | 包成 HTTP 服务 | 对外营业窗口 |
| `batteries/fastapi_server/ratelimit.py` | 评审期限流（每 IP 滑动窗口） | 门口的取号机 |

## 3. 贯穿全项目的 5 个 Python 语法（先混个脸熟，后面逐个细讲）

1. `from __future__ import annotations`：让类型注解延迟成字符串，写新式注解不报错。
2. `async def / await / async for / yield`：异步与异步生成器，loop.py 的核心。
3. `@dataclass`：几行代码写出"只装数据的类"。
4. `Protocol`：不继承也能算"符合接口"（鸭子类型的书面版）。
5. `X | None`、`list[dict]`：新式类型注解。

## 4. 分篇目录

- 01 · `types.py`：数据词汇表
- 02 · `spi/`：可替换插槽（model/channel/memory/policy；Discovery 见第 15 篇、Sandbox 见第 17 篇）
- 03 · `discovery/introspect.py`：能力自发现
- 04 · `tools/`：注册表 + 执行总线
- 05 · `kernel/router.py` + `context.py`：决策与上下文
- 06 · `kernel/loop.py`：主循环逐行（最重要）
- 07 · 默认实现：memory / policy / channels
- 08 · `llm/openai_compat.py` + `core.py`：真实模型与门面
- 09 · `batteries/fastapi_server` + `Dockerfile`：变成在线 API 并容器化部署
- 10 · `integrations/mcp/client.py`：MCP Client，把外部工具接进注册表
- 11 · `memory/sqlite_store.py`：SQLite 持久化记忆（scope、迁移钩子、WAL）
- 12 · `integrations/openapi/`：OpenAPI 发现（REST API 零适配变工具，$ref/鉴权/只读闸）
- 13 · `memory/retention.py` + 两个 Store：历史保留策略（条数裁剪/TTL，轮边界对齐，opt-in）
- 14 · `batteries/fastapi_server/ratelimit.py`：限流 Battery（滑动窗口、429 + Retry-After、GET 放行）
- 15 · `spi/discovery.py` + `discovery/catalog.py`：能力缺口的最小闭环（缺口 → 发现 → 注册 → 本轮可用）
- 16 · `tools/composer.py`：组合工具（不越界地造工具：占位符解析、逐步授权、能力并集）
- 17 · `spi/sandbox.py`：第六个插槽 ToolSandbox（代码工具沙箱，只定义契约，执行由宿主实现）
- 18 · `tools/code_tools.py` + `meta.py` + 执行器接线：代码工具注册表、48h TTL、调用刷新、双重授权（执行仍走宿主沙箱）

## 5. 自检（读完本篇应能回答）

1. 宿主写的函数，是在哪一步变成工具的？
2. 策略是在哪决定的？四种策略分别什么意思？
3. 工具结果通过什么回到模型那里？
4. 为什么说 Channel 是"显示屏"而 Core 本身不打印东西？
