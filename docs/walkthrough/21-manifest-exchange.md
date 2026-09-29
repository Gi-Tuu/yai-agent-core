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
    return manifest
```
- 组合工具：把每个 `CompositeStep`（dataclass）降成普通 dict 列表——dataclass 不能直接 `json.dumps`，转成 dict 才行；
- 代码工具：`code` 本来就是字符串，原样携带（执行仍需宿主沙箱，manifest 不含执行器）；
- **全程不碰 `handler`**：它是函数运行时对象，不可 JSON 化，也不可移植。

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
    )
```
最后构造对象，**handler 写死 `None`**。这是"manifest 只搬运数据"的落点：
- composite / code 靠 steps / code + 宿主沙箱就能跑；
- 其余来源拿到的是"待绑定后端的声明"，不会假装可执行。

## 4. `tools/manifest.py`：批量打包 + 文件 IO

单个工具的转换在 `types.py`；这个文件负责"一批工具 + 版本包装 + 落盘"。

```python
MANIFEST_VERSION = 1

def specs_to_manifest(specs):
    return {"manifest_version": MANIFEST_VERSION,
            "tools": [spec.to_manifest_dict() for spec in specs]}
```
顶层包一层 `manifest_version`——**格式将来不兼容升级时可据此判断**，读旧文件不会静默误解。

```python
def specs_from_manifest(data):
    if data.get("manifest_version") != MANIFEST_VERSION:
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

## 5. 为什么"不做调用通道"是对的

可能会想顺手加一个"manifest 里写个 endpoint，Core 用 JSON-RPC 调它"——**不要做**：
- 那已经包含"列出工具（list）+ 调用工具（call）"两部分，正是 **MCP** 的核心；
- 自己做一份只会得到一个能力更弱、生态不兼容的"简化版 MCP"，正是要避免的重复造轮子。

manifest 的职责收敛为一件事：**让能力描述成为语言中立、可交换、可校验的数据。**
- 想跨语言真正调用 → 让对方暴露 MCP / OpenAPI（第 10 / 12 篇）；
- 想移植工作流 / 代码工具定义 → 用本篇 manifest。

## 6. 自检（读完应能回答）

1. 为什么 manifest 里没有 `handler`？导入后 `handler` 是什么？
2. 哪几种工具导入后能直接（或在沙箱内）执行？为什么？
3. 顶层 `manifest_version` 解决什么问题？
4. 为什么不该在 manifest 之上再发明一套 JSON-RPC 调用？
5. `write_manifest` 为什么要显式 `encoding="utf-8"` 和 `ensure_ascii=False`？
