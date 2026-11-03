"""MemoryVault 损坏存储安全（v0.26 P0）：分类 / 隔离备份 / 只读降级 / 拒绝静默覆盖。

背景：旧实现 ``_load()`` 用一个大 ``except Exception`` 把任何加载失败都变成空库，
下一次 ``remember`` / ``recall`` 的落盘会把损坏或无法解密的原始档案直接覆盖掉，
"空记忆启动成功"掩盖了数据丢失。本文件先固化修复后的语义：

1. 四种失败分类：不可读 / JSON 结构损坏 / 解密失败 / 版本不兼容；
2. 默认 ``quarantine``：内容损坏时先按字节备份到 ``<name>.corrupt-<时间戳>``；
3. ``read_only``：读可用、写入抛 ``MemoryVaultReadOnlyError``，原文件分毫不动；
4. ``raise``：构造器抛 ``MemoryVaultLoadError``，携带分类与路径；
5. 解密失败 / 版本不兼容即便配了 ``quarantine`` 也只读（原始档案可能仍有效）；
6. 备份失败自动降级只读；所有路径都发 ``memory_load_failed`` 诊断。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import yai_core.memory.vault as vault_module
from yai_core.memory.vault import (
    LOAD_CORRUPT,
    LOAD_DECRYPT_FAILED,
    LOAD_MISSING,
    LOAD_OK,
    LOAD_UNREADABLE,
    LOAD_VERSION_INCOMPATIBLE,
    MemoryVault,
    MemoryVaultLoadError,
    MemoryVaultReadOnlyError,
)


class _RaisingCipher:
    """解密必失败的静态加密器（模拟密钥错误）。"""

    def encrypt(self, data: bytes) -> bytes:  # noqa: ARG002 - 接口形状
        return b"enc:" + data

    def decrypt(self, data: bytes) -> bytes:  # noqa: ARG002 - 接口形状
        raise ValueError("bad key")


def _payload(version: int = 1, notes: list[Any] | None = None) -> dict[str, Any]:
    return {
        "version": version,
        "saved_at": "2026-11-03T00:00:00+00:00",
        "notes": notes if notes is not None else [],
    }


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


# ---------- 分类 ----------


def test_missing_file_is_fresh_not_failure(tmp_path: Path) -> None:
    vault = MemoryVault(storage_path=tmp_path / "memory.json")
    assert vault.load_state["status"] == LOAD_MISSING
    assert vault.read_only is False
    assert vault.load_state["quarantined_to"] is None


def test_in_memory_vault_has_ok_status() -> None:
    vault = MemoryVault()
    assert vault.load_state["status"] == LOAD_OK
    assert vault.status()["storage"]["read_only"] is False


def test_corrupt_json_is_quarantined(tmp_path: Path) -> None:
    path = tmp_path / "memory.json"
    original = "{这不是合法 JSON".encode()
    path.write_bytes(original)

    vault = MemoryVault(storage_path=path)

    assert vault.load_state["status"] == LOAD_CORRUPT
    assert vault.read_only is False
    quarantined = Path(vault.load_state["quarantined_to"])
    assert quarantined.name.startswith("memory.json.corrupt-")
    assert quarantined.read_bytes() == original  # 原始字节完整保留

    vault.remember("恢复后的内容")
    assert MemoryVault(storage_path=path).status()["total"] == 1


def test_notes_not_list_is_corrupt(tmp_path: Path) -> None:
    path = tmp_path / "memory.json"
    _write_json(path, _payload(notes={"not": "a list"}))
    vault = MemoryVault(storage_path=path)
    assert vault.load_state["status"] == LOAD_CORRUPT


def test_malformed_note_is_corrupt(tmp_path: Path) -> None:
    path = tmp_path / "memory.json"
    _write_json(path, _payload(notes=[{"id": "x"}]))  # 缺 content
    vault = MemoryVault(storage_path=path)
    assert vault.load_state["status"] == LOAD_CORRUPT


def test_unreadable_path_is_classified(tmp_path: Path) -> None:
    directory = tmp_path / "memory.json"
    directory.mkdir()  # 路径存在但是目录：read_bytes 抛 OSError
    vault = MemoryVault(storage_path=directory)
    assert vault.load_state["status"] == LOAD_UNREADABLE
    # 目录无法按字节隔离备份 → 自动降级只读，绝不尝试覆盖。
    assert vault.read_only is True
    assert vault.load_state["quarantined_to"] is None


def test_decrypt_failure_is_read_only_even_under_quarantine(tmp_path: Path) -> None:
    path = tmp_path / "memory.json"
    original = b"opaque-ciphertext"
    path.write_bytes(original)

    vault = MemoryVault(storage_path=path, cipher=_RaisingCipher())

    assert vault.load_state["status"] == LOAD_DECRYPT_FAILED
    assert vault.read_only is True
    assert vault.load_state["quarantined_to"] is None  # 不复制、不覆盖，原档案原地保留
    with pytest.raises(MemoryVaultReadOnlyError):
        vault.remember("不该写进去")
    assert path.read_bytes() == original


def test_version_incompatible_is_read_only_not_downgraded(tmp_path: Path) -> None:
    path = tmp_path / "memory.json"
    original = json.dumps(_payload(version=99)).encode()
    path.write_bytes(original)

    vault = MemoryVault(storage_path=path)

    assert vault.load_state["status"] == LOAD_VERSION_INCOMPATIBLE
    assert vault.read_only is True
    with pytest.raises(MemoryVaultReadOnlyError):
        vault.remember("用旧版本覆盖新版本")
    assert path.read_bytes() == original


# ---------- 只读降级 ----------


def test_read_only_mode_reads_empty_but_refuses_writes(tmp_path: Path) -> None:
    path = tmp_path / "memory.json"
    original = b"{broken"
    path.write_bytes(original)

    vault = MemoryVault(storage_path=path, on_corruption="read_only")

    assert vault.read_only is True
    assert vault.load_state["status"] == LOAD_CORRUPT
    assert vault.recall("任何查询") == []
    with pytest.raises(MemoryVaultReadOnlyError):
        vault.remember("x")
    with pytest.raises(MemoryVaultReadOnlyError):
        vault.forget("note-id")
    assert path.read_bytes() == original  # 读接口也不得改写原文件


# ---------- 拒绝静默 ----------


def test_raise_mode_surfaces_classification(tmp_path: Path) -> None:
    path = tmp_path / "memory.json"
    original = b"{broken"
    path.write_bytes(original)

    with pytest.raises(MemoryVaultLoadError) as excinfo:
        MemoryVault(storage_path=path, on_corruption="raise")

    assert excinfo.value.status == LOAD_CORRUPT
    assert excinfo.value.path == path
    assert "broken" not in excinfo.value.detail or "JSON" in excinfo.value.detail
    assert path.read_bytes() == original


def test_quarantine_copy_failure_degrades_to_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "memory.json"
    original = b"{broken"
    path.write_bytes(original)

    def _boom(src: object, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(vault_module.shutil, "copyfile", _boom)

    vault = MemoryVault(storage_path=path)

    assert vault.load_state["status"] == LOAD_CORRUPT
    assert vault.read_only is True
    assert vault.load_state["quarantined_to"] is None
    with pytest.raises(MemoryVaultReadOnlyError):
        vault.remember("x")
    assert path.read_bytes() == original


def test_diagnostic_is_emitted_on_load_failure(tmp_path: Path) -> None:
    path = tmp_path / "memory.json"
    path.write_bytes(b"{broken")
    events: list[Any] = []

    MemoryVault(storage_path=path, on_diagnostic=events.append)

    assert len(events) == 1
    event = events[0]
    assert event.kind == "memory_load_failed"
    assert str(event.level) == "error"
    assert event.data["status"] == LOAD_CORRUPT
    assert event.data["path"] == str(path)


def test_invalid_policy_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        MemoryVault(storage_path=tmp_path / "memory.json", on_corruption="bogus")


def test_status_exposes_storage_block(tmp_path: Path) -> None:
    vault = MemoryVault(storage_path=tmp_path / "memory.json")
    storage = vault.status()["storage"]
    assert set(storage) == {
        "status",
        "detail",
        "read_only",
        "quarantined_to",
        "owner",
    }
    assert storage["status"] == LOAD_MISSING


# ---------- 租户 / owner 隔离（v0.26 P0 最小方案） ----------


def test_owner_mismatch_refuses_read_and_write(tmp_path: Path) -> None:
    path = tmp_path / "memory.json"
    alice = MemoryVault(storage_path=path, owner="alice")
    alice.remember("alice 的私密偏好", kind="preference")
    original = path.read_bytes()

    bob = MemoryVault(storage_path=path, owner="bob")

    assert bob.load_state["status"] == "owner_mismatch"
    assert bob.read_only is True
    assert bob.status()["total"] == 0
    assert bob.recall("私密") == []  # 不把别人的记忆读进来
    with pytest.raises(MemoryVaultReadOnlyError):
        bob.remember("bob 覆盖")
    assert path.read_bytes() == original  # 也不覆盖别人的档案


def test_same_owner_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "memory.json"
    MemoryVault(storage_path=path, owner="alice").remember("喜欢深色模式")

    again = MemoryVault(storage_path=path, owner="alice")

    assert again.load_state["status"] == "ok"
    assert again.recall("深色")[0]["content"] == "喜欢深色模式"
    assert again.status()["storage"]["owner"] == "alice"


def test_legacy_file_without_owner_stays_readable(tmp_path: Path) -> None:
    path = tmp_path / "memory.json"
    MemoryVault(storage_path=path).remember("旧版本写入的记忆")

    migrated = MemoryVault(storage_path=path, owner="alice")

    assert migrated.load_state["status"] == "ok"
    assert migrated.status()["total"] == 1
