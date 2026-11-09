"""QuickStart 示例必须永远可跑：README 第一屏的承诺不能烂掉。

用子进程真实执行 `examples/quickstart.py`（离线、零 Key），断言输出里出现
路由 / 工具调用 / 最终结果三段，确保示例与内核 API 不漂移。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_quickstart_example_runs_offline() -> None:
    # ``PYTHONIOENCODING`` + ``-X utf8`` + 显式 UTF-8 解码：Windows CI 的默认控制台编码是
    # charmap，子进程直接 print 中文会 UnicodeEncodeError，与示例本身无关。
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    completed = subprocess.run(
        [sys.executable, "-X", "utf8", str(ROOT / "examples" / "quickstart.py")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    output = completed.stdout
    assert "工具（自动内省）" in output
    assert "[路由]" in output
    assert "[工具] search_notes" in output
    assert "[工具] archive_note" in output
    assert "[完成]" in output
    assert "本周周报 3 条" in output
