> English translation of [中文](README.md) · The Chinese version is canonical; if they disagree, the Chinese wins.

# Architecture Decision Records (ADR)

**English** · [简体中文](README.md)

Point-in-time decisions that record "why it is designed this way"; numbering starts at 0002 (0001 is unused).
Update this table when adding an ADR; changes to the verification status of a decision body are appended as an **addendum**, without rewriting the body.

| No. | Title | Status | In one sentence |
|---|---|---|---|
| [0002](0002-runtime-auth-boundary.md) | Runtime's authentication and authorization boundary (sidecar + FastAPI Battery) | Accepted (2026-10-31, v0.25 B3) | The authentication boundary for the session surface and the tool surface; public deployment must authenticate explicitly |
| [0003](0003-acc-boundary.md) | Boundary of responsibility between ACC and native governance | Accepted (2026-10-31, v0.25 B8) | ACC is demoted to an interoperability shim; only one copy of the determination primitives remains, in the kernel |
| [0004](0004-browser-provider-boundary.md) | Boundary between the Browser Provider and the browser extension | Adopted (v0.26.0, released 2026-11-03) | Browser execution goes through an Integration-layer adapter; the extension is only a Host |
| [0005](0005-outcome-verification.md) | Boundary between execution receipts and task acceptance | Adopted (v0.26.0, released 2026-11-03) | Tool-call facts (receipts) and goal-achievement facts (acceptance) are separated; the verifier is provided by the host |
| [0006](0006-browser-execution-unification.md) | The two execution paths for browser capability and their unified boundary | Accepted (2026-11-03, v0.27 B3) | The extension's read-only snapshot and controlled operation coexist, with a clear boundary and no claim of unification |
| [0007](0007-local-secret-storage.md) | Storage boundary for local model keys and the SecretStore plan | Proposed (2026-11-03, v0.27 B4) | A governance plan for the plaintext risk of bridge.json; **encryption is not yet implemented**, it is still written to disk in plaintext |
| [0008](0008-directed-federation.md) | Boundary of directed parent-child cores (Directed Federation) | **Proposed (2026-11-09, design draft)** | A parent core can call a child core, but a child core has no way back; federation is implemented at the host layer, and the kernel stays a single agent |
