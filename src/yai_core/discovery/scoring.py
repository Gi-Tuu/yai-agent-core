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
