"""MemoryVault（结构化长期记忆）单元测试：纯标准库、离线、无 API Key。"""

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
