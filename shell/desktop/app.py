"""原生桌面端入口：贴顶胶囊窗口 + 员工面板 + 系统托盘。

启动::

    .\\.venv\\Scripts\\python.exe -m shell.desktop            # 离线演示（无 Key，核对动效）
    .\\.venv\\Scripts\\python.exe -m shell.desktop --live      # 接真实模型
    .\\.venv\\Scripts\\python.exe -m shell.desktop --no-tray   # 不要托盘

窗口由 QML 绘制（真原生渲染，非浏览器内容），各应用内置的 Core 与界面同进程。
"""

from __future__ import annotations

import argparse
import ctypes
from pathlib import Path

from PySide6.QtCore import QPointF, QSettings, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QPainter, QPixmap, QPolygonF
from PySide6.QtQml import QQmlApplicationEngine, qmlRegisterType
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from shell.desktop.host import build_workbench, prepare_paths
from shell.desktop.hotkey import YaiWindow, register_global_hotkey
from shell.desktop.specialist import PERMISSION_MODES

#: QML 资源目录。
QML_DIR = Path(__file__).resolve().parent / "qml"

#: 托盘图标边长。
_ICON_SIZE = 32

#: 胶囊距屏幕工作区顶部的留白（逻辑像素）。
TOP_MARGIN = 10


def place_top_center(window) -> None:
    """按屏幕工作区把球定位到顶部居中（DPI 与多屏由 Qt 统一）。

    只定一次位：之后球→胶囊向右长、胶囊→面板向下长，左上角不动，
    移动窗口交给 ``startSystemMove()``（系统负责跟随指针与多屏）。
    """
    screen = QGuiApplication.primaryScreen()
    if screen is None or window is None:
        return
    area = screen.availableGeometry()
    window.setX(area.x() + (area.width() - window.width()) // 2)
    window.setY(area.y() + TOP_MARGIN)


def _point_on_any_screen(x: int, y: int) -> bool:
    """该点是否落在任一屏幕几何内（多屏断开后旧坐标会落在空处）。"""
    for screen in QGuiApplication.screens():
        geo = screen.geometry()
        if (geo.x() <= x < geo.x() + geo.width()
                and geo.y() <= y < geo.y() + geo.height()):
            return True
    return False


def restore_window_position(window, settings: QSettings) -> None:
    """优先恢复上次拖动后的位置；无记录或已不在任何屏幕时回退顶部居中。"""
    x = settings.value("window/x")
    y = settings.value("window/y")
    if x is not None and y is not None and _point_on_any_screen(int(x), int(y)):
        window.setX(int(x))
        window.setY(int(y))
        return
    place_top_center(window)


def hide_from_taskbar(window) -> None:
    """Qt.Window 默认占任务栏；事后加 WS_EX_TOOLWINDOW 隐藏。

    只改扩展样式，不改变 Qt 的窗口创建路径（类名仍是 QWindowIcon、正常
    DWM 合成），避免 Qt.Tool 触发的 QWindowToolSaveBits 遗留合成路径。
    """
    if window is None or not hasattr(window, "winId"):
        return
    user32 = ctypes.windll.user32
    hwnd = int(window.winId())
    exstyle = user32.GetWindowLongW(hwnd, -20)
    user32.SetWindowLongW(hwnd, -20, exstyle | 0x00000080)


def build_icon() -> QIcon:
    """代码生成波形图标，避免为壳引入图片资源。"""
    pixmap = QPixmap(_ICON_SIZE, _ICON_SIZE)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    pen = painter.pen()
    pen.setColor(QColor("#e8eaf2"))
    pen.setWidthF(2.4)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    waveform = QPolygonF(
        [QPointF(x, y) for x, y in
         [(4, 16), (8, 16), (11, 6), (16, 26), (20, 12), (23, 16), (28, 16)]]
    )
    painter.drawPolyline(waveform)
    painter.end()
    return QIcon(pixmap)


def install_tray(app: QApplication, workbench, window) -> QSystemTrayIcon:
    """系统托盘：显示/隐藏窗口与退出。"""
    tray = QSystemTrayIcon(build_icon(), app)
    tray.setToolTip("YAI 数字员工")
    menu = QMenu()
    show_action = menu.addAction("显示员工")
    hide_action = menu.addAction("隐藏员工")
    menu.addSeparator()
    quit_action = menu.addAction("退出")
    tray.setContextMenu(menu)
    show_action.triggered.connect(lambda: _set_visible(window, True))
    hide_action.triggered.connect(lambda: _set_visible(window, False))
    quit_action.triggered.connect(workbench.requestQuit)
    tray.activated.connect(
        lambda reason: _set_visible(window, True)
        if reason == QSystemTrayIcon.Trigger
        else None
    )
    tray.show()
    return tray


def _set_visible(window, on: bool) -> None:
    if window is not None:
        window.setProperty("visible", on)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="YAI 数字员工（原生桌面端）")
    parser.add_argument("--live", action="store_true", help="接真实模型")
    parser.add_argument(
        "--permission", choices=PERMISSION_MODES, default="partial",
        help="初始权限挡位（默认 partial）",
    )
    parser.add_argument("--no-tray", action="store_true", help="不创建系统托盘")
    parser.add_argument(
        "--core-off", action="store_true", help="启动时停用 Core"
    )
    args = parser.parse_args(argv)
    prepare_paths()

    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication([])
    app.setApplicationName("YAI 数字员工")
    app.setQuitOnLastWindowClosed(False)
    # Windows 原生控件样式不允许改 background，统一走 Basic 才能定制深色输入框
    QQuickStyle.setStyle("Basic")

    workbench = build_workbench(args.permission, live=args.live)
    if args.core_off:
        workbench.setCoreEnabled(False)

    # 根窗口用重写了 nativeEvent 的 YaiWindow，接收全局热键的 WM_HOTKEY
    qmlRegisterType(YaiWindow, "YaiWindow", 1, 0, "YaiWindow")

    engine = QQmlApplicationEngine()
    engine.addImportPath(str(QML_DIR))  # 载入 YaiTheme 单例模块
    engine.rootContext().setContextProperty("workbench", workbench)
    engine.load(QUrl.fromLocalFile(str(QML_DIR / "main.qml")))
    if not engine.rootObjects():
        workbench.shutdown()
        return 1

    window = engine.rootObjects()[0]
    settings = QSettings("YAI", "Desktop")
    restore_window_position(window, settings)
    hide_from_taskbar(window)

    # 位置落盘：拖动结束（x/y 变化停 400ms）自动保存，重启即恢复。
    save_timer = QTimer()
    save_timer.setInterval(400)
    save_timer.setSingleShot(True)

    def _save_position() -> None:
        settings.setValue("window/x", window.x())
        settings.setValue("window/y", window.y())

    save_timer.timeout.connect(_save_position)
    window.xChanged.connect(save_timer.start)
    window.yChanged.connect(save_timer.start)

    def _summon() -> None:
        """全局热键 Ctrl+Alt+Y：把岛拉到前台并展开面板。"""
        _set_visible(window, True)
        window.raise_()
        window.requestActivate()
        window.setProperty("shape", "panel")

    unregister_hotkey = register_global_hotkey(app, window, _summon)
    tray = None if args.no_tray else install_tray(app, workbench, window)

    def _cleanup() -> None:
        if unregister_hotkey is not None:
            unregister_hotkey()
        _save_position()
        if tray is not None:
            tray.hide()
        engine.deleteLater()  # 先拆界面，再收运行时，避免绑定读到已销毁对象
        workbench.shutdown()

    workbench.quitRequested.connect(_cleanup)
    workbench.quitRequested.connect(app.quit)
    return app.exec()


if __name__ == "__main__":
    from shell.desktop.host import prepare_paths

    prepare_paths()
    raise SystemExit(main())
