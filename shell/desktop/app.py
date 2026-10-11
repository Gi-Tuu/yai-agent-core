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
import os
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QPointF, QSettings, Qt, QTimer, QUrl, Slot
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QGuiApplication,
    QIcon,
    QPainter,
    QPixmap,
    QPolygonF,
)
from PySide6.QtQml import QQmlApplicationEngine, qmlRegisterType
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from shell.desktop.geometry import clamp_frame
from shell.desktop.host import build_workbench, prepare_paths
from shell.desktop.hotkey import YaiWindow, register_global_hotkey
from shell.desktop.single_instance import SingleInstance
from shell.desktop.specialist import PERMISSION_MODES

#: QML 资源目录。
QML_DIR = Path(__file__).resolve().parent / "qml"
FONTS_DIR = Path(__file__).resolve().parent / "fonts"

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


class GeometryBridge(QObject):
    """暴露给 QML 的展开体边缘钳制桥。

    钳制算法在零 Qt 依赖的 :mod:`shell.desktop.geometry`（可离线测试），
    QML 只负责取当前屏幕工作区几何与球锚点。
    """

    @Slot(int, int, int, int, int, int, int, int, result=QPointF)
    def clampFrame(
        self,
        ball_x: int,
        ball_y: int,
        frame_w: int,
        frame_h: int,
        avail_x: int,
        avail_y: int,
        avail_w: int,
        avail_h: int,
    ) -> QPointF:
        x, y = clamp_frame(
            ball_x, ball_y, frame_w, frame_h,
            avail_x, avail_y, avail_w, avail_h,
        )
        return QPointF(float(x), float(y))


def configure_quick_backend(env: dict[str, str] | None = None) -> None:
    """在 Windows 上固定 Qt Quick 软件场景图后端，修透明置顶窗的圆角黑角。

    根因：Qt Quick 默认 RHI（Windows 上为 D3D11 / DirectComposition，d3d12 /
    vulkan / opengl 后端同样）渲染 frameless + ``color: "transparent"`` 的置顶窗
    时，圆角外的逐像素 alpha 在**跨进程 DWM 终合成**中丢失、被当成不透明黑块；
    同进程的两个 Qt 窗走进程内合成所以正常（因此该问题只在真机、岛浮于其它进程
    窗口之上时出现）。软件后端走分层窗口 ``UpdateLayeredWindow`` 路径，逐像素
    alpha 跨进程合成正确。灵动岛 QML 仅用基础图元（无 ShaderEffect / MultiEffect
    / 阴影模糊 / 粒子），CPU 光栅化性能够用。

    必须在 ``QGuiApplication`` 创建前调用。排障时设 ``YAI_FORCE_RHI=1`` 可退回
    默认 GPU（RHI）后端。非 Windows 不做任何改动。
    """
    env = os.environ if env is None else env
    if sys.platform == "win32" and env.get("YAI_FORCE_RHI") != "1":
        env["QT_QUICK_BACKEND"] = "software"


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
    parser.add_argument(
        "--allow-multiple", action="store_true", help=argparse.SUPPRESS
    )  # 调试用：跳过单实例守护，允许同时开多个
    args = parser.parse_args(argv)
    prepare_paths()
    # 必须在 QApplication 创建前选定场景图后端（修 Windows 透明置顶窗圆角黑角）。
    configure_quick_backend()

    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    app = QApplication([])
    app.setApplicationName("YAI 数字员工")
    app.setQuitOnLastWindowClosed(False)
    # Windows 原生控件样式不允许改 background，统一走 Basic 才能定制深色输入框
    QQuickStyle.setStyle("Basic")

    # 注册随包 JetBrains Mono（终端工艺唯一等宽字体，离线可用）；
    # 找不到文件时静默回退系统等宽字体，不阻断启动。
    for _ttf in ("JetBrainsMono-Regular.ttf", "JetBrainsMono-Bold.ttf"):
        _font_path = FONTS_DIR / _ttf
        if _font_path.exists():
            QFontDatabase.addApplicationFont(str(_font_path))
    # 全窗默认字体走 JetBrains Mono（终端工艺唯一等宽字体）；
    # 中文缺字由 Qt 自动回退系统中文 sans，无需在 QML 里逐处指定。
    app.setFont(QFont("JetBrains Mono"))

    # 单实例：已有岛在跑时，本次启动改为唤起它并退出，不重复建 Core/托盘/热键。
    single = SingleInstance()
    if not args.allow_multiple and not single.acquire():
        return 0

    workbench = build_workbench(args.permission, live=args.live)
    if args.core_off:
        workbench.setCoreEnabled(False)

    # 根窗口用重写了 nativeEvent 的 YaiWindow，接收全局热键的 WM_HOTKEY
    qmlRegisterType(YaiWindow, "YaiWindow", 1, 0, "YaiWindow")

    engine = QQmlApplicationEngine()
    engine.addImportPath(str(QML_DIR))  # 载入 YaiTheme 单例模块
    # 持有桥接对象引用：contextProperty 不替 Python 侧保活，临时对象会被
    # GC 回收导致 QML 里 geom 为 null（须随 main 生命周期存活）。
    geom_bridge = GeometryBridge()
    engine.rootContext().setContextProperty("workbench", workbench)
    engine.rootContext().setContextProperty("geom", geom_bridge)
    engine.load(QUrl.fromLocalFile(str(QML_DIR / "main.qml")))
    if not engine.rootObjects():
        workbench.shutdown()
        return 1

    window = engine.rootObjects()[0]
    settings = QSettings("YAI", "Desktop")
    restore_window_position(window, settings)
    # 记录球锚点：展开时窗口会为避开屏幕边缘而平移，持久化的始终是球位置，
    # 不是展开后的面板左上角（否则下次启动球会错位）。
    window.setProperty("ballX", window.x())
    window.setProperty("ballY", window.y())
    window.setProperty("ballPosKnown", True)
    hide_from_taskbar(window)

    # 位置落盘：拖动结束（x/y 变化停 400ms）自动保存，重启即恢复。
    save_timer = QTimer()
    save_timer.setInterval(400)
    save_timer.setSingleShot(True)

    def _save_position() -> None:
        # 存球锚点而非当前窗口位置：面板展开时窗口为避开屏幕边缘发生了平移。
        if window.property("ballPosKnown"):
            settings.setValue("window/x", window.property("ballX"))
            settings.setValue("window/y", window.property("ballY"))
        else:
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

    # 第二个实例启动时，首个实例在这里被唤起（拉前台并展开）。
    single.set_activator(_summon)
    unregister_hotkey = register_global_hotkey(app, window, _summon)
    tray = None if args.no_tray else install_tray(app, workbench, window)

    def _cleanup() -> None:
        if unregister_hotkey is not None:
            unregister_hotkey()
        _save_position()
        if tray is not None:
            tray.hide()
        single.close()
        engine.deleteLater()  # 先拆界面，再收运行时，避免绑定读到已销毁对象
        workbench.shutdown()

    workbench.quitRequested.connect(_cleanup)
    workbench.quitRequested.connect(app.quit)
    return app.exec()


if __name__ == "__main__":
    from shell.desktop.host import prepare_paths

    prepare_paths()
    raise SystemExit(main())
