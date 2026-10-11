# 逐行讲解 32 · 成本·延迟感知选档 tiering：该花才花，选档本身不花钱

> 对应文件：
> - 选档策略：`src/yai_core/kernel/tiering.py`（`STANDARD` / `STRONG` /
>   `TierDecision` / `TierPolicy`）；
> - 接进执行循环：`src/yai_core/kernel/loop.py`（`_react_cycle` 每轮选档、
>   `_make_plan` / `_handle_replan` 的规划档位、DONE 成本汇总）；
> - 接线开关：`src/yai_core/core.py`（构造参数 `cost_aware`）；
> - 事件类型：`src/yai_core/types.py`（`EventType.TIER_CHANGED`）；
> - 离线测试：`tests/test_tiering.py`、`tests/test_replan.py`。
>
> 读前先读第 6（loop）、8（model 与 core）、31（replan）。本章让内核在
> "便宜快"与"强但贵"两档模型之间，按任务难度自动切换。

## A. 为什么需要选档

真实模型 API 通常分档：一个便宜、快、够用（日常工具调用、简单问答），另一个
更强但更贵、更慢（复杂多步拆解、纠偏）。如果**所有**调用都走强档，简单任务
也在烧钱、还更慢；如果**全走**便宜档，遇到复杂计划或工具反复失败时又做不好。

理想行为是"**该花才花**"：

- 绝大多数执行轮走便宜档；
- 只有真正需要强推理时——生成/修订计划、工具刚失败要纠偏、执行了好几轮还没
  完成（说明任务比预期复杂）——才临时升强档；
- 强档把难点解决后，后续轮次又回到便宜档。

关键约束：**选档本身是确定性规则，不额外调用模型、不花钱**。它只决定下一次
`achat` 时给模型传哪个档位标记。

## B. 两个档位与一次决策结果

先定义档位常量和决策结果：

```python
#: 便宜快档（日常工具调用 / 简单问答）。
STANDARD = "standard"
#: 强档（复杂拆解 / 纠偏 / 后段卡住）。
STRONG = "strong"


@dataclass(frozen=True)
class TierDecision:
    """一次选档结果：档位 + 可审计理由。"""

    tier: str
    reason: str
```

- 档位只用两个字符串标记，不绑定任何具体厂商。"standard 对应哪个模型、strong
  对应哪个模型"由 `ModelProvider` 自己决定（如 OpenAICompatProvider：standard →
  `model`、strong → `strong_model`，没配 strong 时安全回退同档）。这样内核不
  依赖任何一家 API。
- `TierDecision` 是不可变 dataclass，除了档位还带一句 `reason`。理由会进
  `tier_changed` 事件，宿主和评委能看清"这一轮为什么升档"，而不是看到一个
  凭空的档位变化。

## C. `TierPolicy`：三条保守升级规则

策略类构造参数决定规则松紧：

```python
class TierPolicy:
    def __init__(
        self,
        *,
        default_tier: str = STANDARD,
        strong_for_planning: bool = True,
        strong_on_failure: bool = True,
        escalate_after_iter: int = 3,
    ) -> None:
        self.default_tier = default_tier
        self.strong_for_planning = strong_for_planning
        self.strong_on_failure = strong_on_failure
        self.escalate_after_iter = escalate_after_iter
```

| 参数 | 默认 | 含义 |
|---|---|---|
| `default_tier` | standard | 无特殊情况时的档位（省钱、低延迟） |
| `strong_for_planning` | True | 生成 / 修订计划时用 strong（步骤拆解是重活） |
| `strong_on_failure` | True | 工具失败后的纠偏轮用 strong，提高一次改对概率 |
| `escalate_after_iter` | 3 | 执行到第几轮（从 0 起）仍未完成则升 strong；负数 = 关闭此规则 |

### 规划档：`for_planning`

```python
def for_planning(self) -> TierDecision:
    """生成 / 修订计划时的档位（默认 strong）。"""
    if self.strong_for_planning:
        return TierDecision(STRONG, "生成/修订计划需要复杂拆解，用强档")
    return TierDecision(self.default_tier, "规划不升档，用默认档")
```

`_make_plan`（初次规划）和 `_handle_replan`（第 31 篇，修订计划）都通过它
取档。步骤拆解需要把模糊任务拆成有序、不遗漏的步骤，是公认的强推理场景，
默认给 strong；宿主若想极致省钱可关掉。

### 执行档：`select`

```python
def select(
    self,
    *,
    strategy: Strategy,
    iteration: int,
    had_failure: bool,
) -> TierDecision:
    """决定一个执行轮的档位；规则按"该花才花"保守升级。"""
    if had_failure and self.strong_on_failure:
        return TierDecision(
            STRONG, "工具失败后的纠偏轮，升强档以提高一次改对的概率"
        )
    if self.escalate_after_iter >= 0 and iteration >= self.escalate_after_iter:
        return TierDecision(
            STRONG,
            f"执行到第 {iteration} 轮仍未完成，任务比预期复杂，升强档",
        )
    return TierDecision(self.default_tier, "常规执行轮，用便宜快档")
```

规则按优先级从上到下，**命中即返回**：

1. **失败纠偏优先**：上一轮有工具失败（`had_failure`）且开启了 `strong_on_failure`
   → strong。这是最该用强档的时刻：要让模型理解失败原因、一次改对，而不是用
   弱模型反复撞墙。
2. **后段卡住**：`iteration` 达到 `escalate_after_iter`（默认第 3 轮，轮次从 0
   起，即 0/1/2 是前三轮）仍未完成 → strong，说明任务比预想的复杂。
   `escalate_after_iter >= 0` 这个判断让"负数"能真正关闭该规则。
3. **否则默认档**：常规执行轮用便宜快档。

注意 `strategy` 参数当前不参与分支，只作为上下文传入，为以后按 DIRECT/REACT/
PLAN 区分规则留位置；默认规则对所有策略一致。

## D. 接进 loop：每轮选档、变化才发事件

`_react_cycle` 在每一轮模型调用前先选档。跨轮维护两个状态：

```python
had_failure = False      # 是否已发生工具失败（决定纠偏升档）
last_tier: str | None = None   # 上一轮档位（仅变化时发事件）
```

每轮：

```python
if tier_policy is not None:
    tier_dec = tier_policy.select(
        strategy=strategy,
        iteration=iteration,
        had_failure=had_failure,
    )
    tier = tier_dec.tier
    tier_reason = tier_dec.reason
else:
    tier = STANDARD
    tier_reason = "成本感知关闭，执行轮用标准档"
if tier != last_tier:
    yield AgentEvent(
        EventType.TIER_CHANGED,
        {"tier": tier, "reason": tier_reason, "iteration": iteration},
    )
last_tier = tier
if tier_counts is not None:
    tier_counts[tier] = tier_counts.get(tier, 0) + 1
resp = await self.model.achat(
    ctx.llm_messages(), tools=tools_schema, tier=tier
)
```

逐块：

- **成本感知开启**（`tier_policy` 非空）：用 `select` 决定本轮档位和理由；
  **关闭**时恒走 standard，行为与旧版一致。
- **`tier != last_tier` 才发 `tier_changed` 事件**：档位没变就不重复发，避免
  事件流被相同档位刷屏。首轮 `last_tier=None`，必然发一次起始档位（standard），
  相当于声明"从哪个档开始"，可审计。
- **计数**：无论开关，只要传了 `tier_counts` 就把这一轮记到对应档位上，供
  最后汇总。
- 档位通过关键字参数 `tier=tier` 传给 `achat`，由模型侧映射到具体模型。

工具失败时，loop 在本轮末尾置标记（见第 6 篇 failure reflection）：

```python
if failed:
    had_failure = True
    self._append_failure_reflection(ctx, failed)
```

于是**下一轮** `select` 看到 `had_failure=True`，升 strong 纠偏——失败信息
（reflection）和更强的模型在同一轮到位。

## E. DONE 成本汇总：花了多少、调了几次

astream 开始时建一个计数器：

```python
tier_counts = {STANDARD: 0, STRONG: 0}
```

规划轮、每个执行轮、重规划轮都会往里累加对应档位。DONE 事件把汇总带出去：

```python
yield AgentEvent(
    EventType.DONE,
    {
        "final_text": final_text,
        "standard_calls": tier_counts.get(STANDARD, 0),
        "strong_calls": tier_counts.get(STRONG, 0),
        "model_calls": sum(tier_counts.values()),
    },
)
```

- `standard_calls` / `strong_calls`：两档各调了几次，直接对应"便宜调用花了多少、
  贵调用花了多少"；
- `model_calls`：模型调用总数（两档之和）。

> 口径提醒：`model_calls` 统计的是**所有真正发起的模型调用**，包括初次规划、
> 各执行轮、重规划，以及工具超预算时的"聚焦选择"调用；但**不包括**路由分类
> 调用（那属于 router 的增强路径）和上下文压缩的内部调用。读测试数时按这个
> 口径对齐。

以第 31 篇的重规划用例为例：初次规划 strong(1) + 第 0 轮 standard(1) +
重规划 strong(1) + 第 1 轮 standard(1) + 第 2 轮 standard(1)
→ `strong_calls=2、standard_calls=3、model_calls=5`。

## F. 开关与回归：`cost_aware`

`AgentCore` 构造参数 `cost_aware=True`（默认开）控制是否接入策略：

```python
# core.py 构造末尾
self.cost_aware = cost_aware
self._loop = AgentLoop(
    ...,
    tier_policy=TierPolicy() if cost_aware else None,
)
```

- 开：loop 拿到 `TierPolicy`，按上面的规则动态选档；
- 关：传 `None`，执行轮恒 standard、不做动态升级，完全保持旧行为。

这是个纯粹的增量能力：默认规则在简单任务上本就给 standard，因此不开/无感时
不会增加成本；宿主也可以通过自定义 `TierPolicy`（改默认档、关某条规则）精确
控制开销。对"平台不充值、怕被刷量花钱"的场景，配合免费档模型 + 限流
（第 14 篇）即可把成本牢牢压住。

## G. 测试

- `tests/test_tiering.py`（8 个，纯策略、零依赖）：
  - 默认规划 strong；前 3 轮 standard；第 3 轮 strong；失败 strong；
  - 关 `strong_for_planning` → 规划 standard；
  - 同时关失败升档与迭代升档 → 失败也 standard；
  - `escalate_after_iter=-1` → 第 9 轮仍 standard；
  - 自定义 `default_tier=strong` 生效。
- `tests/test_replan.py`（3 个，端到端）：重规划闭环里核对档位序列与 DONE
  计数；`cost_aware=False` 回归全程 standard。

全部离线、不需要 API Key，全量 pytest、ruff、mypy 均绿。

## H. 自检（读完应能回答）

1. 所有调用都走强档 / 都走便宜档，各有什么问题？"该花才花"具体指什么？
2. 为什么说选档不花钱？档位标记最终由谁映射到具体模型？
3. `TierPolicy.select` 的三条规则优先级如何安排？为什么失败纠偏排在最前？
4. `escalate_after_iter=3` 时，哪些轮次走 standard、哪些升 strong？设为 -1 呢？
5. `tier_changed` 事件为什么只在档位变化时发？首轮为什么仍会发一次？
6. DONE 的 `model_calls` 统计口径包括哪些调用、不包括哪些？
7. `cost_aware=False` 时 loop 行为如何？为什么说这是个安全的增量能力？
