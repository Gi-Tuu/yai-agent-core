# 逐行讲解 36 · 子员工预算兜底与跨层级共享并发闸

> 对应文件：
> - `shell/employee/worker.py`（`default_budget`、`STATUS_BUDGET_EXCEEDED`、
>   `WorkerResult.budget`、`_assemble_worker` / `run_worker` 透传预算与并发闸）；
> - `shell/employee/delegate.py`（新增 `DelegationSupervisor`、per-task 预算覆盖、
>   `attach_delegate` 返回并透传 supervisor）；
> - `shell/employee/__init__.py`（导出新符号）；
> - 离线测试：`tests/test_shell_employee.py`（新增 5 组用例，全部零网络、零 API Key）。
>
> 读前先读第 22 / 23 篇（灵动岛与多专员）以及子员工委派本身（`shell/employee/`，
> 第 35 篇的 `kernel/budget.py` 是本篇的前置：内核已有任务级预算熔断，本篇把它
> 接到"薄壳里的子员工"上）。
>
> 第 35 篇给**内核的一次任务**装了预算硬墙；本篇解决薄壳层两个新问题：
> 1. 根员工一次派生好几个子员工，**每个子员工都可能失控烧钱**，它们也要有预算；
> 2. 根并行 5 个、每个子又并行 5 个，理论上会同时冒出 5+25 个子任务，
>    **并发数必须在全层级共享一把闸**，而不是每层各算各的。

---

## A. 先看结论

| 改动 | 防的事故 | 一句话做法 |
|---|---|---|
| 子员工默认预算 | 某个子员工陷入"模型↔工具"异常循环，悄悄刷爆调用量 | 每个子员工构造时就带一份按迭代轮数推导的 `Budget`，触顶回流 `budget_exceeded` |
| 预算快照回流 | 根不知道子员工花了多少、为什么停 | `WorkerResult.budget` 带回账本快照，根可据此缩小任务或放宽预算重派 |
| 跨层级共享并发闸 | 根 5 个 × 子 5 个 = 同时 25 个子任务抢资源 | `DelegationSupervisor` 持**一把**信号量，沿根→子→孙原样透传，全局同时存活 ≤5 |
| per-task 预算覆盖 | 模型/宿主无法对特别小或特别重的子任务单独收紧 | delegate schema 每项可选 `max_model_calls` / `max_tool_calls`，非法值容错 |

纪律不变：**内核零硬依赖（预算只用标准库）、默认行为安全（子员工总有兜底预算）、
全部离线可测（ScriptedModel + 自建无状态模型，不需要真实 API Key）。**

---

## A.1 委派权限只能收窄

子员工的 `_WorkerPolicy` 先检查本地工具白名单，再用真实参数和可信主体检查父级当前
`executor.policy`；只有两层都 `ALLOW` 才执行。父级 `DENY` 或 `ASK` 都拒绝回流，
不以子员工白名单替代父级策略。孙员工递归叠加同一道检查，父级运行期撤权也立即生效。

子通道固定 `CollectChannel(auto_confirm=False)`：工具的 `approval.required` 或 gate
要求审批时不自动同意，也不直接向用户弹窗。失败工具和审批拒绝进入 `denied`，
即便模型说“完成”，这次回流的 `ok` 也为 `False`；需要确认的操作由根员工自己处理。
薄壳包装该实例的 `executor.execute`，按真实返回值收集失败，不依赖可能缺失的事件；
gate 拒绝、未注册工具与未知授权名单都不能漏记。孙员工失败由 delegate 的内部回调
直接记入父级失败集合，保留完整结构化结果，不从可能截断的事件预览猜测。
同批子任务有异常或被取消时，delegate 会取消并等待全部兄弟任务后才退出；
普通超时仍按失败结果回流，不拖垮其他正常子任务。

`AgentCore.astream` 在实例串行锁内维护当次 `_task_subject`，正常结束、异常、取消或关闭
事件流都会清空。delegate 从该可信上下文取得主体并传给 `run_worker(subject=...)`，
工具参数里的身份字段不参与授权；直接调用 `run_worker` 的宿主应显式传入可信主体。

父级 delegate 的闭包不能共享给子级，否则会重新拿到父级工具目录并重置深度。
子级只在允许的深度重新构造自己的 delegate，并保留父级 delegate 的治理声明；
达到最大深度即使模型在工具列表里写 `delegate` 也不授予。显式 `tools=[]` 表示不授予
业务工具，只有省略列表才应用默认只读工具启发式。

## B. 子员工默认预算（`worker.py`）

### B.1 为什么默认值不能太松也不能太紧

内核的 `Budget` 三个上限：`max_model_calls`（模型调用次数）、`max_tool_calls`
（**业务工具**调用次数，meta-tool / 组合编排不重复计）、`max_tokens`。子员工是
"短命、收窄、目标单一"的执行者，迭代轮数 `max_iters` 默认只有 3。我们按轮数推导：

```python
#: 子员工预算熔断后的终态标识（区别于正常 done / 墙钟 timeout / 运行 error）。
STATUS_BUDGET_EXCEEDED = "budget_exceeded"


def default_budget(max_iters: int) -> Budget:
    """按迭代轮数为子员工推导一份保守的硬预算（默认总有兜底，防止并行烧钱）。"""
    return Budget(
        max_model_calls=max_iters + 2,   # 每轮至多一次模型调用，再留规划/收尾两次余量
        max_tool_calls=max_iters * 4,   # 一轮可能并行多个业务工具，按每轮最多 4 个放宽
        max_tokens=None,                # 任务间差异大、部分后端不回 usage，默认不硬限
    )
```

逐行讲：

- **模型 `max_iters + 2`**：一轮 ReAct 最多一次主模型调用，3 轮就是 3 次；
  额外 +2 给"收尾总结"和可能的一次规划，避免正常任务被误杀。
- **工具 `max_iters * 4`**：这是关键。内核计数粒度是**每个业务工具调用一次**——
  模型在一轮里并行返回 3 个工具调用，就计 3 次（见第 35 篇 / `loop.py` 工具分支，
  计数在遍历本批工具 call 的 for 循环体内）。若按"每轮 1 个工具"设上限，
  正常的并行调用会被误判熔断，所以放宽到每轮 4 个。3 轮 → 工具上限 12。
- **token 恒为 `None`**：不同任务 token 量差异极大，而且只有模型真实回传
  `usage` 时内核才会计 token（缺失不估算）。默认不硬限，避免误伤；
  真要限，由根经 delegate 入参或宿主显式给。

> 设计取向：**默认预算是"防失控的安全网"，不是"卡正常任务的紧箍咒"。**
> 宁可上限宽松一点，也不能让一个本该成功的子任务被自己的预算墙挡住。

### B.2 结果里多带一个预算快照

```python
@dataclass
class WorkerResult:
    ...
    #: 终态：done / timeout / error / budget_exceeded（触发预算硬上限，提前熔断）。
    status: str = "done"
    #: 预算用量快照（配置了预算时随 DONE / BUDGET_EXCEEDED 回流），否则为 None。
    budget: dict | None = None

    def to_observation(self) -> dict:
        return {
            ...,
            "budget": self.budget,   # 新增：喂给根模型的 observation 里也带上账本
        }
```

`ok` 要求有 `final_text`、`status == "done"` 且没有失败/拒绝工具——预算触顶时即使已经
产出了部分文本，`ok` 也是 `False`，根不会把"被熔断的半截结果"误当成成功。
但 `final_text` 与 `budget` 照样回流，根能看到"做到哪了、卡在哪个上限"，
从而决定：缩小目标重派、放宽预算重派，或如实告诉用户。

### B.3 构造子员工时挂上预算

`_assemble_worker` 新增两个形参：

```python
def _assemble_worker(root_core, *, goal, tools=None,
                     max_iters=DEFAULT_WORKER_ITERS, depth=1,
                     register_delegate=False,
                     budget: Budget | None = None,
                     supervisor=None):
    if budget is None:
        budget = default_budget(max_iters)   # 没显式给 → 总有一份兜底预算
    ...
    worker = AgentCore(
        root_core.model,
        ...
        max_iters=max_iters,
        budget=budget,                        # ← 预算交给内核 AgentCore
    )
    ...
    if register_delegate and depth < MAX_DEPTH:
        attach_delegate(worker, depth=depth + 1, supervisor=supervisor)  # ← 并发闸透传
```

注意预算是**每个子员工独立一份**：子员工 A 烧到上限，只熔断 A 自己，
不影响同批并行的 B、C。这正是"单一根对用户负责、子员工互不连坐"的体现。

### B.4 运行时识别熔断事件

`run_worker` 的事件循环里，新增对两类事件的处理：

```python
elif kind == "budget_exceeded":
    # 预算硬墙：先于 DONE 到达，标记终态并记下用量快照。
    status = STATUS_BUDGET_EXCEEDED
    if isinstance(data.get("budget"), dict):
        budget_snap = data["budget"]
elif kind == "done":
    if data.get("final_text"):
        final_text = str(data["final_text"])
    if data.get("reason") == "budget_exceeded":
        status = STATUS_BUDGET_EXCEEDED      # DONE 上也带 reason，双保险
    if isinstance(data.get("budget"), dict):
        budget_snap = data["budget"]
```

回顾第 35 篇：内核触顶时**先发一条固定的诚实 `MODEL_MESSAGE`，再正常落 DONE**
（`done_data["reason"]="budget_exceeded"`、`done_data["budget"]=快照`），
`astream` **不向上抛** `BudgetExceeded`（转成事件体面收尾）。所以薄壳这里
**不需要 try/except 预算异常**，只要听事件即可——这也是为什么熔断不会
穿透 `asyncio.gather`、不会把整批并行子员工带崩。

> 墙钟 `timeout` 与预算熔断是两条独立的路：超时抛 `TimeoutError` 被
> `wait_for` 捕获标 `timeout`；预算熔断是正常事件流，很快返回、不进超时分支。

---

## C. 跨层级共享并发闸（`delegate.py`）

### C.1 旧行为的问题：每层一把锁

原来 `build_delegate_tool` 内部各自 `asyncio.Semaphore(MAX_CONCURRENT)`。
考虑：根派生 5 个子员工，其中一个子员工自己又持有 delegate、派生 5 个孙员工。
根的信号量限制"同时 5 个子"，子的信号量**另算**"同时 5 个孙"，两者互不知道，
峰值可以叠到 10、25。层数越深，超额越严重。

### C.2 一把闸，沿链路透传

```python
class DelegationSupervisor:
    """跨层级共享的委派并发闸。"""

    def __init__(self, max_concurrent: int = MAX_CONCURRENT) -> None:
        self.max_concurrent = max_concurrent
        self.semaphore = asyncio.Semaphore(max_concurrent)
```

它只是把 `asyncio.Semaphore` 包一层、起个名字。关键不在类本身，而在**同一个实例
要被根、子、孙共用**：

```python
def build_delegate_tool(root_core, *, depth=1, on_progress=None,
                        supervisor: DelegationSupervisor | None = None):
    if supervisor is None:
        supervisor = DelegationSupervisor()   # 顶层根首次挂载：新建一把
    semaphore = supervisor.semaphore          # 之后都用这一把

    async def _run_one(item):
        iters = item.get("max_iters") or DEFAULT_WORKER_ITERS
        budget = _budget_for_item(item, iters)
        async with semaphore:                  # ← 所有层级的子员工都抢这同一把
            return await run_worker(..., budget=budget, supervisor=supervisor)
```

挂载入口 `attach_delegate` 负责"创建一次、之后透传"，并把 supervisor 返回给调用方：

```python
def attach_delegate(root_core, *, depth=1, on_progress=None,
                    supervisor=None) -> DelegationSupervisor:
    if root_core.registry.has(DELEGATE_TOOL):
        return supervisor if supervisor is not None else DelegationSupervisor()
    if supervisor is None:
        supervisor = DelegationSupervisor()
    root_core.registry.register(
        build_delegate_tool(root_core, depth=depth,
                            on_progress=on_progress, supervisor=supervisor)
    )
    return supervisor
```

链路于是变成：

```
根 attach_delegate(root)            → 新建 supervisor S，注册根 delegate（持 S）
 └ 根 delegate 派生"子" run_worker(..., supervisor=S)
     └ _assemble_worker 给子 attach_delegate(child, depth+1, supervisor=S)
         └ 子 delegate 也持同一把 S
             └ 子 delegate 派生"孙" run_worker(..., supervisor=S) → 仍抢 S
```

无论多少层，同时存活的子员工数被 S 这一把信号量压在 `MAX_CONCURRENT`（默认 5）。

### C.3 每个子任务可单独收紧预算

delegate 的 input schema 给每个 task 加了两个可选字段，handler 用它们构造预算：

```python
"max_model_calls": { "type": "integer", "description": "模型调用硬上限（省略按 max_iters 推导）..." },
"max_tool_calls":  { "type": "integer", "description": "工具调用硬上限（省略按 max_iters 推导）..." },
```

容错与补全是两个小纯函数：

```python
def _as_positive_int(value) -> int | None:
    """模型传入的可选整数上限容错成非负整数；非法 / 缺省返回 None（走默认推导）。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _budget_for_item(item: dict, max_iters: int) -> Budget:
    """按委派任务的可选覆盖项构造预算；未覆盖维度用默认推导补齐。"""
    base = default_budget(max_iters)
    model_cap = _as_positive_int(item.get("max_model_calls"))
    tool_cap = _as_positive_int(item.get("max_tool_calls"))
    if model_cap is None and tool_cap is None:
        return base
    return Budget(
        max_model_calls=base.max_model_calls if model_cap is None else model_cap,
        max_tool_calls=base.max_tool_calls if tool_cap is None else tool_cap,
        max_tokens=None,
    )
```

要点：

- **`bool` 被显式排除**：Python 里 `True == 1`，不挡的话模型传个 `true` 会被当成 1。
- **只覆盖你给的维度**：只给 `max_tool_calls`，模型上限仍用默认推导值，不会被误清零。
- **`0` 合法**：`Budget` 语义里 0 = "一次都不允许"，所以这里放行非负、只拒绝负数。

工具描述（给模型看的）也同步更新，明确告诉模型：全局同时最多 5 个（含嵌套）、
有预算硬墙、触顶回流 `budget_exceeded`、可以补齐工具/放宽预算后**再次委派**。

---

## D. 离线测试怎么证明改对了（`tests/test_shell_employee.py`）

全部用 `ScriptedModel` / 自建无状态模型，不需要 API Key、不联网。

1. **`test_default_budget_derivation`**：`default_budget(3)` 断言模型 5、工具 12、
   token None 且 `enabled`——把 B.1 的口径钉死，谁改公式测试就红。
2. **`test_worker_reports_budget_snapshot_on_done`**：跑一个一步到位的正常任务，
   断言 `status=="done"`、`budget` 快照非空、`limits` 与默认推导一致，且
   `to_observation()["budget"]` 就是同一个快照。
3. **`test_worker_budget_exceeded_marks_status`**：让模型连续两轮都要求调工具，
   显式给 `Budget(max_tool_calls=1)`。第一次工具正常执行（`tool_calls=1`），
   第二次工具调用**前** `before_tool_call` 发现 1≥1 → 熔断。断言
   `status=="budget_exceeded"`、`ok is False`、快照里 `tool_calls==1`、
   `steps==["list_items"]`（只执行了一次，没有第二次副作用）。
4. **`test_shared_supervisor_caps_concurrency_across_levels`**（最有说服力）：
   造一个 `occupy_seat` 异步工具，进入时 `active++ / peak=max`、睡 0.2s、离开 `active--`。
   根 delegate 派 2 个、子 delegate 再派 2 个，共 4 个 run_worker，共用
   `DelegationSupervisor(max_concurrent=2)`。断言**峰值 `peak==2`**。
   若哪天回归成"每层一把独立信号量"，根 2 + 子 2 会同时占座，`peak==4`，测试立即红。
5. **`test_worker_cancellation_propagates`**：外层 `task.cancel()` 一个正在跑慢工具的
   子员工，断言 `CancelledError` 能穿透 `run_worker`（`wait_for` 只捕 `TimeoutError`，
   不吞取消），不会留下卡死的后台任务。

---

## E. 自检（读完应能回答）

1. 为什么子员工默认工具上限是 `max_iters * 4` 而不是 `max_iters`？
2. 预算触顶后，内核是"抛异常"还是"发事件"？薄壳为什么不用 try/except 接预算熔断？
3. `budget_exceeded` 时 `ok` 是 True 还是 False？已经产出的半截文本去哪了？
4. `DelegationSupervisor` 为什么必须沿根→子→孙透传**同一个实例**？每层各建一个
   会发生什么？哪个测试能抓住这种回归？
5. 模型在某个 task 里只传了 `max_tool_calls=2`，没传 `max_model_calls`，
   最终这个子员工的模型调用上限是多少？（提示：`_budget_for_item`）
6. 为什么 `_as_positive_int` 要专门排除 `bool`？
