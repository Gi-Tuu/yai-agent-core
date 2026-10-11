"""桌面壳场景图后端选择（Windows 透明置顶窗圆角黑角修复）。

渲染结果本身只能在真机/跨进程合成下观察，这里只锁定后端选择策略：
Windows 默认走 software（UpdateLayeredWindow，逐像素 alpha 跨进程合成正确），
YAI_FORCE_RHI=1 时退回 GPU(RHI)，非 Windows 不改动。
"""

import sys

import shell.desktop.app as appmod


def test_windows_selects_software_backend(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    env: dict[str, str] = {}
    appmod.configure_quick_backend(env)
    assert env["QT_QUICK_BACKEND"] == "software"


def test_force_rhi_opt_out_on_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    env = {"YAI_FORCE_RHI": "1"}
    appmod.configure_quick_backend(env)
    assert "QT_QUICK_BACKEND" not in env


def test_non_windows_left_untouched(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    env: dict[str, str] = {}
    appmod.configure_quick_backend(env)
    assert "QT_QUICK_BACKEND" not in env
