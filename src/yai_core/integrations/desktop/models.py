"""Windows 桌面观察模型：窗口、控件与快照（v0.26 Desktop Provider，可选适配器）。

零平台耦合：本模块只定义数据形状，不 import pywinauto / ctypes；真实执行由
宿主或 ``pywinauto_backend`` 提供，测试用假后端。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class WindowInfo:
    window_id: str
    title: str = ""
    process_id: str = ""
    class_name: str = ""
    enabled: bool = True
    visible: bool = True
    #: 真实进程映像名（如 ``notepad.exe``）与完整路径；后端尽力提供，取不到为空。
    process_name: str = ""
    process_path: str = ""

    def to_public(self) -> dict[str, Any]:
        return {
            "window_id": self.window_id,
            "title": self.title[:200],
            "process_id": self.process_id,
            "process_name": self.process_name[:120],
            "process_path": self.process_path[:300],
            "class_name": self.class_name[:100],
            "enabled": self.enabled,
            "visible": self.visible,
        }


@dataclass(frozen=True)
class ControlInfo:
    """UIA 控件；``control_id`` 是后端不透明句柄，模型只见带快照版本的引用。"""

    control_id: str
    role: str = ""
    name: str = ""
    automation_id: str = ""
    enabled: bool = True
    value: str = ""
    sensitive: bool = False

    def to_public(self, ref: str) -> dict[str, Any]:
        out: dict[str, Any] = {"ref": ref}
        if self.role:
            out["role"] = self.role
        if self.name:
            out["name"] = self.name[:200]
        if self.automation_id:
            out["automation_id"] = self.automation_id[:100]
        if not self.enabled:
            out["enabled"] = False
        if self.sensitive:
            out["sensitive"] = True
        elif self.value:
            out["value"] = self.value[:200]
        return out


@dataclass(frozen=True)
class DesktopObservation:
    window: WindowInfo
    controls: tuple[ControlInfo, ...] = ()
    focused: str = ""
    fingerprint: str = ""

    def control_by_index(self, index: int) -> ControlInfo | None:
        if 0 <= index < len(self.controls):
            return self.controls[index]
        return None


@dataclass
class DesktopSnapshot:
    version: int
    observation: DesktopObservation
    window_id: str = ""
    controls: dict[str, ControlInfo] = field(default_factory=dict)

    @property
    def snapshot_id(self) -> str:
        return f"d{self.version}"
