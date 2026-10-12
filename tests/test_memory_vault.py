"""MemoryVault（结构化长期记忆）单元测试：纯标准库、离线、无 API Key。"""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from yai_core.memory.vault import MemoryVault


class TickClock:
    """每调用一次前进 1 秒的固定时钟，便于断言时间刷新。"""

    def __init__(self) -> None:
        self.t = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        now = self.t
        self.t += timedelta(seconds=1)
        return now


# ---------- 写入 ----------

def test_remember_sets_fields_and_defaults() -> None:
    vault = MemoryVault(clock=TickClock())
    note, merged = vault.remember("用户对芒果严重过敏", kind="fact", tags=["饮食"], importance=5)
    assert merged is False
    assert note.content == "用户对芒果严重过敏"
    assert note.kind == "fact"
    assert note.tags == ["饮食"]
    assert note.importance == 5
    assert note.id and note.access_count == 0


def test_remember_requires_non_empty_content() -> None:
    vault = MemoryVault()
    with pytest.raises(ValueError):
        vault.remember("   ")


def test_invalid_kind_falls_back_to_note() -> None:
    vault = MemoryVault()
    note, _ = vault.remember("一条内容", kind="not_a_kind")
    assert note.kind == "note"


def test_importance_clamped_and_invalid_defaults() -> None:
    vault = MemoryVault()
    assert vault.remember("c1", importance=9)[0].importance == 5
    assert vault.remember("c2", importance=0)[0].importance == 1
    assert vault.remember("c3", importance="x")[0].importance == 3


def test_tags_normalized_deduped_and_trimmed() -> None:
    vault = MemoryVault()
    note, _ = vault.remember("c", tags=["a", "a", " b ", None])  # type: ignore[list-item]
    assert note.tags == ["a", "b"]


def test_remember_duplicate_merges_tags_and_importance() -> None:
    vault = MemoryVault(clock=TickClock())
    first, m1 = vault.remember("用户对芒果严重过敏", kind="fact", tags=["饮食"], importance=3)
    assert m1 is False
    again, m2 = vault.remember("用户对芒果严重过敏", tags=["过敏"], importance=5)
    assert m2 is True
    # 合并而非新增。
    assert vault.status()["total"] == 1
    assert again.id == first.id
    assert set(again.tags) == {"饮食", "过敏"}
    assert again.importance == 5
    assert again.access_count == 1


# ---------- 检索 ----------

def _seed(vault: MemoryVault) -> None:
    vault.remember("用户对芒果严重过敏", kind="fact", tags=["过敏"], importance=5)
    vault.remember("用户喜欢深色模式", kind="preference", tags=["界面"], importance=2)
    vault.remember("用户是湛江人", kind="profile", tags=["地区"], importance=3)


def test_recall_ranks_most_relevant_first() -> None:
    vault = MemoryVault(clock=TickClock())
    _seed(vault)
    results = vault.recall("芒果过敏能吃什么")
    assert results
    assert "芒果" in results[0]["content"]
    assert "score" in results[0]


def test_recall_respects_k_limit() -> None:
    vault = MemoryVault()
    _seed(vault)
    assert len(vault.recall("用户", k=2)) == 2


def test_recall_filters_by_kinds() -> None:
    vault = MemoryVault()
    _seed(vault)
    results = vault.recall("用户", kinds=["preference"])
    assert results and all(r["kind"] == "preference" for r in results)


def test_recall_filters_by_tags() -> None:
    vault = MemoryVault()
    _seed(vault)
    results = vault.recall("用户", tags=["过敏"])
    assert results and all("过敏" in r["tags"] for r in results)


def test_recall_no_match_and_empty_vault_return_empty() -> None:
    assert MemoryVault().recall("随便查点什么") == []
    vault = MemoryVault()
    _seed(vault)
    assert vault.recall("zzzz qwerty 毫无关系") == []


def test_recall_touch_updates_access_count() -> None:
    vault = MemoryVault(clock=TickClock())
    _seed(vault)
    before = vault._notes[0].access_count  # noqa: SLF001
    vault.recall("芒果过敏")
    # 命中芒果条目后其访问计数 +1。
    mango = next(n for n in vault._notes if "芒果" in n.content)  # noqa: SLF001
    assert mango.access_count == before + 1


# ---------- 删除 / 状态 ----------

def test_forget_removes_note() -> None:
    vault = MemoryVault()
    note, _ = vault.remember("some content here")
    assert vault.forget(note.id) is True
    assert vault.forget(note.id) is False
    assert vault.status()["total"] == 0


def test_status_counts_by_kind_and_lists_recent() -> None:
    vault = MemoryVault()
    vault.remember("a fact", kind="fact")
    vault.remember("a preference", kind="preference")
    status = vault.status()
    assert status["total"] == 2
    assert status["by_kind"]["fact"] == 1
    assert status["by_kind"]["preference"] == 1
    assert len(status["recent"]) == 2


# ---------- 持久化 ----------

def test_persistence_roundtrip_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "memory.json"
    vault = MemoryVault(storage_path=path, clock=TickClock())
    vault.remember("用户对芒果过敏", kind="fact", tags=["过敏"], importance=5)
    assert path.exists()  # 父目录自动创建。

    reloaded = MemoryVault(storage_path=path)
    assert reloaded.status()["total"] == 1
    results = reloaded.recall("芒果过敏")
    assert results and results[0]["content"] == "用户对芒果过敏"
    assert results[0]["kind"] == "fact"


def test_corrupt_storage_cold_starts_tolerated(tmp_path: Path) -> None:
    path = tmp_path / "memory.json"
    path.write_text("{这不是合法 JSON", encoding="utf-8")
    vault = MemoryVault(storage_path=path)
    assert vault.status()["total"] == 0
    # 冷启动后可正常写入并覆盖坏文件。
    vault.remember("恢复后的内容")
    assert MemoryVault(storage_path=path).status()["total"] == 1


def test_in_memory_vault_writes_nothing(tmp_path: Path) -> None:
    vault = MemoryVault()
    vault.remember("纯内存内容")
    assert not list(tmp_path.iterdir())


# ---------- 语义召回（可选 EmbeddingProvider，离线 Fake） ----------

# 记忆场景的概念轴：命中任一触发词，该维度置 1。刻意同时放中文与英文触发词，
# 以便构造"词法零重叠、语义同轴"的 query，证明语义通道独立于字面匹配。
_MEMORY_AXES = (
    ("mango", ("芒果", "过敏", "mango", "allergy")),
    ("dark", ("深色", "暗色", "dark", "夜间模式", "夜间")),
    ("rain", ("下雨", "伞", "rain")),
)


class MemoryFakeEmbedder:
    """与 test_discovery_semantic.py 同款的确定性概念轴嵌入器。"""

    def __init__(self) -> None:
        self.calls = 0

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        vectors: list[list[float]] = []
        for text in texts:
            norm = text.lower()
            vectors.append(
                [1.0 if any(word in norm for word in words) else 0.0 for _, words in _MEMORY_AXES]
            )
        return vectors


class BoomEmbedder:
    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("embedding 服务不可用")


class ShortEmbedder:
    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return []  # 数量与记忆数不一致，触发"数量不符"降级分支


def test_arecall_without_embedder_equals_lexical() -> None:
    vault = MemoryVault(clock=TickClock())
    _seed(vault)
    lexical = vault.recall("芒果过敏能吃什么")
    semantic = asyncio.run(vault.arecall("芒果过敏能吃什么"))
    # 未配置 embedder：异步入口与同步词法结果逐条一致（id 与 score）。
    assert [(r["id"], r["score"]) for r in semantic] == [
        (r["id"], r["score"]) for r in lexical
    ]


def test_arecall_semantic_finds_when_lexical_zero_overlap() -> None:
    vault = MemoryVault(clock=TickClock(), embedder=MemoryFakeEmbedder())
    _seed(vault)
    # 纯英文 query：与中文记忆字面零重叠（词法通道召不回），但与"芒果过敏"同轴，
    # 余弦相似度为 1，语义通道应当把它找回。
    assert vault.recall("mango allergy safe?") == []  # 先证明词法确实召不回
    results = asyncio.run(vault.arecall("mango allergy safe?"))
    assert results, "语义通道应能召回词法零重叠但语义相关的记忆"
    assert "芒果" in results[0]["content"]


def test_arecall_unrelated_query_returns_empty() -> None:
    vault = MemoryVault(embedder=MemoryFakeEmbedder())
    _seed(vault)
    # 与三条记忆都不同轴（向量全 0），词法也无重叠：不召回。
    assert asyncio.run(vault.arecall("股票行情今天怎么样")) == []


def test_arecall_degrades_to_lexical_when_embedder_raises() -> None:
    vault = MemoryVault(embedder=BoomEmbedder())
    _seed(vault)
    # embedder 抛错：降级纯词法，词法命中的 query 仍能召回，且错误被记录可观测。
    results = asyncio.run(vault.arecall("芒果过敏"))
    assert results and "芒果" in results[0]["content"]
    assert vault.semantic_last_error is not None


def test_arecall_degrades_when_vector_count_mismatch() -> None:
    vault = MemoryVault(embedder=ShortEmbedder())
    _seed(vault)
    results = asyncio.run(vault.arecall("芒果过敏"))
    assert results and "芒果" in results[0]["content"]
    assert vault.semantic_last_error is not None


def test_forget_drops_cached_semantic_vector() -> None:
    vault = MemoryVault(embedder=MemoryFakeEmbedder())
    note, _ = vault.remember("用户对芒果严重过敏", kind="fact", tags=["过敏"], importance=5)
    # 首次语义召回建立向量缓存并命中。
    assert asyncio.run(vault.arecall("mango allergy"))
    assert note.id in vault._vectors  # noqa: SLF001
    # 删除后向量缓存一并清除，语义通道不再命中。
    assert vault.forget(note.id) is True
    assert note.id not in vault._vectors  # noqa: SLF001
    assert asyncio.run(vault.arecall("mango allergy")) == []


def test_merge_invalidates_cached_vector() -> None:
    embedder = MemoryFakeEmbedder()
    vault = MemoryVault(embedder=embedder)
    note, _ = vault.remember("用户对芒果严重过敏", kind="fact", tags=["过敏"])
    asyncio.run(vault.arecall("mango allergy"))  # 建立缓存
    assert note.id in vault._vectors  # noqa: SLF001
    # 再次写入同一条（合并）：旧向量失效，等待下次重算。
    vault.remember("用户对芒果严重过敏", tags=["饮食"])
    assert note.id not in vault._vectors  # noqa: SLF001


def test_status_reports_semantic_flag() -> None:
    assert MemoryVault().status()["semantic_enabled"] is False
    assert MemoryVault(embedder=MemoryFakeEmbedder()).status()["semantic_enabled"] is True
