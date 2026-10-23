# 逐行讲解 50 · 能力接入体检与 schema 兼容性

> 对应源码：`src/yai_core/tools/health.py`、`src/yai_core/tools/schema.py`、
> `src/yai_core/policy/approval.py`、`core.py::AgentCore.capability_health`。
>
> 这一篇解决一个问题：**声明了约束，不代表执行器真的能执行它。**

## 0. 为什么需要"体检"

宿主接入工具时最容易写出的不是运行时崩溃，而是"看起来有护栏"：

```python
input_schema = {"type": "object", "properties": {"x": {"anyOf": [...]}}}
governance = ToolGovernance(rate_limit=RateLimitHint(1, "forever"))
approval = ApprovalPolicy(when=(ApprovalRule("totl", ">", 1000),))
```

这三条分别会：

- `anyOf` 当前不参与入参校验；
- `forever` 解析不出秒数，频控实际不拦；
- `totl` 拼错，每次调用都会被审批层判为参数不合法。

它们都不是"跑到一半才炸"，而是**接入那一刻就已经与执行能力不匹配**。
能力体检把这些差异提前列出来，宿主可以选择修声明、换实现，或明确接受降级。

## 1. 只做静态检查

```python
report = core.capability_health()
print(report.to_text())
print(report.to_dict())
```

`check_tool_specs(specs, registry=..., sandbox_available=...)` 的边界：

- **不调用 handler**；
- 不访问网络、不改注册表、不发事件；
- 只读 `ToolSpec` 的声明与注册表里已有的名字；
- 组合步骤是否存在、代码工具是否有沙箱，由调用方提供上下文。

`AgentCore.capability_health()` 自动使用当前注册表与执行器的沙箱状态。

## 2. 报告结构

```python
@dataclass(frozen=True)
class HealthFinding:
    tool: str
    code: str                 # 稳定 code，供宿主筛选/白名单
    severity: str             # error / warning / info
    message: str
    path: str = ""            # 例如 $.properties.x.anyOf
```

`CapabilityHealth.ok` 只表示**没有 error**；warning 是"能力可用，但这层没做到
你以为它做到的事"。`to_text()` 是给人看的，`to_dict()` 可 JSON 序列化。

## 3. 检查项

| 类别 | 例子 | 严重度 |
|---|---|---|
| schema 类型 | 未知 `type`、非法 type 数组 | error |
| schema 未实现键 | `anyOf` / `oneOf` / `$ref` / `additionalProperties` / `const` … | warning |
| format | 只实现 `date` / `date-time`，其余只做注解 | warning |
| pattern | 非法正则（运行期会跳过） | warning |
| required | 引用了未在 properties 声明的字段 | warning |
| 审批路径 | `approval.when` 的路径不在 input_schema 中（运行期每次都拒） | error |
| 审批算子/值 | 未知算子、数值条件给了非数字 | error |
| 治理 | 非法 `timeout_ms`、无法解析的频控窗口 | error |
| 组合 | 没有步骤、引用未注册工具、嵌套组合 | error |
| 代码工具 | 没有代码、没有沙箱、绑定了 handler | warning |
| 声明型工具 | native/openapi/mcp 没有 handler（需绑定后端） | info |

## 4. schema 兼容性：宁可跳过，不可误拒

全局入参校验只实现 JSON Schema 的一个子集。遇到可能**放宽**取值域的组合键时：

```python
if any(key in schema for key in ("anyOf", "oneOf", "$ref")):
    return          # 这层不校验，体检会提示；不按顶层 type 误拒另一条合法分支
```

原因很直接：`{"type": "string", "anyOf": [{"type": "integer"}]}` 在完整 JSON Schema
下可以接受整数；只认 `type` 就会把合法调用判成非法。兼容性优先，未校验的部分由
能力体检标出来。

`additionalProperties`、`exclusive*`、`const` 等只收紧/补充的键不改变顶层类型，
仍按已实现的关键字校验；未实现的部分不进判定。

OpenAPI 3.0 的 `nullable: true` 不丢：清洗时转成标准 `type: [T, "null"]`，
枚举补 `null`，这样模型给可选参数传 `null` 不会被全局校验误拒。

## 5. 它不做什么

- **不自动修 schema**：哪些未实现键该删、该展开、该换成宿主业务校验，是宿主的选择；
- **不替代运行时权限**：体检通过不等于工具安全，权限策略、审批和执行器仍然各司其职；
- **不保证完整 JSON Schema**：只检查已实现子集与已知的"声明/执行不匹配"。

## 6. 自检

1. 为什么 `latest` 这类状态问题和 schema 体检要分开处理？
2. `anyOf` 存在时为什么选择跳过这一层，而不是按顶层 `type` 拒绝？
3. warning 与 error 的分界是什么？为什么"未实现键"不是 error？
4. 能力体检通过，能不能说明工具已经安全？为什么？
5. `nullable` 为什么必须在清洗时归一，而不是留给模型自己理解？
