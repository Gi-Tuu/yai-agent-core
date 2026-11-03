"""Windows Native Host 一键安装/卸载（文件复制 + HKCU 注册）。

扩展本体仍需在 chrome://extensions / edge://extensions 手动加载；
商店分发不在本脚本范围。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXT_ID_RE = re.compile(r"^[a-p]{32}$")


def repo_root(start: Path) -> Path:
    for parent in (start, *start.parents):
        if (parent / "pyproject.toml").is_file():
            return parent
    return start


def default_python(repo: Path) -> str:
    candidate = repo / ".venv" / "Scripts" / "python.exe"
    return str(candidate) if candidate.is_file() else "python"




def absolute_python(value: str) -> str:
    path = Path(str(value)).expanduser()
    if path.is_file():
        return str(path.resolve())
    found = shutil.which(str(value))
    return str(Path(found).resolve()) if found else str(value)

def find_bridge(repo: Path) -> Path:
    bundled = HERE / "bin" / "yai-bridge.exe"
    if bundled.is_file():
        return bundled
    candidates = sorted(
        (repo / "dist").glob("yai-bridge*.exe"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    if candidates:
        return candidates[0]
    raise SystemExit("找不到 Native Bridge（先构建 dist\\yai-bridge.exe）")


def _rollback_root(local: Path) -> Path:
    return local / "rollback"


def _snapshots(local: Path) -> list[Path]:
    root = _rollback_root(local)
    if not root.is_dir():
        return []
    return sorted((p for p in root.iterdir() if p.is_dir()), reverse=True)


def _snapshot(local: Path, *, keep: int = 3) -> Path | None:
    existing = [
        path
        for path in (
            local / "bin" / "yai-bridge.exe",
            local / "bridge.json",
            local / "com.yai.agentcore.json",
            local / "install.json",
        )
        if path.is_file()
    ]
    if not existing:
        return None
    target = _rollback_root(local) / time.strftime("%Y%m%d-%H%M%S")
    target.mkdir(parents=True, exist_ok=True)
    for path in existing:
        shutil.copy2(path, target / path.name)
    for old in _snapshots(local)[keep:]:
        shutil.rmtree(old, ignore_errors=True)
    return target


def _rollback(local: Path) -> int:
    snapshots = _snapshots(local)
    if not snapshots:
        raise SystemExit("没有可用的升级快照")
    latest = snapshots[0]
    target_exe = local / "bin" / "yai-bridge.exe"
    for pid in process_ids(target_exe):
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", f"Stop-Process -Id {pid} -Force"],
            capture_output=True,
            timeout=15,
        )
    target_exe.parent.mkdir(parents=True, exist_ok=True)
    for name, destination in (
        ("yai-bridge.exe", target_exe),
        ("bridge.json", local / "bridge.json"),
        ("com.yai.agentcore.json", local / "com.yai.agentcore.json"),
        ("install.json", local / "install.json"),
    ):
        source = latest / name
        if source.is_file():
            shutil.copy2(source, destination)
    print(f"已回滚到快照：{latest}")
    print("请重启浏览器/侧边栏，让 Native Host 使用回滚后的文件。")
    return 0


def process_ids(exe: Path) -> list[str]:
    script = (
        "Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -eq '"
        + str(exe)
        + "' } | Select-Object -ExpandProperty ProcessId"
    )
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def install(args: argparse.Namespace) -> int:
    if not EXT_ID_RE.fullmatch(args.extension_id):
        raise SystemExit("--extension-id 必须是 32 位 a–p 的扩展 ID")
    repo = Path(args.repo_root).resolve() if args.repo_root else repo_root(HERE)
    python = absolute_python(args.python or default_python(repo))
    bridge = Path(args.bridge_exe).resolve() if args.bridge_exe else find_bridge(repo)
    local = Path(os.environ["LOCALAPPDATA"]) / "YAI"
    target = local / "bin" / "yai-bridge.exe"
    config = local / "bridge.json"
    manifest = local / "com.yai.agentcore.json"
    register = HERE / "scripts" / "register_native_host.py"
    if not register.is_file():
        register = HERE / "scripts" / "register_native_host.py"
    if not register.is_file():
        register = repo / "scripts" / "register_native_host.py"
    version = "unknown"
    match = re.search(
        r'^version = "([^"]+)"', (repo / "pyproject.toml").read_text(encoding="utf-8"), re.M
    )
    if match:
        version = match.group(1)

    print("YAI Native Host 安装")
    print(f"  扩展 ID: {args.extension_id}")
    print(f"  Bridge : {bridge}")
    print(f"  Python : {python}")
    print(f"  目标   : {target}")
    if args.dry_run:
        print("  [dry-run] 不复制、不写注册表")
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_file():
            print(f"升级前快照：{_snapshot(local)}")
        running = process_ids(target)
        if running:
            # 升级必须先停掉指向稳定路径的旧 Bridge；只终止该精确路径，避免误杀。
            print(f"停止运行中的 Bridge：{','.join(running)}")
            for pid in running:
                subprocess.run(
                    ["powershell", "-NoProfile", "-Command", f"Stop-Process -Id {pid} -Force"],
                    capture_output=True,
                    timeout=15,
                )
            time.sleep(0.3)
        shutil.copy2(bridge, target)
        print(f"已复制 Bridge → {target}")

    cmd = [
        python,
        str(register),
        "--extension-id",
        args.extension_id,
        "--launcher",
        str(bridge if args.dry_run else target),
        "--python",
        python,
        "--config",
        str(config),
        "--manifest",
        str(manifest),
    ]
    if (repo / ".env").is_file() and not args.dry_run:
        cmd += ["--env-file", str(repo / ".env")]
    if args.with_browser_mcp:
        cmd += ["--browser-mcp", "playwright"]
        if args.cdp_endpoint:
            cmd += ["--cdp-endpoint", args.cdp_endpoint]
    if args.dry_run:
        cmd.append("--dry-run")
    if subprocess.run(cmd, cwd=repo).returncode != 0:
        raise SystemExit("注册 Native Host 失败")
    if args.dry_run:
        return 0

    info = {
        "version": version,
        "extension_id": args.extension_id,
        "bridge": str(target),
        "config": str(config),
        "manifest": str(manifest),
        "browser_mcp": bool(args.with_browser_mcp),
        "cdp_endpoint": args.cdp_endpoint,
    }
    info_path = local / "install.json"
    info_path.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n安装完成。下一步：")
    print("  1) chrome://extensions 或 edge://extensions → 开发者模式 → 加载 extension 目录")
    print("  2) 固定图标 → 打开侧边栏，状态应为“协议握手完成”")
    print(f"  3) 安装记录：{info_path}")
    print(f"  卸载：uninstall.cmd --extension-id {args.extension_id}")
    return 0


def uninstall(args: argparse.Namespace) -> int:
    repo = Path(args.repo_root).resolve() if args.repo_root else repo_root(HERE)
    python = absolute_python(args.python or default_python(repo))
    register = HERE / "scripts" / "register_native_host.py"
    if not register.is_file():
        register = repo / "scripts" / "register_native_host.py"
    local = Path(os.environ["LOCALAPPDATA"]) / "YAI"
    target = local / "bin" / "yai-bridge.exe"
    config = local / "bridge.json"
    log = local / "bridge.log"
    if args.dry_run:
        print(f"[dry-run] 将注销 HKCU 并删除 {target}")
        if args.purge_config:
            print(f"[dry-run] 将删除 {config}（含 Key）")
        if args.purge_logs:
            print(f"[dry-run] 将删除 {log}")
        return subprocess.run(
            [python, str(register), "--unregister", "--dry-run"], cwd=repo
        ).returncode
    for pid in process_ids(target):
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", f"Stop-Process -Id {pid} -Force"],
            capture_output=True,
            timeout=15,
        )
    if subprocess.run([python, str(register), "--unregister"], cwd=repo).returncode:
        raise SystemExit("反注册失败")
    if target.is_file():
        target.unlink()
        print(f"已删除 Bridge：{target}")
    if args.purge_config:
        for path in (config, Path(str(config) + ".bak"), Path(str(config) + ".lock")):
            if path.is_file():
                path.unlink()
                print(f"已删除：{path}")
    if args.purge_logs and log.is_file():
        log.unlink()
        print(f"已删除：{log}")
    print("卸载完成；扩展本体请在浏览器扩展管理页手动移除。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="YAI Native Host 安装/卸载（Windows）")
    parser.add_argument(
        "--action", choices=("install", "uninstall", "rollback", "status"), default="install"
    )
    parser.add_argument("--extension-id", default="")
    parser.add_argument("--bridge-exe", default="")
    parser.add_argument("--python", default="")
    parser.add_argument("--repo-root", default="")
    parser.add_argument("--with-browser-mcp", action="store_true")
    parser.add_argument("--cdp-endpoint", default="")
    parser.add_argument("--purge-config", action="store_true")
    parser.add_argument("--purge-logs", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if sys.platform != "win32":
        raise SystemExit("该安装器只支持 Windows")
    if args.action == "status":
        local = Path(os.environ["LOCALAPPDATA"]) / "YAI"
        info = local / "install.json"
        print(json.dumps({
            "installed": json.loads(info.read_text(encoding="utf-8")) if info.is_file() else None,
            "bridge_exists": (local / "bin" / "yai-bridge.exe").is_file(),
            "snapshots": [str(p) for p in _snapshots(local)],
        }, ensure_ascii=False, indent=2))
        return 0
    if args.action == "rollback":
        return _rollback(Path(os.environ["LOCALAPPDATA"]) / "YAI")
    if args.action == "install":
        if not args.extension_id:
            raise SystemExit("install 需要 --extension-id")
        return install(args)
    return uninstall(args)


if __name__ == "__main__":
    raise SystemExit(main())
