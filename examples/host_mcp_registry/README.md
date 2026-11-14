# host_mcp_registry · MCP Registry 只读发现

[English](README.en.md) · **简体中文**

官方 [MCP Registry](https://registry.modelcontextprotocol.io/) 搜索示例：只读元数据，
不自动安装、不自动连接。安全边界见 [`docs/guides/mcp-registry.md`](../../docs/guides/mcp-registry.md)。

```bash
uv run python examples/host_mcp_registry/search.py sql --limit 3
uv run python examples/host_mcp_registry/search.py github --json
```

输出包含 server 名称/版本、描述（已净化）、remote（含必填鉴权头）、package 与仓库地址。
接入前请自行确认来源、固定版本，并把 secret 放进环境变量/密钥库；可选 `RegistryPolicy.pinned_digests` 固定条目摘要。

### 显式接入 + 调用

```bash
uv run python examples/host_mcp_registry/connect.py \
    --query time --name ai.realtimeapi/mcp --allow-domain realtimeapi.ai --alias feeds
uv run python examples/host_mcp_registry/connect.py \
    --query time --name ai.realtimeapi/mcp --allow-domain realtimeapi.ai --call list_feeds
```

`--name` 必填；默认只列工具，`--call` 才真正调用。
