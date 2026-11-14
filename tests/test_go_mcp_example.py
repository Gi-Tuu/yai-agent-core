"""#168：Go 示例的无 Go 环境提示（离线）。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "examples" / "host_o_go_mcp" / "run.py"


def test_missing_go_returns_actionable_exit_code() -> None:
    env = os.environ.copy()
    env["PATH"] = ""
    completed = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert completed.returncode == 2
    assert "go.dev/dl" in (completed.stdout + completed.stderr)
