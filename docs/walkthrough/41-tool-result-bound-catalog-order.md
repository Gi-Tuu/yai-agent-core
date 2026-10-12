# 逐行讲解 41 · 工具结果统一有界收口 + 工具目录确定性排序

> 对应文件：
> - `src/yai_core/tools/executor.py`（新增纯函数 `bound_tool_result`、构造参数
>   `result_char_limit`，原生 / 代码 / 组合三类工具结果在回灌上下文前**统一收口**；
>   `TOOL_RESULT` 成功事件新增 `truncated` / `result_chars` 两个字段）；
> - `src/yai_core/tools/registry.py`（新增内部 `_ordered()`，**喂给模型的枚举**
>   catalog / catalog_text / describe / llm_schemas 一律按工具名确定性排序；内部
>   存储顺序 `all()` 与按需 `schemas_for()` 不变）；
> - `src/yai_core/config.py` + `src/yai_core/core.py`（`tool_result_char_limit`
>   经 `CoreConfig` 早期校验后透传给执行器）；
> - 离线测试：新增 `tests/test_tool_result_bound.py`，扩 `tests/test_registry.py`
>   （排序）、`tests/test_core_config.py`（非法值）。全部零网络、零 API Key。
>
> 读前先读第 04 篇（注册表 + 执行总线）、第 25 篇（上下文摘要压缩）、
> 第 27 篇（HTTP 响应守卫 payload_guard）。
> 本篇不改任何 SPI、不加依赖、不改路由主循环，只做两处"上下文轻量化"加固。

---

## A. 先看结论：两处"喂给模型的字节"为什么要治

内核里影响**每次模型调用输入**的东西有两类：工具返回的**结果**、系统提示里的
**工具清单**。这一篇各治一个小毛病，都遵循"默认行为不变、显式标记、零依赖"。

| 编号 | 改动前的问题 | 后果 | 本篇做法 | 默认行为 |
|---|---|---|---|---|
| C4 | 原生 / 代码 / 组合工具的结果 `json.dumps` 后**全文**塞进对话；只有 OpenAPI（16000）、沙箱输出、HTTP 响应守卫各自有界，标准不统一 | 一个工具吐出几万字，会撑大上下文；最后靠 `Context` 的 24000 字符**事后整轮硬裁剪**兜底——粒度粗、可能误伤其它轮、模型也不知道某条工具结果被切了 | 在三类结果回灌的**唯一汇聚点** `ToolExecutor` 统一做"头 + 尾"有界收口并打标记 | 单结果上限 **16000** 字符（与 OpenAPI 对齐）；小结果逐字不变；`None` 可关闭 |
| C1 | 系统提示里的工具目录 / function-calling schema 按宿主**注册顺序**枚举 | 宿主注册顺序的偶然波动（先注册谁、动态发现先补到谁）会改变系统提示字节，无谓击穿提示缓存的稳定前缀；快照测试也不稳定 | 喂模型的枚举统一按工具名排序 | 内部存储 `all()` 仍是注册顺序，执行 / 注销逻辑零影响 |

关键分层要先分清，三处"截断"各管一层、互不替代：

```
单个工具结果（本篇 C4，executor，默认 16000，头+尾+标记）
        │  进入对话历史
        ▼
整轮上下文（第 25 篇 compaction，超 24000 字符把旧轮压成摘要）
        │  组装成 HTTP 响应
        ▼
对外响应体（第 27 篇 payload_guard，256KB，事件序列头+尾守卫）
```

C4 是**最靠近源头**的那一道：在大结果进入历史之前就收住，避免它一路放大到
整轮压缩才被发现。

---

## B. C4 逐行：`bound_tool_result` 纯函数

打开 `tools/executor.py`，类定义上方新增了模块常量和一个纯函数。

### B.1 默认上界

```python
DEFAULT_TOOL_RESULT_CHARS = 16_000
```

取 16000 是为了和 OpenAPI 集成里早已存在的 `max_result_chars=16000` 对齐
（`integrations/openapi/client.py`），让"外部 REST 工具"和"原生函数工具"口径一致。
它只是默认值，宿主可以在构造 `AgentCore` 时覆盖，也可以传 `None` 完全关闭。

### B.2 纯函数签名与快速路径

```python
def bound_tool_result(text: str, limit: int | None) -> tuple[str, bool, int]:
    original_chars = len(text)
    if limit is None or original_chars <= limit:
        return text, False, original_chars
```

返回三元组：`(可能被裁剪后的文本, 是否裁剪, 原始字符数)`。
两条快速路径——`limit=None`（关闭）或本来就没超限——**原样返回**，`truncated=False`。
这保证默认配置下小结果一个字符都不会变（向后兼容）。

### B.3 头 + 尾 + 显式省略标记

```python
    head_budget = int(limit * 0.7)
    tail_budget = limit - head_budget
    head = text[:head_budget]
    tail = text[original_chars - tail_budget:]
```

超限时不是简单"砍到 16000 字就停"（那样会丢掉结果结尾，而结尾常常是汇总行、
状态码、末尾记录），而是**留 70% 头、30% 尾**。中间用一句中文标记显式说明：

```python
    def _join(head_part: str) -> str:
        omitted = original_chars - len(head_part) - tail_budget
        marker = f"\n…[工具结果过长，已省略 {omitted} 字符，以下为结尾部分]…\n"
        return head_part + marker + tail
```

`omitted` 是真正被丢掉的中间字符数（原始长度 − 头 − 尾），模型读到后能明确知道
"这里被省略了多少、自己看到的是结尾"，而不是误以为工具只返回了这么点数据。
这和第 27 篇 payload_guard 的理念一致：**有界可以，但必须显式标记，绝不静默删改**。

### B.4 严格不越上界的兜底循环

```python
    bounded = _join(head)
    while len(bounded) > limit and head_budget > 0:
        head_budget -= 1
        head = text[:head_budget]
        bounded = _join(head)
    return bounded, True, original_chars
```

标记串的长度会随 `omitted` 的位数变化（省略 9 字符和省略 99999 字符，标记本身
长度不同）。所以拼完后若仍超 `limit`，就从头部分配里**逐字符回退**，直到总长
严格 ≤ 上界。循环最多迭代标记串的位数差（几次），不是性能负担。

### B.5 执行器构造参数与校验

```python
result_char_limit: int | None = DEFAULT_TOOL_RESULT_CHARS,
...
if result_char_limit is not None and (
    isinstance(result_char_limit, bool) or result_char_limit <= 0
):
    raise ValueError("result_char_limit 必须是正整数或 None")
...
self.result_char_limit = result_char_limit

def _bound(self, text: str) -> tuple[str, bool, int]:
    return bound_tool_result(text, self.result_char_limit)
```

注意 `isinstance(..., bool)` 这道判断：Python 里 `True` 是 `int` 的子类，
`True <= 0` 为 `False` 会漏过校验，但布尔值语义上根本不是"字符数"，所以显式拒绝
（和 `CoreConfig` 里 `_is_positive_int` 排除 bool 是同一条纪律）。非法值在**装配
阶段**就抛错，而不是等到第一次工具调用。

### B.6 三处结果在回灌前统一收口

执行器里工具分三类（第 04 篇），成功结果各有一段 `text = json.dumps(...)`，
现在三处都在返回前加同一行收口，并给成功事件补两个字段。以原生工具为例：

```python
text, truncated, result_chars = self._bound(text)
events.append(
    AgentEvent(
        EventType.TOOL_RESULT,
        {
            "tool": name,
            "ok": True,
            "preview": text[:200],          # 事件预览仍只取前 200 字
            "truncated": truncated,         # 新增：这条结果是否被收口
            "result_chars": result_chars,   # 新增：收口前的原始字符数
        },
    )
)
return events, True, text
```

代码工具（沙箱输出）、组合工具（`{"steps": [...]}`）两处是完全相同的模式。
要点：

1. **回灌给模型的 `text`** 是收口后的有界文本；
2. **事件 `preview`** 仍只暴露前 200 字给 UI / 日志（这是给人看的预览，不变）；
3. **`truncated` / `result_chars`** 让灵动岛面板、metrics、排障日志能区分
   "工具本来就返回这么点" 和 "工具返回了 4 万字、被收了口"；
4. **失败事件不加这两个字段**——错误信息通常很短，语义是"出错"而不是"结果"。

> 组合工具的一个小细节：它内部会对每步结果 `json.loads`。若某步子结果被收口、
> 不再是合法 JSON，`composer` 本来就有 `JSONDecodeError` 兜底（保留原始文本），
> 所以收口不会让组合工具崩，只是该步以"被标记的文本"形态进入最终汇总，最后
> 汇总结果再过一次同一个 `_bound`。

---

## C. C1 逐行：喂模型的枚举确定性排序

打开 `tools/registry.py`。内部存储 `self._tools` 是一个按注册顺序写入的 dict，
这一点**不动**。新增一个只用于"对外枚举"的私有方法：

```python
def _ordered(self) -> list[ToolSpec]:
    return sorted(self._tools.values(), key=lambda spec: spec.name)
```

然后把四个"喂给模型 / 拼系统提示"的枚举从 `self._tools.values()` 改成
`self._ordered()`：

| 方法 | 用途 | 改动 |
|---|---|---|
| `llm_schemas()` | function-calling 的完整 schema 列表 | 改为按 name 排序 |
| `catalog()` | 轻量目录（name + 一句话摘要） | 改为按 name 排序 |
| `describe()` | 完整工具清单文本 | 改为按 name 排序 |
| `catalog_text()` | 轻量目录文本（内部调 `catalog()`） | 自动有序 |

两个顺序**刻意保持不变**：

```python
def all(self) -> list[ToolSpec]:
    """内部存储视图：保持注册顺序。"""
    return list(self._tools.values())

def schemas_for(self, names: Iterable[str]) -> list[dict[str, Any]]:
    """按名称返回完整 schema（按调用方给定的 names 顺序）。"""
    return [self.get(name).llm_schema() for name in names]
```

- `all()` 服务于注册 / 注销 / 执行等内部逻辑，没有理由改变它的顺序；
- `schemas_for(names)` 是"模型点名要哪几个工具的完整参数"，顺序应跟随模型点的
  名字，而不是再排序（第 04 篇两层目录的第二层）。

为什么排序对提示缓存友好：系统提示里工具目录是一段**稳定前缀**，只要工具集没变，
它的字节就应当一模一样。按注册顺序枚举时，宿主哪怕只是调换两句注册代码的先后，
目录字节就变了，缓存前缀失效；按名字排序后，只有工具集**真正变化**（新增 / 删除
/ 改名）才会重建一次缓存，这正是我们想要的代价。动态发现新工具也纳入排序，
"新增一个工具导致一次缓存重建"是合理且不可避免的。

---

## D. 透传链路：`AgentCore` → `CoreConfig` → `ToolExecutor`

C4 的上界要能被宿主配置，链路穿过三层，逐行看 `core.py` / `config.py`。

1. **门面参数**（`AgentCore.__init__`），默认引用执行器常量，保证单一真相源：

   ```python
   from yai_core.tools.executor import DEFAULT_TOOL_RESULT_CHARS
   ...
   full_schema_budget: int = 24,
   tool_result_char_limit: int | None = DEFAULT_TOOL_RESULT_CHARS,
   ```

2. **装配早期校验**：构造开头的 `CoreConfig(...).validate()` 里传入该字段，
   `config.py` 在"循环 / 上下文控制"区新增字段与校验：

   ```python
   tool_result_char_limit: int | None = 16_000
   ...
   if self.tool_result_char_limit is not None and not _is_positive_int(
       self.tool_result_char_limit
   ):
       raise ValueError("tool_result_char_limit 必须是正整数或 None")
   ```

   `CoreConfig` 里默认值写字面量 `16_000` 并注释"与 executor 常量对齐"，是为了
   不引入 `config → tools` 的导入耦合（config 刻意保持叶子、不反向依赖）。

3. **传给执行器**：

   ```python
   self.executor = ToolExecutor(
       self.registry, self.policy, self.channel,
       sandbox=sandbox, code_manager=self.code_manager,
       result_char_limit=tool_result_char_limit,
   )
   ```

`AgentCore.from_config(model, cfg)` 不需要额外改动：`CoreConfig.as_kwargs()` 用
`dataclasses.asdict` 自动把新字段带进关键字参数，只要字段名和 `__init__` 参数名
一致（都叫 `tool_result_char_limit`）即可。测试 `test_from_config_*` 与新增的
默认值断言共同守住这条链路。

---

## E. 测试怎么证明它是对的

`tests/test_tool_result_bound.py` 全部离线、零 API Key：

- **纯函数**：未超限原样、`None` 关闭、恰好等于上界不裁、超限后头尾都在且
  `len(out) <= limit`；并用多组长度（121 / 257 / 999 / 5000）循环断言严格不越界；
- **执行器原生大结果**：工具返回 4 万字，给执行器设 1000 的上界，断言
  `ok=True`、`len(text) <= 1000`、含省略标记、JSON 首尾引号都在、事件
  `truncated=True` 且 `result_chars` 等于裁剪前长度、`preview` 不超过 200；
- **`None` 关闭**：同样 4 万字，断言全文保留、`truncated=False`；
- **默认阈值小结果**：`{"ok": True}` 逐字等于 `json.dumps` 的结果；
- **非法上界**：`0` / `-1` / `True` 构造执行器都抛 `ValueError`。

`tests/test_registry.py` 新增一条：用乱序（`zeta / alpha / mid / Beta`）注册，
断言 catalog、llm_schemas、describe、catalog_text 全部按名字排序，**同时**
`all()` 仍是注册顺序——一条测试同时钉住"喂模型的有序"和"内部存储的不动"。
`tests/test_core_config.py` 补了默认值 16000、非法值（0 / -1 / True）被拒、
`None` 合法三组。

---

## F. 自检（读完本篇应能回答）

1. 为什么工具结果收口要放在 `ToolExecutor`，而不是继续依赖 Context 的 24000
   字符整轮裁剪？
2. `bound_tool_result` 返回的三元组分别是什么？为什么要把原始字符数
   `result_chars` 也放进事件？
3. 超限时为什么保留"头 + 尾"而不是只留前 16000 字？省略标记里为什么要写明
   省略了多少字符？
4. C1 为什么只改 catalog / describe / llm_schemas，而不改 `all()` 和
   `schemas_for()`？这对提示缓存有什么好处？
5. 宿主想完全关闭单结果收口、或把上界改成 4000，分别怎么配？传 `True`
   会发生什么、为什么？
6. C4（单结果）、compaction（整轮）、payload_guard（HTTP 响应）三道上界
   各管哪一层？为什么三者不能互相替代？
