# 结构化 Tracing（OpenTelemetry）

[English](tracing.en.md) · **简体中文**

YAI 把一次 Agent 运行映射成一条 [OpenTelemetry](https://opentelemetry.io/) trace，让"模型选了什么策略、调了哪些工具、哪个工具失败、卡在哪一步"在 Jaeger / Tempo / Grafana / 任意 OTLP 后端里一目了然。

它是一个**外侧订阅**的可选集成，不是内核的一部分：

- 内核本体仍然**零第三方硬依赖**，OpenTelemetry 全部在可选 `[otel]` extra 里、懒加载；
- **不改 `kernel/`、`core.py` 一行**——tracing 只是像普通宿主一样订阅 `AgentCore.astream` 的事件流；
- 库内**不初始化** TracerProvider / Exporter，是否上报、报到哪里，完全由宿主决定。

## 1. 安装

```bash
uv pip install -e ".[otel]"
# 或 PyPI：pip install "yai-agent-core[otel]"
```

不装也能正常 import 整个包；只有真正开始记录 trace 时才会抛出带安装指引的 `ImportError`。

## 2. 两种用法

### 2.1 `traced_run`：一次性拿结果 + trace

```python
from yai_core.integrations.tracing import traced_run

result = await traced_run(core, "搜索本周记录并整理成报告")
# result 与 core.run(...) 完全同构：strategy / events / final_text
# 同时产出一条 trace（用全局 TracerProvider）
```

### 2.2 `traced_astream`：边流式渲染边埋点

需要把事件实时推给前端（灵动岛 / Web）时用它；事件**一个不少**地原样转发，trace 在外侧静默记录：

```python
from yai_core.integrations.tracing import traced_astream

async for event in traced_astream(core, "整理本周笔记"):
    await websocket.send_json(event.model_dump())   # 与 core.astream 完全一致
```

两者都接受 `tracer=` 与 `root_attributes=`：

```python
result = await traced_run(
    core,
    "任务",
    tracer=my_tracer,                       # 不传则用全局 provider 的 tracer
    root_attributes={"yai.host": "notes", "yai.env": "local"},
)
```

## 3. Span 模型

一次 run 是一棵两层的 span 树：

```
yai.agent.run                         （root span，一次任务一个）
├── yai.tool.recall_memories          （每次工具调用一个子 span）
├── yai.tool.search_notes
└── yai.tool.save_memory
```

### Root span：`yai.agent.run`

| 属性 / 事件 | 含义 |
|---|---|
| 属性 `yai.task` | 原始任务文本（超长截断到 2000 字符） |
| 属性 `yai.strategy` | 最终策略 `direct / react / plan / clarify` |
| 属性 `yai.tier` | 最近一次模型档位 `standard / strong` |
| 属性 `yai.final_text_len` | 最终回复长度（不塞正文，避免 trace 里堆大文本 / 隐私） |
| 属性 `yai.host` 等 | 由 `root_attributes` 透传，用来区分是哪个宿主应用 |
| Span event `yai.<事件类型>` | 策略选择、计划创建 / 修订、澄清、能力缺口、工具发现 / 组合 / 创建、上下文压缩、档位切换等，事件字段以 `yai.<key>` 作为 event 属性 |
| Status | 流抛异常或收到 `error` 事件 → `ERROR`（异常同时 `record_exception`）；否则 `OK` |

### 工具子 span：`yai.tool.<工具名>`

| 属性 | 含义 |
|---|---|
| `yai.tool` | 工具名 |
| `yai.arguments` | 入参（JSON，超长截断） |
| `yai.ok` | 成功 / 失败 |
| `yai.preview` | 成功时的结果预览（内核本就截断到 200 字符） |
| `yai.error` | 失败时的错误信息，同时 span Status 置 `ERROR` |
| `yai.without_call_event` | 该结果没有配对的 `tool_call`（组合工具 / 内核 meta-tool 不发 call），由 tracer 补的即时 span |
| `yai.unclosed` | 流被取消 / 异常中断时，被强制收尾的未闭合工具 span |

工具在执行器里是**严格串行**的，`tool_call` 与 `tool_result` 不交错，因此用一个按工具名配对的栈就能正确建立父子关系；配对缺失的两种边界（无 call 的结果、无 result 的 call）都做了容错，不丢观测。

## 4. 对接 Jaeger / Tempo（宿主侧，二选一）

库不碰 provider 初始化。宿主在进程启动时配置一次即可，之后 `traced_run` 不传 `tracer` 也会自动走全局 provider。

### 4.1 OTLP/HTTP（推荐，跨语言通用）

```python
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

provider = TracerProvider()
provider.add_span_processor(
    BatchSpanProcessor(
        OTLPSpanExporter(endpoint="http://localhost:4318/v1/traces")
    )
)
trace.set_tracer_provider(provider)
```

本地起 Jaeger：

```bash
docker run --rm -p 16686:16686 -p 4318:4318 jaegertracing/all-in-one:latest
# 跑一次 traced_run 后，打开 http://localhost:16686 查看 yai.agent.run
```

### 4.2 OTLP/gRPC

把 exporter 换成 `opentelemetry.exporter.otlp.proto.grpc.trace_exporter.OTLPSpanExporter`（`[otel]` extra 已包含 grpc 传输），endpoint 形如 `http://localhost:4317`（不带路径）。Grafana Tempo / 任意 OTEL Collector 同理，把地址指向 Collector 即可。

> 生产环境建议用 `BatchSpanProcessor`（异步批量、低开销）；测试 / 调试可用 `SimpleSpanProcessor` + `ConsoleSpanExporter` 直接把 span 打到 stdout。

## 5. 设计边界（刻意不做的事）

- **不侵入内核**：tracing 是事件流的一个普通消费者，内核不知道它的存在，因此永远不会因为"没装 tracing"而影响 Agent 行为。
- **不把 opentelemetry 转成硬依赖**：`pyproject.toml` 的 `dependencies` 仍为空，otel 只在 `[otel]` extra。
- **不替宿主初始化上报通道**：库内不创建 provider、不读环境变量连后端，避免"引入库就偷偷外发数据"。
- **不记录最终回复正文 / 记忆内容**：只记长度与结构化字段，降低 trace 里的隐私面；需要正文由宿主自行在 `root_attributes` 或自己的 span 里加。
- **不改变时序与配对语义**：严格按事件流到达顺序开 / 关 span。

## 6. 离线测试

`tests/test_tracing_otel.py` 用 SDK 的 `InMemorySpanExporter` 在进程内断言整棵 span 树（root 父子关系、成功 / 失败状态、异常路径、组合工具 orphan 补 span、流中断收尾、属性转换），**不连任何 collector、不需要 API Key**；未安装 `[otel]` 时需要 SDK 的用例自动跳过，"缺依赖给出安装提示"的用例始终运行。
