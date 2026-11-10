> English translation of [中文](0008-directed-federation.md) · The Chinese version is canonical; if they disagree, the Chinese wins.

# ADR 0008: Boundaries of Directed Federation

**English** · [简体中文](0008-directed-federation.md)

- Status: **Accepted (2026-11-10)** — Phase 1 (in-process), Phase 2 (local IPC) and Phase 3 (ecosystem ops) are implemented; the decision text is unchanged, see the addendum at the end.
- Related: `docs/guides/federation.md` (protocol design draft), `shell/employee/` (existing hierarchical
  delegation skeleton), `src/yai_core/sidecar/` (JSON-RPC and discovery file shapes),
  `src/yai_core/integrations/` (manifest / MCP / OpenAPI), `docs/architecture.md`
  (concurrency and multi-instance contracts), `docs/IMPLEMENTATION_STATUS.md`
- Related requirements: product ecosystem — n primary products + m secondary products, each embedding a
  core and usable independently; a parent core has the right to call child cores, child cores cannot
  exceed their authority in the reverse direction; communication between parent and child cores must be
  lightweight, accurate and efficient.

## Background

- The products themselves have no agent capability, and users use them normally; after embedding a core,
  each product gains an **independent AgentCore**: its own tools, policy, memory and budget, with no
  dependency on the others. This is consistent with the kernel's existing "multi-instance / multi-specialist"
  contract.
- What the ecosystem needs is "the parent product gains an additional slice of the child product's
  capability" (for example: an ATM system calls the core inside the document product and writes business
  records into documents), not "all capabilities converge into one super Agent".
- The current repository provides three reusable pieces: the hierarchical delegation in `shell/employee`
  (monotonically narrowing permissions, budget, concurrency gate), export/import of the capability
  inventory manifest, and the sidecar's JSON-RPC shape plus the Bridge's "discovery file + token" pattern.
- Repository red line: the kernel **does not do Multi-Agent orchestration** and does not do MCP Server.
  Federation must therefore live at the host layer, while the kernel continues to run only a single agent
  loop.

## Decision

**Adopt "host-layer directed federation": cores form a DAG, and the edges carry only parent → child
call rights.**

1. **Location**: implemented under `shell/federation/` (host layer, optional module), with zero kernel
   changes and `dependencies = []` unchanged; no new SPI, no new kernel event types (child core lifecycle
   is expressed by host-layer events plus receipt evidence).
2. **Topology**: directed acyclic; a core may be both a parent and a child; **sibling direct connections
   are forbidden by default**, and sibling collaboration is routed through the shared parent core; all
   edges are configured explicitly by the host, with no automatic discovery and no automatic trust.
3. **Directionality is guaranteed by missing credentials**: the child core process has no endpoint / token
   for the parent core, so "child calls parent" is not rejected by policy but is simply **no path
   available**; the `child_grant` held by the parent core grants only "calling the tool subset declared by
   the child core".
4. **Capability exchange**: a child core publishes a capability manifest (existing format); the parent core
   explicitly selects a subset and registers it as tools, and does not expose the child core's entire
   capability set to the model by default.
5. **Communication protocol v1**: JSON-RPC 2.0 over NDJSON on local loopback (`127.0.0.1` random port) +
   discovery file (0600, containing pid / port / token / protocol version) + connection-level token; the
   protocol message shapes align with the sidecar. Replacing the transport layer with POSIX UDS / Windows
   named pipes is reserved (the protocol stays unchanged).
6. **Governance**: a `grant` (HMAC) binds "edge + child core identity + tool subset + subject + expiry
   time"; the parent core applies its own approval policy, and the child core judges again with its own
   policy (dual defense lines, fail-closed by default); budgets are aggregated and issued down the tree;
   write operations must carry an idempotency key.
7. **Audit continuity**: a child core returns a summary of its own execution receipt, and the parent core
   merges it into its own evidence and events; **cross-core transaction rollback is not promised**
   (cross-core consistency relies on idempotency + retry semantics).
8. **Hard limits**: maximum depth 3 by default, per-parent fan-out 16 by default, cycle detection, timeout
   and cancellation propagated downstream, and `child_unavailable` returned with degradation when a child
   core is unavailable (the parent core does not crash and does not blindly retry write operations).

## Alternatives and Trade-offs

| Option | Conclusion | Rationale |
|---|---|---|
| A. Kernel-level Multi-Agent orchestration | Rejected | Violates the repository red line; would push scheduling, negotiation and failure semantics into the kernel, making it bloated and uncontrollable |
| B. Service-ize every core as MCP/HTTP and interconnect in a mesh | Long-term, only as cross-machine transport | Requires an MCP Server (red line); cycles and permissions in a mesh topology are uncontrollable; local overhead is also high |
| C. Reuse and extend `shell/employee` into a resident child core | **Adopted** | The governance skeleton already exists; only needs upgrading from "short-lived task level" to "resident, reusable + communication layer" |
| D. Directed DAG + local IPC (this ADR) | **Adopted** | Directionality can be structurally guaranteed; lightweight; consistent with the existing sidecar/Bridge shapes |

## Addenda

- 2026-11-09: **Phase 1 (in-process) implemented** — `shell/federation/` (`CoreGrant` / `ChildCore` /
  `Federation`), `tests/test_federation.py` (12 cases), `examples/host_m_federation/`. Local IPC (Phase 2)
  and HMAC-signed grants are still not implemented; the decision in the body is unchanged.
- 2026-11-10: **Phase 2 (local IPC) implemented** — `ChildServer` / `LinkChild`, discovery file (0600) +
  token, HMAC grant, `health` / `tools.list` / `tools.call`, a two-process demo and 7 IPC regression cases;
  manifest hot reload, per-edge quotas, cross-core budget aggregation and cross-core transactions are still
  not implemented (starting from Phase 3).
- 2026-11-10: **Phase 3 (ecosystem operations) implemented** — `Federation.refresh_child()` manifest hot
  reload, `ChildServer(edge_quota_per_minute=...)` per-edge rate limiting,
  `register_child(max_calls=...)` + `edge_stats()`, `ManagedChild` child process supervision; 8 new
  regression cases. Token-level budget aggregation, cross-core transactions and guardian-style automatic
  restarts are still not implemented.

## Consequences

- New host-layer modules and protocol documentation; the kernel is still single-agent with zero third-party
  dependencies.
- Security precondition: parent and child cores run under the **same OS user**; token + file permissions
  only guard against other processes on the same machine, and **no claim is made of protection against
  root / debuggers / memory scraping**.
- The ecosystem can evolve independently: when primary/secondary products upgrade their kernel versions,
  the grants and manifest subsets on the edges can be negotiated in a versioned way.
- Documentation must always distinguish the two forms: **independently usable** (the child core runs on its
  own) and **called by the parent core** (capabilities are borrowed); "the parent core demo succeeded" must
  not be substituted for "the child core passed independent acceptance".
