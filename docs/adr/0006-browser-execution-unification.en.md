> English translation of [中文](0006-browser-execution-unification.md) · The Chinese version is canonical; if they disagree, the Chinese wins.

# ADR 0006: The two execution paths for browser capability and their unified boundary

**English** · [简体中文](0006-browser-execution-unification.md)

- Status: Accepted (2026-11-03, v0.27 Batch 3)
- Related: `src/yai_core/integrations/browser/`, `extension/`, `src/yai_core/sidecar/native_bridge.py`,
  `docs/guides/browser-extension-bridge.md`, `docs/IMPLEMENTATION_STATUS.md`
- Related task book: v0.27 §5 (unifying the execution paths of the Browser Extension and the Browser Provider)

## Context

Two browser capability paths exist in the repository at the same time:

1. **Browser Provider (governed path)**: `BrowserProvider` + `BrowserBackend` +
   `PlaywrightBrowserBackend` + `BrowserPolicy`; tools are registered into `ToolRegistry` and uniformly go through
   `ToolExecutor` for parameter validation, permissions, approval, receipts, and events; snapshot references, origin gating, password-field rejection, and
   redirect aborts at the Playwright routing layer all live in this layer.
2. **Browser extension path**: MV3 Side Panel + `chrome.scripting` reading the current active tab +
   Native Messaging Bridge + Sidecar + Playwright MCP (optional `--cdp-endpoint`).

The **actual capabilities of the two paths are not the same**:

- The extension's `activeTab` + `chrome.scripting` can only **read** the current tab after the user clicks, and what it gets is a
  **read-only, untrusted page snapshot**; it has no DOM write permission, and it will not click the current tab on the user's behalf.
- Actual browser automation is performed by Playwright MCP: by default it launches its own browser, or it connects via CDP to a Chrome with an
  **independent profile**; this is not the same as taking over the user's current default browser session.
- MCP's `browser_browser_*` tools are generic Playwright tools and **do not automatically inherit** `BrowserPolicy`'s
  origin/password-field/approval semantics; what can really guarantee `BrowserPolicy` is the Browser Provider path.

Therefore "read the current page" cannot be derived as "the Agent can operate the current page", nor can the two tool sets be opened at once and then
claim that unification has been achieved.

## Decision

**Adopt C (coexistence + a clear boundary), while keeping the interfaces to evolve toward A/B.**

- **C as it stands**:
  - The extension side panel promises only this: read a **read-only snapshot** of the current tab and attach it to the task as untrusted context;
    the panel must explicitly state "read-only, will not operate the current tab".
  - Controlled browser operations go only through one of the two explicit backends:
    - Browser Provider (governed by `BrowserPolicy`; recommended for production);
    - Playwright MCP (explicitly configured by the host; in production it must use the `ToolExecutor` allowlist/approval and
      host-side process isolation, and it must not be assumed to inherit `BrowserPolicy`).
  - The two do not share tool names and do not fall back to each other; the panel shows whether a "controlled browser" is currently connected.
- **Evolution directions**:
  - A (Browser Provider becomes the only execution entry point, and the extension implements `BrowserBackend`): this requires
    an authorization model for `chrome.debugger` / `scripting` write operations plus per-action approval, which is a large amount of work;
    until it is done, do not claim "the extension can already operate the current tab".
  - B (keep MCP but connect it through a unified browser policy adapter): this requires mapping generic MCP tool parameters onto
    `BrowserPolicy` (origin, sensitive fields, approval); the current MCP schema is not sufficient to implement this reliably,
    so it is not adopted for now.
- **Decision gate**: before either A or B lands, a permission-consistency test must first be provided (the same action yields the same DENY/approval/origin
   conclusion on both paths) plus a real-tab E2E; otherwise stay with C.

## Consequences

- Users must be able to see three states: `current tab (read-only snapshot)`, `controlled browser (Browser Provider)`,
  `controlled browser (MCP / CDP)`; when not connected, the panel must explicitly say "no controlled browser connected".
- Docs/README/IMPLEMENTATION_STATUS must distinguish "read" from "operate", and must not use a successful real-device page read
  as a substitute for operation acceptance.
- Production recommendation: use Browser Provider + `BrowserPolicy.secure()` for governed operations; treat MCP only as an
  optional execution backend explicitly configured by the host, with the tool allowlist/approval tightened at the host layer.
- No second browser protocol is added; the Bridge only forwards Sidecar/MCP events and does not execute page actions directly.

## Rejected options

- **A implemented immediately**: the extension's write-operation permissions, per-action approval, and the semantic differences of CDP/activeTab are unresolved,
  which would create the illusion of "seemingly unified, yet with different permissions".
- **B implemented immediately**: the MCP tool schema does not match BrowserPolicy semantics, so origin/password-field
  consistency cannot be guaranteed; force-wrapping it would only make approval moot.
- **Opening both tool sets at once and calling it unification**: explicitly forbidden; this is exactly the risk this ADR is meant to eliminate.

## Acceptance

- The panel visibly distinguishes "read-only snapshot" from "controlled browser" (static tests pin down the DOM/copy).
- `docs/guides/browser-extension-bridge.md` explicitly states "reading the current tab ≠ operating the current tab".
- Any later A/B implementation must include permission-consistency tests and a real E2E; until then, the C boundary of this ADR stands.
