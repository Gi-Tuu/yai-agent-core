"""Windows 桌面后端契约（Provider 只依赖这个 Protocol，不知道 pywinauto）。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from yai_core.integrations.desktop.models import DesktopObservation, WindowInfo


@runtime_checkable
class DesktopBackend(Protocol):
    """一个可被 Provider 驱动的桌面（真实 UIA / 假实现）。"""

    async def list_windows(self) -> list[WindowInfo]:
        """列出可见的顶层窗口。"""
        ...

    async def observe(self, window_id: str) -> DesktopObservation:
        """观察指定窗口的 UIA 控件树快照。"""
        ...

    async def focus(self, window_id: str) -> None:
        """把目标窗口带到前台（显式动作；不用于绕过焦点保护）。"""
        ...

    async def is_foreground(self, window_id: str) -> bool:
        """目标窗口当前是否在前台。"""
        ...

    async def invoke(self, window_id: str, control_id: str) -> None:
        """通过 UIA Invoke / Toggle / SelectionItem 等语义 Pattern 操作控件。"""
        ...

    async def set_value(self, window_id: str, control_id: str, value: str) -> None:
        """通过 UIA ValuePattern 设置值（优先于模拟键盘）。"""
        ...

    async def click(self, window_id: str, control_id: str) -> None:
        """坐标点击兜底（仅当宿主显式允许 geometry click 时由 Provider 调用）。"""
        ...

    async def select_option(self, window_id: str, control_id: str, value: str) -> None:
        """下拉/列表选择：优先 ComboBox.select，其次 ValuePattern；不支持时失败关闭。"""
        ...

    async def screenshot(self, window_id: str) -> bytes | None:
        """可选截图；不支持时返回 None（不假装支持）。"""
        ...
