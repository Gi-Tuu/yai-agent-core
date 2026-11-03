"""生成固定扩展 ID 所需的 RSA 密钥（本地执行，私钥绝不提交）。

Chrome/Edge 扩展 ID = SHA-256(公钥 DER) 前 128 位，按 0–15 映射到 a–p。
把公钥 Base64 写入 manifest 的 ``"key"``，unpacked / 商店构建就能得到固定 ID。

用法::

    .venv\\Scripts\\python.exe packaging\\extension\\keygen.py

输出：
- ``packaging/extension/extension-key.pem``（私钥，gitignore，自行离线保管）
- 控制台打印 public key base64 与 32 位扩展 ID
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

HERE = Path(__file__).resolve().parent
KEY_PATH = HERE / "extension-key.pem"


def extension_id(public_key_der: bytes) -> str:
    digest = hashlib.sha256(public_key_der).hexdigest()[:32]
    return "".join(chr(ord("a") + int(char, 16)) for char in digest)


def main() -> int:
    if KEY_PATH.exists():
        raise SystemExit(f"私钥已存在，拒绝覆盖：{KEY_PATH}")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    KEY_PATH.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    public_key = key.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    encoded = base64.b64encode(public_key).decode("ascii")
    print("private_key:", KEY_PATH)
    print("public_key_base64:", encoded)
    print("extension_id:", extension_id(public_key))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
