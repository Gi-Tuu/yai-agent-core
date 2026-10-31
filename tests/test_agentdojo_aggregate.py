"""agentdojo 聚合与分类的离线测试（纯函数，不 import agentdojo）。

对应 v0.25 B4 批次：重复采样聚合、逐用例四分类。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "benchmarks" / "agentdojo"))

from aggregate import aggregate_runs, classify_case, classify_counts


def _summary(utility: float | None, asr: float | None) -> dict:
    return {"utility": utility, "asr": asr, "injection_task_utility": None,
            "injection_task_count": 0, "cases": 10}


# ---------- aggregate_runs ----------

def test_aggregate_mean_and_std() -> None:
    out = aggregate_runs([_summary(0.8, 0.0), _summary(0.6, 0.2), _summary(1.0, 0.1)])
    assert out["runs"] == 3
    assert out["utility"] == pytest.approx(0.8, abs=1e-6)
    assert out["utility_std"] == pytest.approx(0.1633, abs=1e-3)
    assert out["asr"] == pytest.approx(0.1, abs=1e-6)


def test_aggregate_single_run_std_zero() -> None:
    out = aggregate_runs([_summary(0.9, 0.05)])
    assert out["utility"] == 0.9
    assert out["utility_std"] == 0.0


def test_aggregate_handles_missing_keys() -> None:
    out = aggregate_runs([_summary(None, None), _summary(None, None)])
    assert out["utility"] is None and out["utility_std"] is None


# ---------- classify_case / classify_counts ----------

def test_case_classification_four_ways() -> None:
    base = {"user_task": "t", "utility": True, "attack_success": False}
    assert classify_case({**base, "injection_task": "inj"}) == "defended_task_done"
    assert classify_case({**base, "injection_task": "inj", "attack_success": True}) == (
        "injection_achieved"
    )
    assert classify_case({**base, "injection_task": "inj", "utility": False}) == (
        "defended_task_failed"
    )
    assert classify_case({**base, "injection_task": None}) == "no_injection"


def test_classify_counts_all_keys_present() -> None:
    rows = [
        {"injection_task": "i", "utility": True, "attack_success": False},
        {"injection_task": "i", "utility": False, "attack_success": False},
        {"injection_task": "i", "utility": True, "attack_success": True},
        {"injection_task": None, "utility": True, "attack_success": False},
    ]
    counts = classify_counts(rows)
    assert counts == {
        "injection_achieved": 1,
        "defended_task_done": 1,
        "defended_task_failed": 1,
        "no_injection": 1,
    }
