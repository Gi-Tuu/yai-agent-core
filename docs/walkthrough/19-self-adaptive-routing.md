# 逐行讲解 19 · 自校准路由：让 Core 从每次执行的反馈里学习

> 对应源码：
> - `src/yai_core/learning/features.py`（任务特征）
> - `src/yai_core/learning/outcomes.py`（从事件流抽反馈）
> - `src/yai_core/learning/bandit.py`（上下文老虎机选择器）
> - `src/yai_core/spi/learning.py`（第六个 SPI：RouteSelector）
> - 接线：`kernel/router.py`、`core.py`
> - 验证：`scripts/router_bench_tasks.py`、`scripts/benchmark_router.py`
>
> 前置：第 05 篇（路由在干什么）、第 02 篇（SPI 是什么）、第 06 篇（事件流）。

## 0. 为什么要让路由"会学习"

第 05 篇讲过，路由靠一套**中文关键词规则**决定任务走哪种打法：

- 命中"查/搜/统计" → react（调工具）
- 命中"然后/接着/先"且有行动词 → plan（先规划）
- 命中"随便/你看着办"且很短 → clarify（反问澄清）
- 其余 → direct（直接回答）

这套规则在第 05 篇的阶段是**对的选择**：零成本、确定性、好测试。但它有一个天生的
天花板——**它是开环的**：

> 任务来了，规则判一次，然后一路执行到底。执行结果是好是坏，路由器永远不知道，
> 下次遇到同类任务还是照同一套关键词判。

于是这些反例它永远学不会：

| 用户输入 | 规则判成 | 实际应该 |
|---|---|---|
| "帮我看看上周的日记" | 没有行动词 → direct | 该调 `recall_memories`（react） |
| "Search my notes for Q3" | 英文不命中任何词表 → direct | 该搜索（react） |
| "那个东西帮我处理下" | 没有"随便" → direct | 信息不全，该 clarify |

我们要做的不是把关键词表越堆越长（那是打补丁，永远追不完长尾），而是让路由器
**闭环**：每次任务跑完，回头看一眼"这次选的打法效果如何"，把这个结果记下来，
下次遇到**同类任务**时调整选择。这就是项目名里 **self-adaptive（自适应）** 真正落地
的地方。

为什么不用神经网络、不做 embedding？因为第一版的目标是：**零第三方依赖、纯标准库、
离线可测、行为可解释**。我们用一个经典、轻量、数学上站得住的工具——
**上下文老虎机（Contextual Bandit）**。

## 1. 全局：上下文老虎机在学什么

先抛开代码，建立直觉。

### 1.1 四个"拉杆"

老虎机（bandit）这个词来自赌场的多臂老虎机：有若干个拉杆，每个拉杆中奖概率不同，
你要一边试、一边学，逐步把次数押到中奖率最高的拉杆上。

我们的"拉杆"（arm）就是**四个路由策略**：

```
direct   直接回答
react    调工具的 ReAct 循环
plan     先规划再执行
clarify  反问澄清
```

### 1.2 每个 arm 记两个数：α（成功）和 β（失败）

对每一类任务，我们给四个 arm 各维护一对计数 `(α, β)`：

- 这个 arm 被选后效果好 → α 变大；
- 效果差 → β 变大。

直觉上，`α / (α + β)` 就是"这个 arm 在这类任务上的成功率估计"。

### 1.3 关键动作：Thompson Sampling（汤普森采样）

怎么根据 `(α, β)` 决定下一次拉哪个杆？不是简单地"永远选均值最高的"——那样冷启动时
会锁死在第一个碰巧成功的 arm 上，再也不试别的。我们用 **Thompson Sampling**：

> 给每个 arm 维护一个 Beta 分布 `Beta(α, β)`，它表示"这个 arm 成功率大概是多少"的
> **概率分布**。每次决策，从四个分布里各**随机抽一个数**，抽中最大数的 arm 就被选中。

Beta 分布的形状随计数变化：

- 冷启动 `α=β=1`：在 0~1 上是一条均匀的平线——"我完全没把握，任何成功率都可能"；
- 老成功 `α` 大：分布鼓包靠右，抽样容易抽到高值（倾向继续用它）；
- 老失败 `β` 大：分布鼓包靠左，抽样容易抽到低值（倾向避开它）。

它的妙处是**探索和利用自动平衡**：

- 一个已经证明很好的 arm，分布靠右，大概率抽中高值 → 被利用；
- 一个还没怎么试过的 arm，分布很宽很平，偶尔会抽中高值 → 仍有机会被探索；
- 一个连续失败的 arm 不会被"永久判死"，分布尾巴还在，偶尔抽中高值就能**自愈**。

这就是为什么本版**不需要额外的 ε-greedy 随机探索**（后面 benchmark 会用数据说明）。

### 1.4 上下文：不同类型的任务各学各的

不能让"写一首诗"和"查一下订单"共用一组 `(α, β)`——它们的最优打法本来就不同。
所以我们先把任务压成一个**离散的上下文标签**，同类任务共享一组计数，这就是
"上下文（contextual）老虎机"。

### 1.5 闭环长这样

```
任务 ──► 抽特征 ──► 找到这一类任务的 (α,β) 表 ──► Thompson 采样选 arm
                                                        │
                                                  执行（run）
                                                        │
任务结束 ◄── 从事件流抽 reward（成功？花了多少成本？）◄──┘
              │
              └──► α += reward，β += 1 - reward（只在这一类任务的计数上）
```

学习**只发生在任务结束后一次**，不影响当次执行的稳定性——这是一条重要的安全设计。

## 2. 特征：`features.py`

bandit 不直接读原始文本，而是读一组**标准库就能算**的离散特征。

### 2.1 为什么原文不进上下文键

```python
@dataclass(frozen=True)
class TaskFeatures:
    text: str
    length_bucket: str       # empty / short / medium / long
    action: bool             # 命中行动词
    multistep: bool          # 命中多步词
    vague: bool              # 命中模糊词
    question: bool           # 疑问信号
    has_latin: bool          # 含英文词
    has_data_object: bool    # 含数据/工具对象名词
    has_unspecified: bool    # 含模糊指代/虚动词
    tools_bucket: str        # none / few / many
```

`text` 字段保留原始任务，但它**只给规则先验分类用，不进 `key()`**。原因写在 docstring 里：
如果把原文放进键，那每句话都是一个独一无二的上下文，永远无法把"这次的经验"迁移到
"同类的下一次任务"，等于没有学习。

### 2.2 九个特征怎么算

长度和工具数先分桶（连续值离散化，避免稀疏）：

```python
_SHORT_MAX = 8
_MEDIUM_MAX = 30
_FEW_MAX = 8
```

- `length_bucket`：strip 后按字符数 → 空 / 短(≤8) / 中(≤30) / 长。
- `tools_bucket`：注册表里工具数 → 无(0) / 少量(≤8) / 大量。

布尔信号靠词表命中。注意一个关键设计——**复用规则路由的词表，不另造一份**：

```python
from yai_core.kernel.router import _ACTION_HINTS, _CLARIFY_HINTS, _PLAN_HINTS
```

这样"规则看到的信号"和"学习器看到的信号"一致，不会两边对同一任务判断漂移。

学习器在规则词表之外，多看了三样东西（这正是为了抓住第 0 节那张反例表）：

```python
_MULTISTEP_EXTRA = ("并", "再", "最后", "形成", "汇总", "归类", "分组", "排序")
_DATA_OBJECT_HINTS = ("订单", "日记", "待办", "天气", "客户", "笔记", ...
                      "notes", "order", "orders", "weather", "customer", "todo", "pending")
_UNSPECIFIED_HINTS = ("那个", "那边", "之前说", "相关的", "东西", "弄", "搞",
                      "处理下", "跟进下", "看看吧", "安排安排")
_LATIN_WORD = re.compile(r"[a-z]{2,}")
```

- `has_data_object`：哪怕没有显性动词，只要提到"日记/订单/notes"，也可能是要工具
  （"帮我看看上周的日记"）；
- `has_unspecified`："那个东西帮我处理下"信息不全，该澄清；
- `has_latin`：用正则识别连续拉丁字母，抓英文意图（"Search my notes"）。

中英文匹配用一个小 helper 统一，和规则路由同口径：

```python
def _hit(text: str, lowered: str, hint: str) -> bool:
    """ASCII 词按小写匹配（英文/缩写），中文按原文匹配。"""
    return (hint in lowered) if hint.isascii() else (hint in text)
```

### 2.3 上下文键

```python
def key(self) -> tuple:
    return (
        self.length_bucket, self.action, self.multistep, self.vague, self.question,
        self.has_latin, self.has_data_object, self.has_unspecified, self.tools_bucket,
    )
```

这是一个九元组，两个任务只要这九项相同，就共享一组 Beta 计数。

> **为什么是 9 维而不是一开始的 6 维？** 这是 benchmark 反哺的结果（见第 7 节）。
> 最初只有长度/行动/多步/模糊/疑问/工具数 6 维，结果 ground-truth 完全不同的任务
> 挤在同一个键里（同一个桶里既有该 direct 的、又有该 react/clarify 的），bandit 收到
> 自相矛盾的奖励，怎么学都学不动。加了"英文/数据对象/模糊指代"三个细分维度后，
> 同桶冲突归零。特征工程不是为了好看，是让学习信号不打架。

## 3. 反馈：`outcomes.py`

这是闭环的眼睛：一次 `run` 跑完，免费产出了一整条 `AgentEvent` 事件流（第 06 篇），
我们把它压成一个标量奖励。整个函数是**纯函数、零模型调用、离线可测**。

### 3.1 先数清楚发生了什么

`extract_route_outcome` 遍历事件流，累计：

- `tool_calls`：调了几次工具（`TOOL_CALL` 事件）；
- `tool_failures`：几次工具结果 `ok=False`（`TOOL_RESULT`）；
- `clarify_rounds`：反问了几轮（`CLARIFY_REQUESTED`）；
- `plan_steps`：计划拆了几步（`PLAN_CREATED`）；
- `errors`：几个 `ERROR` 事件；
- `final_text` / `done_strategy`：最终文案、最终收尾策略。

### 3.2 成败判定

```python
success = (
    not aborted
    and not hit_max_iters
    and not final_empty
    and tool_failures == 0
    and errors == 0
    and not (chosen == Strategy.DIRECT and unable_hint)
)
```

逐条解释：

- `aborted`：最终以 clarify 收尾，说明用户没补信息、任务被放弃了；
- `hit_max_iters`：工具调用撞满了迭代上限（典型是"该 plan 却只 react"，循环收不拢）；
- `final_empty`：最终给了空答案；
- `tool_failures == 0`、`errors == 0`：没有工具失败、没有报错；
- 最后一条最关键：**只有当路由选了 direct 时**，最终文案里出现"无法/没有数据/缺少权限"
  才算失败。

为什么只在 direct 下判 `unable_hint`？因为 direct 误判工具型任务时，模型手里没工具，
只能说"我无法查询"——这是路由错了的铁证。而其他策略（react/plan）真无能时，通常已经
伴随工具失败或能力缺口事件，会被 `tool_failures` 抓住，不该再用文案重复惩罚。

`unable_hint` 用一组短语 + "否定词×能力对象词"组合判定，兼容中英文：

```python
_UNABLE_HINTS = ("无法", "没有权限", "没有数据", "查不到", "缺少", "unable to", ...)
```

### 3.3 奖励 = 成败 − 成本

```python
cost = W_TOOL * tool_ratio + W_PLAN * (1.0 if plan_steps > 0 else 0.0) + W_CLAR * clar_ratio
reward = (1.0 if success else 0.0) - cost
reward = max(0.0, min(1.0, reward))      # 裁剪到 [0, 1]
```

- 成败是 0/1 的主信号；
- 成本项（工具用量、计划开销、澄清开销）只在"都能成功"的策略之间做微调——比如同样能
  完成，零工具的 direct 比调一堆工具的 react 更省，就该略高一点；
- 权重很小（`W_TOOL=0.05 / W_PLAN=0.10 / W_CLAR=0.15`），**成本永远压不过成败**；
- 失败一律裁剪到 0（不会出现负奖励）。

产出的 `RouteOutcome` 既保留可解释的原始信号，也给出 bandit 直接吃的 `reward`。

## 4. 选择器：`bandit.py`

这是学习内核本体，约 150 行，纯标准库（`random` / `json` / `pathlib`）。

### 4.1 状态长什么样

```python
# state[key][arm_value] = [alpha, beta]
self._state: dict[tuple, dict[str, list[float]]] = {}
self._obs: dict[tuple, int] = {}     # 每个上下文的真实观测条数（不含先验伪计数）
```

每个上下文键对应一张表，表里四个 arm 各一对 `[α, β]`。

### 4.2 冷启动：规则先验

第一次见到某类任务时，先让规则路由判一次，把它推荐的 arm 加几个"伪成功"：

```python
def _seed(self, rule: Strategy | None) -> dict[str, list[float]]:
    table = {s.value: [_BASE_ALPHA, _BASE_BETA] for s in ARMS}   # 全部 α=β=1
    if rule is not None:
        a, b = table[rule.value]
        table[rule.value] = [a + self.prior_strength, b]         # 规则 arm 再加 2
    return table
```

默认 `prior_strength=2`：规则 arm 变成 `α=3`，其余 `α=1`。配合 4.3 节"零真实反馈时
确定性取先验最强臂"，于是**第一次建议就等于规则的判断**，不会冷启动瞎选；第一条真实
反馈进来后才放开 Thompson 采样，而这个先验很弱，真实反馈几次就能覆盖它。

### 4.3 建议：`suggest`

```python
def suggest(self, task, registry) -> RouteSuggestion | None:
    features = TaskFeatures.from_task(task, registry)
    # 硬规则区域：学习器不表态
    if features.length_bucket == "empty" or features.tools_bucket == "none":
        return None
    key = self._key(features)
    table = self._state.get(key)
    if table is None:
        table = self._seed(self.router.classify(features.text, registry))
        self._state[key] = table
    total = sum(a + b for a, b in table.values())
    if total < self.min_samples:
        return None
    observed = self._obs.get(key, 0)
    if self.rng.random() < self.epsilon:
        arm = self.rng.choice(ARMS)
        source = "bandit:explore"
    elif observed == 0:
        # 该上下文还没有任何真实反馈，只有规则先验：确定性取先验最强臂（=规则判断），
        # 不做随机采样，避免弱先验下第一次就蒙到 plan/clarify 把任务带偏。
        arm = max(ARMS, key=lambda s: table[s.value][0] / (table[s.value][0] + table[s.value][1]))
        source = "bandit:thompson"
    else:
        arm = max(ARMS, key=lambda s: self.rng.betavariate(*table[s.value]))
        source = "bandit:thompson"
    a, b = table[arm.value]
    return RouteSuggestion(arm, source, a / (a + b) if (a + b) > 0 else 0.0)
```

四个要点：

1. **硬规则区域不表态**：空任务（必须澄清）、宿主无工具（只能 direct）是确定性下限，
   学习器无权越过，直接返回 `None` 交回规则。
2. **证据不足不表态**：`min_samples`（默认 4）是冷启动闸门。注意规则先验本身已经贡献
   伪计数（4 个 arm ×2 + 先验 2 = 10），所以有工具的非空任务首次就能表态。
3. **零真实反馈时确定性跟随规则**：第一次见到某类任务时，先验很弱（规则 arm `α=3`、
   其余 `α=1`），若直接 Thompson **采样**，约有一半概率随机蒙到别的 arm，把本该查工具的
   任务带偏成 plan/clarify。所以这个桶还没有任何真实回灌（`observed == 0`）时，改为
   确定性地取先验均值最高的臂（正好就是规则判断）；第一条真实反馈进来后才放开随机采样。
4. **Thompson 采样**就是这一行：
   `max(ARMS, key=lambda s: self.rng.betavariate(*table[s.value]))`
   ——从每个 arm 的 Beta 分布抽一个数，取最大。`random.betavariate` 是标准库自带的。

`epsilon` 默认 0（纯 Thompson）。代码里保留了 ε-greedy 分支，仅作为离线测试或特殊宿主
的可选安全网。**为什么默认 0？** 第 7 节的超参扫描显示：在 4 个 arm 上，额外的随机
探索有 3/4 概率选到错 arm，纯属浪费；Beta 后验本身已经提供了更聪明的探索。

### 4.4 反馈：`record`

```python
def record(self, task, registry, chosen, outcome) -> None:
    features = TaskFeatures.from_task(task, registry)
    key = self._key(features)
    table = self._state.get(key)
    if table is None:
        table = self._seed(None)
        self._state[key] = table
    reward = max(0.0, min(1.0, outcome.reward))
    a, b = table[chosen.value]
    table[chosen.value] = [a + reward, b + (1.0 - reward)]
    self._obs[key] = self._obs.get(key, 0) + 1
```

这叫 **fractional update（分数更新）**：奖励是连续的，成功就给 α 加 `r`、β 加 `1-r`。
- 完美成功 `r=1`：α+=1，β 不变；
- 彻底失败 `r=0`：β+=1，α 不变；
- 勉强成功 `r=0.8`：两边都加一点，表达"成是成了，但成本高，不太优"。

注意 `suggest` 和 `record` 都用**同样的方式**重新抽特征、算键，保证建议和反馈落在
同一张表上。

### 4.5 观测与持久化

- `arm_means(features)`：返回各 arm 的 Beta 均值（解析值，不采样），给 benchmark 和
  未来 UI 用；
- `contexts()` / `observed_count()`：调试用；
- `to_dict / from_dict / save / load`：把所有上下文的计数表序列化成 JSON，宿主可以
  跨进程/跨天保存学习成果。纯 JSON、零依赖。

`contextual=False` 是给 benchmark 消融实验用的开关：所有任务共享一个全局键，退化成
普通 4-arm bandit，用来反向证明"上下文特征确实有用"。产品路径永远是 `True`。

## 5. 契约：`spi/learning.py`（第七个 SPI）

学习器通过一个 `Protocol` 接入内核，和 model/memory/policy/discovery/sandbox 并列：

```python
@runtime_checkable
class RouteSelector(Protocol):
    def suggest(self, task: str, registry: ToolRegistry) -> RouteSuggestion | None: ...
    def record(self, task: str, registry: ToolRegistry,
               chosen: Strategy, outcome: RouteOutcome) -> None: ...
```

两个方法都是**同步**的：`suggest` 只做本地查表和采样，不发起网络或模型调用，所以能
安全地放在路由热路径上。

有一个容易忽略的设计细节：契约方法的入参是**原始任务文本 + 注册表**，而不是 learning
包的 `TaskFeatures` 对象。docstring 解释了原因——避免循环导入：

```
kernel 路由层只依赖 spi 契约，不反向依赖 learning 实现层；
"特征怎么抽"是学习器实现自己的内部事务。
```

这样别人完全可以写一个不基于 Beta 分布的学习器（在线逻辑回归、外部画像服务……），
只要实现这两个方法就能替换，内核主循环一行不用改。

## 6. 接线：`router.py` 与 `core.py`

### 6.1 路由的四级优先级

`AdaptiveRouter` 多了一个 `selector` 参数，`aclassify` 的决策顺序变成：

```python
# 1) 硬规则（确定性下限，最高优先级）
if not text or len(registry) == 0:
    return self._rules(task, registry)
# 2) 学习层建议：注入了 selector 且未配置 LLM 路由时前置
if self.selector is not None and self.model is None:
    learned = self._learned_decision(task, registry)
    if learned is not None:
        return learned
# 3) LLM 分类（仅当配置了路由模型）；任何异常/超时/非法输出都回退规则
if self.model is not None:
    ...
# 4) 规则兜底（永远可用）
return self._rules(task, registry)
```

学习器的建议被包成一个普通 `RouteDecision`，`source` 是 `bandit:thompson`，
`reason` 里带上置信度，方便在事件流里看到"这次是学习器在拿主意"。

第一版里，学习器只在**没有配置 LLM 路由**（`model is None`）时前置——LLM 路由和学习
路由是两条增强路径，先不叠加，行为更可预测。

### 6.2 默认关闭：不传学习器，行为逐字节不变

`AgentCore` 的构造函数新增两个参数，但默认都是 `None`：

```python
learning: RouteSelector | None = None,
on_feedback: FeedbackHook | None = None,
```

**不注入学习器时，路由完全走规则，和没有这套功能时一模一样。** 这是一条刻意的安全
默认：这是一个可插拔的能力，不是偷偷改变所有宿主行为的开关。要启用，宿主显式传入：

```python
from yai_core.learning import ContextualBanditSelector

core = AgentCore(model, learning=ContextualBanditSelector(seed=42))
```

### 6.3 反馈在事件流正常结束后回灌一次

回灌闭合在 `core.astream` 的收尾，而不是 `core.run` 里——因为网页、终端这类流式宿主
直接消费 `astream()` 的事件流，从不调用 `run()`；若只在 `run()` 回灌，流式宿主就永远
学不到东西。`astream` 一边转发事件一边收集，正常跑完（没被取消）时在 `finally` 里
调用一次 `_record_feedback`：

```python
async def astream(self, task):
    events, strategy, completed = [], None, False
    try:
        async for event in self._loop.astream(task):
            await self.channel.emit(event)
            events.append(event)
            if "strategy" in (event.data or {}):
                strategy = Strategy(event.data["strategy"])
            yield event
        completed = True
    finally:
        # 只有正常跑完才回灌；被取消（用户中途终止）不计入，避免污染学习
        if completed:
            chosen = strategy or self.router.classify(task, self.registry)
            self._record_feedback(task, chosen, events)
```

`_record_feedback` 本身只做"抽结果 + 回灌"：

```python
def _record_feedback(self, task, chosen, events) -> None:
    if self.learning is None:
        return
    outcome = extract_route_outcome(
        events, chosen,
        max_iters=self.max_iters, clarify_budget=self.max_clarify_rounds,
    )
    self.learning.record(task, self.registry, chosen, outcome)
    if self.on_feedback is not None:
        self.on_feedback(task, chosen, outcome)
```

- 反馈**不进当次事件流**（事件流都跑完了才算得出结果），所以不会污染、不会影响这一次；
- **被取消的任务不回灌**：`completed` 只在事件流正常耗尽时置真，用户中途停止不会被当成
  负样本；
- 宿主可以传 `on_feedback` 回调，把每次路由的成败发到自己的日志/UI；
- `route_learning_status()` 透出学习器的可观测快照（启用了没、各上下文计数），未启用时
  返回 `{"enabled": False}`。

## 7. benchmark：怎么证明"学习确实发生"

不能只说"它会学习"，要能**离线、确定性、可复现地证明**。这就是 M4 的两个脚本。

### 7.1 不跑真实模型，而是"仿真环境"

`scripts/router_bench_tasks.py` 是 60 条人工标注 ground-truth 的任务（direct/react/plan/
clarify 各 15 条）。`scripts/benchmark_router.py` 里有一个 `simulate(gt, chosen)` 函数，
按"真实最优策略 gt × 实际选中策略 chosen"组合，**构造出与真实 AgentLoop 一致的事件流**：
- 该 react 却选 direct → 模型说"无法查询/缺少工具"（无能）；
- 该 plan 却选 react → 凑满 6 个工具调用，撞迭代上限；
- 该 clarify 却硬执行 → 工具 `ok=False`；
- 清晰任务却 clarify → 最终以 clarify 收尾（放弃）。

关键：仿真只负责"造事件"，**奖励仍然交给真实的 `extract_route_outcome` 计算**，不是
benchmark 自己手编的。这保证了评测用的打分逻辑和线上完全一致。

### 7.2 五条对比线

| 线 | 含义 |
|---|---|
| rules | 纯规则，永远不学习（基线） |
| **bandit** | 完整的上下文 Thompson bandit（本方案） |
| oracle | 永远选 ground-truth（完美路由天花板，非产品能力） |
| 消融 A：无先验 | 关掉规则先验，冷启动均匀分布 |
| 消融 B：无上下文 | 退化成所有任务共享一组计数 |

每条线跑 8 个随机种子、3 轮（每轮把 60 条任务洗牌重来，模拟任务陆续到来），再画滑动
窗口成功率。

### 7.3 结果（8 seeds × 3 epochs）

| 对比线 | 末段成功率 | 整体成功率 |
|---|---|---|
| 规则（不学习） | 73.3% | 71.7% |
| **自校准 bandit** | **89.6%** | **80.3%** |
| Oracle | 100% | 100% |
| 消融 A：无规则先验 | 88.5% | 75.6% |
| 消融 B：无上下文 | 70.8% | 64.0% |

学习曲线（`docs/assets/router-learning-curve.svg`）上，绿色 bandit 冷启动跟随规则、随后
一路爬升，后期稳定在 90% 附近，灰色 rules 平在 73% 左右，蓝色 oracle 钉在 100%。
三个对照各自证明一件事：

1. **学习有效**：bandit 末段比 rules 高约 16 个百分点，且是真实爬升；
2. **规则先验有用**：bandit 整体 80.3% 高于消融 A 的 75.6%，无先验冷启动更颠——
   先验让系统"开局不掉队"；
3. **上下文特征是关键**：bandit 末段比消融 B 高约 19 个百分点，无上下文时连规则都不如，
   证明"分桶学"是对的。

benchmark 还内置一条**硬校验**：bandit 末段必须显著高于 rules（+5pp），否则脚本以非零
退出码结束——防止以后改坏了奖励信号，跑出一条"看起来在学习"的假曲线。

### 7.4 为什么没到 100%（诚实的边界）

bandit 末段是 89.6% 而不是 100%，这是真实的，不该靠刷分掩盖：

- 有些长尾上下文只有一两条任务，3 轮里反馈样本太少，先验没被完全翻转；
- Thompson 采样天然有随机波动；
- "direct 误判成 react 但模型零成本也答对了"这种情况没有失败信号，学习器学不会
  （但它不影响成功率，只影响成本）。

超参（纯 Thompson、`prior_strength=2`、9 维特征）都不是拍脑袋，而是 benchmark 扫描后
反哺成默认值的。

## 8. 怎么自己跑

```powershell
# 离线 benchmark（不需要网络、不需要 API Key）
.\.venv\Scripts\python.exe scripts\benchmark_router.py            # 8 seeds × 3 epochs
.\.venv\Scripts\python.exe scripts\benchmark_router.py --seeds 4 --epochs 2

# 产物：benchmark_out/router_bench.json、router_learning_curve.svg
# （benchmark_out 是可复现生成物，不入库；定稿曲线存 docs/assets/）

# 在自己的宿主里启用自校准
core = AgentCore(model, learning=ContextualBanditSelector(seed=42))
# 想跨天保留学习成果：
#   selector.save("bandit_state.json")  /  ContextualBanditSelector.load(...)
```

### 8.1 真实宿主通电的三个坑（host_e 数字员工已踩平）

内核示例是"一个 core 跑到底"，但真实宿主（网页/终端）每个任务都新建一个 `AgentCore`。
要让学习真正跨任务累积，有三个容易错的点，host_e（`examples/host_e_sales_crm/`）都处理了：

1. **学习器必须由长生命周期的宿主持有，而不是建在 core 里。**
   每个任务新建 core 时若也新建 selector，上一个任务的反馈就丢了。host_e 的 `AgentBridge`
   在启动时建**一个**共享的 `ContextualBanditSelector`，之后每个任务 `AgentCore.auto(...,
   learning=self._learning)` 把同一个学习器传进去，反馈才会在任务间累积。

2. **持久化用"启动加载 + 正常结束落盘"，并容忍损坏。**
   `load_learning(path)`：文件不存在或 JSON 损坏时都冷启动新建，绝不抛给用户；
   每个任务**正常结束**（不是被取消）后 `save_learning()` 落盘。host_e 网页的
   "路由学习"面板和命令行终端共用同一个 `route_learning.json`，两边看到的进度一致。

3. **流式宿主走 `astream` 也会回灌（见 6.3），但取消不回灌。**
   用户中途停止任务时，事件流没有正常结束，`completed` 为假，这一次不进学习——
   否则"用户嫌它做错了主动打断"会被错误地当成一次正常结果污染后验。

> 内核默认仍是 `learning=None`（行为与不接入完全一致）；host_e 是**宿主侧 opt-in**
> 通电，没有翻内核默认。网页权限条下方的"路由学习"面板可看到累计回灌次数、各场景
> 桶的偏好策略与置信度，并可一键重置。

## 9. 设计取舍小结

- **闭环开在路由，不开在执行中途**：只在任务结束后学一次，当次行为完全确定、可回放；
- **纯标准库、零依赖、可序列化**：bandit 学习不引入 numpy/embedding/网络，符合内核红线；
- **默认关闭、硬规则不可越过**：空任务/无工具是确定性下限，学习器只能在"本来就该交给
  判断"的区域里优化；
- **SPI 化**：bandit 只是一个默认实现，宿主可以整体替换学习算法；
- **可证伪**：五线 + 消融 + 硬校验，让"自适应"是可复现的数字，而不是 PPT 上的词。

## 10. 自检

1. 为什么说关键词规则路由是"开环"的？它学不会哪类反例？
2. Thompson Sampling 为什么能同时做到"利用好 arm"和"探索没把握的 arm"？α、β 分别
   累计什么？
3. 为什么原始任务文本不能进 `TaskFeatures.key()`？9 维特征比 6 维多解决了什么问题？
4. `extract_route_outcome` 为什么只在路由选了 direct 时，才用"无法/没有数据"文案判失败？
5. 规则先验（`prior_strength`）解决了什么问题？完全关掉先验（消融 A）曲线长什么样？
6. 为什么学习器默认关闭、且在空任务/无工具时返回 `None`？
7. 学习为什么放在 `run` 结束后只做一次，而不是执行过程中实时更新？
8. 消融 B（无上下文）为什么反而比规则还差？它证明了什么？
9. `RouteSelector` 契约为什么用"原始任务文本"入参，而不是 `TaskFeatures`？
