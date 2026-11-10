"""YAI 员工原生桌面端（PySide6 + QML）：一个灵动岛管理多个专员。

- ``specialist``：单专员运行时（事件投递、挂起决策、独立权限挡位）；
- ``workbench``：工作台（唯一后台循环 + 多专员 + 当前专员聚合 + 未读）；
- ``channel``：Channel SPI 的 Qt 实现，授权/澄清挂起等待界面决策；
- ``host``：四专员装配（离线演示 / 真实模型两种形态）；
- ``app``：QML 窗口入口（球 ⇄ 胶囊  面板 + 拖动 + 系统托盘）。

依赖只进 ``[desktop]`` 可选 extra；内核 ``src/yai_core`` 仍零第三方硬依赖。

本包**惰性导出**：``shell.desktop.federation_view``（纯 Python 的跨核聚合）不依赖 PySide6，
CI 里没装 ``[desktop]`` 也能直接 import；只有真正取 ``QtChannel`` / ``WorkbenchRuntime``
等符号时才加载 Qt 模块。
"""

from __future__ import annotations

import importlib
from typing import Any

__all__ = [
    "PERMISSION_MODES",
    "PROMPT_TIMEOUT_SECONDS",
    "QtChannel",
    "SpecialistListModel",
    "SpecialistRuntime",
    "SpecialistSpec",
    "WorkbenchRuntime",
]

_EXPORTS = {
    "PROMPT_TIMEOUT_SECONDS": "channel",
    "QtChannel": "channel",
    "PERMISSION_MODES": "specialist",
    "SpecialistRuntime": "specialist",
    "SpecialistSpec": "specialist",
    "SpecialistListModel": "workbench",
    "WorkbenchRuntime": "workbench",
}


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(f"{__name__}.{module_name}")
    value = getattr(module, name)
    globals()[name] = value  # 缓存，后续访问不再走 __getattr__
    return value
