"""长期记忆批量写入与增量倒排回归（v0.31，10k 规模的前置能力）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from yai_core.memory import MemoryVault


def _entries(size: int):
    for index in range(size):
        # 每条内容带全局唯一 token，避免被 0.92 去重阈值合并（基准脚本同策略）。
        yield {
            "content": f"note{index} alpha{index} beta{index}",
            "kind": "note",
            "tags": [f"tag{index % 7}"],
        }


def test_remember_many_matches_single_writes(tmp_path: Path) -> None:
    batch = MemoryVault(storage_path=tmp_path / "batch.json")
    single = MemoryVault(storage_path=tmp_path / "single.json")
    batch_results = batch.remember_many(_entries(200))
    single_results = [single.remember(**entry) for entry in _entries(200)]

    assert len(batch_results) == len(single_results) == 200
    assert [note.content for note, _ in batch_results] == [
        note.content for note, _ in single_results
    ]
    assert [merged for _, merged in batch_results] == [
        merged for _, merged in single_results
    ]


def test_batch_persists_once_at_exit(tmp_path: Path) -> None:
    vault = MemoryVault(storage_path=tmp_path / "mem.json")
    saves: list[int] = []
    original = vault._save  # noqa: SLF001 - 测试计数落盘次数

    def counting_save() -> bool:
        saves.append(1)
        return original()

    vault._save = counting_save  # type: ignore[method-assign]  # noqa: SLF001

    with vault.batch():
        for entry in _entries(50):
            vault.remember(**entry)
        assert saves == []  # 批量期间不落盘

    assert len(saves) == 1  # 退出时统一写一次
    reloaded = MemoryVault(storage_path=tmp_path / "mem.json")
    assert len(reloaded._notes) == 50  # noqa: SLF001


def test_incremental_lex_index_matches_rebuild(tmp_path: Path) -> None:
    vault = MemoryVault(storage_path=tmp_path / "mem.json")
    vault.remember_many(_entries(120))
    vault.remember("用户对芒果严重过敏", kind="fact")
    vault.forget(vault._notes[0].id)  # noqa: SLF001

    incremental = {
        token: set(ids) for token, ids in vault._ensure_lex_index().items()  # noqa: SLF001
    }
    vault._lex_index = None  # noqa: SLF001 - 强制重建
    rebuilt = vault._ensure_lex_index()  # noqa: SLF001
    assert {token: set(ids) for token, ids in rebuilt.items()} == incremental


def test_batch_dedup_semantics_unchanged(tmp_path: Path) -> None:
    vault = MemoryVault(storage_path=tmp_path / "mem.json")
    first, merged_first = vault.remember("用户对芒果严重过敏", kind="fact")
    results = vault.remember_many(
        [
            {"content": "用户对芒果严重过敏", "kind": "fact", "tags": ["健康"], "importance": 5},
            {"content": "用户喜欢草莓", "kind": "preference"},
        ]
    )
    merged_note, merged = results[0]
    assert merged_first is False
    assert merged is True  # 批量内仍然做高相似合并
    assert merged_note.id == first.id
    assert merged_note.importance == 5
    assert "健康" in merged_note.tags


def test_two_thousand_notes_build_and_reload(tmp_path: Path) -> None:
    """10k 的完整基准走 scale_bench.py；CI 里用 2k 守住"能构建、能检索、能重载"。"""
    vault = MemoryVault(storage_path=tmp_path / "mem.json")
    results = vault.remember_many(_entries(2000))
    assert len(results) == 2000
    assert len(vault._notes) == 2000  # noqa: SLF001
    hits = vault.recall("note1999 alpha1999 beta1999", k=3)
    assert hits and "1999" in hits[0]["content"]
    reloaded = MemoryVault(storage_path=tmp_path / "mem.json")
    assert len(reloaded._notes) == 2000  # noqa: SLF001


def test_errors_inside_batch_still_persist_partial(tmp_path: Path) -> None:
    vault = MemoryVault(storage_path=tmp_path / "mem.json")
    with pytest.raises(ValueError):
        with vault.batch():
            vault.remember("先写一条", kind="note")
            vault.remember("   ")  # 空内容 → ValueError；外层 batch 退出时仍落盘已写部分
    reloaded = MemoryVault(storage_path=tmp_path / "mem.json")
    assert [note.content for note in reloaded._notes] == ["先写一条"]  # noqa: SLF001


def test_scale_bench_supports_batch_mode() -> None:
    from benchmarks.memory import scale_bench

    row = scale_bench._measure(200, 3)
    assert row["notes"] == 200
    assert row["build_mode"] == "batch"
    assert row["stored_notes"] == 180
    json.dumps(row)  # 脚本会把它打印成 JSON
