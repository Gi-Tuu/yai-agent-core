"""MemoryVault（结构化长期记忆）单元测试：纯标准库、离线、无 API Key。"""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from yai_core.discovery.scoring import token_similarity
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


# ---------- 规模治理：词法倒排索引（与全表扫描逐字节等价） ----------


def _seed_mixed_corpus(vault: MemoryVault) -> None:
    """灌入一批中英混合、含同义词与无关项的确定性语料。"""
    vault.remember("用户对芒果严重过敏，聚餐要避开芒果", kind="fact", tags=["饮食"], importance=5)
    vault.remember("明天下午三点开团队站会，记得同步进度", kind="episode", tags=["会议"])
    vault.remember("偏好深色界面，代码字体用 JetBrains Mono", kind="preference", tags=["界面"])
    vault.remember("the weather forecast calls for heavy rain tomorrow", kind="note")
    vault.remember("quarterly budget report is due by end of month", kind="note")
    vault.remember("用户养了一只叫团子的橘猫，今年三岁", kind="fact", tags=["宠物"])
    vault.remember("部署上线前要跑一遍冒烟测试和回滚预案", kind="note", tags=["运维"])
    vault.remember("她出门总记得带伞，因为常忘看天气预报", kind="episode")


def _brute_force_lexical(vault: MemoryVault, query: str, *, kinds=None, tags=None):
    """不使用倒排索引的参考实现：对全部记忆逐条算 token_similarity（再过 filter）。"""
    hits = {}
    for note in vault._notes:  # noqa: SLF001
        if not MemoryVault._matches_filters(note, kinds, tags):  # noqa: SLF001
            continue
        sim = token_similarity(query, note.candidate_text())
        if sim > 0:
            hits[note.id] = sim
    return hits


def test_inverted_index_matches_brute_force_across_queries() -> None:
    vault = MemoryVault(clock=TickClock())
    _seed_mixed_corpus(vault)
    queries = [
        "芒果过敏吃什么",                       # 中文 + 同义词归一
        "明天天气怎么样，会下雨吗",            # "天气/雨" 命中天气条与带伞条（canonicalize）
        "站会几点开",                           # 会议条
        "weather forecast rain",               # 英文词命中
        "季度预算报告什么时候交",              # 中文问英文条：无字面交集，应都不命中
        "!!!???",                               # 切不出 token：倒排回退全扫，结果为空
        "与上述内容完全无关的量子隧穿推导",     # 无命中
    ]
    for query in queries:
        indexed = {note.id: sim for note, sim in vault._rank_lexical(  # noqa: SLF001
            query, kinds=None, tags=None
        )}
        brute = _brute_force_lexical(vault, query)
        assert indexed == brute, f"倒排与全扫在 query={query!r} 上不一致"


def test_inverted_index_respects_kinds_tags_filters() -> None:
    vault = MemoryVault(clock=TickClock())
    _seed_mixed_corpus(vault)
    for query, kinds, tags in [
        ("芒果过敏", ["fact"], None),
        ("天气", None, ["运维"]),
        ("站会", ["episode"], None),
    ]:
        indexed = {note.id: sim for note, sim in vault._rank_lexical(  # noqa: SLF001
            query, kinds=kinds, tags=tags
        )}
        brute = _brute_force_lexical(vault, query, kinds=kinds, tags=tags)
        assert indexed == brute


def test_lex_index_lazily_built_and_invalidated_by_writes() -> None:
    vault = MemoryVault(clock=TickClock())
    vault.remember("用户对芒果严重过敏", kind="fact")
    assert vault._lex_index is None  # noqa: SLF001  写入后懒失效，尚未构建。
    vault.recall("芒果过敏")
    assert vault._lex_index is not None  # noqa: SLF001  召回触发构建。
    note_id = next(iter(vault._lex_index.get("芒果", set())))  # noqa: SLF001
    assert note_id  # 倒排链确实挂到了对应 note。
    vault.forget(note_id)
    assert vault._lex_index is None  # noqa: SLF001  删除后失效，下次召回重建。


def _brute_force_duplicate(vault: MemoryVault, content: str):
    """不使用倒排索引的重复检测参考：全表逐条打分，取 >= 合并阈值且最高者。"""
    from yai_core.memory.vault import DEDUP_SIMILARITY

    best = None
    best_sim = 0.0
    for note in vault._notes:  # noqa: SLF001
        sim = token_similarity(content, note.candidate_text())
        if sim >= DEDUP_SIMILARITY and sim > best_sim:
            best, best_sim = note, sim
    return best


def test_find_duplicate_inverted_index_matches_brute_force() -> None:
    """写入路径的重复检测复用倒排索引后，结果必须与全表扫描逐条一致。"""
    vault = MemoryVault(clock=TickClock())
    _seed_mixed_corpus(vault)
    probes = [
        "用户对芒果严重过敏，聚餐要避开芒果",          # 与芒果条完全一致
        "明天下午三点开团队站会，记得同步进度",          # 与站会条完全一致
        "用户养了一只叫团子的橘猫，今年三岁",
        "部署上线前要跑一遍冒烟测试和回滚预案",
        "the weather forecast calls for heavy rain tomorrow",
        "quarterly budget report is due by end of month",
        "芒果过敏，聚餐别上芒果",                        # 近改写，可能命中芒果条
        "与上述内容完全无关的量子隧穿推导",              # 零公共 token，绝不能误合并
        "!!!???",                                       # 切不出 token：回退全扫
    ]
    for content in probes:
        indexed = vault._find_duplicate(content, [])  # noqa: SLF001
        brute = _brute_force_duplicate(vault, content)
        assert (indexed.id if indexed is not None else None) == (
            brute.id if brute is not None else None
        ), f"重复检测倒排与全扫在 {content!r} 上不一致"


# ---------- 规模治理：可选 max_notes 容量上限（默认 None 不删数据） ----------


def test_max_notes_evicts_low_importance_then_oldest() -> None:
    vault = MemoryVault(clock=TickClock(), max_notes=3)
    vault.remember("第一条低优先的闲聊", importance=1)
    vault.remember("第二条普通记录", importance=3)
    vault.remember("第三条普通记录", importance=3)
    vault.remember("第四条普通记录", importance=3)
    vault.remember("第五条最重要的事实", importance=5)
    status = vault.status()
    assert status["total"] == 3
    assert status["capacity"] == {"max_notes": 3, "evicted": 2}
    kept = {n.content for n in vault._notes}  # noqa: SLF001
    # 重要度最低 + 同重要度里最旧的两条先淘汰，高重要度与较新的保留。
    assert "第五条最重要的事实" in kept
    assert "第一条低优先的闲聊" not in kept
    assert "第二条普通记录" not in kept
    assert "第三条普通记录" in kept and "第四条普通记录" in kept


def test_max_notes_recently_used_survives_over_idle_note() -> None:
    clock = TickClock()
    vault = MemoryVault(clock=clock, max_notes=3)
    vault.remember("最旧但稍后会被重新翻看的记录", importance=3)
    vault.remember("中间一条一直没人碰的记录", importance=3)
    vault.remember("较新的记录", importance=3)
    # 重新使用最旧那条：last_used_at 刷新到当前（k=1 只触碰它，query 足够独特）。
    touched = vault.recall("最旧但稍后会被重新翻看的记录", k=1)
    assert touched and "最旧" in touched[0]["content"]
    vault.remember("第四条新记录把容量顶到超限", importance=3)
    kept = {n.content for n in vault._notes}  # noqa: SLF001
    # 同重要度下"最久未用"先删：一直闲置的中间条被淘汰，最近用过的最旧条留下。
    assert "中间一条一直没人碰的记录" not in kept
    assert "最旧但稍后会被重新翻看的记录" in kept
    assert "较新的记录" in kept
    assert "第四条新记录把容量顶到超限" in kept


# 30 个用字与主题都明显不同的短语，避免模板句高度重叠被 0.92 去重误合并。
_DISTINCT_PHRASES = [
    "芒果过敏要避开甜品", "团队站会改到周四", "偏好深色代码界面",
    "明天有暴雨记得关窗", "季度预算报告待审批", "橘猫团子今年三岁",
    "上线前先跑冒烟测试", "她出门总带着折叠伞", "吉他爬格子每天二十分钟",
    "马拉松配速降到六分", "手冲咖啡水温九十二度", "书架按出版社重新分类",
    "航班提前二十四小时值机", "牙套每月复诊一次", "阳台多肉每周浇透",
    "周末和棋友复盘残局", "钢琴中央 C 键走音了", "简历投了三家初创公司",
    "房租每月一号自动转账", "护照有效期不足半年", "冷冻室积霜需要化冻",
    "晨起做十分钟猫式拉伸", "照片每周备份到移动硬盘", "新眼镜配防蓝光镜片",
    "无人签收的包裹已拒收", "聚餐定在巷口火锅店", "胎压不足就近补气",
    "签证要准备银行流水", "路由器每周重启一次", "洗衣液快用完记得补货",
]


def test_max_notes_default_none_never_evicts() -> None:
    vault = MemoryVault(clock=TickClock())  # 默认不限制。
    for phrase in _DISTINCT_PHRASES:
        vault.remember(phrase)
    status = vault.status()
    assert status["total"] == len(_DISTINCT_PHRASES)
    assert status["capacity"] == {"max_notes": None, "evicted": 0}


def test_max_notes_converges_existing_store_on_reload(tmp_path) -> None:
    path = tmp_path / "vault.json"
    first = MemoryVault(storage_path=path, clock=TickClock())  # 先不设上限写 5 条。
    for phrase in _DISTINCT_PHRASES[:5]:
        first.remember(phrase)
    assert first.status()["total"] == 5
    # 宿主后来把上限调到 2 重开：启动即收敛并落盘。
    second = MemoryVault(storage_path=path, max_notes=2)
    assert second.status()["total"] == 2
    assert second.status()["capacity"]["evicted"] == 3
    # 收敛结果确实已写进文件：再用一个无上限 vault 打开，看到的就是 2 条。
    third = MemoryVault(storage_path=path)
    assert third.status()["total"] == 2


def test_max_notes_merge_does_not_evict_at_capacity() -> None:
    vault = MemoryVault(clock=TickClock(), max_notes=2)
    vault.remember("用户对芒果严重过敏", importance=3)
    vault.remember("另一条独立的事实记录", importance=3)
    # 与第一条高度重复 → 合并不增条数，容量已满也不应误删任何记忆。
    _, merged = vault.remember("用户对芒果严重过敏", tags=["新增标签"], importance=4)
    assert merged is True
    assert vault.status()["total"] == 2
    assert vault.status()["capacity"]["evicted"] == 0


@pytest.mark.parametrize("bad", [0, -3, True, "20"])
def test_invalid_max_notes_raises(bad) -> None:
    with pytest.raises(ValueError):
        MemoryVault(max_notes=bad)  # type: ignore[arg-type]
