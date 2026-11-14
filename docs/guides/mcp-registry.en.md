> English translation of [中文](mcp-registry.md) · The Chinese version is canonical; if they disagree, the Chinese wins.

# MCP Registry discovery (read-only)

**English** · [简体中文](mcp-registry.md)

The [official MCP Registry](https://registry.modelcontextprotocol.io/) is a public metadata directory of
MCP servers. YAI offers **read-only discovery**: search by keyword, inspect remotes/packages, and let the
host **explicitly** decide whether to connect. It never auto-installs packages, never runs registry
commands, and never auto-connects to a server.

## 1. Quick start

```bash
uv run python examples/host_mcp_registry/search.py sql --limit 3
uv run python examples/host_mcp_registry/search.py github --json
```

## 2. Connection example (explicit choice)

```bash
# --name is required; by default only tools are listed, nothing is called
uv run python examples/host_mcp_registry/connect.py \
    --query time --name ai.realtimeapi/mcp \
    --allow-domain realtimeapi.ai --alias feeds

# Add --call to actually invoke one explicitly
uv run python examples/host_mcp_registry/connect.py \
    --query time --name ai.realtimeapi/mcp \
    --allow-domain realtimeapi.ai --call list_feeds
```

Verified 2026-11-14: connected to `https://mcp.realtimeapi.ai/mcp`, listed 6 tools, and
`list_feeds` returned the feed list. The example never picks "the first" server for you — `--name`
is required.

## 3. Trust policy and signature hook

```python
from yai_core.integrations.mcp import McpRegistryClient, RegistryPolicy

policy = RegistryPolicy(
    allowed_domains=("realtimeapi.ai",),      # exact domain or subdomain
    allowed_repository_hosts=("github.com",),
    require_repository=True,
    verify_entry=lambda entry: my_verify(entry),  # optional signature/provenance hook
)
result = McpRegistryClient().search("time", policy=policy)
config = result.servers[0].build_config(policy=policy)
```

- **Domain allowlist**: exact or subdomain; the CLI requires `--allow-domain` by default
  (`--allow-any-host` is the explicit opt-out);
- **Built-in digest pinning**: `pinned_digests=(('server/name', sha256),)` pins the SHA-256 of the
  canonical raw entry; `require_pinned=True` rejects unpinned entries. This is integrity, not identity.
- **Signature hook**: `verify_entry(entry)` receives the raw registry entry and drops it on False;
  this module ships **no crypto** (registry v0 has no stable unified signature field), so hosts can
  plug sigstore/JWS;
- **Secret defaults are never trusted**: registry-provided defaults for `isSecret` headers are
  ignored; the caller must supply them;
- **Version/status**: `isLatest` + `active` required by default, configurable.

## 4. Code entry point

```python
from yai_core.integrations.mcp import McpRegistryClient

result = McpRegistryClient().search("sql", limit=5)
for server in result.servers:
    print(server.name, server.version, server.safe_description)
    for remote in server.remotes:
        print(remote.type, remote.url, remote.required_headers)

# Only after you trust the server and supply variables/headers, build a config explicitly.
config = result.servers[0].build_config(
    alias="registry_demo",
    variables={"tenant": "eu"},
    headers={"X-API-Key": "..."},   # take this from env / a secret store
)
```

## 3. Security boundaries (important)

The registry is **untrusted input**. Before connecting, check at least:

- **Provenance**: prefer official namespaces / servers with a source repository; `repository_url` is a
  claim, not verification;
- **Transport**: only `https` + `streamable-http`; URL templates must be filled explicitly, no guessed
  defaults;
- **Auth**: `isSecret` headers come from the host (env / secret store); never hard-code or commit them;
- **Description poisoning**: titles/descriptions pass through `sanitize_untrusted_text` (control /
  zero-width / bidi characters removed, length capped), but there is **no semantic instruction
  detection**; use `trust_tool_descriptions=False` in high-risk environments;
- **Version**: pin the version when possible;
- **Permissions**: after connecting, the kernel `PermissionPolicy` / approvals / audit still apply; being
  listed in the registry grants no extra trust.

YAI does not decide whether a server is trustworthy; it only organizes metadata, and **trust stays with
the host**.

## 4. Boundaries

- Read-only search (`GET /v0/servers`); no publishing or registry mutation;
- No OAuth flow, no package installation, no registry command execution;
- The Registry API is still v0; the parser tolerates missing fields but ignores unknown ones;
- Only `streamable-http` remotes convert to `McpServerConfig`; `sse` is display-only.

## 5. Tests

```bash
uv run pytest tests/test_mcp_registry.py -q
```

Covers nested response parsing, hidden-character sanitization, latest flag, URL variables/required
headers, rejection of HTTP and unresolved templates, malformed responses; fully offline (injected fake
`urlopen`).
