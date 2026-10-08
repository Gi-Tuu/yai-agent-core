# 逐行讲解 25 · 上下文摘要压缩：长对话不丢事实

> 对应源码：`kernel/compaction.py`（新）、`kernel/context.py`（改造）、`kernel/loop.py`（接线）。
> 读完你会明白：为什么"超预算就删旧消息"不够，以及怎样把旧历史**压成摘要**、
> 同时保证发给模型的消息结构永远合法。

## 0. 一句话

**上下文快爆时，把较早的完整轮交给模型压成一条摘要，最近几轮原样保留；模型压不动就退回硬删除——无论哪条路，消息结构都不会被切坏。**

## 1. 为什么硬裁剪不够

v0.3 之前（以及现在的兜底）`Context.enforce_budget()` 的做法是：超 `max_chars` 就把最旧的整轮**直接删掉**。它解决了"切坏工具配对导致 400"的问题，但有个信息层面的硬伤：

- 长任务里，早期的**用户目标、关键约束、前面查到的数据**会随着删除彻底消失；
- 模型可能因此忘记任务初衷、重复询问、或用到已经被否定的方案。

灵动岛是长期运行的产品，对话只会越来越长，"删了就忘"是真实痛点。我们要的是：**旧信息可以变"短"，但不该变"无"。**

## 2. 分层窗口：三段式

压缩后，上下文被分成三段：

```
┌──────────────────────────────────────────────┐
│ system（原始系统提示）                         │ 永留，index 0
├──────────────────────────────────────────────┤
│ 【早期对话摘要】…（一条 system 摘要）          │ 旧轮被压成这一条
├──────────────────────────────────────────────┤
│ 最近 N 个完整轮（user/assistant/tool 全保留）  │ keep_recent_rounds，默认 2
└──────────────────────────────────────────────┘
```

- **最近的轮完整保留**：模型正在用的工具配对、刚拿到的数据不能动；
- **较旧的轮压成摘要**：用一次模型调用，把"目标 / 约束 / 事实 / 工具关键结果 / 未决问题"提炼成一段话；
- **system 永留**：原始的身份与工具目录提示始终在最前。

## 3. 逐块读 `compaction.py`

### 3.1 策略：`CompactionPolicy`

```python
@dataclass(frozen=True)
class CompactionPolicy:
    hard_max_chars: int = 24000   # 高水位：超过它才触发压缩
    target_chars: int = 16000     # 低水位口径（recent 必须完整时可能达不到）
    keep_recent_rounds: int = 2   # 最近几个完整轮不进摘要
```

- `frozen=True`：策略对象不可变，只读、安全，可放心共享；
- `hard_max_chars` 和 `Context.max_chars` 同口径（都是字符数），一个负责"何时压"、一个负责"硬删的底线"。

### 3.2 找轮首：`user_round_starts`

```python
def user_round_starts(messages):
    return [i for i in range(1, len(messages)) if messages[i].role == "user"]
```

一轮对话从 `user` 消息开始。这个函数列出所有轮首的下标，`range(1, …)` 跳过 index 0 的 system。它是"边界对齐"的基础——压缩的起点和终点都必须落在轮首。

### 3.3 确定压哪段：`select_compaction_range`

```python
total = sum(len(m.content) for m in messages)
if total <= policy.hard_max_chars:
    return None                          # ① 没超水位：不压
starts = user_round_starts(messages)
if len(starts) <= policy.keep_recent_rounds:
    return None                          # ② 旧轮不足：全在 recent 窗口，不压
start = starts[0]
keep_from = starts[-policy.keep_recent_rounds]
if keep_from <= start:
    return None
return (start, keep_from)                # ③ 压 [第一轮首, 倒数第N轮首)
```

三种返回 `None` 的情况，核心思想是一句话：**宁可暂时超预算，也绝不从一轮中间下刀。** 真正返回区间时：

- `start = starts[0]`：从第一个完整轮开始压；
- `keep_from = starts[-N]`：倒数第 N 个轮首，它和之后的 recent 轮完整保留；
- 区间两端都是 `user`，因此区间里的 `assistant(tool_calls)` 与其 `tool` 结果要么整段进摘要、要么整段保留，**不会出现"有 tool_calls 却没有 tool"的孤儿消息**。

### 3.4 旧轮转文本：`render_for_summary`

摘要模型不能直接吃 `tool_calls` 结构，所以把每条消息渲染成带角色标签的文字：

- `user` → `用户：…`
- `assistant` → `助手：…`，若有工具调用再附一行 `[调用工具：名称]`
- `tool` → `工具结果：…`（**关键事实往往在这里，必须带进摘要**）

### 3.5 执行压缩：`compact_context`

```python
rng = select_compaction_range(ctx.messages, policy)
if rng is None:
    return None
start, end = rng
history_text = render_for_summary(ctx.messages, start, end)
resp = await model.achat([{system: 摘要指令}, {user: history_text}],
                         tools=None, tier="standard")
summary = resp.content.strip()
summary_msg = ChatMessage(role="system",
                          content=f"【早期对话摘要】\n{summary}")
ctx.replace_range(start, end, summary_msg)
```

几个关键决定：

1. **摘要调用 `tools=None, tier="standard"`**：摘要不需要再调工具，用标准档即可（省钱）；
2. **摘要消息用 `role="system"`**：它是"背景信息"而非新的用户指令，多个 system 消息 OpenAI 兼容接口都支持；
3. **替换交给 `Context.replace_range`**：压缩逻辑只决定"压哪段、摘要是什么"，真正改消息列表由 Context 负责，职责清晰；
4. **返回一个 `CONTEXT_COMPACTED` 事件**：带着压缩前后的消息数、字符数、摘要长度，符合项目铁律——**每个自适应决策都可观测**。

### 3.6 失败回退：压不动就硬删

```python
except Exception:
    ctx.enforce_budget()
    return None
if not summary:
    ctx.enforce_budget()
    return None
```

两种失败——模型抛错、或返回空摘要——都走同一条退路：调用硬裁剪 `enforce_budget()`，然后返回 `None`。摘像是**增强**而不是依赖：任何环节出问题，系统退回到你已经验证过的旧行为，绝不崩溃、也绝不发出切坏结构的消息。

## 4. Context 与 Loop 的配套改动

**`context.py`** 新增三个方法：

- `total_chars()`：统一的字符统计口径；
- `replace_range(start, end, summary)`：`[system…start) + 摘要 + [end…]` 三段拼接，并校验 `start >= 1`（system 永留）、`end > start`；
- `enforce_budget()`：把原来 `_compact` 的硬删除逻辑改成公共方法，`add()` 仍自动调用它，摘要失败时也复用它。

**`loop.py`** 在 ReAct 循环里、**每轮模型调用之前**插一个压缩检查：

```python
for _ in range(self.max_iters):
    if self.compaction_policy is not None:
        compact_ev = await compact_context(ctx, self.model, self.compaction_policy)
        if compact_ev is not None:
            yield compact_ev
    resp = await self.model.achat(ctx.llm_messages(), tools=tools_schema)
```

放在每轮开头有两个覆盖作用：第一轮处理"从记忆载入的长历史 + 当前任务"，后续轮处理"工具结果不断累积"。`compaction_policy is None`（默认）时这段完全不生效，老用户行为零变化。

**`core.py`** 用 `context_compaction` 参数控制，且特意做成三种取值：

- `False`（默认）：关闭；
- `True`：用默认 `CompactionPolicy`；
- 传一个 `CompactionPolicy` 实例：用自定义水位（测试和需要精细调参的宿主用）。

灵动岛真机工厂（`shell/desktop/host.py`）显式开启，离线 ScriptedModel 演示不开（没有意义）。

## 5. 自检（答得上再往下走）

1. 摘要压缩和硬裁剪，分别在什么情况下触发？为什么两者都要保留？
2. 为什么压缩区间的两端必须对齐 `user` 轮首？如果从 `assistant(tool_calls)` 和 `tool` 中间切，会发生什么？
3. 摘要消息为什么用 `role="system"` 而不是 `role="user"`？
4. 模型摘要失败时，系统走到哪条退路？返回什么？
5. `context_compaction=True` 和传一个 `CompactionPolicy` 对象，效果有何区别？
