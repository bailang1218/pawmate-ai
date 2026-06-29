/** PawMate Skills Hall — full logic extracted from app.js. */
(function () { "use strict";
  var _ctx = {};
  var _hallTab = "overview";
  var _isOpen = false;
  var _didBind = false;
  var _busyUntil = 0;
  var _loadTimer = null;
  var _hallRenderGeneration = 0;

  function getUiLang() {
    try {
      var st = window.PawSettings && typeof window.PawSettings.getState === "function" ? window.PawSettings.getState() : null;
      return (st && (st.uiLang || (st.config && st.config.ui && st.config.ui.language_mode))) || "zh-CN";
    } catch(e) {
      return "zh-CN";
    }
  }

  function __(key) {
    var dicts = window.__PAWMATE_I18N || {};
    var d = dicts[getUiLang()] || dicts["zh-CN"] || {};
    return d[key] || key;
  }

  function fmt(key, values) {
    var text = __(key);
    values = values || {};
    Object.keys(values).forEach(function(k) {
      text = text.replace(new RegExp("\\{" + k + "\\}", "g"), values[k]);
    });
    return text;
  }

  function statusLabel(status) {
    var map = {
      ready: "skillStatusReady",
      needs_review: "skillStatusNeedsReview",
      needs_setup: "skillStatusNeedsSetup",
      disabled: "skillStatusDisabled",
      quarantined: "skillStatusQuarantined",
    };
    return __(map[status] || status || "skillsErrorUnknown");
  }

  function localizeSkillsHallChrome() {
    var panel = document.getElementById("skillsHallPanel");
    if (!panel) return;
    var title = panel.querySelector(".skills-hall-hdr h2");
    var subtitle = panel.querySelector(".skills-hall-hdr p");
    var close = document.getElementById("skillsHallCloseBtn");
    var path = panel.querySelector(".skills-hall-path");
    if (title) title.textContent = __("skillsHallTitle");
    if (subtitle) subtitle.textContent = __("skillsHallSubtitle");
    if (close) close.setAttribute("aria-label", __("skillsHallClose"));
    if (path) path.textContent = __("skillsHallRepo");
    var tabLabels = {
      overview: "skillsTabOverview",
      installed: "skillsTabInstalled",
      builtin: "skillsTabBuiltin",
      clawhub: "skillsTabClawHub",
      import: "skillsTabImport",
    };
    Object.keys(tabLabels).forEach(function(tab) {
      var btn = panel.querySelector('[data-hall-tab="' + tab + '"]');
      if (btn) btn.textContent = __(tabLabels[tab]);
    });
  }

  function showHallConfirm(message) {
    return new Promise(function(resolve) {
      var backdrop = document.createElement("div");
      backdrop.className = "clawhub-modal-backdrop";
      backdrop.style.cssText = "position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(15,23,42,0.34);z-index:10000;display:flex;align-items:center;justify-content:center;";

      var card = document.createElement("div");
      card.className = "clawhub-modal-card";
      card.style.cssText = "width:380px;max-width:90vw;background:#fff;border:1px solid #d9e6ec;border-radius:14px;box-shadow:0 18px 52px rgba(15,23,42,0.20);overflow:hidden;";
      card.innerHTML =
        '<div style="padding:18px 20px;border-bottom:1px solid #e2e8f0;">' +
          '<h3 style="margin:0;color:#1f2d35;font-size:16px;">' + getSkillsHallContext().escapeHtml(__("skillsUninstall")) + '</h3>' +
        '</div>' +
        '<div style="padding:16px 20px;color:#52616b;font-size:13px;line-height:1.6;">' + getSkillsHallContext().escapeHtml(message) + '</div>' +
        '<div style="display:flex;justify-content:flex-end;gap:8px;padding:12px 20px 18px;border-top:1px solid #e2e8f0;">' +
          '<button class="hall-confirm-cancel hall-btn">' + getSkillsHallContext().escapeHtml(__("skillsCancel")) + '</button>' +
          '<button class="hall-confirm-ok hall-btn hall-btn-danger">' + getSkillsHallContext().escapeHtml(__("skillsUninstall")) + '</button>' +
        '</div>';

      function close(value) {
        if (backdrop.parentNode) backdrop.parentNode.removeChild(backdrop);
        resolve(value);
      }
      backdrop.appendChild(card);
      document.body.appendChild(backdrop);
      backdrop.addEventListener("click", function(e) {
        if (e.target === backdrop) close(false);
      });
      card.querySelector(".hall-confirm-cancel").addEventListener("click", function() { close(false); });
      card.querySelector(".hall-confirm-ok").addEventListener("click", function() { close(true); });
    });
  }

  function getSkillsHallContext() {
    var d = _ctx.dom || window.PawMateDom || null;
    var t = _ctx.toast || window.PawMateToast || null;
    return {
      state: _ctx.state || null,
      settingsState: _ctx.settingsState || null,
      bridge: (_ctx.state && _ctx.state.bridge) || _ctx.bridge || null,
      escapeHtml: (d && d.escapeHtml) || _ctx.escapeHtml || function(v) {
        return String(v||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
      },
      showToast: (t && t.show) || _ctx.showToast || function() {},
      bridgeClient: _ctx.bridgeClient || window.PawMateBridge || null,
      clawhub: _ctx.clawhub || window.PawMateSkillsClawHub || null,
      importer: _ctx.importer || window.PawMateSkillsImport || null,
    };
  }

  function getContextSummary() {
    var ctx = getSkillsHallContext();
    return {
      hasBridge: !!ctx.bridge,
      hasDom: !!window.PawMateDom,
      hasToast: !!window.PawMateToast,
      hasClawHub: !!ctx.clawhub,
      hasImporter: !!ctx.importer,
    };
  }

  function isCurrentHallRender(generation, tab) {
    return generation === _hallRenderGeneration && _hallTab === tab && _isOpen;
  }

  function deactivateDelegatedTabs(activeTab) {
    try {
      if (activeTab !== "clawhub" && window.PawMateSkillsClawHub && typeof window.PawMateSkillsClawHub.deactivate === "function") {
        window.PawMateSkillsClawHub.deactivate();
      }
    } catch(e) { console.warn("[SkillsHall] failed to deactivate ClawHub", e); }
    try {
      if (activeTab !== "import" && window.PawMateSkillsImport && typeof window.PawMateSkillsImport.deactivate === "function") {
        window.PawMateSkillsImport.deactivate();
      }
    } catch(e) { console.warn("[SkillsHall] failed to deactivate Import", e); }
  }

function initSkillsHall() {
    try {
      if (_didBind) return;
      _didBind = true;
      if (window.PawPanelManager) window.PawPanelManager.register("skills", window.PawMateSkillsHall);
      var btn = document.getElementById("skillsHallBtn");
      if (btn) {
        btn.addEventListener("pointerdown", function(e) { e.stopPropagation(); });
        btn.addEventListener("mousedown", function(e) { e.stopPropagation(); });
        btn.addEventListener("click", function(e) { e.stopPropagation(); toggleSkillsHall(); });
      }

      var close = document.getElementById("skillsHallCloseBtn");
      if (close) close.addEventListener("click", closeSkillsHall);
      localizeSkillsHallChrome();
      var overlay = document.getElementById("skillsHallOverlay");
      if (overlay) overlay.addEventListener("click", closeSkillsHall);
      document.addEventListener("keydown", function(e) {
        if (e.key === "Escape" && _isOpen) closeSkillsHall();
      });

      var tabs = document.querySelectorAll(".skills-hall-tabs");
      tabs.forEach(function(el) {
        el.addEventListener("click", function(e) {
          var t = e.target.closest("[data-hall-tab]");
          if (!t) return;
          _hallTab = t.dataset.hallTab;
          document.querySelectorAll("[data-hall-tab]").forEach(function(b) { b.classList.toggle("active", b.dataset.hallTab === _hallTab); });
          renderHallContent();
        });
      });

      var content = document.getElementById("skillsHallContent");
      if (content) content.addEventListener("click", handleHallClick);

      console.log("[SkillsHall] initialized");
    } catch(e) {
      console.error("[SkillsHall] init failed", e);
    }
  }

  function openSkillsHall() {
    if (_isOpen) return;
    if (window.PawPanelManager) window.PawPanelManager.requestOpen("skills");
    else if (window.PawSettings && typeof window.PawSettings.close === "function") window.PawSettings.close();
    var o = document.getElementById("skillsHallOverlay");
    var p = document.getElementById("skillsHallPanel");
    _isOpen = true;
    if (o) {
      o.style.display = "block";
      requestAnimationFrame(function() { o.classList.add("open"); });
    }
    if (p) {
      p.style.display = "flex";
      requestAnimationFrame(function() { p.classList.add("open"); });
    }
    if (window.PawPanelManager) window.PawPanelManager.setBodyLocked(true);
    else document.body.style.overflow = "hidden";
    localizeSkillsHallChrome();
    scheduleHallLoad();
  }

  function closeSkillsHall() {
    var o = document.getElementById("skillsHallOverlay");
    var p = document.getElementById("skillsHallPanel");
    if (!_isOpen && (!p || p.style.display === "none")) return;
    _isOpen = false;
    if (o) o.classList.remove("open");
    if (p) p.classList.remove("open");
    clearTimeout(_loadTimer);
    _hallRenderGeneration += 1;
    deactivateDelegatedTabs(null);
    if (window.PawPanelManager) {
      window.PawPanelManager.notifyClosed("skills");
      window.PawPanelManager.setBodyLocked(window.PawPanelManager.hasOpenPanel());
    } else {
      document.body.style.overflow = "";
    }
    setTimeout(function() {
      if (!_isOpen) {
        if (o) o.style.display = "none";
        if (p) p.style.display = "none";
      }
    }, 160);
  }

  function toggleSkillsHall() {
    if (Date.now() < _busyUntil) return;
    _busyUntil = Date.now() + 180;
    _isOpen ? closeSkillsHall() : openSkillsHall();
  }

  function scheduleHallLoad() {
    var el = document.getElementById("skillsHallContent");
    if (el && !el.innerHTML.trim()) el.innerHTML = '<div class="hall-center"><div class="hall-spinner"></div><p>' + __("skillsLoading") + '</p></div>';
    clearTimeout(_loadTimer);
    _loadTimer = setTimeout(function () {
      if (_isOpen) loadHallOverview();
    }, 0);
  }

  function loadHallOverview() {
    _hallTab = "overview";
    document.querySelectorAll("[data-hall-tab]").forEach(function(b) { b.classList.toggle("active", b.dataset.hallTab === "overview"); });
    renderHallContent();
  }

  function renderHallContent() {
    var ctx = getSkillsHallContext();

    var el = document.getElementById("skillsHallContent");
    if (!el) return;
    var generation = ++_hallRenderGeneration;
    var tab = _hallTab;
    deactivateDelegatedTabs(tab);
    el.innerHTML = '<p>' + __("skillsLoading") + '</p>';
    var loadingMarkup = '<p>' + __("skillsLoading") + '</p>';
    var loadTimeout = setTimeout(function() {
      if (isCurrentHallRender(generation, tab) && el && el.innerHTML === loadingMarkup) {
        el.innerHTML = '<div class="hall-empty"><p>' + __("skillsTimeout") + '</p></div>';
      }
    }, 5000);
    if (tab === "overview" || tab === "installed") {
      if (ctx.bridge && typeof ctx.bridge.listLocalSkills === "function") {
        ctx.bridge.listLocalSkills(function(raw) {
          clearTimeout(loadTimeout);
          if (!isCurrentHallRender(generation, tab)) return;
          try {
            var d = JSON.parse(raw);
            if (tab === "overview") renderHallOverview(d);
            else renderHallInstalled(d);
          } catch(e) {}
        });
      } else {
        el.innerHTML = '<div class="hall-empty"><p>' + __("skillsBridgeMissing") + '</p></div>';
      }
    } else if (tab === "builtin") {
      if (ctx.bridge && typeof ctx.bridge.listBuiltinSkills === "function") {
        ctx.bridge.listBuiltinSkills(function(raw) {
          clearTimeout(loadTimeout);
          if (!isCurrentHallRender(generation, tab)) return;
          try { renderHallBuiltin(JSON.parse(raw)); } catch(e) {}
        });
      } else {
        el.innerHTML = '<div class="hall-empty"><p>' + __("skillsBridgeMissing") + '</p></div>';
      }
    } else if (tab === "clawhub") {
      delegateTab(el, window.PawMateSkillsClawHub, "ClawHub");
    } else if (tab === "import") {
      delegateTab(el, window.PawMateSkillsImport, "Import");
    }
  }

  function renderHallOverview(data) {
    var el2 = document.getElementById("skillsHallContent");
    if (!el2) return;
    var items = (data && data.ok ? data.items : []) || [];
    var stats = { ready:0, needs_review:0, needs_setup:0, disabled:0, quarantined:0 };
    for (var i=0; i<items.length; i++) { var s=items[i].status; if (stats[s]!==undefined) stats[s]++; }
    var st = document.getElementById("skillsHallStats");
    if (st) st.textContent = __("skillsStatsInstalled") + ': ' + items.length + ' | ' + __("skillStatusReady") + ': ' + stats.ready + ' | ' + __("skillStatusNeedsReview") + ': ' + stats.needs_review + ' | ' + __("skillStatusDisabled") + ': ' + stats.disabled;

    el2.innerHTML = '<div class="hall-overview">' +
      '<div class="hall-stat-card"><div class="hall-stat-num">' + items.length + '</div><div class="hall-stat-label">' + __("skillsStatsInstalled") + '</div></div>' +
      '<div class="hall-stat-card"><div class="hall-stat-num">' + stats.ready + '</div><div class="hall-stat-label">' + __("skillStatusReady") + '</div></div>' +
      '<div class="hall-stat-card"><div class="hall-stat-num">' + stats.needs_review + '</div><div class="hall-stat-label">' + __("skillStatusNeedsReview") + '</div></div>' +
      '<div class="hall-stat-card"><div class="hall-stat-num">' + stats.disabled + '</div><div class="hall-stat-label">' + __("skillStatusDisabled") + '</div></div>' +
      '<p class="hall-safety">' + __("skillsSafetyOverview") + '</p>' +
      '</div>';
  }

  function renderHallInstalled(data) {
    var ctx = getSkillsHallContext();

    var el3 = document.getElementById("skillsHallContent");
    if (!el3) return;
    var items = (data && data.ok ? data.items : []) || [];
    if (!items.length) { el3.innerHTML = '<div class="hall-empty"><p>' + __("skillsNoInstalled") + '</p></div>'; return; }
    var h = '';
    for (var i=0; i<items.length; i++) {
      var s = items[i];
      var isRdy = s.status === "ready";
      h += '<div class="hall-card">';
      h += '<div class="hall-card-body">';
      h += '<div class="hall-card-info">';
      h += '<h4>' + ctx.escapeHtml(s.name || s.slug) + '</h4>';
      h += '<div class="hall-card-meta">' + ctx.escapeHtml(s.slug) + ' v' + ctx.escapeHtml(s.version||'?') + ' | ' + ctx.escapeHtml(s.source||'?') + '</div>';
      h += '<div class="hall-card-meta">' + ctx.escapeHtml(s.local_path||'') + '</div>';
      h += '<span class="hall-badge status-' + ctx.escapeHtml(s.status||'?') + '">' + ctx.escapeHtml(statusLabel(s.status)) + '</span>';
      h += '</div>';
      h += '<div class="hall-card-actions">';
      h += '<button class="hall-btn" data-hall-action="detail" data-slug="' + ctx.escapeHtml(s.slug) + '">' + __("skillsDetail") + '</button>';
      if (s.status !== "quarantined") {
        if (isRdy) {
          h += '<button class="hall-btn hall-btn-warn" data-hall-action="disable" data-slug="' + ctx.escapeHtml(s.slug) + '">' + __("skillsDisable") + '</button>';
        } else {
          h += '<button class="hall-btn hall-btn-prime" data-hall-action="enable" data-slug="' + ctx.escapeHtml(s.slug) + '" ' + ((s.status==="needs_review"||s.status==="needs_setup")?'title="' + ctx.escapeHtml(__("skillsNeedsReviewTitle")) + '"':'') + '>' + __("skillsEnable") + '</button>';
        }
      }
      h += '<button class="hall-btn hall-btn-danger" data-hall-action="uninstall" data-slug="' + ctx.escapeHtml(s.slug) + '">' + __("skillsUninstall") + '</button>';
      h += '</div></div></div>';
    }
    el3.innerHTML = h;
  }

  function renderHallBuiltin(data) {
    var ctx = getSkillsHallContext();

    var el4 = document.getElementById("skillsHallContent");
    if (!el4) return;
    var items = (data && data.ok ? data.items : []) || [];
    if (!items.length) { el4.innerHTML = '<div class="hall-empty"><p>' + __("skillsNoBuiltin") + '</p></div>'; return; }
    var h = '';
    for (var i=0; i<items.length; i++) {
      var s = items[i];
      h += '<div class="hall-card">';
      h += '<div class="hall-card-body">';
      h += '<div class="hall-card-info">';
      h += '<h4>' + ctx.escapeHtml(s.name || s.slug) + '</h4>';
      h += '<div class="hall-card-meta">' + ctx.escapeHtml(s.description||'') + '</div>';
      h += '</div>';
      h += '<div class="hall-card-actions">';
      if (s.installed) {
        h += '<span class="hall-badge status-ready">' + __("skillsStatsInstalled") + '</span>';
      } else {
        h += '<button class="hall-btn hall-btn-prime" data-hall-action="install-builtin" data-slug="' + ctx.escapeHtml(s.slug) + '">' + __("skillsInstallLocal") + '</button>';
      }
      h += '</div></div></div>';
    }
    el4.innerHTML = h;
  }

  function handleHallClick(e) {
    var btn = e.target.closest("button");
    if (!btn) return;
    var action = btn.dataset.hallAction;
    var slug = btn.dataset.slug;
    if (!action) return;
    try {
      if (action === "detail" && slug) showHallDetail(slug);
      else if (action === "enable") setSkillStatusHall(slug, "ready");
      else if (action === "disable") setSkillStatusHall(slug, "disabled");
      else if (action === "uninstall" && slug) {
        showHallConfirm(fmt("skillsUninstallConfirm", {slug: slug})).then(function(confirmed) {
          if (confirmed) doUninstallHall(slug);
        });
      }
      else if (action === "install-builtin" && slug) doInstallBuiltinHall(slug);
    } catch(err) {
      console.error("[SkillsHall] action error", err);
    }
  }

  function showHallDetail(slug) {
    var ctx = getSkillsHallContext();

    var el = document.getElementById("skillsHallContent");
    if (!el) return;
    el.innerHTML = '<p>' + __("skillsLoading") + '</p>';
    if (ctx.bridge && typeof ctx.bridge.getLocalSkillDetail === "function") {
      ctx.bridge.getLocalSkillDetail(slug, function(raw) {
        try {
          var d = JSON.parse(raw);
          if (d.ok) {
            var dt = d.detail || {};
            var md = d.skill_md || "";
            el.innerHTML = '<div class="hall-detail">' +
              '<button class="hall-btn" data-hall-action="back">' + __("skillsBack") + '</button>' +
              '<h5>' + ctx.escapeHtml(dt.name||slug) + '</h5>' +
              '<div class="hall-card-meta">' + ctx.escapeHtml(dt.slug) + ' v' + ctx.escapeHtml(dt.version||"?") + ' | ' + ctx.escapeHtml(dt.source||"?") + '</div>' +
              '<div class="hall-card-meta">' + ctx.escapeHtml(dt.local_path||"") + '</div>' +
              '<span class="hall-badge status-' + ctx.escapeHtml(dt.status||"?") + '">' + ctx.escapeHtml(statusLabel(dt.status)) + '</span>' +
              '<pre class="hall-md">' + ctx.escapeHtml(md.substring(0, 2000)) + '</pre>' +
              '</div>';
          } else { el.innerHTML = '<p class="hall-error">' + ctx.escapeHtml((d.error&&d.error.message)||__("skillsFailed")) + '</p>'; }
        } catch(e) {}
      });
    }
    // Back handler
    el.addEventListener("click", function(ev) {
      if (ev.target.closest("[data-hall-action=back]")) renderHallContent();
    }, { once: true });
  }

  function setSkillStatusHall(slug, newStatus) {
    var ctx = getSkillsHallContext();

    if (ctx.bridge && typeof ctx.bridge.setSkillStatus === "function") {
      ctx.bridge.setSkillStatus(slug, newStatus, function(raw) {
        try { var d = JSON.parse(raw); if (d.ok) renderHallContent(); } catch(e) {}
      });
    }
  }

  function doUninstallHall(slug) {
    var ctx = getSkillsHallContext();

    if (ctx.bridge && typeof ctx.bridge.uninstallLocalSkill === "function") {
      ctx.bridge.uninstallLocalSkill(slug, function(raw) {
        try { var d = JSON.parse(raw); if (d.ok) renderHallContent(); } catch(e) {}
      });
    }
  }

  function doInstallBuiltinHall(slug) {
    var ctx = getSkillsHallContext();

    if (ctx.bridge && typeof ctx.bridge.installBuiltinSkill === "function") {
      ctx.bridge.installBuiltinSkill(slug, function(raw) {
        try { var d = JSON.parse(raw); if (d.ok) renderHallContent(); } catch(e) {}
      });
    }
  }
  function delegateTab(el, mod, name) {
    try {
      if (mod && typeof mod.init === "function") {
        mod.init({ container: el });
        return;
      }
    } catch (e) { console.error("[SkillsHall] " + name + " module error", e); }
    el.innerHTML = "<div class=hall-placeholder><h4>" + getSkillsHallContext().escapeHtml(name) + "</h4><p>" + __("skillsModuleMissing") + "</p></div>";
  }

  function init(options) {
    _ctx = options || {};
    if (typeof initSkillsHall === 'function') {
      try { initSkillsHall(); } catch(e) { console.error('[SkillsHall] init failed', e); }
    }
  }
  function open_() { if (typeof openSkillsHall === 'function') openSkillsHall(); }
  function close_() { if (typeof closeSkillsHall === 'function') closeSkillsHall(); }
  function toggle_() { if (typeof toggleSkillsHall === 'function') toggleSkillsHall(); }
  function switchTab() { if (typeof loadHallOverview === 'function') loadHallOverview(); }
  function refresh() { if (typeof renderHallContent === 'function') renderHallContent(); }

  window.PawMateSkillsHall = {
    getContextSummary: getContextSummary,
    init: init,
    open: open_,
    close: close_,
    toggle: toggle_,
    isOpen: function() { return _isOpen; },
    switchTab: switchTab,
    refresh: refresh,
    renderOverview: renderHallOverview,
    renderInstalled: renderHallInstalled,
    renderBuiltin: renderHallBuiltin,
    handleClick: handleHallClick,
    showDetail: showHallDetail,
    setStatus: setSkillStatusHall,
    uninstall: doUninstallHall,
    installBuiltin: doInstallBuiltinHall,
  };
})();
