"""注册 / 卸载 Chrome、Edge 的 Native Messaging Host（Windows，用户级 HKCU）。

这是"把扩展接到本地 Bridge"的一次性机器配置，**不修改仓库、不需要管理员权限**：
1. 校验扩展 ID（32 位 a–p）与启动器可执行文件；
2. 生成 Bridge 配置（allowed_origins + initialize + sidecar_command，无模型凭据）；
3. 生成 native host manifest（path 指向启动器）；
4. 写 HKCU 注册表（Chrome / Edge 可分别指定）。

用法::

    # 注册（Chrome + Edge；--python 指向装了 yai-agent-core 的解释器）
    python scripts/register_native_host.py --extension-id <32位ID> \
        --launcher "dist\\yai-bridge.exe" --python ".venv\\Scripts\\python.exe"

    # 只看会写什么，不落盘
    python scripts/register_native_host.py --extension-id <id> --launcher ... --dry-run

    # 卸载
    python scripts/register_native_host.py --unregister

安全说明：不写密钥；模型凭据由 Chrome 拉起的进程通过环境变量继承（推荐用户级
`setx OPENAI_API_KEY ...` 后重启 Chrome），不要把 Key 放进扩展或 manifest。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

HOST_NAME = "com.yai.agentcore"
_EXTENSION_ID_RE = re.compile(r"^[a-p]{32}$")
_REGISTRY_ROOTS = {
    "chrome": r"Software\Google\Chrome\NativeMessagingHosts",
    "edge": r"Software\Microsoft\Edge\NativeMessagingHosts",
}
_DEFAULT_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "YAI"


def _fail(message: str) -> int:
    print(f"[register-native-host] {message}", file=sys.stderr)
    return 2


def _write_json(path: Path, payload: dict[str, Any], *, dry_run: bool) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if dry_run:
        print(f"[dry-run] would write {path}:\n{text}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copyfile(path, path.with_suffix(path.suffix + ".bak"))
    path.write_text(text + "\n", encoding="utf-8")


def _register_windows(key_path: str, manifest_path: Path, *, dry_run: bool) -> bool:
    if dry_run:
        print(f"[dry-run] would set HKCU\\{key_path} default = {manifest_path}")
        return True
    try:
        import winreg
    except ImportError:  # pragma: no cover - 非 Windows
        return False
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_WRITE) as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, str(manifest_path))
    return True


def _unregister_windows(*, dry_run: bool) -> list[str]:
    removed: list[str] = []
    try:
        import winreg
    except ImportError:  # pragma: no cover - 非 Windows
        return removed
    for browser, root in _REGISTRY_ROOTS.items():
        key_path = f"{root}\\{HOST_NAME}"
        if dry_run:
            print(f"[dry-run] would delete HKCU\\{key_path}")
            removed.append(browser)
            continue
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)
            removed.append(browser)
        except FileNotFoundError:
            continue
    return removed


def register(args: argparse.Namespace) -> int:
    if not _EXTENSION_ID_RE.fullmatch(args.extension_id or ""):
        return _fail("--extension-id 必须是 32 位 a–p 的 Chrome 扩展 ID")
    launcher = Path(args.launcher).expanduser().resolve()
    if not launcher.is_file():
        return _fail(f"启动器不存在：{launcher}")
    python = Path(args.python).expanduser().resolve()
    if not python.is_file():
        return _fail(f"--python 不存在：{python}")

    config_path = Path(args.config).expanduser().resolve()
    manifest_path = Path(args.manifest).expanduser().resolve()
    origin = f"chrome-extension://{args.extension_id}/"

    config: dict[str, Any] = {
        "allowed_origins": [origin],
        "initialize": {"policy": {"mode": "auto"}, "interaction_timeout": 300},
        "max_task_chars": 4000,
        "sidecar_command": [str(python), "-m", "yai_core.sidecar"],
    }
    manifest = {
        "name": HOST_NAME,
        "description": "YAI Agent Core Native Messaging Bridge",
        "path": str(launcher),
        "type": "stdio",
        "allowed_origins": [origin],
    }
    _write_json(config_path, config, dry_run=args.dry_run)
    _write_json(manifest_path, manifest, dry_run=args.dry_run)

    registered: list[str] = []
    browsers = [item.strip() for item in args.browsers.split(",") if item.strip()]
    for browser in browsers:
        root = _REGISTRY_ROOTS.get(browser)
        if root is None:
            return _fail(f"未知浏览器：{browser}（只支持 chrome / edge）")
        if _register_windows(f"{root}\\{HOST_NAME}", manifest_path, dry_run=args.dry_run):
            registered.append(browser)

    result = {
        "ok": bool(registered) or args.dry_run,
        "host": HOST_NAME,
        "extension_id": args.extension_id,
        "origin": origin,
        "launcher": str(launcher),
        "config": str(config_path),
        "manifest": str(manifest_path),
        "registered": registered,
        "dry_run": args.dry_run,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


def unregister(args: argparse.Namespace) -> int:
    removed = _unregister_windows(dry_run=args.dry_run)
    manifest_path = Path(args.manifest).expanduser().resolve()
    config_path = Path(args.config).expanduser().resolve()
    if not args.dry_run:
        for path in (manifest_path,):
            if path.exists():
                path.unlink()
    print(
        json.dumps(
            {
                "ok": True,
                "removed_registry": removed,
                "manifest_deleted": str(manifest_path) if not args.dry_run else "(dry-run)",
                "config_kept": str(config_path),
                "dry_run": args.dry_run,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="注册/卸载 YAI Native Messaging Host（Windows）")
    parser.add_argument("--extension-id", default="", help="Chrome/Edge 扩展 ID（32 位 a–p）")
    parser.add_argument("--launcher", default="", help="Bridge 启动器可执行文件路径")
    parser.add_argument("--python", default=sys.executable, help="运行 yai_core.sidecar 的 Python")
    parser.add_argument(
        "--config",
        default=str(_DEFAULT_DIR / "bridge.json"),
        help="Bridge 配置路径（默认 %LOCALAPPDATA%\\YAI\\bridge.json）",
    )
    parser.add_argument(
        "--manifest",
        default=str(_DEFAULT_DIR / f"{HOST_NAME}.json"),
        help="native host manifest 路径",
    )
    parser.add_argument("--browsers", default="chrome,edge", help="逗号分隔：chrome,edge")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不落盘/写注册表")
    parser.add_argument("--unregister", action="store_true", help="删除注册表与 manifest")
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        return _fail("本脚本目前只支持 Windows（其它平台按 Chrome 文档放置 manifest）")
    if args.unregister:
        return unregister(args)
    if not args.extension_id or not args.launcher:
        return _fail("注册需要 --extension-id 与 --launcher")
    return register(args)


if __name__ == "__main__":
    raise SystemExit(main())
