"""离线记忆质量基准（v0.26 Batch 6）。

测量口径（与 ``yai_core.evaluation.memory_metrics`` 一致）：
Recall@5 / MRR@5 / 无关召回率 / P50·P95 延迟；并给出：
- 词法与语义的互补收益（词法 0 命中但语义命中的 query 数，反之亦然）；
- 嵌入器故障时的降级行为（应等于纯词法）；
- 更新 / 删除后的索引一致性（与重建库对比）；
- 真实 BGE-M3 与假嵌入器的**明确区分**。

用法::

    python benchmarks/memory/run_bench.py                 # 假嵌入器（离线、确定性）
    python benchmarks/memory/run_bench.py --real-embed    # 需要 models/bge-m3 + local-embed extra
    python benchmarks/memory/run_bench.py --json          # 机器可读

注意：假嵌入器分数只证明"通道接线正确"，**不能**当作真实语义效果；真实 BGE 结果
依赖本机模型文件，CI 不跑，也不在文档里冒充 CI 结果。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dataset import NOTES, QUERIES, BrokenEmbedder, ConceptEmbedder  # noqa: E402

from yai_core.evaluation.memory_metrics import (  # noqa: E402
    aggregate,
    irrelevant_rate,
    recall_at_k,
    reciprocal_rank_at_k,
)
from yai_core.memory import MemoryVault  # noqa: E402

K = 5


def _build_vault(embedder: Any = None) -> tuple[MemoryVault, dict[str, str]]:
    vault = MemoryVault(embedder=embedder)
    mapping: dict[str, str] = {}
    for item in NOTES:
        note, _merged = vault.remember(
            item["content"],
            kind=item["kind"],
            tags=list(item["tags"]),
            importance=3,
        )
        mapping[note.id] = item["key"]
    return vault, mapping


async def _evaluate(
    vault: MemoryVault,
    mapping: dict[str, str],
    *,
    semantic: bool,
    repeats: int,
    warmup: int = 1,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """逐 query：先 warmup，再重复测量；延迟用**全部样本**算 P50/P95。"""
    per_query: list[dict[str, Any]] = []
    latencies: list[float] = []
    for item in QUERIES:
        for _ in range(max(0, warmup)):
            if semantic:
                await vault.arecall(item["q"], k=K)
            else:
                vault.recall(item["q"], k=K)
        ranked_keys: list[str] = []
        samples: list[float] = []
        for _ in range(max(1, repeats)):
            started = time.perf_counter()
            if semantic:
                results = await vault.arecall(item["q"], k=K)
            else:
                results = vault.recall(item["q"], k=K)
            samples.append((time.perf_counter() - started) * 1000)
            ranked_keys = [mapping.get(row["id"], "") for row in results]
        latencies.extend(samples)
        relevant = list(item["expected"])
        per_query.append(
            {
                "query": item["q"],
                "expected": relevant,
                "returned": ranked_keys,
                "recall": recall_at_k(ranked_keys, relevant, K),
                "mrr": reciprocal_rank_at_k(ranked_keys, relevant, K),
                "irrelevant": irrelevant_rate(ranked_keys, relevant, K),
            }
        )
    metrics = aggregate(per_query, k=K, latencies_ms=latencies)
    metrics["latency_samples"] = len(latencies)
    return metrics, per_query


async def _cold_start(vault: MemoryVault, *, semantic: bool) -> float:
    """首次调用（未预热）耗时；冷启动与热启动分开报告。"""
    started = time.perf_counter()
    if semantic:
        await vault.arecall(QUERIES[0]["q"], k=K)
    else:
        vault.recall(QUERIES[0]["q"], k=K)
    return (time.perf_counter() - started) * 1000

def _complement(
    lexical: list[dict[str, Any]], semantic: list[dict[str, Any]]
) -> dict[str, int]:
    lexical_only = sum(
        1
        for lex, sem in zip(lexical, semantic, strict=True)
        if lex["recall"] > 0 and sem["recall"] == 0
    )
    semantic_only = sum(
        1
        for lex, sem in zip(lexical, semantic, strict=True)
        if sem["recall"] > 0 and lex["recall"] == 0
    )
    both = sum(
        1
        for lex, sem in zip(lexical, semantic, strict=True)
        if sem["recall"] > 0 and lex["recall"] > 0
    )
    return {"lexical_only": lexical_only, "semantic_only": semantic_only, "both": both}


def _degenerate_equals_lexical(
    degraded: list[dict[str, Any]], lexical: list[dict[str, Any]]
) -> bool:
    return all(
        deg["returned"] == lex["returned"]
        for deg, lex in zip(degraded, lexical, strict=True)
    )


def _index_consistency() -> dict[str, Any]:
    """更新 / 删除后，索引召回应与"按最终内容重建的库"一致。"""
    vault, mapping = _build_vault()
    # 删除一条、合并一条（同主题合并会改写标签 / 重要度）。
    victim = next(note_id for note_id, key in mapping.items() if key == "phone_number")
    vault.forget(victim)
    vault.remember("用户偏好深色主题界面", kind="preference", tags=["主题", "可访问性"])

    rebuilt, rebuilt_mapping = _build_vault()
    # 重建库里同样去掉 phone_number（未插入即可，因为数据集本身含它）。
    rebuilt_victim = next(
        note_id for note_id, key in rebuilt_mapping.items() if key == "phone_number"
    )
    rebuilt.forget(rebuilt_victim)
    rebuilt_mapping.pop(rebuilt_victim, None)

    mismatched: list[str] = []
    for item in QUERIES:
        left = [mapping.get(row["id"], "") for row in vault.recall(item["q"], k=K)]
        right = [rebuilt_mapping.get(row["id"], "") for row in rebuilt.recall(item["q"], k=K)]
        if left != right:
            mismatched.append(item["q"])
    return {"consistent": not mismatched, "mismatched_queries": mismatched}


def _try_real_embedder() -> tuple[Any | None, str]:
    model_dir = ROOT / "models" / "bge-m3"
    if not (model_dir / "tokenizer.json").is_file():
        return None, "models/bge-m3 不存在（本地模型不随仓库分发）"
    try:
        from yai_core.integrations.embedding.local_bge import LocalBgeEmbedder
    except ImportError as exc:  # pragma: no cover - 取决于可选依赖
        return None, f"导入 LocalBgeEmbedder 失败：{exc}"
    try:
        return LocalBgeEmbedder(str(model_dir)), ""
    except Exception as exc:  # noqa: BLE001 - 缺 onnxruntime / tokenizers 等
        return None, f"初始化本地 BGE 失败：{type(exc).__name__}: {exc}"


async def main_async(args: argparse.Namespace) -> dict[str, Any]:
    report: dict[str, Any] = {
        "dataset": {
            "notes": len(NOTES),
            "queries": len(QUERIES),
            "k": K,
            "embedder": "real-bge-m3" if args.real_embed else "fake-concept-axes",
            "embedder_kind": "real" if args.real_embed else "fake",
            "device": "cpu",
            "warmup": args.warmup,
            "repeats": args.repeats,
        }
    }

    lexical_vault, lexical_map = _build_vault()
    cold_lexical = await _cold_start(lexical_vault, semantic=False)
    lexical_metrics, lexical_rows = await _evaluate(
        lexical_vault,
        lexical_map,
        semantic=False,
        repeats=args.repeats,
        warmup=args.warmup,
    )
    report["lexical"] = lexical_metrics

    if args.real_embed:
        embedder, reason = _try_real_embedder()
        if embedder is None:
            report["real_embed"] = {"skipped": True, "reason": reason}
            semantic_metrics, semantic_rows = None, None
        else:
            semantic_vault, semantic_map = _build_vault(embedder)
            cold_semantic = await _cold_start(semantic_vault, semantic=True)
            semantic_metrics, semantic_rows = await _evaluate(
                semantic_vault,
                semantic_map,
                semantic=True,
                repeats=args.repeats,
                warmup=args.warmup,
            )
            report["semantic"] = semantic_metrics
            report["real_embed"] = {"skipped": False, "model_dir": "models/bge-m3"}
    else:
        semantic_vault, semantic_map = _build_vault(ConceptEmbedder())
        cold_semantic = await _cold_start(semantic_vault, semantic=True)
        semantic_metrics, semantic_rows = await _evaluate(
            semantic_vault,
            semantic_map,
            semantic=True,
            repeats=args.repeats,
            warmup=args.warmup,
        )
        report["semantic"] = semantic_metrics

    degraded_vault, degraded_map = _build_vault(BrokenEmbedder())
    degraded_metrics, degraded_rows = await _evaluate(
        degraded_vault,
        degraded_map,
        semantic=True,
        repeats=args.repeats,
        warmup=args.warmup,
    )
    report["degraded_embedding"] = degraded_metrics

    if semantic_rows is not None:
        report["lexical_vs_semantic"] = _complement(lexical_rows, semantic_rows)
        report["degraded_equals_lexical"] = _degenerate_equals_lexical(
            degraded_rows, lexical_rows
        )
        report["semantic_last_error"] = degraded_vault.semantic_last_error
        report["queries_hit_by_semantic_only"] = [
            row["query"]
            for row in semantic_rows
            if row["recall"] > 0
            and next(
                lex["recall"]
                for lex in lexical_rows
                if lex["query"] == row["query"]
            )
            == 0
        ]
    report["latency"] = {
        "cold_lexical_ms": cold_lexical,
        "cold_semantic_ms": cold_semantic if args.real_embed else None,
        "warmup": args.warmup,
        "repeats": args.repeats,
        "note": "P50/P95 使用全部热启动样本；冷启动单独计时",
    }
    report["index_consistency"] = _index_consistency()
    return report


def _print_human(report: dict[str, Any]) -> None:
    dataset = report["dataset"]
    print(f"数据集：{dataset['notes']} 条记忆 / {dataset['queries']} 个 query / k={K}")
    print(f"嵌入器：{dataset['embedder']}\n")
    header = (
        f"{'模式':<12}{'Recall@5':>10}{'MRR@5':>10}{'无关率':>10}"
        f"{'P50(ms)':>10}{'P95(ms)':>10}"
    )
    print(header)
    print("-" * len(header))
    for label, key in (
        ("纯词法", "lexical"),
        ("语义(双通道)", "semantic"),
        ("嵌入降级", "degraded_embedding"),
    ):
        row = report.get(key)
        if not row:
            continue
        print(
            f"{label:<12}{row['recall@5']:>10.3f}{row['mrr@5']:>10.3f}"
            f"{row['irrelevant_rate']:>10.3f}{row['latency_p50_ms']:>10.2f}"
            f"{row['latency_p95_ms']:>10.2f}"
        )
    comp = report.get("lexical_vs_semantic")
    if comp:
        print(
            f"\n互补性：仅词法命中 {comp['lexical_only']} / "
            f"仅语义命中 {comp['semantic_only']} / 都命中 {comp['both']}"
        )
        print(f"语义独占命中 query：{report.get('queries_hit_by_semantic_only')}")
    print(
        "说明：无关召回率按'标注为必含相关'计算；标注不是穷尽集，"
        "语义通道会带出相关但未标注的条目。"
    )
    print(f"降级结果 == 纯词法：{report.get('degraded_equals_lexical')}")
    print(f"索引一致性：{report['index_consistency']}")
    real = report.get("real_embed")
    if real and real.get("skipped"):
        print(f"真实 BGE：未运行（{real['reason']}）")


def main() -> int:
    parser = argparse.ArgumentParser(description="离线记忆质量基准")
    parser.add_argument(
        "--real-embed",
        action="store_true",
        help="用本地 bge-m3（需模型文件与 [local-embed]）",
    )
    parser.add_argument(
        "--repeats", type=int, default=5, help="每个 query 热启动重复测量次数"
    )
    parser.add_argument(
        "--warmup", type=int, default=1, help="每个 query 预热次数（不计入 P50/P95）"
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    report = asyncio.run(main_async(args))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_human(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
