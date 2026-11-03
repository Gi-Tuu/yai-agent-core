"""构建固定 ID 的扩展副本：复制 extension/ 并注入 manifest.key。

私钥由 ``keygen.py`` 生成且不提交；本脚本只读取公钥写进副本 manifest。
输出 ``dist/extension-<version>/`` 与 ``extension-id.txt``，可直接加载/打包商店。

用法::

    .venv\\Scripts\\python.exe packaging\\extension\\build_extension.py
"""

from __future__ import annotations

import base64
import json
import shutil
from pathlib import Path

from cryptography.hazmat.primitives import serialization

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
KEY_PATH = HERE / "extension-key.pem"


def extension_id(public_key_der: bytes) -> str:
    import hashlib

    digest = hashlib.sha256(public_key_der).hexdigest()[:32]
    return "".join(chr(ord("a") + int(char, 16)) for char in digest)


def main() -> int:
    if not KEY_PATH.is_file():
        raise SystemExit("缺少 extension-key.pem；先运行 packaging/extension/keygen.py")
    key = serialization.load_pem_private_key(KEY_PATH.read_bytes(), password=None)
    public_der = key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    version = "0.0.0"
    for line in (ROOT / "pyproject.toml").read_text(encoding="utf-8").splitlines():
        if line.startswith("version = "):
            version = line.split("=", 1)[1].strip().strip('"')
            break
    target = ROOT / "dist" / f"extension-{version}"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(ROOT / "extension", target)
    manifest_path = target / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["key"] = base64.b64encode(public_der).decode("ascii")
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    ext_id = extension_id(public_der)
    (ROOT / "dist" / "extension-id.txt").write_text(ext_id + "\n", encoding="utf-8")
    print(f"extension_dir: {target}")
    print(f"extension_id: {ext_id}")
    print("把 dist/extension-id.txt 的 ID 传给 installer.py --extension-id")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
