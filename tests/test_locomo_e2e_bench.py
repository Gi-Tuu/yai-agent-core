"""LoCoMo 端到端问答：假模型/假嵌入离线回归。"""

from __future__ import annotations

from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

from benchmarks.memory import locomo_e2e_bench as bench  # noqa: E402

FIXTURE = Path("tests/fixtures/locomo_mini.json")


async def fake_embed(texts: list[str]) -> list[list[float]]:
    vocab = ["apples", "pears", "paris", "violin", "rome", "alice", "bob", "carol"]
    return [[1.0 if word in text.lower() else 0.0 for word in vocab] for text in texts]


async def fake_answer(messages: list[dict[str, str]]) -> str:
    return "apples"


def test_normalization_and_f1() -> None:
    assert bench.f1_score("The apples.", "apples") == 1.0
    assert bench.exact_match("  Apples ", "apples") == 1.0
    assert bench.f1_score("banana", "apples") == 0.0


def test_end_to_end_pipeline_with_fake_model() -> None:
    report = bench.evaluate(
        bench.load_dataset(FIXTURE),
        answer_fn=fake_answer,
        k=5,
        retrieval="semantic",
        embed_fn=fake_embed,
    )
    assert report["qa_scored"] == 3
    assert report["qa_failed"] == 0
    assert report["metrics"]["retrieval_recall"] == 1.0
    assert len(report["predictions"]) == 3


def test_answer_errors_are_counted_not_raised() -> None:
    async def broken(_messages: list[dict[str, str]]) -> str:
        raise RuntimeError("model down")

    report = bench.evaluate(
        bench.load_dataset(FIXTURE),
        answer_fn=broken,
        k=5,
        embed_fn=fake_embed,
        concurrency=2,
    )
    assert report["qa_scored"] == 0
    assert report["qa_failed"] == 3
