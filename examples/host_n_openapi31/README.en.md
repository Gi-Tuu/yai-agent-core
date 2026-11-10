> English translation of [中文](README.md) · The Chinese version is canonical; if they disagree, the Chinese wins.

# host_n · Offline OpenAPI 3.1 tool registration

**English** · [简体中文](README.md)

An inline OpenAPI 3.1 description registers one read operation in the `ToolRegistry` used by Core,
then prints the tool name and sanitized `input_schema`. No API key, model or network is needed.

## Run

From the repository root, install the optional dependencies and run:

```bash
python -m pip install -e ".[openapi]"
python examples/host_n_openapi31/run.py
```

Or use the project's uv environment:

```bash
uv run --extra openapi python examples/host_n_openapi31/run.py
```

## What to inspect

The output is one JSON object. Its `tools` list contains `openapi31_list_items`, and `notes` is empty.
Inspect these parameters under the tool's `input_schema.properties`:

| Parameter | Construct in the description | Sanitized result |
|---|---|---|
| `note` | `type: ["string", "null"]` | The nullable union is preserved |
| `mode` | `const: "preview"` | `enum: ["preview"]`, with no `const` |
| `limit` | An integer `$ref` with sibling overrides for `maximum` and `description` | Keeps `type: "integer"` and `minimum: 1`; maximum changes from 50 to 3 and the description is replaced |

See the [OpenAPI 3.1 compatibility guide](../../docs/guides/openapi-31-compat.en.md) for the full matrix and limits.

## Why there is no HTTP server

This example only demonstrates discovery, schema translation and registration. It does not invoke
the registered tool or run an Agent session. The description is loaded from memory;
`example.invalid` is only a placeholder operation address.
An in-memory `httpx.MockTransport` rejects requests, so even an accidental future tool invocation
cannot connect to the network. Leaving the bridge's async context closes its HTTP client.

This does not verify a real API, authentication, server-side argument validation, or full JSON Schema
2020-12 compatibility.
