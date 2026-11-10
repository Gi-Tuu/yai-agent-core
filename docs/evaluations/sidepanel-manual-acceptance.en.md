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
