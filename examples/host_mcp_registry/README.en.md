> English translation of [中文](README.md) · The Chinese version is canonical; if they disagree, the Chinese wins.

# host_mcp_registry · read-only MCP Registry discovery

**English** · [简体中文](README.md)

Search the [official MCP Registry](https://registry.modelcontextprotocol.io/): metadata only; no
auto-install and no auto-connect. See [`docs/guides/mcp-registry.md`](../../docs/guides/mcp-registry.md)
for the security boundary.

```bash
uv run python examples/host_mcp_registry/search.py sql --limit 3
uv run python examples/host_mcp_registry/search.py github --json
```

The output lists server name/version, sanitized description, remotes (including required auth headers),
packages and repository URL. Verify provenance, pin the version, and keep secrets in env/secret stores.

### Explicit connect + call

```bash
uv run python examples/host_mcp_registry/connect.py \
    --query time --name ai.realtimeapi/mcp --allow-domain realtimeapi.ai --alias feeds
uv run python examples/host_mcp_registry/connect.py \
    --query time --name ai.realtimeapi/mcp --allow-domain realtimeapi.ai --call list_feeds
```

`--name` is required; tools are listed by default, `--call` actually invokes one.
