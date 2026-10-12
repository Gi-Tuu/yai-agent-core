# 第 42 篇 · 摘要专用模型可注入（C2）与按上下文窗口算水位（C3）

> 本篇在第 28 篇（上下文摘要压缩 compaction）基础上做两处小增强，主题是**省钱**与**好配**：
> 1. 让"摘要压缩"这一次额外模型调用可以走一个**更便宜的小模型**，主模型专心做任务；
> 2. 给 `CompactionPolicy` 一个按"上下文窗口字符预算"**按比例算高/低水位**的便捷工厂，
>    宿主不必再手填两个绝对数字。
>
> 两处都**默认零行为变化**：不传摘要模型时压缩照旧走主模型；不调用工厂时默认水位仍是
> 24000 / 16000。不改任何 SPI、不加依赖、不引入 tokenizer。

## A. 先看结论

| 改动 | 之前 | 现在 | 默认行为 |
| --- | --- | --- | --- |
| **C2 摘要模型** | 压缩固定用主模型 `self.model` | `AgentCore(compaction_model=小模型)`，压缩改走它 | `None` → 仍走主模型，完全等价 |
| **C3 水位工厂** | 只能手填 `hard_max_chars` / `target_chars` | `CompactionPolicy.from_context_window(chars, ...)` 按比例算 | 不调用即可，默认值不变 |

一句话：**C2 让"为记忆续命"的那次调用可以用最便宜的模型；C3 让水位配置从"拍脑袋填两个数字"
变成"给一个窗口大小，比例我来算"。**

## B. C2 逐行：把摘要调用从主模型拆出来

### B1 为什么要拆

第 28 篇讲过：对话超过高水位时，内核会把旧轮交给模型压成一段摘要。这是一次**额外的模型调用**，
而且任务很简单——"把这段话压缩、保留关键事实"，不需要主模型那种最强推理。

- 主模型（贵 / 慢）应该留给真正的任务决策与工具规划；
- 摘要这种"体力活"交给免费或廉价的小模型（例如 flash 档）足够；
- 这正契合"长期运行的数字员工"诉求：灵动岛可能几小时不关闭、反复触发压缩，用小模型能把
  这部分成本压到接近零。

预算账本（第 34 篇的 Budget）**本来就不把路由分类与上下文压缩计入**，所以换小模型不会
干扰任务预算熔断，语义自洽。

### B2 `kernel/loop.py`：新增一个可选参数

构造函数 `AgentLoop.__init__` 在 `compaction_policy` 后面多了一个参数：

```python
compaction_policy: CompactionPolicy | None = None,
compaction_model: ModelProvider | None = None,   # ← 新增：摘要专用模型
```

存成实例字段：

```python
self.compaction_policy = compaction_policy
# 摘要专用模型：None 时压缩走主模型；传入小模型则摘要改走它（省钱）。
self.compaction_model = compaction_model
```

真正的关键在压缩调用点（ReAct 每轮开头）。第 28 篇时这里写死传主模型：

```python
compact_ev = await compact_context(
    ctx,
    self.model,                 # ← 旧：永远是主模型
    self.compaction_policy,
    timeout=self.model_timeout,
    on_diagnostic=self._on_diagnostic,
)
```

现在改成一行"二选一"：

```python
compact_ev = await compact_context(
    ctx,
    self.compaction_model or self.model,   # ← 新：有专用模型用它，否则回退主模型
    self.compaction_policy,
    ...
)
```

`self.compaction_model or self.model` 是 Python 常见写法：`None` 为假，所以不传时取主模型；
传了对象（为真）就取专用模型。**这一行就是 C2 的全部核心逻辑**——`compact_context` 函数
本身一个字都不用改，因为它早就把"用哪个模型"做成了入参 `model`（见第 28 篇）。

### B3 诊断回调也要接上

主模型在构造时会被"鸭子接线"挂上诊断回调（第 32 篇 diagnostics），这样模型超时 / 失败能上报。
专用摘要模型同理，否则摘要小模型静默出错时旁路看不到：

```python
if self.compaction_model is not None and hasattr(
    self.compaction_model, "on_diagnostic"
):
    self.compaction_model.on_diagnostic = on_diagnostic
```

`hasattr` 判断保证：宿主传入一个没有 `on_diagnostic` 属性的自定义模型也不会报错。

### B4 `core.py`：对外暴露一个参数

`AgentCore.__init__` 同步增加参数，并透传给内部的 `AgentLoop`：

```python
context_compaction: bool | CompactionPolicy = False,
compaction_model: ModelProvider | None = None,   # ← 新增
```

```python
self._loop = AgentLoop(
    ...
    compaction_policy=comp_policy,
    compaction_model=compaction_model,           # ← 透传
    ...
)
```

**为什么 `compaction_model` 不放进 `CoreConfig`？** 第 35 篇讲过 `CoreConfig` 是"与模型无关、
可序列化、能写进配置文件"的数据类（开关、数字、路径）。而模型对象是一个带连接、API Key、
HTTP 客户端的活实例，主模型 `model` 本身也不进 `CoreConfig`（它是 `AgentCore(model, config)`
里单独传入的）。摘要模型和主模型地位相同，所以同样作为**对象参数**传入，不进配置数据类。

### B5 宿主怎么用

```python
from yai_core import AgentCore
from yai_core.llm.openai_compat import OpenAICompatProvider

main = OpenAICompatProvider(model="你的主力模型", api_key=..., base_url=...)
cheap = OpenAICompatProvider(model="便宜的flash档", api_key=..., base_url=...)

core = AgentCore(
    main,
    context_compaction=True,     # 开启摘要压缩
    compaction_model=cheap,      # 摘要改走便宜模型；不传则走 main
)
```

## C. C3 逐行：`from_context_window` 按比例算水位

### C1 解决什么痛点

`CompactionPolicy` 有两个绝对数字：`hard_max_chars`（触发压缩的高水位）、`target_chars`
（压缩后想回到的低水位）。宿主面对的问题通常是："我这个模型上下文窗口挺大 / 挺小，这两个数
到底填多少？" 让每个宿主都拍脑袋填绝对值既不统一也容易错。

工厂方法让宿主只给一个**本次会话的字符工作区预算**，比例由内核给默认：

```python
@classmethod
def from_context_window(
    cls,
    chars: int,
    *,
    keep_recent_rounds: int = 2,
    hard_ratio: float = 0.6,
    target_ratio: float = 0.4,
) -> CompactionPolicy:
```

- `hard_ratio=0.6`：上下文用到工作区的 60% 就触发压缩——给系统提示、工具 schema、模型输出
  预留 40% 余量，避免触顶；
- `target_ratio=0.4`：压缩后回到 40%，留出再次增长的空间；
- 高水位必须严格大于低水位（`0 < target_ratio < hard_ratio <= 1`）。

### C2 一个漂亮的换算锚点

默认策略是 24000 / 16000，比例正好是 0.6 / 0.4，对应一个 40000 字符的工作区：

```python
CompactionPolicy.from_context_window(40_000) == CompactionPolicy()
# True —— hard=24000、target=16000、keep_recent=2，与无参默认逐项相等
```

这个等式既是用法示例，也是测试锚点：它证明工厂的默认比例和内核长期使用的默认水位完全一致，
老宿主换写法不会有任何行为漂移。

### C3 为什么坚持"字符口径"，不引入 tokenizer

不同模型 token 算法不同，引入 tokenizer 会给零硬依赖内核背上第三方包。YAI 全程用**字符数**
作为上下文口径（第 28 篇、`Context.max_chars` 都是字符），字符对中英文都是稳定的保守上界。
工厂也只做 `int(chars * ratio)` 的纯算术，不猜测 token。宿主若确实想按 token 换算，可在
**宿主侧**自行乘一个经验系数后把字符数传进来，内核不替你做这个模型相关的假设。

### C4 校验：宁早勿错

工厂在装配期拒绝所有会产出荒谬水位的入参：

- `chars` 非正整数、bool 冒充 → `ValueError`；
- 比例不满足 `0 < target < hard <= 1`（倒挂 / 相等 / 超过 1 / 低水位为 0）→ `ValueError`；
- `keep_recent_rounds < 1` 或 bool → `ValueError`；
- 窗口小到按比例取整后高/低水位为 0 或高 ≤ 低（例如 `chars=1`）→ `ValueError`。

## D. 测试怎么证明（全部离线，不需要 API Key）

测试文件仍是 `tests/test_context_compaction.py`，用第 28 篇同一个 `ScriptedModel`。

**C2——摘要确实走专用模型、主模型没被借用：**

```python
summary_model = ScriptedModel([ModelResponse(content="专用模型摘要QWE")])
main_model    = ScriptedModel([ModelResponse(content="主模型最终答复")])
core = AgentCore(main_model, memory=memory,
                 context_compaction=policy, compaction_model=summary_model)
result = await core.run("你好")

assert summary_model.calls == 1   # 压缩恰好调用一次专用模型
assert main_model.calls == 1      # 主模型只被调一次做最终答复
assert result.final_text == "主模型最终答复"
```

如果实现错误地仍让主模型做摘要，主模型就会被调两次（第一次响应被当成摘要、第二次才是答复），
`main_model.calls == 1` 立刻失败。默认回归由第 28 篇已有测试覆盖（不传 `compaction_model` 时
压缩与答复都走同一个模型）。

**C3——工厂等价锚点、自定义比例、非法入参：**

```python
assert CompactionPolicy.from_context_window(40_000) == CompactionPolicy()
p = CompactionPolicy.from_context_window(100_000, hard_ratio=0.5, target_ratio=0.25)
assert (p.hard_max_chars, p.target_chars) == (50_000, 25_000)
# chars=0/负数/bool、比例倒挂/相等/>1、keep=0、窗口过小 → 全部 pytest.raises(ValueError)
```

## E. 明确不做（边界）

- **不改 `ModelProvider.achat` 的 SPI 去加 `temperature`**：摘要想要"低温稳定"，但给 SPI 加
  参数会波及所有模型后端与全部调用点；摘要质量由摘要系统提示保证，需要时宿主可在**自己的**
  模型后端里固定低温，内核不扩 SPI。
- **不引入 tokenizer、不按 token 计水位**：保持零硬依赖与字符口径一致。
- **专用摘要模型不计入任务预算**：延续既有设计（压缩 / 路由本就不计 Budget），避免"为了省钱
  反而更快触发预算熔断"。
- **`None` 即完全回归**：不传 `compaction_model` 时，调用路径、诊断、事件与旧版逐字节一致。

## F. 自检题

1. 不传 `compaction_model` 时，压缩用哪个模型？写出那行"二选一"表达式并解释短路逻辑。
2. 为什么 `compact_context` 函数本身不需要为 C2 改动？（提示：它的 `model` 是什么。）
3. 为什么要给 `compaction_model` 也挂 `on_diagnostic`？不挂会怎样？
4. `compaction_model` 为什么不放进 `CoreConfig`？还有哪个参数和它地位相同？
5. `CompactionPolicy.from_context_window(40_000)` 与 `CompactionPolicy()` 是什么关系？为什么
   选 0.6 / 0.4 作为默认比例？
6. 为什么工厂坚持字符口径而不是 token？`chars=1` 为什么会抛错？
