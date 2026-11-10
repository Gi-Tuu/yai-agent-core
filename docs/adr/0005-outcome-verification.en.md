> English translation of [中文](0005-outcome-verification.md) · The Chinese version is canonical; if they disagree, the Chinese wins.

# ADR 0005: Boundary between execution receipts and task acceptance

**English** · [简体中文](0005-outcome-verification.md)

- Status: Adopted (v0.26.0, released 2026-11-03)
- Date: 2026-11-03
- Related: `src/yai_core/verification.py`, `src/yai_core/types.py` (`Evidence` /
  `VerificationResult` / `RunResult.verification`), `docs/IMPLEMENTATION_STATUS.md`

## Context

An Agent calling a tool, or a model saying "done", does not mean the user's goal was achieved: clicking submit ≠ submission succeeded,
writing a file ≠ the artifact is valid, a download button returning ≠ the download succeeded. At the same time, the execution layer already has a set of
`ExecutionReceipt`s recording **what happened to each tool call** (not_started / succeeded /
failed / unknown / partial). Reusing a receipt's `succeeded` to pass it off as "task complete" would be
manufacturing false certainty.

## Decision

1. **Keep the two sets of facts separate**:
   - `ExecutionReceipt`: the facts of a tool call (whether it executed, whether it timed out, whether the side effect is unknown,
     whether postconditions were confirmed);
   - `VerificationResult`: the facts of the task goal (`verified_success` / `verified_failure` /
     `partial` / `unknown` / `not_verifiable` + confidence + evidence).
2. **The verifier is provided by the host** (`OutcomeVerifier` contract, async `verify(goal, evidence, metadata)`).
   The kernel has no built-in domain rules and no built-in visual judgment; `CompositeVerifier` only performs deterministic merging
   (failure first, all success, partial, unknown, not decidable).
3. **Evidence prefers deterministic sources first**: execution receipts, the `evidence` a tool explicitly reports (browser page metadata,
   desktop window/control fingerprints, etc.), API/file/DOM state that the host itself queried; a vision model can only be an
   optional supplement, and the contract does not depend on it.
4. **Bypass semantics**: verification happens before `DONE`, but a failure/exception **does not change** the final text, policy, or
   event-stream structure; it only degrades to `not_verifiable` and emits a `verification_failed` diagnostic;
   **never automatically retry a tool because verification failed** (possible duplicate side effects).
5. **Not configured = None**: with no verifier configured, `RunResult.verification is None`, preserving the old behavior,
   and not pretending verification happened.

## Consequences

- A host that wants to "prove the task is complete" must provide checkable evidence or a verifier itself; YAI provides the contract, evidence
  collection, and the combiner, and does not invent business acceptance criteria on the host's behalf.
- Action results from browser/desktop Providers carry `outcome` and `evidence` and naturally become verification input;
  both still only report "observable changes" and do not overstep by claiming success.
- If visual acceptance is needed later, it plugs in as one implementation of a host verifier, not into the kernel.
