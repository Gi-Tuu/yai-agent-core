"""启动 / 停止一个带 CDP 端口的 Chrome（宿主侧工具，不是内核依赖）。

用途：让 Playwright MCP 通过 ``--cdp-endpoint`` 连接并操作用户自己的 Chrome 窗口
（而不是 MCP 自己新开一个浏览器）。Chrome 的远程调试要求使用独立 user-data-dir，
且不会接管你已经开着的那个 Chrome（已有实例无法事后开启调试端口，需要用它启动）。

用法::

    python scripts/chrome_cdp.py start           # 有头 Chrome，CDP 端口 9222
    python scripts/chrome_cdp.py status
    python scripts/chrome_cdp.py stop

退出码：0 成功；2 环境不具备；1 失败。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

_CHROME_CANDIDATES = (
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    Path.home() / r"AppData\Local\Google\Chrome\Application\chrome.exe",
)
_DEFAULT_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "YAI"


def _find_chrome() -> Path | None:
    for candidate in _CHROME_CANDIDATES:
        if candidate.is_file():
            return candidate
    return None


def _version(endpoint: str, timeout: float = 2.0) -> dict | None:
    try:
        with urllib.request.urlopen(f"{endpoint}/json/version", timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - 端口未就绪/不可用
        return None


def _pid_path() -> Path:
    return _DEFAULT_DIR / "chrome-cdp.pid"


def start(*, port: int, headless: bool, profile: Path) -> int:
    chrome = _find_chrome()
    if chrome is None:
        print(json.dumps({"ok": False, "skipped": "chrome-not-found"}))
        return 2
    endpoint = f"http://127.0.0.1:{port}"
    if _version(endpoint) is not None:
        print(json.dumps({"ok": True, "already_running": True, "endpoint": endpoint}))
        return 0
    profile.mkdir(parents=True, exist_ok=True)
    args = [
        str(chrome),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "about:blank",
    ]
    if headless:
        args.insert(1, "--headless=new")
        args.insert(2, "--disable-gpu")
    process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _pid_path().write_text(str(process.pid), encoding="utf-8")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        version = _version(endpoint)
        if version is not None:
            print(
                json.dumps(
                    {
                        "ok": True,
                        "endpoint": endpoint,
                        "pid": process.pid,
                        "browser": version.get("Browser"),
                        "profile": str(profile),
                        "headless": headless,
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        if process.poll() is not None:
            break
        time.sleep(0.4)
    print(json.dumps({"ok": False, "error": "Chrome CDP 端口未就绪", "pid": process.pid}))
    return 1


def status(*, port: int) -> int:
    endpoint = f"http://127.0.0.1:{port}"
    version = _version(endpoint)
    print(
        json.dumps(
            {"ok": version is not None, "endpoint": endpoint, "version": version},
            ensure_ascii=False,
        )
    )
    return 0 if version is not None else 1


def stop(*, port: int) -> int:
    endpoint = f"http://127.0.0.1:{port}"
    pid_file = _pid_path()
    pid = None
    if pid_file.is_file():
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
        except ValueError:
            pid = None
    if pid is not None:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
        )
        try:
            pid_file.unlink()
        except OSError:
            pass
    for _ in range(20):
        if _version(endpoint, timeout=0.5) is None:
            print(json.dumps({"ok": True, "stopped": True, "endpoint": endpoint}))
            return 0
        time.sleep(0.2)
    print(
        json.dumps(
            {"ok": bool(_version(endpoint) is None), "stopped": False, "endpoint": endpoint}
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="启动/停止带 CDP 端口的 Chrome")
    parser.add_argument("action", choices=("start", "stop", "status"))
    parser.add_argument("--port", type=int, default=9222)
    parser.add_argument("--headless", action="store_true", help="无头运行（默认有头，用户可见）")
    parser.add_argument(
        "--profile",
        default=str(_DEFAULT_DIR / "chrome-cdp"),
        help="独立 user-data-dir（Chrome 远程调试要求，不复用你正在用的默认配置）",
    )
    args = parser.parse_args()
    if sys.platform != "win32":
        print(json.dumps({"ok": False, "skipped": "windows-only"}))
        return 2
    if args.action == "start":
        return start(port=args.port, headless=args.headless, profile=Path(args.profile))
    if args.action == "status":
        return status(port=args.port)
    return stop(port=args.port)


if __name__ == "__main__":
    raise SystemExit(main())
