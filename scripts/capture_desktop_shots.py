"""离线生成灵动岛截图（无需 API Key）：引导球态 + 完整对话面板。

``QScreen.grabWindow(0)`` 抓整屏（DWM 真实合成，无分层窗口白块），按窗口几何
精确裁剪；composite 时按 shell 形状（球=椭圆、面板=圆角矩形）做 clip mask，
丢弃窗口透明区（屏幕抓取时是桌面），成品为浮在深色圆角卡片上的干净球 / 面板。

面板内容不跑异步 startTask（对焦点/时序敏感、易 flaky），而是遍历离线 SCRIPT，
直接调用 QML 的 ``onSpecialistEvent`` 同步喂事件（permission_asked 批准后不
yield，跳过），确定、无挂起。

运行::

    .\\.venv\\Scripts\\python.exe scripts\\capture_desktop_shots.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, QSettings, Qt, QUrl
from PySide6.QtGui import (
    QColor,
    QCursor,
    QGuiApplication,
    QPainter,
    QPainterPath,
    QPixmap,
    QRadialGradient,
)
from PySide6.QtQml import QQmlApplicationEngine, qmlRegisterType
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shell.demo_script import SCRIPT  # noqa: E402
from shell.desktop.host import build_workbench, prepare_paths  # noqa: E402
from shell.desktop.hotkey import YaiWindow  # noqa: E402

QML_DIR = ROOT / "shell" / "desktop" / "qml"
ASSETS = ROOT / "docs" / "assets"
BG = "#0D0F15"

_TASK = "算这笔订单的含税金额并归档"


def composite(grab: QPixmap, pad: int, card_radius: int, kind: str,
              shape_radius: float, dpr: float, name: str) -> None:
    """把抓到的窗口按 shell 形状裁剪，合成到深色圆角卡片（accent 径向光晕）。

    kind="ball" 裁剪成椭圆（圆形球）；kind="panel" 裁剪成圆角矩形
    （shape_radius 为逻辑圆角，乘 dpr 得物理像素）。窗口透明区（圆/圆角外，
    屏幕抓取时是桌面）一律丢弃，露出卡片底色，成品才是干净的圆形球 / 圆角面板。
    """
    # grab 的 devicePixelRatio=屏幕 dpr，canvas 默认 dpr=1。重置为 1 后全程
    # 物理像素 1:1，drawPixmap 不会被按逻辑尺寸缩小，clip 与贴图严格对齐。
    grab.setDevicePixelRatio(1.0)
    w = grab.width() + pad * 2
    h = grab.height() + pad * 2
    canvas = QPixmap(w, h)
    canvas.fill(QColor(0, 0, 0, 0))

    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(BG))
    painter.drawRoundedRect(0, 0, w, h, card_radius, card_radius)

    grad = QRadialGradient(QPointF(w / 2, h * 0.3), max(w, h) * 0.72)
    c_top = QColor("#7C5CFF")
    c_top.setAlpha(48)
    grad.setColorAt(0, c_top)
    c_edge = QColor("#7C5CFF")
    c_edge.setAlpha(0)
    grad.setColorAt(1, c_edge)
    painter.setBrush(grad)
    painter.drawRoundedRect(0, 0, w, h, card_radius, card_radius)
    painter.end()

    # 内容：按 shell 形状裁剪后再贴图
    target = QRectF(pad, pad, grab.width(), grab.height())
    path = QPainterPath()
    if kind == "ball":
        path.addEllipse(target)
    else:
        path.addRoundedRect(target, shape_radius * dpr, shape_radius * dpr)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setRenderHint(QPainter.SmoothPixmapTransform)
    painter.setClipPath(path)
    painter.drawPixmap(pad, pad, grab)
    painter.end()
    canvas.save(str(ASSETS / name))


def grab_window(screen, root) -> QPixmap:
    """抓整屏后按窗口几何（物理像素）精确裁剪出灵动岛。"""
    dpr = screen.devicePixelRatio()
    full = screen.grabWindow(0)
    x = int(round(root.x() * dpr))
    y = int(round(root.y() * dpr))
    w = int(round(root.width() * dpr))
    h = int(round(root.height() * dpr))
    return full.copy(x, y, w, h)


def feed_event(root, sid, ev) -> None:
    """同步把一个 DemoEvent 喂给 QML 的 onSpecialistEvent（直接 Python 调用）。"""
    payload = json.dumps(ev.data, ensure_ascii=False)
    root.onSpecialistEvent(sid, ev.type, payload)


def main() -> int:
    prepare_paths()
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication([])
    QQuickStyle.setStyle("Basic")

    # 引导态：先清完成标记（build_workbench 会读取它决定 onboardingOpen）
    settings = QSettings("YAI", "Desktop")
    prev_onboarding = settings.value("onboarding/done", False, type=bool)
    settings.setValue("onboarding/done", False)

    wb = build_workbench("partial", live=False)
    qmlRegisterType(YaiWindow, "YaiWindow", 1, 0, "YaiWindow")

    engine = QQmlApplicationEngine()
    engine.addImportPath(str(QML_DIR))
    engine.rootContext().setContextProperty("workbench", wb)
    engine.load(QUrl.fromLocalFile(str(QML_DIR / "main.qml")))
    if not engine.rootObjects():
        return 1
    root = engine.rootObjects()[0]
    screen = QGuiApplication.primaryScreen()

    # ---- 截图 1：引导球（"点我" + 呼吸外圈）----
    QCursor.setPos(2, 2)                    # 移开物理鼠标，避免悬停触发成胶囊
    QTest.qWait(250)
    root.setProperty("shape", "ball")
    QTest.qWait(1100)                       # 等形变 + onboardingRing 呼吸
    composite(grab_window(screen, root), pad=92, card_radius=26, kind="ball",
              shape_radius=0, dpr=screen.devicePixelRatio(),
              name="desktop-ball.png")

    # ---- 截图 2：面板完整对话（同步喂 SCRIPT，确定性）----
    root.setProperty("shape", "panel")
    QTest.qWait(650)                        # 等形变动画完成（height 420ms）

    # 强制底层 render surface 按最终几何重建（快速形变后 surface 可能滞后、裁剪场景）
    root.hide()
    QTest.qWait(200)
    root.show()
    root.raise_()
    QTest.qWait(600)

    # 强制 feedModel 重新绑定（规避 Loader/Binding 初始化时序）：切走再切回
    wb.setActiveSpecialist("warehouse")
    QTest.qWait(120)
    wb.setActiveSpecialist("sales")
    QTest.qWait(120)
    sid = wb.activeSpecialistId

    root.setProperty("typing", True)         # 阻止 done 后自动收起，给足渲染时间
    root.pushUser(sid, _TASK)                # 用户气泡
    for _delay, ev in SCRIPT:
        if ev.type == "permission_asked":
            continue                         # 批准后不 yield，对话不留此步
        feed_event(root, sid, ev)

    QTest.qWait(1200)                        # 渲染最终对话
    root.setProperty("shape", "panel")
    QTest.qWait(500)
    composite(grab_window(screen, root), pad=46, card_radius=28, kind="panel",
              shape_radius=28, dpr=screen.devicePixelRatio(),
              name="desktop-panel.png")

    # 显式清理，避免 QML / 后台 asyncio 线程残留
    settings.setValue("onboarding/done", prev_onboarding)  # 恢复本机标记
    root.close()
    wb.shutdown()
    app.quit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
