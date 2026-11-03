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
    # 最小权限：只保留实际用到的四项（storage 未使用，已移除）。
    assert permissions == {"sidePanel", "nativeMessaging", "activeTab", "scripting"}
    assert "storage" not in permissions
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
    # 面板显示当前模型 / MCP（非敏感字段，不含 api_key 值本身）。
    assert "message.model" in text
    assert "mcp_servers" in text
    assert "localStorage" not in text


def test_sidepanel_page_and_assets_exist() -> None:
    html = _read("sidepanel.html")
    assert 'id="run"' in html
    assert 'id="events"' in html
    assert 'id="conn-text"' in html
    # ADR 0006：面板必须明确区分"当前标签页只读快照"与"受控浏览器"。
    assert 'id="browser-mode"' in html
    assert "只读快照" in html
    assert "不操作当前页" in html
    assert 'src="sidepanel.js"' in html
    assert 'href="sidepanel.css"' in html
    # LLM 设置入口（用户反馈"没看到配置 LLM 的入口"，这里守住 DOM 与输入类型）。
    for element_id in (
        "cfg-base-url",
        "cfg-model",
        "cfg-strong-model",
        "cfg-api-key",
        "cfg-load",
        "cfg-save",
    ):
        assert f'id="{element_id}"' in html
    assert 'type="password"' in html
    assert (EXT / "sidepanel.js").is_file()
    assert (EXT / "sidepanel.css").is_file()
    # 面板页不引外部脚本（离线可用、无供应链面）；placeholder 里的 URL 不算。
    assert 'src="http' not in html and 'href="http' not in html
    assert "localStorage" not in _read("sidepanel.js")


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
    # 受控浏览器状态必须可见；不得把"读取当前页"说成"能操作当前页"。
    assert "受控浏览器" in js
    assert "只读快照" in js
    # Native Messaging 连接只发生在 service worker（浏览器端不持有凭据/不直连进程）。
    assert "connectNative" not in js
    assert "connectNative" in _read("service_worker.js")


def test_native_host_manifest_template_is_valid() -> None:
    data = json.loads(_read("native/com.yai.agentcore.json"))
    assert data["name"] == "com.yai.agentcore"
    assert data["type"] == "stdio"
    assert isinstance(data["allowed_origins"], list) and data["allowed_origins"]
    assert data["path"] == "REPLACE_WITH_ABSOLUTE_PATH_TO_PYTHON"


# ---------- v0.26 修复回归：运行态同步 / 竞态去重 / Bridge 生命周期 ----------


def test_panel_syncs_run_state_on_connect() -> None:
    """EXT-1：面板连接后主动查 status，并能按 active_run_id 恢复运行态。"""
    js = _read("sidepanel.js")
    assert "send({ op: \"status\" })" in js
    assert 'case "status"' in js
    assert "active_run_id" in js
    assert "applyStatus" in js


def test_panel_ignores_late_run_started_for_finished_run() -> None:
    """NB-1：已完成 run 的迟到 run_started 必须被忽略，避免 UI 卡回运行中。"""
    panel = _read("sidepanel.js")
    worker = _read("service_worker.js")
    assert "finishedRunIds" in panel
    assert "finishedRunIds" in worker
    assert "rememberFinishedRun" in panel
    assert "rememberFinishedRun" in worker
    # 终态统一清 pending 审批卡，避免 run 结束后还能点"允许"。
    assert "pendingRequestId = null" in panel


def test_panel_shows_config_save_errors() -> None:
    """CFG-1：api_key_required / invalid_base_url 要在面板可见。"""
    js = _read("sidepanel.js")
    assert "api_key_required" in js
    assert "invalid_base_url" in js
    assert "重启浏览器或重新打开侧边栏" in js


def test_service_worker_restarts_bridge_and_sidecar() -> None:
    """NB-2/CFG-2：sidecar 退出后主动断开重连；最后面板关闭时重启 Bridge。"""
    worker = _read("service_worker.js")
    assert "dropBridgeAndReconnect" in worker
    assert "maybeCloseBridgeForReload" in worker
    assert "closeBridgeForReload" in worker
    assert "sidecar_exit" in worker
    assert "panels.size === 0" in worker
    # run 前不能再清 activeRunId（否则 run_busy 时失去取消能力）。
    run_case = worker.split('case "run":', 1)[1].split("break;", 1)[0]
    assert "activeRunId = null" not in run_case
    # 旧端口回调不能把新端口置空。
    assert "if (port !== current) return;" in worker


def test_extension_handles_truncated_terminal_frames() -> None:
    """VER-2：Bridge 超帧截断帧仍含终态标识，扩展静态契约必须能识别并收尾。"""
    worker = _read("service_worker.js")
    panel = _read("sidepanel.js")

    # Service Worker：终态判定兼容截断帧（params.type / 顶层 event_type / method）。
    assert "isTerminalNotification" in worker
    assert "message.event_type" in worker
    assert "message.run_id" in worker
    assert "agent/cancelled" in worker and "agent/error" in worker

    # 面板：截断帧也要 finishRun，且明确告诉用户结果被截断（不假装是完整结果）。
    assert "message.event_type" in panel
    assert "message.truncated" in panel
    assert "已截断" in panel
    assert "finishRun" in panel
