"""LoCoMo 端到端评分的两个增强：**时序日期归一化**与 **LLM judge**。

原始 F1/EM 是词法重叠：模型答“Yesterday / Last year”时，即使和 gold 的绝对日期同义，
也会被判错。这里：

- **temporal**：把相对时间按证据轮次所在会话日期（LoCoMo `session_N_date_time`）归一成
  `YYYY-MM-DD / YYYY-MM / YYYY`，再与 gold 比较；非时间答案仍用原始 F1/EM；
- **judge**：用同一真实模型判「prediction 与 gold 是否语义等价」，输出 YES/NO；
  时间表达式在 judge prompt 里带上参考日期。

输入是 `locomo_e2e_bench.py --predictions` 产出的 JSONL，不重新生成回答，只增加评分调用。

运行::

    uv run python benchmarks/memory/locomo_e2e_judge.py \
        --data locomo10.json --predictions predictions.jsonl --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))
sys.path.insert(0, str(HERE))

from locomo_bench import load_dataset, session_turns  # noqa: E402
from locomo_e2e_bench import exact_match, f1_score  # noqa: E402

MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9, "october": 10,
    "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}
MONTH_RE = "|".join(MONTHS)
DATE_DAY_MONTH_YEAR = re.compile(rf"\b(\d{{1,2}})\s+({MONTH_RE})\.?,?\s+(\d{{4}})\b", re.I)
DATE_MONTH_DAY_YEAR = re.compile(rf"\b({MONTH_RE})\.?\s+(\d{{1,2}}),?\s+(\d{{4}})\b", re.I)
DATE_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
MONTH_YEAR = re.compile(rf"\b({MONTH_RE})\.?,?\s+(\d{{4}})\b", re.I)
YEAR = re.compile(r"\b(19|20)\d{2}\b")

JUDGE_PROMPT = """You are grading a memory QA answer.

Question: {question}
Gold answer: {gold}
Prediction: {prediction}
Reference date for relative time expressions: {anchor}

Decide whether the prediction is semantically equivalent to the gold answer for this question.
Rules:
- Ignore articles, punctuation, casing and word order.
- Accept synonyms and paraphrases that mean the same thing.
- For dates, accept a relative expression ("yesterday", "last year") when it resolves to the gold
  date relative to the reference date, and accept a year when the gold gives only a year.
- If the gold says the answer is not mentioned / unknown, accept an equivalent refusal.
Reply with exactly one word: YES or NO."""


def parse_anchor(text: str) -> date | None:
    """解析 LoCoMo 会话日期，如 ``1:56 pm on 8 May, 2023``。"""
    match = DATE_DAY_MONTH_YEAR.search(text) or DATE_MONTH_DAY_YEAR.search(text)
    if not match:
        return None
    groups = match.groups()
    if match.re is DATE_DAY_MONTH_YEAR:
        day, month, year = groups
    else:
        month, day, year = groups
    try:
        return date(int(year), MONTHS[month.lower()], int(day))
    except (KeyError, ValueError):
        return None


def canonical_time(text: str, anchor: date | None) -> tuple[str, Any] | None:
    """把日期/相对时间归一成 (kind, value)。无法识别返回 None。"""
    lower = text.lower().strip()

    if anchor:
        if re.search(r"\byesterday\b", lower):
            return ("date", anchor - timedelta(days=1))
        if re.search(r"\btoday\b", lower):
            return ("date", anchor)
        if re.search(r"\btomorrow\b", lower):
            return ("date", anchor + timedelta(days=1))
        if re.search(r"\blast year\b", lower):
            return ("year", anchor.year - 1)
        if re.search(r"\bthis year\b", lower):
            return ("year", anchor.year)
        if re.search(r"\bnext year\b", lower):
            return ("year", anchor.year + 1)
        if re.search(r"\blast week\b", lower):
            return ("date", anchor - timedelta(days=7))
        if re.search(r"\blast month\b", lower):
            return ("date", anchor - timedelta(days=30))
        years_ago = re.search(r"\b(?:an?|one|two|three|four|five|\d+)\s+years?\s+ago\b", lower)
        if years_ago:
            number = re.search(r"\d+", years_ago.group(0))
            amount = int(number.group(0)) if number else {
                "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5
            }.get(years_ago.group(0).split()[0], 1)
            return ("year", anchor.year - amount)

    match = DATE_ISO.search(lower)
    if match:
        try:
            return ("date", date(int(match.group(1)), int(match.group(2)), int(match.group(3))))
        except ValueError:
            pass

    match = DATE_DAY_MONTH_YEAR.search(lower)
    if match:
        day, month, year = match.groups()
        try:
            return ("date", date(int(year), MONTHS[month.lower()], int(day)))
        except (KeyError, ValueError):
            pass

    match = DATE_MONTH_DAY_YEAR.search(lower)
    if match:
        month, day, year = match.groups()
        try:
            return ("date", date(int(year), MONTHS[month.lower()], int(day)))
        except (KeyError, ValueError):
            pass

    match = MONTH_YEAR.search(lower)
    if match:
        month, year = match.groups()
        return ("month", (int(year), MONTHS[month.lower()]))

    match = YEAR.search(lower)
    if match:
        return ("year", int(match.group(0)))
    return None


def temporal_equivalent(left: tuple[str, Any], right: tuple[str, Any]) -> bool:
    if left[0] == "date" and right[0] == "date":
        return left[1] == right[1]
    if left[0] == "date" and right[0] == "year":
        return left[1].year == right[1]
    if left[0] == "year" and right[0] == "date":
        return right[1].year == left[1]
    if left[0] == "date" and right[0] == "month":
        return (left[1].year, left[1].month) == right[1]
    if left[0] == "month" and right[0] == "date":
        return (right[1].year, right[1].month) == left[1]
    if left[0] == "month" and right[0] == "year":
        return left[1][0] == right[1]
    if left[0] == "year" and right[0] == "month":
        return right[1][0] == left[1]
    return left[0] == right[0] and left[1] == right[1]


def temporal_scores(prediction: str, gold: str, anchor: date | None) -> tuple[float, float, bool]:
    """返回 (temporal_f1, temporal_em, used_temporal)；无法归一化时退回原始 F1/EM。"""
    left = canonical_time(prediction, anchor)
    right = canonical_time(gold, anchor)
    if left is None or right is None:
        return f1_score(prediction, gold), exact_match(prediction, gold), False
    matched = temporal_equivalent(left, right)
    return (1.0 if matched else 0.0), (1.0 if matched else 0.0), True


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def _build_anchors(dataset: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按数据集顺序列出可评估 QA 及证据会话的参考日期。"""
    jobs: list[dict[str, Any]] = []
    for sample in dataset:
        conversation = sample.get("conversation") or {}
        session_dates: dict[str, date | None] = {}
        for key, value in conversation.items():
            match = re.match(r"session_(\d+)_date_time$", key)
            if match and isinstance(value, str):
                session_dates[match.group(1)] = parse_anchor(value)
        turns = session_turns(conversation)
        note_set = {str(turn["dia_id"]) for turn in turns}
        for qa in sample.get("qa") or []:
            evidence = [str(item) for item in (qa.get("evidence") or [])]
            if not evidence or any(item not in note_set for item in evidence):
                continue
            session = re.match(r"D(\d+):", evidence[0])
            anchor = session_dates.get(session.group(1)) if session else None
            jobs.append(
                {
                    "question": str(qa.get("question") or ""),
                    "gold": str(qa.get("answer") or ""),
                    "category": str(qa.get("category")),
                    "evidence": evidence,
                    "anchor": anchor,
                }
            )
    return jobs


def _load_env() -> None:
    path = HERE.parents[1] / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _judge_prompt(question: str, gold: str, prediction: str, anchor: date | None) -> str:
    return JUDGE_PROMPT.format(
        question=question,
        gold=gold,
        prediction=prediction or "(empty)",
        anchor=anchor.isoformat() if anchor else "unknown",
    )


def _judge_yes(response: str) -> bool:
    first = response.strip().lower().split()
    return bool(first and first[0].strip(".,!") in {"yes", "true", "correct", "equivalent"})


async def evaluate(
    dataset: list[dict[str, Any]],
    predictions: list[dict[str, Any]],
    *,
    judge_fn: Any | None = None,
    concurrency: int = 8,
    limit: int | None = None,
) -> dict[str, Any]:
    anchors = _build_anchors(dataset)
    if len(predictions) != len(anchors):
        raise ValueError(
            f"predictions 行数 {len(predictions)} 与可评估 QA {len(anchors)} 不一致；"
            "请用同一数据集和同一跳过规则生成 predictions"
        )
    rows = [
        {**anchor, **prediction}
        for anchor, prediction in zip(anchors, predictions, strict=True)
    ]
    if limit:
        rows = rows[:limit]
    for row in rows:
        anchor = row["anchor"]
        f1, em, used = temporal_scores(row["prediction"], row["gold"], anchor)
        row["temporal_f1"] = f1
        row["temporal_em"] = em
        row["temporal_used"] = used
        row["raw_f1"] = f1_score(row["prediction"], row["gold"])
        row["raw_em"] = exact_match(row["prediction"], row["gold"])

    judge_results: list[bool] = []
    judge_error = 0
    if judge_fn is not None:
        started = time.perf_counter()
        semaphore = asyncio.Semaphore(max(1, concurrency))
        verdicts: list[bool | None] = [None] * len(rows)

        async def one(index: int, row: dict[str, Any]) -> None:
            async with semaphore:
                for attempt in range(3):
                    try:
                        response = await judge_fn(
                            [
                                {
                                    "role": "user",
                                    "content": _judge_prompt(
                                        row["question"],
                                        row["gold"],
                                        row["prediction"],
                                        row["anchor"],
                                    ),
                                }
                            ]
                        )
                        verdicts[index] = _judge_yes(response)
                        return
                    except Exception:  # noqa: BLE001
                        if attempt == 2:
                            verdicts[index] = None
                        else:
                            await asyncio.sleep(1.5 * (attempt + 1))

        await asyncio.gather(*(one(i, row) for i, row in enumerate(rows)))
        judge_results = [value for value in verdicts if value is not None]
        judge_error = len(verdicts) - len(judge_results)
        for row, value in zip(rows, verdicts, strict=True):
            row["judge_correct"] = value
        judge_ms = (time.perf_counter() - started) * 1000
    else:
        judge_ms = 0.0

    def aggregate(subset: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "n": len(subset),
            "raw_f1": _mean([row["raw_f1"] for row in subset]),
            "raw_em": _mean([row["raw_em"] for row in subset]),
            "temporal_f1": _mean([row["temporal_f1"] for row in subset]),
            "temporal_em": _mean([row["temporal_em"] for row in subset]),
            "temporal_used": sum(1 for row in subset if row["temporal_used"]),
            "judge_accuracy": _mean(
                [
                    1.0 if row.get("judge_correct") else 0.0
                    for row in subset
                    if row.get("judge_correct") is not None
                ]
            ),
            "judge_scored": sum(1 for row in subset if row.get("judge_correct") is not None),
        }

    answerable = [row for row in rows if row["category"] != "5"]
    per_category: dict[str, dict[str, Any]] = {}
    for row in rows:
        per_category.setdefault(row["category"], []).append(row)
    return {
        "qa_scored": len(rows),
        "judge_errors": judge_error,
        "timing_ms": {"judge": round(judge_ms, 2)},
        "overall": aggregate(rows),
        "answerable": aggregate(answerable),
        "per_category": {
            category: aggregate(subset) for category, subset in sorted(per_category.items())
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="LoCoMo 端到端评分：时序归一化 + LLM judge")
    parser.add_argument("--data", type=Path, default=Path("locomo10.json"))
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--no-judge", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    _load_env()
    dataset = load_dataset(args.data)
    predictions = [
        json.loads(line)
        for line in args.predictions.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    judge_fn = None
    if not args.no_judge:
        if not os.environ.get("OPENAI_API_KEY"):
            print("缺少 OPENAI_API_KEY；可加 --no-judge 只跑时序归一化", file=sys.stderr)
            return 2
        from yai_core.llm.openai_compat import OpenAICompatProvider

        provider = OpenAICompatProvider()

        async def judge(messages: list[dict[str, str]]) -> str:
            response = await provider.achat(messages)
            return response.content.strip()

        judge_fn = judge
    report = asyncio.run(
        evaluate(
            dataset,
            predictions,
            judge_fn=judge_fn,
            concurrency=args.concurrency,
            limit=args.limit,
        )
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for scope in ("overall", "answerable"):
            values = report[scope]
            print(
                f"{scope}: n={values['n']} rawF1={values['raw_f1']:.4f} "
                f"temporalF1={values['temporal_f1']:.4f} rawEM={values['raw_em']:.4f} "
                f"temporalEM={values['temporal_em']:.4f} judge={values['judge_accuracy']:.4f}"
            )
        for category, values in report["per_category"].items():
            print(
                f"cat {category}: n={values['n']} rawF1={values['raw_f1']:.3f} "
                f"temporalF1={values['temporal_f1']:.3f} judge={values['judge_accuracy']:.3f}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
