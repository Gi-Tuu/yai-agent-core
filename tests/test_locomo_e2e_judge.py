"""LoCoMo 评分增强：时序归一化与 judge 解析的离线回归。"""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

import pytest

pytest.importorskip("numpy")

from benchmarks.memory import locomo_e2e_judge as judge  # noqa: E402

FIXTURE = Path("tests/fixtures/locomo_mini.json")


def test_parse_anchor_and_relative_dates() -> None:
    anchor = judge.parse_anchor("1:56 pm on 8 May, 2023")
    assert anchor == date(2023, 5, 8)
    assert judge.canonical_time("Yesterday", anchor) == ("date", date(2023, 5, 7))
    assert judge.canonical_time("Last year", anchor) == ("year", 2022)
    assert judge.canonical_time("7 May 2023", anchor) == ("date", date(2023, 5, 7))
    assert judge.canonical_time("no date here", anchor) is None


def test_temporal_scores_accept_equivalent_dates() -> None:
    anchor = date(2023, 5, 8)
    assert judge.temporal_scores("Yesterday", "7 May 2023", anchor)[:2] == (1.0, 1.0)
    assert judge.temporal_scores("Last year", "2022", anchor)[:2] == (1.0, 1.0)
    assert judge.temporal_scores("2021", "2022", anchor)[:2] == (0.0, 0.0)
    # 非时间答案退回原始 F1/EM
    f1, em, used = judge.temporal_scores("Paris", "Paris", anchor)
    assert (f1, em, used) == (1.0, 1.0, False)


def test_judge_yes_parser() -> None:
    assert judge._judge_yes("YES") is True  # noqa: SLF001
    assert judge._judge_yes("No.") is False  # noqa: SLF001
    assert judge._judge_yes("") is False  # noqa: SLF001


def test_evaluate_with_fake_judge() -> None:
    dataset = judge.load_dataset(FIXTURE)
    predictions = [
        {"prediction": "apples", "context_ids": ["D1:1"]},
        {"prediction": "Paris", "context_ids": ["D1:2", "D2:2"]},
        {"prediction": "violin", "context_ids": ["D1:1"]},
    ]

    async def yes_judge(_messages: list[dict[str, str]]) -> str:
        return "YES"

    report = asyncio.run(judge.evaluate(dataset, predictions, judge_fn=yes_judge))
    assert report["qa_scored"] == 3
    assert report["answerable"]["judge_accuracy"] == 1.0
    assert report["overall"]["temporal_used"] >= 0
