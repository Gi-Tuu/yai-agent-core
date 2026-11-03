"""浏览器能力（可选执行适配器，v0.26）。

独立性：本包**不进入内核硬依赖**，也不被 ``yai_core`` 顶层自动 import；宿主
要显式 ``from yai_core.integrations.browser import ...`` 才会加载。真实浏览器
需要可选依赖 ``playwright``（``pip install yai-agent-core[browser]``）；不装也能
用假后端跑测试，内核其它部分完全不受影响。
"""

from yai_core.integrations.browser.backend import BrowserBackend
from yai_core.integrations.browser.models import (
    ElementInfo,
    PageObservation,
    Snapshot,
    TabInfo,
)
from yai_core.integrations.browser.policy import (
    BrowserBackendError,
    BrowserError,
    BrowserPolicy,
    BrowserPolicyError,
    BrowserSecurityWarning,
    StaleElementRefError,
    origin_allowed,
    origin_of,
)
from yai_core.integrations.browser.provider import (
    CLICK,
    OBSERVE,
    OPEN,
    SELECT,
    SWITCH_TAB,
    TABS,
    TYPE,
    WAIT,
    BrowserProvider,
    build_browser_tools,
)

__all__ = [
    "BrowserBackend",
    "BrowserBackendError",
    "BrowserError",
    "BrowserPolicy",
    "BrowserPolicyError",
    "BrowserSecurityWarning",
    "BrowserProvider",
    "ElementInfo",
    "PageObservation",
    "Snapshot",
    "StaleElementRefError",
    "TabInfo",
    "OBSERVE",
    "CLICK",
    "TYPE",
    "SELECT",
    "TABS",
    "SWITCH_TAB",
    "WAIT",
    "OPEN",
    "build_browser_tools",
    "origin_allowed",
    "origin_of",
]
