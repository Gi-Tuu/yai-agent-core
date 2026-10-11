"""词法相关度打分：语义发现的第一通道（零第三方依赖）。

``StaticCatalog`` 只做"关键词子串是否出现"的布尔判断，覆盖面窄：模型把缺口
写成"明天出门要不要带伞"，候选关键词写的是"天气"，字面不重合就召回不到。

本模块在不引入任何第三方依赖的前提下，把词法匹配升级为可排序的相关度分数：

- 规范化（NFKC 全角转半角、小写、折叠标点），对中文与拉丁词都稳定；
- 中文按"单字 + 相邻 bigram"建索引，拉丁/数字按词建索引，bigram 比单字更精确；
- 一个刻意保持很小的高置信同义词表（``DEFAULT_SYNONYM_GROUPS``），在归一阶段把
  "带伞/下雨/气温"等改写归并到"天气"，宿主可传入自己的组覆盖或扩展；
- 相关度 = 关键词命中（强信号）+ token 重叠（名称/描述/关键词的字面相似度）。

它解决的是"字面改写"，不解决真正的跨概念语义（"带伞"→"天气"靠同义词表，
"帮我决定出门装备"→"天气"仍无能为力）。后者由可选的 embedding 通道负责
（见 ``discovery/semantic.py``）。两者构成双通道，本模块永远可用、可离线测试。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from yai_core.types import ToolSpec

# 拉丁词/数字（NFKC + lower 之后），如 weather、q3、get_weather 归一后的 get weather。
_LATIN_WORD = re.compile(r"[a-z0-9]+")
_CJK_CHAR = re.compile(r"[一-鿿]")

# 高置信同义组：每组第一个词是规范词，其余在归一阶段被替换为规范词。
# 刻意保持很小且通用，避免主观大词表在召回阶段制造误命中；宿主可注入自己的组。
DEFAULT_SYNONYM_GROUPS: tuple[tuple[str, ...], ...] = (
    ("天气", "气温", "温度", "下雨", "降雨", "降水", "雨伞", "带伞", "淋雨", "weather"),
    ("搜索", "查询", "查找", "检索", "搜一下", "找一下", "查一下", "search", "find", "lookup"),
    ("创建", "新增", "新建", "添加", "录入", "登记", "create", "add"),
)

# 关键词命中是强信号（原 StaticCatalog 的唯一判据），给它较高权重；
# 其余权重留给名称/描述的 token 重叠，让"没有关键词但高度字面重合"也能被召回。
DEFAULT_KEYWORD_WEIGHT = 0.6


def normalize(text: str | None) -> str:
    """NFKC + 小写 + 标点折叠为空格，保留中文与拉丁字母数字。"""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text).lower()
    kept: list[str] = []
    for ch in text:
        if "一" <= ch <= "鿿" or ch.isalnum():
            kept.append(ch)
        else:
            kept.append(" ")
    return re.sub(r"\s+", " ", "".join(kept)).strip()


def canonicalize(
    text: str | None,
    groups: tuple[tuple[str, ...], ...] = DEFAULT_SYNONYM_GROUPS,
) -> str:
    """先规范化，再把各同义组的非规范词替换为组首规范词。

    query 与 candidate 都经过同一套归一时，"带伞"与描述里的"天气"会落到同一字面，
    后续 bigram/词重叠才能对齐。
    """
    norm = normalize(text)
    for group in groups:
        canon = normalize(group[0])
        if not canon:
            continue
        for word in group[1:]:
            w = normalize(word)
            if w and w != canon:
                norm = norm.replace(w, canon)
    return norm


def tokenize(text: str | None) -> tuple[set[str], set[str]]:
    """把（已规范化的）文本切成 (unigram 集合, bigram 集合)。

    - 拉丁/数字：按词（``get weather`` → {"get","weather"}）；
    - 中文：单字（{"天","气"}）+ 相邻 bigram（{"天气"}）。
    """
    norm = normalize(text)
    unigrams: set[str] = set(_LATIN_WORD.findall(norm))
    cjk = _CJK_CHAR.findall(norm)
    unigrams.update(cjk)
    bigrams = {cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1)}
    return unigrams, bigrams


def _overlap(query_tokens: set[str], candidate_tokens: set[str]) -> float:
    """对 query 归一的重叠系数：query 的 token 有多少能在候选里找到。"""
    if not query_tokens:
        return 0.0
    return len(query_tokens & candidate_tokens) / len(query_tokens)


def token_similarity(
    query: str,
    candidate: str,
    *,
    groups: tuple[tuple[str, ...], ...] = DEFAULT_SYNONYM_GROUPS,
) -> float:
    """query 与一段候选文本的字面相似度（0~1），bigram 权重高于单字。"""
    q_unigrams, q_bigrams = tokenize(canonicalize(query, groups))
    c_unigrams, c_bigrams = tokenize(canonicalize(candidate, groups))
    uni = _overlap(q_unigrams, c_unigrams)
    if not q_bigrams:
        return round(uni, 4)
    bi = _overlap(q_bigrams, c_bigrams)
    return round(0.4 * uni + 0.6 * bi, 4)


@dataclass(frozen=True)
class LexicalHit:
    """一次词法打分的结果。"""

    score: float
    """融合后的相关度（0~1），关键词命中权重 + token 重叠权重。"""

    keyword_hit: bool
    """是否有任一关键词在（归一后的）缺口/任务原文中命中。"""

    token_sim: float
    """名称/描述/关键词与 query 的字面相似度（0~1）。"""


def _squash(text: str) -> str:
    """去掉全部空白用于中文子串匹配，避免中文/拉丁边界的空格切断词。"""
    return normalize(text).replace(" ", "")


def lexical_score(
    need: str,
    task: str,
    *,
    keywords: tuple[str, ...] | list[str],
    name: str,
    description: str,
    groups: tuple[tuple[str, ...], ...] = DEFAULT_SYNONYM_GROUPS,
    keyword_weight: float = DEFAULT_KEYWORD_WEIGHT,
) -> LexicalHit:
    """对单个候选计算词法相关度。

    Args:
        need: 缺失能力的自然语言描述（路由的 missing_capability）。
        task: 触发缺口的原始任务文本。
        keywords: 候选登记的关键词。
        name: 候选工具名。
        description: 候选描述（docstring 或自定义覆盖）。
        groups: 同义词组，默认 ``DEFAULT_SYNONYM_GROUPS``。
        keyword_weight: 关键词命中的权重（0~1），其余给 token 重叠。
    """
    query = f"{need}\n{task}"
    query_squash = _squash(canonicalize(query, groups))

    keyword_hit = False
    for kw in keywords:
        kw_squash = _squash(canonicalize(kw, groups))
        if kw_squash and kw_squash in query_squash:
            keyword_hit = True
            break

    candidate_text = " ".join(
        part for part in (name or "", description or "", " ".join(keywords)) if part
    )
    token_sim = token_similarity(query, candidate_text, groups=groups)

    score = keyword_weight * (1.0 if keyword_hit else 0.0) + (
        1.0 - keyword_weight
    ) * token_sim
    return LexicalHit(
        score=round(score, 4),
        keyword_hit=keyword_hit,
        token_sim=token_sim,
    )


# --- 面向已构造 ToolSpec（远程 MCP/OpenAPI 工具）的相关性筛选 ----------------
# 远程 ToolSpec 不登记关键词，只有 name + description，词法筛选只能靠它们与缺口的
# token 重叠；阈值单独标定。核心安全口径是"保守收窄"：识别得出明确相关子集才
# 收窄，一个达标都没有就回退全量，绝不在不确定时把可能相关的工具筛掉。
# 双向词法（取 query→工具、工具→query 两方向较大者）实测：相关工具约 0.23~0.33，
# 无关工具 ≤0.11。阈值定 0.20：过阈值且与最高分边际足够（margin）才收窄；一个达标
# 都没有则回退全量。跌破阈值只会导致"不收窄、返回全量"，不会漏召回。
DEFAULT_SPEC_NEED_THRESHOLD = 0.20
DEFAULT_SPEC_MARGIN = 0.08
DEFAULT_REMOTE_TOP_K = 8


def spec_text(spec: ToolSpec) -> str:
    """远程工具用于相关性匹配的轻量文本：工具名 + 描述。"""
    return " ".join(part for part in (spec.name or "", spec.description or "") if part)


def _directed(
    a_unigrams: set[str],
    a_bigrams: set[str],
    b_unigrams: set[str],
    b_bigrams: set[str],
) -> float:
    """``a`` 的 token 有多少出现在 ``b``（对 a 归一），bigram 权重高于 unigram。"""
    uni = len(a_unigrams & b_unigrams) / len(a_unigrams) if a_unigrams else 0.0
    if not a_bigrams:
        return uni
    bi = len(a_bigrams & b_bigrams) / len(a_bigrams)
    return 0.4 * uni + 0.6 * bi


def spec_relevance(
    need: str,
    task: str,
    spec: ToolSpec,
    *,
    groups: tuple[tuple[str, ...], ...] = DEFAULT_SYNONYM_GROUPS,
) -> float:
    """缺口/任务与单个远程工具（name+description）的**双向**词法相关度（0~1）。

    单方向（对 query 归一）在 query 是两句自然语言、工具描述很短时会被严重稀释
    （实测相关工具仅约 0.17）。取 ``query→工具`` 与 ``工具→query`` 两方向的较大者：
    后者回答"这个工具讲的是不是这件事"，工具描述短、领域词集中，覆盖率高且稳定，
    因此对长短 query 都稳健，区分度（相关 vs 无关）明显。
    """
    q_unigrams, q_bigrams = tokenize(canonicalize(f"{need}\n{task}", groups))
    c_unigrams, c_bigrams = tokenize(canonicalize(spec_text(spec), groups))
    q_to_c = _directed(q_unigrams, q_bigrams, c_unigrams, c_bigrams)
    c_to_q = _directed(c_unigrams, c_bigrams, q_unigrams, q_bigrams)
    return round(max(q_to_c, c_to_q), 4)


def filter_specs_by_need(
    specs: list[ToolSpec],
    need: str,
    *,
    task: str,
    threshold: float = DEFAULT_SPEC_NEED_THRESHOLD,
    top_k: int = DEFAULT_REMOTE_TOP_K,
    margin: float = DEFAULT_SPEC_MARGIN,
    groups: tuple[tuple[str, ...], ...] = DEFAULT_SYNONYM_GROUPS,
) -> tuple[list[ToolSpec], bool, dict[str, float]]:
    """保守地按缺口收窄一组已构造工具。

    Args:
        specs: 已去重的候选 ToolSpec。
        need: 缺失能力的自然语言描述。
        task: 触发缺口的原始任务文本。
        threshold: 双向词法相关度门槛。
        top_k: 收窄时最多保留的工具数。
        margin: 只保留与最高分差距在 margin 内的领先者，防弱相关/噪声混入。
        groups: 词法归一同义词组。

    Returns:
        ``(selected, narrowed, scores)``：
        - 有工具过阈值：``narrowed=True``，保留"过阈值且与最高分同档(margin)"者，
          按相关度降序取 top_k；
        - 一个达标都没有：``narrowed=False``，原样返回全部（不因筛选漏召回）；
        - ``scores`` 为每个工具的相关度，便于宿主观测与测试。
    """
    if not specs:
        return [], False, {}
    scored = [
        (spec_relevance(need, task, spec, groups=groups), index, spec)
        for index, spec in enumerate(specs)
    ]
    scores = {spec.name: score for score, _, spec in scored}
    hits = [item for item in scored if item[0] >= threshold]
    if not hits:
        return list(specs), False, scores
    hits.sort(key=lambda item: (-item[0], item[1]))
    top_score = hits[0][0]
    keep = [item for item in hits if item[0] >= top_score - margin]
    selected = [spec for _, _, spec in keep[:top_k]]
    return selected, True, scores
