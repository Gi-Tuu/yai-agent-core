"""LoCoMo 端到端记忆问答：BGE-M3 召回 + 真实模型生成 + F1/EM。

与 v0.43 的纯检索对照不同，这里把「召回」接到「回答」：

1. 每段对话独立建库（关闭去重），bge-m3 语义召回 top-k（默认 k=5）；
2. 把召回轮次拼进 prompt，让真实模型**只依据这些片段**回答；
3. 用 SQuAD 风格 token F1 + Exact Match 对比 gold answer；
4. 同时报告检索 Recall@k、分类别指标、耗时与 token 用量。

需要真实模型 Key（读 `.env` / 环境变量，OpenAI 兼容）；数据集不随仓库分发。

运行::

    uv sync --extra local-embed --extra llm
    uv run python benchmarks/memory/locomo_e2e_bench.py --download --json
    uv run python benchmarks/memory/locomo_e2e_bench.py --limit-qa 50 --json   # 小样本试跑
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import string
import sys
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "src"))
sys.path.insert(0, str(HERE))

from locomo_bench import (  # noqa: E402
    LOCOMO_URL,
    _NoDedupeVault,
    download_dataset,
    load_dataset,
    retrieved_ids,
    session_turns,
    verify_dataset,
)
from locomo_embedding_bench import (  # noqa: E402
    cosine_ranking,
    normalize_rows,
    rrf_merge,
)

AnswerFn = Callable[[list[dict[str, str]]], Awaitable[str]]
EmbedFn = Callable[[list[str]], Awaitable[list[list[float]]]]

PROMPT = """You are a memory QA assistant.
Answer the question using ONLY the conversation excerpts below.
If the excerpts do not contain the answer, reply exactly: Not mentioned.
Keep the answer as short as possible (a name, date, number, or short phrase).

Excerpts:
{context}

Question: {question}
Answer:"""


def normalize_answer(text: str) -> str:
    text = text.lower()
    text = "".join(ch if ch not in string.punctuation else " " for ch in text)
    text = re.sub(r"\b(a|an|the)\b", " ", text)
    return " ".join(text.split())


def f1_score(prediction: str, gold: str) -> float:
    pred_tokens = normalize_answer(prediction).split()
    gold_tokens = normalize_answer(gold).split()
    if not pred_tokens or not gold_tokens:
        return float(pred_tokens == gold_tokens)
    common = Counter(pred_tokens) & Counter(gold_tokens)
    overlap = sum(common.values())
    if overlap == 0:
        return 0.0
    precision = overlap / len(pred_tokens)
    recall = overlap / len(gold_tokens)
    return 2 * precision * recall / (precision + recall)


def exact_match(prediction: str, gold: str) -> float:
    return float(normalize_answer(prediction) == normalize_answer(gold))


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


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


def _stratified_limit(jobs: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """按 category 轮转取样，避免只取到某一类。"""
    groups: dict[str, list[dict[str, Any]]] = {}
    for job in jobs:
        groups.setdefault(job["category"], []).append(job)
    ordered: list[dict[str, Any]] = []
    while len(ordered) < limit and any(groups.values()):
        for category in sorted(groups):
            if groups[category] and len(ordered) < limit:
                ordered.append(groups[category].pop(0))
    return ordered


def _build_jobs(
    dataset: list[dict[str, Any]],
    *,
    k: int,
    retrieval: str,
    max_samples: int | None,
    limit_qa: int | None,
    workers: int = 8,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from yai_core.integrations.embedding.local_bge import LocalBgeEmbedder

    embedder = LocalBgeEmbedder()
    embed_one = embedder._embed_one_sync  # noqa: SLF001

    def embed(texts: list[str]) -> list[list[float]]:
        if workers <= 1:
            return [embed_one(text) for text in texts]
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(embed_one, texts))

    samples = dataset[:max_samples] if max_samples else dataset
    jobs: list[dict[str, Any]] = []
    stats = {
        "samples": len(samples),
        "turns": 0,
        "skipped": 0,
        "timing": {"ingest_ms": 0.0, "embed_ms": 0.0},
    }
    for sample in samples:
        turns = session_turns(sample.get("conversation") or {})
        stats["turns"] += len(turns)
        if not turns:
            continue
        note_ids = [str(turn["dia_id"]) for turn in turns]
        note_set = set(note_ids)
        notes = [
            f"[{turn['dia_id']}] {turn.get('speaker', '')}: {turn.get('text', '')}"
            for turn in turns
        ]
        started = time.perf_counter()
        vault = _NoDedupeVault()
        vault.remember_many(
            [
                {"content": note, "kind": "note", "tags": [note_id]}
                for note, note_id in zip(notes, note_ids, strict=True)
            ]
        )
        stats["timing"]["ingest_ms"] += (time.perf_counter() - started) * 1000

        started = time.perf_counter()
        note_matrix = normalize_rows(embed(notes))
        stats["timing"]["embed_ms"] += (time.perf_counter() - started) * 1000
        questions = [str(qa.get("question") or "") for qa in sample.get("qa") or []]
        question_vectors = normalize_rows(embed(questions)) if questions else []

        for qa, question, vector in zip(
            sample.get("qa") or [], questions, question_vectors, strict=True
        ):
            evidence = [str(item) for item in (qa.get("evidence") or [])]
            if not evidence or any(item not in note_set for item in evidence):
                stats["skipped"] += 1
                continue
            lexical_ids = retrieved_ids(vault.recall(question, k=k))
            semantic_ids = [note_ids[i] for i in cosine_ranking(vector, note_matrix)[:k]]
            if retrieval == "lexical":
                context_ids = lexical_ids
            elif retrieval == "hybrid":
                context_ids = rrf_merge(lexical_ids, semantic_ids, k=k)
            else:
                context_ids = semantic_ids
            id_to_note = dict(zip(note_ids, notes, strict=True))
            context = "\n".join(id_to_note.get(note_id, "") for note_id in context_ids)
            jobs.append(
                {
                    "question": question,
                    "gold": str(qa.get("answer") or ""),
                    "category": str(qa.get("category")),
                    "evidence": evidence,
                    "context_ids": context_ids,
                    "prompt": PROMPT.format(context=context, question=question),
                }
            )
    if limit_qa:
        jobs = _stratified_limit(jobs, limit_qa)
    return jobs, stats


def evaluate(
    dataset: list[dict[str, Any]],
    *,
    answer_fn: AnswerFn,
    k: int = 5,
    retrieval: str = "semantic",
    max_samples: int | None = None,
    limit_qa: int | None = None,
    concurrency: int = 6,
    embed_fn: EmbedFn | None = None,
    workers: int = 8,
) -> dict[str, Any]:
    """`answer_fn` 注入真实模型或测试假模型；返回端到端指标。"""
    if embed_fn is not None:
        jobs, stats = _build_jobs_with_embed(
            dataset,
            k=k,
            retrieval=retrieval,
            max_samples=max_samples,
            limit_qa=limit_qa,
            embed_fn=embed_fn,
        )
    else:
        jobs, stats = _build_jobs(
            dataset,
            k=k,
            retrieval=retrieval,
            max_samples=max_samples,
            limit_qa=limit_qa,
            workers=workers,
        )

    started = time.perf_counter()
    results: list[dict[str, Any] | None] = [None] * len(jobs)
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def run_one(index: int, job: dict[str, Any]) -> None:
        async with semaphore:
            for attempt in range(3):
                try:
                    prediction = await answer_fn([{"role": "user", "content": job["prompt"]}])
                    results[index] = {**job, "prediction": prediction}
                    return
                except Exception as exc:  # noqa: BLE001
                    if attempt == 2:
                        results[index] = {**job, "prediction": "", "error": str(exc)[:200]}
                    else:
                        await asyncio.sleep(1.5 * (attempt + 1))

    async def run_all() -> None:
        await asyncio.gather(*(run_one(i, job) for i, job in enumerate(jobs)))

    if jobs:
        asyncio.run(run_all())
    answer_ms = (time.perf_counter() - started) * 1000

    rows = [row for row in results if row is not None]
    scored = [row for row in rows if "error" not in row]
    f1 = [f1_score(row["prediction"], row["gold"]) for row in scored]
    em = [exact_match(row["prediction"], row["gold"]) for row in scored]
    recall = [
        len(set(row["context_ids"]) & set(row["evidence"])) / len(row["evidence"])
        for row in scored
    ]

    per_category: dict[str, dict[str, Any]] = {}
    for row in scored:
        bucket = per_category.setdefault(row["category"], {"f1": [], "em": [], "recall": []})
        bucket["f1"].append(f1_score(row["prediction"], row["gold"]))
        bucket["em"].append(exact_match(row["prediction"], row["gold"]))
        bucket["recall"].append(
            len(set(row["context_ids"]) & set(row["evidence"])) / len(row["evidence"])
        )

    return {
        "dataset": "locomo10",
        "retrieval": retrieval,
        "k": k,
        "qa_total": len(jobs),
        "qa_scored": len(scored),
        "qa_failed": len(rows) - len(scored),
        "qa_skipped_missing_evidence": stats["skipped"],
        "metrics": {
            "f1": _mean(f1),
            "exact_match": _mean(em),
            "retrieval_recall": _mean(recall),
        },
        "per_category": {
            category: {
                "n": len(bucket["f1"]),
                "f1": _mean(bucket["f1"]),
                "exact_match": _mean(bucket["em"]),
                "retrieval_recall": _mean(bucket["recall"]),
            }
            for category, bucket in sorted(per_category.items())
        },
        "timing_ms": {
            **{key: round(value, 2) for key, value in stats["timing"].items()},
            "answer": round(answer_ms, 2),
        },
        "samples": stats["samples"],
        "turns": stats["turns"],
        "predictions": rows,
    }


def _build_jobs_with_embed(
    dataset: list[dict[str, Any]],
    *,
    k: int,
    retrieval: str,
    max_samples: int | None,
    limit_qa: int | None,
    embed_fn: EmbedFn,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """测试用：注入假嵌入，避免加载 bge-m3。"""
    samples = dataset[:max_samples] if max_samples else dataset
    jobs: list[dict[str, Any]] = []
    stats = {
        "samples": len(samples),
        "turns": 0,
        "skipped": 0,
        "timing": {"ingest_ms": 0.0, "embed_ms": 0.0},
    }
    for sample in samples:
        turns = session_turns(sample.get("conversation") or {})
        stats["turns"] += len(turns)
        if not turns:
            continue
        note_ids = [str(turn["dia_id"]) for turn in turns]
        note_set = set(note_ids)
        notes = [
            f"[{turn['dia_id']}] {turn.get('speaker', '')}: {turn.get('text', '')}"
            for turn in turns
        ]
        vault = _NoDedupeVault()
        vault.remember_many(
            [
                {"content": note, "kind": "note", "tags": [note_id]}
                for note, note_id in zip(notes, note_ids, strict=True)
            ]
        )
        note_matrix = normalize_rows(asyncio.run(embed_fn(notes)))
        questions = [str(qa.get("question") or "") for qa in sample.get("qa") or []]
        question_vectors = normalize_rows(asyncio.run(embed_fn(questions))) if questions else []
        for qa, question, vector in zip(
            sample.get("qa") or [], questions, question_vectors, strict=True
        ):
            evidence = [str(item) for item in (qa.get("evidence") or [])]
            if not evidence or any(item not in note_set for item in evidence):
                stats["skipped"] += 1
                continue
            lexical_ids = retrieved_ids(vault.recall(question, k=k))
            semantic_ids = [note_ids[i] for i in cosine_ranking(vector, note_matrix)[:k]]
            if retrieval == "lexical":
                context_ids = lexical_ids
            elif retrieval == "hybrid":
                context_ids = rrf_merge(lexical_ids, semantic_ids, k=k)
            else:
                context_ids = semantic_ids
            id_to_note = dict(zip(note_ids, notes, strict=True))
            context = "\n".join(id_to_note.get(note_id, "") for note_id in context_ids)
            jobs.append(
                {
                    "question": question,
                    "gold": str(qa.get("answer") or ""),
                    "category": str(qa.get("category")),
                    "evidence": evidence,
                    "context_ids": context_ids,
                    "prompt": PROMPT.format(context=context, question=question),
                }
            )
    if limit_qa:
        jobs = _stratified_limit(jobs, limit_qa)
    return jobs, stats


def main() -> int:
    parser = argparse.ArgumentParser(description="LoCoMo 端到端记忆问答（BGE-M3 + 真实模型）")
    parser.add_argument("--data", type=Path, default=Path("locomo10.json"))
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--samples", type=int, default=None)
    parser.add_argument("--limit-qa", type=int, default=None)
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument(
        "--retrieval", choices=("semantic", "lexical", "hybrid"), default="semantic"
    )
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--predictions", type=Path, default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    _load_env()
    if not os.environ.get("OPENAI_API_KEY"):
        print("缺少 OPENAI_API_KEY（.env 或环境变量）", file=sys.stderr)
        return 2
    if args.download and not args.data.exists():
        download_dataset(args.data)
    if not args.data.exists():
        raise SystemExit(f"找不到数据集：{args.data}；先 --download，或从 {LOCOMO_URL} 下载")

    from yai_core.llm.openai_compat import OpenAICompatProvider

    provider = OpenAICompatProvider()
    model_name = os.environ.get("LLM_MODEL", "unknown")

    async def answer(messages: list[dict[str, str]]) -> str:
        response = await provider.achat(messages)
        return response.content.strip()

    dataset = load_dataset(args.data)
    started = time.perf_counter()
    report = evaluate(
        dataset,
        answer_fn=answer,
        k=args.k,
        retrieval=args.retrieval,
        max_samples=args.samples,
        limit_qa=args.limit_qa,
        concurrency=args.concurrency,
        workers=args.workers,
    )
    report["model"] = model_name
    report["dataset_info"] = verify_dataset(args.data)
    report["timing_ms"]["total"] = round((time.perf_counter() - started) * 1000, 2)
    if args.predictions:
        args.predictions.write_text(
            "\n".join(
                json.dumps(
                    {
                        "question": row["question"],
                        "gold": row["gold"],
                        "prediction": row["prediction"],
                        "category": row["category"],
                        "context_ids": row["context_ids"],
                    },
                    ensure_ascii=False,
                )
                for row in report["predictions"]
            ),
            encoding="utf-8",
        )
        del report["predictions"]
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    print(
        f"model={model_name} retrieval={report['retrieval']} k={report['k']} "
        f"qa={report['qa_scored']}/{report['qa_total']}"
    )
    print(
        f"F1={report['metrics']['f1']:.4f} EM={report['metrics']['exact_match']:.4f} "
        f"retrieval_recall={report['metrics']['retrieval_recall']:.4f}"
    )
    for category, bucket in report["per_category"].items():
        print(
            f"  category {category}: n={bucket['n']} F1={bucket['f1']:.3f} "
            f"EM={bucket['exact_match']:.3f} recall={bucket['retrieval_recall']:.3f}"
        )
    print(f"timing: {report['timing_ms']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
