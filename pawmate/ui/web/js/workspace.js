(function () {
  "use strict";

  var panel, body, title, subtitle, refreshButton, browserSidecar, browserSidecarResizer;
  var active = "chat";
  var browserSidecarOpen = false;
  var browserSidecarWidth = 0;
  var browserSidecarResizeActive = false;
  var browserSidecarRevealTimer = null;
  var browserHostObscured = false;
  var currentDirectory = "";
  var currentFile = "";
  var logTimer = null;
  var browserTab = "compose";
  var browserPending = {};
  var browserGeneratedWorkflow = null;
  var browserEditingWorkflowId = "";
  var browserWorkflowInputs = "{}";
  var browserNotice = "";
  var browserNoticeType = "info";
  var browserConfigCache = null;
  var browserRequirementGoal = "";
  var browserRequirementUrl = "";
  var browserModelConsent = false;
  var browserStatusTimer = null;
  var browserCurrentRunId = "";
  var browserLastRun = null;
  var browserLastTrace = [];
  var browserTraceExpanded = false;
  var browserSurface = "cockpit";
  var cliTranscript = "";
  var cliHistory = [];
  var cliHistoryIndex = 0;
  var cliStatus = {state: "stopped", shell: "PowerShell", cwd: ""};
  var cliAgentStatus = {state: "unavailable", turnId: 0, available: false};
  var cliAgentLineOpen = false;
  var cliSignalBound = false;

  var views = {
    chat: ["\u5bf9\u8bdd", "\u4e0e PawMate \u534f\u4f5c"],
    browser: ["浏览器自动化", "Playwright / CDP 自动化控制台"],
    logs: ["日志", "PawMate 实时运行日志"],
    cli: ["Agent CLI", "PawMate 对话与显式 PowerShell"]
  };

  function svg(path) {
    return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="' + path + '"/></svg>';
  }

  function buildRail() {
    if (document.querySelector(".sidebar-rail")) return;
    var rail = document.createElement("nav");
    rail.className = "sidebar-rail";
    rail.setAttribute("aria-label", "\u5de5\u4f5c\u533a\u5bfc\u822a");
    var items = [
      ["chat", "\u5bf9\u8bdd", "M4 5h16v11H8l-4 4V5z"],
      ["browser", "自动化", "M3 5h18v14H3V5zm0 4h18M6 7h.01"],
      ["logs", "日志", "M4 5h16v14H4V5zm3 4h10M7 13h10M7 17h7"],
      ["cli", "CLI", "M5 7l4 5-4 5m6 0h8"]
    ];
    items.forEach(function (item) {
      var button = document.createElement("button");
      button.type = "button";
      button.className = "rail-btn" + (item[0] === "chat" ? " active" : "");
      button.dataset.workspace = item[0];
      button.title = item[1];
      button.innerHTML = svg(item[2]) + "<span>" + item[1] + "</span>";
      button.addEventListener("click", function () {
        open(item[0]);
        if (item[0] === "chat" && window.PawSidebar && typeof window.PawSidebar.open === "function") {
          window.PawSidebar.open();
        }
      });
      rail.appendChild(button);
    });
    var spacer = document.createElement("div");
    spacer.className = "rail-spacer";
    rail.appendChild(spacer);
    addUtilityButton(
      rail, "desktop-pet", "桌宠",
      "M12 3c-4.4 0-8 3.1-8 7v5c0 3.3 3.6 6 8 6s8-2.7 8-6v-5c0-3.9-3.6-7-8-7zm-3 8h.01M15 11h.01M9 16c2 1.3 4 1.3 6 0",
      function () {
        var checkbox = document.getElementById("desktopPetTopCheckbox");
        if (checkbox) checkbox.click();
      }
    );
    addUtilityButton(
      rail, "skills", "技能",
      "M12 2L2 7l10 5 10-5-10-5zM2 12l10 5 10-5M2 17l10 5 10-5",
      function () {
        var button = document.getElementById("skillsHallBtn");
        if (button) button.click();
      }
    );
    addUtilityButton(
      rail, "memory", "记忆",
      "M5 5c0-1 3-2 7-2s7 1 7 2-3 2-7 2-7-1-7-2zm0 0v5c0 1 3 2 7 2s7-1 7-2V5m-14 5v5c0 1 3 2 7 2s7-1 7-2v-5m-14 5v4c0 1 3 2 7 2s7-1 7-2v-4",
      function () {
        var button = document.getElementById("memoryHallBtn");
        if (button) button.click();
      }
    );
    var settings = document.createElement("button");
    settings.type = "button";
    settings.className = "rail-btn";
    settings.title = "\u8bbe\u7f6e";
    settings.innerHTML = svg("M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8zm0-5v2m0 14v2M3 12h2m14 0h2M5.6 5.6 7 7m10 10 1.4 1.4M18.4 5.6 17 7M7 17l-1.4 1.4") + "<span>\u8bbe\u7f6e</span>";
    settings.addEventListener("click", function () {
      if (window.PawWorkspace && typeof window.PawWorkspace.close === "function") {
        window.PawWorkspace.close();
      }
      var original = document.getElementById("settingsBtn");
      if (original) original.click();
    });
    rail.appendChild(settings);
    document.querySelector(".app-shell").appendChild(rail);

    var petCheckbox = document.getElementById("desktopPetTopCheckbox");
    var petRailButton = rail.querySelector('[data-utility="desktop-pet"]');
    function syncPetState() {
      if (petRailButton && petCheckbox) petRailButton.classList.toggle("active", !!petCheckbox.checked);
    }
    if (petCheckbox) petCheckbox.addEventListener("change", syncPetState);
    syncPetState();
  }

  function addUtilityButton(rail, name, label, path, handler) {
    var button = document.createElement("button");
    button.type = "button";
    button.className = "rail-btn rail-utility-btn";
    button.dataset.utility = name;
    button.title = label;
    button.innerHTML = svg(path) + "<span>" + label + "</span>";
    button.addEventListener("click", handler);
    rail.appendChild(button);
    return button;
  }

  function setActive(name) {
    document.querySelectorAll(".rail-btn[data-workspace]").forEach(function (button) {
      button.classList.toggle("active", button.dataset.workspace === name);
    });
  }

  function close() { open("chat"); }

  function open(name) {
    active = name;
    setActive(name);
    stopLogRefresh();
    stopBrowserStatusRefresh();
    panel.classList.toggle("browser-cockpit-mode", name === "browser" && browserSurface === "cockpit");
    if (name === "chat") {
      panel.hidden = true;
      applyBrowserSidecarState();
      return;
    }
    applyBrowserSidecarState();
    if (name !== "browser") setBrowserHostVisibility(false);
    panel.hidden = false;
    title.textContent = views[name][0];
    subtitle.textContent = views[name][1];
    render();
  }

  function render() {
    if (active === "logs") return renderLogs();
    if (active === "cli") return renderCli();
    if (active === "browser") return renderBrowser();
  }

  function renderBrowser() {
    if (browserSurface === "workbench") return renderBrowserWorkbench();
    panel.classList.add("browser-cockpit-mode");
    body.innerHTML = '<section class="browser-cockpit">' +
      '<aside class="browser-pilot">' +
        '<header class="browser-pilot-head"><div class="browser-pilot-icon">' + svg("M3 5h18v14H3V5zm0 4h18M6 7h.01") + '</div>' +
          '<div><span class="browser-kicker">PLAYWRIGHT RPA</span><h3>任务驾驶舱</h3></div>' +
          '<span class="browser-dashboard-status is-loading" data-browser-status><i></i><em>连接中</em></span></header>' +
        '<div class="browser-pilot-form">' +
          '<label class="browser-field browser-task-field"><span>告诉 PawMate 要做什么</span><textarea id="browserGoal" rows="5" placeholder="例如：打开后台，筛选今天的订单，把订单号和金额整理出来。">' + escapeHtml(browserRequirementGoal) + '</textarea></label>' +
          '<label class="browser-field browser-url-field"><span>从哪个页面开始（可选）</span><input id="browserStartUrl" type="url" placeholder="https://example.com" value="' + escapeAttr(browserRequirementUrl) + '" /></label>' +
          '<label class="browser-consent browser-cockpit-consent"><input id="browserModelConsent" type="checkbox"' + (browserModelConsent ? ' checked' : '') + ' /><span>允许当前模型读取执行所需的页面内容</span></label>' +
          '<button class="browser-start-button" id="browserStartAgent" type="button"><span>开始执行</span><small>Playwright 将逐步观察并操作</small></button>' +
        '</div>' +
        '<section class="browser-stack-card"><div class="browser-stack-head"><div><span class="browser-kicker">LIVE STACK</span><h4>执行步骤</h4></div><span data-browser-stack-count>等待任务</span></div>' +
          '<div class="browser-stack-progress"><i data-browser-progress></i></div>' +
          '<ol class="browser-step-stack" data-browser-stack><li class="is-next"><b>1</b><div><strong>等待开始</strong><span>任务会按真实页面状态逐步展开</span></div></li></ol>' +
          '<button class="browser-trace-toggle" data-browser-trace-toggle type="button" hidden>查看 Playwright 轨迹</button>' +
          '<pre class="browser-cockpit-trace" data-browser-trace hidden></pre>' +
        '</section>' +
        '<div class="browser-pilot-actions"><button id="browserStopRun" class="danger" type="button" disabled>停止运行</button><button id="browserTakeOver" type="button">手动接管</button></div>' +
        '<button class="browser-settings-link" id="browserOpenSettings" type="button">浏览器设置</button>' +
      '</aside>' +
      '<main class="browser-native-stage">' +
        '<div class="browser-shell-toolbar"><div class="browser-controls"><button type="button" aria-label="后退">←</button><button type="button" aria-label="前进">→</button><button type="button" aria-label="刷新">↻</button></div>' +
          '<div class="browser-address-pill"><span>●</span><b data-browser-address>由 PawMate browser_* 控制</b></div><button class="manual-button" id="browserTakeOverTop" type="button">人工接管</button></div>' +
        '<div class="browser-meta"><span><i class="browser-state-dot"></i><b>专属浏览器已嵌入</b></span><span>不是外部窗口，也不是 iframe</span></div>' +
        '<div class="browser-native-viewport" data-browser-native-stage><div class="browser-stage-empty"><div class="browser-loader"></div><strong>正在创建嵌入式浏览器</strong><span>启动任务后，PawMate 专属真实 Edge 会锁进这个区域。</span></div></div>' +
      '</main>' +
      '<footer class="browser-cockpit-footer"><span>DOM / ref 优先 · 每一步由 Playwright 实际页面结果驱动</span><span class="browser-workbench-notice ' + escapeAttr(browserNoticeType) + '" data-browser-notice>' + escapeHtml(browserNotice) + '</span></footer>' +
    '</section>';

    body.querySelector("#browserGoal").addEventListener("input", function (event) { browserRequirementGoal = event.target.value; });
    body.querySelector("#browserStartUrl").addEventListener("input", function (event) { browserRequirementUrl = event.target.value; });
    body.querySelector("#browserModelConsent").addEventListener("change", function (event) { browserModelConsent = event.target.checked; });
    body.querySelector("#browserStartAgent").addEventListener("click", function () { submitBrowserRequirement("agent"); });
    body.querySelector("#browserStopRun").addEventListener("click", function () { cancelCockpitRun(false); });
    body.querySelector("#browserTakeOver").addEventListener("click", function () { cancelCockpitRun(true); });
    body.querySelector("#browserTakeOverTop").addEventListener("click", function () { cancelCockpitRun(true); });
    body.querySelector("#browserOpenSettings").addEventListener("click", openCockpitBrowserSettings);
    body.querySelector("[data-browser-trace-toggle]").addEventListener("click", function () {
      browserTraceExpanded = !browserTraceExpanded;
      updateCockpitRun(browserLastRun, browserLastTrace);
    });
    if (browserLastRun) updateCockpitRun(browserLastRun, browserLastTrace);
    window.requestAnimationFrame(function () { syncBrowserHostGeometry(true); });
    startBrowserStatusRefresh();
  }

  function startBrowserStatusRefresh() {
    stopBrowserStatusRefresh();
    refreshBrowserStatus();
    browserStatusTimer = window.setInterval(refreshBrowserStatus, 800);
  }

  function stopBrowserStatusRefresh() {
    if (browserStatusTimer) window.clearInterval(browserStatusTimer);
    browserStatusTimer = null;
  }

  function setBrowserHostVisibility(visible) {
    var bridge = window.windowBridge;
    if (!bridge || typeof bridge.setBrowserHostGeometry !== "function") return;
    if (!visible) bridge.setBrowserHostGeometry(0, 0, 0, 0, false);
    else syncBrowserHostGeometry(true);
  }

  function finishBrowserSidecarReveal() {
    if (browserSidecarRevealTimer) window.clearTimeout(browserSidecarRevealTimer);
    browserSidecarRevealTimer = null;
    if (!browserSidecar || !browserSidecarOpen || active !== "chat") return;
    browserSidecar.classList.remove("is-entering");
    window.requestAnimationFrame(function () { syncBrowserHostGeometry(true); });
  }

  function applyBrowserSidecarState(animate) {
    if (!browserSidecar) return;
    var visible = browserSidecarOpen && active === "chat";
    var shell = document.querySelector(".app-shell");
    var wasHidden = browserSidecar.hidden;
    if (browserSidecarRevealTimer) {
      window.clearTimeout(browserSidecarRevealTimer);
      browserSidecarRevealTimer = null;
    }
    if (!visible) {
      browserSidecar.classList.remove("is-entering");
      browserSidecar.hidden = true;
      shell.classList.remove("browser-sidecar-open");
      window.requestAnimationFrame(function () {
        syncBrowserHostGeometry(active === "browser");
      });
      return;
    }

    setBrowserHostVisibility(false);
    if (animate && wasHidden) browserSidecar.classList.add("is-entering");
    browserSidecar.hidden = false;
    shell.classList.add("browser-sidecar-open");
    if (animate && wasHidden) {
      browserSidecar.getBoundingClientRect();
      window.requestAnimationFrame(function () {
        browserSidecar.classList.remove("is-entering");
      });
      var reducedMotion = window.matchMedia &&
        window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      browserSidecarRevealTimer = window.setTimeout(
        finishBrowserSidecarReveal,
        reducedMotion ? 0 : 280
      );
      return;
    }
    window.requestAnimationFrame(function () {
      syncBrowserHostGeometry(true);
    });
  }

  function showBrowserSidecar() {
    browserSidecarOpen = true;
    applyBrowserSidecarState(true);
  }

  function hideBrowserSidecar() {
    browserSidecarOpen = false;
    applyBrowserSidecarState(false);
  }

  function browserSidecarWidthBounds() {
    var railWidth = window.innerWidth <= 760 ? 58 : 72;
    var minimum = 360;
    var maximum = Math.max(minimum, window.innerWidth - railWidth - 300);
    return { minimum: minimum, maximum: maximum };
  }

  function setBrowserSidecarWidth(value, persist, syncHost) {
    var numeric = Number(value);
    if (!isFinite(numeric)) return;
    var bounds = browserSidecarWidthBounds();
    browserSidecarWidth = Math.round(Math.max(bounds.minimum, Math.min(bounds.maximum, numeric)));
    document.querySelector(".app-shell").style.setProperty(
      "--browser-sidecar-width",
      browserSidecarWidth + "px"
    );
    if (browserSidecarResizer) {
      browserSidecarResizer.setAttribute("aria-valuemin", String(bounds.minimum));
      browserSidecarResizer.setAttribute("aria-valuemax", String(bounds.maximum));
      browserSidecarResizer.setAttribute("aria-valuenow", String(browserSidecarWidth));
    }
    if (persist) {
      try { window.localStorage.setItem("pawmate.browserSidecarWidth", String(browserSidecarWidth)); }
      catch (_error) {}
    }
    if (syncHost && browserSidecarOpen && active === "chat") {
      window.requestAnimationFrame(function () { syncBrowserHostGeometry(true); });
    }
  }

  function restoreBrowserSidecarWidth() {
    var saved = "";
    try { saved = window.localStorage.getItem("pawmate.browserSidecarWidth") || ""; }
    catch (_error) {}
    if (saved) setBrowserSidecarWidth(saved, false, false);
  }

  function finishBrowserSidecarResize() {
    if (!browserSidecarResizeActive) return;
    browserSidecarResizeActive = false;
    document.documentElement.classList.remove("browser-sidecar-resizing");
    window.removeEventListener("pointermove", resizeBrowserSidecar);
    window.removeEventListener("pointerup", finishBrowserSidecarResize);
    window.removeEventListener("pointercancel", finishBrowserSidecarResize);
    setBrowserSidecarWidth(browserSidecarWidth, true, false);
    window.requestAnimationFrame(function () { syncBrowserHostGeometry(true); });
  }

  function resizeBrowserSidecar(event) {
    if (!browserSidecarResizeActive) return;
    setBrowserSidecarWidth(window.innerWidth - event.clientX, false, false);
  }

  function startBrowserSidecarResize(event) {
    if (event.button !== 0) return;
    event.preventDefault();
    browserSidecarResizeActive = true;
    browserSidecarWidth = Math.round(browserSidecar.getBoundingClientRect().width);
    document.documentElement.classList.add("browser-sidecar-resizing");
    setBrowserHostVisibility(false);
    window.addEventListener("pointermove", resizeBrowserSidecar);
    window.addEventListener("pointerup", finishBrowserSidecarResize);
    window.addEventListener("pointercancel", finishBrowserSidecarResize);
  }

  function resetBrowserSidecarWidth() {
    setBrowserSidecarWidth(window.innerWidth * 0.52, true, true);
  }

  function handleBrowserSidecarResizeKey(event) {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight" && event.key !== "Home") return;
    event.preventDefault();
    var current = browserSidecarWidth || browserSidecar.getBoundingClientRect().width;
    if (event.key === "Home") return resetBrowserSidecarWidth();
    setBrowserSidecarWidth(current + (event.key === "ArrowLeft" ? 24 : -24), true, true);
  }

  function syncBrowserHostGeometry(visible) {
    var bridge = window.windowBridge;
    var stage = null;
    if (browserHostObscured) visible = false;
    if (active === "browser") {
      stage = body && body.querySelector("[data-browser-native-stage]");
    } else if (active === "chat" && browserSidecarOpen && browserSidecar) {
      stage = browserSidecar.querySelector("[data-browser-sidecar-stage]");
    } else {
      visible = false;
    }
    if (!bridge || typeof bridge.setBrowserHostGeometry !== "function") return;
    if (!visible || !stage) {
      bridge.setBrowserHostGeometry(0, 0, 0, 0, false);
      return;
    }
    var rect = stage.getBoundingClientRect();
    bridge.setBrowserHostGeometry(
      Math.round(rect.left), Math.round(rect.top),
      Math.round(rect.width), Math.round(rect.height), true
    );
  }

  function setBrowserHostObscured(obscured) {
    browserHostObscured = !!obscured;
    if (browserHostObscured) {
      setBrowserHostVisibility(false);
      return;
    }
    window.requestAnimationFrame(function () {
      syncBrowserHostGeometry(
        active === "browser" || (active === "chat" && browserSidecarOpen)
      );
    });
  }

  function cancelCockpitRun(takeOver) {
    if (!browserCurrentRunId) {
      return setBrowserNotice(takeOver ? "右侧浏览器已经可以手动操作。" : "当前没有正在运行的任务。", "info");
    }
    browserCall("cancelRun", [browserCurrentRunId], function (reply) {
      trackBrowserRequest(reply, takeOver ? "take_over" : "cancel_run");
      if (takeOver) setBrowserNotice("正在暂停自动化；随后可直接操作右侧浏览器。", "info");
    });
  }

  function openCockpitBrowserSettings() {
    setBrowserHostVisibility(false);
    browserSurface = "workbench";
    browserTab = "settings";
    renderBrowser();
  }

  function browserTabButton(name, label, hint) {
    return '<button type="button" data-browser-tab="' + name + '" class="' + (browserTab === name ? "active" : "") + '">' +
      '<strong>' + label + '</strong><span>' + hint + '</span></button>';
  }

  function renderBrowserPane() {
    if (browserTab === "workflows") return renderBrowserWorkflows();
    if (browserTab === "runs") return renderBrowserRuns();
    if (browserTab === "settings") return renderBrowserSettings();
    renderBrowserCompose();
  }

  function renderBrowserWorkbench() {
    panel.classList.remove("browser-cockpit-mode");
    body.innerHTML = '<section class="browser-workbench">' +
      '<header class="browser-workbench-hero"><div class="browser-dashboard-icon">' +
        svg("M3 5h18v14H3V5zm0 4h18M6 7h.01") +
        '</div><div><span class="browser-kicker">PLAYWRIGHT / CDP</span><h3>浏览器自动化工作台</h3><p>需求、工作流、运行记录和浏览器设置共用同一套运行状态。</p></div>' +
        '<button class="browser-dashboard-back" id="browserBackCockpit" type="button">返回任务驾驶舱</button></header>' +
      '<nav class="browser-workbar">' +
        browserTabButton("compose", "需求编排", "生成或编辑工作流") +
        browserTabButton("workflows", "工作流", "运行已保存流程") +
        browserTabButton("runs", "运行记录", "查看状态与轨迹") +
        browserTabButton("settings", "设置", "运行方式与安全") +
      '</nav><main class="browser-workbench-pane" data-browser-pane></main>' +
      '<footer class="browser-workbench-footer"><span>浏览器配置与安全开关统一保存</span><span class="browser-workbench-notice ' +
        escapeAttr(browserNoticeType) + '" data-browser-notice>' + escapeHtml(browserNotice) + '</span></footer></section>';
    body.querySelector("#browserBackCockpit").addEventListener("click", function () {
      browserSurface = "cockpit";
      renderBrowser();
    });
    body.querySelectorAll("[data-browser-tab]").forEach(function (button) {
      button.addEventListener("click", function () {
        browserTab = button.dataset.browserTab;
        renderBrowserWorkbench();
      });
    });
    renderBrowserPane();
  }

  function renderBrowserCompose() {
    var pane = body.querySelector("[data-browser-pane]");
    if (!pane) return;
    var draft = browserGeneratedWorkflow ? JSON.stringify(browserGeneratedWorkflow, null, 2) : "";
    pane.innerHTML = '<div class="browser-compose-grid">' +
      '<section class="browser-panel browser-requirement-panel"><div class="browser-panel-heading"><span>01</span><div><h4>梳理自动化需求</h4><p>不需要先去聊天。直接说明目标、边界和期望结果。</p></div></div>' +
        '<label class="browser-field"><span>需要完成什么</span><textarea id="browserGoal" rows="8" placeholder="例如：打开供应商后台，筛选今天的订单，提取订单号和金额并生成可重复运行的工作流。请说明停止条件和不能执行的操作。">' + escapeHtml(browserRequirementGoal) + '</textarea></label>' +
        '<label class="browser-field"><span>起始网址（可选）</span><input id="browserStartUrl" type="url" placeholder="https://example.com" value="' + escapeAttr(browserRequirementUrl) + '" /></label>' +
        '<label class="browser-consent"><input id="browserModelConsent" type="checkbox"' + (browserModelConsent ? ' checked' : '') + ' /><span>允许把需求和必要的页面观察发送给当前模型</span></label>' +
        '<div class="browser-action-row"><button class="primary" id="browserStartAgent" type="button">直接让 AI 操作</button><button id="browserGenerateWorkflow" type="button">生成工作流草稿</button></div>' +
        '<button class="browser-chat-link" id="browserContinueInChat" type="button">也可以带到对话中继续讨论 →</button>' +
      '</section>' +
      '<section class="browser-panel browser-draft-panel"><div class="browser-panel-heading"><span>02</span><div><h4>工作流草稿</h4><p>AI 生成后先校验；也可以直接粘贴或编辑 DSL JSON。</p></div></div>' +
        '<textarea id="browserWorkflowJson" class="browser-json-editor" spellcheck="false" placeholder="生成的工作流 JSON 会显示在这里…">' + escapeHtml(draft) + '</textarea>' +
        '<div class="browser-validation" id="browserValidation">尚未校验</div>' +
        '<div class="browser-action-row"><button id="browserNewWorkflow" type="button">新建草稿</button><button id="browserValidateWorkflow" type="button">校验</button><button class="primary" id="browserSaveWorkflow" type="button">' + (browserEditingWorkflowId ? '保存新版本' : '保存到工作流') + '</button></div>' +
      '</section></div>';

    pane.querySelector("#browserGoal").addEventListener("input", function (event) { browserRequirementGoal = event.target.value; });
    pane.querySelector("#browserStartUrl").addEventListener("input", function (event) { browserRequirementUrl = event.target.value; });
    pane.querySelector("#browserModelConsent").addEventListener("change", function (event) { browserModelConsent = event.target.checked; });
    pane.querySelector("#browserStartAgent").addEventListener("click", function () { submitBrowserRequirement("agent"); });
    pane.querySelector("#browserGenerateWorkflow").addEventListener("click", function () { submitBrowserRequirement("generate"); });
    pane.querySelector("#browserContinueInChat").addEventListener("click", function () {
      var goal = pane.querySelector("#browserGoal").value.trim();
      openBrowserChat(goal ? "请帮我完善这个浏览器自动化需求：\n" + goal : "请帮我梳理一个浏览器自动化需求：");
    });
    pane.querySelector("#browserNewWorkflow").addEventListener("click", function () {
      browserEditingWorkflowId = "";
      browserGeneratedWorkflow = null;
      renderBrowserCompose();
    });
    pane.querySelector("#browserValidateWorkflow").addEventListener("click", validateBrowserDraft);
    pane.querySelector("#browserSaveWorkflow").addEventListener("click", saveBrowserDraft);
  }

  function submitBrowserRequirement(mode) {
    var goalNode = body.querySelector("#browserGoal");
    var urlNode = body.querySelector("#browserStartUrl");
    var consent = body.querySelector("#browserModelConsent");
    var goal = goalNode ? goalNode.value.trim() : "";
    var startUrl = urlNode ? urlNode.value.trim() : "";
    if (!goal) return setBrowserNotice("请先写清楚需要完成的任务。", "error");
    if (!consent || !consent.checked) return setBrowserNotice("AI 模式需要先勾选模型数据许可。", "error");
    var fullGoal = startUrl ? goal + "\n起始网址：" + startUrl : goal;
    if (mode === "agent") {
      browserCall("startAgentRun", [JSON.stringify({goal: fullGoal, allow_model_data: true, visibility: "foreground"})], function (result) {
        trackBrowserRequest(result, "start_agent");
      });
      return;
    }
    browserCall("generateWorkflow", [JSON.stringify({
      goal: goal,
      context: startUrl ? {start_url: startUrl} : {},
      save: false,
      allow_model_data: true
    })], function (result) { trackBrowserRequest(result, "generate_workflow"); });
  }

  function validateBrowserDraft() {
    var editor = body.querySelector("#browserWorkflowJson");
    if (!editor) return;
    var parsed = parseJsonObject(editor.value, "工作流 JSON");
    if (!parsed.ok) return showBrowserValidation(null, parsed.error);
    browserCall("validateWorkflow", [JSON.stringify(parsed.value)], function (result) {
      showBrowserValidation(result, result.ok ? "" : browserErrorMessage(result));
    });
  }

  function saveBrowserDraft() {
    var editor = body.querySelector("#browserWorkflowJson");
    if (!editor) return;
    var parsed = parseJsonObject(editor.value, "工作流 JSON");
    if (!parsed.ok) return showBrowserValidation(null, parsed.error);
    var payload = Object.assign({}, parsed.value);
    if (browserEditingWorkflowId) {
      delete payload.id;
      delete payload.version;
      delete payload.created_at;
      delete payload.updated_at;
      browserCall("updateWorkflow", [browserEditingWorkflowId, JSON.stringify(payload)], function (result) {
        trackBrowserRequest(result, "update_workflow");
      });
      return;
    }
    browserCall("createWorkflow", [JSON.stringify(payload)], function (result) {
      trackBrowserRequest(result, "create_workflow");
    });
  }

  function showBrowserValidation(result, error) {
    var node = body.querySelector("#browserValidation");
    if (!node) return;
    if (error) {
      node.className = "browser-validation error";
      node.textContent = error;
      return;
    }
    var issues = result.issues || [];
    node.className = "browser-validation " + (result.valid ? "success" : "error");
    node.textContent = result.valid ? "校验通过，可以保存。" : issues.map(function (item) {
      return item.path + " · " + item.message;
    }).join("\n");
  }

  function renderBrowserWorkflows() {
    var pane = body.querySelector("[data-browser-pane]");
    if (!pane) return;
    pane.innerHTML = '<section class="browser-panel browser-list-panel"><div class="browser-list-toolbar"><div><h4>工作流</h4><p>聊天、AI 生成和手工创建的工作流都会汇入这里。</p></div>' +
      '<label class="browser-inline-json"><span>本次运行输入 JSON</span><input id="browserWorkflowInputs" value="' + escapeAttr(browserWorkflowInputs) + '" /></label></div>' +
      '<div class="browser-card-list" data-workflow-list><div class="browser-list-empty">正在读取工作流…</div></div></section>';
    pane.querySelector("#browserWorkflowInputs").addEventListener("input", function (event) { browserWorkflowInputs = event.target.value; });
    browserCall("listWorkflows", [], function (result) {
      var list = body.querySelector("[data-workflow-list]");
      if (!list) return;
      if (!result.ok) { list.innerHTML = '<div class="browser-list-empty error">' + escapeHtml(browserErrorMessage(result)) + '</div>'; return; }
      if (!result.workflows.length) { list.innerHTML = '<div class="browser-list-empty">还没有工作流。去“需求编排”生成第一个。</div>'; return; }
      list.innerHTML = result.workflows.map(browserWorkflowCard).join("");
      list.querySelectorAll("[data-run-workflow]").forEach(function (button) {
        button.addEventListener("click", function () { runBrowserWorkflow(button.dataset.runWorkflow, button.dataset.dryRun === "1"); });
      });
      list.querySelectorAll("[data-edit-workflow]").forEach(function (button) {
        button.addEventListener("click", function () {
          var workflow = result.workflows.find(function (item) { return item.id === button.dataset.editWorkflow; });
          if (!workflow) return;
          browserEditingWorkflowId = workflow.id;
          browserGeneratedWorkflow = workflow;
          browserTab = "compose";
          renderBrowser();
        });
      });
    });
  }

  function browserWorkflowCard(workflow) {
    return '<article class="browser-record-card"><div class="browser-record-main"><span class="browser-record-kind">WORKFLOW</span><h5>' + escapeHtml(workflow.name) + '</h5>' +
      '<p>' + escapeHtml(workflow.description || "暂无描述") + '</p><small>' + escapeHtml(workflow.id) + ' · v' + workflow.version + ' · ' + (workflow.steps || []).length + ' 步</small></div>' +
      '<div class="browser-record-actions"><button data-edit-workflow="' + escapeAttr(workflow.id) + '">查看 JSON</button><button data-run-workflow="' + escapeAttr(workflow.id) + '" data-dry-run="1">试运行</button><button class="primary" data-run-workflow="' + escapeAttr(workflow.id) + '" data-dry-run="0">运行</button></div></article>';
  }

  function runBrowserWorkflow(workflowId, dryRun) {
    var parsed = parseJsonObject(browserWorkflowInputs || "{}", "运行输入 JSON");
    if (!parsed.ok) return setBrowserNotice(parsed.error, "error");
    browserCall("startWorkflowRun", [workflowId, JSON.stringify({inputs: parsed.value, dry_run: dryRun})], function (result) {
      trackBrowserRequest(result, dryRun ? "dry_run" : "start_workflow");
    });
  }

  function renderBrowserRuns() {
    var pane = body.querySelector("[data-browser-pane]");
    if (!pane) return;
    pane.innerHTML = '<section class="browser-panel browser-list-panel"><div class="browser-list-toolbar"><div><h4>运行记录</h4><p>查看检查点、失败原因和逐步 trace。</p></div><button id="browserRefreshRuns" type="button">刷新</button></div>' +
      '<div class="browser-card-list" data-run-list><div class="browser-list-empty">正在读取运行记录…</div></div><pre class="browser-trace-view" data-trace-view hidden></pre></section>';
    pane.querySelector("#browserRefreshRuns").addEventListener("click", renderBrowserRuns);
    browserCall("listRuns", ["100"], function (result) {
      var list = body.querySelector("[data-run-list]");
      if (!list) return;
      if (!result.ok) { list.innerHTML = '<div class="browser-list-empty error">' + escapeHtml(browserErrorMessage(result)) + '</div>'; return; }
      if (!result.runs.length) { list.innerHTML = '<div class="browser-list-empty">暂无运行记录。</div>'; return; }
      list.innerHTML = result.runs.map(browserRunCard).join("");
      list.querySelectorAll("[data-run-trace]").forEach(function (button) {
        button.addEventListener("click", function () { showBrowserTrace(button.dataset.runTrace); });
      });
      list.querySelectorAll("[data-run-cancel]").forEach(function (button) {
        button.addEventListener("click", function () {
          browserCall("cancelRun", [button.dataset.runCancel], function (reply) { trackBrowserRequest(reply, "cancel_run"); });
        });
      });
    });
  }

  function browserRunCard(run) {
    var activeRun = run.status === "queued" || run.status === "running";
    var detail = run.error && run.error.message ? run.error.message : (run.current_step || (run.completed_steps || []).length + " 个步骤已完成");
    return '<article class="browser-record-card"><div class="browser-record-main"><span class="browser-run-status status-' + escapeAttr(run.status) + '">' + escapeHtml(run.status) + '</span>' +
      '<h5>' + escapeHtml(run.workflow_id) + '</h5><p>' + escapeHtml(detail) + '</p><small>' + escapeHtml(run.id) + ' · ' + escapeHtml(run.created_at || "") + '</small></div>' +
      '<div class="browser-record-actions"><button data-run-trace="' + escapeAttr(run.id) + '">查看 trace</button>' +
      (activeRun ? '<button class="danger" data-run-cancel="' + escapeAttr(run.id) + '">取消</button>' : '') + '</div></article>';
  }

  function showBrowserTrace(runId) {
    browserCall("getTrace", [runId], function (result) {
      var view = body.querySelector("[data-trace-view]");
      if (!view) return;
      view.hidden = false;
      view.textContent = result.ok ? JSON.stringify(result.trace, null, 2) : browserErrorMessage(result);
      view.scrollIntoView({behavior: "smooth", block: "nearest"});
    });
  }

  function renderBrowserSettings() {
    var pane = body.querySelector("[data-browser-pane]");
    if (!pane) return;
    pane.innerHTML = '<section class="browser-panel browser-settings-panel"><div class="browser-list-empty">正在读取浏览器配置…</div></section>';
    bridgeCall("getConfig", [], function (cfg) {
      browserConfigCache = cfg || {};
      var currentPane = body.querySelector("[data-browser-pane]");
      if (!currentPane || browserTab !== "settings") return;
      var bu = (((cfg || {}).tools || {}).browser_use) || {};
      var security = ((cfg || {}).security) || {};
      currentPane.innerHTML = '<section class="browser-panel browser-settings-panel"><div class="browser-panel-heading"><span>CFG</span><div><h4>浏览器运行与安全设置</h4><p>Playwright / CDP、登录状态和演示启动权限统一在这里管理。</p></div></div>' +
        '<div class="browser-settings-grid">' +
          browserCheckbox("wbBrowserEnabled", "启用浏览器自动化", bu.enabled !== false) +
          browserSelect("wbBrowserType", "浏览器", bu.browser || "edge", [["auto","自动"],["edge","Microsoft Edge"],["chrome","Google Chrome"]]) +
          browserSelect("wbAttachMode", "连接方式", bu.attach_mode || "auto", [["auto","自动（推荐）"],["prefer","优先接管已打开浏览器"],["attach","只接管，失败则停止"],["new","总是新开浏览器"]]) +
          browserSelect("wbAutomationLevel", "自动化等级", bu.automation_level || "standard", [["conservative","保守"],["standard","标准"],["aggressive","允许屏幕操作兜底"]]) +
          browserSelect("wbProfile", "登录状态", bu.profile_directory === "managed" ? "managed" : "auto", [["auto","沿用登录状态"],["managed","全新空白浏览器"]]) +
          browserCheckbox("wbHeadless", "静默后台运行", !!bu.headless) +
          browserInput("wbTimeout", "超时时间（秒）", String(bu.timeout || 180), "number") +
          browserInput("wbCdpUrl", "自定义 CDP 地址（高级）", bu.cdp_url || "", "url") +
          browserInput("wbProfileCustom", "自定义 Profile 路径（高级）", ["auto","managed"].indexOf(bu.profile_directory) >= 0 ? "" : (bu.profile_directory || ""), "text") +
          '<label class="browser-setting checkbox browser-setting-wide"><input id="wbAllowBrowserProcessLaunch" type="checkbox"' +
            (security.allow_browser_process_launch === true ? " checked" : "") +
            ' /><span><strong>演示模式：允许命令启动浏览器</strong><small>仅放行受支持浏览器的单条启动命令；其他后台程序和危险命令仍会拦截。工具审批继续由“设置 → 工具”单独控制。</small></span></label>' +
        '</div><div class="browser-settings-note">前台模式会打开独立受控浏览器窗口；不会再嵌入 PawMate 主窗口。</div>' +
        '<div class="browser-action-row"><button class="primary" id="wbSaveSettings" type="button">保存自动化设置</button></div></section>';
      currentPane.querySelector("#wbSaveSettings").addEventListener("click", saveBrowserSettings);
    });
  }

  function browserCheckbox(id, label, checked) {
    return '<label class="browser-setting checkbox"><input id="' + id + '" type="checkbox"' + (checked ? " checked" : "") + ' /><span><strong>' + label + '</strong></span></label>';
  }

  function browserSelect(id, label, value, options) {
    return '<label class="browser-setting"><span>' + label + '</span><select id="' + id + '">' + options.map(function (item) {
      return '<option value="' + item[0] + '"' + (item[0] === value ? " selected" : "") + '>' + item[1] + '</option>';
    }).join("") + '</select></label>';
  }

  function browserInput(id, label, value, type) {
    return '<label class="browser-setting"><span>' + label + '</span><input id="' + id + '" type="' + type + '" value="' + escapeAttr(value) + '" /></label>';
  }

  function saveBrowserSettings() {
    if (!browserConfigCache || !window.configBridge) return setBrowserNotice("配置桥接尚未就绪。", "error");
    if (!browserConfigCache.llm || typeof browserConfigCache.llm !== "object") {
      return setBrowserNotice("没有读取到完整配置，已阻止覆盖保存。请刷新后重试。", "error");
    }
    var tools = browserConfigCache.tools = browserConfigCache.tools || {};
    var bu = tools.browser_use = tools.browser_use || {};
    bu.enabled = !!body.querySelector("#wbBrowserEnabled").checked;
    bu.browser = body.querySelector("#wbBrowserType").value;
    bu.attach_mode = body.querySelector("#wbAttachMode").value;
    bu.automation_level = body.querySelector("#wbAutomationLevel").value;
    bu.headless = !!body.querySelector("#wbHeadless").checked;
    bu.timeout = Math.max(5, Math.min(3600, parseInt(body.querySelector("#wbTimeout").value, 10) || 180));
    bu.cdp_url = body.querySelector("#wbCdpUrl").value.trim();
    bu.profile_directory = body.querySelector("#wbProfileCustom").value.trim() || body.querySelector("#wbProfile").value;
    var security = browserConfigCache.security = browserConfigCache.security || {};
    security.allow_browser_process_launch = !!body.querySelector("#wbAllowBrowserProcessLaunch").checked;
    window.configBridge.saveConfig(JSON.stringify(browserConfigCache));
    setBrowserNotice("浏览器自动化设置已提交保存。", "success");
  }

  function trackBrowserRequest(result, operation) {
    if (!result || !result.ok) return setBrowserNotice(browserErrorMessage(result), "error");
    if (!result.accepted || !result.request_id) return setBrowserNotice("自动化服务没有接受请求。", "error");
    browserPending[result.request_id] = operation;
    setBrowserNotice("请求已提交，正在后台执行…", "info");
  }

  function handleBrowserOperation(requestId, raw) {
    var result;
    try { result = JSON.parse(raw || "{}"); }
    catch (error) { result = {ok: false, error: {message: String(error)}}; }
    var operation = browserPending[requestId] || result.operation || "";
    delete browserPending[requestId];
    if (!result.ok) {
      setBrowserNotice(browserErrorMessage(result), "error");
      return;
    }
    if (operation === "generate_workflow") {
      browserEditingWorkflowId = "";
      browserGeneratedWorkflow = result.result && result.result.workflow;
      browserTab = "compose";
      browserNotice = "工作流草稿已生成，请校验后保存。";
      browserNoticeType = "success";
      if (active === "browser") renderBrowser();
      return;
    }
    if (operation === "create_workflow" || operation === "update_workflow") {
      browserEditingWorkflowId = "";
      browserGeneratedWorkflow = null;
      browserTab = "workflows";
      browserNotice = "工作流已保存。";
      browserNoticeType = "success";
      if (active === "browser") renderBrowser();
      return;
    }
    if (operation === "start_agent") {
      browserCurrentRunId = result.result && result.result.id ? result.result.id : browserCurrentRunId;
      browserLastRun = result.result || browserLastRun;
      browserNotice = "任务已开始，右侧浏览器会跟随 Playwright 执行。";
      browserNoticeType = "success";
      if (active === "browser") {
        updateCockpitRun(browserLastRun, browserLastTrace);
        refreshBrowserStatus();
      }
      return;
    }
    if (["start_workflow", "dry_run"].indexOf(operation) >= 0) {
      browserNotice = "工作流已开始执行。";
      browserNoticeType = "success";
      if (active === "browser") refreshBrowserStatus();
      return;
    }
    if (operation === "cancel_run" || operation === "take_over") {
      browserNotice = "已提交取消请求。";
      browserNoticeType = "success";
      if (operation === "take_over") browserNotice = "自动化已停止，现在可以直接操作右侧浏览器。";
      if (active === "browser") refreshBrowserStatus();
    }
  }

  function setBrowserNotice(message, type) {
    browserNotice = String(message || "");
    browserNoticeType = type || "info";
    var node = body && body.querySelector("[data-browser-notice]");
    if (node) { node.className = "browser-workbench-notice " + browserNoticeType; node.textContent = browserNotice; }
  }

  function browserErrorMessage(result) {
    return result && result.error ? (result.error.message || result.error.code || String(result.error)) : "自动化请求失败";
  }

  function parseJsonObject(text, label) {
    try {
      var value = JSON.parse(text || "{}");
      if (!value || Array.isArray(value) || typeof value !== "object") throw new Error(label + " 必须是 JSON 对象");
      return {ok: true, value: value};
    } catch (error) {
      return {ok: false, error: label + " 格式错误：" + error.message};
    }
  }

  function openBrowserChat(prefill) {
    close();
    var input = document.getElementById("messageInput");
    if (!input) return;
    if (prefill && !input.value) input.value = prefill;
    input.focus();
    input.dispatchEvent(new Event("input", { bubbles: true }));
  }

  function browserCall(method, args, done) {
    var bridge = window.browserAutomation;
    if (!bridge || typeof bridge[method] !== "function") {
      done({ ok: false, error: { message: "自动化服务桥接尚未就绪" } });
      return;
    }
    var values = (args || []).slice();
    values.push(function (raw) {
      try { done(JSON.parse(raw || "{}")); }
      catch (error) { done({ ok: false, error: { message: String(error) } }); }
    });
    bridge[method].apply(bridge, values);
  }

  function refreshBrowserStatus() {
    if (active !== "browser") return;
    browserCall("getStatus", [], function (status) {
      var badge = body.querySelector("[data-browser-status]");
      if (!badge) return;
      badge.classList.remove("is-loading", "is-error", "is-busy");
      if (!status.ok || !status.ready) {
        badge.classList.add("is-error");
        badge.querySelector("em").textContent = status.error ? status.error.message : "引擎正在启动";
      } else if (status.busy) {
        badge.classList.add("is-busy");
        badge.querySelector("em").textContent = "任务执行中";
      } else {
        badge.querySelector("em").textContent = "自动化待命";
      }
      var startButton = body.querySelector("#browserStartAgent");
      var stopButton = body.querySelector("#browserStopRun");
      if (startButton) startButton.disabled = !status.ok || !status.ready || !!status.busy;
      if (stopButton) stopButton.disabled = !status.busy;
      if (status.active_run_id) browserCurrentRunId = status.active_run_id;
      if (browserCurrentRunId) refreshCockpitRun(browserCurrentRunId);
    });
  }

  function refreshCockpitRun(runId) {
    browserCall("getRun", [runId], function (reply) {
      if (!reply.ok || !reply.run) return;
      browserLastRun = reply.run;
      browserCall("getTrace", [runId], function (traceReply) {
        browserLastTrace = traceReply.ok && Array.isArray(traceReply.trace) ? traceReply.trace : [];
        updateCockpitRun(browserLastRun, browserLastTrace);
      });
    });
  }

  function updateCockpitRun(run, trace) {
    var stack = body && body.querySelector("[data-browser-stack]");
    if (!stack || !run) return;
    var completed = Array.isArray(run.completed_steps) ? run.completed_steps.length : 0;
    var status = String(run.status || "queued");
    var decisions = (trace || []).filter(function (item) { return item && item.event === "agent_decision"; }).map(function (item) {
      return item.data && item.data.decision ? item.data.decision : {};
    }).filter(function (item) { return item.done !== true && item.action; });
    var results = (trace || []).filter(function (item) { return item && item.event === "agent_action_result"; });
    var items = [];
    if (!decisions.length) {
      items.push({state: status === "failed" ? "error" : "current", title: "连接并观察页面", detail: "Playwright 正在读取真实页面状态"});
    } else {
      decisions.slice(-3).forEach(function (decision, index) {
        var absolute = decisions.length - Math.min(decisions.length, 3) + index;
        var isDone = absolute < results.length;
        items.push({
          state: isDone ? "done" : (status === "failed" ? "error" : "current"),
          title: browserActionLabel(decision.action),
          detail: String(decision.reason || "由当前页面状态生成")
        });
      });
    }
    if (status === "running" && items.every(function (item) { return item.state === "done"; })) {
      items.push({state: "current", title: "重新观察页面", detail: "等待 Playwright 返回下一步所需的信息"});
    }
    if (["succeeded", "completed"].indexOf(status) >= 0) {
      items.push({state: "done", title: "任务完成", detail: (run.outputs && run.outputs.summary) || "执行结果已经保存"});
    } else if (["failed", "cancelled", "approval_required"].indexOf(status) >= 0) {
      var failure = run.error && run.error.message ? run.error.message : (status === "cancelled" ? "任务已停止" : "需要处理后继续");
      items.push({state: status === "cancelled" ? "next" : "error", title: browserRunStatusLabel(status), detail: failure});
    }
    stack.innerHTML = items.slice(-4).map(function (item, index) {
      return '<li class="is-' + item.state + '"><b>' + (Math.max(1, completed - items.length + index + 2)) + '</b><div><strong>' + escapeHtml(item.title) + '</strong><span>' + escapeHtml(item.detail) + '</span></div></li>';
    }).join("");
    var count = body.querySelector("[data-browser-stack-count]");
    if (count) count.textContent = browserRunStatusLabel(status) + (completed ? " · " + completed + " 步" : "");
    var progress = body.querySelector("[data-browser-progress]");
    if (progress) progress.style.width = (["succeeded", "completed"].indexOf(status) >= 0 ? 100 : Math.min(92, 12 + completed * 12)) + "%";
    var toggle = body.querySelector("[data-browser-trace-toggle]");
    var traceView = body.querySelector("[data-browser-trace]");
    if (toggle) {
      toggle.hidden = !(trace || []).length;
      toggle.textContent = browserTraceExpanded ? "收起 Playwright 轨迹" : "查看 Playwright 轨迹";
    }
    if (traceView) {
      traceView.hidden = !browserTraceExpanded;
      traceView.textContent = browserTraceExpanded ? JSON.stringify(trace || [], null, 2) : "";
    }
  }

  function browserActionLabel(action) {
    return ({
      "browser.goto": "打开目标页面",
      "browser.read": "读取页面状态",
      "browser.click": "点击页面元素",
      "browser.fill": "填写表单",
      "browser.type": "输入内容",
      "browser.smart_type": "智能填写内容",
      "browser.key": "发送按键",
      "browser.hotkey": "执行快捷键",
      "browser.scroll": "滚动页面",
      "browser.extract": "提取页面信息",
      "browser.wait": "等待页面响应"
    })[action] || String(action || "执行下一步");
  }

  function browserRunStatusLabel(status) {
    return ({queued:"准备中",running:"执行中",succeeded:"已完成",completed:"已完成",failed:"执行失败",cancelled:"已停止",approval_required:"等待确认"})[status] || status;
  }

  function bridgeCall(method, args, done) {
    var bridge = window.configBridge;
    if (!bridge || typeof bridge[method] !== "function") {
      done({ ok: false, error: "\u5de5\u4f5c\u533a\u6865\u63a5\u5c1a\u672a\u5c31\u7eea" });
      return;
    }
    var values = (args || []).slice();
    values.push(function (raw) {
      try { done(JSON.parse(raw || "{}")); }
      catch (error) { done({ ok: false, error: String(error) }); }
    });
    bridge[method].apply(bridge, values);
  }

  function renderLogs() {
    body.innerHTML = '<pre class="log-view">正在读取 PawMate 日志…</pre>';
    refreshLogs();
    logTimer = window.setInterval(refreshLogs, 2000);
  }

  function refreshLogs() {
    if (active !== "logs") return;
    bridgeCall("getAppLogs", ["500"], function (result) {
      var logView = body.querySelector(".log-view");
      if (!logView) return;
      logView.textContent = result.text || result.error || "暂无日志";
      logView.scrollTop = logView.scrollHeight;
    });
  }

  function stopLogRefresh() {
    if (logTimer) window.clearInterval(logTimer);
    logTimer = null;
  }

  function cliCall(method, args, done) {
    var bridge = window.cliBridge;
    if (!bridge || typeof bridge[method] !== "function") {
      if (done) done({ok: false, error: "CLI 桥接尚未就绪"});
      return;
    }
    var values = (args || []).slice();
    values.push(function (raw) {
      var result;
      try { result = JSON.parse(raw || "{}"); }
      catch (error) { result = {ok: false, error: String(error)}; }
      if (done) done(result);
    });
    bridge[method].apply(bridge, values);
  }

  function appendCliOutput(text) {
    cliTranscript += String(text || "");
    if (cliTranscript.length > 240000) cliTranscript = cliTranscript.slice(-200000);
    var output = body && body.querySelector(".cli-output");
    if (active === "cli" && output) {
      output.textContent = cliTranscript || "[CLI] 等待输入命令…\n";
      output.scrollTop = output.scrollHeight;
    }
  }

  function appendAgentOutput(text) {
    var value = String(text || "");
    if (!value) return;
    if (!cliAgentLineOpen) {
      appendCliOutput("PawMate> ");
      cliAgentLineOpen = true;
    }
    appendCliOutput(value);
  }

  function applyCliAgentStatus(raw) {
    var next = raw;
    if (typeof raw === "string") {
      try { next = JSON.parse(raw || "{}"); }
      catch (_error) { next = {state: "error", error: raw}; }
    }
    if (next && typeof next === "object") {
      var previousTurn = Number(cliAgentStatus.turnId || 0);
      cliAgentStatus = Object.assign({}, cliAgentStatus, next);
      if (previousTurn && !Number(cliAgentStatus.turnId || 0) && cliAgentLineOpen) {
        appendCliOutput("\r\n");
        cliAgentLineOpen = false;
      }
    }
    var status = body && body.querySelector("[data-cli-agent-status]");
    if (active === "cli" && status) {
      var labels = {
        ready: "Agent 就绪",
        thinking: "思考中",
        working: "调用工具",
        answering: "回答中",
        cancelling: "取消中",
        error: "Agent 错误",
        unavailable: "Agent 未就绪"
      };
      status.className = "cli-status is-" + escapeAttr(cliAgentStatus.state || "unavailable");
      status.textContent = labels[cliAgentStatus.state] || cliAgentStatus.state;
    }
  }

  function appendCliToolEvent(raw) {
    var payload;
    try { payload = JSON.parse(raw || "{}"); }
    catch (_error) { payload = {type: "working", name: "tool"}; }
    if (cliAgentLineOpen) {
      appendCliOutput("\r\n");
      cliAgentLineOpen = false;
    }
    var type = payload.type === "start" ? "调用" : (payload.type === "error" ? "失败" : "完成");
    appendCliOutput("[工具" + type + "] " + (payload.name || "tool") + "\r\n");
  }

  function applyCliStatus(raw) {
    var next = raw;
    if (typeof raw === "string") {
      try { next = JSON.parse(raw || "{}"); }
      catch (_error) { next = {state: "stopped", error: raw}; }
    }
    if (next && typeof next === "object") cliStatus = Object.assign({}, cliStatus, next);
    var status = body && body.querySelector("[data-cli-status]");
    var cwd = body && body.querySelector("[data-cli-cwd]");
    if (active === "cli" && status) {
      status.className = "cli-status is-" + escapeAttr(cliStatus.state || "stopped");
      status.textContent = ({running: "运行中", starting: "启动中", stopped: "已停止"})[cliStatus.state] || cliStatus.state;
    }
    if (active === "cli" && cwd) cwd.textContent = cliStatus.cwd || "";
  }

  function renderCli() {
    body.innerHTML = '<section class="cli-workspace">' +
      '<header class="cli-toolbar"><div><strong>PawMate Agent CLI</strong>' +
        '<span data-cli-cwd>' + escapeHtml(cliStatus.cwd || "正在读取工作目录…") + '</span></div>' +
        '<div class="cli-toolbar-actions"><span class="cli-status is-' + escapeAttr(cliAgentStatus.state || "unavailable") +
          '" data-cli-agent-status>' + escapeHtml(cliAgentStatus.available ? "Agent 就绪" : "Agent 未就绪") +
          '</span><button type="button" data-cli-interrupt>取消 / Ctrl+C</button><button type="button" data-cli-restart>重启 Shell</button><button type="button" data-cli-clear>清屏</button></div></header>' +
      '<pre class="cli-output" aria-live="polite"></pre>' +
      '<form class="cli-input-row"><span>pawmate&gt;</span><input type="text" autocomplete="off" spellcheck="false" aria-label="Agent CLI 输入" placeholder="直接与 PawMate 对话；执行本机命令请用 /shell &lt;命令&gt;" /><button type="submit">发送</button></form>' +
      '<footer class="cli-warning">普通输入走 PawMate Agent；只有以 /shell 开头的内容才会在本机 PowerShell 执行。输入 /help 查看命令。</footer></section>';
    var output = body.querySelector(".cli-output");
    var input = body.querySelector(".cli-input-row input");
    output.textContent = cliTranscript || "PawMate Agent CLI 已就绪。\r\n输入 /help 查看用法。\r\n";
    output.scrollTop = output.scrollHeight;
    body.querySelector(".cli-input-row").addEventListener("submit", function (event) {
      event.preventDefault();
      var command = input.value.trim();
      if (!command) return;
      cliHistory.push(command);
      if (cliHistory.length > 200) cliHistory.shift();
      cliHistoryIndex = cliHistory.length;
      input.value = "";
      submitCliInput(command);
    });
    input.addEventListener("keydown", function (event) {
      if (event.key === "ArrowUp") {
        event.preventDefault();
        if (cliHistoryIndex > 0) cliHistoryIndex -= 1;
        input.value = cliHistory[cliHistoryIndex] || "";
        input.setSelectionRange(input.value.length, input.value.length);
      } else if (event.key === "ArrowDown") {
        event.preventDefault();
        if (cliHistoryIndex < cliHistory.length) cliHistoryIndex += 1;
        input.value = cliHistory[cliHistoryIndex] || "";
      } else if (event.ctrlKey && event.key.toLowerCase() === "l") {
        event.preventDefault();
        clearCliOutput();
      } else if (event.ctrlKey && event.key.toLowerCase() === "c") {
        event.preventDefault();
        interruptCli();
      }
    });
    body.querySelector("[data-cli-interrupt]").addEventListener("click", interruptCli);
    body.querySelector("[data-cli-restart]").addEventListener("click", function () {
      appendCliOutput("\r\n[Shell] 正在重启 PowerShell…\r\n");
      cliCall("restart", [], applyCliStatus);
    });
    body.querySelector("[data-cli-clear]").addEventListener("click", clearCliOutput);
    cliCall("getStatus", [], applyCliStatus);
    cliCall("getAgentStatus", [], applyCliAgentStatus);
    window.setTimeout(function () { input.focus(); }, 0);
  }

  function submitCliInput(command) {
    if (command === "/help") {
      appendCliOutput(
        "命令：\r\n" +
        "  /shell <命令>  在本机 PowerShell 执行\r\n" +
        "  /cancel        取消当前 Agent 任务\r\n" +
        "  /clear         清空当前屏幕\r\n" +
        "  /help          显示此帮助\r\n" +
        "其他输入会发送给 PawMate Agent。\r\n"
      );
      return;
    }
    if (command === "/clear") {
      clearCliOutput();
      return;
    }
    if (command === "/cancel") {
      interruptCli();
      return;
    }
    if (command === "/shell" || command.indexOf("/shell ") === 0) {
      var shellCommand = command.slice(6).trim();
      if (!shellCommand) {
        appendCliOutput("[CLI] 用法：/shell <PowerShell 命令>\r\n");
        return;
      }
      appendCliOutput("PS> " + shellCommand + "\r\n");
      cliCall("writeLine", [shellCommand], function (result) {
        applyCliStatus(result);
        if (!result.ok) appendCliOutput("[Shell error] " + (result.error || "命令未执行") + "\r\n");
      });
      return;
    }

    if (cliAgentLineOpen) {
      appendCliOutput("\r\n");
      cliAgentLineOpen = false;
    }
    appendCliOutput("pawmate> " + command + "\r\n");
    cliCall("submitAgent", [command], function (result) {
      applyCliAgentStatus(result);
      if (!result.ok) appendCliOutput("[Agent error] " + (result.error || "任务未提交") + "\r\n");
    });
  }

  function interruptCli() {
    if (Number(cliAgentStatus.turnId || 0)) {
      cliCall("cancelAgent", [], function (result) {
        applyCliAgentStatus(result);
        if (!result.cancelled) appendCliOutput("[Agent] 当前没有可取消的任务。\r\n");
      });
      return;
    }
    if (cliStatus.state === "running") {
      cliCall("interrupt", [], applyCliStatus);
      return;
    }
    appendCliOutput("[CLI] 当前没有运行中的 Agent 任务或 Shell 命令。\r\n");
  }

  function clearCliOutput() {
    cliTranscript = "";
    cliAgentLineOpen = false;
    var output = body && body.querySelector(".cli-output");
    if (output) output.textContent = "[CLI] 屏幕已清空。\n";
  }

  function renderFiles(path) {
    currentDirectory = path || "";
    currentFile = "";
    body.innerHTML = '<aside class="file-browser"><div class="workspace-status">\u6b63\u5728\u8bfb\u53d6\u6587\u4ef6\u2026</div></aside><section class="file-editor"><div class="workspace-empty"><strong>\u9009\u62e9\u6587\u4ef6</strong>\u4ece\u5de6\u4fa7\u6253\u5f00\u4e00\u4e2a UTF-8 \u6587\u672c\u6587\u4ef6\u3002</div></section>';
    bridgeCall("listWorkspaceFiles", [currentDirectory], function (result) {
      var browser = body.querySelector(".file-browser");
      if (!browser) return;
      if (!result.ok) {
        browser.innerHTML = '<div class="workspace-status">' + escapeHtml(result.error || "\u8bfb\u53d6\u5931\u8d25") + '</div>';
        return;
      }
      var html = '<div class="workspace-status">/' + escapeHtml(result.path || "") + '</div>';
      if (currentDirectory) html += '<button class="file-row" data-up="1">[D] <span>..</span></button>';
      (result.entries || []).forEach(function (entry) {
        html += '<button class="file-row" data-path="' + escapeAttr(entry.path) + '" data-dir="' + (entry.directory ? "1" : "0") + '">' +
          (entry.directory ? "[D]" : "[F]") + " <span>" + escapeHtml(entry.name) + "</span></button>";
      });
      browser.innerHTML = html;
      var up = browser.querySelector("[data-up]");
      if (up) up.addEventListener("click", function () {
        var bits = currentDirectory.split("/").filter(Boolean); bits.pop(); renderFiles(bits.join("/"));
      });
      browser.querySelectorAll("[data-path]").forEach(function (row) {
        row.addEventListener("click", function () {
          row.dataset.dir === "1" ? renderFiles(row.dataset.path) : openFile(row.dataset.path);
        });
      });
    });
  }

  function openFile(path) {
    bridgeCall("readWorkspaceFile", [path], function (result) {
      var editor = body.querySelector(".file-editor");
      if (!editor) return;
      if (!result.ok) {
        editor.innerHTML = '<div class="workspace-empty"><strong>\u65e0\u6cd5\u6253\u5f00</strong>' + escapeHtml(result.error || "") + '</div>';
        return;
      }
      currentFile = path;
      editor.innerHTML = '<div class="file-editor-bar"><span>' + escapeHtml(path) + '</span><button class="workspace-save" type="button">\u4fdd\u5b58</button></div><textarea spellcheck="false"></textarea>';
      editor.querySelector("textarea").value = result.content || "";
      editor.querySelector(".workspace-save").addEventListener("click", saveFile);
    });
  }

  function saveFile() {
    var textarea = body.querySelector(".file-editor textarea");
    if (!textarea || !currentFile) return;
    bridgeCall("saveWorkspaceFile", [currentFile, textarea.value], function (result) {
      subtitle.textContent = result.ok ? "\u5df2\u4fdd\u5b58 " + currentFile : (result.error || "\u4fdd\u5b58\u5931\u8d25");
    });
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (ch) {
      return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch];
    });
  }
  function escapeAttr(value) { return escapeHtml(value); }

  function init() {
    panel = document.getElementById("workspacePanel");
    body = document.getElementById("workspaceBody");
    title = document.getElementById("workspaceTitle");
    subtitle = document.getElementById("workspaceSubtitle");
    refreshButton = document.getElementById("workspaceRefresh");
    browserSidecar = document.getElementById("browserSidecar");
    browserSidecarResizer = document.getElementById("browserSidecarResizer");
    if (!panel || !body) return;
    buildRail();
    if (browserSidecar) {
      restoreBrowserSidecarWidth();
      browserSidecar.addEventListener("transitionend", function (event) {
        if (event.target === browserSidecar && event.propertyName === "transform") {
          finishBrowserSidecarReveal();
        }
      });
      browserSidecar.querySelector("#browserSidecarClose").addEventListener("click", hideBrowserSidecar);
      browserSidecar.querySelector("#browserSidecarCockpit").addEventListener("click", function () {
        openBrowser();
      });
    }
    if (browserSidecarResizer) {
      browserSidecarResizer.addEventListener("pointerdown", startBrowserSidecarResize);
      browserSidecarResizer.addEventListener("dblclick", resetBrowserSidecarWidth);
      browserSidecarResizer.addEventListener("keydown", handleBrowserSidecarResizeKey);
    }
    document.getElementById("workspaceClose").addEventListener("click", close);
    refreshButton.addEventListener("click", render);
    var browserSignalAttempts = 0;
    function bindBrowserSignals() {
      var bridge = window.browserAutomation;
      if (!bridge) {
        browserSignalAttempts += 1;
        if (browserSignalAttempts < 40) window.setTimeout(bindBrowserSignals, 250);
        return;
      }
      if (bridge.stateChanged && typeof bridge.stateChanged.connect === "function") {
        bridge.stateChanged.connect(function () { refreshBrowserStatus(); });
      }
      if (bridge.operationFinished && typeof bridge.operationFinished.connect === "function") {
        bridge.operationFinished.connect(function (requestId, raw) {
          handleBrowserOperation(requestId, raw);
          refreshBrowserStatus();
        });
      }
    }
    bindBrowserSignals();
    var configSignalAttempts = 0;
    function bindConfigSignals() {
      var configBridge = window.configBridge;
      if (!configBridge) {
        configSignalAttempts += 1;
        if (configSignalAttempts < 40) window.setTimeout(bindConfigSignals, 250);
        return;
      }
      if (configBridge.configSaved && typeof configBridge.configSaved.connect === "function") {
        configBridge.configSaved.connect(function () {
          if (active === "browser" && browserTab === "settings") setBrowserNotice("浏览器自动化设置已保存。", "success");
        });
      }
      if (configBridge.configError && typeof configBridge.configError.connect === "function") {
        configBridge.configError.connect(function (message) {
          if (active === "browser" && browserTab === "settings") setBrowserNotice(message, "error");
        });
      }
    }
    bindConfigSignals();
    var cliSignalAttempts = 0;
    function bindCliSignals() {
      var cli = window.cliBridge;
      if (!cli) {
        cliSignalAttempts += 1;
        if (cliSignalAttempts < 40) window.setTimeout(bindCliSignals, 250);
        return;
      }
      if (cliSignalBound) return;
      cliSignalBound = true;
      if (cli.outputReady && typeof cli.outputReady.connect === "function") {
        cli.outputReady.connect(appendCliOutput);
      }
      if (cli.stateChanged && typeof cli.stateChanged.connect === "function") {
        cli.stateChanged.connect(applyCliStatus);
      }
      if (cli.agentOutputReady && typeof cli.agentOutputReady.connect === "function") {
        cli.agentOutputReady.connect(appendAgentOutput);
      }
      if (cli.agentStateChanged && typeof cli.agentStateChanged.connect === "function") {
        cli.agentStateChanged.connect(applyCliAgentStatus);
      }
      if (cli.agentToolEvent && typeof cli.agentToolEvent.connect === "function") {
        cli.agentToolEvent.connect(appendCliToolEvent);
      }
      cliCall("getStatus", [], applyCliStatus);
      cliCall("getAgentStatus", [], applyCliAgentStatus);
    }
    bindCliSignals();
    window.addEventListener("resize", function () {
      if (browserSidecarWidth) setBrowserSidecarWidth(browserSidecarWidth, false, false);
      if (active === "browser" || (active === "chat" && browserSidecarOpen)) {
        window.requestAnimationFrame(function () { syncBrowserHostGeometry(true); });
      }
    });
    window.addEventListener("beforeunload", function () { setBrowserHostVisibility(false); });
    function openBrowser(tab) {
      browserTab = tab || "compose";
      browserSurface = tab ? "workbench" : "cockpit";
      open("browser");
    }
    window.PawWorkspace = {
      open: open,
      openBrowser: openBrowser,
      close: close,
      setActive: setActive,
      showBrowserSidecar: showBrowserSidecar,
      hideBrowserSidecar: hideBrowserSidecar,
      setBrowserHostObscured: setBrowserHostObscured
    };
  }

  document.addEventListener("DOMContentLoaded", init);
})();
