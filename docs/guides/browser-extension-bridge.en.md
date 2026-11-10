> English translation of [中文](browser-extension-bridge.md) · The Chinese version is canonical; if they disagree, the Chinese wins.

# YAI Agent Core Browser Extension (MV3 · Side Panel) and Native Bridge

**English** · [简体中文](browser-extension-bridge.md)

> Status (real-hardware acceptance on this machine, 2026-11-03): **the extension has been loaded successfully in real Chrome 156 / Edge 156**
> (`scripts/extension_smoke.py`: `--pack-extension` passes, the MV3 service worker appears in the CDP targets);
> **the Bridge has been verified end-to-end against a real Sidecar** (`scripts/bridge_smoke.py`).
> Verified on real hardware (2026-11-03, real Edge): extension loading, side panel, approval/clarification, real tasks, Native Messaging
> registration end-to-end, reopening the panel while a run is in progress and then cancelling, automatic reconnect after force-killing the Sidecar, restarting the Bridge when the last panel closes
> (see the "Real-hardware acceptance record" at the end of this document).
> Still unverified: opening the side panel by actually clicking the toolbar icon with the mouse (user gestures cannot be automated), installation-package distribution, and cross-browser differences.
> "Loads successfully" must not be taken as "the distribution path is verified".

## Structure

```text
Chrome / Edge Extension (MV3)
  ├─ Service Worker: only handles the side panel ↔ Bridge connection/forwarding, may be reclaimed by the browser at any time
  ├─ Side Panel: task input, collapsible workflow, approval/clarification, cancel, connection status
  └─ chrome.scripting (activeTab): injects a one-shot collection function only after the user clicks "Read current page"
        ⇅ Native Messaging (4-byte little-endian length + UTF-8 JSON)
  yai_core.sidecar.native_bridge (local Bridge process)
        ⇅ stdio NDJSON + JSON-RPC 2.0 (does not modify the existing Sidecar public protocol)
  yai_core.sidecar → AgentCore
        ├─ ToolExecutor / PermissionPolicy / approval / execution receipts
        ├─ Outcome Verification (host verifier optional)
        └─ Browser Provider (optional, Playwright)
```

## Security boundaries (per task brief §5.3)

| Item | Approach |
|---|---|
| Extension identity | The Bridge only accepts the `chrome-extension://<id>/` passed in by Chrome, and it must be inside the locally configured `allowed_origins`; an empty config = reject everything, fail closed |
| Credentials | **No high-privilege credentials are kept on the browser side**; `initialize` parameters such as model / policy only come from the local Bridge config file. When the panel saves, the Key passes through memory exactly once and is always redacted when read back (`***last 4 chars`). **Note: `bridge.json` and `.bak` are plaintext sensitive files** (location/ACL/rotation risks in [ADR 0007](../adr/0007-local-secret-storage.md)); they are currently not advertised as encrypted storage |
| Operation allowlist | The extension may only send `hello / run / cancel / host_response / ping / status / shutdown / get_config / configure_llm`; unknown operations are rejected outright |
| Field allowlist | `configure_llm` may only change `base_url/model/strong_model/api_key` of `initialize.model` (it cannot change `sidecar_command`/`allowed_origins`/other initialize fields); **changing base_url/provider requires a new api_key at the same time**, so the old Key is never redirected to a new endpoint; base_url only accepts http/https. `host_response` may only answer `srv-*` reverse requests |
| Size limits | Inbound frames ≤ 1 MiB; outbound frames over the limit are replaced with a `truncated` marker (to avoid Chrome killing the host process) |
| Config writes | Bridge and local CLI share "file lock + temp file in the same directory + fsync + os.replace"; the old version is rotated to `.bak`, so a half-written JSON file is never left behind |
| Reading pages | Scripts are not injected by default; `tabs` is only used to read the current tab's URL/title (not the page content); when the user clicks "Read current page", an optional host permission is first requested for the **current origin** (`optional_host_permissions`, once per site), then `activeTab`/`scripting` is used to collect; page content is attached to the task as **untrusted input**. If authorization is refused, nothing is read |
| Read ≠ operate | Only read-only snapshots; controlled browser operations go through the Browser Provider (governed by `BrowserPolicy`) or a Playwright MCP explicitly configured by the host (separate/CDP session), and **do not control the current tab**. See [ADR 0006](../adr/0006-browser-execution-unification.md) |

> The panel header shows "Current tab: read-only snapshot; controlled browser: not connected / MCP browser",
> so that reading the current page is not mistaken for being able to operate it.

## Installation (Windows / Chrome or Edge)

1. Install YAI (repository root): `uv sync --extra llm` (real models) or add `--extra browser` as needed.
2. Build the extension: Chrome → `chrome://extensions` → turn on "Developer mode" → "Load unpacked" → select this repository's `extension/` directory. Note down the extension ID.
3. Generate an executable Native Host (Chrome's manifest `path` must point to an executable file;
   `python.exe` cannot carry a `-m` argument):
   ```powershell
   .\.venv\Scripts\python.exe -m PyInstaller --onefile --name yai-bridge `
     --distpath dist --workpath build\pyinstaller --specpath build\pyinstaller `
     --paths src extension\native\bridge_host.py
   ```
4. Register (a single command writes the config + manifest + the Chrome/Edge HKCU registry entries; no administrator rights needed; `--dry-run` previews first — **in the preview the api_key is shown only as `***last 4 chars`**):
   ```powershell
   .\.venv\Scripts\python.exe scripts\register_native_host.py `
     --extension-id <your extension ID> --launcher dist\yai-bridge.exe --python .\.venv\Scripts\python.exe
   ```
   It writes `%LOCALAPPDATA%\YAI\bridge.json` (`allowed_origins` + `sidecar_command`; by default it **contains no model credentials**,
   and `initialize.model` is written only when `--env-file .env` is passed explicitly)
   and `%LOCALAPPDATA%\YAI\com.yai.agentcore.json`, and registers
   `HKCU\Software\Google\Chrome\NativeMessagingHosts\com.yai.agentcore` and
   `HKCU\Software\Microsoft\Edge\NativeMessagingHosts\com.yai.agentcore`.
   Uninstall: `python scripts\register_native_host.py --unregister`.
5. Click the extension icon to open the side panel; once the status badge shows "Connected to local Sidecar" (or "Protocol handshake complete") you can run tasks.
   Troubleshooting log: `%LOCALAPPDATA%\YAI\bridge.log` (Chrome cannot see the native host's stderr,
   so the Bridge writes startup/errors there).

> Model credentials: set `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `LLM_MODEL` in the environment that launches Chrome
> (the Sidecar default model factory reads these variables); **do not** put them into the extension or into browser messages. After changing
> environment variables, Chrome must be restarted (child processes inherit the environment from launch time).

## Model and tool integration (measured on this machine)

- **Model credentials**: `register_native_host.py --env-file .env` writes `OPENAI_API_KEY / OPENAI_BASE_URL /
  LLM_MODEL / LLM_STRONG_MODEL` into `initialize.model` of the local `%LOCALAPPDATA%\YAI\bridge.json`
  (**only in the local config file, never into the extension, never sent to the browser**; the Key is not echoed when results are printed).
  If you do not want it on disk, you can instead use user-level environment variables + restart the browser.
- **Browser capability (optional)**: `--browser-mcp playwright` adds a
  Playwright MCP server to `initialize.mcp` (`npx -y @playwright/mcp@0.0.84 --browser chrome`, **pinned version**, headed by default,
  so the user can see the Chrome being operated; `--mcp-headless` makes it headless). This is an **optional extension**: MCP goes through the existing `[mcp]` extra
  and lazy-loading integration, and the kernel `dependencies` still stays empty (the AST guard tests pass as usual).
- **Approval**: default `policy.mode=auto`; a tool outside the allowlist pops a confirmation card in the side panel; `allow_all` is recommended only for local demos.

End-to-end measurement on this machine (real Edge + real Chrome via Playwright MCP): task "open the Baidu homepage and tell me the title"
→ route `react` → two approvals → `browser_navigate` / `browser_evaluate` succeed → final answer
`"百度一下，你就知道"` (the Baidu homepage slogan). Screenshot: `docs/exports/extension-e2e.png` (gitignore).

## CDP mode: let tools operate your own Chrome window

The default Playwright MCP opens a new controlled browser; to operate the user's own Chrome session, use a CDP connection.
When registering with `--cdp-endpoint`, the config automatically switches to `scripts/playwright_mcp_launcher.py`:
if the endpoint is not listening, it first automatically launches the standalone debug Chrome from `scripts/chrome_cdp.py start`; if that also fails it
**falls back to the MCP-managed browser**, so that one initialize never makes every browser_* tool unavailable because 9222 was not started.

```powershell
# 1) Start a Chrome with a remote debugging port (separate profile; Chrome does not allow enabling a debugging port on the default profile)
.\.venv\Scripts\python.exe scripts\chrome_cdp.py start          # defaults to http://127.0.0.1:9222
.\.venv\Scripts\python.exe scripts\chrome_cdp.py status

# 2) Point the MCP config at the CDP endpoint (written into the local bridge.json)
.\.venv\Scripts\python.exe scripts\register_native_host.py `
  --extension-id <extension ID> --launcher dist\yai-bridge-v2.exe --python .\.venv\Scripts\python.exe `
  --env-file .env --browser-mcp playwright --cdp-endpoint http://127.0.0.1:9222

# 3) Close the last side panel and open it again (this restarts the Bridge and re-reads the config)
# 4) When done, stop the debugging Chrome (does not affect the Chrome you normally use; only stops processes whose recorded pid passes validation)
.\.venv\Scripts\python.exe scripts\chrome_cdp.py stop
```

**Real-hardware verification on this machine (real Edge + real Chrome/156 via CDP)**: the side panel shows
`协议握手完成（bridge v1 · 模型 deepseek/deepseek-v4.1-flash-1017 · MCP browser）` ("protocol handshake complete: bridge v1 · model deepseek/deepseek-v4.1-flash-1017 · MCP browser");
task "open the Baidu homepage and tell me the title" → two approvals → `browser_navigate` / `browser_evaluate`
succeed, answer `"百度一下，你就知道"`; in `http://127.0.0.1:9222/json/list` the current page is
`https://www.baidu.com/`. Screenshot: `docs/exports/extension-cdp-e2e.png` (gitignore).

> Note: CDP connects to the Chrome started by `scripts/chrome_cdp.py` (a separate profile),
> not to the default Chrome window you are using; an already-running Chrome cannot enable a debugging port after the fact. If you want to attach
> to an existing window, it must have been started with `--remote-debugging-port`.

> **Concurrent saves (v0.27)**: Bridge and CLI share an OS-level
> `<config>.lock` for read-modify-write of `bridge.json`; when the lock is busy it returns `config_lock_busy` and abandons this write (it never overwrites without the lock),
> and the original file, `.bak` and atomic replacement stay unchanged. Key read-back is always redacted.

## LLM configuration entry points (panel + local CLI)

The side panel has a collapsible **"LLM settings (saved to local config)"** section: you can fill in Base URL / model / strong model /
API Key (password field). Behaviour:

- "Read current config" fills the model/Base URL back in, and the Key is shown only as `***last 4 chars` (the Bridge never returns plaintext);
- "Save to local" writes the config into `%LOCALAPPDATA%\YAI\bridge.json` (atomic write: temp file in the same directory + fsync +
  `os.replace`, the old version rotated to `.bak`); **it takes effect after you restart the browser or close the last side panel and reopen it
  (closing the panel ends the Bridge/Sidecar, and reopening re-reads the config)**. Merely closing and reopening the panel
  does not necessarily trigger a reconnect — rely on whether the status bar performs a new handshake;
- **Switching Base URL / provider requires filling in a new API Key at the same time**, otherwise the Bridge rejects it (`api_key_required`):
  the old Key will never be redirected to a new endpoint; Base URL only accepts `http/https` (`invalid_base_url`);
- The Key travels through memory only once, when you click save, and is written into the local config file; **the last one is not stored and nothing goes into localStorage**,
  and the input field is cleared after saving; the panel does not offer "read the plaintext Key back";
- A failed save shows the reason in the settings section (`api_key_required` / `invalid_base_url`); it does not fail silently.
- Measured on this machine: after reading, the fields are filled with `https://api.suanli.cn/v1` / `deepseek/...`,
  the Key is shown as `已配置（***QRhb）` ("configured", with only the last 4 characters visible); saving reports that the config
  has been saved locally and takes effect after restarting the browser or reopening the side panel (closing the last panel
  restarts the Bridge).
  Screenshot: `docs/exports/extension-llm-settings.png` (gitignore).

An equivalent CLI is still kept (suitable for scripts/CI):

```powershell
.\.venv\Scripts\python.exe scripts\configure_bridge_llm.py --show
Get-Content .env | .\.venv\Scripts\python.exe scripts\configure_bridge_llm.py --api-key-stdin
```

## LLM configuration (local, never through the browser)

The panel only shows the current model / MCP names (non-sensitive fields of `hello_ok`); the API Key never enters the browser:

```powershell
.\.venv\Scripts\python.exe scripts\configure_bridge_llm.py --show
.\.venv\Scripts\python.exe scripts\configure_bridge_llm.py --model deepseek/deepseek-v4.1-flash-1017 `
  --base-url https://api.suanli.cn/v1
Get-Content .env | .\.venv\Scripts\python.exe scripts\configure_bridge_llm.py --api-key-stdin  # the Key does not land in shell history
```

After changing, restart the browser or reopen the side panel (which restarts the Bridge) for it to take effect; the script output always redacts the Key.
The CLI and the Bridge share the same config write lock and atomic write entry point, so changes the panel just saved are not overwritten with old values.
MCP and these settings are optional host-side capabilities; the kernel `dependencies = []` is unchanged.

## Protocol mapping

| Browser message | Sidecar (JSON-RPC) | Notes |
|---|---|---|
| `hello {protocol:1}` | — | Version negotiation; on mismatch it replies `hello_rejected` |
| `run {task}` | request `agent/run` | Replies `run_started {run_id}`; only one run is allowed at a time. If a short task's terminal state arrives before the response, the Bridge forwards only the terminal state and does not send `run_started` afterwards |
| `cancel {run_id}` | request `agent/cancel` | Replies `run_cancel_requested` |
| `status` | — | Replies `{type:"status", active_run_id, sidecar_alive, initialized, hello_done}`; reopening the panel uses it to restore run state |
| `host_response {request_id,result}` | response to `srv-*` | Only reverse-request IDs are accepted; everything else is rejected |
| `ping` / `shutdown` | `ping` / `shutdown` | Health check and shutdown |
| — | notification `agent/event` etc. | Converted to `{type:"notification", method, params}` |
| — | request `approval/request` / `clarify/request` | Converted to `{type:"host_request"}`, and the side panel pops the confirmation/follow-up question |
| — | Sidecar exit / transport error | Converted to `{type:"sidecar_exit", reason}`; the extension then actively disconnects and reconnects |
| — | — | Errors are uniformly `{type:"error", code, message}`: `run_busy` / `sidecar_not_running` / `sidecar_timeout` / `sidecar_start_failed` / `api_key_required` / `invalid_base_url` and so on |

## Lifecycle and known limitations (honest statement)

- The Bridge is a **short-lived process bound to one extension connection**: when the connection drops → the Sidecar ends, and **no cross-process task recovery is provided**.
- **Closing the panel = restarting the Bridge**: when the last side panel closes, the Service Worker actively disconnects the native port
  (if a task is still running, it waits for its terminal state before disconnecting, to avoid a silent abort); reopening the panel starts a new Bridge and
  re-reads `bridge.json`, so after changing the model config, "close the panel and open it again" is enough. After the panel reopens it actively
  sends `status` to synchronize run state, and a running task can still be cancelled.
- **A Sidecar crash does not leave a zombie Bridge**: after the Bridge sends `sidecar_exit`, the Service Worker actively disconnects
  and reconnects after 3s (the new Bridge starts a new Sidecar); interrupted tasks are marked unknown and are **not retried automatically**.
- After a disconnect / Service Worker reclamation, the side panel marks the result as **unknown** and **does not automatically redo** operations that may already have had side effects;
  the user needs to restate the task or first confirm the external state.
- Bridge RPCs have timeouts (initialize 60s / run 10s, etc.); on timeout it returns `sidecar_timeout` and does not hang the whole
  serial message loop; late responses are dropped as orphan responses and are not disguised as event notifications.
- `scripts/chrome_cdp.py stop` only terminates a Chrome that matches the "pid file record and image/command line/`--remote-debugging-port`/
  `--user-data-dir` all match" condition; any mismatch (PID reuse, file tampered with) makes it refuse the `taskkill`
  and return non-zero.
- Currently only Chrome / Edge (Chromium 116+ Side Panel) are supported; Firefox and others are not adapted.
- **Verified on real hardware**: the extension can be loaded into Chrome 156 / Edge 156 via `--load-extension` and registers an
  MV3 service worker (`scripts/extension_smoke.py`, with the CDP target as evidence); hello / status / shutdown between the Bridge and a
  real Sidecar pass (`scripts/bridge_smoke.py`);
  **Native Messaging end-to-end has been verified on real Edge**: after registration the side panel status changes to
  "protocol handshake complete (bridge v1) / ready", and `%LOCALAPPDATA%\YAI\bridge.log` records
  a `frozen=True` startup and `exit ok`.
- **Troubleshooting**: when the panel shows "Error when communicating with the native messaging host",
  first look at `%LOCALAPPDATA%\YAI\bridge.log`. Measured behaviour: Chrome/Edge additionally pass
  `--parent-window=0` (already handled, no longer treated as a config path); also check whether the manifest's
  `allowed_origins` matches the extension ID exactly.
- **Still not verified on real hardware**: opening the side panel by really clicking the toolbar icon with the mouse (user gestures cannot be simulated automatically), installation-package distribution,
  the panel hint that changing base_url requires a new Key (only covered as a static contract regression), and cross-browser differences. Multi-tab /
  download / upload / CAPTCHA on real sites are likewise unverified. These are covered by offline tests for static contracts and fake-object behaviour, but that cannot replace real-hardware acceptance.
- **Verified on real hardware on 2026-11-03 (real Edge, see the "Real-hardware acceptance record" at the end)**: Native Messaging registration end-to-end,
  restoring "running" after the panel reopens and then cancelling, the SW reconnecting automatically after force-killing the Sidecar, and closing the last panel to restart the Bridge
  and re-read the config.

## Acceptance commands on this machine (2026-11-03)

```powershell
# 1) Real browser task (Playwright Chromium; --channel chrome uses the system Chrome instead)
.\.venv\Scripts\python.exe scripts\browser_smoke.py --json

# 2) Bridge ↔ real Sidecar (no browser; frame protocol + rejection of unauthorized origins)
.\.venv\Scripts\python.exe scripts\bridge_smoke.py --json

# 3) Real-hardware extension loading (headed Chrome / Edge; headless does not load extensions)
.\.venv\Scripts\python.exe scripts\extension_smoke.py --json
.\.venv\Scripts\python.exe scripts\extension_smoke.py --browser msedge --json

# 4) Windows desktop UIA smoke (a self-built window, does not touch the user's programs)
.\.venv\Scripts\python.exe scripts\windows_uia_smoke.py --json
```

Evidence summary: 11 execution receipts for the browser task (source-gate interception and stale-reference expiry both verified); on the Chrome and
Edge sides a `chrome-extension://<id>/service_worker.js` target appeared in each; the Bridge exits with code 2 for an unauthorized origin, and
handshake/status/shutdown all pass for an authorized origin. Note that **for an unpacked extension the ID is determined by the path**, so when formally registering
the native host, use a stable ID under a fixed installation path.

## Can't see the side panel? Troubleshoot in this order

1. **Click the extension icon, not the browser's built-in "side panel" button**: open developer mode in `chrome://extensions` →
   load the unpacked extension → select `extension/`; then **pin** the YAI icon in the toolbar and click it →
   the Side Panel appears on the right side of the browser. The extension does not pop the side panel open automatically after loading.
2. **Reload after code changes**: click "Reload" on the YAI card in `chrome://extensions` (or remove it and
   load it again), otherwise the browser is still running the old service worker.
3. **If there is still no reaction**: open `chrome://extensions` → YAI card → click the "Service Worker" link to see
   console errors; you can also open
   `chrome-extension://<your extension ID>/sidepanel.html` directly in a new tab — this is the tab version of the same UI,
   useful to confirm that the panel itself renders and what the error is.
4. **A status of "local Bridge not registered" is normal**: the side panel is open, it has simply not registered the
   Native Messaging Host from section 3 yet; clicking "Run" before registering gives a clear error rather than failing silently.
5. **Recent Chrome versions restrict command-line `--load-extension`**: this does not affect loading it manually in the UI;
   the automated acceptance scripts use Playwright's bundled Chromium / Edge to cover loading, side panel page rendering and
   `openPanelOnActionClick` behaviour.

> The extension has a built-in fallback: the service worker top level and onInstalled/onStartup all set
> `openPanelOnActionClick`; when the icon is clicked and `chrome.sidePanel.open` is unavailable or fails,
> `sidepanel.html` is automatically opened as an ordinary tab, preventing "clicked it and nothing happened".

## Real-hardware acceptance record (2026-11-03, real Edge)

The following three items were executed on real Edge + a real Native Bridge (frozen exe) + a real Sidecar, and all passed:

1. **Closing/reopening the panel during a run restores it and allows cancelling**: close the side panel while running → reopen a new panel → the state is restored to "running",
   the run button is disabled and cancel is available → click cancel → the state becomes "cancelled".
2. **Automatic reconnect after force-killing the Sidecar**: kill `python -m yai_core.sidecar` during a run → the SW receives
   `sidecar_exit`, actively disconnects and reconnects → the panel returns to "ready" → after reconnecting, the new task "hello, reply with just OK" completes normally.
3. **Closing the last panel restarts the Bridge**: when idle, the corresponding Sidecar exits within 6s of closing the panel; reopening the panel
   starts the Bridge/Sidecar again, the config is re-read, and the state is "ready".

Local screenshots (gitignore artifacts, not distributed in the repository): `docs/exports/accept-reopen-cancel.png`,
`docs/exports/accept-reopen-idle.png`. Items 8/9/11 of the verification checklist above are covered by this record.

## Verification checklist (requires a browser environment)

1. `chrome://extensions` loads with no errors; clicking the icon opens the side panel.
2. The status changes from "connecting" to "connected to local Sidecar".
3. Enter a task → the event stream shows `strategy_selected / tool_call / tool_result / done`.
4. When a tool needs approval, a confirmation card appears in the side panel, and both allow and deny are passed back to the Sidecar.
5. Click "cancel" while running; the Sidecar sends `agent/cancelled`.
6. Close the browser (or force-kill the Bridge) and reopen: the state is unknown, and the task is not resent automatically.
7. A Bridge with an unauthorized extension ID refuses to start outright (exit code 2).
8. **Close and reopen the side panel during a run**: the new panel should show "running" and allow cancelling (status synchronization;
   if the task had already finished when the last panel closed, the Bridge restarts and the task state is treated as unknown).
9. **Force-kill the Sidecar process**: the panel shows that the Sidecar has exited / is reconnecting; after about 3s the state is restored, the old task is not
   replayed automatically, and running again normally starts a new Sidecar.
10. **In LLM settings, change only the Base URL without a new Key**: this should be rejected and the error shown in the settings section; after filling in a new Key, saving succeeds.
11. **After the last panel closes, change `bridge.json` and then open the panel**: `hello_ok`/the settings section should reflect the new config
    (proving the Bridge has restarted and re-read the config).
