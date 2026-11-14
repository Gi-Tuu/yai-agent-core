"""LoCoMo 词法 vs 语义对照：纯函数 + 假嵌入回归（不触网、不加载模型）。"""

from __future__ import annotations

import pytest

np = pytest.importorskip("numpy")

from benchmarks.memory import locomo_embedding_bench as bench  # noqa: E402

FIXTURE = "tests/fixtures/locomo_mini.json"


async def fake_embed(texts: list[str]) -> list[list[float]]:
    vocab = ["apples", "pears", "paris", "violin", "rome", "alice", "bob", "carol"]
    return [
        [1.0 if word in text.lower() else 0.0 for word in vocab]
        for text in texts
    ]


def test_cosine_ranking_and_normalization() -> None:
    matrix = bench.normalize_rows([[1.0, 0.0], [0.0, 1.0], [1.0, 1.0]])
    assert bench.cosine_ranking(np.array([1.0, 0.0]), matrix)[0] == 0


def test_rrf_prefers_items_seen_by_both_lists() -> None:
    merged = bench.rrf_merge(["a", "b", "c"], ["c", "d", "b"], k=3)
    assert merged[0] in {"b", "c"}
    assert set(merged[:3]) == {"b", "c", "a"} or set(merged[:3]) == {"b", "c", "d"}


def test_evaluate_with_fake_embedding() -> None:
    dataset = bench.load_dataset(__import__("pathlib").Path(FIXTURE))
    report = bench.evaluate(dataset, ks=(5,), embed_fn=fake_embed)
    assert report["qa_evaluated"] == 3
    for method in ("lexical", "semantic", "hybrid"):
        assert report["metrics"][method]["recall@5"] == 1.0
        assert report["metrics"][method]["mrr"] == 1.0
