/* YAI 数字员工浮窗逻辑：SSE 事件 -> 动效/消息，授权与澄清回传，Core 开关。 */

(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);

  const orb = $("orb");
  const drawer = $("drawer");
  const badge = $("orb-badge");
  const headState = $("head-state");
  const coreToggle = $("core-toggle");
  const permSelect = $("permission-select");
  const stageFlow = $("stage-flow");
  const feed = $("feed");
  const taskInput = $("task-input");

  const permission = $("permission");
  const clarify = $("clarify");

  let currentRunId = null;
  let evtSource = null;
  let unread = 0;
  let isDemo = false;

  /* ---------- 小工具 ---------- */

  function make(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function addFeed(kind, text) {
    const node = make("div", `msg ${kind}`, text);
    feed.appendChild(node);
    feed.scrollTop = feed.scrollHeight;
    if (!drawer.classList.contains("open")) bumpUnread();
  }

  function bumpUnread() {
    unread += 1;
    badge.textContent = unread > 9 ? "9+" : String(unread);
    badge.classList.remove("hidden");
  }

  function addStage(kind, text) {
    if (stageFlow.children.length) {
      stageFlow.appendChild(make("span", "fx-connector"));
    }
    stageFlow.appendChild(make("span", `fx ${kind}`, text));
    stageFlow.scrollLeft = stageFlow.scrollWidth;
  }

  function setHead(text) {
    headState.textContent = text;
  }

  function setBusy(on) {
    orb.classList.toggle("busy", on);
  }

  function shortArgs(args) {
    try {
      const text = JSON.stringify(args);
      return text.length > 80 ? text.slice(0, 80) + "…" : text;
    } catch (_e) {
      return String(args);
    }
  }

  /* ---------- 抽屉 / 悬浮球 ---------- */

  function openDrawer() {
    drawer.classList.add("open");
    unread = 0;
    badge.classList.add("hidden");
  }

  function closeDrawer() {
    drawer.classList.remove("open");
  }

  orb.addEventListener("click", openDrawer);
  $("drawer-close").addEventListener("click", closeDrawer);

  /* ---------- 事件流 ---------- */

  function closeStream() {
    if (evtSource) {
      evtSource.close();
      evtSource = null;
    }
  }

  function connectStream(runId) {
    closeStream();
    const source = new EventSource(`/api/stream?run_id=${encodeURIComponent(runId)}`);
    evtSource = source;

    source.onmessage = (e) => {
      let event;
      try {
        event = JSON.parse(e.data);
      } catch (_err) {
        return;
      }
      handleEvent(event);
    };

    source.onerror = () => {
      // 本地演示：终结事件会主动 close()；走到这里说明连接异常，停止重连避免空转。
      if (evtSource === source) {
        closeStream();
        if (currentRunId === runId) {
          setBusy(false);
          addFeed("sys", "事件流中断，可重新发送任务。");
        }
      }
    };
  }

  function finish() {
    setBusy(false);
    currentRunId = null;
    closeStream();
  }

  function handleEvent(event) {
    const d = event.data || {};
    switch (event.type) {
      case "strategy_selected":
        addStage("strategy", `策略 ${d.strategy} · ${d.source}`);
        setHead(`路由：${d.strategy}`);
        break;

      case "plan_created":
        addFeed("sys", `计划：${(d.steps || []).join(" → ")}`);
        break;

      case "model_message":
        addFeed("model", d.text);
        break;

      case "capability_missing":
        addStage("gap", `缺口 · ${d.missing}`);
        addFeed("sys", `能力缺口：${d.missing}`);
        break;

      case "permission_asked":
        showPermission(d);
        break;

      case "clarify_requested":
        showClarify(d);
        break;

      case "tool_call":
        if (d.tool === "delegate") {
          const tasks = (d.arguments && d.arguments.tasks) || [];
          addFeed("tool", `⇉ 委派 ${tasks.length} 个子员工并行处理`);
          tasks.forEach(() => addStage("tool", "子员工"));
        } else {
          addStage("tool", d.tool);
          addFeed("tool", `→ ${d.tool}(${shortArgs(d.arguments)})`);
        }
        break;

      case "tool_result":
        if (d.tool === "delegate") {
          addFeed("tool", "↙ 子员工结果已回流，根员工汇总中");
        } else if (d.ok) {
          addFeed("tool", `← ${d.preview || ""}`);
        } else {
          addFeed("tool bad", `✕ ${d.tool || ""}：${d.error || ""}`);
        }
        break;

      case "tool_discovered":
        addStage("tool", `发现 ${(d.registered || []).join(", ")}`);
        addFeed("sys", `按需发现新能力：${(d.registered || []).join(", ")}（${d.source}）`);
        break;

      case "tool_composed":
        addStage("compose", `组合 ${d.tool}`);
        addFeed("sys", `组合工具 ${d.tool}：编排 ${(d.steps || []).join(" → ")}`);
        break;

      case "code_tool_created":
        addFeed("sys", `代码工具已创建：${d.name}`);
        break;

      case "code_tool_retired":
        addFeed("sys", `代码工具已回收：${d.name}`);
        break;

      case "error":
        addFeed("sys", `错误：${d.error || ""}`);
        setHead("出错");
        finish();
        break;

      case "cancelled":
        addFeed("sys", "任务已终止。");
        setHead("已终止");
        finish();
        break;

      case "done":
        setHead("完成 · 待命");
        finish();
        break;

      default:
        // 未知事件安全忽略（如后续子员工动效在旧页面上）。
        break;
    }
  }

  /* ---------- 授权弹窗 ---------- */

  function showPermission(d) {
    $("perm-tool").textContent = `工具：${d.tool}`;
    $("perm-args").textContent = (() => {
      try {
        return JSON.stringify(d.arguments, null, 1);
      } catch (_e) {
        return String(d.arguments);
      }
    })();
    permission.classList.remove("hidden");
    if (isDemo) {
      setTimeout(() => permission.classList.add("hidden"), 2500);
    }
  }

  function decide(approved) {
    if (currentRunId) {
      fetch("/api/decision", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ run_id: currentRunId, approved }),
      });
    }
    permission.classList.add("hidden");
  }

  $("perm-allow").addEventListener("click", () => decide(true));
  $("perm-deny").addEventListener("click", () => decide(false));

  /* ---------- 澄清弹窗 ---------- */

  function showClarify(d) {
    $("clarify-q").textContent = d.question;
    $("clarify-input").value = "";
    clarify.classList.remove("hidden");
    $("clarify-input").focus();
    if (isDemo) {
      setTimeout(() => clarify.classList.add("hidden"), 2500);
    }
  }

  function sendClarify() {
    const answer = $("clarify-input").value.trim();
    if (currentRunId) {
      fetch("/api/answer", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ run_id: currentRunId, answer }),
      });
    }
    clarify.classList.add("hidden");
  }

  $("clarify-send").addEventListener("click", sendClarify);
  $("clarify-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") sendClarify();
  });

  /* ---------- 发送任务 ---------- */

  async function startTask(text) {
    const task = (text || "").trim();
    if (!task) return;
    if (currentRunId) {
      addFeed("sys", "请等待当前任务完成或先终止。");
      return;
    }
    addFeed("user", task);
    setBusy(true);
    setHead("思考中…");
    try {
      const resp = await fetch("/api/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ task }),
      });
      const payload = await resp.json();
      if (resp.ok && payload.run_id) {
        currentRunId = payload.run_id;
        connectStream(payload.run_id);
      } else {
        setBusy(false);
        setHead("待命");
        addFeed("sys", payload.detail || payload.error || "任务未能启动。");
      }
    } catch (_e) {
      setBusy(false);
      setHead("待命");
      addFeed("sys", "无法连接本地代理，请确认服务已启动。");
    }
  }

  $("send-btn").addEventListener("click", () => {
    startTask(taskInput.value);
    taskInput.value = "";
  });

  taskInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      startTask(taskInput.value);
      taskInput.value = "";
    }
  });

  /* ---------- Core 开关 / 权限挡位 ---------- */

  coreToggle.addEventListener("change", () => {
    const enabled = coreToggle.checked;
    fetch("/api/core", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    });
    addFeed("sys", enabled ? "Core 已启用。" : "Core 已停用，回到纯宿主形态。");
    setHead(enabled ? "待命" : "Core 已停用");
  });

  permSelect.addEventListener("change", () => {
    const mode = permSelect.value;
    fetch("/api/permission", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode }),
    });
    const labels = { manual: "全部审批", partial: "部分审批", auto: "无需审批" };
    addFeed("sys", `权限挡位切换为：${labels[mode] || mode}（对下次任务生效）。`);
  });

  /* ---------- 初始化：与后端状态对齐 ---------- */

  async function init() {
    try {
      const resp = await fetch("/api/state");
      const state = await resp.json();
      coreToggle.checked = !!state.core_enabled;
      if (state.permission_mode) permSelect.value = state.permission_mode;
      isDemo = state.mode === "demo";
      setHead(state.core_enabled ? "待命" : "Core 已停用");
    } catch (_e) {
      addFeed("sys", "无法连接本地代理。");
    }

    // ?autoplay：自动开抽屉并跑一次演示；?autoplay=delegate 演并行委派。
    const autoParams = new URLSearchParams(location.search);
    if (autoParams.has("autoplay")) {
      const delegateScene = autoParams.get("autoplay") === "delegate";
      const autoTask = delegateScene
        ? "请并行处理：汇总本月订单总额，同时列出今天该跟进的客户"
        : "帮我算这笔订单的含税金额并归档";
      openDrawer();
      setTimeout(() => {
        startTask(autoTask);
      }, 500);
    }
  }

  init();
})();
