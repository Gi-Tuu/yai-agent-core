"""双通道语义工具目录：词法相关度 + 可选 embedding 语义检索。

在 ``StaticCatalog``（关键词子串，二值命中）之上，本实现把"发现哪个候选工具"
变成一个可排序的相关度问题，并有两条互补通道：

- 词法/Schema 通道（``scoring.lexical_score``，零依赖、永远可用）：关键词命中 +
  工具名/描述/关键词的中文 bigram、拉丁词重叠，外加一个小型同义词表。擅长精确
  专有名词（工具名、参数名、领域词）。
- 语义通道（可选 ``EmbeddingProvider``，第八个 SPI）：把缺口/任务与候选"摘要"
  分别编码为向量，算余弦相似度。擅长同义改写、跨语言、概括性描述（"明天出门
  怎么穿、要不要带外套"→ 天气）。

两层结构（大规模工具目录不爆上下文）：检索阶段只对 name + 一句话描述 + 关键词
这类轻量摘要做 embedding；只有相关度过阈值、进入 top-k 的候选，才调用
``build_spec`` 内省出完整 JSON Schema 交给内核注册。

红线：
- 内核不内置任何 embedding 实现、不依赖 numpy，余弦用标准库 ``math``；
- 不配置 embedder 时退化为纯词法通道（仍是对 StaticCatalog 的打分增强）；
- embedder 抛错时本次自动降级为纯词法，记录在 ``last_error``，不中断发现
  （内核另有一层"发现失败不致命"的兜底，这里是双保险）。
"""

from __future__ import annotations

import inspect
import math
from collections.abc import Iterable, Sequence

from yai_core.discovery.catalog import DiscoveredCandidate
from yai_core.discovery.introspect import build_spec
from yai_core.discovery.scoring import (
    DEFAULT_KEYWORD_WEIGHT,
    DEFAULT_SYNONYM_GROUPS,
    lexical_score,
)
from yai_core.types import ToolSpec

# 召回门槛（用本地 bge-m3 实测校准）：完全无关 query 余弦约 0.30~0.32，
# 语义相关（"出门/外套"→天气、"冷不冷"→天气）约 0.48~0.52，词法命中约 0.68。
# 因此 0.45 能放成语义改写、挡住明显无关。
DEFAULT_SCORE_THRESHOLD = 0.45
DEFAULT_TOP_K = 5
DEFAULT_SEMANTIC_WEIGHT = 0.5
# 多候选（≥3）时，语义 top1 必须比第二名高出该边际才放行，避免在一堆相似度
# 接近的候选里乱选（实测"订机票"对 order=0.517、对 task=0.51，边际仅 0.007，
# 应判为"没有真正匹配的工具"而不是误召回 order）。
DEFAULT_SEMANTIC_MARGIN = 0.05


def _docstring_summary(fn: object) -> str:
    """取函数 docstring 首段作为一句话摘要（与 build_spec 的描述口径一致）。"""
    doc = inspect.getdoc(fn) or ""
    return doc.split("\n\n", 1)[0].strip()


def _candidate_summary(cand: DiscoveredCandidate) -> str:
    """检索阶段使用的轻量摘要：工具名 + 一句话描述 + 关键词（不含完整 schema）。"""
    name = cand.spec_name()
    desc = cand.description or _docstring_summary(cand.fn)
    return " ".join(part for part in (name, desc, " ".join(cand.keywords)) if part)


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """标准库实现的余弦相似度，截断到 [0, 1]；维度不齐按共有长度计算。"""
    n = min(len(a), len(b))
    if n == 0:
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for i in range(n):
        x, y = a[i], b[i]
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    cos = dot / (math.sqrt(norm_a) * math.sqrt(norm_b))
    if cos < 0.0:
        return 0.0
    return 1.0 if cos > 1.0 else cos


class SemanticCatalog:
    """词法 + 语义双通道的工具发现目录（实现 ToolDiscovery 协议）。

    Args:
        candidates: 候选能力（函数 + 关键词），与 StaticCatalog 同构。
        embedder: 可选文本嵌入器；None 时只走词法通道。
        top_k: 单次最多注册的候选数。
        score_threshold: 融合分阈值，低于此值不注册。
        semantic_weight: 融合排序中语义通道的权重（0~1），其余给词法通道。
        keyword_weight: 词法通道内部关键词命中的权重。
        sem_min_margin: 候选数 ≥3 时，语义 top1 必须领先第二名的最小边际。
        synonym_groups: 词法归一使用的同义词组。
    """

    def __init__(
        self,
        candidates: Iterable[DiscoveredCandidate],
        embedder: object | None = None,
        *,
        top_k: int = DEFAULT_TOP_K,
        score_threshold: float = DEFAULT_SCORE_THRESHOLD,
        semantic_weight: float = DEFAULT_SEMANTIC_WEIGHT,
        keyword_weight: float = DEFAULT_KEYWORD_WEIGHT,
        sem_min_margin: float = DEFAULT_SEMANTIC_MARGIN,
        synonym_groups: tuple[tuple[str, ...], ...] = DEFAULT_SYNONYM_GROUPS,
    ) -> None:
        self._candidates: list[DiscoveredCandidate] = list(candidates)
        self._embedder = embedder
        self.top_k = top_k
        self.score_threshold = score_threshold
        self.semantic_weight = semantic_weight
        self.keyword_weight = keyword_weight
        self.sem_min_margin = sem_min_margin
        self.synonym_groups = synonym_groups
        # 候选向量按 self._candidates 全序缓存，懒计算、只算一次。
        self._candidate_vectors: list[Sequence[float]] | None = None
        # 最近一次 embedder 降级原因（None 表示未降级），便于宿主观测与测试。
        self.last_error: str | None = None

    def _summary(self, cand: DiscoveredCandidate) -> str:
        return _candidate_summary(cand)

    async def _embed_candidates(self) -> list[Sequence[float]] | None:
        """懒加载并缓存候选摘要向量；失败返回 None（调用方降级为纯词法）。"""
        if self._candidate_vectors is not None:
            return self._candidate_vectors
        if self._embedder is None:
            return None
        summaries = [self._summary(c) for c in self._candidates]
        try:
            vectors = await self._embedder.embed_texts(summaries)
        except Exception as exc:  # noqa: BLE001 - 语义是增强，失败降级词法
            self.last_error = f"{type(exc).__name__}: {exc}"
            return None
        if not isinstance(vectors, list) or len(vectors) != len(self._candidates):
            self.last_error = "embedder 返回数量与候选数不一致"
            return None
        self._candidate_vectors = vectors
        self.last_error = None
        return vectors

    async def discover(
        self,
        need: str,
        *,
        task: str,
        available: list[str],
    ) -> list[ToolSpec]:
        already = set(available)
        query = f"{need}\n{task}"

        # 语义向量（query 每次不同，需现算；候选向量缓存）。任何失败都降级。
        query_vector: Sequence[float] | None = None
        candidate_vectors: list[Sequence[float]] | None = None
        if self._embedder is not None:
            candidate_vectors = await self._embed_candidates()
            if candidate_vectors is not None:
                try:
                    qv = await self._embedder.embed_texts([query])
                    if isinstance(qv, list) and len(qv) == 1:
                        query_vector = qv[0]
                except Exception as exc:  # noqa: BLE001 - 降级纯词法
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    query_vector = None
            if query_vector is None:
                candidate_vectors = None  # 任一侧失败，本次整体走词法

        # 先收集每个"可发现"候选的词法分与语义分。
        scored: list[tuple[float, float | None, str, DiscoveredCandidate]] = []
        for idx, cand in enumerate(self._candidates):
            name = cand.spec_name()
            if name in already:
                continue
            lex = lexical_score(
                need,
                task,
                keywords=cand.keywords,
                name=name,
                description=cand.description or _docstring_summary(cand.fn),
                groups=self.synonym_groups,
                keyword_weight=self.keyword_weight,
            )
            sem = (
                cosine_similarity(query_vector, candidate_vectors[idx])
                if candidate_vectors is not None and query_vector is not None
                else None
            )
            scored.append((lex.score, sem, name, cand))

        # 语义通道是"独立召回 gate"而非加权平均的一项：纯语义命中（词法分为 0）
        # 不应被 (1-w) 系数稀释。先在候选集上选出语义 top1，多候选时要求它对
        # 第二名有足够边际，避免在一堆相似度接近的候选里误召回。
        sem_pick_name: str | None = None
        sem_entries = [(i, s[1]) for i, s in enumerate(scored) if s[1] is not None]
        if sem_entries:
            top_i, top_sem = max(sem_entries, key=lambda kv: kv[1])
            runner_up = max(
                (s for j, s in sem_entries if j != top_i),
                default=None,
            )
            margin_ok = (
                len(sem_entries) < 3
                or runner_up is None
                or (top_sem - runner_up) >= self.sem_min_margin
            )
            if top_sem >= self.score_threshold and margin_ok:
                sem_pick_name = scored[top_i][2]

        # 召回：词法过阈值（高精度）OR 被语义通道选中（高召回）；融合分仅用于排序。
        best_by_name: dict[str, tuple[float, DiscoveredCandidate]] = {}
        for lex_score, sem, name, cand in scored:
            lex_pass = lex_score >= self.score_threshold
            sem_pass = sem_pick_name == name
            if not (lex_pass or sem_pass):
                continue
            final = (
                (1.0 - self.semantic_weight) * lex_score + self.semantic_weight * sem
                if sem is not None
                else lex_score
            )
            incumbent = best_by_name.get(name)
            if incumbent is None or final > incumbent[0]:
                best_by_name[name] = (final, cand)

        ranked = sorted(best_by_name.items(), key=lambda kv: kv[1][0], reverse=True)
        specs: list[ToolSpec] = []
        for _, (_, cand) in ranked[: self.top_k]:
            spec = build_spec(cand.fn, name=cand.name)
            if cand.description:
                spec.description = cand.description
            specs.append(spec)
        return specs
