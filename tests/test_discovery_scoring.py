"""词法打分器（discovery/scoring.py）离线测试：不依赖网络与 API Key。"""

from yai_core.discovery.scoring import (
    LexicalHit,
    canonicalize,
    lexical_score,
    normalize,
    token_similarity,
    tokenize,
)

# ---------- 规范化与分词 ----------

def test_normalize_nfkc_lowercase_and_punctuation() -> None:
    assert normalize("Ｗｅａｔｈｅｒ") == "weather"  # 全角转半角
    assert normalize("查一下，湛江天气！") == "查一下 湛江天气"
    assert normalize("") == ""
    assert normalize(None) == ""


def test_canonicalize_synonym_rewrite() -> None:
    # 带伞/下雨/气温 都归并到规范词"天气"
    assert "天气" in canonicalize("明天出门要不要带伞")
    assert "天气" in canonicalize("广州下雨吗")
    assert "天气" in canonicalize("现在气温多少")
    # 查一下 -> 搜索
    assert canonicalize("查一下股价") == "搜索股价"


def test_tokenize_chinese_unigram_and_bigram() -> None:
    unigrams, bigrams = tokenize("天气")
    assert {"天", "气"} <= unigrams
    assert bigrams == {"天气"}


def test_tokenize_latin_words() -> None:
    unigrams, bigrams = tokenize("get weather")
    assert {"get", "weather"} <= unigrams
    assert bigrams == set()  # 拉丁词不做相邻 bigram


# ---------- 相似度 ----------

def test_token_similarity_related_beats_unrelated() -> None:
    related = token_similarity("湛江天气", "查询指定城市的天气")
    unrelated = token_similarity("湛江天气", "录入一条销售订单")
    assert related > unrelated
    # query 含候选没有的"湛江"二字，对 query 归一后 bigram"天气"命中 → 0.4 合理
    assert related >= 0.4
    assert unrelated == 0.0


def test_token_similarity_empty_query_is_zero() -> None:
    assert token_similarity("", "天气查询") == 0.0


# ---------- 候选打分 ----------

def _weather_score(need: str, task: str = "") -> LexicalHit:
    return lexical_score(
        need,
        task,
        keywords=("天气", "气温", "weather"),
        name="get_weather",
        description="查询指定城市的实时天气",
    )


def test_keyword_hit_in_need() -> None:
    hit = _weather_score("天气查询能力")
    assert hit.keyword_hit is True
    assert hit.score >= 0.6


def test_keyword_hit_in_task() -> None:
    hit = _weather_score("某外部能力", task="帮我查一下湛江天气")
    assert hit.keyword_hit is True
    assert hit.score >= 0.6


def test_english_keyword_case_insensitive() -> None:
    hit = _weather_score("weather lookup")
    assert hit.keyword_hit is True


def test_synonym_rewrite_matches_without_literal_keyword() -> None:
    # 原文没有"天气"二字，但"带伞"经同义归并后命中天气组
    hit = _weather_score("想知道明天出门要不要带伞")
    assert hit.keyword_hit is True
    assert hit.score >= 0.6


def test_unrelated_gap_scores_low() -> None:
    hit = _weather_score("股票行情能力", task="查一下某股票价格")
    assert hit.keyword_hit is False
    assert hit.score < 0.35


def test_description_overlap_without_keyword_can_score() -> None:
    # 没有关键词命中，但复述了描述里的"城市/天气"，token 重叠应给非零分
    hit = lexical_score(
        "我想知道某个城市的实时天气情况",
        "",
        keywords=("xyzqwerty",),  # 不可能命中的关键词
        name="f",
        description="查询指定城市的实时天气",
    )
    assert hit.keyword_hit is False
    assert hit.token_sim > 0
    assert hit.score > 0


# ---------- 排序：相关候选应排在不相关候选前面 ----------

def test_related_candidate_ranks_first() -> None:
    candidates = [
        ("stock", ("股票", "股价"), "查询股票行情"),
        ("weather", ("天气", "气温"), "查询指定城市的实时天气"),
        ("order", ("订单", "下单"), "录入销售订单"),
    ]
    need, task = "出门装备建议", "明天去广州要不要带伞"
    scored = []
    for name, kws, desc in candidates:
        hit = lexical_score(need, task, keywords=kws, name=name, description=desc)
        scored.append((name, hit.score))
    scored.sort(key=lambda x: x[1], reverse=True)
    assert scored[0][0] == "weather"
