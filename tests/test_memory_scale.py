"""记忆规模基准的离线回归：默认只跑 100 条（1000+ 需显式，O(n²) 去重很慢）。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_scale_benchmark_100_offline() -> None:
    completed = subprocess.run(
        [sys.executable, "benchmarks/memory/scale_bench.py", "--sizes", "100", "--queries", "10"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    lines = [line for line in completed.stdout.splitlines() if line.startswith("{")]
    report = json.loads(lines[-1])
    row = report["results"][0]
    assert row["notes"] == 100
    assert row["stored_notes"] >= 80, "去重最多允许少量合并；大量合并说明语料不够多样"
    assert row["build_ms"] > 0
    assert row["retrieval_p50_ms"] >= 0
    assert row["retrieval_p95_ms"] >= row["retrieval_p50_ms"]
