/* PawMate — Conversation Sidebar
   Multi-conversation: create / switch / delete / rename sessions.

   Integrate:
     1. index.html: <script defer src="js/sidebar.js"> after app.js
     2. ConversationManager registered as "conversationManager" in QWebChannel
     3. window.PawSidebar.init(callback) called from app.js initWebChannel

   NOTE: All _mgr.xxx() calls use callbacks (QWebChannel @pyqtSlot is async).
*/

window.PawSidebar = (function () {
  "use strict";

  let _mgr = null;
  let _sessions = [];
  let _currentSid = "";
  let _isOpen = false;
  let _renameId = null;
  let _lastNewDraftAt = 0;

  let $overlay, $sidebar, $list, $newBtn, $toggleBtn;

  /* ── Init ──────────────────────────────────────── */
  function init(conversationManager) {
    _mgr = conversationManager;
    _buildDOM();
    _bindEvents();
    _bindManagerSignals();
    _currentSid = "";
    _refresh();
    _showBlankDraft();
    console.log("[Sidebar] initialized");
  }

  function _bindManagerSignals() {
    if (!_mgr || _mgr.__pawSidebarSignalsBound) return;
    _mgr.__pawSidebarSignalsBound = true;
    if (_mgr.sessionChanged && typeof _mgr.sessionChanged.connect === "function") {
      _mgr.sessionChanged.connect(function (sid) {
        _currentSid = sid || "";
        _refresh();
      });
    }
  }

  /* ── Build DOM ─────────────────────────────────── */
  function _buildDOM() {
    var oldToggle = document.getElementById("sidebarToggle");
    if (oldToggle) oldToggle.remove();
    document.querySelectorAll(".sidebar-overlay, .conv-sidebar").forEach(function (node) {
      node.remove();
    });

    $toggleBtn = document.createElement("button");
    $toggleBtn.className = "icon-btn sidebar-toggle";
    $toggleBtn.id = "sidebarToggle";
    $toggleBtn.type = "button";
    $toggleBtn.title = "\u5bf9\u8bdd\u5217\u8868";
    $toggleBtn.setAttribute("aria-label", "\u5bf9\u8bdd\u5217\u8868");
    $toggleBtn.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 4h18v2H3V4zm0 7h18v2H3v-2zm0 7h18v2H3v-2z"/></svg>';

    var titleGroup = document.querySelector(".title-group");
    if (titleGroup) {
      titleGroup.insertBefore($toggleBtn, titleGroup.firstChild);
    }

    $overlay = document.createElement("div");
    $overlay.className = "sidebar-overlay";
    $overlay.style.display = "none";

    $sidebar = document.createElement("aside");
    $sidebar.className = "conv-sidebar";
    $sidebar.innerHTML =
      '<div class="sidebar-head">' +
        '<h2>\u5bf9\u8bdd</h2>' +
        '<button class="sidebar-new-btn" id="sidebarNewBtn" type="button" title="\u65b0\u5efa\u5bf9\u8bdd">' +
          '<svg viewBox="0 0 24 24"><path d="M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6v2z"/></svg>' +
        '</button>' +
      '</div>' +
      '<div class="sidebar-list" id="sidebarList"></div>';

    var shell = document.querySelector(".app-shell");
    if (shell) {
      shell.appendChild($overlay);
      shell.appendChild($sidebar);
    }

    $list = document.getElementById("sidebarList");
    $newBtn = document.getElementById("sidebarNewBtn");
  }

  /* ── Events ────────────────────────────────────── */
  function _bindEvents() {
    // ★ 拦截侧边栏 dom 树内所有 mousedown，不让 Qt 标题栏拖拽劫持事件
    $sidebar.addEventListener("mousedown", function (e) { e.stopPropagation(); });
    $overlay.addEventListener("mousedown", function (e) { e.stopPropagation(); });

    // ☰ 按钮注入在 title-group 里，不在 $sidebar 内，需单独声明
    $toggleBtn.addEventListener("mousedown", function (e) { e.stopPropagation(); });

    $toggleBtn.addEventListener("click", toggle);
    $overlay.addEventListener("click", close);
    $newBtn.addEventListener("pointerdown", _handleNewDraftEvent);
    $newBtn.addEventListener("mousedown", function (e) {
      e.preventDefault();
      e.stopPropagation();
    });
    $newBtn.addEventListener("click", _handleNewDraftEvent);
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && _isOpen) close();
    });
  }

  function _handleNewDraftEvent(e) {
    if (e) {
      e.preventDefault();
      e.stopPropagation();
    }
    var now = Date.now();
    if (now - _lastNewDraftAt < 250) return;
    _lastNewDraftAt = now;
    console.log("[Sidebar] + button activated");
    _startBlankDraft(close);
  }

  /* ── Open / Close ──────────────────────────────── */
  function toggle() { _isOpen ? close() : open(); }

  function open() {
    _refresh();
    $sidebar.classList.add("open");
    $overlay.style.display = "block";
    requestAnimationFrame(function () { $overlay.classList.add("visible"); });
    _isOpen = true;
  }

  function close() {
    $sidebar.classList.remove("open");
    $overlay.classList.remove("visible");
    setTimeout(function () { $overlay.style.display = "none"; }, 250);
    _isOpen = false;
    _renameId = null;
  }

  /* ── Data refresh (async callbacks) ────────────── */
  function _refresh(done) {
    if (!_mgr) return;
    _mgr.get_current_session_id(function (sid) {
      _currentSid = sid || "";
      _mgr.get_sessions(function (raw) {
        try { _sessions = JSON.parse(raw || "[]"); }
        catch (e) { _sessions = []; }
        _render();
        if (typeof done === "function") done();
      });
    });
  }

  /* ── Render ────────────────────────────────────── */
  function _render() {
    if (!$list) return;
    $list.innerHTML = "";

    if (_sessions.length === 0) {
      $list.innerHTML = '<div class="sidebar-empty">\u6ca1\u6709\u5bf9\u8bdd\u8bb0\u5f55</div>';
      return;
    }

    _sessions.forEach(function (sess) {
      var item = document.createElement("div");
      item.className = "sidebar-item" + (sess.id === _currentSid ? " active" : "");
      item.dataset.sid = sess.id;

      var title = _escHtml(sess.title || "\u5bf9\u8bdd");
      var count = sess.message_count || 0;
      var timeStr = _formatTime(sess.updated_at);

      item.innerHTML =
        '<div class="si-main">' +
          '<span class="si-title">' + title + '</span>' +
          '<span class="si-meta">' + count + ' \u6761 \u00b7 ' + timeStr + '</span>' +
        '</div>' +
        '<div class="si-actions">' +
          '<button class="si-btn si-rename" title="\u91cd\u547d\u540d" data-action="rename" data-sid="' + sess.id + '">' +
            '<svg viewBox="0 0 24 24"><path d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04a1 1 0 000-1.42l-2.34-2.34a1 1 0 00-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z"/></svg>' +
          '</button>' +
          '<button class="si-btn si-delete" title="\u5220\u9664" data-action="delete" data-sid="' + sess.id + '">' +
            '<svg viewBox="0 0 24 24"><path d="M6 19c0 1.1.9 2 2 2h8a2 2 0 002-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"/></svg>' +
          '</button>' +
        '</div>';

      function bindFastClick(selector, handler) {
        var el = item.querySelector(selector);
        if (!el) return;
        var last = 0;
        function wrap(e) {
          var now = Date.now();
          if (now - last < 300) return;
          last = now;
          handler(e);
        }
        el.addEventListener("pointerdown", wrap);
        el.addEventListener("click", wrap);
      }

      bindFastClick(".si-main", function () {
        _switchSession(sess.id);
      });
      bindFastClick(".si-rename", function (e) {
        if (e && e.stopPropagation) e.stopPropagation();
        _startRename(sess.id, sess.title);
      });
      bindFastClick(".si-delete", function (e) {
        if (e && e.stopPropagation) e.stopPropagation();
        _deleteSession(sess.id);
      });

      $list.appendChild(item);
    });
  }

  /* ── Actions (async callbacks) ─────────────────── */
  function _createSession(title, done) {
    console.log("[Sidebar] createSession clicked, _mgr=", _mgr);
    if (!_mgr) {
      console.error("[Sidebar] _mgr is null, init() never called?");
      return;
    }
    if (typeof _mgr.create_session !== "function") {
      console.error("[Sidebar] _mgr.create_session is not a function. Available methods:", Object.keys(_mgr));
      return;
    }
    _mgr.create_session(title || "", function(sid) {
      console.log("[Sidebar] create_session returned sid=", sid);
      if (sid) {
        _currentSid = sid;
        _refresh();
        if (typeof done === "function") done(sid);
      } else {
        console.error("[Sidebar] empty sid returned, check Python logs");
        if (typeof done === "function") done("");
      }
    });
  }

  function ensureActiveSession(done) {
    if (_currentSid) {
      if (typeof done === "function") done(_currentSid);
      return;
    }
    _createSession("", done);
  }

  function _startBlankDraft(done) {
    _showBlankDraft();
    if (typeof done === "function") done();
    if (_mgr && typeof _mgr.start_blank_session === "function") {
      try {
        _mgr.start_blank_session(function (ok) {
          if (ok === false) console.warn("[Sidebar] backend refused blank draft");
        });
      } catch (e) {
        console.warn("[Sidebar] start_blank_session failed:", e);
      }
    }
  }

  function _switchSession(sid) {
    if (!_mgr || sid === _currentSid) return;
    _mgr.switch_session(sid, function (ok) {
      if (ok) {
        _currentSid = sid;
        _refresh();
        close();
        _reloadChatUI(sid);
      }
    });
  }

  function _deleteSession(sid) {
    if (!_mgr) return;
    var sess = _sessions.find(function (item) { return item.id === sid; });
    _showDeleteDialog(sess ? sess.title : "\u8fd9\u4e2a\u5bf9\u8bdd", function () {
      _performDeleteSession(sid);
    });
  }

  function _performDeleteSession(sid) {
    _mgr.delete_session(sid, function (ok) {
      if (!ok) return;
      _mgr.get_current_session_id(function (newSid) {
        var wasCurrent = sid === _currentSid;
        _currentSid = newSid || "";
        _refresh();
        if (wasCurrent) {
          if (_currentSid) _reloadChatUI(_currentSid);
          else _showBlankDraft();
        }
      });
    });
  }

  function _showDeleteDialog(title, onDelete) {
    var old = document.querySelector(".sidebar-confirm-overlay");
    if (old) old.remove();

    var overlay = document.createElement("div");
    overlay.className = "sidebar-confirm-overlay";
    overlay.innerHTML =
      '<div class="sidebar-confirm-card" role="dialog" aria-modal="true">' +
        '<h3>\u5220\u9664\u5bf9\u8bdd\uff1f</h3>' +
        '<p>\u201c' + _escHtml(title || "\u8fd9\u4e2a\u5bf9\u8bdd") + '\u201d\u5c06\u4ece\u5386\u53f2\u5217\u8868\u79fb\u9664\u3002</p>' +
        '<div class="sidebar-confirm-actions">' +
          '<button class="sidebar-confirm-cancel" type="button">\u53d6\u6d88</button>' +
          '<button class="sidebar-confirm-delete" type="button">\u5220\u9664</button>' +
        '</div>' +
      '</div>';

    var host = document.querySelector(".app-shell") || document.body;
    host.appendChild(overlay);

    var card = overlay.querySelector(".sidebar-confirm-card");
    var cancelBtn = overlay.querySelector(".sidebar-confirm-cancel");
    var deleteBtn = overlay.querySelector(".sidebar-confirm-delete");

    function closeDialog() {
      document.removeEventListener("keydown", onKeyDown);
      overlay.remove();
    }

    function onKeyDown(e) {
      if (e.key === "Escape") closeDialog();
    }

    overlay.addEventListener("click", closeDialog);
    card.addEventListener("click", function (e) { e.stopPropagation(); });
    cancelBtn.addEventListener("click", closeDialog);
    deleteBtn.addEventListener("click", function () {
      closeDialog();
      if (typeof onDelete === "function") onDelete();
    });
    document.addEventListener("keydown", onKeyDown);
    deleteBtn.focus();
  }

  function _startRename(sid, currentTitle) {
    _renameId = sid;
    var item = $list.querySelector('.sidebar-item[data-sid="' + sid + '"]');
    if (!item) return;

    var titleEl = item.querySelector(".si-title");
    var oldText = titleEl.textContent;
    var input = document.createElement("input");
    input.className = "si-rename-input";
    input.type = "text";
    input.value = currentTitle || oldText;
    input.maxLength = 30;

    titleEl.replaceWith(input);
    input.focus();
    input.select();

    function commit() {
      var val = input.value.trim();
      if (val && val !== oldText && _mgr) {
        _mgr.rename_session(sid, val, function () { _refresh(); });
      } else {
        _refresh();
      }
      _renameId = null;
    }

    input.addEventListener("blur", commit);
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); input.blur(); }
      if (e.key === "Escape") { _renameId = null; _refresh(); }
    });
  }

  /* ── Chat UI integration ───────────────────────── */
  function _clearChatUI() {
    var msgList = document.getElementById("messageList");
    if (window.PawChat && typeof window.PawChat.clearAll === "function") {
      window.PawChat.clearAll();
    } else if (msgList) {
      msgList.innerHTML = "";
    }
    if (window.PawToolCards && typeof window.PawToolCards.clearAll === "function") {
      window.PawToolCards.clearAll();
    }
    if (msgList) msgList.dataset.loadedSession = "";
    if (window._state) {
      window._state.streamingRow = null;
      window._state.streamingText = "";
    }
  }

  function _showBlankDraft() {
    _currentSid = "";
    _clearChatUI();
    if (window.PawChat && typeof window.PawChat.showEmptyState === "function") {
      window.PawChat.showEmptyState();
    }
    _render();
  }

  function _reloadChatUI(sid) {
    _clearChatUI();
    if (!_mgr) return;
    _mgr.get_session_messages(sid, function (raw) {
      try {
        var msgs = JSON.parse(raw || "[]");
        var msgList = document.getElementById("messageList");
        if (!msgList) return;
        msgList.dataset.loadedSession = sid || "";

        msgs.forEach(function (msg) {
          var row = document.createElement("div");
          row.className = "message-row " + (msg.role === "user" ? "user" : "assistant");

          if (msg.role === "assistant") {
            var avatar = document.createElement("div");
            avatar.className = "avatar-wrap ai";
            avatar.innerHTML = '<img class="avatar-peek-img" src="images/avatar_transparent.png" alt="PawMate" />';
            row.appendChild(avatar);
          }

          var bubble = document.createElement("div");
          bubble.className = "bubble " + (msg.role === "user" ? "user" : "assistant");

          if (window.PawChat && window.PawChat.renderMarkdown && msg.role === "assistant") {
            bubble.innerHTML = window.PawChat.renderMarkdown(msg.content || "");
          } else {
            bubble.textContent = msg.content || "";
          }
          if (window.PawChat && typeof window.PawChat.appendMessageTime === "function") {
            window.PawChat.appendMessageTime(bubble, msg.created_at);
          }

          row.appendChild(bubble);
          msgList.appendChild(row);
        });

        requestAnimationFrame(function () {
          msgList.scrollTop = msgList.scrollHeight;
        });
      } catch (e) {
        console.error("[Sidebar] reload chat failed:", e);
      }
    });
  }

  /* ── Helpers ───────────────────────────────────── */
  function _escHtml(s) {
    var d = document.createElement("div");
    d.textContent = s;
    return d.innerHTML;
  }

  function _formatTime(ts) {
    if (!ts) return "";
    var d = new Date(ts * 1000);
    var now = new Date();
    var diff = now - d;
    if (diff < 60000) return "\u521a\u521a";
    if (diff < 3600000) return Math.floor(diff / 60000) + " \u5206\u949f\u524d";
    if (diff < 86400000) return Math.floor(diff / 3600000) + " \u5c0f\u65f6\u524d";
    if (diff < 604800000) return Math.floor(diff / 86400000) + " \u5929\u524d";
    return (d.getMonth() + 1) + "/" + d.getDate();
  }

  /* ── Public API ────────────────────────────────── */
  return {
    init: init,
    open: open,
    close: close,
    toggle: toggle,
    _createSession: _createSession,
    _switchSession: _switchSession,
    _deleteSession: _deleteSession,
    ensureActiveSession: ensureActiveSession,
    startBlankDraft: _startBlankDraft,
    showBlankDraft: _showBlankDraft,
  };

})();
