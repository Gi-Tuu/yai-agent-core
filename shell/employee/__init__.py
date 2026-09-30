"""员工薄壳 · 子员工委派编排（S2）。

单一根员工对用户负责，子员工是被收窄的短命执行者：工具白名单 ⊆ 根、权限单调
收窄、独立小上下文、带预算、结果只回流根、完成即销毁。
"""

from shell.employee.delegate import (
    DELEGATE_TOOL,
    attach_delegate,
    build_delegate_tool,
)
from shell.employee.worker import (
    DEFAULT_WORKER_ITERS,
    MAX_CONCURRENT,
    MAX_DEPTH,
    WorkerResult,
    build_worker_core,
    default_read_tools,
    run_worker,
)

__all__ = [
    "DELEGATE_TOOL",
    "DEFAULT_WORKER_ITERS",
    "MAX_CONCURRENT",
    "MAX_DEPTH",
    "WorkerResult",
    "attach_delegate",
    "build_delegate_tool",
    "build_worker_core",
    "default_read_tools",
    "run_worker",
]
