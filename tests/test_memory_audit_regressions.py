"""MemoryVault 异常路径回归：审计 F1–F4。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from yai_core.memory.vault import STORAGE_VERSION, MemoryVault
from yai_core.spi.cipher import StorageCipher


class _BrokenCipher(StorageCipher):
    def encrypt(self, data: bytes) -> bytes:
        raise RuntimeError("KMS unavailable")

    def decrypt(self, data: bytes) -> bytes:
        return data


def test_cipher_failure_does_not_break_startup(tmp_path: Path) -> None:
    path = tmp_path / "vault.json"
    vault = MemoryVault(storage_path=path)
    for index in range(3):
        vault.remember(f"note {index}")

    reopened = MemoryVault(
        storage_path=path,
        cipher=_BrokenCipher(),
        max_notes=1,
    )

    assert reopened.load_state["status"] == "ok"
    assert reopened.status()["storage"]["dirty"] is True
    assert reopened.status()["storage"]["persisted"] is False
    assert reopened.recall("note") is not None


def test_atomic_write_failure_sets_durable_signals(tmp_path: Path) -> None:
    vault = MemoryVault(storage_path=tmp_path / "vault.json")
    vault.remember("one")

    def fail(_raw: bytes) -> None:
        raise OSError("disk full")

    vault._atomic_write = fail  # type: ignore[method-assign]  # noqa: SLF001
    with pytest.raises(OSError):
        vault.remember("two")

    storage = vault.status()["storage"]
    assert storage["dirty"] is True
    assert storage["persisted"] is False
    assert vault.status()["total"] == 2


def test_mixed_naive_timestamps_are_normalized(tmp_path: Path) -> None:
    path = tmp_path / "vault.json"
    aware = {
        "id": "a1",
        "content": "hello",
        "kind": "note",
        "tags": [],
        "importance": 3,
        "created_at": "2026-01-01T00:00:00+00:00",
        "last_used_at": "2026-01-01T00:00:00+00:00",
        "access_count": 0,
    }
    naive = dict(
        aware,
        id="a2",
        created_at="2026-01-01T00:00:00",
        last_used_at="2026-01-01T00:00:00",
    )
    path.write_text(
        json.dumps(
            {"version": STORAGE_VERSION, "owner": None, "notes": [aware, naive]}
        ),
        encoding="utf-8",
    )

    vault = MemoryVault(storage_path=path)
    assert vault.load_state["status"] == "ok"
    assert vault.status()["total"] == 2

    capped = MemoryVault(storage_path=path, max_notes=1)
    assert capped.status()["total"] == 1


def test_single_token_duplicate_is_merged(tmp_path: Path) -> None:
    vault = MemoryVault(storage_path=tmp_path / "vault.json")
    vault.remember("猫")
    _note, merged = vault.remember("猫")

    assert merged is True
    assert vault.status()["total"] == 1
