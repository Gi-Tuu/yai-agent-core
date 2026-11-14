> English translation of [中文](README.md) · The Chinese version is canonical; if they disagree, the Chinese wins.

# host_o_go_mcp · Go host over MCP

**English** · [简体中文](README.md)

A **zero-third-party-dependency** Go stdio MCP server (`initialize` / `tools/list` / `tools/call`),
started and called by a Python `AgentCore` through the MCP client (`lookup_order`).

```bash
# Requires Go 1.22+; run from the repository root
uv run python examples/host_o_go_mcp/run.py
```

Without Go, the script prints install instructions and exits with code **2** (it never tries to
install Go over the network).

## Layout

| File | Purpose |
|---|---|
| `go.mod` / `main.go` | Minimal Go MCP server: newline-delimited JSON-RPC, exposes `lookup_order` |
| `run.py` | checks Go → `McpToolBridge` starts `go run` → registers tools → one ScriptedModel call |

## MCP boundary

- The Go server exposes only the MCP tool surface; tools remain ordinary `ToolSpec(source="mcp")`;
- No new tool-call channel is introduced; routing/permissions/audit stay in Python;
- This is an **example**, not part of the CI matrix (Go is not a project dependency).

## Known limits

- Go is not installed on this machine, so the Go code was not compiled here; the no-Go path is
  covered by an offline test;
- The Go server implements only the minimal method set needed by the example.
