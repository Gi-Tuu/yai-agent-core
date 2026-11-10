> English translation of [中文](execution-audit-persistence.md) · The Chinese version is canonical; if they disagree, the Chinese wins.

# The Persistence Boundary of Execution Auditing (host integration guide)

**English** · [简体中文](execution-audit-persistence.md)

> Corresponds to the v0.25 B7 batch (task sheet P1-3). YAI provides a **reliable in-process audit contract**;
> persistent storage is the host's responsibility. This document spells out the boundary between the two and the correct
> posture on the host side.

## 1. What the in-process hash chain can and cannot guarantee

`ExecutionAudit` seals one `ExecutionReceipt` per execution (status / call_id /
idempotency_key / error / duration / replayed / postcondition) and links them into a hash chain using the previous
receipt's `receipt_hash`. After the host exports the events, `verify_receipt_chain(receipts)`
is enough to detect any tampering, removal, or reordering.

**Can guarantee**: while the process is alive, any modification to any entry in the chain will be caught by verification.

**Cannot guarantee**:
- The chain is still in memory after a process crash / restart — **exporting is the host's responsibility**;
- Receipts outside the chain (the `partial_events` carried by an exception are lost if the host does not consume them);
- "the first entry has an empty prev_hash" is the starting point of the chain — **a process restart opens a new chain**,
  and linking chains across processes is handled by the host on the persistence side.

## 2. The correct way for the host to persist

1. **Export in real time**: while consuming the event stream, write each `receipt` field into your own audit storage
   (database / append-only log); do not wait until the task ends and then collect it all at once;
2. **unknown after restart**: for executions interrupted by a restart, the side-effect state is unknown —
   the host should mark such entries as `unknown`, and rely on the tool's own idempotency key to replay or on manual verification;
   **do not** assume "restart = never happened";
3. **Preventing duplicate side effects**: for calls that declare an idempotency key, replay is handled by the kernel cache (only the same key with the
   same parameters hits, and tightened permissions still reject); cross-process idempotent deduplication must be done by the host in its own storage;
4. **Postcondition check failure**: a check that raises only stays `unknown` (verdict=error); the kernel
   never guesses on the host's behalf "whether it actually happened" — the host should treat an errored postcondition as a manual ticket.

## 3. Sensitive-information boundary

- The receipt **does not contain** parameters or result bodies: `error` is governed and redacted through `event_error` before sealing;
- The `arguments` in the event stream are an **independent redacted copy** (per the `governance.sensitive`
  declaration; if it is not declared, there is no redaction — this is the contract, not a defect);
- When the host writes events to a log, it should run its own log redaction again over the `sensitive` paths it declares;
  the division of responsibility for belt-and-braces protection: the kernel owns the event shape, the host owns its own storage.

## 4. Correlation relationships

`call_id` (the model's tool-call id), the composite step id (`call_id:step-index`), and `run_id`
(sidecar) are the three existing clues. The session ID belongs to the host: when the host persists, it only needs to write its own session ID
into the database together with `run_id`/`call_id` to establish the correlation; the kernel does not need to know the session concept.

## References

- `docs/walkthrough/52-execution-receipts.md` (a line-by-line walkthrough of receipts)
- `docs/adr/0002-runtime-auth-boundary.md` (identity boundary)
- `tests/test_execution_receipts.py` (test evidence for chain verification and redaction)
