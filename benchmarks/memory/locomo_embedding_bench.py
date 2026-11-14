"""LoCoMo 记忆检索对照：纯词法 vs BGE-M3 语义 vs RRF 混合。

同一份 LoCoMo 数据、同一套证据口径，分别用：

- **lexical**：`MemoryVault.recall`（同步词法，现有基线）；
- **semantic**：本地 `LocalBgeEmbedder`（bge-m3 ONNX，CLS pooling + L2 norm）的余弦 top-k；
- **hybrid**：词法 top-k 与语义 top-k 的 Reciprocal Rank Fusion（RRF，k=60）。

数据集不随仓库分发；`--download` 从上游拉取并校验（同 ``locomo_bench.py``）。

运行::

    uv sync --extra local-embed
    uv run python benchmarks/memory/locomo_embedding_bench.py --download --json
    uv run python benchmarks/memory/locomo_embedding_bench.py --samples 1 --json
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import json
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import numpy as np

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

EmbedFn = Callable[[list[str]], Awaitable[list[list[float]]]]


def normalize_rows(vectors: list[list[float]]) -> np.ndarray:
    """L2 归一化后的 float32 矩阵。"""
    matrix = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return matrix / np.maximum(norms, 1e-9)


def cosine_ranking(query: np.ndarray, note_matrix: np.ndarray) -> list[int]:
    """按余弦相似度降序返回 note 下标（query 已归一化）。"""
    scores = note_matrix @ query.reshape(-1)
    return list(np.argsort(-scores))


def rrf_merge(lexical_ids: list[str], semantic_ids: list[str], *, k: int = 10, rrf_k: int = 60) -> list[str]:
    """Reciprocal Rank Fusion：两路 top-k 的排名倒数相加，稳定排序。"""
    scores: dict[str, float] = {}
    for rank, note_id in enumerate(lexical_ids[:k], start=1):
        scores[note_id] = scores.get(note_id, 0.0) + 1.0 / (rrf_k + rank)
    for rank, note_id in enumerate(semantic_ids[:k], start=1):
        scores[note_id] = scores.get(note_id, 0.0) + 1.0 / (rrf_k + rank)
    return [note_id for note_id, _ in sorted(scores.items(), key=lambda item: (-item[1], item[0]))]


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def evaluate(
    dataset: list[dict[str, Any]],
    *,
    ks: tuple[int, ...] = (5, 10),
    max_samples: int | None = None,
    limit_qa: int | None = None,
    embedder: Any | None = None,
    embed_fn: EmbedFn | None = None,
    workers: int = 8,
) -> dict[str, Any]:
    """跑三路检索；`embed_fn` 便于测试注入假嵌入，默认用 LocalBgeEmbedder。"""
    samples = dataset[:max_samples] if max_samples else dataset
    max_k = max(ks)
    methods = ("lexical", "semantic", "hybrid")
    recall: dict[str, dict[int, list[float]]] = {
        method: {k: [] for k in ks} for method in methods
    }
    mrr: dict[str, list[float]] = {method: [] for method in methods}
    per_category: dict[str, dict[str, list[float]]] = {}
    skipped = 0
    qa_total = 0
    evaluated = 0
    turns_total = 0
    timings: dict[str, float] = {"ingest_ms": 0.0, "embed_ms": 0.0, "retrieve_ms": 0.0}

    if embedder is None and embed_fn is None:
        from yai_core.integrations.embedding.local_bge import LocalBgeEmbedder

        embedder = LocalBgeEmbedder()
    embed_one = getattr(embedder, "_embed_one_sync", None)

    def _embed(texts: list[str]) -> list[list[float]]:
        if embed_fn is not None:
            return asyncio.run(embed_fn(texts))
        assert embed_one is not None
        if workers <= 1:
            return [embed_one(text) for text in texts]
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(embed_one, texts))

    for sample in samples:
        turns = session_turns(sample.get("conversation") or {})
        turns_total += len(turns)
        if not turns:
            continue
        note_ids = [str(turn["dia_id"]) for turn in turns]
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
        timings["ingest_ms"] += (time.perf_counter() - started) * 1000

        started = time.perf_counter()
        note_matrix = normalize_rows(_embed(notes))
        timings["embed_ms"] += (time.perf_counter() - started) * 1000

        questions = [str(qa.get("question") or "") for qa in sample.get("qa") or []]
        if limit_qa:
            questions = questions[:limit_qa]
        started = time.perf_counter()
        question_vectors = normalize_rows(_embed(questions)) if questions else []
        timings["embed_ms"] += (time.perf_counter() - started) * 1000

        for index, qa in enumerate((sample.get("qa") or [])[: len(questions)]):
            qa_total += 1
            evidence = [str(item) for item in (qa.get("evidence") or [])]
            if not evidence or any(item not in set(note_ids) for item in evidence):
                skipped += 1
                continue
            evaluated += 1
            evidence_set = set(evidence)
            category = str(qa.get("category"))
            bucket = per_category.setdefault(
                category,
                {
                    **{f"{m}_recall@{k}": [] for m in methods for k in ks},
                    **{f"{m}_mrr": [] for m in methods},
                },
            )

            started = time.perf_counter()
            lexical_ids = retrieved_ids(vault.recall(questions[index], k=max_k))
            semantic_order = cosine_ranking(question_vectors[index], note_matrix)
            semantic_ids = [note_ids[i] for i in semantic_order[:max_k]]
            ranked = {
                "lexical": lexical_ids,
                "semantic": semantic_ids,
                "hybrid": rrf_merge(lexical_ids, semantic_ids, k=max_k),
            }
            timings["retrieve_ms"] += (time.perf_counter() - started) * 1000

            for method, ids in ranked.items():
                for k in ks:
                    hit = len(set(ids[:k]) & evidence_set) / len(evidence)
                    recall[method][k].append(hit)
                    bucket[f"{method}_recall@{k}"].append(hit)
                rank = next(
                    (position + 1 for position, item in enumerate(ids) if item in evidence_set),
                    None,
                )
                value = 1.0 / rank if rank else 0.0
                mrr[method].append(value)
                bucket[f"{method}_mrr"].append(value)

    return {
        "dataset": "locomo10",
        "samples": len(samples),
        "turns": turns_total,
        "qa_total": qa_total,
        "qa_evaluated": evaluated,
        "qa_skipped_missing_evidence": skipped,
        "metrics": {
            method: {
                **{f"recall@{k}": _mean(recall[method][k]) for k in ks},
                "mrr": _mean(mrr[method]),
            }
            for method in methods
        },
        "per_category": {
            category: {
                **{
                    f"{method}_recall@{k}": _mean(bucket[f"{method}_recall@{k}"])
                    for method in methods
                    for k in ks
                },
                **{f"{method}_mrr": _mean(bucket[f"{method}_mrr"]) for method in methods},
                "n": len(bucket["lexical_mrr"]),
            }
            for category, bucket in sorted(per_category.items())
        },
        "timing_ms": {key: round(value, 2) for key, value in timings.items()},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="LoCoMo 词法 vs BGE-M3 语义对照")
    parser.add_argument("--data", type=Path, default=Path("locomo10.json"))
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--samples", type=int, default=None)
    parser.add_argument("--limit-qa", type=int, default=None)
    parser.add_argument("--ks", type=int, nargs="+", default=[5, 10])
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.download and not args.data.exists():
        download_dataset(args.data)
    if not args.data.exists():
        raise SystemExit(f"找不到数据集：{args.data}；先 --download，或从 {LOCOMO_URL} 下载")
    ks = tuple(sorted({k for k in args.ks if k > 0}))
    dataset = load_dataset(args.data)
    report = evaluate(
        dataset,
        ks=ks,
        max_samples=args.samples,
        limit_qa=args.limit_qa,
        workers=args.workers,
        embedder=None,
    )
    report["dataset_info"] = verify_dataset(args.data)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    for method in ("lexical", "semantic", "hybrid"):
        metrics = report["metrics"][method]
        print(
            f"{method:8s} "
            + " ".join(f"R@{k}={metrics[f'recall@{k}']:.4f}" for k in ks)
            + f" MRR={metrics['mrr']:.4f}"
        )
    print(
        f"{report['qa_evaluated']}/{report['qa_total']} QA 可评估 · "
        f"embed {report['timing_ms']['embed_ms']}ms · "
        f"retrieve {report['timing_ms']['retrieve_ms']}ms"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
