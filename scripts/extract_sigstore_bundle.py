"""从 sigstore ``.bundle`` (JSON) 提取传统 cosign 分离产物 ``.sig`` 与 ``.pem``。

OpenSSF Scorecard 的 Signed-Releases 检查只识别以 ``.asc`` / ``.minisig`` /
``.sig`` / ``.sign`` / ``.sigstore`` / ``.sigstore.json`` 结尾的发布资产，
不识别新版 cosign 默认生成的 ``.bundle``。本脚本在保留完整 ``.bundle``
（含签名、证书链与透明日志，可独立验证）的同时，导出其中**真实**的
签名字节（``.sig``）与签名证书（``.pem``），供 Scorecard 与传统工具验证。

用法::

    python scripts/extract_sigstore_bundle.py FILE.bundle --sig FILE.sig --pem FILE.pem
"""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path

_PEM_BEGIN = "-----BEGIN CERTIFICATE-----"
_PEM_END = "-----END CERTIFICATE-----"


def _der_to_pem(der: bytes) -> bytes:
    """把 DER 证书字节包装成 PEM 文本（每行 64 字符）。"""
    b64 = base64.b64encode(der).decode("ascii")
    lines = [b64[i : i + 64] for i in range(0, len(b64), 64)]
    return ("\n".join([_PEM_BEGIN, *lines, _PEM_END]) + "\n").encode("ascii")


def extract(bundle_path: Path) -> tuple[bytes, bytes]:
    """读取 bundle，返回 (raw 签名字节, 证书 PEM 字节)。

    兼容两种证书布局：新版 cosign / sigstore-go 的
    ``verificationMaterial.certificate.rawBytes``，与旧版的
    ``verificationMaterial.x509CertificateChain.certificates[0].rawBytes``。
    """
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    signature = base64.b64decode(bundle["messageSignature"]["signature"])
    material = bundle["verificationMaterial"]
    if "certificate" in material:
        cert_b64 = material["certificate"]["rawBytes"]
    else:
        cert_b64 = material["x509CertificateChain"]["certificates"][0]["rawBytes"]
    return signature, _der_to_pem(base64.b64decode(cert_b64))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("bundle", type=Path, help="sigstore .bundle 路径")
    ap.add_argument("--sig", type=Path, required=True, help="输出 .sig 路径")
    ap.add_argument("--pem", type=Path, required=True, help="输出 .pem 证书路径")
    args = ap.parse_args()
    signature, cert_pem = extract(args.bundle)
    args.sig.write_bytes(signature)
    args.pem.write_bytes(cert_pem)


if __name__ == "__main__":
    main()
