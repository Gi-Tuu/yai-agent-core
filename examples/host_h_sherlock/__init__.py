"""host_h 演示宿主：嵌入真实开源项目 sherlock（92.5k stars, MIT）。

我们**不改 sherlock 一行源码**，只在它外面包一个干净的查询函数。
这就是 YAI 主张的"零改造嵌入真实软件"：宿主原本是个命令行工具，
现在 Core 通过函数内省自动拿到 ``lookup_username``，用自然语言调度。

运行：.venv/Scripts/python.exe examples/host_h_sherlock/run.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# 允许直接运行：补 examples/ 与 src/。
_EXAMPLES = Path(__file__).resolve().parents[1]
_SRC = Path(__file__).resolve().parents[2] / "src"
for _p in (str(_EXAMPLES), str(_SRC)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
