"""#169：CROSS-CORE 面板离屏 QML 交互（子进程隔离，避免 Qt 退出拖垮 pytest）。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "tests" / "_qml_cross_core_probe.py"


def test_cross_core_offscreen_probe() -> None:
    pytest.importorskip("PySide6")
    env = os.environ.copy()
    env["QT_QPA_PLATFORM"] = "offscreen"
    completed = subprocess.run(
        [sys.executable, "-X", "utf8", str(PROBE)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "QML_OK" in completed.stdout
