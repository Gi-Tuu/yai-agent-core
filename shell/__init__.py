"""yai-shell：罩在 yai-agent-core 外的官方员工薄壳（浮窗 + 员工委派）。

- overlay：高动效 Web 浮窗（独立、可选的呈现层），宿主免写交互 UI；
- employee：员工/子员工层级委派（单一根、最小权限、预算、结果回流）。

薄壳 import yai_core，yai_core 不依赖薄壳；内核"零第三方硬依赖"红线不变，
浮窗与委派都不进入 src/yai_core。
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
