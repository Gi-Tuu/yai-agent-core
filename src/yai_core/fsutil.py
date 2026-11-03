"""小工具：跨平台原子替换重试。

Windows 上 ``os.replace`` 可能因目标文件被另一个进程短暂持有（索引器、杀毒、
并发读者）抛 ``PermissionError`` / WinError 5。原子替换本身仍必须保证“要么旧文件、
要么新文件”，所以只对**瞬态** PermissionError 做有界退避重试；其它错误立即上抛。
"""

from __future__ import annotations

import os
import time


def replace_with_retry(
    src: str,
    dst: str,
    *,
    attempts: int = 8,
    base_delay: float = 0.05,
) -> None:
    """``os.replace`` 的 Windows 共享冲突有界重试；最终失败原样抛出最后一个错误。"""
    last_error: PermissionError | None = None
    for attempt in range(max(1, attempts)):
        try:
            os.replace(src, dst)
            return
        except PermissionError as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(base_delay * (attempt + 1))
    assert last_error is not None
    raise last_error
