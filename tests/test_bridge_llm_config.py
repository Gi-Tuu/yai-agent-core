"""configure_bridge_llm.py 的纯函数测试（不写用户配置、不打印 Key）。"""

from __future__ import annotations

import importlib.util
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
