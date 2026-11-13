"""LoCoMo 风格记忆基准的离线回归（合成 fixture，不触网）。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from benchmarks.memory import locomo_bench

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "locomo_mini.json"


def test_session_turns_and_vault_ingestion() -> None:
    dataset = locomo_bench.load_dataset(FIXTURE)
    turns = locomo_bench.session_turns(dataset[0]["conversation"])
    assert [turn["dia_id"] for turn in turns] == ["D1:1", "D1:2", "D2:1", "D2:2"]
    vault = locomo_bench.build_vault(turns)
    assert len(vault._notes) == 4  # noqa: SLF001 - 基准专用无去重 vault


def test_evaluate_metrics_on_fixture() -> None:
    report = locomo_bench.evaluate(locomo_bench.load_dataset(FIXTURE), ks=(5, 10))
    assert report["samples"] == 2
    assert report["qa_total"] == 4
    assert report["qa_evaluated"] == 3
    assert report["qa_skipped_missing_evidence"] == 1
    assert report["metrics"]["recall@5"] == 1.0
    assert report["metrics"]["recall@10"] == 1.0
    assert report["metrics"]["mrr"] == 1.0
    assert report["per_category"]["4"]["n"] == 2
    assert report["per_category"]["2"]["n"] == 1


def test_missing_evidence_is_skipped_not_counted() -> None:
    dataset = locomo_bench.load_dataset(FIXTURE)
    dataset[0]["qa"] = [{"question": "unanswerable", "evidence": ["D9:9"], "category": 5}]
    report = locomo_bench.evaluate(dataset, ks=(5,))
    assert report["qa_skipped_missing_evidence"] == 1
    assert report["qa_evaluated"] == 1


def test_cli_emits_json_report() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            str(ROOT / "benchmarks" / "memory" / "locomo_bench.py"),
            "--data",
            str(FIXTURE),
            "--json",
            "--ks",
            "5",
            "10",
        ],
        cwd=ROOT,
        capture_output=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["metrics"]["recall@5"] == 1.0
    assert report["dataset_info"]["sha256"]


def test_missing_dataset_fails_with_actionable_error(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            str(ROOT / "benchmarks" / "memory" / "locomo_bench.py"),
            "--data",
            str(tmp_path / "nope.json"),
        ],
        cwd=ROOT,
        capture_output=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert completed.returncode != 0
    assert "download" in completed.stderr.lower() or "download" in completed.stdout.lower()
