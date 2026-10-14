# 逐行讲解 34 · 生产加固三件套：并发串行契约、模型硬超时、零依赖诊断

> 对应文件：
> - 并发：`src/yai_core/core.py`（实例级 `asyncio.Lock`，`astream` 全程持锁）；
> - 超时：`src/yai_core/kernel/loop.py`（`model_timeout` + 统一入口 `_model_chat`）、
>   `kernel/compaction.py`（压缩超时）、`shell/desktop/sandbox.py` 与
>   `shell/desktop/docker_sandbox.py`（asyncio 原生子进程 + 取消即 kill）；
> - 诊断：`src/yai_core/diagnostics.py`（新文件，零依赖），接线进 `loop.py`、
>   `router.py`、`llm/fallback.py`、`compaction.py`、`core.py`；
> - 离线测试：`tests/test_core_concurrency.py`、`tests/test_model_timeout.py`、
>   `tests/test_diagnostics.py`（全部零网络、零 API Key）。
>
> 读前先读第 6（loop 主循环）、8（core 门面）、25（压缩）、33（tracing）。
> 这三件事共同回答一个问题：**"演示能跑"和"长期挂着不炸"之间，还差什么？**

## A. 先看清三件事各自防的是什么

| 加固 | 防的故障 | 一句话做法 |
|---|---|---|
| P0-1 并发串行契约 | 两条任务在 `await` 点交错，互相踩脏共享的上下文/记忆/注册表/学习态 | 一个 `AgentCore` 实例一把锁，任务全程串行 |
| P0-2 模型硬超时 | 模型接口挂死，任务永远卡在 `await model.achat(...)`；用户取消传不进去 | loop 层用 `asyncio.wait_for` 统一兜底，超时/取消都清理 |
| P0-3 本地诊断 | 增强环节悄悄降级（回退规则、发现失败、压缩回退），现场却什么都没留下 | 一个零依赖、永不反噬的旁路回调，把"降级瞬间"喊出来 |

三者都遵守同一条内核红线：**零第三方硬依赖、行为默认与旧版一致（不传开关就安静/安全）、
全部能用 ScriptedModel/Fake 组件离线测。**

---

## B. P0-1：为什么要"恒串行"，而不是加个并发开关

asyncio 是**单线程协作式**并发：同一线程里的两段协程，只有在 `await` 让出点才可能交错。
YAI 的一次 `astream` 里有大量 `await`（模型调用、工具执行、发现、沙箱）。如果同一个
`AgentCore` 实例上同时跑两条任务，它们会共享这些**可变状态**：

- `Context`（组装中的消息列表）；
- `MemoryStore`（历史读写）；
- `ToolRegistry`（发现/造工具会动态注册）；
- 路由学习器 `learning`、代码工具管理器 `code_tool_manager`。

交错的后果不是"报错"那么简单，而是**静默的数据污染**：A 任务发现的工具被 B 看到、
A 的历史混进 B 的上下文。这类 bug 极难复现。所以内核不暴露"并发开关"，直接在
`core.py` 里把串行做成硬契约：

```python
# core.py __init__
self._task_lock = asyncio.Lock()

# astream：整个任务体（含 finally 里的学习反馈回灌）都包在锁里
async def astream(self, task):
    async with self._task_lock:
        async for event in self._loop.astream(task):
            yield event
        # finally 的反馈回灌也在锁内，保证"一次任务"完整原子
```

几个关键细节，逐块看：

1. **锁跨 `yield` 持有**。`async with` 包住了 `yield`，意味着事件一条条往外吐的整个过程中
   锁都不释放——这正是我们要的：任务没结束，状态就还属于它。
2. **生成器被提前关闭会自动释放**。宿主 `break` 跳出 `async for`、或显式 `aclose()` 时，
   `async with` 的 `__aexit__` 仍会执行，锁不会泄漏。这也是
   `tests/test_model_timeout.py` 里"取消后同一 core 还能接下一单"断言的来源。
3. **要并发就多实例**。多租户/多专员/多子员工各自 `AgentCore(...)`，各自带独立的
   registry/memory/learning；无状态的模型客户端、发现源、沙箱可以做成单例被共享。
   灵动岛的每个专员、`shell/employee` 的每个子员工正是这样天然并行的。

> 设计取舍：为什么不用线程锁/队列/分布式锁？因为内核是进程内库，水平扩展被简化成
> "单机多会话=一进程多实例，跨机器=多副本+共享无状态后端"。内核不替宿主引入它不需要的
> 并发基础设施。

---

## C. P0-2：超时放在 loop 层，而不是改 SPI 签名

### C.1 一个关键决定：不给 `ModelProvider.achat` 加 `timeout` 形参

SPI 契约 `achat(messages, tools, *, tier="standard")` 已经被多个实现（OpenAI 兼容、
Scripted、Fallback、宿主自定义）实现。一旦给契约方法加 `timeout` 形参，所有实现都要改，
宿主自己写的 provider 还会签名不兼容。

所以超时放在**调用方**——`AgentLoop` 用 `asyncio.wait_for` 统一包裹：

```python
# loop.py __init__
DEFAULT_MODEL_TIMEOUT = 60.0
def __init__(..., model_timeout: float | None = DEFAULT_MODEL_TIMEOUT):
    self._model_timeout = model_timeout

# 唯一的模型调用入口：5 处 achat 全部改走这里
async def _model_chat(self, messages, tools, *, tier):
    coro = self.model.achat(messages, tools, tier=tier)
    if self._model_timeout is None:
        return await coro            # 显式关闭，交给模型客户端自身超时
    return await asyncio.wait_for(coro, timeout=self._model_timeout)
```

- `None` = 不设内核级超时（宿主已在客户端层配好超时的场景）；
- 超时抛 `TimeoutError`（`wait_for` 原生），消息里带"超时"二字，最终被翻译成一条
  `ERROR` 事件体面收尾，而不是裸异常穿透宿主；
- **`CancelledError` 不被吞**。它继承自 `BaseException` 而非 `Exception`，外层
  `except Exception` 抓不到它，所以用户主动取消（外层 `task.cancel()`）会干净地一路传播，
  同时 `wait_for` 会先把取消真正送达挂起的模型调用——测试用一个记录 `cancelled` 的
  `SlowThenOk` 模型验证了这一点。

### C.2 压缩超时：增强失败必须能回退

`compact_context(..., timeout=None)` 也加了 keyword-only `timeout`。摘要压缩是**增强**
不是主路径：超时或模型报错时 `except` 里回退到 `Context.enforce_budget()` 硬裁剪，
保证上下文一定被收进预算，任务继续。

### C.3 沙箱：取消一个子进程，不能只取消协程

P0-2 顺手把三份沙箱（教学 `SubprocessSandbox`、桌面封装、`DockerSandbox`）重构成
**asyncio 原生子进程** `create_subprocess_exec`，并用 `wait_for(communicate())` 守时。
关键是超时/取消后的清理：

```python
try:
    out, err = await asyncio.wait_for(proc.communicate(), timeout)
except (asyncio.TimeoutError, asyncio.CancelledError):
    proc.kill()                 # 先杀进程
    await proc.communicate()    # 再排空 transport，否则 Windows 报 unclosed transport
    raise
# Docker 还要 _force_remove() 把一次性容器删掉，不留残渣
```

只取消 Python 协程、不杀底层进程，会让真实的 python/docker 进程继续在后台跑、占着资源。
"取消即 kill、kill 后排空"是这一段的核心纪律。

---

## D. P0-3：逐行读 `diagnostics.py`（整个文件只有三十几行）

```python
class DiagnosticLevel(StrEnum):
    DEBUG = "debug"; INFO = "info"; WARNING = "warning"; ERROR = "error"

class DiagnosticKind(StrEnum):
    MODEL_ERROR = "model_error"
    MODEL_TIMEOUT = "model_timeout"
    MODEL_FALLBACK = "model_fallback"
    ROUTER_LLM_FALLBACK = "router_llm_fallback"
    DISCOVERY_FAILED = "discovery_failed"
    DISCOVERABLE_LIST_FAILED = "discoverable_list_failed"
    COMPACTION_FALLBACK = "compaction_fallback"
    SCHEMA_FOCUS_FALLBACK = "schema_focus_fallback"

@dataclass
class DiagnosticEvent:
    kind: str
    message: str
    level: DiagnosticLevel = DiagnosticLevel.WARNING
    data: dict = field(default_factory=dict)

DiagnosticSink = Callable[[DiagnosticEvent], None]   # 同步回调

def emit(sink, event):
    if sink is None:
        return
    try:
        sink(event)
    except Exception:   # noqa: BLE001  诊断是旁路，回调炸了绝不能反噬主流程
        pass
```

逐块解释设计意图：

1. **为什么用 `StrEnum` 而不是裸字符串**：`event.kind == DiagnosticKind.MODEL_TIMEOUT`
   既能类型检查，序列化成 JSON 时又是 `"model_timeout"` 字符串，日志/监控直接可读。
2. **为什么回调是同步的**：诊断发生在事件循环线程里，异步回调反而要再调度、可能乱序。
   写一行日志、`counter += 1` 都是微秒级同步操作；宿主想异步上报，自己在回调里
   `create_task` 即可。
3. **为什么 `emit` 要吞掉回调异常**：日志/监控系统挂了，不应该让 Agent 主任务跟着崩。
   这是"旁路"二字的落地。
4. **为什么不 `import logging`**：内核本体零硬依赖，也不预设宿主的日志栈（有人用
   loguru、structlog、stdout）。给一个回调，接什么由宿主决定。

### D.1 三套可观测，别混用

| 通道 | 面向 | 回答 |
|---|---|---|
| `AgentEvent` | 产品/用户 | Agent **正在做什么**（必经、可回放，第 6 篇） |
| `DiagnosticEvent` | 本地运维 | 内核**在哪里悄悄降级了**（传了回调才有） |
| OpenTelemetry | 分布式链路 | 一次请求跨进程的**耗时与归因**（第 33 篇，外侧订阅） |

### D.2 8 种 kind 分别在哪个降级点发出

- `model_error` / `model_timeout`：模型调用抛错/超时（loop 顶层兜底 stage=`model`，
  react 主轮局部捕获 stage=`react`）；
- `model_fallback`：`FallbackModelProvider` 的某个后端可重试失败、降级到下一个；
- `router_llm_fallback`：LLM 分类器异常/超时/非法输出，回退关键词规则；
- `discovery_failed`：动态发现 `discover()` 抛错（仍发 ERROR 业务事件并继续）；
- `discoverable_list_failed`：拉"可发现目录"失败（静默回退到不展示目录）；
- `compaction_fallback`：摘要压缩失败或返回空摘要，回退硬裁剪（WARNING/DEBUG）；
- `schema_focus_fallback`：工具 schema 聚焦失败，退回注入完整 schema（DEBUG）。

### D.3 一处传入、鸭子接线

宿主只在门面传一次：

```python
core = AgentCore(model, on_diagnostic=lambda ev: print(ev.level, ev.kind, ev.message))
```

`AgentLoop.__init__` 收到后，用鸭子类型把同一回调接到内部组件：

```python
if hasattr(self.router, "on_diagnostic"):
    self.router.on_diagnostic = on_diagnostic
if hasattr(self.model, "on_diagnostic"):
    self.model.on_diagnostic = on_diagnostic
```

用 `hasattr` 而不是强类型，是因为宿主可能传入自定义 router/model（不认识这个属性）；
鸭子接线对它们零影响。`AdaptiveRouter` 与 `FallbackModelProvider` 都暴露了可写的
`self.on_diagnostic`。**不传回调时整条链路是 `None`，`emit(None, ...)` 直接返回，
行为与旧版完全一致。**

### D.4 刻意不发诊断的情况

沙箱执行失败会冒泡成 `tool_result(ok=False)`、权限被拒会发 `permission_asked`——
这些**本来就是用户可见的业务事件**，不是"静默兜底"，再发一条诊断只会重复，所以不接。

---

## E. 离线测试怎么把这三件事测实（不需要真实模型）

- **并发**（`test_core_concurrency.py`）：两个任务 `create_task` 并发跑，用带
  `asyncio.Event`/延迟的 ScriptedModel 制造 `await` 交错点，断言二者的执行区间**不重叠**、
  共享状态不串；再断言 `aclose()` 后锁释放、能接下一单。
- **超时/取消**（`test_model_timeout.py`）：`SlowThenOk` 第一次 sleep 1s 并记录
  `cancelled`，`model_timeout=0.1` 时断言约在 0.1s 终止、收到 ERROR、模型协程确实被取消；
  `model_timeout=None` 时慢响应照常返回；外层 `cancel()` 时 `CancelledError` 干净传播。
- **诊断**（`test_diagnostics.py`）：自定义 `_SleepModel/_BoomModel/_ConnErrorModel/
  _OkModel/_BrokenDiscovery`，分别触发超时、路由回退、发现失败、多模型降级、压缩回退，
  断言回调收到的 `kind/level/data`；并断言回调抛错被吞、`AgentCore` 把回调注入了
  router 与 FallbackModelProvider。

> 注意一个容易写错的断言：无工具的直答任务（如 `"你好"`）模型调用发生在 **react 主轮**，
> 超时诊断的 `data["stage"]` 是 `"react"`；只有规划/收尾等未被局部捕获的模型异常才是
> 顶层的 `"model"`。

---

## F. 后续追加的两件：工具 handler 超时与 `AgentCore.aclose`

模型侧有了硬超时之后，工具侧长期是裸的：一个不返回的 OpenAPI 调用能把整轮任务吊住，
而宿主收尾时也没有一个"把这张图关干净"的入口。两件都在这里补上。

**F-1 `handler_timeout`（默认 `None` = 关闭，零行为变化）**

C 节讲的是模型超时，这里同一思路用在工具上，但两种 handler 的语义**不能一起承诺**：

```python
if inspect.iscoroutinefunction(handler):
    return await asyncio.wait_for(handler(**arguments), timeout)   # 真取消：协程收到 CancelledError
# 同步 handler：丢进线程池，但**不用 wait_for**
future = loop.run_in_executor(None, functools.partial(handler, **arguments))
done, _ = await asyncio.wait({future}, timeout=timeout)            # 到点没完成就立刻放弃
```

这里踩过一个具体的坑：`wait_for` 取消一个已在线程池里跑的 future 时，会 **await 那个 future**
直到线程结束——实测一个睡 0.4s 的同步工具，配 0.05s 超时仍然要 0.4s 才回话。
换成 `asyncio.wait` 才拿到"到点即回话"的行为（测试 `test_sync_handler_timeout_does_not_block_forever`
量的就是 `execute` 的返回时刻，不是 `asyncio.run` 的返回时刻）。

还有一条不粉饰的边界：被放弃的线程仍在循环默认线程池里，**事件循环收尾会 join 它**。
所以同步工具的超时是"给模型的止损"，不是进程级立即脱身；可能长时间阻塞的能力应当写成 `async def`。
超时回给模型的话术也分两种（异步说"已取消"，同步说"仍在后台、先确认状态再重试"），
否则模型只会原样重试同一个调用。

**F-2 `await core.aclose(*, close_shared=False)`**

规则只有一句：**谁创建，谁负责关**。

```python
self._owned = []                      # __init__ 末尾记账
if channel is None: self._owned.append(self.channel)     # 默认实现是 core 建的
if memory is None:  self._owned.append(self.memory)
if self.memory_vault is not None: self._owned.append(self.memory_vault)
```

默认只关 `_owned`；宿主传进来的 `model` / `memory` / `discovery` / `sandbox` / `channel`
**默认一个都不碰**——多个 core 常共享同一个模型或发现源（并发会话的正确用法就是共享无状态模型），
替别人关资源是误伤。确实要连整张图一起收才传 `close_shared=True`，顺序固定
channel → discovery → sandbox → memory → model。三条细节：

- 幂等（`_closed` 标记），可以放在 `finally` 里重复调；
- 拿实例串行锁，所以**不会在当前任务跑到一半时把资源抽走**（要关先取消任务）；
- 单个资源关不掉不阻止其余收尾，但会在全部尝试之后抛汇总异常——收尾故障要能被运维看见。

## G. 自检（读完应能回答）

1. 为什么一个 `AgentCore` 实例上要强制任务串行？共享的可变状态有哪几处？
2. 想要多租户/多专员并发，正确的用法是什么？哪些部件可以共享、哪些必须随实例独立？
3. 为什么超时放在 loop 层用 `wait_for`，而不是给 `ModelProvider.achat` 加 `timeout` 参数？
4. 用户主动取消时，`CancelledError` 为什么不会被 `except Exception` 误吞？
5. 取消一个沙箱子进程，为什么必须 `kill()` 之后再 `communicate()`？
6. `DiagnosticEvent` 和 `AgentEvent`、OpenTelemetry span 三者各面向谁、回答什么问题？
7. `emit()` 为什么要吞掉回调自身的异常？这体现了什么定位？
8. 宿主只传一次 `on_diagnostic`，它是怎么到达内部 router 和 FallbackModelProvider 的？
9. 哪两类失败**刻意不发**诊断，为什么？
10. 同步 handler 为什么不能用 `asyncio.wait_for` 做超时？`asyncio.wait` 与它的行为差别在哪一处实测里？
11. `aclose()` 默认为什么不关宿主传进来的 model / memory？什么情况下才该传 `close_shared=True`？
