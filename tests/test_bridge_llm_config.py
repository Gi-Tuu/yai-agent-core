"""configure_bridge_llm.py 的纯函数测试（不写用户配置、不打印 Key）。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "configure_bridge_llm", ROOT / "scripts" / "configure_bridge_llm.py"
)
assert _SPEC and _SPEC.loader
_module = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_module)


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
    # 原子写不留 temp / lock 残留。
    assert list(tmp_path.glob("*.tmp-*")) == []
    assert not (tmp_path / "bridge.json.lock").exists()


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
