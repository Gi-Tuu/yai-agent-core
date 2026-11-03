"""register_native_host.py 的纯函数测试（不写注册表、不启动浏览器）。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "register_native_host", ROOT / "scripts" / "register_native_host.py"
)
assert _SPEC and _SPEC.loader
_module = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_module)


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
