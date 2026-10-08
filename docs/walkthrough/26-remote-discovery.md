# 逐行讲解 26 · 远程动态发现源与多源聚合

> 对应源码：`discovery/remote_sources.py`、`discovery/aggregate.py`。
> 读完你应能说清：内核只接受**一个** discovery，当宿主同时有进程内目录和 MCP / OpenAPI
> 远程源时，这些外部工具是怎么被"懒连接、去重、聚合"成一个发现源的。

## 0. 一句话

**把一组现成的 MCP / OpenAPI bridge 交给 `RemoteSourceDiscovery`，它首次被问时才连接、
缓存并保持连接；`CompositeDiscovery` 再把多个发现源串成内核要的那一个。**

## 1. 为什么需要它

v0.6 的 `StaticCatalog` 只能发现"进程内预先登记的函数"。但真实工具常在外部：

- 一个公共 MCP Server（如 GitHub MCP）；
- 一个已部署服务的 OpenAPI 描述。

第 10 / 12 篇的 bridge 已经能"连接 → 列出 ToolSpec"，但它们**不是** `ToolDiscovery`
（没有 `discover(need, *, task, available)` 方法）。本文件做一层薄包装，不改 bridge。

## 2. `_RemoteBridge`：用 Protocol 描述"我需要 bridge 有什么"

```python
class _RemoteBridge(Protocol):
    async def connect(self) -> list[ToolSpec]: ...
    async def aclose(self) -> None: ...
```

- 不关心你是 MCP 还是 OpenAPI，只要有"异步连接返回 specs"和"异步关闭"两个方法即可。
- 这让测试能直接塞一个**假 bridge**（不用装 mcp / httpx）。

## 3. `RemoteSourceDiscovery.discover`：懒连接 + 去重

```python
async def _ensure_specs(self) -> list[ToolSpec]:
    if self._cache is None:                 # ① 只在第一次连接
        specs = []
        for bridge in self._bridges:
            try:
                specs.extend(await bridge.connect())
            except Exception:               # ② 单源挂了跳过，不拖垮其他源
                continue
        self._cache = specs
    return self._cache
```

要点：

1. **懒连接**：`_cache is None` 才连；构造发现源时不发生任何网络调用。
2. **单源容错**：某个外部源超时 / 连不上，`except` 跳过，其余源照常。
3. **连接常驻**：连接结果被缓存、bridge **不关闭**——因为每个 ToolSpec 的 handler 闭包
   依赖 bridge 底层的 client（MCP 连接 / httpx client），关了 handler 就调不通。

```python
async def discover(self, need, *, task, available):
    specs = await self._ensure_specs()
    already, seen, out = set(available), set(), []
    for spec in specs:
        if spec.name in already or spec.name in seen:
            continue                        # ③ 跳过已注册 / 同批重名
        seen.add(spec.name)
        out.append(spec)
    return out
```

- `available`：内核传入"当前已注册工具名"，避免重复注册。
- `seen`：同一批里多个源给了同名工具，只留第一个。
- `need` 当前**不做**自由文本筛选：远程工具名与一句自然语言匹配不可靠、容易漏；
  配置了哪些源本身就是白名单。相关性检索留给 v0.7 的语义层（见第 20 篇）。

## 4. 两个工厂：把 config 变成发现源

```python
def mcp_discovery(configs):
    from ...mcp.client import McpToolBridge          # 函数内 import
    return RemoteSourceDiscovery([McpToolBridge(c) for c in configs])

def openapi_discovery(configs, *, transport=None):
    from ...openapi.client import OpenApiToolBridge
    return RemoteSourceDiscovery(
        [OpenApiToolBridge(c, transport=transport) for c in configs])
```

- import 放在函数内：不构造就不加载集成模块；bridge 内部对第三方包也是懒加载。
- `transport` 仅供测试注入 `httpx.MockTransport`，生产传 None。

## 5. `CompositeDiscovery`：多个发现源 → 一个

内核签名是 `discovery: ToolDiscovery | None`（**单个**）。当宿主想同时用 StaticCatalog
和远程源，就用聚合器：

```python
async def discover(self, need, *, task, available):
    out = []
    current_available = set(available)
    for source in self._sources:
        specs = await source.discover(need, task=task,
                                     available=list(current_available))
        for spec in specs:
            if spec.name in current_available:
                    continue                # 跨源再去重
            current_available.add(spec.name)
            out.append(spec)
    return out
```

关键：**把前面源已返回的名字加进 `current_available`**，传给后面的源。这样后一个源
"知道"前一个源已经提供了哪些工具，不会重复返回——跨源去重。

`list_discoverable` 同理：用 `getattr(source, "list_discoverable", None)` 探测，没实现
该可选方法的源跳过，结果按 name 去重聚合。

## 6. 测试里怎么验证（tests/test_remote_discovery.py、test_composite_discovery.py）

- `FakeBridge`：记录 `connect_calls` / `closed`，可设 `fail=True` 抛错。
- 断言：聚合后名字正确、单源失败被跳过、`connect` 只调一次（懒连接）、`aclose`
  关闭全部；CompositeDiscovery 给第二个源的 available 已包含第一个源的工具名。
- 全程假对象，**不连网、不装 mcp / httpx**。

## 7. 自检

1. 为什么 `RemoteSourceDiscovery` 连接后不能立刻 `aclose`？
2. `available` 和 `seen` 分别去掉哪一类重复？
3. 内核只要一个 discovery，宿主同时想用静态目录 + MCP，该用哪个类？它如何避免跨源重名？
