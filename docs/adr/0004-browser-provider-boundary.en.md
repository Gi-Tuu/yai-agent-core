> English translation of [中文](0004-browser-provider-boundary.md) · The Chinese version is canonical; if they disagree, the Chinese wins.

# ADR 0004: Boundary between the Browser Provider and the browser extension

**English** · [简体中文](0004-browser-provider-boundary.md)

- Status: Adopted (v0.26.0, released 2026-11-03)
- Date: 2026-11-03
- Related: `src/yai_core/integrations/browser/`, `src/yai_core/sidecar/native_bridge.py`,
  `extension/`, `docs/guides/browser-extension-bridge.md`

## Context

v0.26 must let YAI actually operate a browser and verify results, while avoiding becoming "a second browser product" or a
Windows-only Agent. One question must be answered first: does browser capability belong to Core, Integration, Battery, or Host?

## Decision

1. **Core does not import any browser library**, and `dependencies` stays empty.
2. Browser execution capability lands in the **Integration layer as an optional adapter** `yai_core.integrations.browser`:
   - `BrowserProvider` depends only on a single `BrowserBackend` Protocol;
   - the real backend `playwright_backend.py` goes through the optional `[browser]` extra;
   - the Provider only produces `ToolSpec`s; once registered into `ToolRegistry`, `ToolExecutor` uniformly performs
     parameter validation, permission policy, approval, receipts, and events — **no separate Tool Bus, no separate permission system**.
3. Element references carry a **snapshot version** (`s<N>:<i>`); after the page changes, old references all expire and prompt for re-observation;
   an action returns only `executed + outcome` and does not claim task success; the task-level conclusion is expressed separately by the
   Outcome Verification contract (`VerificationResult`).
4. The browser extension is a **Host**, not the Agent Core:
   - `extension/` only does the side panel UI and connection management; it does not run the Agent loop;
   - it connects to the local Sidecar through the **Native Messaging Bridge** (`native_bridge.py`);
   - it does not modify the public Sidecar protocol (still stdio NDJSON + JSON-RPC 2.0);
   - the browser side stores no model credentials; `initialize` parameters come only from the local Bridge configuration.
5. Extension operation allowlist + origin validation fail closed; outbound oversized frames are truncated; after a disconnect results are marked `unknown`,
   and operations that may already have side effects are not replayed automatically.

## Consequences

- Users who do not install `[browser]` are completely unaffected; browser/extension capability is not a kernel requirement.
- Real Playwright behavior and the browser extension lifecycle still need on-device acceptance; the code and docs explicitly mark
  "implemented · not verified on real devices", and offline tests are not passed off as real-device success.
- If a Windows Desktop Provider is added later, it reuses the same `ToolExecutor` + receipts +
  Outcome Verification contract, without adding a second governance chain for the platform.
- Addendum (2026-11-05): the real Chromium smoke and the controlled-browser E2E are complete
  (`docs/evaluations/browser-task-e2e.md`); the extension on-device acceptance record is in
  `docs/guides/browser-extension-bridge.md`; real-site diversity, nested frames, download/upload, and
  installer distribution remain unverified.
