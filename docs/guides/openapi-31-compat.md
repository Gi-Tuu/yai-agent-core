# OpenAPI 3.1 / JSON Schema 2020-12 兼容矩阵

[English](openapi-31-compat.en.md) · **简体中文**

> 状态：**v0.32 起支持**（证据：`tests/test_openapi_31_compat.py`，10 例）。
> 本页只讲"工具入参 schema 的翻译边界"：OpenAPI 全量 JSON Schema 2020-12 词汇里，只有
> 工具调用需要的子集被翻译；**未翻译的构造只记 notes，不因此拒绝整份描述**。

## 1. 支持矩阵

| OpenAPI / 2020-12 构造 | 处理方式 | 模型看到的结果 |
|---|---|---|
| `openapi: 3.1.x` | 接受（3.0.x / 3.1.x 同一路径） | — |
| `type: ["string", "null"]` | 原样保留 | 标准 JSON Schema 联合类型；null 不会被入参校验误拒 |
| `const: value` | 归一成 `enum: [value]`（语义等价） | 单值 enum（部分后端不认 `const`） |
| `nullable: true`（3.0） | 转成 `type: [..., "null"]` | 标准联合类型 |
| `$ref` + 兄弟键（3.1 允许） | 先内联目标，再用兄弟键覆盖同名字段 | 覆盖后的完整 schema |
| `exclusiveMinimum/Maximum`（数值型） | 原样保留 | 全局入参校验**不强制**这一层（见 §3） |
| `anyOf` / `oneOf` / `allOf` / `not` | 保留 | 全局校验遇到放宽型组合时跳过该层，避免误拒 |
| `prefixItems` | **不翻译**（数组只保留 `items`） | 仅数组类型；notes 提示 |
| `if` / `then` / `else` | **不翻译** | 无；notes 提示 |
| `patternProperties` / `dependentSchemas` / `contains` / `unevaluatedProperties` | **不翻译** | 无；notes 提示 |
| `webhooks`（3.1） | **不注册为工具**（只处理 `paths`） | 无；notes 提示 |
| 路径级 `servers`（3.1） | 不生效（仍用全局 `servers`） | 无；notes 提示 |
| 外部 `$ref`（跨文件/URL） | 不解析，原样保留并记 note | 该参数 schema 可能不完整 |

## 2. 为什么"不翻译也不报错"

OpenAPI 3.1 允许完整的 JSON Schema 2020-12，而工具入参只需要其中很窄的子集。
为个别没用到的关键字（例如只在响应里出现的 `patternProperties`）拒绝整份描述，
会把本来可用的操作一起挡掉。因此策略是：**能安全翻译的翻译；翻译不了的记 notes；
校验层宁可放宽也不误拒**（与 `tests/test_schema_compatibility.py` 的口径一致）。

`plan_operations()` 返回的 `notes` 会带出这些信息，宿主可以展示在能力体检/日志里。

## 3. 已知边界

- **全局入参校验是子集**：`exclusiveMinimum/Maximum`、`const`（归一为 enum 后按 enum 校验）、
  `patternProperties` 等不在校验器里强制；schema 会原样交给模型，但最终防线仍在宿主/服务端。
- **不追求 2020-12 全覆盖**：`$dynamicRef`、`unevaluatedItems`、`contentEncoding` 等不支持，
  也不会出现在模型 schema 里（被白名单清洗掉）。
- **不跨文档解析 `$ref`**：外部引用保持原样，参数 schema 可能不完整。
- **webhooks 永远不是工具**：本集成只把 `paths` 翻译成工具。

## 4. 复现

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_openapi_31_compat.py -q
.\.venv\Scripts\python.exe -m pytest tests\test_schema_compatibility.py tests\test_schema_sanitize.py -q
```

## 5. 可运行的离线示例

安装 `[openapi]` 可选依赖后，在仓库根目录运行：

```bash
python examples/host_n_openapi31/run.py
```

[示例说明](https://github.com/Gi-Tuu/yai-agent-core/blob/main/examples/host_n_openapi31/README.md)展示工具注册后的清洗结果：
`note` 保留 `["string", "null"]`，`mode` 的 `const` 转为 `enum`，`limit` 的 `$ref`
兄弟键将最大值从 50 覆盖为 3。全程无需模型、Key 或网络；不执行工具或启动 HTTP 服务，
误发的请求也会被内存中的 mock transport 拒绝。
