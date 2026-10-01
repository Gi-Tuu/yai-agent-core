"""全局快捷键：无论焦点在哪个程序，一键唤起灵动岛。

Windows 用 ``RegisterHotKey`` 向根窗口注册热键，系统在按下时往该窗口投递
``WM_HOTKEY``；根窗口类 :class:`YaiWindow` 重写 ``nativeEvent`` 接收。

- 默认热键：``Ctrl + Alt + Y``（``MOD_NOREPEAT`` 按住不连发）；
- 只注册这一个组合键，不监听其它按键（比低级键盘钩子更干净、无隐私顾虑）；
- 热键被别的程序占用 / 注册失败时，:func:`register_global_hotkey` 返回
  ``None`` 静默降级（灵动岛仍可由托盘唤起），不影响启动。

注：PySide6 的 ``QAbstractNativeEventFilter`` 在当前环境收不到消息，故不在
全局事件过滤器里拦截，改为在窗口自身的 ``nativeEvent`` 中处理。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

from PySide6.QtQuick import QQuickWindow

#: WM_HOTKEY 消息号。
_WM_HOTKEY = 0x0312
#: 修饰键：Ctrl / Alt / 按住不重复。
_MOD_CONTROL = 0x0002
_MOD_ALT = 0x0001
_MOD_NOREPEAT = 0x4000
#: 本程序热键标识（WM_HOTKEY 的 wParam 回传它）。
_HOTKEY_ID = 0xC01
#: 虚拟键码 'Y'。
_VK_Y = 0x59


class YaiWindow(QQuickWindow):
    """QML 根窗口：重写 ``nativeEvent`` 接收系统投递的 ``WM_HOTKEY``。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._hotkey_id: int | None = None
        self._on_hotkey = None

    def set_hotkey_handler(self, hotkey_id: int, on_hotkey) -> None:
        self._hotkey_id = hotkey_id
        self._on_hotkey = on_hotkey

    def clear_hotkey_handler(self) -> None:
        self._hotkey_id = None
        self._on_hotkey = None

    def nativeEvent(self, eventType, message):
        if eventType == "windows_generic_MSG" and self._on_hotkey is not None:
            try:
                msg = wintypes.MSG.from_address(int(message))
            except (TypeError, ValueError):
                return False, 0
            if (msg.message == _WM_HOTKEY
                    and self._hotkey_id is not None
                    and int(msg.wParam) == self._hotkey_id):
                self._on_hotkey()
                return True, 0
        return False, 0


def register_global_hotkey(app, window, on_hotkey):
    """注册 ``Ctrl+Alt+Y`` 并把回调挂到根窗口。

    成功返回注销回调（无参，调用即 UnregisterHotKey）；注册失败返回 ``None``。
    """
    user32 = ctypes.windll.user32
    installed = user32.RegisterHotKey(
        int(window.winId()),
        _HOTKEY_ID,
        _MOD_CONTROL | _MOD_ALT | _MOD_NOREPEAT,
        _VK_Y,
    )
    if not installed:
        return None

    if hasattr(window, "set_hotkey_handler"):
        window.set_hotkey_handler(_HOTKEY_ID, on_hotkey)

    def _unregister() -> None:
        user32.UnregisterHotKey(int(window.winId()), _HOTKEY_ID)
        if hasattr(window, "clear_hotkey_handler"):
            window.clear_hotkey_handler()

    return _unregister
