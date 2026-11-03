"""configure_bridge_llm.py 的纯函数测试（不写用户配置、不打印 Key）。"""

from __future__ import annotations

import importlib.util
import json
import multiprocessing
import os
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "configure_bridge_llm", ROOT / "scripts" / "configure_bridge_llm.py"
)
assert _SPEC and _SPEC.loader
_module = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_module)


def _hold_lock_worker(path: str, ready, release) -> None:
    """子进程：持有配置锁，等待父进程检查完再释放。"""
    from yai_core.sidecar.native_bridge import bridge_config_lock

    with bridge_config_lock(path, timeout=5):
        ready.set()
        release.wait(10)


def _update_worker(path: str, field: str, value: str) -> None:
    """子进程：锁内读改写一个字段（用于验证并发不同字段不丢更新）。"""
    _module.apply_update(Path(path), **{field: value})


def _config() -> dict:
    return {
        "allowed_origins": ["chrome-extension://abc/"],
        "initialize": {
            "policy": {"mode": "auto"},
            "model": {
                "provider": "openai-compatible",
                "api_key": "sk-secret-1234",
                "base_url": "https://api.old/v1",
                "model": "old",
            },
        },
        "sidecar_command": ["python", "-m", "yai_core.sidecar"],
    }


def test_masked_never_exposes_full_key() -> None:
    view = _module.masked(_config())
    assert view["api_key"] == "***1234"
    assert "secret" not in str(view)


def test_update_model_merges_and_preserves_other_fields() -> None:
    updated = _module.update_model(
        _config(),
        base_url="https://api.new/v1",
        model="new-model",
        strong_model="strong-model",
        api_key="sk-new-key",
    )
    model = updated["initialize"]["model"]
    assert model["base_url"] == "https://api.new/v1"
    assert model["model"] == "new-model"
    assert model["strong_model"] == "strong-model"
    assert model["api_key"] == "sk-new-key"
    assert updated["initialize"]["policy"] == {"mode": "auto"}
    assert updated["allowed_origins"] == ["chrome-extension://abc/"]
    # 原配置不被原地修改。
    assert _config()["initialize"]["model"]["api_key"] == "sk-secret-1234"


def test_update_model_without_key_keeps_existing_key() -> None:
    updated = _module.update_model(_config(), model="only-name")
    model = updated["initialize"]["model"]
    assert model["api_key"] == "sk-secret-1234"
    assert model["model"] == "only-name"


# ---------- v0.26 修复回归：CLI 与 Bridge 共用的原子配置写（CFG-3） ----------


def _write_config(tmp_path: Path) -> tuple[Path, dict]:
    path = tmp_path / "bridge.json"
    path.write_text(json.dumps(_config()), encoding="utf-8")
    return path, _config()


def test_apply_update_is_atomic_with_backup(tmp_path: Path) -> None:
    path, original = _write_config(tmp_path)
    updated, changed = _module.apply_update(path, api_key="sk-new-9999")
    assert changed is True
    assert updated["initialize"]["model"]["api_key"] == "sk-new-9999"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved == updated
    backup = path.with_name("bridge.json.bak")
    assert backup.is_file()
    assert json.loads(backup.read_text(encoding="utf-8")) == original
    # 原子写不留 temp；OS 锁文件会保留（不含数据，锁由句柄释放）。
    assert list(tmp_path.glob("*.tmp-*")) == []
    lock = tmp_path / "bridge.json.lock"
    assert lock.is_file()
    assert b"sk-" not in lock.read_bytes()


def test_apply_update_merges_into_latest_file(tmp_path: Path) -> None:
    path, _original = _write_config(tmp_path)
    _module.apply_update(path, base_url="https://api.new/v1", api_key="sk-new")
    _module.apply_update(path, model="new-model")
    model = json.loads(path.read_text(encoding="utf-8"))["initialize"]["model"]
    assert model["base_url"] == "https://api.new/v1"
    assert model["api_key"] == "sk-new"
    assert model["model"] == "new-model"


def test_apply_update_noop_does_not_rewrite(tmp_path: Path) -> None:
    path, _original = _write_config(tmp_path)
    before = path.read_text(encoding="utf-8")
    updated, changed = _module.apply_update(path)
    assert changed is False
    assert path.read_text(encoding="utf-8") == before
    assert not path.with_name("bridge.json.bak").exists()
    assert updated == _config()


def _spawn_ctx(monkeypatch) -> multiprocessing.context.BaseContext:
    root = Path(__file__).resolve().parents[1]
    extra = os.pathsep.join([str(root / "tests"), str(root / "src")])
    inherited = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv(
        "PYTHONPATH", extra + os.pathsep + inherited if inherited else extra
    )
    return multiprocessing.get_context("spawn")


def test_config_lock_times_out_fail_closed_without_writing(
    tmp_path: Path, monkeypatch
) -> None:
    """对端持锁时：必须失败关闭，绝不能无锁 read-modify-write。"""
    from yai_core.sidecar.native_bridge import ConfigLockError, bridge_config_lock

    path, _original = _write_config(tmp_path)
    before = path.read_text(encoding="utf-8")
    ctx = _spawn_ctx(monkeypatch)
    ready = ctx.Event()
    release = ctx.Event()
    holder = ctx.Process(target=_hold_lock_worker, args=(str(path), ready, release))
    holder.start()
    try:
        assert ready.wait(15), "子进程未能取得锁"
        with pytest.raises(ConfigLockError):
            with bridge_config_lock(path, timeout=0.1):
                pass
        with pytest.raises(OSError):
            _module.apply_update(path, model="blocked")
        assert path.read_text(encoding="utf-8") == before, "锁失败时不得写配置"
    finally:
        release.set()
        holder.join(timeout=15)
        if holder.is_alive():  # pragma: no cover - 超时兜底
            holder.terminate()


def test_two_processes_update_different_fields_keep_both(
    tmp_path: Path, monkeypatch
) -> None:
    """两个进程锁内分别改 model / strong_model，谁的更新都不能丢。"""
    path, _original = _write_config(tmp_path)
    ctx = _spawn_ctx(monkeypatch)
    processes = [
        ctx.Process(target=_update_worker, args=(str(path), "model", "m1")),
        ctx.Process(
            target=_update_worker, args=(str(path), "strong_model", "s2")
        ),
    ]
    for process in processes:
        process.start()
    try:
        for process in processes:
            process.join(timeout=30)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():  # pragma: no cover
                process.terminate()
    model = json.loads(path.read_text(encoding="utf-8"))["initialize"]["model"]
    assert model["model"] == "m1"
    assert model["strong_model"] == "s2"


def test_stale_lock_file_is_reused_not_deleted(tmp_path: Path) -> None:
    """残留锁文件无害，但持锁期间不得删除它（删除会破坏跨进程互斥）。"""
    from yai_core.sidecar.native_bridge import bridge_config_lock, config_lock_path

    path, _original = _write_config(tmp_path)
    lock = config_lock_path(path)
    lock.write_bytes(b"crashed process left this")
    with bridge_config_lock(path, timeout=1.0):
        assert lock.exists()
        assert time.monotonic() >= 0
    assert lock.exists()
