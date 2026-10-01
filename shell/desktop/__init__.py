"""YAI 员工原生桌面端（PySide6 + QML）：一个灵动岛管理多个专员。

- ``specialist``：单专员运行时（事件投递、挂起决策、独立权限挡位）；
- ``workbench``：工作台（唯一后台循环 + 多专员 + 当前专员聚合 + 未读）；
- ``channel``：Channel SPI 的 Qt 实现，授权/澄清挂起等待界面决策；
- ``host``：四专员装配（离线演示 / 真实模型两种形态）；
- ``app``：QML 窗口入口（球 ⇄ 胶囊  面板 + 拖动 + 系统托盘）。

依赖只进 ``[desktop]`` 可选 extra；内核 ``src/yai_core`` 仍零第三方硬依赖。
"""

from __future__ import annotations

from shell.desktop.channel import PROMPT_TIMEOUT_SECONDS, QtChannel
from shell.desktop.specialist import (
    PERMISSION_MODES,
    SpecialistRuntime,
    SpecialistSpec,
)
from shell.desktop.workbench import SpecialistListModel, WorkbenchRuntime

__all__ = [
    "PERMISSION_MODES",
    "PROMPT_TIMEOUT_SECONDS",
    "QtChannel",
    "SpecialistListModel",
    "SpecialistRuntime",
    "SpecialistSpec",
    "WorkbenchRuntime",
]
