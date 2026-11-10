> English translation of [中文](0003-acc-boundary.md) · The Chinese version is canonical; if they disagree, the Chinese wins.

# ADR 0003 · Boundary of responsibility between ACC and native governance

**English** · [简体中文](0003-acc-boundary.md)

- Status: Accepted (2026-10-31, v0.25 B8 batch)
- Related: the three steps of Route 2 on 10-16 (native approval promoted, ACC demoted to translator), v0.25 task book 6.3

## Context

ACC (Agent Capability Contract) once historically carried the determination primitive for parameter-level approval.
After the three steps of Route 2 (PR #63/#64/#65, 2026-10-16), the determination primitives are all native: `policy/approval.py` carries the approval determination, `ToolGovernance` carries the data, and the ACC layer is left with translation only.

## Inventory of current responsibilities (measured 2026-10-31)

| Responsibility | Owner |
|---|---|
| Approval determination primitive (nine operators, strict JSON comparison, fail-closed) | **YAI native** (`policy/approval.py`; the ACC side is an isomorphic alias) |
| Risk/scope enumeration | **YAI native** (`ToolGovernance.risk/scope`, landing in PERMISSION_ASKED/TOOL_CALL) |
| Resource scope gate | **YAI native** (`filesystem_gate` + `paths.py` single source of truth) |
| Timeout/rate enforcement | **YAI native** (`execution.timeout_ms`/`rate_limit` translated into governance data) |
| OpenAPI `x-agent-capability` declaration parsing and mapping | **ACC translator** (`integrations/acc/`; if the declaration is unavailable the whole operation is skipped) |
| 26 official vector consistency tests | **Test asset of the ACC translator** (already excluded from the sdist distribution; if the files are missing the whole module is skipped) |

## Decision

1. **Keep maintaining ACC as an optional OpenAPI interoperability translator**, do not delete it:
   - it has been part of the public manifest format since v0.10.0 (the `governance.acc` field); deleting it is a breaking change;
   - the 26 Apache-2.0 official vectors are the only external consistency evidence;
   - the kernel runtime has **zero dependency** on ACC (verified by Route 2 step 3: a subprocess importing the kernel does not
     bring in `yai_core.integrations`).
2. **Maintain only one copy of the determination logic**: ACC's `AccApprovalCondition` is an alias of `ApprovalRule`,
   parity between the two paths is pinned by parameterized tests; new operators go only into the native layer.
3. **Profile wording**: claim only the OpenAPI Binding Parser Profile; the per-subject, resource-level authorization required
   by the Runtime Profile is already covered by the native filesystem_gate, so do not claim it again.

## Consequences

- New hosts gain the full governance capability without knowing about ACC (native, data-driven);
- Hosts that need ACC interoperability get governance of the same strength simply by loading the declarations (it can only get stricter);
- The cost of deleting ACC (breaking manifest compatibility + losing external consistency evidence) exceeds the maintenance cost,
   and the maintenance surface has already converged to two file groups: "the translator + the vector tests".
