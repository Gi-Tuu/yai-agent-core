# 逐行讲解 18 · 代码工具：注册表、48h 生命周期与授权闸

> 对应源码：`src/yai_core/types.py`（新增 source 与事件）、`src/yai_core/tools/code_tools.py`（新文件）、
> `src/yai_core/tools/meta.py`（新增 `create_code_tool`）、`src/yai_core/tools/executor.py`（新增 `_execute_code`）、
> `src/yai_core/kernel/loop.py`（新增 `_handle_create_code_tool`）、`src/yai_core/core.py`（条件装配与后台方法）。
> 前置：第 16 篇（组合工具与两级能力模型）、第 17 篇（ToolSandbox 契约）。
> 测试：`tests/test_code_tools.py`（12 个离线测试，假时钟 + FakeSandbox + ScriptedModel）。

## 0. 这一篇把第 17 篇的"契约"接上了

第 17 篇只定义了沙箱长什么样（`ToolSandbox` 协议），但当时内核还**没有**"代码工具"这个概念：
模型即使想造一个新工具，也无处注册、不知道能活多久、谁来授权。

这一篇补齐内核侧的另一半：

1. 模型在 react 中途调用 meta-tool `create_code_tool`，声明一个由 Python 代码实现的新工具；
2. 内核把它注册进工具表，记录它的**生命周期**（默认 48h，被调用就刷新，后台可永久保留）；
3. 创建和首次执行都过**权限闸**；
4. 真正执行时，把代码交给宿主沙箱（第 17 篇的 SPI）——**内核依然不内置任何代码执行器**。

一句话边界：**内核负责"登记、计时、授权、转交"，宿主负责"安全执行"。**

## 1. 两级能力模型回顾（为什么代码工具是最后一级）

| 级别 | 怎么得到新能力 | 执行模型生成的代码？ | 能力上限 | 风险 |
|---|---|---|---|---|
| 原生工具 | 宿主函数内省 | 否 | 宿主声明的能力 | 无 |
| 组合工具 composite | `compose_tool` 编排已有工具 | 否 | 被组合工具的并集 | 零（物理不越界） |
| **代码工具 code** | `create_code_tool` 生成一段代码 | **是（在沙箱里）** | 沙箱允许的范围 | 较高，需沙箱+授权 |

所以决策顺序是：**能用现有工具就用，能组合就组合，确实需要宿主没有的新原始逻辑，才走到代码工具**。
这条优先级写进了 `create_code_tool` 的描述里，模型会被引导先走低风险路径。

## 2. types.py：新的 source、code 字段、两个事件

```python
class EventType(StrEnum):
    ...
    CODE_TOOL_CREATED = "code_tool_created"
    CODE_TOOL_RETIRED = "code_tool_retired"
```

```python
@dataclass
class ToolSpec:
    ...
    source: Literal["native", "openapi", "mcp", "composite", "code"] = "native"
    steps: list[CompositeStep] | None = None   # 仅 composite 用
    code: str | None = None                    # 仅 code 用：模型生成的 Python 代码
```

逐点：

- `source` 多了 `"code"` 这一类。执行器靠它分流：`composite` 走编排，`code` 走沙箱，其余走普通 handler。
- 代码工具的 `handler` 是 `None`——它没有一个 Python 函数可直接调用，真正的"函数体"是 `code` 字符串，
  要送进沙箱执行。这是它和原生工具最本质的区别。
- 两个新事件：创建成功发 `CODE_TOOL_CREATED`（宿主 UI 可提示"AI 新建了一个工具"）；
  后台回收发 `CODE_TOOL_RETIRED`（本版回收由宿主定时调用，事件名先预留）。

## 3. code_tools.py：生命周期记录与管理器

### 3.1 一条记录 CodeToolRecord

```python
@dataclass
class CodeToolRecord:
    name: str
    created_at: datetime
    last_used_at: datetime
    ttl_seconds: int
    permanent: bool = False
    call_count: int = 0

    def is_expired(self, now: datetime) -> bool:
        if self.permanent:
            return False
        return (now - self.last_used_at).total_seconds() > self.ttl_seconds
```

- 计时基准是 `last_used_at`（上次使用时间），不是创建时间——这正是"被调用就刷新"的关键。
- `permanent=True` 的工具永不过期（后台白名单）。
- 边界是**严格大于** TTL：恰好 48h 还活着，超过 1 秒才过期（测试里专门卡了这个边界）。

### 3.2 时钟可注入（为了离线测试）

```python
def _utc_now() -> datetime:
    return datetime.now(UTC)

class CodeToolManager:
    def __init__(self, registry, *, clock=None, default_ttl=DEFAULT_CODE_TTL_SECONDS):
        self._clock = clock or _utc_now
```

`clock` 是一个"返回当前时间"的可调用对象。测试里传入一个可以手动 `advance(秒数)` 的假时钟，
就能在不等待真实 48 小时的情况下验证过期逻辑。这和全项目"可离线测试"原则一致（第 09/13 篇的限流、
历史裁剪也是注入时钟）。

### 3.3 创建 create：校验 + 注册 + 登记

```python
def create(self, name, description, input_schema, code, *, ttl_seconds=None, now=None):
    name = (name or """).strip()
    ...
    if not name: raise ValueError("代码工具必须提供非空 name")
    if not description: raise ValueError(...)
    if not code: raise ValueError(...)
    if self.registry.has(name): raise ValueError(f"工具名 {name!r} 已存在，不能重复创建")
    spec = ToolSpec(name=name, description=description,
                    input_schema=input_schema or {"type": "object", "properties": {}},
                    handler=None, source="code", code=code)
    self.registry.register(spec)
    self._records[name] = CodeToolRecord(name=name, created_at=now,
                                         last_used_at=now, ttl_seconds=ttl)
    return spec
```

- 三个必填字段（name / description / code）缺一个都抛 `ValueError`，由上层 loop 捕获后**回灌给模型**，
  让它补全，而不是让进程崩溃。
- 重名拒绝，避免模型覆盖宿主已有工具（这是一条安全防线）。
- 注意注册的是 `handler=None, source="code"` 的规格。

### 3.4 调用刷新 touch、永久保留、回收

```python
def touch(self, name, now=None) -> bool:
    rec = self._records.get(name)
    if rec is None or rec.is_expired(now):
        return False
    rec.last_used_at = now      # 刷新存活期
    rec.call_count += 1         # 计数
    return True

def make_permanent(self, name) -> bool: ...      # 后台白名单化
def expire_stale(self, now=None) -> list[str]:   # 回收所有过期且非永久的
    for name, rec in list(self._records.items()):
        if rec.is_expired(now):
            self.registry.unregister(name)      # 从工具表注销
            del self._records[name]
            retired.append(name)
```

- `touch` 一个方法同时完成"判断存活 + 刷新 + 计数"，执行器在真正调用代码工具前会先 `touch`：
  过期返回 False，执行器就注销它并提示"请重新创建"。
- `expire_stale` 是惰性 GC，宿主可以定时（比如每小时）调一次 `core.sweep_code_tools()`。
- `records()` / `status()` 给出后台/UI 可展示的状态快照（总数、存活数、永久数、逐项明细）。

## 4. meta.py：create_code_tool 是一个"被拦截的工具"

和第 15 篇的 `request_capability` 一样，`create_code_tool` 也注册进工具表让模型能看到，
但它的 handler 是 `_unreachable`（真被普通执行器碰到就抛错），实际由 Agent Loop 按名字拦截处理。

它的入参 schema 有四个必填字段：`name`、`description`、`input_schema`、`code`。
`code` 的描述明确要求：**定义函数 `run(inputs: dict)`，返回可 JSON 序列化结果，不得访问网络或敏感文件**。
工具描述里还写了"优先用 compose_tool、默认 48h 回收、不要创建危险或越权工具"。

## 5. executor.py：执行代码工具 _execute_code

普通工具的执行链路是"权限 → 调 handler"。代码工具在权限通过后分流到 `_execute_code`：

```python
if spec.source == "code":
    return await self._execute_code(spec, arguments, events)
```

`_execute_code` 三步走：

```python
# 1) 生命周期：touch 成功即刷新 TTL；过期则注销并提示重建
if self.code_manager is not None and not self.code_manager.touch(spec.name):
    self.registry.unregister(spec.name)
    return events, False, f"代码工具 {spec.name} 已超过存活期被回收，请重新创建"

# 2) 没有沙箱：优雅降级，而不是崩溃
if self.sandbox is None:
    return events, False, "宿主未提供代码沙箱（ToolSandbox），代码工具不可用"

# 3) 交给宿主沙箱执行
sb = await self.sandbox.execute(spec.code or "", inputs=arguments, timeout=self.code_timeout)
if not sb.ok:
    return events, False, f"工具 {spec.name} 沙箱执行失败: {sb.error}"
text = json.dumps(sb.output, ensure_ascii=False, default=str)
```

要点：

- **内核不 `exec` 任何代码**。它只是把 `spec.code` 字符串和入参 `arguments` 转交给 `self.sandbox.execute`，
  拿回第 17 篇定义的 `SandboxResult`。
- 沙箱异常、`ok=False`、超时，全部转成 `TOOL_RESULT(ok=False)` 回灌给模型，绝不穿透到主循环。
- `code_timeout` 默认 10 秒（`DEFAULT_CODE_TIMEOUT`），可由宿主覆盖。
- 走到这里之前已经过了一次 `authorize_tool_call`（见第 15 篇），所以**每次执行代码工具都要过权限闸**，
  不只是创建那一刻。

## 6. loop.py：创建动作 _handle_create_code_tool

react 循环里，模型若调用 meta-tool，会进入 `_handle_meta_tool` 分发；`create_code_tool` 走
`_handle_create_code_tool`：

```python
# 1) 创建是敏感动作，先授权（allow_all 即"无需审批"档才免授权）
auth_events, approved, reason = await authorize_tool_call(
    self.executor.policy, self.channel, CREATE_CODE_TOOL, arguments)
if not approved:
    _reply(f"创建代码工具未被授权：{reason}。请改用现有工具或组合工具。"); return

# 2) 没配沙箱就不该有这个工具（core 装配时也不会注册），双保险
manager = self.executor.code_manager
if manager is None:
    _reply("宿主未提供代码沙箱，无法创建代码工具；请改用组合工具 compose_tool。"); return

# 3) 取字段、校验、注册（ValueError 回灌）
spec = manager.create(name, description, input_schema, code)   # 抛 ValueError 则回灌

# 4) 发事件 + 回灌，告诉模型下一轮可直接调用
yield AgentEvent(EventType.CODE_TOOL_CREATED,
                 {"name": spec.name, "source": "code",
                  "ttl_seconds": manager.default_ttl, "permanent": False})
_reply(f"已创建代码工具 {spec.name}（默认 48 小时有效，每次被调用刷新…）…首次执行仍会按权限策略确认…")
```

创建后还有一个关键接线：react 循环在收集 meta-tool 事件时，把 `CODE_TOOL_CREATED` 的工具名也并入
`newly_registered`，于是**下一轮 function-calling 的 schema 里立刻能看到这个新工具**，模型"创建完就能调到"，
不会出现"造了却看不见"的断层（和第 15 篇 `tool_discovered` 的刷新逻辑同一套）。

## 7. core.py：条件装配与后台三个方法

代码工具能力是**按需开启**的——只有宿主传入沙箱才启用：

```python
self.code_manager = CodeToolManager(self.registry) if sandbox is not None else None
self.executor = ToolExecutor(..., sandbox=sandbox, code_manager=self.code_manager)
if sandbox is not None:
    self.registry.register(build_create_code_tool())
    meta_tools.add(CREATE_CODE_TOOL)
```

这遵循"不给模型注定失败的工具"：没有沙箱就不注册 `create_code_tool`，模型根本看不到它，
也就不会反复尝试。

对外暴露三个后台方法：

```python
core.retain_code_tool("weighted_score")  # -> bool，置为永久保留
core.code_tools_status()                 # -> {enabled,total,live,permanent,tools:[...]}
core.sweep_code_tools()                  # -> ["过期工具名", ...]，回收
```

宿主可以在管理后台列出 AI 自建的工具、一键"永久保留"，或起一个定时任务调 `sweep_code_tools()` 清理。

## 8. 安全模型：为什么内核不判断"这段代码越不越界"

一个自然的想法是"让内核判断代码是否超出宿主能力范围，超了就不创建"。但这在一般情况下是
**不可判定的**（等价于判断一段任意代码的运行时行为）。所以 YAI 不做这种猜语义的假安全，而是用
多层确定性防线：

1. **优先级引导**：先原生、再组合、最后代码，描述里明确要求不访问网络/敏感文件；
2. **双重授权**：创建要授权、每次执行还要授权（"无需审批"档才免）；
3. **沙箱硬隔离**：无网、无敏感文件、超时、资源上限（由宿主实现，第 17 篇契约）；
4. **生命周期收敛**：默认 48h 自动回收，只有后台显式保留才长期存在；
5. **能力可观测**：创建发事件、注册表明细可查、调用计数可审计。

## 9. 自检

1. 代码工具的 `ToolSpec.handler` 为什么是 `None`？真正的"函数体"放在哪个字段、由谁执行？
2. TTL 为什么以 `last_used_at` 而不是 `created_at` 为基准？`touch` 一次做了哪三件事？
3. 一个代码工具从创建到被调用，要过几次权限闸？分别在什么位置？
4. 为什么宿主不提供沙箱时，内核干脆不注册 `create_code_tool`，而不是注册了等它失败？
5. 为什么内核不去判断"这段代码是否越权"？它改用哪几层确定性机制来保证安全？
6. 模型创建完一个代码工具后，下一轮为什么能立刻在 function-calling 里看到它？是哪段接线保证的？
