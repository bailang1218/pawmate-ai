/* =============================================================
   PawMate Web UI — Entry Point (app.js)

   This file is the ONLY orchestrator. All logic lives in modules:
     - js/chat/messages.js  → PawChat    (bubbles, markdown, streaming)
     - js/chat/tools.js     → PawToolCards (tool cards, confirm dialog)
     - js/settings/settings.js → PawSettings (drawer, sections, save)
     - js/sidebar.js        → PawSidebar (multi-conversation, optional)

   Load order (all <script defer>):
     i18n.js → core/*.js → chat/messages.js → chat/tools.js →
     settings/settings.js → sidebar.js → app.js
   ============================================================= */

window.__PAWMATE_WEB_VERSION__ = "modular-refactor-001";
console.log("[PawMate] version: " + window.__PAWMATE_WEB_VERSION__);

(function () {
  "use strict";

  /* ── State ─────────────────────────────────────── */
  const state = {
    bridge: null,
    pendingEcho: [],
    isReady: false,
    isTaskRunning: false,
    isCancelling: false,
    canSend: false,
    bridgeSignalsBound: false,
    webChannelInitStarted: false,
    fallbackBannerTimer: null,
  };
  const VALID_SETTINGS_TABS = ["general", "llm", "browser", "security", "logs", "about"];
  const appState = window.PawAppState;
  window._state = state;
  window.__PAWMATE_VALID_SETTINGS_TABS__ = VALID_SETTINGS_TABS;

  /* ── DOM refs ──────────────────────────────────── */
  const el = {};

  function log(msg) {
    console.log("[PawMate] " + msg);
  }

  function frontendTrace(kind, payload) {
    const bridge = state.bridge;
    if (!bridge || typeof bridge.frontendTrace !== "function") return;
    const streamTraceEnabled = window.__PAWMATE_DEBUG_STREAM_TRACE__ === true;
    if (!streamTraceEnabled && /^delta-/.test(String(kind || ""))) return;
    try {
      bridge.frontendTrace(JSON.stringify({
        kind: kind,
        payload: payload || {},
        t: Date.now(),
      }));
    } catch (e) {
      console.log("[FrontendTrace] failed", e);
    }
  }

  function initElements() {
    el.messageList  = document.getElementById("messageList");
    el.taskListPanel = document.getElementById("taskListPanel");
    if (!el.taskListPanel && el.messageList && el.messageList.parentElement) {
      el.taskListPanel = document.createElement("section");
      el.taskListPanel.id = "taskListPanel";
      el.taskListPanel.className = "task-list-panel";
      el.taskListPanel.hidden = true;
      el.messageList.parentElement.insertBefore(el.taskListPanel, el.messageList);
    }
    el.messageInput = document.getElementById("messageInput");
    el.sendBtn      = document.getElementById("sendBtn");
    el.statusText   = document.getElementById("statusText");
    el.attachBtn    = document.getElementById("attachBtn");
    el.minimizeBtn  = document.getElementById("minimizeBtn");
    el.maximizeBtn  = document.getElementById("maximizeBtn");
    el.closeBtn     = document.getElementById("closeBtn");
    el.settingsBtn  = document.getElementById("settingsBtn");
    el.taskStopBtn  = document.getElementById("taskStopBtn");
    el.modePill     = document.getElementById("modePill");
    el.modelMeter   = document.getElementById("modelMeter");
    el.modelMeterName = document.getElementById("modelMeterName");
    el.modelMeterTokens = document.getElementById("modelMeterTokens");
    el.modelFallbackBanner = document.getElementById("modelFallbackBanner");
    el.modelFallbackText = document.getElementById("modelFallbackText");
    el.desktopPetTopToggle = document.getElementById("desktopPetTopToggle");
    el.desktopPetTopCheckbox = document.getElementById("desktopPetTopCheckbox");

    // Init modules that need DOM refs
    PawChat.init(el.messageList);
    PawToolCards.init(el.messageList);

    log("elements initialized");
  }

  /* ── Input ─────────────────────────────────────── */
  function resizeComposer() {
    const ta = el.messageInput;
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 120) + "px";
  }

  function submitMessage() {
    const text = el.messageInput.value.trim();
    if (!text || !state.bridge || !state.canSend) return;
    const sendNow = function () {
      PawChat.createMessageRow("user", PawChat.escapeHtml(text), Date.now());
      state.pendingEcho.push({ text, timestamp: Date.now() });
      state.bridge.sendMessage(text);
      el.messageInput.value = "";
      resizeComposer();
      el.messageInput.focus();
    };
    if (window.PawSidebar && typeof window.PawSidebar.ensureActiveSession === "function") {
      window.PawSidebar.ensureActiveSession(function (sid) {
        if (sid) sendNow();
      });
      return;
    }
    PawChat.createMessageRow("user", PawChat.escapeHtml(text), Date.now());
    state.pendingEcho.push({ text, timestamp: Date.now() });
    state.bridge.sendMessage(text);
    el.messageInput.value = "";
    resizeComposer();
    el.messageInput.focus();
  }

  function consumePendingEcho(text) {
    const now = Date.now();
    const idx = state.pendingEcho.findIndex(
      (i) => i.text === text && now - i.timestamp < 1200
    );
    if (idx >= 0) {
      state.pendingEcho.splice(idx, 1);
      return true;
    }
    return false;
  }

  function setStatus(text) {
    el.statusText.textContent = text;
  }

  function syncLocalState(appSnapshot) {
    const inputState = appState.getInputState(appSnapshot);
    state.isReady = !!appSnapshot.bridgeReady && appSnapshot.chatStatus === "ready";
    state.isTaskRunning = !!appSnapshot.taskRunning;
    state.isCancelling = !!appSnapshot.cancelling;
    state.canSend = !!inputState.enabled;
  }

  function renderAppState(appSnapshot) {
    if (!appState || !appSnapshot) return;
    syncLocalState(appSnapshot);
    setStatus(appState.getDisplayStatus(appSnapshot));
    updateTaskStopButton(appSnapshot);
    updateComposerInput(appSnapshot);
  }

  function updateComposerInput(appSnapshot) {
    const inputState = appState.getInputState(appSnapshot || appState.getState());
    const enabled = !!inputState.enabled;
    if (el.messageInput) el.messageInput.disabled = !enabled;
    if (el.sendBtn) {
      el.sendBtn.disabled = !enabled;
      el.sendBtn.style.opacity = enabled ? "1" : "0.5";
      el.sendBtn.style.pointerEvents = enabled ? "auto" : "none";
    }
  }

  function parseBridgeJson(payload, fallback) {
    try {
      return JSON.parse(payload || "{}");
    } catch (e) {
      return fallback || {};
    }
  }

  function showSoftNotice(text, type) {
    if (!text) return;
    if (window.PawMateToast && typeof window.PawMateToast.show === "function") {
      window.PawMateToast.show(text, type || "info");
    } else {
      console.log("[Notice]", text);
    }
  }

  function applyHeartbeatStatus(payload) {
    appState.dispatch({ type: "HEARTBEAT_STATUS", payload: payload });
  }

  function applyHeartbeatWarning(payload) {
    var data = parseBridgeJson(payload, {});
    var message = data.message || "系统状态异常";
    if (data.type === "task_running" && data.snapshot) {
      message = "任务仍在运行，已持续 " + data.snapshot.task_duration_sec + " 秒";
    }
    showSoftNotice(message, "error");
  }

  function applyPresenceNudge(payload) {
    var data = parseBridgeJson(payload, {});
    var text = data.text || payload;
    if (!text) return;
    if (data.display === "chat" && window.PawChat && typeof window.PawChat.createPresenceNudge === "function") {
      PawChat.createPresenceNudge(text, { label: data.type === "idle_greeting" ? "闲置提醒" : "在线状态" });
      return;
    }
    showSoftNotice(text, "info");
  }

  function applyMaintenanceTick(payload) {
    var data = parseBridgeJson(payload, {});
    console.log("[Maintenance]", data);
  }

  function _removeLastNMessages(n) {
    for (var i = 0; i < n; i++) {
      var last = el.messageList.lastElementChild;
      if (last && !last.classList.contains("system-tip")) {
        last.remove();
      }
    }
  }

  // ── 全局中断按钮状态 ──────────────────────
  function updateTaskStopButton(appSnapshot) {
    var btn = el.taskStopBtn;
    if (!btn) return;
    var stopState = appState.getStopButtonState(appSnapshot || appState.getState());
    btn.classList.toggle("is-running", stopState.running);
    btn.classList.toggle("is-idle", stopState.idle);
    btn.classList.toggle("is-cancelling", stopState.cancelling);
    btn.disabled = stopState.disabled;
    if (stopState.status === "cancelling") {
      btn.title = "正在中断...";
      btn.setAttribute("aria-label", "正在中断...");
      return;
    }
    if (stopState.status === "running") {
      btn.title = "中断当前任务";
      btn.setAttribute("aria-label", "中断当前任务");
      return;
    }
    btn.title = "当前没有运行中的任务";
    btn.setAttribute("aria-label", "当前没有运行中的任务");
  }

  function setTaskRunning(isRunning) {
    console.log("[TaskState] running =", isRunning);
    appState.dispatch({ type: "TASK_RUNNING", value: isRunning });
  }

  function renderTaskList(payload) {
    if (!el.taskListPanel) return;
    let tasks = [];
    try {
      tasks = JSON.parse(payload || "[]");
    } catch (e) {
      tasks = [];
    }
    const active = tasks.filter((task) => {
      const status = String(task.status || "");
      return ["thinking", "working", "answering", "queued"].indexOf(status) >= 0;
    });
    el.taskListPanel.hidden = active.length === 0;
    el.taskListPanel.innerHTML = "";
    active.forEach((task) => {
      const row = document.createElement("div");
      row.className = "task-list-row";
      row.dataset.turnId = String(task.turn_id || "");
      const title = document.createElement("span");
      title.className = "task-list-title";
      title.textContent = "#" + (task.turn_id || "?") + " " + String(task.title || "task");
      const status = document.createElement("span");
      status.className = "task-list-status";
      status.textContent = String(task.status || "running");
      const detail = document.createElement("span");
      detail.className = "task-list-detail";
      detail.textContent = String(task.detail || "");
      row.appendChild(title);
      row.appendChild(status);
      row.appendChild(detail);
      el.taskListPanel.appendChild(row);
    });
  }

  function setTaskCancelling(isCancelling) {
    console.log("[TaskState] cancelling =", isCancelling);
    appState.dispatch({ type: "TASK_CANCELLING", value: isCancelling });
  }

  function syncDesktopPetToggle(enabled) {
    if (!el.desktopPetTopCheckbox) return;
    el.desktopPetTopCheckbox.checked = !!enabled;
    if (el.desktopPetTopToggle) {
      el.desktopPetTopToggle.classList.toggle("is-on", !!enabled);
      el.desktopPetTopToggle.title = enabled ? "关闭桌宠" : "打开桌宠";
    }
  }

  function loadDesktopPetToggle() {
    const cb = window.configBridge;
    if (!cb || typeof cb.getConfig !== "function") return;
    cb.getConfig(function (raw) {
      var cfg = {};
      try {
        cfg = JSON.parse(raw || "{}");
      } catch (e) {
        cfg = {};
      }
      syncDesktopPetToggle(!!(cfg.desktop_pet && cfg.desktop_pet.enabled));
    });
  }

  function saveDesktopPetEnabled(enabled) {
    const cb = window.configBridge;
    if (!cb || typeof cb.getConfig !== "function" || typeof cb.saveConfig !== "function") {
      syncDesktopPetToggle(!enabled);
      showSoftNotice("配置桥未就绪，稍后再试", "error");
      return;
    }
    cb.getConfig(function (raw) {
      var cfg = {};
      try {
        cfg = JSON.parse(raw || "{}");
      } catch (e) {
        cfg = {};
      }
      cfg.desktop_pet = cfg.desktop_pet || {};
      cfg.desktop_pet.enabled = !!enabled;
      try {
        cb.saveConfig(JSON.stringify(cfg, null, 2));
        syncDesktopPetToggle(enabled);
        showSoftNotice(enabled ? "桌宠已打开" : "桌宠已关闭", "info");
      } catch (e) {
        syncDesktopPetToggle(!enabled);
        showSoftNotice("桌宠开关保存失败", "error");
      }
    });
  }

  function refreshBackendStatus(bridge) {
    if (!bridge || typeof bridge.getBackendStatus !== "function") return;
    bridge.getBackendStatus(function (text) {
      appState.dispatch({ type: "CHAT_STATUS", value: text || "starting..." });
    });
  }

  /* ── Bridge binding ────────────────────────────── */
  function formatTokenCount(value) {
    var n = Number(value || 0);
    if (!isFinite(n) || n <= 0) return "0";
    if (n >= 1000000) {
      return (n / 1000000).toFixed(n >= 10000000 ? 1 : 2).replace(/\.0+$/, "") + "m";
    }
    if (n >= 1000) {
      return (n / 1000).toFixed(n >= 10000 ? 1 : 2).replace(/\.0+$/, "") + "k";
    }
    return String(Math.round(n));
  }

  function formatModelRuntimeLabel(data) {
    var provider = String((data && data.provider) || "").trim();
    var model = String((data && data.model) || "").trim();
    if (provider && model) return provider + "/" + model;
    if (model) return model;
    if (provider) return provider;
    return "\u6a21\u578b\u51c6\u5907\u4e2d";
  }

  function formatTokenUsageLabel(data) {
    var usage = (data && data.usage) || {};
    var turn = usage.turn || {};
    var total = usage.total || {};
    var last = usage.last || {};
    var turnTokens = Number(turn.total_tokens || 0);
    var totalTokens = Number(total.total_tokens || 0);
    var estimated = !!(turn.estimated || total.estimated || last.estimated);
    if (!turnTokens && !totalTokens) return "tokens 0";
    var prefix = estimated ? "\u4f30\u7b97 " : "";
    return prefix
      + "\u672c\u8f6e " + formatTokenCount(turnTokens)
      + " \u00b7 \u7d2f\u8ba1 " + formatTokenCount(totalTokens);
  }

  function formatProviderName(value) {
    var text = String(value || "").trim();
    if (!text) return "";
    return text.charAt(0).toUpperCase() + text.slice(1);
  }

  function applyProviderRuntimeEvent(data) {
    var providerEvent = (data && data.provider_event) || {};
    if (!providerEvent || providerEvent.event !== "provider_fallback_switch") return;
    if (!el.modelFallbackBanner || !el.modelFallbackText) return;

    var fromList = Array.isArray(providerEvent.after) ? providerEvent.after : [];
    var fromProvider = formatProviderName(fromList.length ? fromList[fromList.length - 1] : "");
    var toProvider = formatProviderName(providerEvent.provider || data.provider || "");
    var inherited = providerEvent.context_inherited ? "\u00b7 \u672c\u8f6e\u4e0a\u4e0b\u6587\u5df2\u7ee7\u627f" : "";
    var prefix = fromProvider && toProvider
      ? "\u5df2\u7531 " + fromProvider + " \u5207\u5230 " + toProvider
      : "\u5907\u7528\u6a21\u578b\u5df2\u63a5\u7ba1";
    el.modelFallbackText.textContent = prefix + " " + inherited;
    el.modelFallbackBanner.hidden = false;
    el.modelFallbackBanner.title = providerEvent.reason
      ? "\u63a5\u7ba1\u539f\u56e0: " + providerEvent.reason
      : "\u5df2\u4f7f\u7528\u5907\u7528\u6a21\u578b\u63a5\u7ba1\u672c\u8f6e\u8bf7\u6c42";
    if (el.modelMeter) {
      el.modelMeter.classList.add("is-fallback");
    }
    if (state.fallbackBannerTimer) {
      clearTimeout(state.fallbackBannerTimer);
    }
    state.fallbackBannerTimer = setTimeout(function () {
      if (el.modelFallbackBanner) el.modelFallbackBanner.hidden = true;
      if (el.modelMeter) el.modelMeter.classList.remove("is-fallback");
      state.fallbackBannerTimer = null;
    }, 8000);
  }

  function applyModelRuntime(payload) {
    if (!el.modelMeterName || !el.modelMeterTokens) return;
    var data = typeof payload === "string" ? parseBridgeJson(payload, {}) : (payload || {});
    var label = formatModelRuntimeLabel(data);
    var tokenLabel = formatTokenUsageLabel(data);
    el.modelMeterName.textContent = label;
    el.modelMeterTokens.textContent = tokenLabel;
    if (el.modelMeter) {
      el.modelMeter.title = label + "\n" + tokenLabel;
      el.modelMeter.classList.toggle("is-estimated", tokenLabel.indexOf("\u4f30\u7b97") === 0);
    }
    applyProviderRuntimeEvent(data);
  }

  function refreshModelRuntime(bridge) {
    if (!bridge || typeof bridge.getModelRuntime !== "function") return;
    bridge.getModelRuntime(function (payload) {
      applyModelRuntime(payload);
    });
  }

  function bindBridge(bridge) {
    if (state.bridgeSignalsBound || window.__PAWMATE_BRIDGE_SIGNALS_BOUND__) {
      log("bridge already bound, skipping duplicate signal handlers");
      state.bridge = bridge;
      frontendTrace("bind-skip", { reason: "already-bound" });
      refreshModelRuntime(bridge);
      return;
    }
    state.bridge = bridge;
    state.bridgeSignalsBound = true;
    window.__PAWMATE_BRIDGE_SIGNALS_BOUND__ = true;
    window.__PAWMATE_STREAM_TRACE__ = { turnDelta: 0, legacyDelta: 0 };
    PawToolCards.setBridge(bridge);
    frontendTrace("bind", {
      hasTurnDelta: !!bridge.appendAssistantDeltaForTurn,
      hasLegacyDelta: !!bridge.appendAssistantDelta,
      hasTurnFinalize: !!bridge.finalizeAssistantForTurn,
      hasLegacyFinalize: !!bridge.finalizeAssistant,
    });

    bridge.appendUserMessage.connect((text) => {
      if (consumePendingEcho(text)) return;
      PawChat.createMessageRow("user", PawChat.escapeHtml(text));
    });

    bridge.appendAssistantMessage.connect((text) => {
      PawChat.createMessageRow("assistant", PawChat.renderMarkdown(text));
    });

    if (bridge.appendAssistantDeltaForTurn) {
      bridge.appendAssistantDeltaForTurn.connect((turnId, text) => {
        window.__PAWMATE_STREAM_TRACE__.turnDelta += 1;
        if (window.__PAWMATE_STREAM_TRACE__.turnDelta <= 80) {
          frontendTrace("delta-turn", {
            turnId: turnId,
            seq: window.__PAWMATE_STREAM_TRACE__.turnDelta,
            len: String(text || "").length,
            text: String(text || "").slice(0, 80),
          });
        }
        if (text === "") PawChat.startStreamingBubbleForTurn(turnId);
        else PawChat.appendStreamingDeltaForTurn(turnId, text);
      });
    } else {
      bridge.appendAssistantDelta.connect((text) => {
        window.__PAWMATE_STREAM_TRACE__.legacyDelta += 1;
        if (window.__PAWMATE_STREAM_TRACE__.legacyDelta <= 80) {
          frontendTrace("delta-legacy", {
            seq: window.__PAWMATE_STREAM_TRACE__.legacyDelta,
            len: String(text || "").length,
            text: String(text || "").slice(0, 80),
          });
        }
        if (text === "") PawChat.startStreamingBubble();
        else PawChat.appendStreamingDelta(text);
      });
    }

    if (bridge.finalizeAssistantForTurn) {
      bridge.finalizeAssistantForTurn.connect((turnId) => {
        log("finalize assistant turn " + turnId);
        PawChat.finalizeStreamingBubbleForTurn(turnId);
        PawToolCards.resetActivityGroup(turnId);
      });
    } else {
      bridge.finalizeAssistant.connect(() => {
        log("finalize assistant");
        PawChat.finalizeStreamingBubble();
        PawToolCards.resetActivityGroup();
      });
    }

    if (bridge.appendToolCardForTurn) {
      bridge.appendToolCardForTurn.connect((turnId, t, d, s, dur) => {
        PawToolCards.renderToolActivityForTurn(turnId, t, d, s, dur);
      });
    } else {
      bridge.appendToolCard.connect((t, d, s, dur) => {
        PawToolCards.renderToolActivity(t, d, s, dur);
      });
    }
    if (bridge.taskListChanged) {
      bridge.taskListChanged.connect(renderTaskList);
    }

    bridge.setInputEnabled.connect((enabled) => {
      console.log("[UI] setInputEnabled:", enabled);
      appState.dispatch({ type: "INPUT_ENABLED", value: enabled });
      if (enabled && state.canSend) el.messageInput.focus();
    });

    bridge.turnCancelled.connect((deletedCount) => {
      log("[Bridge] turn cancelled, deleted " + deletedCount);
      appState.dispatch({ type: "TURN_CANCELLED" });
      PawChat.finalizeStreamingBubble();
      PawToolCards.resetActivityGroup();
      if (deletedCount > 0) _removeLastNMessages(deletedCount);
      var tip = document.createElement("div");
      tip.className = "system-tip";
      tip.textContent = deletedCount > 0
        ? "已中断任务（回退 " + deletedCount + " 条消息）"
        : "已中断任务，聊天记录已保留";
      el.messageList.appendChild(tip);
      setTimeout(function () { tip.remove(); }, 3000);
    });

    // 全局任务中断按钮（独立，不依赖 setInputEnabled）
    bridge.taskRunningChanged.connect(function (isRunning) {
      console.log("[TaskState] running =", isRunning);
      appState.dispatch({ type: "TASK_RUNNING", value: isRunning });
    });

    bridge.toolConfirmRequest.connect((toolName, inputJson) => {
      PawToolCards.showToolConfirmDialog(toolName, inputJson);
    });

    bridge.setStatus.connect((text) => {
      appState.dispatch({ type: "CHAT_STATUS", value: text });
    });

    if (bridge.heartbeatStatus) {
      bridge.heartbeatStatus.connect((payload) => applyHeartbeatStatus(payload));
    }
    if (bridge.heartbeatWarning) {
      bridge.heartbeatWarning.connect((payload) => applyHeartbeatWarning(payload));
    }
    if (bridge.presenceNudge) {
      bridge.presenceNudge.connect((payload) => applyPresenceNudge(payload));
    }
    if (bridge.maintenanceTick) {
      bridge.maintenanceTick.connect((payload) => applyMaintenanceTick(payload));
    }
    if (bridge.modelRuntimeChanged) {
      bridge.modelRuntimeChanged.connect((payload) => applyModelRuntime(payload));
    }
    if (bridge.planUpdate) {
      bridge.planUpdate.connect((payload) => PawToolCards.renderPlanChecklist(payload));
    }

    if (el.modePill) {
      el.modePill.textContent = bridge._mock_mode ? "Mock" : "AI";
    }

    appState.dispatch({ type: "BRIDGE_READY" });
    appState.dispatch({ type: "CHAT_STATUS", value: "starting..." });
    refreshBackendStatus(bridge);
    refreshModelRuntime(bridge);
    log("bridge bound OK");
  }

  /* ── Window bridge helpers ─────────────────────── */
  function callWindowBridge(method, label) {
    log(label + " → windowBridge." + method);
    const wb = window.windowBridge;
    if (wb && typeof wb[method] === "function") {
      wb[method]();
    } else {
      log("  FAILED: windowBridge." + method + " not available");
    }
  }

  /* ── QWebChannel init ──────────────────────────── */
  function initWebChannel() {
    if (state.webChannelInitStarted) {
      log("QWebChannel init already started");
      return;
    }
    state.webChannelInitStarted = true;
    if (typeof QWebChannel === "undefined" || typeof qt === "undefined") {
      log("QWebChannel not available");
      appState.dispatch({ type: "BRIDGE_UNAVAILABLE", text: "bridge unavailable" });
      return;
    }
    new QWebChannel(qt.webChannelTransport, function (channel) {
      log("QWebChannel connected");
      const bridge = channel.objects.bridge;
      window.windowBridge = channel.objects.windowBridge;
      window.configBridge = channel.objects.configBridge;
      window.browserAutomation = channel.objects.browserAutomation;
      window.cliBridge = channel.objects.cliBridge;
      loadDesktopPetToggle();

      if (bridge) bindBridge(bridge);

      // Init sidebar if ConversationManager is available
      // (引擎初始化可能较慢，需轮询等待)
      _initSidebar(channel);
    });
  }

  function _initSidebar(channel) {
    if (!window.PawSidebar) return;
    const cm = channel.objects.conversationManager;
    if (cm && typeof cm.get_sessions === 'function') {
      window.PawSidebar.init(cm);
      log("sidebar initialized");
    } else {
      // 引擎还没就绪，等 500ms 再试
      log("sidebar: waiting for conversationManager...");
      setTimeout(function () { _initSidebar(channel); }, 500);
    }
  }

  /* ── Event binding ─────────────────────────────── */
  function bindEvents() {
    el.sendBtn.addEventListener("click", submitMessage);

    // 全局中断按钮——独立于输入框，始终存在
    if (el.taskStopBtn) {
      el.taskStopBtn.addEventListener("click", function () {
        if (!state.isTaskRunning || state.isCancelling) return;
        console.log("[Cancel] global stop button clicked");
        appState.dispatch({ type: "TASK_CANCELLING", value: true });
        if (state.bridge) state.bridge.cancelCurrentTask();
      });
    }

    el.messageInput.addEventListener("input", resizeComposer);
    el.messageInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        submitMessage();
      }
    });

    el.attachBtn.addEventListener("click", () => {
      appState.dispatch({ type: "TRANSIENT_STATUS", text: "附件功能预留", ttl: 900 });
    });

    // Window controls
    el.minimizeBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      callWindowBridge("minimizeWindow", "minimize");
    });
    el.maximizeBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      callWindowBridge("maximizeRestoreWindow", "maximize");
    });
    el.closeBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      callWindowBridge("closeWindow", "close");
    });

    // Settings
    PawSettings.bindEvents(el.settingsBtn);

    if (el.desktopPetTopToggle && el.desktopPetTopCheckbox) {
      el.desktopPetTopToggle.addEventListener("pointerdown", function (e) { e.stopPropagation(); });
      el.desktopPetTopToggle.addEventListener("mousedown", function (e) { e.stopPropagation(); });
      el.desktopPetTopCheckbox.addEventListener("change", function () {
        saveDesktopPetEnabled(el.desktopPetTopCheckbox.checked);
      });
    }

    log("events bound");
  }

  /* ── Boot ──────────────────────────────────────── */
  document.addEventListener("DOMContentLoaded", function () {
    if (window.__PAWMATE_APP_BOOTED__) {
      log("app already booted, skipping duplicate init");
      return;
    }
    window.__PAWMATE_APP_BOOTED__ = true;
    log("DOMContentLoaded");
    initElements();
    appState.subscribe(renderAppState);

    // Skills Hall (if loaded)
    if (window.PawMateSkillsHall && typeof window.PawMateSkillsHall.init === "function") {
      window.PawMateSkillsHall.init({
        state: state,
        escapeHtml: PawChat.escapeHtml,
        showToast: PawSettings.showToast,
        bridgeClient: window.PawMateBridge || null,
        dom: window.PawMateDom || null,
        toast: window.PawMateToast || null,
        clawhub: window.PawMateSkillsClawHub || null,
        importer: window.PawMateSkillsImport || null,
      });
    }

    if (window.PawMemoryHall && typeof window.PawMemoryHall.init === "function") {
      window.PawMemoryHall.init({
        escapeHtml: PawChat.escapeHtml,
        showToast: PawSettings.showToast,
      });
    }

    bindEvents();
    resizeComposer();
    initWebChannel();

    // 标题栏所有图标按钮统一声明：mousedown 不冒泡到 Qt 窗口 drag
    document.querySelectorAll(".top-bar .icon-btn").forEach(function (btn) {
      btn.addEventListener("mousedown", function (e) { e.stopPropagation(); });
    });

    el.messageInput.focus();
    log("init complete");
  });

})();
