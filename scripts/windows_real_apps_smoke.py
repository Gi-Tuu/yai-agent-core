"""Windows Desktop Provider 真实第三方应用冒烟（安全、可重复）。

使用系统自带「字符映射表」(charmap.exe)，不打开用户文档、不保存文件；
覆盖：窗口枚举/白名单、观察、文本输入(set_value)、按钮 Invoke、聚焦、用户接管
(pause/resume)、旧控件引用失效、未允许窗口拒绝。输出 JSON 指标。

运行：.venv\\Scripts\\python.exe scripts\\windows_real_apps_smoke.py
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from typing import Any

from yai_core.integrations.desktop import (
    DesktopPolicy,
    DesktopProvider,
)
from yai_core.integrations.desktop.pywinauto_backend import PywinautoDesktopBackend


async def _run() -> dict[str, Any]:
    metrics: dict[str, Any] = {
        "app": "charmap.exe",
        "window_found": False,
        "observe_ok": False,
        "text_input_ok": False,
        "button_invoke_ok": False,
        "focus_ok": False,
        "takeover_blocked": False,
        "takeover_resumed": False,
        "stale_ref_blocked": False,
        "disallowed_window_blocked": False,
        "errors": [],
    }
    proc = subprocess.Popen(
        ["charmap.exe"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    backend = PywinautoDesktopBackend()
    try:
        # 等窗口出现（最多 10s），并从后端枚举拿到 PID / window_id（只读元数据）。
        target = None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            await asyncio.sleep(0.4)
            try:
                windows = await backend.list_windows()
            except Exception as exc:  # noqa: BLE001
                metrics["errors"].append(f"list_windows: {exc}")
                continue
            for window in windows:
                if window.process_id == str(proc.pid) or "字符映射表" in window.title:
                    target = window
                    break
            if target is not None:
                break
        if target is None:
            metrics["errors"].append("未找到字符映射表窗口")
            return metrics
        metrics["window_found"] = True
        metrics["window_title"] = target.title
        metrics["process_id"] = target.process_id

        policy = DesktopPolicy.secure(
            [str(proc.pid)], allowed_window_titles=["字符映射表"]
        )
        provider = DesktopProvider(backend, policy=policy)

        listed = await provider.list_windows()
        metrics["listed_windows"] = len(listed.get("windows", []))
        metrics["hidden_windows"] = listed.get("hidden_count", 0)

        observation = await provider.observe(target.window_id)
        metrics["observe_ok"] = bool(observation.get("ok"))
        if not metrics["observe_ok"]:
            metrics["errors"].append(f"observe: {observation}")
            return metrics
        controls = observation.get("controls", [])
        metrics["control_count"] = len(controls)

        def find(role: str, name_part: str) -> dict[str, Any] | None:
            return next(
                (
                    item
                    for item in controls
                    if item.get("role") == role and name_part in item.get("name", "")
                ),
                None,
            )

        edit = next((c for c in controls if c.get("role") == "Edit"), None)
        button = find("Button", "选择")
        if edit is None or button is None:
            metrics["errors"].append("未找到 Edit/选择按钮")
            return metrics

        # 1) 文本输入
        result = await provider.set_value(edit["ref"], "A")
        metrics["text_input_ok"] = bool(result.get("ok"))
        if not metrics["text_input_ok"]:
            metrics["errors"].append(f"set_value: {result}")

        # 2) 重新观察并验证输入值（证据来自真实 UIA 控件值）
        observation = await provider.observe(target.window_id)
        metrics["observe_after_input_ok"] = bool(observation.get("ok"))
        after_controls = observation.get("controls", [])
        edit_after = next(
            (c for c in after_controls if c.get("role") == "Edit"), {}
        )
        metrics["text_value"] = edit_after.get("value")
        metrics["text_value_verified"] = str(edit_after.get("value", "")).startswith("A")

        # 3) 按钮 Invoke（选择字符）
        button_after = next(
            (
                c
                for c in after_controls
                if c.get("role") == "Button" and "选择" in c.get("name", "")
            ),
            None,
        )
        if button_after is not None:
            result = await provider.invoke(button_after["ref"])
            metrics["button_invoke_ok"] = bool(result.get("ok"))
            if not metrics["button_invoke_ok"]:
                metrics["errors"].append(f"invoke: {result}")

        # 4) 聚焦
        focus = await provider.focus(target.window_id)
        metrics["focus_ok"] = bool(focus.get("ok"))

        # 5) 用户接管：pause 后动作被拒，观察仍可用；resume 后恢复
        provider.pause("真实应用冒烟：模拟用户接管")
        paused = await provider.set_value(edit["ref"], "B")
        metrics["takeover_blocked"] = paused.get("status") == "paused"
        provider.resume()
        resumed = await provider.focus(target.window_id)
        metrics["takeover_resumed"] = bool(resumed.get("ok"))

        # 6) 未允许窗口：用另一个真实窗口验证只回拒绝、不读内容
        other = next(
            (w for w in windows if w.window_id != target.window_id), None
        )
        if other is not None:
            denied = await provider.observe(other.window_id)
            metrics["disallowed_window_blocked"] = (
                denied.get("status") == "window_not_allowed"
            )

        # 7) 旧引用失效：关闭应用后旧 ref 不得再执行
        old_ref = edit["ref"]
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        await asyncio.sleep(0.8)
        stale = await provider.invoke(old_ref)
        metrics["stale_ref_blocked"] = stale.get("status") in (
            "stale_ref",
            "backend_error",
        )
        if not metrics["stale_ref_blocked"]:
            metrics["errors"].append(f"stale ref: {stale}")
    finally:
        if proc.poll() is None:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=15,
            )
    return metrics


def main() -> int:
    if sys.platform != "win32":
        print(json.dumps({"ok": False, "skipped": "windows-only"}))
        return 2
    metrics = asyncio.run(_run())
    checks = [
        "window_found",
        "observe_ok",
        "text_input_ok",
        "text_value_verified",
        "button_invoke_ok",
        "focus_ok",
        "takeover_blocked",
        "takeover_resumed",
        "stale_ref_blocked",
        "disallowed_window_blocked",
    ]
    metrics["passed"] = [name for name in checks if metrics.get(name)]
    metrics["failed"] = [name for name in checks if not metrics.get(name)]
    metrics["ok"] = not metrics["failed"]
    print(json.dumps(metrics, ensure_ascii=False, indent=2))
    return 0 if metrics["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
