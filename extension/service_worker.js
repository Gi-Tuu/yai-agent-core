// YAI Agent Core 浏览器扩展 —— Service Worker（MV3）。
//
// 职责边界（刻意收窄）：
// - 只做"侧边栏 ↔ Native Messaging Bridge"的连接与转发，不执行 Agent 逻辑；
// - Service Worker 随时可能被浏览器回收：所有状态都可从 Bridge 重新查询，
//   断开时一律把结果标记为 unknown（不自动重做任何可能已有副作用的操作）；
// - 不保存任何 API Key / 系统凭据（凭据只存在本地 Bridge 配置与 Sidecar 进程里）。

const HOST_NAME = "com.yai.agentcore";
const BRIDGE_PROTOCOL_VERSION = 1;
const RECONNECT_DELAY_MS = 3000;

/** @type {chrome.runtime.Port | null} */
let port = null;
/** @type {{state: string, detail: string, sidecarProtocol?: number}} */
let status = { state: "disconnected", detail: "尚未连接本地 Bridge" };
/** @type {string | null} */
let activeRunId = null;
/** @type {Set<chrome.runtime.Port>} */
const panels = new Set();
let reconnectTimer = null;

chrome.runtime.onInstalled.addListener(() => {
  enableSidePanelOnActionClick();
});

chrome.runtime.onStartup.addListener(() => {
  enableSidePanelOnActionClick();
});

// 顶层也调一次：卸载重载（unpacked reload）时 onInstalled 不一定再触发，
// 但用户点击图标时仍应能打开侧边栏。
enableSidePanelOnActionClick();

// 兜底入口：即使 setPanelBehavior 没生效（旧版浏览器 / 企业策略 / 行为被重置），
// 点击工具栏图标时也主动尝试打开侧边栏；仍打不开就把面板页开成普通标签页，
// 至少让用户能看见 UI 与错误原因，而不是"点了没反应"。
chrome.action.onClicked.addListener(async (tab) => {
  const windowId = tab && typeof tab.windowId === "number" ? tab.windowId : undefined;
  try {
    if (chrome.sidePanel && chrome.sidePanel.open && windowId !== undefined) {
      await chrome.sidePanel.open({ windowId });
      return;
    }
  } catch (error) {
    console.warn("[yai] sidePanel.open 失败，回退为标签页打开：", String(error));
  }
  openPanelAsTab();
});

function enableSidePanelOnActionClick() {
  if (!chrome.sidePanel || !chrome.sidePanel.setPanelBehavior) return;
  chrome.sidePanel
    .setPanelBehavior({ openPanelOnActionClick: true })
    .catch((error) => console.warn("[yai] setPanelBehavior 失败：", String(error)));
}

function openPanelAsTab() {
  chrome.tabs.create({ url: chrome.runtime.getURL("sidepanel.html") }).catch((error) => {
    console.warn("[yai] 无法打开面板标签页：", String(error));
  });
}

chrome.runtime.onConnect.addListener((panelPort) => {
  if (panelPort.name !== "yai-sidepanel") return;
  panels.add(panelPort);
  panelPort.onMessage.addListener((message) => handlePanelMessage(panelPort, message));
  panelPort.onDisconnect.addListener(() => panels.delete(panelPort));
  sendToPanel(panelPort, { type: "bridge_status", status });
  ensureConnected();
});

function handlePanelMessage(panelPort, message) {
  if (!message || typeof message !== "object") return;
  switch (message.op) {
    case "run": {
      activeRunId = null;
      postToBridge({ op: "run", task: String(message.task || "") });
      break;
    }
    case "cancel": {
      postToBridge({ op: "cancel", run_id: String(message.run_id || activeRunId || "") });
      break;
    }
    case "host_response": {
      postToBridge({
        op: "host_response",
        request_id: String(message.request_id || ""),
        result: message.result,
        error: message.error,
      });
      break;
    }
    case "status": {
      postToBridge({ op: "status" });
      break;
    }
    case "shutdown": {
      postToBridge({ op: "shutdown" });
      break;
    }
    default: {
      sendToPanel(panelPort, {
        type: "error",
        code: "unsupported_op",
        message: `不支持的侧边栏操作：${message.op}`,
      });
    }
  }
}

async function ensureConnected() {
  if (port) return;
  connectBridge();
}

function connectBridge() {
  if (port) return;
  try {
    port = chrome.runtime.connectNative(HOST_NAME);
  } catch (error) {
    setStatus({ state: "disconnected", detail: String(error) });
    scheduleReconnect();
    return;
  }
  port.onMessage.addListener(onBridgeMessage);
  port.onDisconnect.addListener(() => {
    const detail = chrome.runtime.lastError?.message || "Bridge 已断开";
    port = null;
    // 断开时绝不自动重放任务：把状态明确标为 unknown，由用户决定是否重试。
    if (activeRunId) {
      setStatus({ state: "unknown", detail: `${detail}；进行中的任务状态未知，不会自动重试` });
    } else {
      setStatus({ state: "disconnected", detail });
    }
    activeRunId = null;
    scheduleReconnect();
  });
  setStatus({ state: "connecting", detail: "正在连接本地 Bridge…" });
  postToBridge({ op: "hello", protocol: BRIDGE_PROTOCOL_VERSION });
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    if (panels.size > 0) connectBridge();
  }, RECONNECT_DELAY_MS);
}

function postToBridge(message) {
  if (!port) {
    broadcast({ type: "error", code: "not_connected", message: "本地 Bridge 未连接" });
    return false;
  }
  try {
    port.postMessage(message);
    return true;
  } catch (error) {
    broadcast({ type: "error", code: "post_failed", message: String(error) });
    return false;
  }
}

function onBridgeMessage(message) {
  if (!message || typeof message !== "object") return;
  if (message.type === "bridge_ready") {
    setStatus({
      state: "connected",
      detail: "已连接本地 Sidecar",
      sidecarProtocol: message.sidecar_protocol_version,
    });
    return;
  }
  if (message.type === "hello_ok") {
    const model = message.model ? ` · 模型 ${message.model}` : "";
    const servers = (message.mcp_servers || []).length
      ? ` · MCP ${message.mcp_servers.join(",")}`
      : "";
    setStatus({
      state: "connected",
      detail: `协议握手完成（bridge v${message.bridge_protocol_version}${model}${servers}）`,
      sidecarProtocol: message.sidecar_protocol_version,
    });
    return;
  }
  if (message.type === "hello_rejected") {
    setStatus({
      state: "error",
      detail: `Bridge 协议不匹配：期望 ${message.expected}，收到 ${message.received}`,
    });
    return;
  }
  if (message.type === "sidecar_exit") {
    activeRunId = null;
    setStatus({ state: "unknown", detail: `Sidecar 已退出（${message.reason}）` });
    return;
  }
  if (message.type === "run_started") {
    activeRunId = message.run_id || null;
  }
  if (message.type === "notification") {
    const params = message.params || {};
    if (params.type === "done" || message.method === "agent/cancelled" ||
        message.method === "agent/error") {
      activeRunId = null;
    }
  }
  broadcast(message);
}

function setStatus(next) {
  status = next;
  broadcast({ type: "bridge_status", status });
}

function broadcast(message) {
  for (const panel of panels) sendToPanel(panel, message);
}

function sendToPanel(panel, message) {
  try {
    panel.postMessage(message);
  } catch {
    panels.delete(panel);
  }
}
