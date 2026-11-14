> English translation of [中文](federation.md) · The Chinese version is canonical; if they disagree, the Chinese wins.

# Directed Federation Design Draft

**English** · [简体中文](federation.md)

> Status: **Phase 1 (in-process), Phase 2 (local IPC), and Phase 3 (ecosystem operations) are implemented.**
> For the decision background and boundaries see [ADR 0008](../adr/0008-directed-federation.en.md).
> In one sentence: **every product embeds its own core; a core inside a parent product can call a core inside a child product, but there is no path in the reverse direction.**

## 0. Implemented scope (Phase 1 / Phase 2 / Phase 3)

Code entry points: `shell/federation/`; example: `examples/host_m_federation/`; tests: `tests/test_federation.py`,
`tests/test_federation_ipc.py`.

- **Phase 1 (in-process)**: a subset of the child core's tools is registered as parent-core tools (`<prefix>__<tool>`); the grant validates child-core identity /
  tool subset / expiry; parent-core policy plus child-core policy form a dual defense line; depth, fan-out, and cycle detection; timeouts; failures flow back as tool errors.
- **Phase 2 (local IPC)**: `ChildServer` (loopback TCP + NDJSON JSON-RPC + 0600 discovery file + token) and
  `LinkChild` (parent-side long-lived connection, request multiplexing); the handshake issues an **HMAC grant** (the key lives only inside the child process);
  `health` / `tools.list` / `tools.call` are supported; cross-process calls inject the call chain into cycle detection; timeout / disconnection / invalid grant
  all fail closed.
- **Phase 3 (ecosystem operations)**: parent-side `refresh_child()` supports hot manifest updates (on the IPC side this works together with
  `manifest_version` from `federation.tools.list`); `ChildServer(edge_quota_per_minute=...)` rate-limits each edge with a sliding window; parent-side
  `register_child(max_calls=...)` plus `edge_stats()` track calls / failures / elapsed time;
  `ManagedChild` supervises a child process (spawn → wait for the discovery file → connect → reconnect / stop).
- **Phase 3 closing items**:
  - **Specialist bridge**: `build_specialist_child()` /
    `attach_specialists()` in `shell/federation/specialists.py` introspect the public methods of an "application object" (the shape used by desktop dynamic-island specialists) into child-core tools,
    so one line attaches it to a parent core; wiring the Qt panel itself is still host-side.
  - **usage / token budget**: child-core tool results may carry `{"usage": {...}}`; the parent aggregates them per edge into
    the `tokens` / `usage` of `edge_stats()`; `register_child(max_tokens=...)` yields
    `edge_token_budget_exceeded` once the limit is exceeded (fail closed).
  - **Two installable child-core examples**: `examples/child_cores/` (notes / calculator), where
    `hub_demo.py` hosts both processes at once and calls across cores.
- **Not implemented**: cross-core transactions, supervised auto-restart (after the process dies the host must decide to restart),
  server-side manifest push (today the parent pulls it by calling `refresh_child` explicitly).
- **Security precondition**: the same OS user; a 0600 discovery file plus a token guard against other users / processes on the same machine; there is **no defense against root / debuggers / memory scraping**.

### Phase 2 protocol implementation points

| Item | Implementation |
|---|---|
| Transport | `127.0.0.1` random port, NDJSON with one JSON-RPC 2.0 message per line, a single message ≤ 1 MiB |
| Discovery file | `<data>/yai-federation/<node_id>.json`: protocol / node_id / child_id / host / port / token / pid, permissions 0600 (chmod semantics are limited on Windows, see `_noop_chmod`) |
| Handshake | `federation.handshake`: validates the protocol version + token + parent allowlist → returns the manifest subset + HMAC grant |
| Call | `federation.tools.call`: validates grant signature / expiry / tool subset / child-core identity / bound subject; `deadline_ms` is optional |
| Subject | Handshake carries `subject` and signs it into the grant; calls must match. Grants without a subject reject subject-bearing calls |
| Health | `federation.health`: process alive + child-core identity + protocol version + tool count |
| Errors | `grant_invalid` / `protocol_error` / `deadline_exceeded` / connection dropped → parent-side `ChildUnavailable` or tool failure flow-back |

## 1. Roles and topology

```
Parent product (main product A)                    Secondary product 1 (docs)
┌─────────────────────────────────┐              ┌─────────────────────────────────┐
│ Host UI / business logic        │              │ Host UI / business logic        │
│  ┌───────────────────────────┐  │   grant A→1  │  ┌───────────────────────────┐  │
│  │ Parent core AgentCore     │──┼─────────────▶│  │ Child core AgentCore      │  │
│  │ tools ⊆ own + child       │  │  JSON-RPC    │  │ tools = docs capabilities │  │
│  └───────────────────────────┘  │  (loopback)  │  └───────────────────────────┘  │
└─────────────────────────────────┘              └─────────────────────────────────┘
        ▲                                                          ▲
        │ Another parent product B can also                        │ The child core stays usable
        └──────────────────────────────────────────────────────────┘ on its own: the user can
          configure an independent A→1 grant                         open the docs product
                                                                     directly and it works as usual
```

Rules:

1. **Directed acyclic**: an edge = the right of a parent to call a child. One core can be both a parent and a child (an ecosystem inevitably looks like that).
2. **m:n explicit configuration**: the same child core can be called by several parent cores, with an independent grant / audit / quota per edge.
3. **Sibling-to-sibling direct connections are forbidden by default**: sibling cores cooperate by routing through a common parent core (the parent orchestrates); no sibling channel is created automatically.
4. **Independence first**: a child core stays fully usable while the parent is offline; the parent merely "borrows" the capabilities the child declares.

## 2. Components and code locations (host layer, zero kernel changes)

| Component | Location | Responsibility |
|---|---|---|
| `CoreNode` | `shell/federation/node.py` | Wraps an `AgentCore` + its own `policy` / `manifest` / `subject`; independent lifecycle |
| `Federation` | `shell/federation/parent.py` | Parent side: register child cores, pull manifests, pick the tool subset, issue grants, depth/fan-out/cycle detection, budget aggregation |
| `ChildLink` | `shell/federation/link.py` | Parent-side long-lived connection client (reconnect, correlation ID, timeout, cancel) |
| `ChildServer` | `shell/federation/server.py` | Child side: loopback JSON-RPC server + discovery file + grant validation + invoking child-core tools |
| Discovery file | `%LOCALAPPDATA%/yai-federation/<product>.json` | pid / port / protocol version / token (permissions 0600) |

Reused: permission narrowing, budgets, and the concurrency gate from `shell/employee`; the capability-list format of `manifest`;
the JSON-RPC shape of the sidecar; the discovery-file + token pattern of the Bridge.

## 3. Capability exchange (manifest subset)

- After startup, a child core returns its own manifest through `federation.handshake` (tool names, JSON Schemas, governance summary,
  manifest version, protocol version).
- The parent **explicitly selects a subset** and registers it as tools (by default not all child capabilities are exposed):
  names such as `doc.append_entry` and `doc.search` are prefixed by the parent, which avoids collisions with its own tools.
- Version negotiation: after the child manifest is upgraded the parent can hot-update it; when a tool is removed/renamed the parent marks `child_tool_gone` and stops routing to it.

## 4. Grant (call credential)

```json
{
  "v": 1,
  "edge": "atm-main:doc-app",
  "child_id": "doc-app@0.27.2",
  "tools": ["doc.append_entry", "doc.search"],
  "subject": {"id": "user-42", "tenant": "default"},
  "issued_at": "2026-11-09T14:00:00Z",
  "expires_at": "2026-11-09T15:00:00Z",
  "nonce": "b7f1…",
  "sig": "hmac-sha256(...)"
}
```

- Issuing: the child side generates a **symmetric key** (written into the discovery file, readable only by the same user); after the parent obtains the token from the local discovery file,
  does it sign grant requests itself with a key of its own? — **No**. Simplified: the token in the discovery file is a "connection credential"; the grant is issued by the child during the
  handshake based on "parent identity + tool subset", so the parent holds only the grant, never the child's key.
- Validation: for every `tools.call` the child validates the grant signature / expiry / edge / tool name / subject match;
  any mismatch → `grant_invalid` / `tool_not_granted`, fail closed.
- Revocation: the child may return `child_policy_denied` at any time (the data owner vetoes); the parent side also has its own approval gate (dual defense lines).

## 5. Protocol v1 (local IPC)

**Transport**: `127.0.0.1` random port + NDJSON (one JSON-RPC 2.0 message per line), reused over a long-lived connection.
The first stage uses loopback; the protocol is decoupled from the transport, so it can later be replaced by POSIX UDS / Windows named pipes without changing messages.

**Methods**:

| Method | Direction | Description |
|---|---|---|
| `federation.handshake` | parent → child | Exchange protocol version and parent identity, request the tool subset → return a manifest summary + grant |
| `federation.tools.list` | parent → child | Currently available tools (filtered by grant) |
| `federation.tools.call` | parent → child | Call one child-core tool (the main method) |
| `federation.tools.cancel` | parent → child | Cancel an in-flight call (correlation ID) |
| `federation.health` | parent → child | Heartbeat / version / load (lightweight, does not trigger the model) |

**Call envelope**:

```json
{
  "jsonrpc": "2.0", "id": "c-42",
  "method": "federation.tools.call",
  "params": {
    "grant": "…",
    "tool": "doc.append_entry",
    "arguments": {"title": "ATM statement", "body": "…"},
    "subject": {"id": "user-42"},
    "idempotency_key": "atm-2026-11-09-0001",
    "deadline_ms": 5000,
    "budget_left": {"tokens": 8000, "tool_calls": 12},
    "trace_id": "0af7651916cd43dd8448eb211c80319c"
  }
}
```

**Response**: `{ok, result, evidence, receipts, usage}`; errors are uniformly `code/message/data`.
Key error codes:

| code | Meaning | Parent behavior |
|---|---|---|
| `child_unavailable` | Child core not running / disconnected | Degrade, do not retry write operations |
| `grant_invalid` | Grant expired / bad signature / mismatch | Stop routing and alert, then re-issue |
| `tool_not_granted` | Tool not in the grant subset | Do not retry |
| `child_policy_denied` | Child policy denial (including a human denial) | Return the denial to the caller as a visible event |
| `deadline_exceeded` | Timeout | Cancel and report back |
| `cancelled` | Cancelled by the parent | Clean up idempotently |
| `idempotency_conflict` | Same idempotency key but different arguments | Fail closed |

**Efficiency and backpressure**: the default per-connection concurrency limit is 8; a single message ≤ 256 KB (large results go through references/segmentation, v2);
the child side has a bounded queue and returns `busy` when it is full; on the parent side, exponential backoff applies to **read-only** calls only.

## 6. Governance inheritance

- **Tool set**: the parent registers only the subset in the grant; the model cannot see ungranted capabilities.
- **Subject**: `subject` is injected by the parent (not taken from model arguments); the child treats it as "who this call is on behalf of".
- **Budget**: the parent sends the remaining budget with each request; the child must not overspend; the tree-level budget is aggregated by the parent (children report usage).
- **Approval**: parent-side policy decides first (it may require human confirmation); the child side then decides (the data owner may refuse).
  Execution happens only when both sides pass; a refusal on either side fails closed.
- **Rate**: each edge is rate-limited independently; the child side defaults to 60 calls/minute and is configurable.

## 7. Audit and evidence

- The child returns `receipts` (an execution-receipt summary: tool, argument hash, result hash, timestamp, receipt-chain head).
- The parent merges the summary into the `evidence` of this tool call; `trace_id` runs through both layers, so the OTel side can stitch them into one trace.
- **No cross-core transactions are promised**: write operations rely on idempotency keys; partial success must be reported explicitly (`partial: true` + completed steps).

## 8. Failure and recovery

- Child core restart → the parent reconnects, re-handshakes, and re-pulls the manifest; old grants become invalid and must be re-issued.
- Parent core crash → the child is stateless (in-flight calls are cleaned up when the connection drops), and independent use is unaffected.
- Cancel propagation: parent cancel → `federation.tools.cancel` → the child cancels tool execution (a synchronous handler can only cut losses).
- Write operations are never replayed automatically; read-only calls can be retried safely with an idempotency key.

## 9. Security boundary (must be written into the docs)

- Precondition: parent and child cores run under the **same OS user**; the discovery file is 0600 and the port binds only to 127.0.0.1.
- What it defends against: other **users/processes** on the same machine hijacking the connection (token + file permissions). It **does not defend** against root, debuggers, or memory scraping.
- Log redaction: sensitive fields in grants / tokens / arguments are handled per the existing `sensitive` declarations.
- The child's independent policy always wins: a parent grant is not a pass to "bypass child approval".

## 10. Performance targets and verification

| Metric | Target | Verification |
|---|---|---|
| Direct in-process call | p50 < 0.1 ms | Unit benchmark |
| Local IPC (heartbeat / read-only tools) | p50 < 2 ms, p95 < 5 ms | Offline benchmark (loopback) |
| Disconnection recovery | reconnect and re-handshake in < 1 s | Integration test (kill the child process) |
| Write idempotency | replaying the same key produces no second write | Integration test |

## 11. Phasing and acceptance

| Phase | Content | Acceptance |
|---|---|---|
| Phase 1 | In-process `CoreNode` + `Federation` + depth/fan-out/cycle detection + grant validation | Unit tests + the offline demo `examples/host_m_federation` |
| Phase 2 | Local IPC: `ChildServer` + `ChildLink` + discovery file + two-process e2e | e2e (docs-product simulation ↔ ATM simulation), latency benchmarks met |
| Phase 3 | Ecosystem operations: manifest hot updates, per-edge quota/audit panel, desktop dynamic-island integration | 2 installable child-core examples |
| Phase 4 | Cross-machine (optional): TLS + reuse of the FastAPI sidecar protocol | As needed |

## 12. Non-goals

- No peer-to-peer Multi-Agent negotiation / auto-discovery / auto-trust.
- No MCP Server, no kernel-level orchestration, no new SPI.
- No cross-core transaction rollback or distributed consistency.
- No public-internet exposure (Phase 4 is a separate discussion, and it must go through the existing auth boundary).

## 13. Open review items (defaults are given and can be changed)

1. **Transport MVP**: loopback by default (cross-platform, debuggable); UDS/named pipes are a Phase 2 optimization item.
2. **Sibling edges**: forbidden by default, sibling cooperation goes through a common parent; allowing them requires explicit configuration plus cycle detection.
3. **Approval ownership**: dual defense lines by default (parent decides first + child decides again); a refusal on either side fails closed.
