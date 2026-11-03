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
};

let port = null;
let activeRunId = null;
let pendingRequestId = null;
let pageSnapshot = null;
let connected = false;

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
    renderStatus({ state: "unknown", detail: "与本地 Bridge 的连接已断开；不会自动重试任务" });
    setTimeout(connect, 1500);
  });
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
        // 上一次运行还没结束：保持"运行中"状态，避免用户再次点击。
        setRunning(Boolean(activeRunId));
      }
      return;
    case "run_started":
      activeRunId = message.run_id || null;
      setRunning(true);
      el.runId.textContent = activeRunId ? `run ${activeRunId}` : "";
      el.state.textContent = "运行中";
      appendEvent("event", `run 已开始：${activeRunId}`);
      return;
    case "run_cancel_requested":
      appendEvent("event", message.cancelled ? "已请求取消" : "取消请求未被接受");
      return;
    case "sidecar_exit":
      appendEvent("error", `Sidecar 已退出：${message.reason || "未知原因"}`);
      finishRun("状态未知");
      return;
    case "host_request":
      handleHostRequest(message);
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
  const eventType = params.type || message.method || "event";
  if (message.method === "agent/cancelled") {
    appendEvent("event", "任务已取消");
    finishRun("已取消");
    return;
  }
  if (message.method === "agent/error") {
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
      appendEvent(
        "done",
        finalText || "（无最终文本：检查模型凭据 / 上面的 error 事件）"
      );
      finishRun("完成");
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
  }
}

function finishRun(label) {
  activeRunId = null;
  setRunning(false);
  el.runId.textContent = "";
  el.state.textContent = label;
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

el.observe.addEventListener("click", async () => {
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab || tab.id === undefined) throw new Error("没有活动标签页");
    const url = String(tab.url || "");
    if (/^(chrome|edge|about|devtools|chrome-extension):/i.test(url)) {
      throw new Error(
        `当前是浏览器内部页面（${url.split("/")[0]}://），不能读取；请切换到普通网页后再试`
      );
    }
    const results = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: collectPageSnapshot,
      args: [MAX_PAGE_TEXT, MAX_ELEMENTS],
    });
    const snapshot = results?.[0]?.result;
    if (!snapshot || !snapshot.text) throw new Error("页面没有可读文本");
    pageSnapshot = snapshot.text;
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
