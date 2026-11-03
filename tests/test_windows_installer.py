"""安装器升级快照 / 回滚（不触碰真实注册表）。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load():
    path = ROOT / "packaging" / "windows" / "installer.py"
    spec = importlib.util.spec_from_file_location("windows_installer", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_snapshot_and_rollback_restore_files(tmp_path, monkeypatch) -> None:
    module = _load()
    local = tmp_path / "YAI"
    (local / "bin").mkdir(parents=True)
    (local / "bin" / "yai-bridge.exe").write_bytes(b"old-bridge")
    (local / "bridge.json").write_text('{"old": true}\n', encoding="utf-8")
    (local / "com.yai.agentcore.json").write_text('{"old": true}\n', encoding="utf-8")

    # 模拟升级破坏当前文件
    snapshot = module._snapshot(local)
    assert snapshot is not None
    (local / "bin" / "yai-bridge.exe").write_bytes(b"new-bridge")
    (local / "bridge.json").write_text('{"new": true}\n', encoding="utf-8")
    monkeypatch.setattr(module, "process_ids", lambda exe: [])

    assert module._rollback(local) == 0
    assert (local / "bin" / "yai-bridge.exe").read_bytes() == b"old-bridge"
    assert (local / "bridge.json").read_text(encoding="utf-8") == '{"old": true}\n'
    assert module._snapshots(local) == [snapshot]


def test_rollback_without_snapshot_fails(tmp_path) -> None:
    module = _load()
    local = tmp_path / "YAI"
    local.mkdir()
    try:
        module._rollback(local)
    except SystemExit as exc:
        assert "没有可用" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("空快照必须失败关闭")
