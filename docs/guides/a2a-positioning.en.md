> English translation of [中文](a2a-positioning.md) · The Chinese version is canonical; if they disagree, the Chinese wins.

# A2A 1.0 and YAI's positioning

**English** · [简体中文](a2a-positioning.md)

> Bottom line: **A2A is not a kernel feature, and YAI does not implement Multi-Agent inside
> `src/yai_core/`.** Directed federation (ADR 0008) is hierarchical composition — a parent product
> calling its own child product's capabilities — not A2A peer-to-peer collaboration. If a host needs
> A2A, add an adapter at the **host layer** that exposes a remote A2A agent as a tool; the kernel
> stays unaware.

## 1. What A2A 1.0 is

[A2A (Agent-to-Agent) 1.0](https://a2a-protocol.org/latest/announcing-1.0/) is an open protocol under
the Linux Foundation for **agent-to-agent** discovery, task delegation and result exchange. Its core
concepts are the Agent Card (capability description), Task (long-running lifecycle) and
messages/artifacts. It sits at a different layer from MCP (agent↔tools/data) and AG-UI (agent↔UI):

| Layer | Protocol | Problem it solves |
|---|---|---|
| Agent ↔ tools/data | **MCP** | Turn external capabilities into callable tools |
| Agent ↔ user interface | **AG-UI** | Turn the event stream into standard frontend events |
| Agent ↔ agent | **A2A** | Cross-agent task delegation and collaboration |

## 2. Where YAI stands, and its red line

YAI is an **in-process embedded kernel**: a host `import`s it, declares its capabilities, and the
kernel handles routing, the loop, tool execution and governance. It is not another agent platform, so:

- **No Multi-Agent orchestration**: no peer discovery, voting, group chat, or A2A server;
- **No A2A implementation**: no Agent Card, Task lifecycle, or remote-agent registry in the kernel;
- **Directed federation** (`shell/federation/`) is an exception at a different layer: a parent calls
  its **own child product's** capabilities, directed, non-reversible, with no peer discovery; see the
  [federation guide](federation.en.md) and [ADR 0008](../adr/0008-directed-federation.en.md).

## 3. Directed federation ≠ A2A

| Dimension | Directed federation (YAI) | A2A 1.0 |
|---|---|---|
| Topology | Parent → child, one-way; child cannot call back | Peer agents may delegate both ways |
| Discovery | Parent explicitly configures/starts children | Agent Card / registry discovery |
| Identity | Parent/child product boundary, grants, manifest | Cross-org agent identity and auth |
| Goal | One product composing another product's capabilities | Cross-team / cross-vendor task collaboration |
| Failure semantics | Parent owns child lifecycle and quotas | Task state machine, remote retry/cancel |

Conflating them causes two mistakes: stuffing peer orchestration into the kernel, or claiming
parent/child federation is A2A-compatible.

## 4. If a host needs A2A: put it in the host

The recommended shape is a **host adapter**, not a kernel change:

```
remote A2A agent  ──A2A client──▶  host adapter  ──ToolSpec──▶  YAI AgentCore
```

- The adapter owns Agent Card parsing, auth, task creation/polling/cancellation, timeouts and retries;
- It exposes one ordinary tool to the kernel (e.g. `a2a_delegate(agent, task)`) returning text or
  structured results;
- Permissions, approvals, audit and budgets remain governed by the YAI kernel;
- A failed sub-agent becomes a failed tool result, following the same `tool_result` and reflection
  path as any other tool.

This keeps A2A's interoperability value without breaking the "kernel does not do Multi-Agent" red line.

## 5. Decision and revisit conditions

**Current decision**: A2A does not enter the kernel; this document records the boundary; no official
A2A server/client dependency is shipped.

**Revisit only if**:

1. A real host needs kernel-level A2A discovery/orchestration and the host layer cannot satisfy it;
2. Cross-process/cross-org agent identity must become a first-class kernel concept, not a tool arg;
3. A2A's Task semantics irreconcilably conflict with YAI's event stream/budget/approval model, which
   would warrant a dedicated ADR.

Until then, the right place for A2A is a **host integration example**, not `src/yai_core/`.

## 6. References

- [A2A 1.0 announcement](https://a2a-protocol.org/latest/announcing-1.0/)
- [Linux Foundation: A2A surpasses 150 organizations](https://www.linuxfoundation.org/press/a2a-protocol-surpasses-150-organizations-lands-in-major-cloud-platforms-and-sees-enterprise-production-use-in-first-year)
- Repo: [federation guide](federation.en.md) · [ADR 0008](../adr/0008-directed-federation.en.md) ·
  [AG-UI adapter](ag-ui.en.md) · [MCP Registry](mcp-registry.en.md)
