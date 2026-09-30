"""Shell 启动引导：把仓库 src/ 加入 sys.path，使薄壳可直接运行、无需安装。

shell 位于仓库顶层（``<ROOT>/shell``），故根目录是本文件的上一级。
"""

from __future__ import annotations

import sys
from pathlib import Path

#: 仓库根目录（shell/ 的上一级）。
ROOT = Path(__file__).resolve().parents[1]


def bootstrap() -> Path:
    """把 ``<ROOT>/src`` 放到 sys.path 最前，返回仓库根路径。"""
    src = ROOT / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    return ROOT
