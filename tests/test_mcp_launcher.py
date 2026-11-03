"""MCP 浏览器启动器：CDP 自动拉起与回退逻辑（不真正启动浏览器）。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]


def _load() -> ModuleType:
    path = ROOT / "scripts" / "playwright_mcp_launcher.py"
    spec = importlib.util.spec_from_file_location("mcp_launcher", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_falls_back_to_managed_browser_when_cdp_unavailable(monkeypatch) -> None:
    module = _load()
    called: list[list[str]] = []

    monkeypatch.setattr(module, "_ensure_cdp", lambda endpoint: False)
    monkeypatch.setattr(module.shutil, "which", lambda name: "npx")
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda argv, **kwargs: called.append(argv) or type("R", (), {"returncode": 0})(),
    )
    code = module.main(
        ["-y", "@playwright/mcp@0.0.84", "--cdp-endpoint", "http://127.0.0.1:9222"]
    )
    assert code == 0
    assert called == [
        [
            "npx",
            "-y",
            "@playwright/mcp@0.0.84",
            "--browser",
            "chrome",
        ]
    ]


def test_keeps_cdp_args_when_endpoint_healthy(monkeypatch) -> None:
    module = _load()
    called: list[list[str]] = []

    monkeypatch.setattr(module, "_ensure_cdp", lambda endpoint: True)
    monkeypatch.setattr(module.shutil, "which", lambda name: "npx")
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda argv, **kwargs: called.append(argv) or type("R", (), {"returncode": 0})(),
    )
    code = module.main(
        ["-y", "@playwright/mcp@0.0.84", "--cdp-endpoint", "http://127.0.0.1:9222"]
    )
    assert code == 0
    assert called[0][-2:] == ["--cdp-endpoint", "http://127.0.0.1:9222"]
