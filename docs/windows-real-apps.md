# Windows Desktop Provider 真实第三方应用验证（v0.27 Batch 5）

> 环境：Windows 11 + 真实系统自带应用 `charmap.exe`（字符映射表），
> Python 3.13 / pywinauto 0.6.9。脚本：`scripts/windows_real_apps_smoke.py`。
> 不使用用户文档、不保存文件；测试结束关闭应用。

## 方法

- 启动 `charmap.exe`，用 `PywinautoDesktopBackend.list_windows()` 读取 PID/窗口句柄；
- 用 `DesktopPolicy.secure([pid], allowed_window_titles=["字符映射表"])` 显式收窄范围；
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

## 诚实边界

- 只验证了一个系统自带应用（字符映射表）；**不代表任意 Windows 应用兼容**。
- 下拉选择已覆盖：新增 `desktop_select`，优先 ComboBox.select、其次
  ValuePattern；不支持时失败关闭，不猜坐标。当前只在字符映射表字体 ComboBox
  上验证过，其他应用/控件类型仍需抽样。
- 未验证：第三方应用、UAC/安全桌面、多显示器、截图、复杂 UIA Pattern、本地化/
  无名称图标按钮、长时运行。
- 控件名称关键词只作为附加拒绝信号，不是安全边界；本测试用进程/标题白名单收窄。
- 真实任务成功率只来自本次 10 项检查，样本量 1，不能外推为生产指标。

## 复现

```powershell
.\.venv\Scripts\python.exe scripts\windows_real_apps_smoke.py
```
