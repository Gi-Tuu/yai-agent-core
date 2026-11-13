> English translation of [中文](sidepanel-manual-acceptance.md) · The Chinese version is canonical; if they disagree, the Chinese wins.

# YAI Real Side Panel Manual Acceptance Checklist (v0.27)

**English** · [简体中文](sidepanel-manual-acceptance.md)

> Automation cannot grant `activeTab` (a real user gesture is required), so the following steps must be completed
> manually in a **real Chrome/Edge side panel**; screenshots / event streams / logs serve as evidence — do not look
> only at the final answer.

## Prerequisites

- [ ] Extension reloaded (a Reload is mandatory after manifest changes)
- [ ] The **Side Panel** is opened via the toolbar icon, not the `chrome-extension://.../sidepanel.html` tab
- [ ] Bridge status "协议握手完成" (protocol handshake complete); model/MCP status as expected
- [ ] Ordinary http/https test pages, with no passwords / payments / real account operations
- [ ] If testing current-tab write operations: `initialize.current_tab_tool=true` in `bridge.json`, and the Sidecar restarted

## A. Current tab, read-only

| # | Operation | Expected |
|---|---|---|
| A1 | Open the test page and click "读取当前页面" | A site permission prompt appears the first time; after allowing, the title/URL/body preview is shown |
| A2 | Deny the site permission | An explicit "拒绝读取" (read denied) message; page content does not enter the task |
| A3 | Click "运行" (Run) with the snapshot attached | The panel shows "只读快照" (read-only snapshot); the final answer may cite the body text |
| A4 | Switch to a `chrome://` internal page and then read | Explicit refusal; no injection is attempted |

## B. Controlled browser / CDP

| # | Operation | Expected |
|---|---|---|
| B1 | After configuring CDP 9222, run "打开页面并读取标题" (open the page and read the title) | browser_* tools work normally; the final title is correct |
| B2 | Close the 9222 Chrome, then restart the side panel | The launcher should start it automatically; on failure it falls back to the MCP-managed browser (visible in logs) |
| B3 | The page redirects to an unauthorized origin | Blocked before load; unauthorized content does not enter the context |

## C. Current-tab write operations (opt-in)

| # | Operation | Expected |
|---|---|---|
| C1 | After authorizing page reading, run "用 current_tab_action 输入并点击" (type and click via current_tab_action) | An approval card appears before every click/type; if denied, nothing is executed |
| C2 | Allow the approval | The page's real DOM changes; re-observing shows the result |
| C3 | Deny the approval | The page is unchanged and the event stream records the denial |
| C4 | Check the receipt/verification after the operation | The tool result has a receipt; the page text is the real evidence |
| C5 | Switch tabs during the click | Only the already-authorized `authorizedTabId` is operated; the new tab is not touched |

## D. Lifecycle / exceptions

| # | Operation | Expected |
|---|---|---|
| D1 | Cancel during a run | The task terminates; no rollback of side effects is promised; the terminal state is explicit |
| D2 | Close the side panel | The Bridge disconnects; after reopening, the result is treated as unknown and not redone automatically |
| D3 | The Service Worker is reclaimed / reconnects | The panel restores the connection; the run slot is not occupied permanently |
| D4 | Approval timeout | Treated as a denial; no indefinite hanging |
| D5 | Bridge disconnect | An explicit error; no "fake success" is shown |

## Execution environment (optional)

When no interactive desktop is available locally, this checklist can be executed on a cloud Linux
desktop (**browser / Side Panel only**; it does not replace Windows UIA acceptance):

- **box by Simular** (<https://box.simular.ai/>): a cloud Ubuntu desktop with Chrome and a shell,
  driven over MCP or VNC. One free cloud desktop per account (official wording; pricing may change —
  check <https://box.simular.ai/billing>). It sleeps when idle and keeps files, but **a box asleep for
  14 consecutive days is deleted**. No stable public port: not a public server.
- Access: box's MCP endpoint needs OAuth or a personal key (`Authorization: Bearer cb_...`).
  YAI's MCP client currently supports only URL/stdio and **does not yet support custom HTTP headers**;
  wiring it into the kernel first needs `headers` / `token_env` support.
- Security: use a throwaway account; never put keys, `.env` or GitHub tokens on the box; clean up files
  and revoke the key after acceptance.
- When recording, note `box / Ubuntu / Chrome <version>` in the "Date / browser / version" field and
  attach VNC screenshots.

> As of 2026-11-13 per the official page; the product is in beta, so pricing and quotas may change.

## Box MCP real-machine smoke (2026-11-14)

Verified "MCP connection + cloud desktop shell + cloud Chrome" on Box by Simular:

- Personal key: <https://box.simular.ai/access> → Keys and devices → Create key (`cb_...`,
  stored only in the ignored local `.env.local`);
- The same `McpToolBridge` registers **36 tools** (`shell` / `read_file` / `browser_*` /
  `computer_*` / `gui_plan` / `box_*`, etc.);
- `box_status`: `absent` → `box_ensure`: `running` (image `sbxdesk:v22`);
- `shell`: `hello-from-box` · `Linux 6.8.0-142-generic x86_64` · `/workspace`;
- `browser_navigate` → `https://example.com` returns `Page: Example Domain`;
- Reproduce: `uv sync --extra mcp && uv run python examples/host_box_mcp/smoke.py`.

Boundaries: Box Free shows `0 credits left`; this smoke uses only the box shell/Chrome (YAI uses its
own model). A full Side Panel desktop-GUI acceptance is not included; revoke the key from the Access
page if it leaks.

## Record template

```text
Date / browser / version:
Extension ID:
Test page URL:
Scenario ID:
Approval count:
Final status: done / failed / cancelled / unknown
Real page changes (screenshot/text):
Receipt hash:
Exceptions and logs:
Verdict: pass / fail / cannot verify
```

## Pass criteria

- All scenarios among A/B/C/D that relate to this change have evidence;
- Unauthorized content did not enter the model context;
- There are no side effects when an approval is denied;
- "The model says it is done" is not accepted in place of page/tool evidence;
- Scenarios that did not pass state the reason and are not marked as passed on a real machine.
