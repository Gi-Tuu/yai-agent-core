"""OpenAICompatEmbedder 离线测试：用 fake client 验证契约，不发起任何网络请求。"""

import asyncio
import random

from yai_core.integrations.embedding import OpenAICompatEmbedder


class _Item:
    def __init__(self, index: int, embedding: list[float]) -> None:
        self.index = index
        self.embedding = embedding


class _Resp:
    def __init__(self, items: list[_Item]) -> None:
        self.data = items


class _FakeEmbeddings:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        items = [_Item(i, [float(i), 1.0]) for i in range(len(kwargs["input"]))]
        random.Random(0).shuffle(items)  # 故意打乱端点返回顺序
        return _Resp(items)


class _FakeClient:
    def __init__(self) -> None:
        self.embeddings = _FakeEmbeddings()


def _embedder(**kwargs) -> OpenAICompatEmbedder:
    emb = OpenAICompatEmbedder(
        api_key="test",
        base_url="https://example.invalid/v1",
        model="fake-embed",
        **kwargs,
    )
    emb.client = _FakeClient()  # 替换为不联网的 fake
    return emb


def test_empty_input_returns_empty_without_call() -> None:
    emb = _embedder()
    assert asyncio.run(emb.embed_texts([])) == []
    assert emb.client.embeddings.calls == []


def test_results_reordered_by_index() -> None:
    emb = _embedder()
    vecs = asyncio.run(emb.embed_texts(["a", "b", "c"]))
    assert vecs == [[0.0, 1.0], [1.0, 1.0], [2.0, 1.0]]


def test_model_passed_through() -> None:
    emb = _embedder()
    asyncio.run(emb.embed_texts(["x"]))
    assert emb.client.embeddings.calls[0]["model"] == "fake-embed"


def test_dimensions_passed_only_when_set() -> None:
    emb_with_dim = _embedder(dimensions=64)
    asyncio.run(emb_with_dim.embed_texts(["x"]))
    assert emb_with_dim.client.embeddings.calls[0]["dimensions"] == 64

    emb_no_dim = _embedder()
    asyncio.run(emb_no_dim.embed_texts(["x"]))
    assert "dimensions" not in emb_no_dim.client.embeddings.calls[0]
