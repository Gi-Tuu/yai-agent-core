"""构建 Windows 便携安装包（zip）：冻结 Bridge + 扩展 + 安装器 + 文档。

不包含 Python 运行时/模型；目标机需要 Python 3.11+ 且已安装
`yai-agent-core`（或从源码目录运行）。这是**便携包**，不是 MSI/商店安装包；
扩展仍需在 chrome://extensions / edge://extensions 手动加载。
"""

from __future__ import annotations

import json
import shutil
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
DIST = ROOT / "dist"


def _version() -> str:
    for line in (ROOT / "pyproject.toml").read_text(encoding="utf-8").splitlines():
        if line.startswith("version = "):
            return line.split("=", 1)[1].strip().strip('"')
    return "0.0.0"


def _bridge() -> Path:
    candidates = sorted(
        DIST.glob("yai-bridge*.exe"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    if not candidates:
        raise SystemExit("找不到 dist\\yai-bridge*.exe；先运行 PyInstaller 构建 Bridge")
    return candidates[0]


def main() -> int:
    if sys.platform != "win32":
        raise SystemExit("Windows 打包器只支持 Windows")
    version = _version()
    bundle = DIST / "windows-bundle"
    if bundle.exists():
        shutil.rmtree(bundle)
    (bundle / "bin").mkdir(parents=True)
    (bundle / "scripts").mkdir()
    shutil.copy2(_bridge(), bundle / "bin" / "yai-bridge.exe")
    for name in ("installer.py", "install.cmd", "uninstall.cmd"):
        shutil.copy2(HERE / name, bundle / name)
    for name in (
        "register_native_host.py",
        "configure_bridge_llm.py",
        "playwright_mcp_launcher.py",
        "chrome_cdp.py",
    ):
        shutil.copy2(ROOT / "scripts" / name, bundle / "scripts" / name)
    shutil.copytree(ROOT / "extension", bundle / "extension")
    (bundle / "README.md").write_text(
        f"""# YAI Agent Core Windows 便携包 v{version}

1. 安装 Python 3.11+ 与 `yai-agent-core`（或把本包放到源码仓库根后运行）。
2. 打开 `chrome://extensions` / `edge://extensions` → 开发者模式 → 加载
   `extension/` 目录。
3. 在命令行运行 `install.cmd --extension-id <你的32位扩展ID>`
   （可选 `--with-browser-mcp`、`--cdp-endpoint http://127.0.0.1:9222`）。
4. 固定扩展图标 → 打开侧边栏；状态应为“协议握手完成”。
5. 卸载：`uninstall.cmd --extension-id <id>`；默认保留 bridge.json（含模型配置），
   加 `--purge-config` 才会删除。

边界：本包不包含 Python 运行时/模型/Chrome；扩展商店分发、MSI 安装、自动更新
不在本包范围。PDF 说明见仓库 `docs/browser-extension-bridge.md`。
""",
        encoding="utf-8",
    )
    zip_path = DIST / f"yai-agent-core-windows-{version}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(bundle.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(bundle))
    print(
        json.dumps(
            {
                "ok": True,
                "bundle": str(bundle),
                "zip": str(zip_path),
                "zip_bytes": zip_path.stat().st_size,
                "bridge": str(_bridge()),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
