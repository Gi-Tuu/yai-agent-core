# 逐行讲解 15 · `spi/discovery.py` + `discovery/catalog.py`：能力缺口的最小闭环

> 第 05 篇里，LLM 路由能在工具不足时发出 `capability_missing` 事件——但那只是"喊一声我缺什么"。
> 本篇把它补成一条**真正的闭环**：缺口 → 发现候选 → 注册 → **本轮就能用**。
> 知识点：第五个 SPI（Protocol）、`@dataclass(frozen=True)`、关键词匹配、防御性去重、"发现不等于授权"、失败不致命。

## 0. 先看闭环长什么样

运行 `examples/host_f_discovery/run.py`（无需 API Key，结果完全确定）：

```
自动发现的工具： ['list_tasks', 'add_task']          # 启动时只有 2 个默认能力
[路由] 选定策略：react
[能力缺口] 缺少：天气查询能力                          # ① 模型说"我缺这个"
           现有工具：['list_tasks', 'add_task']
[按需发现] 新工具已注册：['get_weather']（来源 StaticCatalog）  # ② 内核从目录里"长"出工具
[工具] 调用 get_weather({'city': '湛江'})             # ③ 本轮立刻调用
[观察] 返回：{"city": "湛江", "weather": "晴", "temp_c": 27}
[模型] 湛江当前晴，27℃，下午出门不用带伞。
```

关键是：**`get_weather` 在任务开始时并不存在**，它是在模型报出缺口之后、本轮 ReAct 开始之前被发现并注册的。这就是 YAI"自适应"从"选策略"走到"补能力"的一步。

## 1. 第五个 SPI：只约定"怎么找"，不规定"从哪找"（`spi/discovery.py`）

YAI 的内核零第三方硬依赖，所有外部能力都走 SPI 契约。发现能力也不例外——先定义一个插槽：

```python
class ToolDiscovery(Protocol):
    async def discover(
        self,
        need: str,
        *,
        task: str = "",
        available: list[str] | None = None,
    ) -> list[ToolSpec]:
        ...
```

逐行：

- `class ToolDiscovery(Protocol)`：和 model/channel/memory/policy 一样，是一个**结构化协议**。任何类只要有一个签名匹配的异步 `discover` 方法，就算实现了它，不需要继承（鸭子类型的书面版，见第 02 篇）。
- `need: str`：**缺什么**，来自 LLM 路由输出的 `missing_capability`，一句自然语言，例如"天气查询能力"。
- `*`：后面的参数必须用关键字传，调用方写 `discover(need, task=..., available=...)`，避免位置传错。
- `task: str = ""`：**原始任务全文**。光有"天气查询能力"可能太短，任务原文"帮我查湛江天气"能帮发现源做更准的匹配。
- `available: list[str] | None`：**当前已注册的工具名**。发现源据此跳过已经存在的能力，避免重复注册。
- 返回 `list[ToolSpec]`：发现源只负责"交回候选规格"，**不负责注册**。注册、去重、权限都在内核手里（见第 3 节）。

为什么返回的是 `ToolSpec`（第 01 篇的工具身份证）而不是函数？因为发现源未来可能是 MCP 目录、OpenAPI 服务、插件市场——它们给的本来就是"规格"而非本地函数。统一成 `ToolSpec`，内核就不用关心工具是本地的还是远程的。

docstring 里列了三个未来的发现源：

| 发现源 | 形态 | 现状 |
|---|---|---|
| 进程内静态目录 | `StaticCatalog` | **本篇已落地**，离线可测 |
| MCP 目录 | 按语义找到公共 MCP Server，连上去 `list_tools` | 后续（MCP Client 已在第 10 篇） |
| OpenAPI 服务目录 | 拉 OpenAPI 描述自动注册 | 后续（OpenAPI 发现已在第 12 篇） |

最后那句注释是整条闭环的**安全红线**：

> 能被发现不等于被授权执行；注册后的工具仍需经过 PermissionPolicy。

## 2. 最小实现：进程内静态目录（`discovery/catalog.py`）

先定义"候选能力"：

```python
@dataclass(frozen=True)
class DiscoveredCandidate:
    fn: Any
    keywords: tuple[str, ...]
    name: str | None = None
    description: str | None = None
```

- `@dataclass(frozen=True)`：自动生成 `__init__`，且实例**不可变**（创建后不能改字段）。候选是配置数据，冻结它更安全、可哈希。
- `fn`：宿主的普通 Python 函数（和 `build_spec` 吃的是同一种东西）。
- `keywords`：**触发关键词元组**。这是有意设计的"显式授权"——函数不会因为名字像就被发现，必须宿主明确登记关键词。
- `name / description`：可选，覆盖内省出来的默认值。

再看目录本体：

```python
class StaticCatalog:
    def __init__(self, candidates: Iterable[DiscoveredCandidate]) -> None:
        self._candidates = list(candidates)
```

- 接收一组候选，存成列表。`Iterable` 意味着你可以传列表、元组、生成器，构造时统一 `list(...)` 固化。

核心匹配逻辑：

```python
async def discover(self, need, *, task="", available=None) -> list[ToolSpec]:
    available_set = set(available or [])
    found: list[ToolSpec] = []
    seen: set[str] = set()
    haystack = f"{need} {task}".lower()
```

- `available or []`：`available` 为 None 时用空列表，再转成集合，查重 O(1)。
- `found` 收集结果，`seen` 记录本轮已收录的名字。
- `haystack`：把"缺口描述 + 任务原文"拼起来转小写，作为关键词匹配的文本面。

```python
    for candidate in self._candidates:
        if not candidate.keywords:
            continue
        if not any(
            kw.lower() in haystack
            for kw in candidate.keywords
        ):
            continue
```

- **没有关键词的候选直接跳过**：这是防"无条件匹配"的闸。一个没登记关键词的函数永远不会被自动发现，宿主必须显式声明它能补什么缺口。
- `any(kw.lower() in haystack ...)`：任一关键词（大小写不敏感）作为子串出现就算命中。中文关键词直接子串匹配，英文关键词转小写后匹配，所以 `"weather"` 能命中 `"Weather lookup"`。

```python
        name = candidate.name or candidate.fn.__name__
        if name in seen or name in available_set:
            continue
        seen.add(name)
        available_set.add(name)
```

- 候选名：显式 `name` 优先，否则用函数名。
- 双重去重：`seen` 防**候选之间同名**（比如两个候选都叫 `get_weather`），`available_set` 防**和已注册工具撞名**。
- 收录后立刻加进 `available_set`，让后续候选也能感知到它。

```python
        found.append(
            build_spec(
                candidate.fn,
                name=candidate.name,
                description=candidate.description,
                source="native",
            )
        )
    return found
```

- 复用第 03 篇的 `build_spec` 把函数内省成 `ToolSpec`——**不重复造轮子**。`source="native"` 标明它是本地函数；未来 MCP/OpenAPI 发现的工具会标 `source="mcp"`/`"openapi"`，在注册表里同构。

## 3. 内核接线：缺口出现后，在"本轮"补上工具（`kernel/loop.py`）

### 3.1 构造函数收下发现源（L62、L75-77）

```python
discovery: ToolDiscovery | None = None,
...
# 工具发现源：默认 None（安全默认，只发缺口事件、不动态注册）；
# 宿主显式注入（如 StaticCatalog）后，缺口出现时才会发现并注册新工具。
self.discovery = discovery
```

默认 `None` 是关键：**不配置发现源时，行为和以前完全一致**——只发 `capability_missing` 事件，绝不偷偷注册工具。这让新功能对老宿主零影响（第 15 篇测试里专门有一条守这个安全默认）。

### 3.2 在发缺口事件之后、构建提示词之前发现（L97-115）

```python
if (
    decision.missing_capability
    and strategy != Strategy.CLARIFY
    and len(self.registry) > 0
):
    yield AgentEvent(EventType.CAPABILITY_MISSING, {...})
    # 配置了发现源时，把"缺口"闭环成"新工具"：发现 -> 注册 -> 本轮即可用。
    if self.discovery is not None:
        async for ev in self._discover_tools(decision.missing_capability, task):
            yield ev
```

注意这个位置：它在 `_SYSTEM_TEMPLATE` 被格式化（L160）**之前**。系统提示词里的工具目录是在那之后才生成的，所以新注册的工具会**立刻出现在本轮发给模型的工具清单里**，不用等下一轮任务。

### 3.3 `_discover_tools`：发现、容错、去重、注册（L187-227）

```python
try:
    candidates = await self.discovery.discover(
        need,
        task=task,
        available=[s.name for s in self.registry.all()],
    )
except Exception as exc:
    yield AgentEvent(
        EventType.ERROR,
        {"stage": "tool_discovery", "error": f"{type(exc).__name__}: {exc}"},
        return
```

- 发现是**增强不是依赖**：目录服务挂了（比如未来连远程 MCP 目录超时），只发一个 `stage="tool_discovery"` 的错误事件然后 `return`，主流程照常收尾。`except Exception` 在这里是有意的，配 `noqa: BLE001` 并注释说明。

```python
new_specs: list[ToolSpec] = []
seen: set[str] = set()
for spec in candidates:
    if spec.name in seen or self.registry.has(spec.name):
        continue
    seen.add(spec.name)
    new_specs.append(spec)
if not new_specs:
    return
```

- **内核再做一次去重**，不信任发现源：既防候选彼此同名，也防"发现源返回时该工具已被注册"。纵深防御。
- 没有新工具就静默返回（不发 `tool_discovered` 事件，避免噪声）。

```python
self.registry.register_many(new_specs)
yield AgentEvent(
    EventType.TOOL_DISCOVERED,
    {
        "missing": need,
        "source": type(self.discovery).__name__,
        "registered": [s.name for s in new_specs],
    },
)
```

- 真正注册进 `ToolRegistry`，从此和内省/MCP/OpenAPI 工具**同构**，Router/Loop/Executor 完全无感知。
- 发 `tool_discovered` 事件（`types.py` L36 新增的第 9 个事件类型），带上缺口、发现源类名、注册名单——**每次自适应决策都可审计**这条红线在这里继续成立。
- `source` 用 `type(self.discovery).__name__`，于是事件里能看到 `"StaticCatalog"`，未来会是 `"McpDirectory"` 等。

### 3.4 发现的工具仍被权限闸拦住

注册只是"上架"，执行还要过 `PermissionPolicy`。测试 `test_discovered_tool_still_gated_by_permission_policy` 验证了：在 `auto_approve_tools=False` 下，新发现的 `get_weather` 不在白名单，执行时照样发 `permission_asked`，用户拒绝就不会真正调用。这就是"能被发现 ≠ 被授权执行"。

## 4. 门面透传（`core.py`）

`AgentCore.__init__` 增加 `discovery: ToolDiscovery | None = None`（L40），原样传给 `AgentLoop`（L74）。于是宿主的接线方式是：

```python
catalog = StaticCatalog([
    DiscoveredCandidate(get_weather, ("天气", "气温", "weather")),
])
core = AgentCore(model, llm_router=True, discovery=catalog)
```

`AgentCore.auto(...)` 也接受 `**kwargs` 透传，所以用自动内省的宿主同样能挂目录。

## 5. 谁在守这条闭环：13 个离线测试

`tests/test_discovery_catalog.py` 全部用脚本模型 + 进程内目录，**不需要网络或 Key**：

- 目录匹配 8 条：关键词命中缺口/命中任务、英文大小写、跳过已注册、无匹配返回空、无关键词永不命中、同名去重、描述覆盖。
- 闭环 5 条：
  1. `gap → discovered → tool_call → tool_result → 最终答案`，且注册后长期可用；
  2. 不配置发现源 → 只发缺口、不注册（安全默认）；
  3. 缺口与目录不匹配 → 不发 discovered；
  4. 发现源抛异常 → 发错误事件但不中断，照常 `done`；
  5. 发现的工具在严格权限下仍被 `permission_asked` 拦截。

脚本模型 `_GapThenCallModel` 的三次 `achat` 正好对应"分类报缺口 / 调用新工具 / 收尾"，是离线复现闭环的标准套路，host_f 的 `GapDiscoveryModel` 就是它的演示版。

## 6. 设计上的克制（哪些故意没做）

- **没做语义向量检索**：静态目录用关键词，确定性强、零依赖、好测试。语义匹配等接 MCP/插件市场时再上。
- **没让发现源自己注册工具**：发现源只交候选，注册/去重/权限归内核，防止外部来源绕过审计。
- **没在执行中反复发现**：当前只在"路由判定缺口"时发现一次。执行中途反思、动态换工具属于朋友方案里的 P2/P3，进 ROADMAP，不在这个最小切片里。
- **没做工具创造（写代码）**：那是 P3，需要沙箱 SPI，风险高，明确不做。

## 自检

1. 为什么 `StaticCatalog` 要求每个候选必须有关键词？把 `if not candidate.keywords: continue` 删掉会有什么后果？
2. 发现动作为什么放在系统提示词构建（L160）之前？放后面会怎样？
3. 发现源 `discover()` 抛异常时，任务为什么还能正常结束？这体现了什么设计原则？
4. "发现不等于授权"在代码里由哪两道闸分别保证？
5. 把 host_f 的 `get_weather` 关键词改成 `("股票",)`，再跑默认任务，会发生什么？为什么？
6. 为什么默认 `discovery=None` 是安全默认？它守的是哪条兼容性承诺？
