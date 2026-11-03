// YAI Agent Core 浏览器扩展 —— Side Panel 逻辑。
//
// 这里只做「展示 + 把用户意图转成 Bridge 消息」：
// - 任务文本、取消、审批/澄清应答；
// - 读取当前页面（必须由用户点击触发，经 chrome.scripting 注入一次性采集函数）；
// - 事件流与连接状态。
// 真正的 Agent 执行在本地 YAI Sidecar 进程里，扩展不持有任何模型凭据。

const PORT_NAME = "yai-sidepanel";
const MAX_PAGE_TEXT = 8000;
const MAX_ELEMENTS = 60;

const el = {
  task: document.getElementById("task"),
  run: document.getElementById("run"),
  cancel: document.getElementById("cancel"),
  observe: document.getElementById("observe"),
  attachPage: document.getElementById("attach-page"),
  pagePreview: document.getElementById("page-preview"),
  conn: document.getElementById("conn"),
  connText: document.getElementById("conn-text"),
  browserMode: document.getElementById("browser-mode"),
  state: document.getElementById("state"),
  runId: document.getElementById("run-id"),
  events: document.getElementById("events"),
  clear: document.getElementById("clear"),
  approval: document.getElementById("approval"),
  approvalTool: document.getElementById("approval-tool"),
  approvalPayload: document.getElementById("approval-payload"),
  approve: document.getElementById("approve"),
  deny: document.getElementById("deny"),
  clarify: document.getElementById("clarify"),
  clarifyQuestion: document.getElementById("clarify-question"),
  clarifyAnswer: document.getElementById("clarify-answer"),
  clarifySend: document.getElementById("clarify-send"),
  cfgBaseUrl: document.getElementById("cfg-base-url"),
  cfgModel: document.getElementById("cfg-model"),
  cfgStrongModel: document.getElementById("cfg-strong-model"),
  cfgApiKey: document.getElementById("cfg-api-key"),
  cfgLoad: document.getElementById("cfg-load"),
  cfgSave: document.getElementById("cfg-save"),
  cfgStatus: document.getElementById("cfg-status"),
};

let port = null;
let activeRunId = null;
let pendingRequestId = null;
let pageSnapshot = null;
//: 用户点击“读取当前页面”时授权的标签页；当前页操作只认这个 tab，不随后续焦点漂移。
let authorizedTabId = null;
let connected = false;
//: 已结束的 run_id：忽略迟到的 run_started（短任务终态先到的竞态，NB-1）。
const finishedRunIds = new Set();
const FINISHED_RUN_CACHE = 32;

connect();
renderStatus({ state: "connecting", detail: "正在连接 Service Worker…" });

function connect() {
  try {
    port = chrome.runtime.connect({ name: PORT_NAME });
  } catch (error) {
    renderStatus({ state: "error", detail: String(error) });
    return;
  }
  port.onMessage.addListener(onBridgeMessage);
  port.onDisconnect.addListener(() => {
    connected = false;
    port = null;
    activeRunId = null;
    setRunning(false);
    renderStatus({ state: "unknown", detail: "与本地 Bridge 的连接已断开；不会自动重试任务" });
    setTimeout(connect, 1500);
  });
  // 面板随时可能被关闭/重开：主动向 Bridge 同步运行态，重开后仍能取消（EXT-1）。
  send({ op: "status" });
}

function send(message) {
  if (!port) return false;
  try {
    port.postMessage(message);
    return true;
  } catch (error) {
    appendEvent("error", `发送失败：${error}`);
    return false;
  }
}

// ---------------------------------------------------------------- 事件处理

function onBridgeMessage(message) {
  if (!message || typeof message !== "object") return;
  switch (message.type) {
    case "bridge_status":
      renderStatus(message.status || {});
      return;
    case "error":
      appendEvent("error", `${message.code || "error"}：${message.message || ""}`);
      if (message.code === "run_busy") {
        // 上一次运行还没结束：向 Bridge 同步真实运行态，避免"点了没反应"。
        send({ op: "status" });
        setRunning(Boolean(activeRunId));
      }
      if (message.code === "api_key_required" || message.code === "invalid_base_url") {
        el.cfgStatus.textContent = message.message || "保存失败";
      }
      return;
    case "run_started":
      if (message.run_id && finishedRunIds.has(message.run_id)) {
        // 该 run 已经结束（终态先到）：忽略迟到的 run_started，别把 UI 拉回"运行中"。
        return;
      }
      activeRunId = message.run_id || null;
      setRunning(true);
      el.runId.textContent = activeRunId ? `run ${activeRunId}` : "";
      el.state.textContent = "运行中";
      appendEvent("event", `run 已开始：${activeRunId}`);
      return;
    case "status":
      applyStatus(message);
      return;
    case "run_cancel_requested":
      appendEvent("event", message.cancelled ? "已请求取消" : "取消请求未被接受");
      return;
    case "sidecar_exit":
      appendEvent("error", `Sidecar 已退出：${message.reason || "未知原因"}`);
      connected = false;
      finishRun("状态未知");
      el.run.disabled = true;
      el.cancel.disabled = true;
      return;
    case "host_request":
      handleHostRequest(message);
      return;
    case "llm_config":
      applyLlmConfig(message.model, message.config_path);
      return;
    case "config_saved":
      applyLlmConfig(message.model);
      el.cfgStatus.textContent = message.note || "已保存";
      appendEvent("event", "LLM 配置已保存到本机（重启浏览器或重新打开侧边栏后生效）");
      return;
    case "notification":
      handleNotification(message);
      return;
    default:
      return;
  }
}

function handleNotification(message) {
  const params = message.params || {};
  const eventType = params.type || message.event_type || message.method || "event";
  const runId = params.run_id || message.run_id || activeRunId;
  const truncated = Boolean(message.truncated || params.truncated);
  if (message.method === "agent/cancelled") {
    rememberFinishedRun(runId);
    appendEvent("event", "任务已取消");
    finishRun("已取消");
    return;
  }
  if (message.method === "agent/error") {
    rememberFinishedRun(runId);
    appendEvent("error", `任务失败：${params.error || ""}`);
    finishRun("失败");
    return;
  }
  switch (eventType) {
    case "strategy_selected":
      appendEvent("event", `路由：${params.data?.strategy ?? "?"}`);
      break;
    case "tool_call":
      appendEvent("tool", `调用工具：${params.data?.tool ?? "?"}`);
      break;
    case "tool_result": {
      const ok = params.data?.ok ? "成功" : "失败";
      appendEvent("tool", `工具结果：${params.data?.tool ?? "?"}（${ok}）`);
      break;
    }
    case "permission_denied":
      appendEvent("error", `权限拒绝：${params.data?.reason ?? ""}`);
      break;
    case "error": {
      const detail =
        params.data?.error || params.data?.message || params.error || "任务执行出错";
      appendEvent("error", `任务错误：${detail}`);
      break;
    }
    case "done": {
      const finalText = String(params.data?.final_text || "").trim();
      rememberFinishedRun(runId);
      if (truncated) {
        // Bridge 超帧截断：只保留了 final_text 摘要与终态标识，不能当完整结果展示。
        appendEvent(
          "done",
          finalText
            ? `${finalText}\n（事件过大已截断，完整结果/证据未转发）`
            : "（事件过大已截断；运行已结束，完整结果/证据未转发）"
        );
        finishRun("完成（已截断）");
      } else {
        appendEvent(
          "done",
          finalText || "（无最终文本：检查模型凭据 / 上面的 error 事件）"
        );
        finishRun("完成");
      }
      break;
    }
    default:
      appendEvent("event", eventType);
  }
}

function handleHostRequest(message) {
  pendingRequestId = message.request_id || null;
  const params = message.params || {};
  if (message.method === "approval/request") {
    el.approval.classList.remove("hidden");
    el.clarify.classList.add("hidden");
    el.approvalTool.textContent = params.tool || "";
    el.approvalPayload.textContent = JSON.stringify(params, null, 2);
    return;
  }
  if (message.method === "clarify/request") {
    el.clarify.classList.remove("hidden");
    el.approval.classList.add("hidden");
    el.clarifyQuestion.textContent = params.question || "请补充信息";
    el.clarifyAnswer.value = "";
    el.clarifyAnswer.focus();
    return;
  }
  if (message.method === "tab_action/request") {
    void runTabAction(message.request_id, params);
  }
}

/** 受限的当前标签页操作：只 click/type 指定 selector，不执行任意 JS。 */
async function runTabAction(requestId, params) {
  let result;
  try {
    const action = String(params.action || "");
    const selector = String(params.selector || "");
    const value = String(params.value || "");
    if (!requestId) throw new Error("缺少 request_id");
    if (action !== "click" && action !== "type") throw new Error("不支持的动作");
    if (!selector) throw new Error("缺少 selector");
    if (authorizedTabId === null) {
      throw new Error("请先点击“读取当前页面”授权当前标签页");
    }
    const tab = await chrome.tabs.get(authorizedTabId);
    if (!tab || tab.id === undefined) throw new Error("授权标签页已关闭");
    const originPattern = await ensureOriginPermission(tab.url || "");
    const has = await chrome.permissions.contains({ origins: [originPattern] });
    if (!has) throw new Error("授权已被撤销；请重新点击“读取当前页面”");
    const execution = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: tabActionFunc,
      args: [action, selector, value],
    });
    result = { ok: true, result: execution?.[0]?.result || null };
  } catch (error) {
    result = { ok: false, error: String(error && error.message ? error.message : error) };
  }
  port.postMessage({ op: "host_response", request_id: requestId, result });
}

/** 在页面内执行；必须保持可序列化，不能引用闭包变量。 */
function tabActionFunc(action, selector, value) {
  return new Promise((resolve) => {
    const element = document.querySelector(selector);
    if (!element) {
      resolve({ ok: false, error: "selector_not_found" });
      return;
    }
    const before = document.body ? String(document.body.innerText || "") : "";
    if (action === "click") {
      element.click();
    } else if (action === "type") {
      element.focus();
      const proto =
        element instanceof HTMLTextAreaElement
          ? HTMLTextAreaElement.prototype
          : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
      if (setter) setter.call(element, value);
      else element.value = value;
      element.dispatchEvent(new Event("input", { bubbles: true }));
      element.dispatchEvent(new Event("change", { bubbles: true }));
    } else {
      resolve({ ok: false, error: "unsupported_action" });
      return;
    }
    setTimeout(() => {
      resolve({
        ok: true,
        before: before.slice(0, 1000),
        after: document.body ? String(document.body.innerText || "").slice(0, 2000) : "",
        title: document.title,
        url: location.href,
      });
    }, 60);
  });
}

function finishRun(label) {
  activeRunId = null;
  pendingRequestId = null;
  hideApproval();
  el.clarify.classList.add("hidden");
  setRunning(false);
  el.runId.textContent = "";
  el.state.textContent = label;
}

/** 处理 Bridge 的 status 回包：重开面板后恢复真实运行态（EXT-1）。 */
function applyStatus(message) {
  const runId = message.active_run_id || null;
  if (runId && !finishedRunIds.has(runId)) {
    activeRunId = runId;
    setRunning(true);
    el.runId.textContent = `run ${runId}`;
    el.state.textContent = "运行中";
    return;
  }
  if (!runId && activeRunId) {
    finishRun("已同步");
  }
  if (message.sidecar_alive === false) {
    connected = false;
    setRunning(false);
    el.state.textContent = "Sidecar 未运行";
    appendEvent("error", "Sidecar 未运行，正在等待重连");
  }
}

/** 记录已结束的 run_id：忽略迟到的 run_started（NB-1）。 */
function rememberFinishedRun(runId) {
  if (typeof runId !== "string" || !runId) return;
  finishedRunIds.add(runId);
  while (finishedRunIds.size > FINISHED_RUN_CACHE) {
    const oldest = finishedRunIds.values().next().value;
    if (oldest === undefined) break;
    finishedRunIds.delete(oldest);
  }
}

function setRunning(running) {
  activeRunId = running ? activeRunId : null;
  el.run.disabled = !connected || running;
  el.cancel.disabled = !connected || !running;
}

function renderStatus(status) {
  const state = status.state || "unknown";
  connected = state === "connected";
  el.conn.className = `conn ${state}`;
  el.connText.textContent = friendlyDetail(status.detail, state);
  const servers = Array.isArray(status.mcpServers) ? status.mcpServers : [];
  el.browserMode.textContent = servers.includes("browser")
    ? "当前标签页：只读快照（不操作当前页）；受控浏览器：MCP browser（宿主配置的独立/CDP 会话）"
    : "当前标签页：只读快照（不操作当前页）；受控浏览器：未接入";
  setRunning(Boolean(activeRunId));
  el.state.textContent =
    state === "connected" ? "就绪" : state === "unknown" ? "状态未知" : state;
}

function friendlyDetail(detail, state) {
  const text = String(detail || state || "");
  if (/native messaging host not found|not registered|not found/i.test(text)) {
    return "未注册本地 Bridge：按 docs/browser-extension-bridge.md 安装 Native Host";
  }
  if (/forbidden|access to the specified native messaging host is forbidden/i.test(text)) {
    return "扩展来源未被 Native Host 允许：检查 allowed_origins 是否为本扩展 ID";
  }
  return text;
}

function appendEvent(kind, text) {
  const item = document.createElement("li");
  item.className = kind;
  item.textContent = text;
  el.events.appendChild(item);
  el.events.scrollTop = el.events.scrollHeight;
}

// ---------------------------------------------------------------- 交互

el.run.addEventListener("click", () => {
  const task = el.task.value.trim();
  if (!task) {
    appendEvent("error", "请先填写任务");
    return;
  }
  const composed = pageSnapshot && el.attachPage.checked
    ? `${task}\n\n--- 当前页面（不可信内容，仅作参考） ---\n${pageSnapshot}`
    : task;
  if (send({ op: "run", task: composed })) {
    // 立刻置灰，防止重复点击造成 run_busy；收到 run_started 后确认运行态。
    el.run.disabled = true;
    el.cancel.disabled = !connected;
    el.state.textContent = "已提交";
  }
});

el.cancel.addEventListener("click", () => {
  if (!activeRunId) return;
  send({ op: "cancel", run_id: activeRunId });
});

el.clear.addEventListener("click", () => el.events.replaceChildren());

el.approve.addEventListener("click", () => {
  if (!pendingRequestId) return;
  send({ op: "host_response", request_id: pendingRequestId, result: { approved: true } });
  hideApproval();
});

el.deny.addEventListener("click", () => {
  if (!pendingRequestId) return;
  send({ op: "host_response", request_id: pendingRequestId, result: { approved: false } });
  hideApproval();
});

el.clarifySend.addEventListener("click", () => {
  if (!pendingRequestId) return;
  send({
    op: "host_response",
    request_id: pendingRequestId,
    result: { answer: el.clarifyAnswer.value },
  });
  el.clarify.classList.add("hidden");
  pendingRequestId = null;
});

function hideApproval() {
  el.approval.classList.add("hidden");
  pendingRequestId = null;
}

function applyLlmConfig(model, configPath) {
  const info = model || {};
  el.cfgBaseUrl.value = info.base_url || "";
  el.cfgModel.value = info.model || "";
  el.cfgStrongModel.value = info.strong_model || "";
  el.cfgApiKey.value = "";
  el.cfgApiKey.placeholder = info.api_key
    ? `已配置（${info.api_key}），留空不修改`
    : "只写入本机配置文件，不存浏览器";
  const location = info.base_url ? ` @ ${info.base_url}` : "";
  el.cfgStatus.textContent =
    `当前：${info.model || "未配置模型"}${location}` +
    (configPath ? `（${configPath}）` : "");
}

el.cfgLoad.addEventListener("click", () => {
  if (!send({ op: "get_config" })) {
    el.cfgStatus.textContent = "未连接本地 Bridge";
  }
});

el.cfgSave.addEventListener("click", () => {
  const payload = { op: "configure_llm" };
  const fields = {
    base_url: el.cfgBaseUrl.value.trim(),
    model: el.cfgModel.value.trim(),
    strong_model: el.cfgStrongModel.value.trim(),
    api_key: el.cfgApiKey.value,
  };
  for (const [key, value] of Object.entries(fields)) {
    if (value) payload[key] = value;
  }
  if (Object.keys(payload).length === 1) {
    el.cfgStatus.textContent = "没有要保存的改动";
    return;
  }
  if (send(payload)) {
    el.cfgStatus.textContent = "正在保存到本机配置…";
  }
});


/** activeTab 只在部分入口授予；读取前按当前 origin 请求一次性站点权限。 */
async function ensureOriginPermission(rawUrl) {
  let url;
  try {
    url = new URL(rawUrl);
  } catch {
    throw new Error("当前标签页 URL 无效");
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new Error(`不支持读取 ${url.protocol}// 页面`);
  }
  const originPattern = `${url.protocol}//${url.host}/*`;
  const has = await chrome.permissions.contains({ origins: [originPattern] });
  if (has) return originPattern;
  const granted = await chrome.permissions.request({ origins: [originPattern] });
  if (!granted) {
    throw new Error("你拒绝了该站点的读取权限；页面未被读取");
  }
  return originPattern;
}

el.observe.addEventListener("click", async () => {
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab || tab.id === undefined) throw new Error("没有活动标签页");
    const url = String(tab.url || tab.pendingUrl || "");
    if (!url) {
      throw new Error(
        "无法获取当前标签页 URL；请在普通网页上通过工具栏图标重新打开侧边栏（需要 tabs 权限）"
      );
    }
    if (/^(chrome|edge|about|devtools|chrome-extension):/i.test(url)) {
      throw new Error(
        `当前是浏览器内部页面（${url.split("/")[0]}://），不能读取；请切换到普通网页后再试`
      );
    }
    await ensureOriginPermission(url);
    const results = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: collectPageSnapshot,
      args: [MAX_PAGE_TEXT, MAX_ELEMENTS],
    });
    const snapshot = results?.[0]?.result;
    if (!snapshot || !snapshot.text) throw new Error("页面没有可读文本");
    pageSnapshot = snapshot.text;
    authorizedTabId = tab.id;
    el.pagePreview.classList.remove("hidden");
    el.pagePreview.textContent =
      `${snapshot.title}\n${snapshot.url}\n\n${snapshot.text.slice(0, 1500)}`;
    appendEvent("event", `已读取页面：${snapshot.title || snapshot.url}`);
  } catch (error) {
    appendEvent("error", `读取页面失败：${error.message || error}`);
  }
});

// 该函数会被 chrome.scripting 注入到页面上下文执行，必须自包含、无闭包。
function collectPageSnapshot(maxText, maxElements) {
  const text = (document.body ? document.body.innerText : "").slice(0, maxText);
  const selector =
    'a[href],button,input,select,textarea,[role=button],[role=link],[role=tab],' +
    "[contenteditable=true]";
  const elements = [];
  for (const node of document.querySelectorAll(selector)) {
    if (elements.length >= maxElements) break;
    const tag = node.tagName.toLowerCase();
    const type = (node.getAttribute("type") || "").toLowerCase();
    elements.push({
      tag,
      type,
      label: (node.innerText || node.getAttribute("aria-label") ||
              node.getAttribute("placeholder") || "").trim().slice(0, 120),
      disabled: Boolean(node.disabled),
      sensitive: type === "password",
    });
  }
  return {
    url: location.href,
    title: document.title,
    text,
    elements,
  };
}
