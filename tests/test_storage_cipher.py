"""长期记忆落盘静态加密（StorageCipher SPI）的离线测试。

- 用标准库 base64 写一个确定性可逆的 FakeCipher，只验证 vault 的加密接线，
  base64 不提供任何安全性，仅让测试零依赖、可离线；
- Fernet 真实实现仅在安装了 cryptography（[crypto] extra）时才测，否则跳过。
"""

import base64
from pathlib import Path

import pytest

from yai_core import AgentCore, StorageCipher
from yai_core.memory import MemoryVault

try:
    import cryptography  # noqa: F401

    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False


class FakeCipher:
    """base64 可逆变换：满足 StorageCipher 契约，但不提供安全性（仅测试接线）。"""

    def encrypt(self, plaintext: bytes) -> bytes:
        return base64.b64encode(plaintext)

    def decrypt(self, ciphertext: bytes) -> bytes:
        return base64.b64decode(ciphertext)


class BrokenCipher:
    """解密永远失败：模拟密钥不符 / 密文被篡改。"""

    def encrypt(self, plaintext: bytes) -> bytes:
        return base64.b64encode(plaintext)

    def decrypt(self, ciphertext: bytes) -> bytes:
        raise ValueError("invalid token")


def test_fake_cipher_satisfies_protocol() -> None:
    assert isinstance(FakeCipher(), StorageCipher)


def test_encrypted_vault_roundtrip_and_ciphertext_on_disk(tmp_path: Path) -> None:
    path = tmp_path / "memory.enc.json"
    cipher = FakeCipher()

    vault = MemoryVault(storage_path=path, cipher=cipher)
    vault.remember("用户对芒果严重过敏", kind="fact", tags=["饮食", "过敏"])

    # 磁盘上是密文字节，不应直接出现明文记忆内容。
    on_disk = path.read_bytes()
    assert b"\xe8\x8a\x92\xe6\x9e\x9c" not in on_disk  # "芒果" 的 UTF-8 字节
    # 直接按明文 JSON 解析应当失败（确实被加密了，而不是裸 JSON）。
    import json

    with pytest.raises(json.JSONDecodeError):
        json.loads(on_disk.decode("utf-8"))

    # 用同一把 cipher 重开：记忆完整恢复、可召回。
    reopened = MemoryVault(storage_path=path, cipher=FakeCipher())
    hits = reopened.recall("饮食禁忌")
    assert any("芒果" in h["content"] for h in hits)


def test_plaintext_default_is_backward_compatible(tmp_path: Path) -> None:
    path = tmp_path / "memory.json"
    vault = MemoryVault(storage_path=path)  # 不传 cipher：明文（默认）。
    vault.remember("用户对芒果严重过敏", kind="fact")

    assert "芒果" in path.read_text(encoding="utf-8")
    reopened = MemoryVault(storage_path=path)
    assert reopened.status()["total"] == 1


def test_decrypt_failure_cold_starts_empty(tmp_path: Path) -> None:
    path = tmp_path / "memory.enc.json"
    MemoryVault(storage_path=path, cipher=FakeCipher()).remember("秘密")

    # 换一把"解不开"的 cipher：冷启动为空库，不抛异常，进程不崩。
    vault = MemoryVault(storage_path=path, cipher=BrokenCipher())
    assert vault.status()["total"] == 0
    # 冷启动后 _save 已用新 cipher 重写，再次打开仍为空且可正常写入。
    vault.remember("新的开始")
    again = MemoryVault(storage_path=path, cipher=BrokenCipher())
    # BrokenCipher 连自己刚写的也解不开（恒失败），故仍冷启动；这里只验证不崩。
    assert again.status()["total"] == 0


def test_core_passes_memory_cipher_through(tmp_path: Path) -> None:
    class DummyModel:
        async def achat(self, messages, tools=None, *, tier="standard"):
            raise AssertionError("本测试不触发模型调用")

    cipher = FakeCipher()
    core = AgentCore(
        DummyModel(),
        long_memory=True,
        memory_storage=tmp_path / "mem.json",
        memory_cipher=cipher,
    )
    assert core.memory_vault is not None
    assert core.memory_vault._cipher is cipher  # noqa: SLF001 - 验证接线


@pytest.mark.skipif(not HAS_CRYPTO, reason="未安装 [crypto] extra，跳过 Fernet 测试")
def test_fernet_cipher_roundtrip_and_tamper() -> None:
    from yai_core.integrations.crypto import FernetStorageCipher

    cipher = FernetStorageCipher()  # 一次性密钥
    plaintext = "用户对芒果过敏".encode()
    token = cipher.encrypt(plaintext)
    assert token != plaintext
    assert cipher.decrypt(token) == plaintext

    # 篡改密文应抛异常（InvalidToken），供 vault 冷启动捕获。
    tampered = token[:-1] + (b"A" if token[-1:] != b"A" else b"B")
    with pytest.raises(Exception):  # noqa: B017, PT011 - InvalidToken，具体类型随库版本
        cipher.decrypt(tampered)


@pytest.mark.skipif(not HAS_CRYPTO, reason="未安装 [crypto] extra，跳过 Fernet 测试")
def test_fernet_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from yai_core.integrations.crypto import FernetStorageCipher

    key = FernetStorageCipher.generate_key().decode("utf-8")
    monkeypatch.setenv("YAI_VAULT_KEY", key)
    cipher = FernetStorageCipher.from_env("YAI_VAULT_KEY")
    assert cipher.decrypt(cipher.encrypt(b"x")) == b"x"

    monkeypatch.delenv("YAI_VAULT_KEY", raising=False)
    with pytest.raises(RuntimeError):
        FernetStorageCipher.from_env("YAI_VAULT_KEY")
