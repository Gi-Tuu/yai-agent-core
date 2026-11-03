"""记忆检索质量指标（零依赖，供 benchmarks/memory 与宿主评估复用）。

指标口径写死在代码里，避免"评测脚本各算各的"：

- **Recall@k**：每个 query 的 top-k 里命中的相关记忆占该 query 全部相关记忆的比例，
  再对 query 取平均（缺失即 0，不把失败 query 从分母里去掉）。
- **MRR@k**：第一个相关结果排名的倒数（1/rank）；k 内没有相关结果为 0，再取平均。
- **无关召回率**：返回结果里不属于该 query 相关集合的比例（越低越好）。
- **P50 延迟**：逐 query 调用耗时的中位数（毫秒）。

注意：这些指标只衡量**给定语料与标注**下的检索质量；FakeEmbedding 上的分数
不能当作真实语义模型效果（真实 BGE-M3 需要本地模型文件，另行验证）。
"""

from __future__ import annotations

from collections.abc import Sequence
from statistics import median
from typing import Any


def recall_at_k(ranked_ids: Sequence[str], relevant_ids: Sequence[str], k: int) -> float:
    """Recall@k；相关集合为空返回 0（不让空标注白拿分）。"""
    relevant = set(relevant_ids)
    if not relevant or k <= 0:
        return 0.0
    hits = sum(1 for note_id in ranked_ids[:k] if note_id in relevant)
    return hits / len(relevant)


def reciprocal_rank_at_k(
    ranked_ids: Sequence[str], relevant_ids: Sequence[str], k: int
) -> float:
    """MRR@k：第一个相关结果排名的倒数。"""
    relevant = set(relevant_ids)
    if not relevant or k <= 0:
        return 0.0
    for rank, note_id in enumerate(ranked_ids[:k], start=1):
        if note_id in relevant:
            return 1.0 / rank
    return 0.0


def irrelevant_rate(
    ranked_ids: Sequence[str], relevant_ids: Sequence[str], k: int
) -> float:
    """top-k 里无关结果的占比。"""
    top = list(ranked_ids[:k])
    if not top:
        return 0.0
    relevant = set(relevant_ids)
    return sum(1 for note_id in top if note_id not in relevant) / len(top)


def percentile(values: Sequence[float], ratio: float) -> float:
    """简单最近秩百分位（不插值），ratio ∈ [0, 1]。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(ratio * (len(ordered) - 1))))
    return ordered[index]


def aggregate(
    per_query: Sequence[dict[str, Any]],
    *,
    k: int = 5,
    latencies_ms: Sequence[float] = (),
) -> dict[str, Any]:
    """把逐 query 结果聚合成报告字典。"""
    count = len(per_query)
    if count == 0:
        return {
            "queries": 0,
            "k": k,
            f"recall@{k}": 0.0,
            f"mrr@{k}": 0.0,
            "irrelevant_rate": 0.0,
            "latency_p50_ms": 0.0,
        }
    return {
        "queries": count,
        "k": k,
        f"recall@{k}": sum(item["recall"] for item in per_query) / count,
        f"mrr@{k}": sum(item["mrr"] for item in per_query) / count,
        "irrelevant_rate": sum(item["irrelevant"] for item in per_query) / count,
        "latency_p50_ms": percentile(list(latencies_ms), 0.5) if latencies_ms else 0.0,
        "latency_p95_ms": percentile(list(latencies_ms), 0.95) if latencies_ms else 0.0,
        "median_ms": median(list(latencies_ms)) if latencies_ms else 0.0,
    }
