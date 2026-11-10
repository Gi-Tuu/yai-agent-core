# Windows Desktop Provider 真实第三方应用验证（v0.27 Batch 5）

> 环境：Windows 11 + 真实系统自带应用 `charmap.exe`（字符映射表），
> Python 3.13 / pywinauto 0.6.9。脚本：`scripts/windows_real_apps_smoke.py`。
> 不使用用户文档、不保存文件；测试结束关闭应用。

## 方法

- 启动 `charmap.exe`，用 `PywinautoDesktopBackend.list_windows()` 读取 PID/窗口句柄/进程映像；
- 用 `DesktopPolicy.secure([pid], allowed_window_titles=["字符映射表"])` 显式收窄范围；
  PID 为**精确匹配**（`123` 不放行 `1234`）；PID 每次重启都会变，长期运行更稳的做法是
  声明 `allowed_process_names` / `allowed_process_paths`；
- 通过 `DesktopProvider`（而不是直接调 pywinauto）执行观察/输入/Invoke/聚焦/暂停；
- 旧引用失效：关闭应用后再用旧 ref 调用，必须失败关闭；未允许窗口只返回拒绝。

## 2026-11-03 实测结果

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

| 指标 | 结果 |
|---|---|
| 目标窗口识别 | 1/1 |
| 观察成功 | 1/1 |
| 文本输入并回读验证 | 1/1（UIA 值 `A\r`） |
| 按钮语义 Invoke | 1/1 |
| 聚焦成功 | 1/1 |
| 下拉选择 + 回读验证 | 1/1（字体 ComboBox → Arial） |
| 用户接管期间动作拒绝 / 恢复 | 1/1 / 1/1 |
| 旧控件引用误操作 | 0（被拒绝） |
| 未允许窗口内容泄漏 | 0（`window_not_allowed`） |
| 允许窗口之外的窗口 | 7 个仅回 `hidden_count`，标题/进程不返回 |

## 进程身份字段实测（2026-11-03，同日）

新增的 `WindowInfo.process_name` / `process_path` 在真实桌面验证：
`PywinautoDesktopBackend.list_windows()` 枚举到 6 个可见窗口，**6/6** 都取到了进程映像名
（explorer.exe / msedge.exe / …）与完整路径；取不到的窗口降级为空串，不阻塞枚举。
这也意味着生产可以用 `allowed_process_names` / `allowed_process_paths` 替代易变的 PID。

## Nightly 真机回归（v0.34 起，非阻塞）

`.github/workflows/nightly-real.yml` 每天 03:17 UTC 跑一次真机回归（也可 `workflow_dispatch`
手动触发），**不参与 PR 门禁**：

- `windows-desktop`（windows-2025 runner）：`windows_uia_smoke.py --json` + `windows_real_apps_smoke.py`，
  日志写入 artifact；两步 `continue-on-error`（托管 runner 的交互桌面不保证），结果以 artifact 为准；
- `browser-real`（ubuntu runner）：真实 Chromium 跑 `browser_smoke.py` 与 `browser_provider_task_smoke.py`，
  失败会让该 job 变红（真实信号）。

本机 2026-11-10 复验（前置真实桌面 + Chromium）：UIA `ok=true`、charmap `12/12`、
browser smoke `11 receipts` 全绿。

## 诚实边界

- 只验证了一个系统自带应用（字符映射表）；**不代表任意 Windows 应用兼容**。
- 下拉选择已覆盖：新增 `desktop_select`，优先 ComboBox.select、其次
  ValuePattern；不支持时失败关闭，不猜坐标。当前只在字符映射表字体 ComboBox
  上验证过，其他应用/控件类型仍需抽样。
- 未验证：第三方应用、UAC/安全桌面、多显示器、截图、复杂 UIA Pattern、本地化/
  无名称图标按钮、长时运行。
- 控件名称关键词与窗口标题都只作为附加过滤信号，不是安全边界；本测试靠进程 PID
  （精确匹配）收窄，未允许窗口的标题/进程信息不返回。
- 真实任务成功率只来自本次 12 项检查，样本量 1，不能外推为生产指标。

## 复现

```powershell
.\.venv\Scripts\python.exe scripts\windows_real_apps_smoke.py
```
