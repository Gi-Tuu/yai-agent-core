# 逐行讲解 33 · 结构化 tracing：在外侧把一次 run 变成一条 OpenTelemetry trace

> 对应文件：
> - 实现：`src/yai_core/integrations/tracing/otel.py`（懒加载、span 映射、两个入口）；
> - 导出：`src/yai_core/integrations/tracing/__init__.py`；
> - 可选依赖：`pyproject.toml` 的 `[otel]` extra（opentelemetry-api/sdk/exporter-otlp）；
> - 事件来源：`src/yai_core/types.py`（`AgentEvent` / `EventType`）、`kernel/loop.py`（事件如何产生）；
> - 门面文档：`docs/tracing.md`（中英）；
> - 离线测试：`tests/test_tracing_otel.py`（InMemory exporter，模块 100% 覆盖）。
>
> 读前先读第 1（types 事件词汇表）、6（loop 事件流）、8（core 门面）。本章不改内核一行，
> 只在**外侧订阅**已经 yield 出来的事件，把它们翻译成一条标准 trace。

## A. 为什么 tracing 放在"外侧"

YAI 的铁律之一是"**每个自适应决策都发事件**"，而事件流有唯一出口
`AgentCore.astream(task)`。这意味着：**任何想观察运行过程的人，都不必钻进内核**——
像普通宿主一样 `async for event in core.astream(...)` 就能看到全过程。

tracing 就是利用这一点。它不修改 `kernel/loop.py`、`core.py`，只是一个"边看事件边画
span"的旁观者。这样设计有三个直接好处：

1. **零侵入**：内核不知道 tracing 的存在，不装 otel 也完全不影响 Agent 行为；
2. **零硬依赖**：`opentelemetry` 只进可选 `[otel]` extra，内核本体 `dependencies` 仍为空；
3. **不偷偷外发数据**：库内不初始化 TracerProvider/Exporter，报到哪、报不报，由宿主决定
   （这是 OpenTelemetry 对 "instrumentation library" 的惯例：库只产生遥测，SDK 配置归应用）。

## B. 先补五个 OpenTelemetry 最小概念

- **Trace**：一次完整任务的完整调用链，由若干 span 组成一棵树。
- **Span**：树上的一个节点，有名字、开始/结束时间、父子关系。
- **Attribute**：span 上的键值对（字符串/布尔/数值），用来记录结构化细节。
- **Span Event**：span 时间线上带时间戳的"事件点"，也能带属性——适合记录"这一刻发生了什么"。
- **Status**：`OK` / `ERROR`，ERROR 可带描述。
- **TracerProvider / Processor / Exporter**：属于 SDK，决定 span 往哪送（控制台 / OTLP /
  Jaeger）。**本模块一个都不创建**，宿主配好后用全局 tracer，或把 tracer 显式传进来。

YAI 把一次 run 映射成两层：

```
yai.agent.run                 root span：一次任务
├── yai.tool.recall_memories  子 span：一次工具调用（CALL 开、RESULT 关）
└── yai.tool.save_memory
```

策略选择、计划、澄清、能力缺口这些"不是工具调用"的瞬间，落到 root span 的 **span event**。

## C. 常量与懒加载

```python
ROOT_SPAN_NAME = "yai.agent.run"
TOOL_SPAN_PREFIX = "yai.tool"
_ATTR_LIMIT = 2000
_EVENT_SKIP_KEYS = frozenset({"final_text"})
```

- 两个名字常量统一 span 命名，测试也引用它们，避免魔法字符串散落。
- `_ATTR_LIMIT`：写进 span 的字符串最多 2000 字符。工具参数 / 结果预览可能很大，
  不截断会把 trace 后端撑爆。
- `_EVENT_SKIP_KEYS`：`final_text`（最终回复正文）不塞进 span event——它体积大、可能含
  隐私，而且我们只记它的**长度**。

```python
def _load_otel(tracer):
    try:
        from opentelemetry import trace      # 函数内 import = 懒加载
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            '结构化 tracing 需要可选依赖 otel，请先安装：pip install "yai-agent-core[otel]"…'
        ) from exc
    if tracer is None:
        tracer = trace.get_tracer("yai-agent-core")
    return trace, tracer
```

逐行看：

- `from opentelemetry import trace` 写在**函数体里**而不是模块顶层，所以"没装 extra"时，
  只有真正开始 trace 才会触发 ImportError，平时 `import yai_core` 完全不受影响。
- ImportError 被换成一句**带安装命令**的中文提示，宿主一看就知道缺什么。
- `tracer is None` 时取全局 provider 的 tracer：宿主只要在进程启动时
  `trace.set_tracer_provider(...)` 配过一次，这里就自动用上；没配过则是 OTel 内置的
  no-op tracer（什么都不报，也不报错）。

## D. `_attr_value`：把任意事件字段净化成 OTel 接受的标量

OTel attribute 的值只接受 str/bool/int/float 以及它们的同质数组，不能直接放 dict/list。

```python
def _attr_value(value):
    if isinstance(value, bool):
        return value                       # 注意 bool 要先判，bool 也是 int 的子类
    if isinstance(value, int | float):
        return value
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):    # pragma: no cover - default=str 下的极端防御
            text = repr(value)
    if len(text) <= _ATTR_LIMIT:
        return text
    return text[:_ATTR_LIMIT] + "…(truncated)"
```

- **bool 必须在 int 之前判断**：Python 里 `True` 同时是 `int`，顺序反了布尔会被当成 1。
- dict/list 用 `json.dumps(..., default=str)` 转成 JSON 字符串；`default=str` 保证连
  datetime、自定义对象都能兜底转字符串，几乎不会抛。
- 超过 2000 字符截断并加 `…(truncated)` 标记，诚实告诉看 trace 的人"这里被裁过"。

## E. `_RunRecorder`：事件 → span 的核心状态机

它持有三样状态：当前 root span、一个"未闭合工具 span"的栈、以及一个 `_failed` 标志。

### E.1 `start`：建 root 并挂为当前 span

```python
def start(self, task):
    self._root = self._tracer.start_span(ROOT_SPAN_NAME)
    self._attach = self._trace.use_span(self._root, end_on_exit=False)
    self._attach.__enter__()
    self._root.set_attribute("yai.task", _attr_value(task))
    for key, value in self._root_attributes.items():
        self._root.set_attribute(str(key), _attr_value(value))
```

`use_span(..., end_on_exit=False)` 把 root 设为"当前 span"但**不**让它随 with 自动结束。
这样之后 `start_span("yai.tool.xxx")` 时，OTel 会自动把新 span 的 parent 指向 root，
父子关系就有了。`root_attributes`（比如 `yai.host=notes`）原样写到 root，用来区分是哪
个宿主应用产生的 trace。

### E.2 `on_event`：三类分发

```python
if etype == EventType.TOOL_CALL:
    self._on_tool_call(data)
elif etype == EventType.TOOL_RESULT:
    self._on_tool_result(data)
else:
    self._on_lifecycle_event(etype, data)
```

工具调用有明确的"开始/结束"两个事件，单独成子 span；其余全是 root 上的瞬时事件。

### E.3 工具 span 的开 / 关与配对容错

开（`TOOL_CALL`）：建 `yai.tool.<name>` 子 span，记工具名和入参，压入栈。

```python
span = self._tracer.start_span(f"{TOOL_SPAN_PREFIX}.{name}")
span.set_attribute("yai.tool", name)
if "arguments" in data:
    span.set_attribute("yai.arguments", _attr_value(data["arguments"]))
self._open_tools.append((name, span))
```

关（`TOOL_RESULT`）：从栈顶往下找**最近的同名** span 关掉。

```python
span = self._pop_open(name)
if span is None:                          # 没有配对的 CALL
    span = self._tracer.start_span(f"{TOOL_SPAN_PREFIX}.{name}")
    span.set_attribute("yai.tool", name)
    span.set_attribute("yai.without_call_event", True)
ok = bool(data.get("ok", True))
span.set_attribute("yai.ok", ok)
if ok:
    if "preview" in data:
        span.set_attribute("yai.preview", _attr_value(data["preview"]))
else:
    error = str(data.get("error", "tool failed"))
    span.set_attribute("yai.error", _attr_value(error))
    span.set_status(self._trace.StatusCode.ERROR, error)
span.end()
```

为什么要"按名字找"而不是简单弹栈顶？因为执行器里工具是**严格串行**的，正常情况下
RESULT 对应的就是栈顶；但有两类工具**不发 `TOOL_CALL`**：

- **组合工具**（composer）：执行器在授权前就分流到逐步编排，只发 `TOOL_COMPOSED` + `TOOL_RESULT`；
- **内核 meta-tool**（request_capability / create_code_tool / replan / delegate）：由 loop 直接处理。

它们也会发 `TOOL_RESULT`。这时栈里找不到开 span，`span is None`，于是**补一个即时子
span** 并打 `yai.without_call_event=True` 标记——保证这条工具结果在 trace 里不丢失，同时
诚实标注"它没有对应的 call 事件"。失败时把 span Status 置 ERROR 并记错误信息。

### E.4 生命周期事件：属性 + span event

```python
if etype == EventType.STRATEGY_SELECTED and "strategy" in data:
    self._root.set_attribute("yai.strategy", str(data["strategy"]))
if etype == EventType.TIER_CHANGED and "tier" in data:
    self._root.set_attribute("yai.tier", str(data["tier"]))
if etype == EventType.DONE:
    self._root.set_attribute("yai.final_text_len", len(str(data.get("final_text", ""))))
    return                                  # DONE 不再加 event
if etype == EventType.ERROR:
    message = str(data.get("error") or _attr_value(data))
    self._failed = True
    self._root.set_status(self._trace.StatusCode.ERROR, message)
attributes = {f"yai.{key}": _attr_value(value)
              for key, value in data.items() if key not in _EVENT_SKIP_KEYS}
self._root.add_event(f"yai.{etype.value}", attributes=attributes)
```

- 最终策略 / 档位是会被后续覆盖更新的"当前状态"，放 **attribute**；
- DONE 只记回复**长度**然后 `return`（不把正文加进 event）；
- ERROR 事件把 root 置失败，`_failed=True`，防止最后又被默认盖成 OK；
- 其余事件（计划、澄清、能力缺口、发现/组合/创建工具、压缩、重规划……）统一转成
  root 上带属性的 **span event**，`final_text` 字段跳过。

### E.5 `finish`：收尾的三种情况

```python
def finish(self, error):
    for _name, span in reversed(self._open_tools):   # ① 流被取消/异常：关掉没等到 RESULT 的 span
        span.set_attribute("yai.unclosed", True)
        span.end()
    self._open_tools.clear()
    if error is not None:                            # ② 流抛异常
        self._root.record_exception(error)
        self._root.set_status(self._trace.StatusCode.ERROR, f"{type(error).__name__}: {error}")
    elif not self._failed:                           # ③ 没异常也没 ERROR 事件 → OK
        self._root.set_status(self._trace.StatusCode.OK)
    self._attach.__exit__(None, None, None)
    self._root.end()
```

- 任务正常跑完时所有工具 span 都已各自 `end()`，栈是空的；但如果流在工具执行**中途**
  被取消或抛异常，那些只开不关的 span 会被统一关掉并打 `yai.unclosed=True`，不留悬挂节点。
- 流本身抛异常时 `record_exception` 记录堆栈快照并置 ERROR。
- 只有"无异常且无 ERROR 事件"才标 OK——`_failed` 让 ERROR 事件的失败结论不会被覆盖。

## F. 两个对外入口

### F.1 `traced_astream`：流式转发 + 埋点

```python
recorder.start(task)
error = None
try:
    async for event in core.astream(task):
        recorder.on_event(event)
        yield event                       # 原样转发，宿主拿到的事件序列与 core.astream 完全一致
except BaseException as exc:              # noqa: BLE001
    error = exc
    raise                               # 关键：记录完照样上抛，不吞异常
finally:
    recorder.finish(error)
```

这是一个**异步生成器**：先 `on_event` 记账、再 `yield` 把事件原样交给宿主，所以前端实时
渲染不受影响。`except BaseException` 里先记下异常再 `raise`——tracing 绝不能改变程序的
错误行为；`finally` 保证无论成功、失败、取消，span 都会被正确收尾。

### F.2 `traced_run`：聚合为 RunResult

它内部 `async for traced_astream(...)`，把事件收集成列表、从 DONE 取最终文本、从
`strategy` 字段取最终策略，最后返回与 `core.run()` 同构的 `RunResult`。策略缺失时用
`core.router.classify(...)` 兜底（正常 run 必发 STRATEGY_SELECTED，兜底只是防御）。

## G. 宿主怎么真正看到 trace（SDK 在宿主侧）

库不碰 provider，宿主在进程启动处配一次（详见 `docs/tracing.md`）：

```python
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

provider = TracerProvider()
provider.add_span_processor(BatchSpanProcessor(
    OTLPSpanExporter(endpoint="http://localhost:4318/v1/traces")))
trace.set_tracer_provider(provider)
# 之后 await traced_run(core, "任务") 即可，tracer 不传也会走全局 provider
```

本地 Jaeger：`docker run --rm -p 16686:16686 -p 4318:4318 jaegertracing/all-in-one`，
跑完打开 http://localhost:16686 就能看到 `yai.agent.run` 这棵树。

## H. 离线测试：不连 collector 也能断言整棵树

`tests/test_tracing_otel.py` 用 SDK 自带的 `InMemorySpanExporter` 把 span 收在内存里：

```python
provider = TracerProvider()
exporter = InMemorySpanExporter()
provider.add_span_processor(SimpleSpanProcessor(exporter))
tracer = provider.get_tracer("test")
# 用 ScriptedModel 跑 traced_astream/traced_run，然后 exporter.get_finished_spans() 断言
```

覆盖了：root 与工具子 span 的父子关系、成功预览/失败 ERROR、流抛异常时 root ERROR 且
记录 exception、direct 直答只有 root、属性转换与截断、组合工具 orphan 补 span、流中断的
unclosed 收尾、ERROR 事件不被 OK 覆盖、不传 tracer 回退全局。全程**不需要 API Key、不连
任何后端**；没装 `[otel]` 时需要 SDK 的用例自动跳过，唯独"缺依赖要给安装提示"始终运行。

## 自检（读完本篇应能回答）

1. 为什么 tracing 不需要修改 `kernel/loop.py`？它从哪里拿到运行过程？
2. root span 和工具子 span 的父子关系是怎么自动建立的？
3. 组合工具 / meta-tool 只发 `TOOL_RESULT` 不发 `TOOL_CALL`，tracing 如何保证不丢这条观测？
4. 流在工具执行中途被取消，已经开了但没关的工具 span 会怎样？
5. 为什么这个模块不创建 TracerProvider/Exporter？真正配置 Jaeger 地址的代码应该写在哪？
6. 为什么最终回复正文不放进 trace，只放 `yai.final_text_len`？
