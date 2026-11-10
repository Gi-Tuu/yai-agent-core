> English translation of [中文](windows-real-apps.md) · The Chinese version is canonical; if they disagree, the Chinese wins.

# Windows Desktop Provider Verification on a Real Third-Party Application (v0.27 Batch 5)

**English** · [简体中文](windows-real-apps.md)

> Environment: Windows 11 + the real system built-in application `charmap.exe` (Character Map),
> Python 3.13 / pywinauto 0.6.9. Script: `scripts/windows_real_apps_smoke.py`.
> No user documents are used and no files are saved; the application is closed when the test ends.

## Method

- Launch `charmap.exe` and use `PywinautoDesktopBackend.list_windows()` to read the PID / window handle / process image;
- Use `DesktopPolicy.secure([pid], allowed_window_titles=["字符映射表"])` to narrow the scope explicitly;
  the PID is an **exact match** (`123` does not admit `1234`); the PID changes on every restart, so declaring
  `allowed_process_names` / `allowed_process_paths` is the more robust approach for long-running use;
- Execute observe / type / Invoke / focus / pause through `DesktopProvider` (rather than calling pywinauto directly);
- Stale references are invalidated: after closing the application, a call with an old ref must fail closed;
  windows that are not allowed only return a refusal.

## Measured results (2026-11-03)

```json
{
  "app": "charmap.exe",
  "window_found": true,
  "observe_ok": true,
  "text_input_ok": true,
  "button_invoke_ok": true,
  "focus_ok": true,
  "dropdown_select_ok": true,
  "dropdown_value_verified": true,
  "dropdown_value": "Arial",
  "takeover_blocked": true,
  "takeover_resumed": true,
  "stale_ref_blocked": true,
  "disallowed_window_blocked": true,
  "text_value": "A\\r",
  "text_value_verified": true,
  "listed_windows": 1,
  "hidden_windows": 7,
  "control_count": 26,
  "passed": [
    "window_found", "observe_ok", "text_input_ok", "text_value_verified",
    "button_invoke_ok", "focus_ok", "dropdown_select_ok", "dropdown_value_verified",
    "takeover_blocked", "takeover_resumed",
    "stale_ref_blocked", "disallowed_window_blocked"
  ],
  "failed": [],
  "ok": true
}
```

| Metric | Result |
|---|---|
| Target window identified | 1/1 |
| Observe succeeded | 1/1 |
| Text input with read-back verification | 1/1 (UIA value `A\r`) |
| Button semantic Invoke | 1/1 |
| Focus succeeded | 1/1 |
| Dropdown selection + read-back verification | 1/1 (font ComboBox → Arial) |
| Action refused during user takeover / resumed | 1/1 / 1/1 |
| Mis-operation through a stale control reference | 0 (refused) |
| Content leak from disallowed windows | 0 (`window_not_allowed`) |
| Windows outside the allowed window | 7 return only `hidden_count`; title/process are not returned |

## Process identity fields measured (2026-11-03, same day)

The newly added `WindowInfo.process_name` / `process_path` were verified on the real desktop:
`PywinautoDesktopBackend.list_windows()` enumerated 6 visible windows and **6/6** yielded the process image
name (explorer.exe / msedge.exe / …) and the full path; windows where it could not be obtained degrade to an
empty string and do not block enumeration.
This also means production can use `allowed_process_names` / `allowed_process_paths` instead of a volatile PID.

## Nightly real-machine regression (since v0.34, non-blocking)

`.github/workflows/nightly-real.yml` runs the real-machine regression once a day at 03:17 UTC (it can also be
triggered manually with `workflow_dispatch`), and **does not participate in the PR gate**:

- `windows-desktop` (windows-2025 runner): `windows_uia_smoke.py --json` + `windows_real_apps_smoke.py`,
  logs written to the artifact; both steps are `continue-on-error` (the hosted runner's interactive desktop is not guaranteed),
  and the artifact is the source of truth;
- `browser-real` (ubuntu runner): a real Chromium runs `browser_smoke.py` and `browser_provider_task_smoke.py`;
  a failure turns that job red (a real signal).

Local re-verification on 2026-11-10 (with a real desktop + Chromium provided): UIA `ok=true`, charmap `12/12`,
browser smoke `11 receipts`, all green.

**Measured on a GitHub-hosted runner (2026-11-10, run 42782702561)**: in the windows job both scripts ran to
completion, but the hosted runner **has no interactive desktop** — UIA returned
`{"ok": false, "detail": "UIA 没找到测试窗口（可能没有交互桌面 / UIA 不可用）"}`, and charmap also failed because it could
not find the window; the artifact contains the real cause rather than an encoding error.
That is exactly why `continue-on-error` is used: nightly only does "it starts up + leaves evidence" and does not
dress up a desktop-less environment as a pass.

## Honest boundaries

- Only one system built-in application (Character Map) was verified; **this does not mean compatibility with
  arbitrary Windows applications**.
- Dropdown selection is covered: the added `desktop_select` prefers ComboBox.select, then
  ValuePattern; when unsupported it fails closed and does not guess coordinates. It has currently only been
  verified on the Character Map font ComboBox; other applications/control types still need sampling.
- Not verified: third-party applications, UAC/secure desktop, multiple monitors, screenshots, complex UIA Patterns,
  localization / unnamed icon buttons, long-running operation.
- Control-name keywords and window titles are only additional filtering signals, not a security boundary; this test
  narrows the scope by process PID (exact match), and the title/process information of disallowed windows is not returned.
- The real-task success rate comes only from these 12 checks, with a sample size of 1; it cannot be extrapolated
  into a production metric.

## Reproduce

```powershell
.\.venv\Scripts\python.exe scripts\windows_real_apps_smoke.py
```
