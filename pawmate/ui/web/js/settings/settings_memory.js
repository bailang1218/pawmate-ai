(function () {
  "use strict";

  var TAB = "profile";
  var PAGE = 1;
  var PAGE_SIZE = 24;
  var QUERY = "";
  var OPEN = false;
  var BOUND = false;
  var LOAD_TIMER = null;
  var SEARCH_TIMER = null;
  var ITEMS = {};
  var STATUS = null;

  function id(name) { return document.getElementById(name); }
  function bridge(name) { return window.configBridge && typeof window.configBridge[name] === "function"; }
  function esc(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function parse(raw, fallback) {
    try { return JSON.parse(raw || "{}"); }
    catch (e) { return fallback || {}; }
  }
  function fmtTime(value) {
    var num = Number(value || 0);
    if (!num) return "未知时间";
    try { return new Date(num * 1000).toLocaleString(); }
    catch (e) { return "未知时间"; }
  }
  function snippet(value, limit) {
    var text = String(value || "").replace(/\s+/g, " ").trim();
    return text.length > limit ? text.slice(0, limit - 1) + "…" : text;
  }
  function toast(message) {
    if (window.PawUI && typeof window.PawUI.toast === "function") window.PawUI.toast(message);
    else console.log(message);
  }
  function setLoading(text) {
    var content = id("memoryHallContent");
    if (content) content.innerHTML = '<div class="memory-hall-empty">' + esc(text || "加载中…") + '</div>';
  }

  function init(ctx) {
    if (BOUND) return;
    BOUND = true;
    if (window.PawPanelManager) window.PawPanelManager.register("memory", window.PawMemoryHall);
    var button = id("memoryHallBtn");
    if (button) button.addEventListener("click", function (event) { event.stopPropagation(); toggle(); });
    var closeButton = id("memoryHallCloseBtn");
    if (closeButton) closeButton.addEventListener("click", close);
    var overlay = id("memoryHallOverlay");
    if (overlay) overlay.addEventListener("click", close);
    var refresh = id("memoryHallRefreshBtn");
    if (refresh) refresh.addEventListener("click", function () { loadStatus(); loadCurrent(); });
    var add = id("memoryHallAddBtn");
    if (add) add.addEventListener("click", showCreateModal);
    var side = document.querySelector(".memory-hall-side");
    if (side) side.addEventListener("click", function (event) {
      var target = event.target.closest("[data-memory-tab]");
      if (target) switchTab(target.getAttribute("data-memory-tab"));
    });
    var search = id("memoryHallSearch");
    if (search) search.addEventListener("input", function () {
      clearTimeout(SEARCH_TIMER);
      SEARCH_TIMER = setTimeout(function () {
        QUERY = search.value.trim();
        PAGE = 1;
        loadCurrent();
      }, 260);
    });
    var content = id("memoryHallContent");
    if (content) content.addEventListener("click", handleAction);
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && OPEN) close();
    });
  }

  function open() {
    if (OPEN) return;
    if (window.PawPanelManager) window.PawPanelManager.requestOpen("memory");
    OPEN = true;
    var overlay = id("memoryHallOverlay");
    var panel = id("memoryHallPanel");
    if (overlay) { overlay.style.display = "block"; requestAnimationFrame(function () { overlay.classList.add("open"); }); }
    if (panel) { panel.style.display = "flex"; requestAnimationFrame(function () { panel.classList.add("open"); }); }
    if (window.PawPanelManager) window.PawPanelManager.setBodyLocked(true);
    else document.body.style.overflow = "hidden";
    switchTab(TAB, true);
    loadStatus();
  }

  function close() {
    if (!OPEN) return;
    OPEN = false;
    clearTimeout(LOAD_TIMER);
    var overlay = id("memoryHallOverlay");
    var panel = id("memoryHallPanel");
    if (overlay) overlay.classList.remove("open");
    if (panel) panel.classList.remove("open");
    if (window.PawPanelManager) {
      window.PawPanelManager.notifyClosed("memory");
      window.PawPanelManager.setBodyLocked(window.PawPanelManager.hasOpenPanel());
    } else document.body.style.overflow = "";
    setTimeout(function () {
      if (!OPEN) {
        if (overlay) overlay.style.display = "none";
        if (panel) panel.style.display = "none";
      }
    }, 160);
  }

  function toggle() { OPEN ? close() : open(); }
  function isOpen() { return OPEN; }

  function switchTab(tab, force) {
    if (!tab) tab = "profile";
    if (!force && TAB === tab) return;
    TAB = tab;
    PAGE = 1;
    ITEMS = {};
    document.querySelectorAll("[data-memory-tab]").forEach(function (button) {
      button.classList.toggle("active", button.getAttribute("data-memory-tab") === TAB);
    });
    var add = id("memoryHallAddBtn");
    if (add) add.style.display = ["profile", "fact", "event", "lesson"].indexOf(TAB) >= 0 ? "" : "none";
    clearTimeout(LOAD_TIMER);
    LOAD_TIMER = setTimeout(loadCurrent, 0);
  }

  function loadStatus() {
    if (!bridge("getMemoryStatus")) return;
    window.configBridge.getMemoryStatus(function (raw) {
      STATUS = parse(raw, {});
      var counts = STATUS.counts || {};
      setCount("memoryProfileCount", counts.profile, "条");
      setCount("memoryFactCount", counts.fact, "条");
      setCount("memoryEventCount", counts.event, "条");
      setCount("memoryLessonCount", counts.lesson, "条");
      setCount("memoryReviewCount", counts.review, "条");
      setCount("memoryTrashCount", counts.trash, "条");
      setCount("memoryArchiveCount", (STATUS.archive || {}).sessions, "个会话");
    });
  }

  function setCount(targetId, value, suffix) {
    var target = id(targetId);
    if (target) target.textContent = Number(value || 0) + " " + suffix;
  }

  function loadCurrent() {
    if (!OPEN) return;
    if (TAB === "archive") loadArchive();
    else loadItems();
  }

  function loadItems() {
    if (!bridge("queryMemoryItems")) { setLoading("新版记忆接口尚未就绪"); return; }
    setLoading("正在从本地记忆库检索…");
    var request = { item_type: TAB, query: QUERY, page: PAGE, page_size: PAGE_SIZE };
    window.configBridge.queryMemoryItems(JSON.stringify(request), function (raw) {
      var data = parse(raw, { ready: false, items: [] });
      if (!data.ready) { setLoading("记忆加载失败：" + (data.error || "未知错误")); return; }
      renderItems(data);
    });
  }

  function renderItems(data) {
    var content = id("memoryHallContent");
    var stats = id("memoryHallStats");
    if (!content || TAB === "archive") return;
    ITEMS = {};
    var items = data.items || [];
    var embedding = (STATUS || {}).embedding || {};
    if (stats) stats.innerHTML = [
      stat(typeLabel(TAB), data.total || 0),
      stat("当前页", (data.page || 1) + " / " + Math.max(1, Math.ceil((data.total || 0) / (data.page_size || PAGE_SIZE)))),
      stat("语义向量", embedding.enabled ? (embedding.model || "已启用") : "未配置 · FTS5"),
    ].join("");
    if (!items.length) {
      content.innerHTML = '<div class="memory-hall-empty">没有匹配的' + esc(typeLabel(TAB)) + '。</div>' + pager(data);
      return;
    }
    var cards = items.map(function (item) {
      ITEMS[String(item.id)] = item;
      var predicate = item.predicate || typeLabel(item.item_type);
      var status = item.status || "active";
      var actions = [];
      if (status === "deleted") actions.push('<button data-act="restore" data-id="' + esc(item.id) + '">恢复</button>');
      else {
        if (status === "pending_review") actions.push('<button data-act="approve" data-id="' + esc(item.id) + '">审核通过</button>');
        actions.push('<button data-act="edit" data-id="' + esc(item.id) + '">编辑</button>');
        actions.push('<button data-act="pin" data-id="' + esc(item.id) + '">' + (item.pinned ? "取消置顶" : "置顶") + '</button>');
        actions.push('<button data-act="delete" data-id="' + esc(item.id) + '">移到回收站</button>');
      }
      return [
        '<article class="memory-card' + (item.pinned ? ' pinned' : '') + '">',
        ' <button class="memory-card-open" data-act="open" data-id="' + esc(item.id) + '">',
        '  <span class="memory-card-top"><strong>' + esc(predicate) + '</strong><span class="memory-badge">' + esc(statusLabel(status)) + '</span></span>',
        '  <span class="memory-card-preview">' + esc(snippet(item.content, 220)) + '</span>',
        '  <span class="memory-card-meta">' + esc(typeLabel(item.item_type)) + ' · 来源 ' + esc(sourceLabel(item.source_kind)) + ' · ' + esc(fmtTime(item.updated_at)) + '</span>',
        ' </button>',
        ' <div class="memory-card-actions">' + actions.join("") + '</div>',
        '</article>',
      ].join("");
    }).join("");
    content.innerHTML = '<div class="memory-card-grid">' + cards + '</div>' + pager(data);
  }

  function loadArchive() {
    var method = QUERY ? "searchConversationArchive" : "queryConversationArchive";
    if (!bridge(method)) { setLoading("聊天档案接口尚未就绪"); return; }
    setLoading("正在检索完整聊天档案…");
    var request = QUERY
      ? { query: QUERY, limit: 50 }
      : { query: "", page: PAGE, page_size: PAGE_SIZE };
    window.configBridge[method](JSON.stringify(request), function (raw) {
      var data = parse(raw, { ready: false, items: [] });
      if (!data.ready) { setLoading("聊天档案加载失败：" + (data.error || "未知错误")); return; }
      renderArchive(data, !!QUERY);
    });
  }

  function renderArchive(data, isSearch) {
    var content = id("memoryHallContent");
    var stats = id("memoryHallStats");
    if (!content || TAB !== "archive") return;
    ITEMS = {};
    var items = data.items || [];
    var archive = (STATUS || {}).archive || {};
    if (stats) stats.innerHTML = [
      stat(isSearch ? "搜索命中" : "历史会话", data.total || items.length),
      stat("原始消息", archive.messages || 0),
      stat("已索引消息", archive.indexed_messages || 0),
    ].join("");
    if (!items.length) {
      content.innerHTML = '<div class="memory-hall-empty">没有找到匹配的聊天档案。</div>';
      return;
    }
    var cards = items.map(function (item) {
      var key = item.hit_id || ("session:" + item.session_id);
      ITEMS[key] = item;
      var start = item.start_seq != null ? item.start_seq : (item.covered_start_seq != null ? item.covered_start_seq : 0);
      var end = item.end_seq != null ? item.end_seq : (item.covered_end_seq != null ? item.covered_end_seq : Math.max(0, Number(item.message_count || 1) - 1));
      var body = item.snippet || item.summary || "这个会话尚未生成摘要，可直接打开原文。";
      return [
        '<article class="memory-episode">',
        ' <button class="memory-episode-open" data-act="history" data-key="' + esc(key) + '" data-session="' + esc(item.session_id) + '" data-start="' + esc(start) + '" data-end="' + esc(end) + '">',
        '  <span class="memory-episode-head"><strong>' + esc(item.title || "历史对话") + '</strong><small>' + esc(fmtTime(item.created_at || item.updated_at)) + '</small></span>',
        '  <span class="memory-episode-preview">' + esc(snippet(body, 300)) + '</span>',
        '  <span class="memory-episode-meta">会话 ' + esc(item.session_id) + ' · 消息 ' + esc(start) + '-' + esc(end) + (item.retrieval ? ' · ' + esc(item.retrieval) : '') + '</span>',
        ' </button>',
        '</article>',
      ].join("");
    }).join("");
    content.innerHTML = '<div class="memory-timeline">' + cards + '</div>' + (isSearch ? "" : pager(data));
  }

  function pager(data) {
    var page = Number(data.page || PAGE);
    var totalPages = Math.max(1, Math.ceil(Number(data.total || 0) / Number(data.page_size || PAGE_SIZE)));
    if (totalPages <= 1) return "";
    return [
      '<div class="memory-card-actions memory-pager">',
      '<button data-act="page" data-page="' + (page - 1) + '"' + (page <= 1 ? ' disabled' : '') + '>上一页</button>',
      '<span>' + page + ' / ' + totalPages + '</span>',
      '<button data-act="page" data-page="' + (page + 1) + '"' + (page >= totalPages ? ' disabled' : '') + '>下一页</button>',
      '</div>',
    ].join("");
  }

  function handleAction(event) {
    var target = event.target.closest("[data-act]");
    if (!target) return;
    var action = target.getAttribute("data-act");
    var itemId = target.getAttribute("data-id");
    if (action === "page") { PAGE = Math.max(1, Number(target.getAttribute("data-page") || 1)); loadCurrent(); return; }
    if (action === "history") {
      openHistory(target.getAttribute("data-session"), Number(target.getAttribute("data-start")), Number(target.getAttribute("data-end")));
      return;
    }
    var item = ITEMS[String(itemId)];
    if (!item) return;
    if (action === "open" || action === "edit") showEditModal(item);
    else if (action === "pin") updateItem(item.id, { pinned: !item.pinned });
    else if (action === "approve") updateItem(item.id, { status: "active" });
    else if (action === "delete") confirmDelete(item);
    else if (action === "restore") restoreItem(item.id);
  }

  function showCreateModal() {
    if (["profile", "fact", "event", "lesson"].indexOf(TAB) < 0) return;
    memoryModal({
      title: "新增" + typeLabel(TAB),
      type: TAB,
      predicate: "",
      content: "",
      importance: 0.6,
      onSave: function (payload) {
        if (!bridge("createMemoryItem")) return;
        window.configBridge.createMemoryItem(JSON.stringify(payload), function (raw) {
          var result = parse(raw, {});
          if (!result.ok) { toast("保存失败：" + (result.error || "未知错误")); return; }
          toast("记忆已保存"); loadStatus(); loadCurrent();
        });
      },
    });
  }

  function showEditModal(item) {
    memoryModal({
      title: "编辑" + typeLabel(item.item_type),
      type: item.item_type,
      predicate: item.predicate || "",
      content: item.content || "",
      importance: item.importance == null ? 0.6 : item.importance,
      onSave: function (payload) { updateItem(item.id, payload); },
    });
  }

  function memoryModal(options) {
    var overlay = document.createElement("div");
    overlay.className = "memory-modal-overlay";
    overlay.innerHTML = [
      '<div class="memory-modal memory-modal-wide">',
      ' <h4>' + esc(options.title) + '</h4>',
      ' <label class="memory-modal-label">类型<select class="memory-modal-input" data-field="type">',
      ["profile", "fact", "event", "lesson"].map(function (value) { return '<option value="' + value + '"' + (value === options.type ? ' selected' : '') + '>' + typeLabel(value) + '</option>'; }).join(""),
      ' </select></label>',
      ' <label class="memory-modal-label">名称 / 谓词<input class="memory-modal-input" data-field="predicate" value="' + esc(options.predicate) + '"></label>',
      ' <label class="memory-modal-label">原子记忆内容<textarea class="memory-modal-input memory-modal-textarea" data-field="content">' + esc(options.content) + '</textarea></label>',
      ' <label class="memory-modal-label">重要度（0-1）<input class="memory-modal-input" data-field="importance" type="number" min="0" max="1" step="0.05" value="' + esc(options.importance) + '"></label>',
      ' <div class="memory-modal-actions"><button class="memory-modal-cancel">取消</button><button class="memory-modal-ok">保存</button></div>',
      '</div>',
    ].join("");
    document.body.appendChild(overlay);
    function closeModal() { overlay.remove(); }
    overlay.addEventListener("click", function (event) { if (event.target === overlay) closeModal(); });
    overlay.querySelector(".memory-modal-cancel").addEventListener("click", closeModal);
    overlay.querySelector(".memory-modal-ok").addEventListener("click", function () {
      var payload = {
        item_type: overlay.querySelector('[data-field="type"]').value,
        predicate: overlay.querySelector('[data-field="predicate"]').value.trim(),
        content: overlay.querySelector('[data-field="content"]').value.trim(),
        importance: Number(overlay.querySelector('[data-field="importance"]').value || 0.6),
      };
      if (!payload.content) return;
      closeModal();
      options.onSave(payload);
    });
  }

  function updateItem(itemId, payload) {
    if (!bridge("updateMemoryItem")) return;
    window.configBridge.updateMemoryItem(String(itemId), JSON.stringify(payload), function (raw) {
      var result = parse(raw, {});
      if (!result.ok) { toast("更新失败：" + (result.error || "未知错误")); return; }
      toast("记忆已更新"); loadStatus(); loadCurrent();
    });
  }

  function confirmDelete(item) {
    confirmModal("移到回收站", "这条记忆不会再参与召回，可在回收站恢复。", function () {
      if (!bridge("deleteMemoryItem")) return;
      window.configBridge.deleteMemoryItem(String(item.id), function (raw) {
        var result = parse(raw, {});
        if (!result.ok) { toast("删除失败：" + (result.error || "未知错误")); return; }
        toast("已移到回收站"); loadStatus(); loadCurrent();
      });
    });
  }

  function restoreItem(itemId) {
    if (!bridge("restoreMemoryItem")) return;
    window.configBridge.restoreMemoryItem(String(itemId), function (raw) {
      var result = parse(raw, {});
      if (!result.ok) { toast("恢复失败：" + (result.error || "未知错误")); return; }
      toast("记忆已恢复"); loadStatus(); loadCurrent();
    });
  }

  function openHistory(sessionId, start, end) {
    if (!bridge("openHistoryContext")) return;
    window.configBridge.openHistoryContext(JSON.stringify({
      session_id: sessionId, start_seq: start, end_seq: end, radius: QUERY ? 3 : 0,
    }), function (raw) {
      var data = parse(raw, { messages: [] });
      var body = (data.messages || []).map(function (message) {
        var value = message.content;
        if (value && typeof value === "object") value = value.text || JSON.stringify(value);
        return "[" + message.seq + "] " + fmtTime(message.created_at) + " " + message.role + ": " + String(value || "");
      }).join("\n\n");
      textModal("聊天来源", "会话 " + sessionId + " · 消息 " + data.start_seq + "-" + data.end_seq, body || data.error || "没有找到来源消息");
    });
  }

  function textModal(title, message, body) {
    var overlay = document.createElement("div");
    overlay.className = "memory-modal-overlay";
    overlay.innerHTML = '<div class="memory-modal memory-modal-wide"><h4>' + esc(title) + '</h4><p class="memory-modal-message">' + esc(message) + '</p><textarea class="memory-modal-input memory-modal-textarea" readonly>' + esc(body) + '</textarea><div class="memory-modal-actions"><button class="memory-modal-ok">关闭</button></div></div>';
    document.body.appendChild(overlay);
    overlay.addEventListener("click", function (event) { if (event.target === overlay) overlay.remove(); });
    overlay.querySelector(".memory-modal-ok").addEventListener("click", function () { overlay.remove(); });
  }

  function confirmModal(title, message, onConfirm) {
    var overlay = document.createElement("div");
    overlay.className = "memory-modal-overlay";
    overlay.innerHTML = '<div class="memory-modal"><h4>' + esc(title) + '</h4><p class="memory-modal-message">' + esc(message) + '</p><div class="memory-modal-actions"><button class="memory-modal-cancel">取消</button><button class="memory-modal-ok">确定</button></div></div>';
    document.body.appendChild(overlay);
    overlay.querySelector(".memory-modal-cancel").addEventListener("click", function () { overlay.remove(); });
    overlay.querySelector(".memory-modal-ok").addEventListener("click", function () { overlay.remove(); onConfirm(); });
  }

  function stat(label, value) { return '<div class="memory-stat"><strong>' + esc(value) + '</strong><span>' + esc(label) + '</span></div>'; }
  function typeLabel(value) {
    return ({ profile: "用户画像", fact: "事实", event: "事件", lesson: "经验 / 决策", review: "待审核", trash: "回收站" })[value] || value || "记忆";
  }
  function statusLabel(value) { return ({ active: "生效", pending_review: "待审核", superseded: "已替代", deleted: "回收站" })[value] || value; }
  function sourceLabel(value) { return ({ manual: "手动", assistant_tool: "用户明确要求", assistant_auto_judgment: "PawMate 主动判断", legacy_manual: "旧版手动", legacy_ai: "旧版 AI" })[value] || value || "未知"; }

  function renderLegacy(container) {
    container.innerHTML = '<div class="memory-settings-root memory-moved-notice"><header class="memory-settings-header"><h2>记忆库已移到左侧工作区</h2><p>请打开“记忆”，管理画像、事实、事件、经验、聊天档案和回收站。</p></header><button class="memory-hall-btn primary" id="openMemoryHallFromSettings" type="button">打开记忆库</button></div>';
    var button = id("openMemoryHallFromSettings");
    if (button) button.addEventListener("click", open);
  }

  window.PawMemoryHall = { init: init, open: open, close: close, toggle: toggle, isOpen: isOpen, refresh: loadCurrent };
  window.PawMemorySettings = { render: renderLegacy };
})();
