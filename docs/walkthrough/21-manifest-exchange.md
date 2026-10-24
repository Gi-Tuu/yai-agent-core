# 逐行讲解 21 · 能力清单交换：ToolSpec manifest（`types` 序列化 + `tools/manifest.py`）

> 第 10、12 篇让 Core 能经 **MCP / OpenAPI** 接入任意语言的服务。本篇补另一种需求：
> 不依赖任何在线服务，把"工具能力"本身变成一份**纯 JSON 文件**带走——
> 比如把一个组合好的工作流搬到另一个 Core 实例、或交给别的语言 / 工具去消费。
> 这份 JSON 叫 **manifest（能力清单）**。
> 关键边界（务必记住）：**manifest 只描述能力、不发明调用通道**——"manifest + 自有 JSON-RPC"
> 等于一个简化版 MCP，是重复造轮子。跨语言的实际调用仍走 MCP / OpenAPI。

## 0. 先看效果：一个组合工具变成 JSON，再在"新 Core"里跑起来

```python
from yai_core.tools import build_composite_spec
from yai_core.types import ToolSpec

composite = build_composite_spec(
    "chain", "先加一再翻倍",
    [{"tool": "add_one", "args": {"value": "{{$input.value}}"}},
     {"tool": "double", "args": {"value": "{{$steps.0}}"}}],
)
print(composite.to_manifest_dict())
```

输出（注意**没有 handler 字段**，工作流 steps 是纯数据）：

```json
{
  "name": "chain",
  "description": "先加一再翻倍",
  "input_schema": {"type": "object", "properties": {}},
  "source": "composite",
  "steps": [
    {"tool": "add_one", "args": {"value": "{{$input.value}}"}},
    {"tool": "double", "args": {"value": "{{$steps.0}}"}}
  ]
}
```

`version` 是**可选**键（v0.13 引入）：`ToolSpec.version` 非空时才会出现在清单里，
上面这个组合工具没设版本，所以输出中**没有**这个键。设了版本就多一行：

```json
{
  "name": "chain",
  "description": "先加一再翻倍",
  "input_schema": {"type": "object", "properties": {}},
  "source": "composite",
  "version": "v2",
  "steps": [
    {"tool": "add_one", "args": {"value": "{{$input.value}}"}},
    {"tool": "double", "args": {"value": "{{$steps.0}}"}}
  ]
}
```

把这份 JSON 交给另一个 Core 实例（`ToolSpec.from_manifest_dict(...)`），注册后它**照样能执行**——
因为组合工具的"执行逻辑"就是 steps 里的确定性编排（第 16 篇），不依赖任何运行时函数对象。

## 1. 核心设计：把"可移植的数据"和"运行时 handler"分开

`ToolSpec` 里既有数据（name / schema / steps / code），也有运行时对象（`handler`，一个 Python 函数）。
函数对象**无法**被 JSON 序列化，也不该跨进程 / 跨语言搬运。于是按来源区别对待：

| source | manifest 携带什么 | 导入后 handler | 导入后能直接执行吗 |
|---|---|---|---|
| `composite` | `steps`（每步 {tool, args}） | `None` | **能**，执行器读 steps 逐步编排 |
| `code` | `code`（Python 源码字符串） | `None` | 宿主**提供沙箱**即可（第 17/18 篇） |
| `native` / `openapi` / `mcp` | 仅能力声明 | `None` | **不能**，需绑定后端（这类跨语言请直接走 MCP/OpenAPI） |

> 一句话：**composite 和 code 是"数据即能力"，天然可移植；其余只导出"声明"。**

## 2. `types.py` · `to_manifest_dict` 逐行

```python
def to_manifest_dict(self) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "name": self.name,
        "description": self.description,
        "input_schema": self.input_schema,
        "source": self.source,
    }
```
先放四个**所有工具共有、且都是纯数据**的字段。`input_schema` 本身就是 JSON Schema（普通 dict / list / 字符串），可直接 JSON 化。

```python
    if self.source == "composite" and self.steps:
        manifest["steps"] = [
            {"tool": step.tool, "args": step.args} for step in self.steps
        ]
    if self.source == "code":
        manifest["code"] = self.code
    if self.version:
        manifest["version"] = self.version
    governance = governance_to_manifest(self.governance)
    if governance:
        manifest["governance"] = governance
    return manifest
```
- 组合工具：把每个 `CompositeStep`（dataclass）降成普通 dict 列表——dataclass 不能直接 `json.dumps`，转成 dict 才行；
- 代码工具：`code` 本来就是字符串，原样携带（执行仍需宿主沙箱，manifest 不含执行器）；
- **版本标记（v0.13 引入）**：`version` 是纯数据（组合工具用它标出"这套流程是 v1 / v2"），
  **非空时才写这个键**——没设版本的工具清单逐字节不变，老读者不认识这个键就当没有（向后兼容）；
  导入侧 `from_manifest_dict` 会把它读回来（`version = str(data.get("version", "") or "").strip()`）；
- 治理声明：只带**数据**（见第 5 节的六样），**空声明时整个键省略**，未治理的清单与旧格式逐字节一致；
- **全程不碰 `handler` 与 `gate` / `preview` / `idempotency_key`**：它们都是运行时函数，不可 JSON 化，也不可移植。

## 3. `types.py` · `from_manifest_dict` 逐行（这是 classmethod）

```python
@classmethod
def from_manifest_dict(cls, data: dict[str, Any]) -> ToolSpec:
    if not isinstance(data, dict):
        raise TypeError(f"manifest 工具必须是 object，得到 {type(data)!r}")
    name = str(data.get("name", "")).strip()
    if not name:
        raise ValueError("manifest 工具缺少非空 name")
```
`@classmethod` 表示这是"从数据造对象"的入口，第一个参数是类本身（`cls`），不用先有实例。
先做两道**入站校验**：必须是 dict、name 非空——脏数据在进注册表前就被拦下。

```python
    source = data.get("source", "native")
    if source not in cls._SOURCES:
        raise ValueError(...)
```
校验 source 必须是五种合法来源之一（`_SOURCES` 是类上的常量元组），防止拼错 / 伪造来源。

```python
    steps: list[CompositeStep] | None = None
    if source == "composite":
        raw_steps = data.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise ValueError(f"组合工具 {name!r} 的 manifest 缺少非空 steps")
        steps = []
        for index, raw in enumerate(raw_steps):
            ...
            tool = str(raw.get("tool", "")).strip()
            if not tool:
                raise ValueError(...)
            steps.append(CompositeStep(tool=tool, args=dict(raw.get("args", {}))))
```
组合工具：把 dict 列表**还原**回 `CompositeStep` 对象；逐步检查是 dict、tool 非空。
这与第 16 篇 `build_composite_spec` 的解析口径一致，只是这里放在最底层的 `types.py`、不依赖 `tools/composer.py`（保持分层：composer 依赖 types，types 不能反过来依赖 composer）。

```python
    code = data.get("code") if source == "code" else None
    return cls(
        name=name,
        description=str(data.get("description", "")),
        input_schema=dict(data.get("input_schema") or {}),
        handler=None,          # 关键：导入的工具永远没有 handler
        source=source,
        steps=steps,
        code=code,
        governance=governance_from_manifest(name, data.get("governance")),
    )
```
最后构造对象，**handler 写死 `None`**。这是"manifest 只搬运数据"的落点：
- composite / code 靠 steps / code + 宿主沙箱就能跑；
- 其余来源拿到的是"待绑定后端的声明"，不会假装可执行；
- 治理块走 `governance_from_manifest`：校验类型、拒绝不认识的键、`gate` 恒为 `None`（第 5 节）。

## 4. `tools/manifest.py`：批量打包 + 文件 IO

单个工具的转换在 `types.py`；这个文件负责"一批工具 + 版本包装 + 落盘"。

```python
MANIFEST_VERSION = 2                     # v2 起治理语义进入格式契约
_SUPPORTED_MANIFEST_VERSIONS = (1, 2)    # 本实现仍能读 v1 历史文件

def specs_to_manifest(specs):
    return {"manifest_version": MANIFEST_VERSION,
            "tools": [spec.to_manifest_dict() for spec in specs]}
```
顶层包一层 `manifest_version`——**格式将来不兼容升级时可据此判断**，读旧文件不会静默误解。
v2 的升级动机是治理语义：v1 老读者会**静默忽略** `governance` 块，于是跨进程导入后脱敏与
审批要求悄悄消失；升到 2 让老读者直接失败关闭。

```python
def specs_from_manifest(data):
    if data.get("manifest_version") not in _SUPPORTED_MANIFEST_VERSIONS:
        raise ValueError(...)
    tools = data.get("tools")
    if not isinstance(tools, list):
        raise ValueError("manifest 缺少 tools 数组")
    return [ToolSpec.from_manifest_dict(item) for item in tools]
```
反向：先验版本、再验 tools 是数组，然后逐个交给 `from_manifest_dict`。

```python
def write_manifest(path, specs):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(specs_to_manifest(specs), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

def read_manifest(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return specs_from_manifest(data)
```
文件 IO：父目录自动创建；`ensure_ascii=False` 让中文描述直接可读（不转成 `\uXXXX`），
`indent=2` 让人眼能看；读写都显式 `encoding="utf-8"`（Windows 默认是 GBK，不写会乱码）。

## 5. 治理声明随清单走 —— 但函数（`gate` 等）永远不走

`ToolSpec.governance` 里有九样东西：**六样是数据**（随清单走），**三样是函数**（不随清单走）：

| 字段 | 随清单导出 | 导入后 | 为什么 |
|---|---|---|---|
| `sensitive`（脱敏路径） | ✅ | 立即生效 | 纯字符串数组，跨语言可读 |
| `audit_sensitive` | ✅ | 立即生效 | 一个布尔提示 |
| `approval`（原生审批要求） | ✅ | **立即生效** | 它是数据，且只会把闸门往上抬 |
| `acc`（外部契约声明快照） | ✅ | **惰性记录**，不产生行为 | 它是"别人怎么描述这个能力" |
| `timeout_ms`（调用硬超时） | ✅ | **立即生效** | 与全局 `handler_timeout` 取更严的一档 |
| `rate_limit`（频率上限） | ✅ | **立即生效** | 进程内滑动窗口，超限不进 handler |
| `gate`（准入判定函数） | ❌ 丢弃 | `None` | 函数不可序列化，与 `handler` 同命 |
| `preview`（变更预览生成器） | ❌ 丢弃 | `None` | 同上，函数不可移植 |
| `idempotency_key`（幂等键生成器） | ❌ 丢弃 | `None` | 同上，跨进程去重归宿主 |

```python
spec.governance = ToolGovernance(
    sensitive=("api_key",),
    audit_sensitive=True,
    approval=ApprovalPolicy(when=(ApprovalRule("amount", ">", 1000),)),
    acc={"version": 1, ...},
    timeout_ms=2000,
    rate_limit=RateLimitHint(count=5, window="1m"),
)
spec.to_manifest_dict()["governance"]
# {"sensitive": ["api_key"], "audit_sensitive": True,
#  "acc": {...},
#  "approval": {"when": [{"param": "amount", "op": ">", "value": 1000}]},
#  "timeout_ms": 2000, "rate_limit": {"count": 5, "window": "1m"}}
#                              ← 没有 gate / preview / idempotency_key
```

`approval` 的清单形状有两处刻意：**没写值的算子（`exists`）不出 `value` 键**，而
`== null` 会写出 `"value": null`——"没给值"与"值就是 null"是两种语义，靠键的存在与否区分，
比多带一个布尔字段更难写错。反方向上，`ApprovalRule` 见到非 None 的值会把 `has_value` 补成
真：手写规则时忘填这个布尔只是省事，不该因此让导出的清单把阈值丢掉、导入后变成"每次都拒"。

反序列化侧 `governance_from_manifest(name, raw)` 做三件事，第三条最重要：

1. **类型校验**：`sensitive` 必须是字符串数组且每项非空、`audit_sensitive` 必须是布尔、
   `acc` 必须是 object 或 null、`approval` 逐字段严校验（算子必须落在 `APPROVAL_OPS` 里，
   因为它**会真的参与准入**）、`timeout_ms` 必须是正整数、`rate_limit` 必须是
   `{"count": 正整数, "window": "30s" / "1m" / "2h" / "1d"}`——脏数据在进注册表前就被拦下；
2. **不认识的键直接拒绝加载**。听起来苛刻，但静默忽略一个写错的 `sensitives`
   等于"宿主声明了脱敏、内核其实没做"，比报错危险得多。要加字段就升 `MANIFEST_VERSION`；
3. **绝不凭 `acc` 快照造 `gate`**。外来清单只是"那份服务说这个能力要求主体、要审批"，
   准入逻辑必须由本地宿主显式绑定：

```python
from dataclasses import replace
from yai_core.integrations.acc import build_gate, parse_declaration

parsed = parse_declaration(moved.governance.acc)
if parsed.valid and parsed.declaration is not None:
    moved.governance = replace(
        moved.governance, gate=build_gate(parsed.declaration, moved.input_schema)
    )
```

方向要说全：不绑定 = 这份声明**不会**给本机工具加上**函数级**闸门；同时也**不能**替本机
已绑定的闸门放行——`gate` 只能被宿主在本机代码里换掉，清单里根本没有这个通道。
`approval` 为什么敢导入即生效？因为它能说的只有两句话："这次要多问一个人"和"这次参数不合法，
别碰业务"。外来数据**加严**是安全的，**放松**才危险，而放松的那条路（函数级 `gate`）不给。
对应契约测试：`test_foreign_acc_snapshot_never_builds_a_gate`、
`test_host_rebinding_restores_the_gate`、`test_declared_paths_still_redact_after_transport`、
`test_approval_survives_manifest_round_trip`、`test_manifest_rejects_unknown_approval_fields_and_ops`。

## 6. 为什么"不做调用通道"是对的

可能会想顺手加一个"manifest 里写个 endpoint，Core 用 JSON-RPC 调它"——**不要做**：
- 那已经包含"列出工具（list）+ 调用工具（call）"两部分，正是 **MCP** 的核心；
- 自己做一份只会得到一个能力更弱、生态不兼容的"简化版 MCP"，正是要避免的重复造轮子。

manifest 的职责收敛为一件事：**让能力描述成为语言中立、可交换、可校验的数据。**
- 想跨语言真正调用 → 让对方暴露 MCP / OpenAPI（第 10 / 12 篇）；
- 想移植工作流 / 代码工具定义 → 用本篇 manifest。

## 7. 自检（读完应能回答）

1. 为什么 manifest 里没有 `handler`？导入后 `handler` 是什么？
2. 哪几种工具导入后能直接（或在沙箱内）执行？为什么？
3. 顶层 `manifest_version` 解决什么问题？
4. 为什么不该在 manifest 之上再发明一套 JSON-RPC 调用？
5. `write_manifest` 为什么要显式 `encoding="utf-8"` 和 `ensure_ascii=False`？
6. `governance` 里哪些字段随清单走、哪些不随？为什么内核不肯凭 `acc` 快照自己造 gate？
7. 治理块里出现不认识的键时为什么选择报错而不是忽略？
8. `approval` 导入即生效，`gate` 却不许凭清单重建——为什么"外来数据"一个放行一个拦？
