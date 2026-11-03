"""register_native_host.py 的纯函数测试（不写注册表、不启动浏览器）。"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "register_native_host", ROOT / "scripts" / "register_native_host.py"
)
assert _SPEC and _SPEC.loader
_module = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_module)

_CDP_SPEC = importlib.util.spec_from_file_location(
    "chrome_cdp", ROOT / "scripts" / "chrome_cdp.py"
)
assert _CDP_SPEC and _CDP_SPEC.loader
_CDP = importlib.util.module_from_spec(_CDP_SPEC)
_CDP_SPEC.loader.exec_module(_CDP)


def test_load_env_file_parses_quotes_and_comments(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "# comment\nOPENAI_API_KEY='sk-test'\nOPENAI_BASE_URL=\"https://api.example/v1\"\nIGNORED\n",
        encoding="utf-8",
    )
    values = _module._load_env_file(str(path))
    assert values["OPENAI_API_KEY"] == "sk-test"
    assert values["OPENAI_BASE_URL"] == "https://api.example/v1"
    assert "IGNORED" not in values


def test_model_config_maps_known_keys() -> None:
    model = _module._model_config(
        {
            "OPENAI_API_KEY": "sk-test",
            "OPENAI_BASE_URL": "https://api.example/v1",
            "LLM_MODEL": "m1",
            "LLM_STRONG_MODEL": "m2",
        }
    )
    assert model == {
        "provider": "openai-compatible",
        "api_key": "sk-test",
        "base_url": "https://api.example/v1",
        "model": "m1",
        "strong_model": "m2",
    }
    assert _module._model_config({"OPENAI_BASE_URL": "x"}) == {}


def test_browser_mcp_config_is_optional_and_prefixed() -> None:
    assert _module._browser_mcp("none", headless=False) == []
    mcp = _module._browser_mcp("playwright", headless=True)
    assert len(mcp) == 1
    entry = mcp[0]
    assert entry["alias"] == "browser"
    assert entry["command"] == "npx"
    assert entry["prefix"] == "browser_"
    assert "@playwright/mcp" in " ".join(entry["args"])
    # 供应链：固定版本，不能再用 @latest。
    assert _module.PLAYWRIGHT_MCP_VERSION == "0.0.84"
    assert "0.0.84" in " ".join(entry["args"])
    assert "@latest" not in " ".join(entry["args"])
    assert "--headless" in entry["args"]
    assert "--headless" not in _module._browser_mcp("playwright", headless=False)[0]["args"]


def test_browser_mcp_cdp_endpoint_switches_mode() -> None:
    entry = _module._browser_mcp(
        "playwright", headless=False, cdp_endpoint="http://127.0.0.1:9222"
    )[0]
    args = entry["args"]
    assert "--cdp-endpoint" in args
    assert args[args.index("--cdp-endpoint") + 1] == "http://127.0.0.1:9222"
    # CDP 模式连接已有浏览器，不应再传 --browser / --headless。
    assert "--browser" not in args
    assert "--headless" not in args


# ---------- v0.26 修复回归：dry-run 脱敏 / CDP pid 归属校验 ----------


def _fake_launcher(tmp_path: Path) -> tuple[Path, Path]:
    launcher = tmp_path / "yai-bridge.exe"
    launcher.write_bytes(b"")
    python = tmp_path / "python.exe"
    python.write_bytes(b"")
    return launcher, python


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="register_native_host.py 的 dry-run 入口只支持 Windows（Linux 上 main 直接返回 2）",
)
def test_dry_run_masks_api_key(tmp_path: Path, capsys) -> None:
    """SCR-1：--dry-run 不得打印完整 api_key（只留 ***末4位）。"""
    launcher, python = _fake_launcher(tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "OPENAI_API_KEY=sk-DUMMY-SECRET-1234\n"
        "OPENAI_BASE_URL=https://api.example/v1\n"
        "LLM_MODEL=m1\n",
        encoding="utf-8",
    )
    rc = _module.main(
        [
            "--extension-id",
            "a" * 32,
            "--launcher",
            str(launcher),
            "--python",
            str(python),
            "--config",
            str(tmp_path / "bridge.json"),
            "--manifest",
            str(tmp_path / "manifest.json"),
            "--env-file",
            str(env_file),
            "--browsers",
            "chrome",
            "--dry-run",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "sk-DUMMY-SECRET-1234" not in out
    assert "***1234" in out
    # dry-run 不落盘。
    assert not (tmp_path / "bridge.json").exists()
    assert not (tmp_path / "manifest.json").exists()


def test_pid_record_roundtrip_and_legacy_format(tmp_path: Path) -> None:
    record = {
        "pid": 1234,
        "port": 9222,
        "profile": str(tmp_path / "prof"),
        "exe": "chrome.exe",
        "started_at": "2026-11-03T00:00:00+00:00",
    }
    path = tmp_path / "chrome-cdp.pid"
    _CDP._write_pid_record(path, record)
    assert _CDP._read_pid_record(path) == record
    path.write_text("4321", encoding="utf-8")  # 旧版纯整数
    assert _CDP._read_pid_record(path) == {"pid": 4321}
    path.write_text("not-json", encoding="utf-8")
    assert _CDP._read_pid_record(path) is None


def test_validate_pid_owner_rejects_mismatch(tmp_path: Path) -> None:
    profile = str(tmp_path / "prof")
    record = {"pid": 7, "port": 9222, "profile": profile}
    good = {
        "image": "chrome.exe",
        "cmdline": f'"C:\\chrome.exe" --remote-debugging-port=9222 --user-data-dir={profile}',
    }
    ok, reason = _CDP._validate_pid_owner(record, good, port=9222)
    assert ok and reason == ""

    wrong_image = {"image": "notepad.exe", "cmdline": good["cmdline"]}
    assert _CDP._validate_pid_owner(record, wrong_image, port=9222)[0] is False

    wrong_port = {
        "image": "chrome.exe",
        "cmdline": f"chrome.exe --remote-debugging-port=9333 --user-data-dir={profile}",
    }
    assert _CDP._validate_pid_owner(record, wrong_port, port=9222)[0] is False

    wrong_profile = {
        "image": "chrome.exe",
        "cmdline": "chrome.exe --remote-debugging-port=9222 --user-data-dir=C:\\other",
    }
    assert _CDP._validate_pid_owner(record, wrong_profile, port=9222)[0] is False
    # 进程已退出（查不到详情）同样拒绝。
    assert _CDP._validate_pid_owner(record, None, port=9222)[0] is False


def test_stop_refuses_non_owned_pid_without_taskkill(tmp_path: Path, monkeypatch, capsys) -> None:
    """SCR-2：pid 被复用成无关进程时拒绝 taskkill，返回非零。"""
    pid_file = tmp_path / "chrome-cdp.pid"
    _CDP._write_pid_record(
        pid_file, {"pid": 4242, "port": 9222, "profile": str(tmp_path / "prof")}
    )
    monkeypatch.setattr(
        _CDP,
        "_process_details",
        lambda pid: {"image": "notepad.exe", "cmdline": "notepad.exe"},
    )
    killed: list[object] = []
    monkeypatch.setattr(_CDP.subprocess, "run", lambda *a, **k: killed.append(a))
    rc = _CDP.stop(port=9222, pid_file=pid_file)
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rc == 1
    assert out["refused"] is True and out["stopped"] is False
    assert killed == []
    assert pid_file.is_file()  # 不删记录：保留证据，别把误杀对象信息抹掉


def test_stop_kills_only_validated_chrome(tmp_path: Path, monkeypatch, capsys) -> None:
    profile = str(tmp_path / "prof")
    pid_file = tmp_path / "chrome-cdp.pid"
    _CDP._write_pid_record(pid_file, {"pid": 5151, "port": 9444, "profile": profile})
    cmdline = f'"chrome.exe" --remote-debugging-port=9444 --user-data-dir={profile}'
    monkeypatch.setattr(
        _CDP,
        "_process_details",
        lambda pid: {"image": "chrome.exe", "cmdline": cmdline},
    )
    monkeypatch.setattr(_CDP, "_version", lambda endpoint, timeout=2.0: None)
    calls: list[list[str]] = []
    monkeypatch.setattr(_CDP.subprocess, "run", lambda args, **kw: calls.append(args))
    rc = _CDP.stop(port=9444, pid_file=pid_file)
    out = json.loads(capsys.readouterr().out.strip())
    assert rc == 0 and out["ok"] is True and out["stopped"] is True
    assert calls and calls[0][:3] == ["taskkill", "/PID", "5151"]
    assert not pid_file.exists()


def test_process_details_ignores_tasklist_info_line(monkeypatch) -> None:
    """查不到进程时 tasklist 输出 INFO 行：必须当作"进程不存在"，不能当镜像名。"""

    class _Completed:
        stdout = "INFO: No tasks are running which match the specified criteria.\n"
        returncode = 0

    monkeypatch.setattr(_CDP.subprocess, "run", lambda *a, **k: _Completed())
    assert _CDP._process_details(999999) is None
