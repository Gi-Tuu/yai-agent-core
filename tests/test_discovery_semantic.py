"""SemanticCatalog 双通道语义发现离线测试。

FakeEmbedder 用确定性的"概念轴"向量：不联网、不需要 API Key，
但能真实复现"词法召不回、语义召得回"的通道差异。
"""

import asyncio

import pytest

from yai_core import (
    AgentCore,
    DiscoveredCandidate,
    ModelResponse,
    SemanticCatalog,
    ToolCallRequest,
    build_spec,
)
from yai_core.discovery.semantic import cosine_similarity

# ---------- 测试用能力 ----------

def get_weather(city: str) -> dict:
    """查询指定城市的实时天气。"""
    return {"city": city, "weather": "晴", "temp_c": 27}


def get_stock(code: str) -> dict:
    """查询股票实时行情与价格。"""
    return {"code": code, "price": 12.3}


def create_order(item: str) -> dict:
    """创建一条销售订单。"""
    return {"item": item, "id": 1}


def _candidates() -> list[DiscoveredCandidate]:
    return [
        DiscoveredCandidate(get_weather, ("天气", "气温")),
        DiscoveredCandidate(get_stock, ("股票", "股价")),
        DiscoveredCandidate(create_order, ("订单", "下单")),
    ]


# ---------- 确定性 Fake embedder：概念轴向量 ----------

_CONCEPT_AXES = (
    ("weather", ("天气", "带伞", "下雨", "气温", "外套", "出门", "weather", "伞")),
    ("stock", ("股票", "股价", "行情", "stock")),
    ("order", ("订单", "下单", "成交", "order")),
)


class FakeEmbedder:
    """每个概念轴一个维度：文本命中该轴任一触发词，该维度置 1。"""

    def __init__(self) -> None:
        self.calls = 0

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        vecs: list[list[float]] = []
        for text in texts:
            norm = text.lower()
            vec = [
                1.0 if any(word in norm for word in words) else 0.0
                for _, words in _CONCEPT_AXES
            ]
            vecs.append(vec)
        return vecs


class BoomEmbedder:
    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("embedding 服务不可用")


class ShortEmbedder:
    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return []  # 数量与候选不一致


# ---------- 余弦 ----------

def test_cosine_basic_properties() -> None:
    assert cosine_similarity([1, 0, 0], [1, 0, 0]) == 1.0
    assert cosine_similarity([1, 0], [0, 1]) == 0.0
    assert cosine_similarity([0, 0], [1, 1]) == 0.0  # 零向量
    assert cosine_similarity([1, 1, 0], [1, 1]) == pytest.approx(1.0)  # 维度不齐按共有长度
    assert cosine_similarity([], []) == 0.0


# ---------- 无 embedder：纯词法通道 ----------

def test_lexical_only_matches_keyword() -> None:
    cat = SemanticCatalog(_candidates())  # 无 embedder
    specs = asyncio.run(cat.discover("天气查询能力", task="", available=[]))
    assert [s.name for s in specs] == ["get_weather"]


def test_lexical_only_matches_stock() -> None:
    cat = SemanticCatalog(_candidates())
    specs = asyncio.run(cat.discover("股票行情能力", task="", available=[]))
    assert [s.name for s in specs] == ["get_stock"]


# ---------- 语义通道：词法召不回，语义召得回 ----------

def test_semantic_recalls_paraphrase_lexical_misses() -> None:
    embedder = FakeEmbedder()
    cat = SemanticCatalog(_candidates(), embedder)
    # 缺口/任务里没有"天气/气温"任一字样，词法通道（含默认同义词表）召不回；
    # 但"出门/外套"在语义轴上指向天气。
    specs = asyncio.run(
        cat.discover("出门该怎么穿、要不要带外套", task="", available=[])
    )
    assert [s.name for s in specs] == ["get_weather"]


def test_semantic_ranks_weather_above_others() -> None:
    cat = SemanticCatalog(_candidates(), FakeEmbedder())
    specs = asyncio.run(
        cat.discover("出门要不要带外套", task="", available=[])
    )
    assert specs[0].name == "get_weather"
    assert "get_stock" not in [s.name for s in specs]
    assert "create_order" not in [s.name for s in specs]


def test_candidate_vectors_are_cached() -> None:
    embedder = FakeEmbedder()
    cat = SemanticCatalog(_candidates(), embedder)
    asyncio.run(cat.discover("天气", task="", available=[]))
    asyncio.run(cat.discover("天气", task="", available=[]))
    # 每次 discover 只应为 query 调一次 embed；候选摘要两次查询只 embed 一次。
    # 第 1 次：候选 3 + query 1 = 2 次调用；第 2 次：仅 query = 1 次。
    assert embedder.calls == 3


# ---------- 去重 / 跳过 / top_k ----------

def test_skips_available() -> None:
    cat = SemanticCatalog(_candidates(), FakeEmbedder())
    specs = asyncio.run(
        cat.discover("天气", task="", available=["get_weather"])
    )
    assert specs == []


def test_dedupes_same_name() -> None:
    cands = [
        DiscoveredCandidate(get_weather, ("天气",)),
        DiscoveredCandidate(get_weather, ("气温",)),
    ]
    cat = SemanticCatalog(cands, FakeEmbedder())
    specs = asyncio.run(cat.discover("天气", task="", available=[]))
    assert [s.name for s in specs] == ["get_weather"]


def test_top_k_limits_results() -> None:
    # 三个候选都命中同一 query（关键词都在），top_k=1 只保留最高分一个。
    cands = [
        DiscoveredCandidate(get_weather, ("通用",)),
        DiscoveredCandidate(get_stock, ("通用",)),
        DiscoveredCandidate(create_order, ("通用",)),
    ]
    cat = SemanticCatalog(cands, top_k=1)
    specs = asyncio.run(cat.discover("通用能力", task="", available=[]))
    assert len(specs) == 1


# ---------- embedder 故障：降级为纯词法，不致命 ----------

def test_embedder_error_falls_back_to_lexical() -> None:
    cat = SemanticCatalog(_candidates(), BoomEmbedder())
    specs = asyncio.run(cat.discover("天气查询能力", task="", available=[]))
    assert [s.name for s in specs] == ["get_weather"]  # 词法仍召回
    assert cat.last_error is not None
    assert "不可用" in cat.last_error


def test_embedder_wrong_count_falls_back_to_lexical() -> None:
    cat = SemanticCatalog(_candidates(), ShortEmbedder())
    specs = asyncio.run(cat.discover("天气查询能力", task="", available=[]))
    assert [s.name for s in specs] == ["get_weather"]
    assert cat.last_error is not None


# ---------- 闭环：语义缺口 → 发现 → 注册 → 本轮使用 ----------

class _SemanticGapModel:
    """第 1 次分类报"语义天气缺口"（无天气字面），第 2 次调用，第 3 次收尾。"""

    def __init__(self) -> None:
        self.calls = 0

    async def achat(self, messages, tools=None, *, tier="standard"):
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                content='{"strategy":"react","tier":"standard","reason":"缺天气",'
                '"missing_capability":"出门该怎么穿、要不要带外套"}'
            )
        if self.calls == 2:
            return ModelResponse(
                content="",
                tool_calls=[ToolCallRequest(
                    id="w1", name="get_weather", arguments={"city": "湛江"}
                )],
            )
        return ModelResponse(content="湛江当前晴，27℃，出门可不带外套。")


def _search_notes(keyword: str) -> list:
    """搜索宿主笔记库。"""
    return []


def test_semantic_gap_discovers_and_uses_tool_in_same_run() -> None:
    cat = SemanticCatalog(_candidates(), FakeEmbedder())
    core = AgentCore(_SemanticGapModel(), llm_router=True, discovery=cat)
    core.register_tools([build_spec(_search_notes)])

    result = asyncio.run(core.run("帮我看看出门该怎么穿、要不要带外套"))

    types = [e.type.value for e in result.events]
    assert "capability_missing" in types
    discovered = next(e for e in result.events if e.type.value == "tool_discovered")
    assert discovered.data["registered"] == ["get_weather"]
    assert discovered.data["source"] == "SemanticCatalog"
    call = next(e for e in result.events if e.type.value == "tool_call")
    assert call.data["tool"] == "get_weather"
    assert "27" in result.final_text
