# 逐行讲解 39 · 第三方播种：host_j 记账宿主与"五拍标准线"

> 对应文件（都在示例与测试层，**内核本体零改动**）：
> - `examples/host_j_actual/backend.py`（领域原语 + 整数分约定；离线
>   `FakeActualBackend` 与对接官方 CLI 的 `CliActualBackend`）；
> - `examples/host_j_actual/capabilities.py`（三层工具：只读 / 需授权写入 / 按需发现）；
> - `examples/host_j_actual/demo_agent.py`（两条链路的脚本模型与 Core 装配）；
> - `examples/host_j_actual/run.py`、`web_app.py`、`web/index.html`（终端 / 网页入口）；
> - `examples/five_beats.py`（**新建**，跨宿主复用的五拍标准线）；
> - 离线测试：`tests/test_host_j_actual.py`（17 例，零网络、零 API Key、不需要 Node）。
>
> 读前先读第 15 篇（能力缺口 → 发现闭环）、第 16 篇（组合工具与授权）、
> 第 22/23 篇（员工壳与子员工）、第 36 篇（子员工预算与并发闸）。本篇不教内核
> 新机制，而是回答一个更关键的问题：**怎么向第三方证明这些机制在一个"和 YAI
> 无关的现成应用"里真的能端到端跑通，而且可重复、可验证。**

---

## A. 先看结论：什么叫"第三方播种"

前面的 host_a / host_c / host_e 都是我们自己写的宿主，容易被质疑"你是不是照着
内核的接口反向捏的 demo"。**第三方播种（seeding）**要消除这个质疑：

1. 挑一个真实、独立、有用户的开源应用（这里是记账应用
   [Actual Budget](https://actualbudget.org/)）；
2. **不改它一行源码**，只把它已有能力包成普通函数；
3. 丢给内核一个它没被专门调教过的"长尾任务"，看内核能不能自己走完
   "缺能力 → 补能力 / 派人 → 授权 → 完成"。

为了让这件事**可断言、可在 CI 里复现**，我们把一次成功接管压成一条"五拍标准线"
（`examples/five_beats.py`），再用一个离线假账后端保证每次演示结果完全一致。

| 概念 | 一句话 |
|---|---|
| 长尾任务 | 一个目标要串 ≥2 个不同工具，不是一问一答 |
| 能力缺口 | 现有工具不足以完成，模型显式报 `CAPABILITY_MISSING` |
| 补能力 | 现场发现并注册一个新工具（`TOOL_DISCOVERED`），本轮即可用 |
| 派子员工 | 根把可并行的只读小块 `delegate` 给短命子员工 |
| 五拍 | 唤醒 → 长尾 → 缺口 → 补/派 → 授权 → 完成 的事件序列契约 |

---

## B. `backend.py`：先把"钱"的领域约定钉死

### B.1 为什么金额一律用整数分

记账最忌浮点。`0.1 + 0.2 != 0.3` 在金额场景里就是对账永远差一分钱。Actual
官方 CLI 的约定也是**整数分（cents）**，所以后端所有金额字段都叫
`amount_cents`、类型是 `int`：

```python
# 38.00 元在账里永远是 -3800（支出为负），绝不存 -38.0 浮点
{"id": "t3", "amount_cents": -3800, "category_id": None, ...}
```

只有**展示层**才换算成元，而且集中在一个函数里做：

```python
def _yuan(cents: int) -> float:
    return round(cents / 100, 2)
```

> 逐行要点：换算只发生在工具返回给模型 / 界面的那一刻；写回账本的入参
> （`add_expense`）接收"元"，但立刻 `int(round(yuan * 100))` 转成负整数分落库。
> 全链路只有一个地方做元↔分，杜绝两处换算口径不一致。

### B.2 假账是"为链路量身种的因"

`FakeActualBackend` 的种子数据不是随便填的，每一笔都对应后面要验证的一个结果：

- `t3 星巴克 / t5 盒马 / t6 滴滴` 三笔 `category_id=None` —— 制造"未分类"；
- 关键词规则能把它们分别判到餐饮 / 生鲜 / 交通 —— 验证发现的建议工具真有用；
- `t8 电影 -12800`，而娱乐预算是 `10000` —— 必然超支 28 元，验证预算核对。

这样一次离线跑下来，"未分类清零""娱乐超支"都是**确定**的，测试能写死断言，
不依赖模型心情。

### B.3 真机后端：把官方 CLI 当一个可注入的函数

Actual 没有朴素 REST，官方让你用 Node CLI。内核和示例**不引入 Node**，真机
通过子进程调用。关键设计是把"怎么跑命令"抽象成一个可注入的 `runner`：

```python
def _default_runner(argv, env):
    return subprocess.run(argv, env=env, capture_output=True, text=True)

class CliActualBackend:
    def __init__(self, *, cli="actual", runner=None, env=None):
        self._runner = runner or _default_runner
        ...
```

- 真机：`runner` 缺省走 `subprocess`；
- 测试：传入一个返回假 `CompletedProcess` 的 Python 函数，**根本不起子进程**，
  却能逐位断言 argv。

所有命令统一经 `_json` 拼，`--format json` 永远在最后：

```python
def _json(self, *parts: str) -> list[str]:
    return [self._cli, *parts, "--format", "json"]
```

于是 `accounts list` 的 argv 被钉死成
`["actual", "accounts", "list", "--format", "json"]`；测试里任何手滑改了参数
顺序都会立刻红。

**口令不进 argv** 是一条安全红线：`ACTUAL_PASSWORD` / `ACTUAL_SESSION_TOKEN`
只放进子进程的 `env`，命令参数里永远不出现，避免它被进程列表 / shell 历史 /
日志截走。测试专门断言 `secret not in any argv part`。

跨账户查询遵循官方 Tips：CLI 不提供一把梭的 ActualQL 聚合，就**逐账户拉原始
交易，再在本地聚合**，绝不臆造一个不存在的端点。

---

## C. `capabilities.py`：工具为什么要分三层

```python
READ_TOOL_NAMES      = [list_accounts, list_recent_transactions,
                        get_month_budget, reconcile_overview, list_categories]
WRITE_TOOL_NAMES     = [categorize_transactions, add_expense]
DISCOVERABLE_TOOL_NAMES = [suggest_category]   # 一开始不注册
```

- **只读层**：开箱注册、白名单放行，随便调，不改账；
- **写入层**：注册了但不在自动白名单里，调用必走 `PERMISSION_ASKED`；
- **可发现层**：`suggest_category` **默认根本不注册**，只有当内核报出"缺自动
  分类能力"、`StaticCatalog` 现场发现它之后才出现。

这三层正好对应权限的递进：看账不需要问，改账要问，"现造一个新能力再改账"
要问两次（先批准用新工具、再批准批量写入）。`build_catalog(tools)` 用关键词
元组描述这个候选能力：

```python
StaticCatalog([DiscoveredCandidate(
    tools.suggest_category,
    ("分类", "归类", "未分类", "对账", "category", "这笔算什么"),
    description="根据收款方/备注为交易建议一个分类（只读建议，不直接改账）",
)])
```

> 注意 `suggest_category` 本身是**只读建议**（返回建议分类，不落库）；真正改账
> 的是另一个写入工具 `categorize_transactions`。把"建议"和"落账"拆开，授权
> 边界才清楚：你可以允许它给建议，但拒接它批量改账。

---

## D. `five_beats.py`：把一次成功接管写成可断言的节拍

这是本篇唯一"跨宿主复用"的新代码。它不跑 Agent，只**读事件列表**，归纳出
节拍时间线：

```python
class Beat(StrEnum):
    WAKE = "wake"          # STRATEGY_SELECTED
    LONG_TAIL = "long_tail"# ≥2 个不同 TOOL_CALL
    GAP = "gap"            # CAPABILITY_MISSING
    DISCOVER = "discover"  # TOOL_DISCOVERED
    DELEGATE = "delegate"  # TOOL_CALL(tool == "delegate")
    AUTHORIZE = "authorize"# PERMISSION_ASKED
    DONE = "done"          # DONE
```

`beat_timeline(events)` 返回 `[(beat, 事件下标), ...]`，断言函数再检查**顺序与
因果**，而不只是"这些事件都出现过"：

```python
def assert_discover_beats(events):
    timeline = dict(beat_timeline(events))
    assert WAKE in timeline and GAP in timeline and DISCOVER in timeline
    assert timeline[GAP] < timeline[DISCOVER]   # 先有缺口，才谈发现
    assert timeline[AUTHORIZE] > timeline[GAP]  # 授权发生在补能力之后
    # LONG_TAIL：至少两个不同工具，证明这是多步长尾任务而非一问一答
    ...
```

**为什么要卡顺序？** 因为"事件都在但因果反了"也是坏的：如果授权发生在能力缺口
之前，说明权限闸是装样子；如果 DONE 不是最后一个，说明内核提前收尾了。
委派链路另有 `assert_delegate_beats`，它不要求 `DISCOVER`（派人是另一条补能力
路径），只要求 `delegate` 出现在 `done` 之前。

---

## E. `demo_agent.py`：两条链路的脚本模型

离线演示不能依赖真实 LLM，于是用"脚本模型"按调用次序返回预定响应。

### E.1 发现链路（ReconcileModel）

六次 `achat` 严格对应六拍：

1. 第 1 次是**路由分类调用**，返回一段纯 JSON：
   `{"strategy":"react","missing_capability":"缺少按收款方自动建议交易分类的能力"}`
   —— 这触发 `CAPABILITY_MISSING` → 发现 `suggest_category`；
2. 列出近期交易（`list_recent_transactions`）；
3. 对星巴克调新工具 `suggest_category`（先问一次授权）；
4. 调 `categorize_transactions` 批量补分类（再问一次授权）；
5. 拉预算对比（`get_month_budget`）；
6. 输出月报，点出娱乐超支。

`build_discovery_core(backend, auto_confirm=True)` 把这些装起来：
`llm_router=True`（让缺口由分类阶段产出）、`discovery=build_catalog(tools)`、
`AllowlistPolicy(READ_TOOL_NAMES, mode="auto")`（只读自动、其余询问）。
`deny=True` 的变体用 `auto_confirm=False` 跑同一条链路，专门验证**拒绝授权**时
新工具不被使用、未分类一笔不动、模型诚实停止。

### E.2 委派链路（DelegatingReconcileModel）

根的任务是"并行列出账户 + 出对账概览"。模型第一次行动就调 `delegate`，tasks
里写死两个子任务，各自只给**一个**只读工具：

```python
tasks = [{"goal": WORKER_GOAL_ACCOUNTS, "tools": ["list_accounts"]},
         {"goal": WORKER_GOAL_OVERVIEW, "tools": ["reconcile_overview"]}]
```

子员工由 `shell/employee` 的 `run_worker` 真正执行（第 36 篇）：独立小上下文、
工具白名单是根的子集、内部 `deny_all`、带默认预算、完成即销毁，结果通过
`delegate` 的工具结果**只回流给根**。根拿到两块结果后再输出一段汇总，全程
只读、不改账。

> 测试里有个易错点：`delegate` 回流的 `tool_result.preview` 在内核里被截断到
> 定长（约 200 字符），**不能整体 `json.loads`**。所以端到端测试只断言节拍、
> `delegate` 只调用一次、preview 里出现 `"ok": true`；"两个子员工各自成功"
> 改用直接 `await run_worker(...)` 拿完整 `WorkerResult` 来严格断言。这是
> "端到端验因果、直驱验内容"的分工。

### E.3 离线模型读消息要同时兼容 dict 与对象

热路径传给 `achat(messages, tools)` 的元素是 **OpenAI 形态的 dict**，不是
`ChatMessage` / `ToolSpec` 对象。脚本模型取角色 / 内容时若直接
`getattr(last, "role")` 会取到 `None`，导致无状态模型分不清自己是根还是子员工、
反复调 delegate。统一用一个双兼容取值器：

```python
@staticmethod
def _field(message, key):
    if isinstance(message, dict):
        return message.get(key)
    return getattr(message, key, None)
```

子员工的 goal 以固定前缀 `"子任务·"` 开头，模型扫描所有 user 消息拼出目标，
`goal.startswith("子任务·")` 即判定当前是子员工上下文——不依赖调用计数，天然
并行安全（根与子员工共用同一个模型实例也不会串）。

---

## F. 网页壳：把"嵌入前 / 嵌入后"并排放

`web_app.py` 只用标准库 `ThreadingHTTPServer`，监听 `127.0.0.1:8240`：

- GET 端点（`/api/accounts`、`/api/transactions`、`/api/budget`、`/api/overview`）
  读一份**固定假账**，左栏始终展示"有 3 笔未分类"的原生应用；
- `POST /api/run` 体里 `mode` 取 `discover` / `delegate`，**每次新建一份全新
  假账**跑 Core，所以可以反复点按钮而不会把演示账改"干净"；
- 返回事件序列、策略、最终文本，以及这份一次性假账跑完后的
  `overview_after`（未分类数 / 超支分类）。

前端 `web/index.html` 是财务翡翠绿终端风双栏：左边账户卡、交易表（未分类行
高亮）、预算进度条；右边两个按钮分别触发两条链路，事件流按 140ms 逐条"播放"，
最后给出根员工结果与"补分类后未分类 = 0"的对照。无 emoji、无外部依赖、无构建。

---

## G. 测试地图（17 例在守什么）

| 组 | 代表用例 | 守的不变量 |
|---|---|---|
| 假账 | 种子整数分 / 3 笔未分类 / 娱乐超支 | 演示的"因"确定 |
| 写原语 | 补分类、记支出改账；非法 id 报错 | 写操作有边界 |
| 工具层 | 预算仅娱乐超支、建议命中与落空、元→分 | 领域换算正确、不臆造分类 |
| CLI argv | 账户命令锁定、口令只在 env、跨账户聚合、非零 / 坏 JSON 抛错、缺配置早失败 | 真机对接不手滑、不泄密 |
| 发现五拍 | 批准：两次授权 + 3 笔清零 + 月报；拒绝：零副作用 | 补能力链路因果正确 |
| 委派五拍 | `delegate` 仅一次、两子任务 goal 不同且单一只读工具、账本不被改 | 派人链路收窄、只读 |
| 直驱子员工 | `run_worker` 两个 goal 均 `ok`、`granted_tools/steps` 为单一工具 | 子员工真的成功（补 preview 截断） |

跑法（Windows 本地注意带临时目录，避免系统 Temp 权限问题）：

```bash
.venv/Scripts/python.exe -m pytest tests/test_host_j_actual.py \
  --basetemp="$env:TEMP\pytest-yai" -q
```

---

## H. 自检（读完本篇应能回答）

1. 为什么金额在账里用整数分、只在展示时换算成元？如果两处都各自换算会怎样？
2. `suggest_category`（只读建议）与 `categorize_transactions`（写入）为什么要
   拆成两个工具？合并成一个会让授权边界发生什么变化？
3. 五拍断言为什么要检查事件**下标顺序**（如 `GAP < DISCOVER`），只检查"事件
   都出现过"会漏掉什么问题？
4. `CliActualBackend` 为什么要让 `runner` 可注入？这给测试带来什么好处？
5. 为什么 `delegate` 回流的 preview 不能整体 `json.loads`？端到端与直驱测试
   分别负责验证什么？
6. 离线脚本模型为什么必须用 dict / 对象双兼容的方式读消息角色？
