> English translation of [中文](browser-task-e2e.md) · The Chinese version is canonical; if they disagree, the Chinese wins.

# Browser Provider Real-Task E2E (v0.27 Batch 3)

**English** · [简体中文](browser-task-e2e.md)

> Script: `scripts/browser_provider_task_smoke.py`
> Environment: real Playwright Chromium (headless) + a local real HTTP page; a **controlled browser**,
> not the user's current tab.

## Chain

1. Start a local HTTP page (form: name input + submit button + result area);
2. `BrowserProvider` + `BrowserPolicy.secure([origin])`, tools registered into the `ToolRegistry`;
3. `ToolExecutor` executes: `browser_open` → `browser_observe` → `browser_type` →
   re-observe (type generates a new snapshot) → `browser_click` → `browser_observe`;
4. Every mutating action goes through `Channel.confirm` approval (this script uses an auto-approving channel
   and records the number of approvals);
5. Verify "完成：YAI" (Done: YAI) from the page's real text, and write a `VerificationResult`;
6. `verify_receipt_chain` validates the execution receipt chain of this run.

## Measured on 2026-11-03

```json
{
  "approvals": 3,
  "page_text": "YAI Browser E2E\n名字 提交\n完成：YAI",
  "receipt_chain_ok": true,
  "verification": {
    "status": "verified_success",
    "confidence": 0.9,
    "evidence": [{"kind": "page_text", "summary": "页面真实文本包含完成结果", "ok": true}]
  },
  "ok": true
}
```

- The 3 approvals correspond to open / type / click (mutating actions, required by default by `BrowserPolicy.secure`);
- Stale references are handled by snapshot versions: after type, re-observe before clicking — no blind operation on an old ref;
- The receipt chain is complete; final acceptance comes from the page's real text, not from the model's own account.

## Boundary with the "current tab" (ADR 0006)

- This E2E operates the Browser Provider's own controlled browser, **not the user's current default Chrome
  tab**; the extension's `activeTab + scripting` is still only responsible for reading a read-only snapshot of the current page.
- "Authorize the current tab → write to it directly" depends on the Extension Backend / CDP reuse approach
  (A/B in ADR 0006) and is not implemented yet; before it is implemented, this E2E or "reading the current page"
  must not be advertised as "can operate the current page".


## Current-tab write operations (v0.27 addendum, opt-in)

Within the C boundary of ADR 0006, one **restricted** path is added, off by default:

- With `initialize.current_tab_tool=true`, the Sidecar registers the `current_tab_action` tool;
- The tool accepts only `action=click|type` + a CSS `selector` + `value`; every action goes through
  `ToolExecutor` approval; it does not execute arbitrary JS and does not read unauthorized pages;
- The extension panel runs `chrome.scripting` only against the `authorizedTabId` bound when the user clicked
  "读取当前页面" (read current page), and does not follow later focus drift; without activeTab authorization it fails closed;
- Protocol-level regression: `tests/test_sidecar_protocol.py` covers the full chain of "not registered by default /
  approval + reverse tab_action/request + tool result once explicitly enabled".

## Nightly real-machine regression (since v0.34, non-blocking)

The `browser-real` job of `.github/workflows/nightly-real.yml` runs every day at 03:17 UTC on a real Chromium:

- `scripts/browser_smoke.py --json`: navigate / observe / type / select / click / wait / re-observe + origin gate + stale reference expiry;
- `scripts/browser_provider_task_smoke.py`: controlled-browser real-task E2E (3 approvals, receipt chain);
- Reports and screenshots are written to the artifact (`artifacts/` + `docs/exports/`). **A failure turns the job red** — that is a real signal.

Local re-verification on 2026-11-10: browser smoke `11 receipts`, screenshot `docs/exports/browser-smoke.png`, all green.

**Measured on a GitHub-hosted runner (2026-11-10, run 42782702561)**: `browser-smoke.json` 11 receipts,
`browser-task-e2e.json` `ok: true`, and the screenshot artifact was uploaded as well — this chain is genuinely
usable on a hosted ubuntu runner.

**Real-browser current-tab E2E limitation**: `activeTab` authorization must come from a user gesture; current
Playwright automation opens the side panel as an ordinary tab and cannot simulate a real Side Panel user click;
therefore this environment can only verify the protocol chain and the extension's static behaviour, and true
end-to-end verification of clicking the real side panel still requires manual acceptance per
`docs/guides/browser-extension-bridge.md`. Before manual acceptance, it must not be claimed that "current-tab
write operations have passed on a real machine".
