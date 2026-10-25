# 逐行讲解 38 · 运行指标（metrics）与就绪探针（readiness）

> 对应文件：
> - `src/yai_core/metrics.py`（**新建**，零依赖 `MetricsRecorder`：runs / 策略 /
>   工具 / 模型档位 / 事件计数 + run 时延直方图 + Prometheus 文本渲染）；
> - `src/yai_core/core.py`（`astream` 唯一出口旁路 tap：开始 / 逐事件 / 结束结算，
>   新增 `metrics_snapshot()` 与三个 `_metrics_*` 容错方法）；
> - `src/yai_core/config.py`（`CoreConfig.metrics` 注入与鸭子类型校验）；
> - `src/yai_core/batteries/fastapi_server/readiness.py`（**新建**，探针协议、
>   并发执行器、超时 / 异常隔离、默认探针）；
> - `src/yai_core/batteries/fastapi_server/app.py`（新增 `GET /readyz`、
>   `GET /metrics`，`/metrics` 鉴权开关 `YAI_METRICS_PUBLIC`）；
> - `src/yai_core/integrations/metrics/otel_meter.py`（**新建**，可选 `[otel]`
>   extra，observable 仪器采集时从 snapshot 拉值，不初始化 Provider）；
> - 离线测试：`tests/test_metrics_recorder.py`、
>   `tests/test_readiness_metrics_api.py`、`tests/test_otel_metrics_bridge.py`。
>   全部零网络、零 API Key。
>
> 读前先读第 09 篇（FastAPI Battery）、第 14 篇（限流）、第 33 篇（OTel tracing）、
> 第 34 篇（生产加固，astream 唯一出口）。本篇是第 33 篇 tracing 的"指标双胞胎"：
> tracing 回答"一次请求内部发生了什么"，metrics 回答"这段时间整体跑得怎么样"。

---

## A. 先看结论：为什么内核要自带 metrics

你最怕的一件事是"别人花我的钱"。模型按 token 计费，一个在线 Agent 如果不统计
**跑了多少次、调了多少回强档模型、工具失败率多高、时延多长**，那它就是个黑盒：
被刷了不知道、强档模型被滥用不知道、工具一直报错也不知道。

但内核又**不能硬依赖** Prometheus 客户端（那会破坏"零硬依赖、import 即用"）。
所以做法和 tracing 完全一致：

1. 内核自带一个**只用标准库**的 `MetricsRecorder`，在 run 热路径上累加计数；
2. 它能直接吐出 Prometheus 文本格式（`/metrics`），不装任何额外依赖就能被抓；
3. 想接 OpenTelemetry / Datadog / 云监控？走可选 `[otel]` extra 的薄桥接，
   **采集时**才从收集器拉一次快照，run 热路径上零额外回调。

| 改动 | 解决的问题 | 一句话做法 |
|---|---|---|
| `MetricsRecorder` | 在线 Agent 的成本 / 健康是黑盒 | 标准库计数器 + 直方图，一把锁，`snapshot()` 深拷贝 |
| astream tap | 指标必须在唯一出口采集，不能散落在 loop 各处 | 开始打点、逐事件累计、`finally` 按结果分类结算 |
| 失败 / 取消分类 | 只数"成功"会掩盖崩溃和被取消 | 见 DONE=completed、非取消异常=failed、取消类=cancelled |
| 模型档位计数 | 强档模型贵，必须单独算 | 从 DONE 事件的 `standard_calls/strong_calls` 累加 |
| `/readyz` | liveness 不能查下游（会引发重启风暴） | 独立就绪探针，失败只摘流量不重启，探针并发 + 超时隔离 |
| `/metrics` | Prometheus 要能直接抓 | 默认文本 exposition，`?format=json` 给人看，鉴权可开关 |
| OTel meter 桥接 | 公司用 OTel 收集，不想自己解析文本 | observable 仪器 pull snapshot，不初始化 Provider |

纪律不变：**内核零硬依赖**（收集器只用 `threading/time/copy`）；**旁路不反噬**
（指标回调抛任何异常都被吞掉，绝不影响 agent 主流程）；**默认探针不花钱**
（只数本地工具 / 报告记忆后端，绝不调模型、不发网络请求）。

---

## B. 计数口径：先把"数什么"定清楚

指标最怕"数得挺多但口径含糊"。`metrics.py` 顶部先定义两类常量。

```python
_CANCELLED_TYPES = (asyncio.CancelledError, GeneratorExit, KeyboardInterrupt)
_DURATION_BUCKETS = (0.5, 1, 2.5, 5, 10, 20, 30, 60)
```

- `_CANCELLED_TYPES`：这三类都表示"run 没有正常跑完，但不是 bug"——客户端断开、
  生成器被关闭、进程收到中断信号。它们要和真正的异常（`RuntimeError`、模型报错）
  **分开计数**，否则一次正常的超时取消会被误报成故障。
- `_DURATION_BUCKETS`：run 时延直方图的上界（秒）。直方图不存每次的精确耗时
  （那要无界内存），只数"有多少次落在每个区间"，`+Inf` 桶等于总样本数。

收集器内部维护的状态（全部是普通 dict / int / float）：

```python
self._runs = {"started": 0, "completed": 0, "failed": 0, "cancelled": 0}
self._runs_by_strategy: dict[str, int] = {}
self._tools: dict[str, dict[str, int]] = {}     # name -> {"total": n, "errors": n}
self._model_calls = {"standard": 0, "strong": 0}
self._events: dict[str, int] = {}
self._hist = {"count": 0, "sum": 0.0, "buckets": {每个上界: 0, "+Inf": 0}}
```

为什么工具计数拆 `total/errors` 而不是 `ok/error`？因为 `total - errors = ok`，
存两个原始量（总调用、失败数）在并发累加时不会出现 ok+error≠total 的口径裂缝。

### B.1 为什么用一把 `threading.Lock`

内核是 asyncio 单线程事件循环，理论上"没有真正的并行"。但：

- `snapshot()` 可能在**另一个线程**被调用（Prometheus client、OTel collector 的
  采集线程、桌面 UI 的刷新线程都不是 agent 所在的事件循环线程）；
- 采集线程遍历 dict 的同时，事件循环线程正在往 dict 里写新 key，会抛
  `RuntimeError: dictionary changed size during iteration`。

所以所有读写都包在 `with self._lock:` 里。锁内只做整数加减和 dict 操作（纳秒级），
不 await、不调模型，不会成为瓶颈。这是"多线程读、单线程写"场景的标准防护。

---

## C. `MetricsRecorder` 的四个公开方法

### C.1 `on_run_start()`：开始打点

```python
def on_run_start(self) -> None:
    with self._lock:
        self._runs["started"] += 1
        self._active_started = True
        self._saw_done = False
        self._start_mono = time.monotonic()
```

- 用 `time.monotonic()` 而不是 `time.time()`：前者是单调时钟，**不受系统时间被
  NTP 回拨 / 手动改时间影响**，测时延永远非负、不会出现负数。
- `_active_started` 是"当前确实有一个 run 在进行"的标志。它让 `snapshot()` 算出的
  uptime（进程活了多久）和 run 计数解耦——uptime 从收集器创建起算，跟有没有请求无关。

### C.2 `record_event(event)`：逐事件累计

这是核心。它把 `AgentEvent` 翻译成计数器增量，全部是**只读事件流**的旁路消费：

```python
def record_event(self, event: AgentEvent) -> None:
    with self._lock:
        self._events[event.type.value] = self._events.get(event.type.value, 0) + 1
        data = event.data or {}
        if event.type is EventType.STRATEGY_SELECTED:
            strategy = str(data.get("strategy", "unknown"))
            self._runs_by_strategy[strategy] = ... + 1
        elif event.type is EventType.TOOL_CALL:
            name = str(data.get("tool", "unknown"))
            self._tools.setdefault(name, {"total": 0, "errors": 0})["total"] += 1
        elif event.type is EventType.TOOL_RESULT:
            ok = bool(data.get("ok", True))      # 缺省视为成功，宽容脏数据
            if not ok: self._tools[name]["errors"] += 1
        elif event.type is EventType.DONE:
            self._accumulate_done(data)          # 模型档位在 DONE 一次性结算
            self._settle_run_locked("completed")
```

注意几个**容错口径**（都有测试守护）：

- 工具名缺失用 `"unknown"` 兜底，不崩；
- `TOOL_RESULT` 没有 `ok` 字段时**默认成功**——观测代码不能因为事件少个字段就误报失败；
- `standard_calls` 被脏数据写成 `"oops"` 时，`_as_int()` 会归 0 而不是抛 `ValueError`。

### C.3 模型档位为什么在 DONE 才算

模型在一次 run 里可能被调很多回（ReAct 每轮一次、重规划再来一次），而且 loop 内部
才知道每次走的是 standard 还是 strong。最干净的口径是：loop 在**结束的 DONE 事件**
里带上本次累计的 `standard_calls / strong_calls`（第 32 篇选档已经在记），收集器
只在 DONE 读一次。这样：

- 不用在每次 `MODEL_MESSAGE` 里猜档位（事件里未必带）；
- 一次 run 的模型成本在 DONE 原子地结算，不会半途中重复计。

### C.4 `on_run_finish(exc)`：按结果分类

```python
def on_run_finish(self, exc: BaseException | None) -> None:
    with self._lock:
        if not self._active_started:
            return
        elapsed = time.monotonic() - self._start_mono
        self._record_duration_locked(elapsed)
        if self._saw_done:
            outcome = None                       # completed 已在 DONE 结算
        elif isinstance(exc, _CANCELLED_TYPES):
            outcome = "cancelled"
        else:
            outcome = "failed" if exc is not None else None
        if outcome:
            self._runs[outcome] += 1
        self._active_started = False
```

三个分支正好对应一次 run 的三种结局：

1. **见过 DONE**：completed 在 `record_event(DONE)` 时已经 +1，这里只补时延，不重复计
   （有测试 `test_done_then_finish_does_not_double_count` 守护）；
2. **没见 DONE 且异常是取消类**：cancelled；
3. **没见 DONE 且是其他异常**：failed。

时延无论哪种结局都记录（失败 / 取消的耗时也有运维价值，比如"总是卡在 30 秒超时"）。

### C.5 `snapshot()`：深拷贝隔离

```python
def snapshot(self) -> dict:
    with self._lock:
        return copy.deepcopy({...})
```

返回**深拷贝**而不是内部 dict 的引用。否则调用方（比如 `/metrics?format=json`
把它 `json.dumps` 出去、或 UI 拿到后改着玩）改了返回值，就会污染收集器内部状态。
有测试故意把 `snap["runs"]["completed"] = 999`，再取一次快照验证仍是原值。

---

## D. Prometheus 文本渲染：不装客户端也能被抓

Prometheus 的拉取协议本质就是"HTTP GET 一段纯文本"。`render_prometheus()` 手拼
这段文本，所以**零依赖**也能对接整个 Prometheus 生态。

计数器 / gauge 用一个内部小helper 统一输出 `# HELP`、`# TYPE`、样本行；直方图按
Prometheus 规范输出三组名字：

```text
# HELP yai_run_duration_seconds Agent run wall-clock duration in seconds.
# TYPE yai_run_duration_seconds histogram
yai_run_duration_seconds_bucket{le="0.5"} 12
yai_run_duration_seconds_bucket{le="1"} 15
...
yai_run_duration_seconds_bucket{le="+Inf"} 16
yai_run_duration_seconds_sum 23.75
yai_run_duration_seconds_count 16
```

关键点：`# TYPE` 行用基础名 `yai_run_duration_seconds`，但数据行必须加后缀
`_bucket{le=...}` / `_sum` / `_count`——这是 Prometheus parser 识别直方图的硬性
约定（本篇实现时这里曾漏写 `_bucket`，被测试抓出来修正）。

标签值统一过 `_escape()`（转义反斜杠、换行、双引号），防止工具名里带个 `"` 把
文本格式破坏掉；数字过 `_fmt_number()` / `_as_int()`，保证永远输出合法数值。

---

## E. core.py：在唯一出口旁路 tap

指标采集点选在哪，决定了会不会漏数。第 34 篇已经把 `astream` 确立为**所有事件的
唯一出口**（run 只是消费 astream），所以 tap 只加在这一个地方：

```python
async def astream(self, task):
    run_error: BaseException | None = None
    self._metrics_start()                       # 开始
    try:
        async with self._run_lock:              # 实例级串行锁（第 34 篇）
            async for event in self._loop.astream(...):
                self._metrics_event(event)      # 逐事件
                yield event
    except BaseException as exc:
        run_error = exc
        raise
    finally:
        self._metrics_finish(run_error)         # 结束结算（一定执行）
        ...
```

- `except BaseException` 而不是 `except Exception`：`CancelledError` 在 Python 3.8+
  继承 `BaseException`，要捕获它来正确分类为 cancelled，就必须接 BaseException；
  捕获后**原样 raise**，不吞取消。
- `finally` 保证无论成功、异常、取消，`on_run_finish` 一定被调用，时延一定结算。
- 三个 `_metrics_start/_event/_finish` 内部各自 `try/except Exception: pass`。
  指标是旁路，**收集器自己有 bug 也不能拖垮用户的 agent**——这和 diagnostics SPI
  （第 34 篇）的容错口径完全一致。

`CoreConfig.metrics` 允许宿主注入**自定义收集器**（比如想把计数直接发到公司监控
agent），`config.validate()` 用鸭子类型检查它必须有 `on_run_start / record_event /
on_run_finish / snapshot` 四个可调用方法，缺一个就在构造期抛 `ValueError`，而不是
跑到一半才 `AttributeError`。不传则默认新建一个标准 `MetricsRecorder`。

---

## F. readiness.py：liveness 与 readiness 必须分家

这是运维新手最容易踩的坑，也是本篇的重点概念。

| 探针 | 问题 | 失败后果 | 该不该查下游 |
|---|---|---|---|
| **liveness** `/health` | 进程还活着吗？ | 重启 / 重建 Pod | **绝不**。查下游会因一次数据库抖动触发重启风暴，越重启越起不来 |
| **readiness** `/readyz` | 现在能接请求吗？ | 移出负载均衡（不重启） | **可以**。DB 连不上、模型端点不通就摘流量，恢复后自动加回 |

`/health` 早已存在且刻意保持宽松（只回 commit）。本篇新增的 `/readyz` 才是可以
认真检查依赖的地方。

### F.1 探针协议：返回值约定

宿主传一个 `{名字: 无参函数}` 的 dict，函数可以是同步或 async，返回值约定：

- `None` / `True`：就绪；
- `False`：未就绪（无详情）；
- `str`：未就绪，字符串作为失败原因；
- `dict`：`{"ok": bool, ...}`，`ok` 缺省 True，其余键原样回显；
- **抛异常**：未就绪，详情自动写成 `异常类型: 异常消息`。

用"普通函数 + 宽松返回值"而不是要求宿主继承某个基类，是为了让加一个探针像写一行
`lambda: ping_db() or "db down"` 一样轻。

### F.2 并发 + 超时 + 异常隔离

```python
async def run_probes(probes, *, timeout=DEFAULT_PROBE_TIMEOUT):
    results = await asyncio.gather(
        *(_run_one(name, probe, timeout) for name, probe in probes.items())
    )
    checks = {name: outcome for name, outcome in results}
    ready = all(outcome["status"] == "ok" for outcome in checks.values())
    return ready, checks
```

三个隔离保证：

1. **并发**：`asyncio.gather` 同时跑所有探针，总耗时≈最慢的一个，不是之和；
2. **超时**：每个探针包 `asyncio.timeout(..., timeout)`，默认 5 秒。一个探针
   `while True` 死循环也不会让 `/readyz` 永远挂起——超时按未就绪计；
3. **异常隔离**：`_run_one` 内部 `try/except`，探针抛任何异常都转成该探针的
   `{"status":"error"}`，**绝不向上抛**。一个写崩的探针不会让整个 500。

### F.3 默认探针：只报告、不花钱

`default_probes(core)` 给两个恒为就绪、零副作用的探针，演示"就绪探针该长什么样"
的同时提供有用状态：

- `tools`：`{"ok": True, "registered_tools": N}`——0 个工具也是合法宿主（纯对话），
  所以只报告数量，不因为没工具就判失败；
- `memory`：`{"ok": True, "backend": "InMemoryStore", "long_memory": False}`——
  报告记忆后端类型和长记忆是否开启。

真正的下游探活（`SELECT 1`、模型 `GET /models`、MCP 握手）由宿主经
`create_app(readiness_probes={...})` 注册，内核不替你瞎连。

---

## G. app.py：两个新端点与鉴权

```python
@app.get("/readyz")
async def readyz():
    ready, checks = await run_probes(probes, timeout=readiness_timeout)
    return JSONResponse(
        {"status": "ready" if ready else "not_ready", "checks": checks},
        status_code=200 if ready else 503,
    )

@app.get("/metrics")
async def metrics(format: str | None = None):
    if format == "json":
        return JSONResponse(core.metrics_snapshot())
    return PlainTextResponse(core.metrics.render_prometheus(),
                             media_type="text/plain; version=0.0.4; charset=utf-8")
```

- `/readyz` 全部探针 ok 才 200，否则 503——负载均衡器（k8s / Caddy / Render）
  只看状态码决定是否导流；
- `/metrics` 默认吐 Prometheus 文本（`media_type` 带 `text/plain; version=0.0.4`
  是 Prometheus 约定的 exposition 版本号），加 `?format=json` 给人 / 调试用；
- 限流中间件只拦 `POST /v1/agent/run`，三个 GET（health / readyz / metrics）
  **任何状态下都不被限流**——探针每 10 秒抓一次，绝不能被 429。有测试连打 60 次
  GET 验证全部 200。

### G.1 `/metrics` 的鉴权开关

指标里可能暴露工具名、策略分布等内部信息，所以**配置了 API Key 时 `/metrics`
默认也要鉴权**；但 Prometheus 抓取端配 Bearer token 有时比较麻烦，于是给了一个
显式开关：

- 设了 `YAI_API_KEY`：`/metrics` 默认 401，带 Key 才 200；
- 再设 `YAI_METRICS_PUBLIC=1`：`/metrics` 显式公开（内网 / 想被白嫖看健康度时）；
- `/health` 与 `/readyz` **恒公开**——编排系统抓探针通常不带 Key，且它们不含敏感数据。

注意 `/v1/tools` 等业务路径不受 `YAI_METRICS_PUBLIC` 影响，仍然要 Key（有测试
守护这个边界，防止"为了公开指标不小心把业务也公开了"）。

---

## H. OTel meter 桥接：采集时 pull，不订阅

第 33 篇 tracing 是"外侧包一层 `traced_astream` 消费事件"。metrics 更适合另一种
模式——**observable 仪器 + 采集时回调**：

```python
def runs_callback(_options):
    runs = core.metrics_snapshot()["runs"]
    return [Observation(runs[outcome], {"outcome": outcome})
            for outcome in ("started", "completed", "failed", "cancelled")]

meter.create_observable_counter("yai.agent.runs", callbacks=[runs_callback], ...)
```

`create_observable_counter` 注册的回调**只在后端每次采集（scrape / collect）时**
执行一次，从 `core.metrics_snapshot()` 拉最新值。好处：

- run 热路径上**完全没有** OTel 代码，挂不挂 OTel 对性能零影响；
- 不初始化 `MeterProvider / Reader / Exporter`——那是宿主进程的职责（OTel 惯例），
  本模块只做 instrumentation library，与 tracing 一致；
- 未装 `[otel]` extra 时调用，给出明确的 `pip install "yai-agent-core[otel]"` 提示。

OTel 没有"observable histogram"（histogram 只支持事件发生时主动 record），而我们
坚持 pull 模式，于是用一组**累计桶 observable counter**（`le` 标签，含 `+Inf` 即
count）加一个 `.sum` counter 来表达时延分布，信息与 `/metrics` 的直方图等价。

测试用 SDK 的 `InMemoryMetricReader` + `MeterProvider`（不联网），注册桥接、跑一次
run、`reader.get_metrics_data()` 触发回调，断言 `yai.agent.runs{outcome=completed}`、
`yai.agent.model.calls{tier=standard}`、`yai.agent.run.duration{le=+Inf}`、
`yai.agent.uptime` 等数据点都采得到。

---

## I. 部署侧怎么用（速览）

- **k8s**：`livenessProbe: GET /health`，`readinessProbe: GET /readyz`；
- **Caddy / Nginx / Render**：健康检查 URL 填 `/readyz`（决定是否导流）；
- **Prometheus**：scrape `GET /metrics`；若配了 Key，在 scrape_config 的
  `authorization: credentials: <key>` 里带 Bearer，或设 `YAI_METRICS_PUBLIC=1`
  走内网公开；
- **多实例水平扩展**：每个进程的 `/metrics` 只报告**本进程**计数（per-process），
  Prometheus 按实例标签分别抓取后在查询层 `sum by (outcome)` 聚合——内核不做跨进程
  状态共享（那会重新引入中心化依赖，违背嵌入式定位）。详见 `docs/deployment-scaling.md`。

---

## J. 自检（读完本篇应能回答）

1. 为什么 run 计数要分 completed / failed / cancelled 三类，而不是只数成功？
2. `CancelledError` 继承 `BaseException`，这对 `astream` 的 `except` 写法有什么要求？
3. 为什么模型档位计数放在 DONE 事件，而不是每次模型调用时立刻累加？
4. `snapshot()` 为什么必须返回深拷贝？
5. liveness 和 readiness 各自回答什么问题？为什么 liveness 不能检查数据库？
6. 一个探针写了死循环，`/readyz` 会发生什么？哪个机制保证它不会挂死？
7. 配了 `YAI_API_KEY` 后，`/health`、`/readyz`、`/metrics`、`/v1/tools` 分别
   要不要鉴权？`YAI_METRICS_PUBLIC=1` 改变其中哪一个？
8. OTel 桥接为什么用 observable 仪器在采集时 pull，而不是在 run 热路径上主动 record？
9. Prometheus 直方图的三组数据行后缀分别是什么？`# TYPE` 行用哪个名字？
10. 为什么指标回调里的异常要全部吞掉？如果不吞，最坏会发生什么？
