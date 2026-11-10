# host_n · OpenAPI 3.1 离线工具注册

[English](README.en.md) · **简体中文**

用一份内联 OpenAPI 3.1 描述，将一个读取操作注册到 Core 使用的 `ToolRegistry`，
打印工具名称与清洗后的 `input_schema`。不需要 API Key、模型或网络。

## 运行

在仓库根目录安装可选依赖后运行：

```bash
python -m pip install -e ".[openapi]"
python examples/host_n_openapi31/run.py
```

使用项目的 uv 环境也可以：

```bash
uv run --extra openapi python examples/host_n_openapi31/run.py
```

## 看输出中的什么

输出是一个 JSON 对象，`tools` 中有 `openapi31_list_items`，`notes` 为空。
查看该工具 `input_schema.properties` 中的三个参数：

| 参数 | 描述中的构造 | 清洗后的结果 |
|---|---|---|
| `note` | `type: ["string", "null"]` | 联合类型保留，可表达空值 |
| `mode` | `const: "preview"` | `enum: ["preview"]`，不再包含 `const` |
| `limit` | `$ref` 指向整数 schema，兄弟键覆盖 `maximum` 和 `description` | 保留 `type: "integer"`、`minimum: 1`，最大值从 50 变成 3，说明也替换 |

完整支持矩阵与边界见 [OpenAPI 3.1 兼容指南](../../docs/guides/openapi-31-compat.md)。

## 为什么不启动真实 HTTP 服务

本示例只验证发现、schema 翻译与工具注册，不调用注册出的工具，也不运行 Agent 会话。
描述从内存读取；`example.invalid` 只是操作的占位地址。
`httpx.MockTransport` 在内存中拦截请求并报错，即使以后误调用工具，也不会连接网络。
桥在退出异步上下文时关闭 HTTP 客户端。

因此这个示例不能证明真实接口、鉴权、服务端参数校验或完整 JSON Schema 2020-12 兼容性。
