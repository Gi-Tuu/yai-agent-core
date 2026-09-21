# 逐行讲解 16 · 组合工具：不越界地"造工具"

> 对应源码：`src/yai_core/tools/composer.py`（新增）、`src/yai_core/types.py` 里的
> `CompositeStep` 与 `ToolSpec.steps`、`src/yai_core/tools/executor.py` 的
> `_execute_composite`，以及 `core.py` 里的 `composition=True` 开关。
> 前置：第 04 篇（注册表 + 执行总线）、第 15 篇（能力发现）。

## 0. 这一篇解决什么问题

模型有时会反复调用同一串固定流程，比如销售场景里：

```
查客户 → 写跟进 → 建待办
```

每次都让模型自己一步步想、一步步调，既费 token 又不稳定。我们希望模型能把这串流程
**固化成一个新工具**（比如 `followup_and_todo`），以后一句话就能整条跑。

但"让模型造工具"听上去很危险——它会不会造出一个能删库、能联网的工具？这一篇的核心
就是一条**安全设计**：

> 组合工具只能把**宿主已经注册、已经授权**的工具串起来，
> 它的能力上限 = 被组合工具的并集，**物理上不可能越界**，也不执行任何模型写的代码。

至于"执行模型当场生成的代码"那种更高风险的能力，是第 17 篇的沙箱契约，本版**只定义
接口、不实现**。

能力分两级，先记住这张表：

| 级别 | 做法 | 风险 | 本版状态 |
|---|---|---|---|
| 组合工具 composite | 编排已有工具，不写代码 | 低（能力不越界，每步仍授权） | **已实现** |
| 代码工具 code tool | 执行模型生成的代码 | 高（可能产生宿主没有的行为） | 只定义 `ToolSandbox` 契约 |

## 1. 数据结构：一个组合工具长什么样

### 1.1 `CompositeStep`（types.py）

```python
@dataclass(frozen=True)
class CompositeStep:
    tool: str                       # 这一步要调用的已注册工具名
    args: dict[str, Any] = field(default_factory=dict)  # 传给它的参数（可含占位符）
```

一个组合工具就是一串 `CompositeStep`。`frozen=True` 表示步骤一旦创建就不能原地改，
和全项目的不可变数据风格一致。

### 1.2 `ToolSpec` 增加的三个字段

```python
handler: Callable[..., Any] | None = None   # 普通工具有处理函数；组合工具为 None
source: Literal["native", "openapi", "mcp", "composite"] = "native"
steps: list[CompositeStep] | None = None     # 只有 source == "composite" 时非空
```

注意三点：

1. 组合工具的 `handler` 是 `None`——它没有自己的处理函数，执行靠**逐步调用别的工具**。
2. `source` 多了一种 `"composite"`，执行器据此分流。
3. 普通工具这两个字段保持默认值（`handler` 有函数、`steps` 为 `None`），完全向后兼容。

## 2. 占位符：让步骤之间能"传话"

组合工具的第二步往往要用第一步的结果。我们用一套简单的模板占位符：

- `{{$input.x}}`：调用组合工具时传入的入参字段 `x`；
- `{{$steps.0}}`：第 0 步的完整结果；
- `{{$steps.0.field}}`：第 0 步结果（字典）里的 `field`，支持点路径和列表下标。

### 2.1 两条正则（composer.py 顶部）

```python
_PLACEHOLDER = re.compile(r"\{\{\s*\$((?:input|steps)(?:\.[\w一-鿿-]+)*)\s*\}\}")
_FULL_PLACEHOLDER = re.compile(r"^\{\{\s*\$((?:input|steps)(?:\.[\w一-鿿-]+)*)\s*\}\}$")
```

- `\{\{`、`\}\}` 匹配字面量双花括号（`{` 在正则里是特殊字符，要转义）。
- `\s*` 允许里面有空白，`{{ $input.x }}` 也能认。
- `((?:input|steps)...)` 是捕获组：开头必须是 `input` 或 `steps`，后面跟若干
  `.字段`，字段允许中文（`一-鿿` 是中文 Unicode 区间）。
- 两条正则的区别：`_FULL_PLACEHOLDER` 带 `^...$`，只匹配**整串就是一个占位符**的情况。

为什么要分两条？看 2.3。

### 2.2 `_lookup`：按点路径取值

```python
def _lookup(path: str, context: dict[str, Any]) -> Any:
    parts = path.split(".")
    cur: Any = context.get(parts[0])
    for key in parts[1:]:
        if cur is None:
            return None
        if isinstance(cur, dict):
            cur = cur.get(key)
        elif isinstance(cur, list) and key.isdigit():
            idx = int(key)
            cur = cur[idx] if 0 <= idx < len(cur) else None
        else:
            return None
    return cur
```

逐行看：

- `path` 形如 `"input.n"` 或 `"steps.0.total"`，按点切成列表。
- 第一段（`input`/`steps`）从 `context` 顶层取。
- 之后每一段：字典就按键取；列表且当前段是数字就按下标取（还做了越界保护）；
  类型对不上就返回 `None`。
- 任何一级是 `None` 直接返回 `None`，不会抛异常——这让"取一个还不存在的字段"变成
  温和的空值，而不是崩溃。

### 2.3 `resolve_args`：递归解析参数

```python
def resolve_args(value: Any, context: dict[str, Any]) -> Any:
    if isinstance(value, str):
        full = _FULL_PLACEHOLDER.match(value.strip())
        if full:
            return _lookup(full.group(1), context)
        return _PLACEHOLDER.sub(
            lambda m: "" if _lookup(m.group(1), context) is None
            else str(_lookup(m.group(1), context)),
            value,
        )
    if isinstance(value, dict):
        return {k: resolve_args(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_args(v, context) for v in value]
    return value
```

三种情况：

1. **字符串**：
   - 如果整串就是一个占位符（`_FULL_PLACEHOLDER` 命中），**原样返回取到的值**，
     不强制转成字符串。这样 `{{$input.n}}` 传入数字 `5`，解析后仍是整数 `5`，
     不会变成 `"5"`——这对需要数值参数的工具很关键。
   - 否则是"嵌在一句话里"的占位符，用 `sub` 做字符串替换；取不到就替成空串。
2. **字典 / 列表**：递归处理每个元素，所以嵌套结构里的占位符也能解析。
3. **其他类型**（数字、布尔）：原样返回。

## 3. 构造组合工具规格

```python
def build_composite_spec(name, description, steps, *, input_schema=None) -> ToolSpec:
    parsed: list[CompositeStep] = []
    for step in steps:
        if isinstance(step, CompositeStep):
            parsed.append(step)
        else:
            tool = str(step.get("tool", "")).strip()
            if not tool:
                raise ValueError("组合工具的每个步骤都必须包含 tool 字段")
            parsed.append(CompositeStep(tool=tool, args=dict(step.get("args", {}))))
    return ToolSpec(
        name=name, description=description,
        input_schema=input_schema or {"type": "object", "properties": {}},
        handler=None, source="composite", steps=parsed,
    )
```

这个函数只负责"把声明式的步骤字典列表，整理成一个 `ToolSpec`"：

- 步骤既可以传现成的 `CompositeStep`，也可以传普通 dict（模型从 JSON 里给的就是 dict）。
- 每步必须有非空 `tool`，否则抛 `ValueError`。
- 关键：`handler=None`、`source="composite"`、`steps=parsed`。

注意它**不校验工具是否已注册**——那是下一节 `build_composer_tool` 的事，把"构造数据"
和"安全校验"分开，各自好测。

## 4. meta-tool：`compose_tool`（模型造工具的唯一入口）

模型不能直接改注册表，它只能调用一个叫 `compose_tool` 的内置工具。这个工具由
`build_composer_tool(registry)` 构造。

### 4.1 三道安全校验

```python
if not name or not description:
    raise ValueError("组合工具必须提供非空的 name 和 description")
if not isinstance(steps, list) or not steps:
    raise ValueError("组合工具必须包含至少一个步骤")
if registry.has(name):
    raise ValueError(f"工具名 {name!r} 已存在，不能重复创建")
for raw in steps:
    tool = str(raw.get("tool", "")).strip()
    if not registry.has(tool):
        raise ValueError(f"步骤引用了未注册工具 {tool!r}，组合工具不能创造宿主没有的能力")
    if registry.get(tool).source == "composite":
        raise ValueError("暂不支持嵌套组合工具（组合工具不能引用另一个组合工具）")
```

逐条理解这三条红线：

1. **只能引用已注册工具**（`registry.has(tool)`）：这是"不越界"的核心。模型想引用一个
   根本不存在的 `rm_rf`、`http_post`，直接拒绝。它能编排的能力，宿主本来就有。
2. **不能嵌套组合**：组合工具引用另一个组合工具会让权限链路变成递归黑盒，也可能造成
   权限放大，本版直接禁止。
3. **不能重名**：避免覆盖已有工具（包括覆盖原生工具）。

校验通过后才 `registry.register(spec)`，并返回创建结果（名字、说明、步骤工具名）。

### 4.2 它本身也是个工具

`compose_tool` 自己是一个 `source="native"` 的普通 `ToolSpec`，有完整的 JSON Schema
（`name` / `description` / `steps` 必填）。这意味着：

- 它走的是和别的工具**完全一样**的执行与权限链路；
- 它会"改变注册表"，本质是写操作，所以在"部分审批"档下，模型调用它时**也会弹授权**；
- 默认不启用，只有 `AgentCore(composition=True)` 才注册它（见第 6 节）。

## 5. 执行：逐步递归，每步都走权限

执行器 `ToolExecutor.execute` 一进来就分流：

```python
spec = self.registry.get(name)
if spec.source == "composite" and spec.steps:
    return await self._execute_composite(spec, arguments)
```

`_execute_composite` 的骨架：

```python
events.append(AgentEvent(EventType.TOOL_COMPOSED,
                         {"tool": spec.name, "steps": step_names, "arguments": arguments}))
step_results: list[Any] = []
context = {"input": arguments, "steps": step_results}
for index, step in enumerate(spec.steps):
    resolved = resolve_args(step.args, context)          # ① 解析这一步的参数
    sub_events, ok, sub_text = await self.execute(step.tool, resolved)  # ② 递归调用
    events.extend(sub_events)
    if not ok:
        return events, False, f"...第 {index+1} 步（{step.tool}）失败：{sub_text}"  # ③ 短路
    try:
        step_results.append(json.loads(sub_text))        # ④ 收集结果供后续步骤引用
    except (json.JSONDecodeError, TypeError):
        step_results.append(sub_text)
final = {"steps": step_results}
return events, True, json.dumps(final, ensure_ascii=False)
```

几个关键设计：

1. **先发 `TOOL_COMPOSED` 事件**：UI 能看到"现在在跑一个组合工具，共几步、每步是谁"，
   决策不静默（全项目红线）。
2. **第②步是递归调用 `self.execute(...)`**，不是直接调 handler。这非常重要：
   每一步都会重新走一遍"权限检查 → （可能）弹确认 → 执行 → 回灌"。所以组合工具不需要
   自己的权限体系——它只是把多次普通调用串起来，**权限精确落在每一步的内部工具上**。
3. **组合工具顶层不做 `policy.check`**：否则用户会看到一个黑盒名字在申请权限，却不知道
   里面要干什么。真正要申请的是内部那几个具体工具，用户在逐步确认时看得明明白白。
4. **任一步失败立即短路**：发失败事件并返回，后面的步骤不再执行。
5. **结果收集**：工具结果文本是 JSON，这里尝试 `json.loads` 还原成对象，后续步骤才能用
   `{{$steps.0.field}}` 取到字段；还原不了就保留原文。

## 6. 开关：`AgentCore(composition=True)`

在内核门面 `core.py` 里，构造执行器之后，只有显式打开开关才注册 `compose_tool`：

```python
if composition:
    self.registry.register(build_composer_tool(self.registry))
```

默认 `False`，所以**不打开组合能力的宿主，行为和以前完全一致**，零负担。host_e 的
`agent_bridge.py` 里传了 `composition=True`，数字员工因此能在运行时编排 CRM 工具。

## 7. 完整走一遍（离线可复现）

测试 `tests/test_composite.py` 用 `ScriptedModel` 离线演示，逻辑等价于：

1. 注册两个普通工具：`add_one(n)`（返回 n+1）、`double(n)`（返回 2n）。
2. 模型调用 `compose_tool`，创建 `plus_one_then_double`，两步：
   - `{tool: "add_one", args: {n: "{{$input.n}}"}}`
   - `{tool: "double", args: {n: "{{$steps.0}}"}}`
3. 模型再调用 `plus_one_then_double(n=5)`。
4. 执行器：
   - 发 `tool_composed`；
   - 第 1 步：`{{$input.n}}` → `5`，`add_one(5)` → `6`；
   - 第 2 步：`{{$steps.0}}` → `6`，`double(6)` → `12`；
   - 返回 `{"steps": [6, 12]}`。

全程没有执行任何模型写的代码，用的两个工具都是宿主本来就有的；如果宿主没注册 `double`，
第 2 步在**创建组合工具时**就会被拒绝，根本轮不到执行。

## 8. 安全红线小结（面试/答辩可以直接讲）

- 组合工具**不产生新原始能力**，能力上限是被组合工具的并集；
- 引用未注册工具、嵌套组合、重名，在创建时一律拒绝；
- 每一步执行都递归走完整权限链路，组合工具顶层不做黑盒授权；
- 不执行模型生成的代码，零沙箱风险；
- 默认关闭（`composition=True` 才启用），是 SPI 之外的一个显式能力开关。

## 9. 自检

1. 为什么组合工具的 `handler` 是 `None`？它执行时实际调用的是什么？
2. `{{$input.n}}` 整串占位符和嵌在句子里的占位符，解析结果有什么区别？为什么？
3. 为什么说组合工具"物理上不可能越界"？是在哪一行代码挡住的？
4. 组合工具为什么要在顶层执行时**跳过**一次权限检查，却在每一步内部做权限检查？
5. 模型想创建一个"先查天气再发邮件"的组合工具，但宿主只注册了查天气、没注册发邮件，
   会发生什么？在哪一步、由谁拒绝？
6. 为什么本版禁止组合工具引用另一个组合工具？
