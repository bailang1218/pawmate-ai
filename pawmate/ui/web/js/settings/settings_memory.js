/* =============================================================
   PawMate Memory Hall
   Standalone memory manager for core memory, episodic notes and growth state.
   ============================================================= */

(function () {
  "use strict";

  var CURRENT_TAB = "core";
  var RETRY_MAX = 10;
  var RETRY_MS = 500;
  var _retryCount = 0;
  var _retryTimer = null;
  var _readyListener = false;
  var _didBind = false;
  var _isOpen = false;
  var _query = "";
  var _coreItems = [];
  var _episodicItems = [];
  var _coreByKey = {};
  var _episodicById = {};
  var _ctx = {};
  var _busyUntil = 0;
  var _loadTimer = null;

  var KEY_LABELS = {
    pawmate_name: "名字",
    security_rules: "安全规则",
    skills_capabilities: "技能能力",
  };

  function $id(id) { return document.getElementById(id); }
  function b(name) { return window.configBridge && typeof window.configBridge[name] === "function"; }
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }
  function labelKey(k) { return KEY_LABELS[k] || k; }
  function sourceLabel(s) {
    if (s === "manual") return "手动添加";
    if (s === "migration") return "迁移数据";
    return "AI 自动记录";
  }
  function sourceRangeLabel(it) {
    if (!it || !it.source_session_id) return "来源：独立摘要";
    var range = "";
    if (it.source_start_seq != null && it.source_end_seq != null) {
      range = " · 消息 " + it.source_start_seq + "-" + it.source_end_seq;
    }
    var time = "";
    if (it.source_started_at || it.source_ended_at) {
      time = " · " + fmtTime(it.source_started_at) + " → " + fmtTime(it.source_ended_at);
    }
    return "来源：对话 " + it.source_session_id + range + time;
  }
  function snippet(text, max) {
    var s = String(text == null ? "" : text).replace(/\s+/g, " ").trim();
    max = max || 120;
    return s.length > max ? s.slice(0, max) + "..." : s;
  }
  function fmtTime(ts) {
    if (!ts) return "";
    var d = new Date(ts * 1000);
    if (isNaN(d.getTime())) return "";
    var p = function (n) { return (n < 10 ? "0" : "") + n; };
    return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) +
      " " + p(d.getHours()) + ":" + p(d.getMinutes());
  }
  function toast(msg) {
    if (_ctx.showToast) _ctx.showToast(msg);
    else if (window.PawSettings && window.PawSettings.showToast) window.PawSettings.showToast(msg);
  }

  function apiSave(k, v, done) {
    if (b("createCoreMemory")) window.configBridge.createCoreMemory(k, v, "false", done);
    else if (done) done();
  }
  function apiUpdate(k, v, done) {
    if (b("updateCoreMemory")) window.configBridge.updateCoreMemory(k, v, "false", done);
    else if (done) done();
  }
  function apiDelete(k, done) {
    if (b("deleteCoreMemory")) window.configBridge.deleteCoreMemory(k, done);
    else if (done) done();
  }
  function apiPin(k, done) {
    if (b("togglePinCoreMemory")) window.configBridge.togglePinCoreMemory(k, done);
    else if (done) done();
  }
  function apiEpisodicUpdate(id, content, done) {
    if (b("updateEpisodicMemory")) window.configBridge.updateEpisodicMemory(String(id), content, done);
    else if (done) done();
  }
  function apiEpisodicDelete(id, done) {
    if (b("deleteEpisodicMemory")) window.configBridge.deleteEpisodicMemory(String(id), done);
    else if (done) done();
  }

  function init(ctx) {
    _ctx = ctx || _ctx || {};
    if (_didBind) return;
    _didBind = true;
    if (window.PawPanelManager) window.PawPanelManager.register("memory", window.PawMemoryHall);

    var btn = $id("memoryHallBtn");
    if (btn) {
      btn.addEventListener("pointerdown", function (e) { e.stopPropagation(); });
      btn.addEventListener("mousedown", function (e) { e.stopPropagation(); });
      btn.addEventListener("click", function (e) {
        e.stopPropagation();
        toggle();
      });
    }

    var closeBtn = $id("memoryHallCloseBtn");
    if (closeBtn) closeBtn.addEventListener("click", close);

    var overlay = $id("memoryHallOverlay");
    if (overlay) overlay.addEventListener("click", close);

    var search = $id("memoryHallSearch");
    if (search) search.addEventListener("input", function () {
      _query = search.value.trim().toLowerCase();
      renderCurrentList();
    });

    var refresh = $id("memoryHallRefreshBtn");
    if (refresh) refresh.addEventListener("click", function () {
      clearRetry();
      refreshCurrent();
    });

    var add = $id("memoryHallAddBtn");
    if (add) add.addEventListener("click", showAddModal);

    var side = document.querySelector(".memory-hall-side");
    if (side) side.addEventListener("click", function (e) {
      var btn2 = e.target.closest("[data-memory-tab]");
      if (!btn2) return;
      switchTab(btn2.getAttribute("data-memory-tab"));
    });

    var content = $id("memoryHallContent");
    if (content) content.addEventListener("click", handleContentClick);

    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && _isOpen) close();
    });

    listenReady();
  }

  function listenReady() {
    if (_readyListener) return;
    _readyListener = true;
    var bridge = window.configBridge;
    if (bridge && bridge.memoryReadyChanged && typeof bridge.memoryReadyChanged.connect === "function") {
      bridge.memoryReadyChanged.connect(function () {
        if (_isOpen) {
          prefetchCounts(CURRENT_TAB);
          refreshCurrent();
        }
      });
    }
  }

  function open() {
    if (_isOpen) return;
    if (window.PawPanelManager) window.PawPanelManager.requestOpen("memory");
    else {
      if (window.PawSettings && typeof window.PawSettings.close === "function") window.PawSettings.close();
      if (window.PawMateSkillsHall && typeof window.PawMateSkillsHall.close === "function") window.PawMateSkillsHall.close();
    }

    _isOpen = true;
    var overlay = $id("memoryHallOverlay");
    var panel = $id("memoryHallPanel");
    if (overlay) {
      overlay.style.display = "block";
      requestAnimationFrame(function () { overlay.classList.add("open"); });
    }
    if (panel) {
      panel.style.display = "flex";
      requestAnimationFrame(function () { panel.classList.add("open"); });
    }
    if (window.PawPanelManager) window.PawPanelManager.setBodyLocked(true);
    else document.body.style.overflow = "hidden";
    var targetTab = CURRENT_TAB || "core";
    prefetchCounts(targetTab);
    switchTab(targetTab, true);
  }

  function close() {
    var overlay = $id("memoryHallOverlay");
    var panel = $id("memoryHallPanel");
    if (!_isOpen && (!panel || panel.style.display === "none")) return;
    _isOpen = false;
    if (overlay) overlay.classList.remove("open");
    if (panel) panel.classList.remove("open");
    clearTimeout(_loadTimer);
    if (window.PawPanelManager) {
      window.PawPanelManager.notifyClosed("memory");
      window.PawPanelManager.setBodyLocked(window.PawPanelManager.hasOpenPanel());
    } else {
      document.body.style.overflow = "";
    }
    setTimeout(function () {
      if (!_isOpen) {
        if (overlay) overlay.style.display = "none";
        if (panel) panel.style.display = "none";
      }
    }, 160);
  }

  function toggle() {
    if (Date.now() < _busyUntil) return;
    _busyUntil = Date.now() + 180;
    _isOpen ? close() : open();
  }
  function isOpen() { return _isOpen; }

  function switchTab(tab, force) {
    if (!tab) tab = "core";
    if (!force && CURRENT_TAB === tab) return;
    CURRENT_TAB = tab;
    document.querySelectorAll("[data-memory-tab]").forEach(function (btn) {
      btn.classList.toggle("active", btn.getAttribute("data-memory-tab") === tab);
    });
    updateToolbar();
    clearRetry();
    scheduleRefresh();
  }

  function scheduleRefresh() {
    clearTimeout(_loadTimer);
    _loadTimer = setTimeout(function () {
      if (_isOpen) refreshCurrent();
    }, 0);
  }

  function updateToolbar() {
    var add = $id("memoryHallAddBtn");
    if (add) add.style.display = CURRENT_TAB === "core" ? "" : "none";
  }

  function refreshCurrent() {
    if (CURRENT_TAB === "core") loadCore();
    else if (CURRENT_TAB === "episodic") loadEpisodic();
    else renderGrowth();
  }

  function prefetchCounts(activeTab) {
    if (activeTab !== "core") prefetchCoreCount();
    if (activeTab !== "episodic") prefetchEpisodicCount();
  }

  function prefetchCoreCount() {
    if (!b("getCoreMemories")) return;
    window.configBridge.getCoreMemories(function (raw) {
      var data;
      try { data = JSON.parse(raw); }
      catch (e) { return; }
      if (!data.ready) return;
      _coreItems = data.items || [];
      updateCounts();
      if (CURRENT_TAB === "growth") renderGrowth();
    });
  }

  function prefetchEpisodicCount() {
    if (!b("getEpisodicMemories")) return;
    window.configBridge.getEpisodicMemories(function (raw) {
      var data;
      try { data = JSON.parse(raw); }
      catch (e) { return; }
      if (!data.ready) return;
      _episodicItems = data.items || [];
      updateCounts();
      if (CURRENT_TAB === "growth") renderGrowth();
    });
  }

  function renderCurrentList() {
    if (CURRENT_TAB === "core") renderCoreList();
    else if (CURRENT_TAB === "episodic") renderEpisodicList();
    else renderGrowth();
  }

  function scheduleRetry(fn) {
    if (_retryCount >= RETRY_MAX) return;
    clearTimeout(_retryTimer);
    _retryTimer = setTimeout(function () {
      _retryCount++;
      fn();
    }, RETRY_MS);
  }
  function clearRetry() {
    _retryCount = 0;
    clearTimeout(_retryTimer);
    _retryTimer = null;
  }

  function setLoading(text) {
    var content = $id("memoryHallContent");
    if (content) content.innerHTML = '<div class="memory-hall-empty">' + esc(text || "加载中...") + "</div>";
  }

  function updateCounts() {
    var core = $id("memoryCoreCount");
    var ep = $id("memoryEpisodicCount");
    if (core) core.textContent = _coreItems.length + " 条";
    if (ep) ep.textContent = _episodicItems.length + " 条";
  }

  function matches(text) {
    if (!_query) return true;
    return String(text || "").toLowerCase().indexOf(_query) !== -1;
  }

  function loadCore() {
    setLoading("长期记忆加载中...");
    if (!b("getCoreMemories")) {
      setLoading("记忆系统未就绪");
      return;
    }
    window.configBridge.getCoreMemories(function (raw) {
      var data;
      try { data = JSON.parse(raw); }
      catch (e) {
        setLoading("长期记忆加载失败，请稍后重试。");
        return;
      }
      if (!data.ready && data.status === "initializing") {
        setLoading("长期记忆正在初始化...");
        scheduleRetry(loadCore);
        return;
      }
      if (!data.ready && data.status === "error") {
        setLoading("长期记忆加载失败：" + (data.error || "未知错误"));
        return;
      }
      clearRetry();
      _coreItems = data.items || [];
      updateCounts();
      renderCoreList();
    });
  }

  function renderCoreList() {
    var content = $id("memoryHallContent");
    var stats = $id("memoryHallStats");
    if (!content || CURRENT_TAB !== "core") return;
    _coreByKey = {};
    var filtered = _coreItems.filter(function (m) {
      return matches([m.key, labelKey(m.key), m.value, m.source].join(" "));
    });
    var pinned = _coreItems.filter(function (m) { return !!m.pinned; }).length;
    if (stats) {
      stats.innerHTML = [
        statCard("长期记忆", _coreItems.length),
        statCard("置顶", pinned),
        statCard("当前筛选", filtered.length),
      ].join("");
    }
    if (!filtered.length) {
      content.innerHTML = '<div class="memory-hall-empty">没有匹配的长期记忆。</div>';
      return;
    }
    content.innerHTML = '<div class="memory-card-grid">' + filtered.map(function (m) {
      _coreByKey[String(m.key)] = m;
      return [
        '<article class="memory-card' + (m.pinned ? ' pinned' : '') + '" data-kind="core" data-k="' + esc(m.key) + '">',
        ' <button class="memory-card-open memory-item__open" type="button" data-act="open-core" data-k="' + esc(m.key) + '">',
        '  <span class="memory-card-top"><strong>' + esc(labelKey(m.key)) + '</strong>' + (m.pinned ? '<span class="memory-badge">置顶</span>' : '') + '</span>',
        '  <span class="memory-card-preview">' + esc(snippet(m.value, 180)) + '</span>',
        '  <span class="memory-card-meta">来源：' + esc(sourceLabel(m.source)) + '</span>',
        ' </button>',
        ' <div class="memory-card-actions">',
        '  <button data-act="pin-core" data-k="' + esc(m.key) + '">' + (m.pinned ? "取消置顶" : "置顶") + '</button>',
        '  <button data-act="edit-core" data-k="' + esc(m.key) + '">编辑</button>',
        '  <button data-act="delete-core" data-k="' + esc(m.key) + '">删除</button>',
        ' </div>',
        '</article>',
      ].join("");
    }).join("") + "</div>";
  }

  function loadEpisodic() {
    setLoading("上下文摘要加载中...");
    if (!b("getEpisodicMemories")) {
      setLoading("上下文摘要功能未就绪");
      return;
    }
    window.configBridge.getEpisodicMemories(function (raw) {
      var data;
      try { data = JSON.parse(raw); }
      catch (e) {
        setLoading("上下文摘要加载失败，请稍后重试。");
        return;
      }
      if (!data.ready && data.status === "initializing") {
        setLoading("上下文摘要正在初始化...");
        scheduleRetry(loadEpisodic);
        return;
      }
      if (!data.ready && data.status === "error") {
        setLoading("上下文摘要加载失败：" + (data.error || "未知错误"));
        return;
      }
      clearRetry();
      _episodicItems = data.items || [];
      updateCounts();
      renderEpisodicList();
    });
  }

  function renderEpisodicList() {
    var content = $id("memoryHallContent");
    var stats = $id("memoryHallStats");
    if (!content || CURRENT_TAB !== "episodic") return;
    _episodicById = {};
    var filtered = _episodicItems.filter(function (it) {
      return matches([it.title, it.content, it.tags, it.source_session_id].join(" "));
    });
    var linked = _episodicItems.filter(function (it) { return !!it.source_session_id; }).length;
    if (stats) {
      stats.innerHTML = [
        statCard("上下文摘要", _episodicItems.length),
        statCard("有关联来源", linked),
        statCard("当前筛选", filtered.length),
      ].join("");
    }
    if (!filtered.length) {
      content.innerHTML = '<div class="memory-hall-empty">没有匹配的上下文摘要。</div>';
      return;
    }
    content.innerHTML = '<div class="memory-timeline">' + filtered.map(function (it) {
      _episodicById[String(it.rowid)] = it;
      var body = String(it.content || "");
      var title = it.title || snippet(body, 24);
      var tags = it.tags ? String(it.tags).split(/\s+/).filter(Boolean).slice(0, 8) : [];
      return [
        '<article class="memory-episode" data-rowid="' + esc(it.rowid) + '">',
        ' <button class="memory-episode-open" type="button" data-act="open-episodic" data-rowid="' + esc(it.rowid) + '">',
        '  <span class="memory-episode-head">',
        '   <strong>' + esc(title) + '</strong>',
        '   <small>' + esc(fmtTime(it.created_at)) + '</small>',
        '  </span>',
        '  <span class="memory-episode-preview">' + esc(snippet(body, 220)) + '</span>',
        '  <span class="memory-episode-meta memory-source">' + esc(sourceRangeLabel(it)) + '</span>',
        ' </button>',
        tags.length ? ' <div class="memory-chip-row">' + tags.map(function (t) { return '<span class="memory-chip">#' + esc(t.replace(/^#/, "")) + '</span>'; }).join("") + '</div>' : '',
        ' <div class="memory-card-actions">',
        '  <button data-act="edit-episodic" data-rowid="' + esc(it.rowid) + '">编辑</button>',
        (it.source_session_id ? '  <button data-act="source-episodic" data-rowid="' + esc(it.rowid) + '">来源</button>' : ''),
        '  <button data-act="delete-episodic" data-rowid="' + esc(it.rowid) + '">删除</button>',
        ' </div>',
        '</article>',
      ].join("");
    }).join("") + "</div>";
  }

  function renderGrowth() {
    var stats = $id("memoryHallStats");
    var content = $id("memoryHallContent");
    if (stats) {
      stats.innerHTML = [
        statCard("长期记忆", _coreItems.length),
        statCard("上下文摘要", _episodicItems.length),
        statCard("陪伴状态", "规划中"),
      ].join("");
    }
    if (content) {
      content.innerHTML = [
        '<section class="memory-growth-card">',
        ' <h3>陪伴状态正在设计中</h3>',
        ' <p>这里以后更适合展示好感、心情、阶段任务、最近互动节奏等“会变化”的状态；它不应该和可编辑事实记忆混在一起。</p>',
        ' <div class="memory-growth-grid">',
        '  <div><strong>状态灯</strong><span>ready / busy / offline</span></div>',
        '  <div><strong>近期节奏</strong><span>对话频率和任务状态</span></div>',
        '  <div><strong>成长摘要</strong><span>rolling summary 接入后展示</span></div>',
        ' </div>',
        '</section>',
      ].join("");
    }
  }

  function statCard(label, value) {
    return '<div class="memory-stat"><strong>' + esc(value) + '</strong><span>' + esc(label) + '</span></div>';
  }

  function handleContentClick(e) {
    var target = e.target.closest("[data-act]");
    if (!target) return;
    var act = target.getAttribute("data-act");
    var key = target.getAttribute("data-k");
    var rowid = target.getAttribute("data-rowid");
    if (act === "open-core" || act === "edit-core") _showCoreModal(_coreByKey[String(key)], act === "edit-core");
    else if (act === "pin-core") apiPin(key, function () { loadCore(); toast("记忆置顶状态已更新"); });
    else if (act === "delete-core") confirmCoreDelete(key);
    else if (act === "open-episodic" || act === "edit-episodic") showEpisodicModal(_episodicById[String(rowid)]);
    else if (act === "delete-episodic") confirmEpisodicDelete(_episodicById[String(rowid)]);
    else if (act === "source-episodic") showEpisodicSource(_episodicById[String(rowid)]);
  }

  function modal(opts) {
    var ov = document.createElement("div");
    ov.className = "memory-modal-overlay";
    var inputs = (opts.fields || []).map(function (f, i) {
      if (f.type === "textarea") {
        return '<label class="memory-modal-label">' + esc(f.label) +
          '<textarea class="memory-modal-input memory-modal-textarea" data-i="' + i + '">' + esc(f.value || "") + '</textarea></label>';
      }
      return '<label class="memory-modal-label">' + esc(f.label) +
        '<input class="memory-modal-input" data-i="' + i + '" value="' + esc(f.value || "") + '"></label>';
    }).join("");
    ov.innerHTML = [
      '<div class="memory-modal' + (opts.wide ? ' memory-modal-wide' : '') + '">',
      ' <h4>' + esc(opts.title) + '</h4>',
      opts.message ? ' <p class="memory-modal-message">' + esc(opts.message) + '</p>' : '',
      inputs,
      ' <div class="memory-modal-actions">',
      '  <button class="memory-modal-cancel" type="button">取消</button>',
      '  <button class="memory-modal-ok" type="button">' + esc(opts.confirmText || "确定") + '</button>',
      ' </div>',
      '</div>',
    ].join("");
    document.body.appendChild(ov);
    function closeModal() { ov.remove(); }
    ov.addEventListener("click", function (e) { if (e.target === ov) closeModal(); });
    ov.querySelector(".memory-modal-cancel").addEventListener("click", closeModal);
    ov.querySelector(".memory-modal-ok").addEventListener("click", function () {
      var vals = Array.prototype.map.call(ov.querySelectorAll(".memory-modal-input"), function (el) {
        return el.tagName === "TEXTAREA" ? el.value : el.value.trim();
      });
      closeModal();
      if (opts.onConfirm) opts.onConfirm(vals);
    });
    var first = ov.querySelector(".memory-modal-input");
    if (first) first.focus();
    return ov;
  }

  function showAddModal() {
    modal({
      title: "新增长期记忆",
      confirmText: "保存",
      fields: [
        { label: "记忆名称" },
        { label: "记忆内容", type: "textarea" },
      ],
      onConfirm: function (v) {
        if (!v[0] || !String(v[1] || "").trim()) return;
        apiSave(v[0], v[1], function () {
          loadCore();
          toast("长期记忆已保存");
        });
      },
    });
  }

  function _showCoreModal(memory) {
    if (!memory) return;
    var key = memory.key;
    var ov = modal({
      title: "长期记忆：" + labelKey(key),
      confirmText: "保存",
      wide: true,
      message: "来源：" + sourceLabel(memory.source || "") + (memory.pinned ? " · 已置顶" : ""),
      fields: [{ label: "完整内容", value: memory.value || "", type: "textarea" }],
      onConfirm: function (v) {
        apiUpdate(key, v[0] || "", function () {
          loadCore();
          toast("长期记忆已更新");
        });
      },
    });
    injectCoreActions(ov, memory);
  }

  function injectCoreActions(ov, memory) {
    var actions = ov && ov.querySelector(".memory-modal-actions");
    if (!actions) return;
    var pin = document.createElement("button");
    pin.type = "button";
    pin.className = "memory-modal-pin";
    pin.textContent = memory.pinned ? "取消置顶" : "置顶";
    pin.addEventListener("click", function () {
      ov.remove();
      apiPin(memory.key, function () { loadCore(); });
    });
    var del = document.createElement("button");
    del.type = "button";
    del.className = "memory-modal-delete";
    del.textContent = "删除";
    del.addEventListener("click", function () {
      ov.remove();
      confirmCoreDelete(memory.key);
    });
    actions.insertBefore(pin, actions.firstChild);
    actions.insertBefore(del, actions.firstChild);
  }

  function confirmCoreDelete(key) {
    modal({
      title: "确认删除",
      confirmText: "删除",
      message: "删除「" + labelKey(key) + "」后，AI 不会再自动恢复这条记忆。",
      fields: [],
      onConfirm: function () {
        apiDelete(key, function () {
          loadCore();
          toast("长期记忆已删除");
        });
      },
    });
  }

  function showEpisodicModal(note) {
    if (!note) return;
    var ov = modal({
      title: note.title || "上下文摘要",
      confirmText: "保存",
      wide: true,
      message: sourceRangeLabel(note),
      fields: [{ label: "摘要内容", value: note.content || "", type: "textarea" }],
      onConfirm: function (v) {
        var val = v[0] || "";
        if (!val.trim()) return;
        apiEpisodicUpdate(note.rowid, val, function () {
          loadEpisodic();
          toast("上下文摘要已更新");
        });
      },
    });
    injectEpisodicActions(ov, note);
  }

  function injectEpisodicActions(ov, note) {
    var actions = ov && ov.querySelector(".memory-modal-actions");
    if (!actions) return;
    if (note.source_session_id) {
      var src = document.createElement("button");
      src.type = "button";
      src.className = "memory-modal-source";
      src.textContent = "查看来源上下文";
      src.addEventListener("click", function () { showEpisodicSource(note); });
      actions.insertBefore(src, actions.firstChild);
    }
    var del = document.createElement("button");
    del.type = "button";
    del.className = "memory-modal-delete";
    del.textContent = "删除";
    del.addEventListener("click", function () {
      ov.remove();
      confirmEpisodicDelete(note);
    });
    actions.insertBefore(del, actions.firstChild);
  }

  function confirmEpisodicDelete(note) {
    if (!note) return;
    modal({
      title: "确认删除",
      confirmText: "删除",
      message: "删除这条上下文摘要后无法恢复。",
      fields: [],
      onConfirm: function () {
        apiEpisodicDelete(note.rowid, function () {
          loadEpisodic();
          toast("上下文摘要已删除");
        });
      },
    });
  }

  function showEpisodicSource(note) {
    if (!note || !b("getEpisodicSourceMessages")) return;
    window.configBridge.getEpisodicSourceMessages(String(note.rowid), function (raw) {
      var data;
      try { data = JSON.parse(raw || "{}"); }
      catch (e) { data = { error: "来源上下文解析失败", messages: [] }; }
      var messages = data.messages || [];
      var body = data.error ? data.error : messages.map(function (m) {
        var content = m.content;
        if (content && typeof content === "object") content = content.text || JSON.stringify(content);
        return "[" + m.seq + "] " + fmtTime(m.created_at) + " " + m.role + ": " + String(content || "");
      }).join("\n\n");
      modal({
        title: "来源上下文",
        confirmText: "关闭",
        wide: true,
        message: data.session_id ? "对话：" + data.session_id + " · 消息 " + data.start_seq + "-" + data.end_seq : "",
        fields: [{ label: "带时间戳的来源消息", value: body || "没有找到来源消息", type: "textarea" }],
        onConfirm: function () {},
      });
    });
  }

  function renderLegacy(container) {
    container.innerHTML = [
      '<div class="memory-settings-root memory-moved-notice">',
      ' <header class="memory-settings-header">',
      '  <h2>记忆库已移到顶栏</h2>',
      '  <p>记忆内容不再放在设置里。请从窗口右上角的“记忆”按钮打开。</p>',
      ' </header>',
      ' <button class="memory-hall-btn primary" id="openMemoryHallFromSettings" type="button">打开记忆库</button>',
      '</div>',
    ].join("");
    var btn = $id("openMemoryHallFromSettings");
    if (btn) btn.addEventListener("click", open);
  }

  window.PawMemoryHall = {
    init: init,
    open: open,
    close: close,
    toggle: toggle,
    isOpen: isOpen,
    refresh: refreshCurrent,
  };

  window.PawMemorySettings = { render: renderLegacy };
})();
