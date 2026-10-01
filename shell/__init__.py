"""yai-shell：罩在 yai-agent-core 外的官方员工薄壳（原生界面 + 员工委派）。

- desktop：PySide6 + QML 原生桌面端（贴顶灵动岛胶囊 + 员工面板），
  内核与界面同进程，依赖只进 ``[desktop]`` 可选 extra；
- employee：员工/子员工层级委派（单一根、最小权限、预算、结果回流）。

薄壳 import yai_core，yai_core 不依赖薄壳；内核"零第三方硬依赖"红线不变，
界面与委派都不进入 src/yai_core。
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
