"""浏览器扩展静态资产检查（不启动浏览器，CI 可跑）。

背景：用户反馈"加载了扩展但没有侧边栏"。根因类是"service worker 没设置
`openPanelOnActionClick` / 没有兜底入口 / 面板页引用的资源缺失"，这些都能在静态层
守住，不该等真机才发现。
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / "extension"


def _read(name: str) -> str:
    return (EXT / name).read_text(encoding="utf-8")


def test_manifest_is_mv3_with_side_panel() -> None:
    manifest = json.loads(_read("manifest.json"))
    assert manifest["manifest_version"] == 3
    assert manifest["side_panel"]["default_path"] == "sidepanel.html"
    permissions = set(manifest.get("permissions", []))
    assert {"sidePanel", "nativeMessaging", "activeTab", "scripting"}.issubset(permissions)
    assert manifest["background"]["service_worker"] == "service_worker.js"
    # 侧边栏依赖用户手势打开，不能声明 action popup（会抢走点击）。
    assert "default_popup" not in manifest.get("action", {})


def test_service_worker_registers_click_behavior_with_fallback() -> None:
    text = _read("service_worker.js")
    assert "setPanelBehavior" in text
    assert "openPanelOnActionClick: true" in text
    # 顶层 + onInstalled/onStartup 都要设置（unpacked 重载时 onInstalled 不一定触发）。
    assert text.count("enableSidePanelOnActionClick()") >= 3
    # 点击图标时主动 open，失败则回退为普通标签页——避免"点了没反应"。
    assert "chrome.action.onClicked" in text
    assert "chrome.sidePanel.open" in text
    assert "openPanelAsTab" in text
    # 面板显示当前模型 / MCP（非敏感字段，不含 api_key）。
    assert "message.model" in text
    assert "mcp_servers" in text
    assert "api_key" not in text


def test_sidepanel_page_and_assets_exist() -> None:
    html = _read("sidepanel.html")
    assert 'id="run"' in html
    assert 'id="events"' in html
    assert 'id="conn-text"' in html
    assert 'src="sidepanel.js"' in html
    assert 'href="sidepanel.css"' in html
    assert (EXT / "sidepanel.js").is_file()
    assert (EXT / "sidepanel.css").is_file()
    # 面板页不引外部脚本（离线可用、无供应链面）。
    assert "http://" not in html and "https://" not in html


def test_sidepanel_js_has_no_unresolved_placeholders() -> None:
    js = _read("sidepanel.js")
    for behaviour in ("chrome.runtime.connect", "host_response", "chrome.scripting.executeScript"):
        assert behaviour in js
    # 用户可见的状态要有明确文案，不能只留英文 lastError。
    assert "未注册本地 Bridge" in js
    # 重复点击 / run_busy / 空最终文本 / 内部页面读取失败都要有可操作提示。
    assert "setRunning" in js
    assert "无最终文本" in js
    assert "浏览器内部页面" in js
    # Native Messaging 连接只发生在 service worker（浏览器端不持有凭据/不直连进程）。
    assert "connectNative" not in js
    assert "connectNative" in _read("service_worker.js")


def test_native_host_manifest_template_is_valid() -> None:
    data = json.loads(_read("native/com.yai.agentcore.json"))
    assert data["name"] == "com.yai.agentcore"
    assert data["type"] == "stdio"
    assert isinstance(data["allowed_origins"], list) and data["allowed_origins"]
    assert data["path"] == "REPLACE_WITH_ABSOLUTE_PATH_TO_PYTHON"
