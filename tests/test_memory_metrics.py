"""记忆质量指标与离线基准的回归测试（v0.26 Batch 6）。

指标函数用固定输入断言；基准脚本以子进程真跑（离线、~1s），守住：
- Fake 嵌入器下语义通道确实能找回词法零重叠的 query；
- 嵌入器故障时结果与纯词法完全一致；
- 更新 / 删除后索引与重建库一致；
- 基准输出字段齐全、可 JSON 解析。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from yai_core.evaluation.memory_metrics import (
    aggregate,
    irrelevant_rate,
    percentile,
    recall_at_k,
    reciprocal_rank_at_k,
)

ROOT = Path(__file__).resolve().parents[1]


# ---------- 指标函数 ----------


def test_recall_at_k() -> None:
    assert recall_at_k(["a", "b", "c"], ["a", "c"], 3) == 1.0
    assert recall_at_k(["a", "x", "y"], ["a", "c"], 3) == 0.5
    assert recall_at_k(["x", "y"], ["a"], 3) == 0.0
    assert recall_at_k([], [], 3) == 0.0  # 空标注不白拿分


def test_recall_respects_k_and_order() -> None:
    assert recall_at_k(["a", "b"], ["b"], 1) == 0.0
    assert recall_at_k(["a", "b"], ["b"], 2) == 1.0


def test_mrr_at_k() -> None:
    assert reciprocal_rank_at_k(["x", "a", "y"], ["a"], 5) == pytest.approx(0.5)
    assert reciprocal_rank_at_k(["a"], ["a"], 5) == 1.0
    assert reciprocal_rank_at_k(["x"], ["a"], 5) == 0.0


def test_irrelevant_rate() -> None:
    assert irrelevant_rate(["a", "x", "y", "z", "w"], ["a"], 5) == pytest.approx(0.8)
    assert irrelevant_rate([], ["a"], 5) == 0.0


def test_percentile_nearest_rank() -> None:
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert percentile(values, 0.5) == 3.0
    assert percentile(values, 0.95) == 5.0
    assert percentile([], 0.5) == 0.0


def test_aggregate_shape_and_empty() -> None:
    rows = [
        {"recall": 1.0, "mrr": 1.0, "irrelevant": 0.2},
        {"recall": 0.0, "mrr": 0.0, "irrelevant": 0.4},
    ]
    report = aggregate(rows, k=5, latencies_ms=[5.0, 15.0])
    assert report["recall@5"] == pytest.approx(0.5)
    assert report["mrr@5"] == pytest.approx(0.5)
    assert report["irrelevant_rate"] == pytest.approx(0.3)
    # 最近秩百分位：两个样本时 P50 取较低者。
    assert report["latency_p50_ms"] == pytest.approx(5.0)
    assert aggregate([], k=5)["queries"] == 0


# ---------- 基准脚本真跑 ----------


def test_benchmark_runs_offline_and_reports() -> None:
    completed = subprocess.run(
        [sys.executable, "benchmarks/memory/run_bench.py", "--json"],
        cwd=ROOT,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["dataset"]["notes"] >= 100
    assert report["dataset"]["queries"] >= 50
    assert report["dataset"]["warmup"] >= 1
    assert report["dataset"]["repeats"] >= 3
    assert report["lexical"]["latency_samples"] >= (
        report["dataset"]["queries"] * report["dataset"]["repeats"]
    )
    assert "latency_p95_ms" in report["lexical"]
    assert "cold_lexical_ms" in report["latency"]
    assert "lexical" in report and "semantic" in report
    # 语义双通道不应比纯词法差；假嵌入器下还应当更强。
    assert report["semantic"]["recall@5"] >= report["lexical"]["recall@5"]
    assert report["degraded_equals_lexical"] is True
    assert report["index_consistency"]["consistent"] is True
    assert report["dataset"]["embedder"] == "fake-concept-axes"
    # 至少一个"词法零重叠、语义命中"的 query，证明双通道确实互补。
    assert report["lexical_vs_semantic"]["semantic_only"] >= 1
    assert report["queries_hit_by_semantic_only"]


def test_dataset_labels_reference_existing_notes() -> None:
    sys.path.insert(0, str(ROOT / "benchmarks" / "memory"))
    from dataset import NOTES, QUERIES  # noqa: PLC0415

    keys = {item["key"] for item in NOTES}
    assert len(keys) == len(NOTES)
    for query in QUERIES:
        assert query["expected"], f"query {query['q']} 没有标注"
        assert set(query["expected"]) <= keys
