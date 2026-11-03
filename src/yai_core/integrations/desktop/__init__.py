"""Windows 桌面能力（可选执行适配器，v0.26 Batch 5）。

独立性：本包不进入内核硬依赖，也不被 ``yai_core`` 顶层自动 import。真实桌面
操作需要可选依赖 ``pywinauto``（``pip install yai-agent-core[windows]``）；
不装也能用假后端跑测试，内核其它部分完全不受影响。
"""

from yai_core.integrations.desktop.backend import DesktopBackend
from yai_core.integrations.desktop.models import (
    ControlInfo,
    DesktopObservation,
    DesktopSnapshot,
    WindowInfo,
)
from yai_core.integrations.desktop.policy import (
    HIGH_RISK_KEYWORDS,
    DesktopBackendError,
    DesktopError,
    DesktopPolicy,
    DesktopPolicyError,
    DesktopSecurityWarning,
    StaleControlRefError,
)
from yai_core.integrations.desktop.provider import (
    CLICK,
    FOCUS,
    INVOKE,
    LIST_WINDOWS,
    OBSERVE,
    PAUSE,
    RESUME,
    SELECT,
    SET_VALUE,
    DesktopProvider,
    build_desktop_tools,
)

__all__ = [
    "CLICK",
    "FOCUS",
    "HIGH_RISK_KEYWORDS",
    "INVOKE",
    "LIST_WINDOWS",
    "OBSERVE",
    "PAUSE",
    "RESUME",
    "SET_VALUE",
    "SELECT",
    "ControlInfo",
    "DesktopBackend",
    "DesktopBackendError",
    "DesktopError",
    "DesktopObservation",
    "DesktopPolicy",
    "DesktopPolicyError",
    "DesktopSecurityWarning",
    "DesktopProvider",
    "DesktopSnapshot",
    "StaleControlRefError",
    "WindowInfo",
    "build_desktop_tools",
]
