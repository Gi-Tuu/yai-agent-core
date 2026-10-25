"""桌面壳场景图后端选择（Windows 透明置顶窗圆角黑角修复）。

渲染结果本身只能在真机/跨进程合成下观察，这里只锁定后端选择策略：
Windows 默认走 software（UpdateLayeredWindow，逐像素 alpha 跨进程合成正确），
YAI_FORCE_RHI=1 时退回 GPU(RHI)，非 Windows 不改动。
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("PySide6.QtCore", reason="需要 PySide6（uv sync --extra desktop）")

import shell.desktop.app as appmod  # noqa: E402


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


@pytest.mark.parametrize("phase", ["idle", "working", "complete"])
@pytest.mark.parametrize("hide", ["window", "minimize", "parent"])
def test_nebula_timer_stops_when_hidden_and_resumes(phase, hide):
    source = Path(__file__).resolve().parents[1] / "shell/desktop/qml/NebulaOrbCanvas.qml"
    code = r'''
import sys
from PySide6.QtCore import QObject, QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQuick import QQuickItem, QQuickView
from PySide6.QtTest import QTest

app = QGuiApplication([])
window = QQuickView()
window.setSource(QUrl.fromLocalFile(sys.argv[1]))
assert window.status() == QQuickView.Ready, window.errors()
orb = window.rootObject()
container = QQuickItem(window.contentItem())
container.setWidth(440)
container.setHeight(52)
orb.setParentItem(container)
timers = [obj for obj in orb.findChildren(QObject)
          if obj.metaObject().className() == "QQmlTimer"]
assert len(timers) == 1
timer = timers[0]
ticks = []
timer.triggered.connect(lambda: ticks.append(1))
orb.setProperty("phase", sys.argv[2])
window.show()
QTest.qWait(220)
assert timer.property("running") and ticks, "visible animation did not start"
interval = timer.property("interval")
assert interval == 16, "所有相位统一 60fps，星云漂移不因降帧出现顿挫"
try:
    if sys.argv[3] == "window":
        window.hide()
    elif sys.argv[3] == "minimize":
        window.showMinimized()
    else:
        orb.parentItem().setVisible(False)
    QTest.qWait(60)
    assert not timer.property("running"), "hidden animation timer still running"
    before = len(ticks)
    QTest.qWait(160)
    assert len(ticks) == before, "hidden animation still schedules paints"
    orb.parentItem().setVisible(True)
    window.showNormal()
    QTest.qWait(220)
    assert timer.property("running") and len(ticks) > before
    orb.setProperty("phase", "frozen")
    orb.setProperty("timeScale", 0)
    QTest.qWait(60)
    assert not timer.property("running"), "frozen animation should stay stopped"
    window.hide()
    window.showNormal()
    QTest.qWait(100)
    assert not timer.property("running"), "showing must not unfreeze animation"
    orb.setProperty("phase", "working")
    QTest.qWait(220)
    assert timer.property("running")
    assert orb.property("timeScale") > 0
finally:
    window.close()
    app.processEvents()
'''
    result = subprocess.run(
        [sys.executable, "-c", code, str(source), phase, hide],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "QT_QUICK_BACKEND": "software"},
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
