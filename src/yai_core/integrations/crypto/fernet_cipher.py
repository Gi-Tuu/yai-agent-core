"""Fernet 静态存储加密：:class:`StorageCipher` 契约的开箱实现（可选 ``crypto`` extra）。

Fernet 是 ``cryptography`` 库提供的对称加密方案（AES-128-CBC + HMAC-SHA256，
自带时间戳与完整性校验），适合"保护落盘记忆不被直接明文看到"这一静态加密场景。
内核本体不依赖 cryptography：未安装 ``[crypto]`` extra 时本模块仍可导入，只有真正
构造 :class:`FernetStorageCipher` 才会抛出带安装指引的 ImportError。

密钥管理（重要）：

- 密钥**不会**、也不应由内核自动持久化。构造时不传 key 只会生成一把"一次性"密钥，
  适合临时演示——进程重启后旧密文无法解密，vault 会按"文件损坏"冷启动为空库；
- 生产请用 :meth:`FernetStorageCipher.generate_key` 生成密钥，放到宿主的环境变量 /
  系统钥匙串 / KMS 中，再用 :meth:`from_env` 读取；密钥泄露=加密失效。

示例::

    from yai_core.integrations.crypto import FernetStorageCipher
    from yai_core.memory import MemoryVault

    cipher = FernetStorageCipher.from_env("YAI_VAULT_KEY")  # 或 FernetStorageCipher(key)
    vault = MemoryVault(storage_path="data/memory.json", cipher=cipher)
"""

from __future__ import annotations

import os


class FernetStorageCipher:
    """用 Fernet 对落盘字节做对称加解密，满足 ``StorageCipher`` 契约。"""

    def __init__(self, key: bytes | str | None = None) -> None:
        try:
            from cryptography.fernet import Fernet  # noqa: PLC0415
        except ImportError as exc:
            raise ImportError(
                "使用 FernetStorageCipher 需要安装可选依赖：uv pip install -e '.[crypto]'"
            ) from exc

        if key is None:
            # 一次性密钥：仅用于临时演示 / 测试，重启后无法解旧密文（冷启动为空库）。
            key = Fernet.generate_key()
        elif isinstance(key, str):
            key = key.encode("utf-8")
        self._fernet = Fernet(key)

    def encrypt(self, plaintext: bytes) -> bytes:
        return self._fernet.encrypt(plaintext)

    def decrypt(self, ciphertext: bytes) -> bytes:
        # 篡改 / 密钥不符时抛 cryptography.fernet.InvalidToken（ValueError 子类），
        # vault._load 会按"文件损坏"冷启动容忍，不会让进程崩溃。
        return self._fernet.decrypt(ciphertext)

    @staticmethod
    def generate_key() -> bytes:
        """生成一把新的 urlsafe-base64 Fernet 密钥（请离线妥善保存，丢了无法解密）。"""
        from cryptography.fernet import Fernet  # noqa: PLC0415

        return Fernet.generate_key()

    @classmethod
    def from_env(cls, env_var: str = "YAI_VAULT_KEY") -> FernetStorageCipher:
        """从环境变量读取 Fernet 密钥构造 cipher；变量缺失时明确报错（拒绝静默明文）。"""
        key = os.getenv(env_var)
        if not key:
            raise RuntimeError(
                f"环境变量 {env_var} 未设置，无法初始化静态加密；请先用 "
                "FernetStorageCipher.generate_key() 生成密钥并配置到宿主环境"
            )
        return cls(key=key)
