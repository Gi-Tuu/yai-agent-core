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
import contextlib
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import UTC, datetime
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


def _write_pid_record(path: Path, record: dict[str, object]) -> None:
    """pid 文件写 JSON（pid/port/profile/exe/started_at），便于 stop 前核验归属。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _read_pid_record(path: Path) -> dict[str, object] | None:
    """读 pid 记录；兼容旧版纯整数写法（无法核验 profile，但至少能校验镜像/端口）。"""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(data, int):
        return {"pid": data}
    if isinstance(data, dict):
        return data
    return None


def _normalize_path_text(text: str) -> str:
    return text.replace("/", "\\").replace('"', "").strip().lower()


def _process_details(pid: int) -> dict[str, str] | None:
    """返回该 PID 的镜像名与命令行；进程不存在 / 查询失败返回 None。"""
    try:
        completed = subprocess.run(
            ["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    image = ""
    for line in completed.stdout.splitlines():
        stripped = line.strip()
        # 无匹配进程时 tasklist 输出 "INFO: No tasks are running..."，不能当镜像名。
        if not stripped or stripped.upper().startswith("INFO:") or stripped.startswith("信息:"):
            continue
        if '"' not in stripped and "," not in stripped:
            continue
        parts = [part.strip().strip('"') for part in stripped.split('","')]
        if parts and parts[0]:
            image = parts[0].strip('"')
            break
    if not image:
        return None
    return {"image": image, "cmdline": _command_line(int(pid))}


def _command_line(pid: int) -> str:
    """尽力读取命令行（wmic 优先，新版 Windows 回退到 PowerShell CIM）。"""
    pid_int = int(pid)  # 先转 int，杜绝把外部字符串拼进命令
    try:
        completed = subprocess.run(
            ["wmic", "process", "where", f"processid={pid_int}", "get", "commandline", "/value"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        for line in completed.stdout.splitlines():
            if line.lower().startswith("commandline="):
                return line.split("=", 1)[1].strip()
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        completed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid_int}').CommandLine",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return completed.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _validate_pid_owner(
    record: dict[str, object], details: dict[str, str] | None, *, port: int
) -> tuple[bool, str]:
    """stop 前核验：PID 必须是本脚本用这个端口/ profile 启动的 Chrome，否则拒绝。"""
    pid = record.get("pid")
    if not details:
        return False, f"找不到 PID {pid} 对应进程（可能已退出）"
    image = str(details.get("image") or "").lower()
    if "chrome" not in image:
        return False, (
            f"PID {pid} 当前镜像是 {details.get('image')!r}，不是 Chrome，"
            "拒绝 taskkill"
        )
    cmdline = str(details.get("cmdline") or "")
    if f"--remote-debugging-port={port}" not in cmdline.lower():
        return False, "命令行不含本脚本使用的 --remote-debugging-port，拒绝 taskkill"
    expected_profile = str(record.get("profile") or "")
    if expected_profile:
        needle = _normalize_path_text(expected_profile)
        if needle and needle not in _normalize_path_text(cmdline):
            return False, "命令行 --user-data-dir 与 pid 文件记录不一致，拒绝 taskkill"
    return True, ""


def start(*, port: int, headless: bool, profile: Path) -> int:
    chrome = _find_chrome()
    if chrome is None:
        print(json.dumps({"ok": False, "skipped": "chrome-not-found"}))
        return 2
    endpoint = f"http://127.0.0.1:{port}"
    if _version(endpoint) is not None:
        print(json.dumps({"ok": True, "already_running": True, "endpoint": endpoint}))
        return 0
    profile = profile.resolve()
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
    _write_pid_record(
        _pid_path(),
        {
            "pid": process.pid,
            "port": port,
            "profile": str(profile),
            "exe": str(chrome),
            "started_at": datetime.now(UTC).isoformat(),
        },
    )
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


def stop(*, port: int, pid_file: Path | None = None) -> int:
    """停止本脚本启动的 Chrome；不匹配 pid 记录时拒绝 taskkill（防误杀/pid 复用）。"""
    endpoint = f"http://127.0.0.1:{port}"
    path = pid_file or _pid_path()
    record = _read_pid_record(path)
    if record is None:
        if _version(endpoint, timeout=0.5) is None:
            print(
                json.dumps(
                    {
                        "ok": True,
                        "stopped": True,
                        "endpoint": endpoint,
                        "note": "没有 pid 记录且端口未监听",
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        print(
            json.dumps(
                {
                    "ok": False,
                    "stopped": False,
                    "endpoint": endpoint,
                    "error": "没有可信 pid 记录，拒绝停止未知进程",
                },
                ensure_ascii=False,
            )
        )
        return 1

    pid = record.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        print(
            json.dumps(
                {"ok": False, "stopped": False, "error": f"pid 记录非法：{pid!r}"},
                ensure_ascii=False,
            )
        )
        return 1

    details = _process_details(pid)
    if details is None:
        # 进程已退出：清掉过期 pid 文件，再按端口实际状态返回。
        with contextlib.suppress(OSError):
            path.unlink()
        stopped = _version(endpoint, timeout=0.5) is None
        print(
            json.dumps(
                {
                    "ok": stopped,
                    "stopped": stopped,
                    "endpoint": endpoint,
                    "pid": pid,
                    "note": "进程已不存在，已清理 pid 记录",
                },
                ensure_ascii=False,
            )
        )
        return 0 if stopped else 1

    owned, reason = _validate_pid_owner(record, details, port=port)
    if not owned:
        # 不匹配：拒绝 taskkill（例如 pid 被复用成无关进程 / 文件被改写）。
        print(
            json.dumps(
                {
                    "ok": False,
                    "stopped": False,
                    "endpoint": endpoint,
                    "pid": pid,
                    "refused": True,
                    "error": reason,
                },
                ensure_ascii=False,
            )
        )
        return 1

    subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"],
        capture_output=True,
        text=True,
    )
    with contextlib.suppress(OSError):
        path.unlink()
    for _ in range(20):
        if _version(endpoint, timeout=0.5) is None:
            print(
                json.dumps(
                    {"ok": True, "stopped": True, "endpoint": endpoint, "pid": pid},
                    ensure_ascii=False,
                )
            )
            return 0
        time.sleep(0.2)
    print(
        json.dumps(
            {
                "ok": False,
                "stopped": False,
                "endpoint": endpoint,
                "pid": pid,
                "error": "已发送 taskkill，但 CDP 端口仍在监听",
            },
            ensure_ascii=False,
        )
    )
    return 1


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
