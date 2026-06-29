/* =============================================================
   PawMate — Chat Module (messages.js)
   消息渲染、Markdown、流式气泡

   依赖：无（纯 DOM 操作）
   暴露：window.PawChat
   ============================================================= */

window.PawChat = (function () {
  "use strict";

  /* ── Shared refs (set by init) ─────────────────── */
  let _messageList = null;
  let _streamingRow = null;
  let _streamingText = "";
  let _streamingByTurn = {};

  function init(messageListEl) {
    _messageList = messageListEl;
  }

  /* ── Utilities ─────────────────────────────────── */
  function escapeHtml(unsafe) {
    return String(unsafe)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  function scrollToBottom() {
    if (!_messageList) return;
    requestAnimationFrame(() => {
      _messageList.scrollTop = _messageList.scrollHeight;
    });
  }

  function removeEmptyState() {
    const el = document.getElementById("emptyState");
    if (el) el.remove();
  }

  function resetStreamingState() {
    _streamingRow = null;
    _streamingText = "";
    _streamingByTurn = {};
  }

  function formatMessageTime(value) {
    const raw = value == null ? Date.now() : Number(value);
    const ms = raw > 1000000000000 ? raw : raw * 1000;
    const d = new Date(ms);
    if (Number.isNaN(d.getTime())) return "";
    const pad = (n) => String(n).padStart(2, "0");
    return (
      d.getFullYear() + "-" +
      pad(d.getMonth() + 1) + "-" +
      pad(d.getDate()) + " " +
      pad(d.getHours()) + ":" +
      pad(d.getMinutes())
    );
  }

  function appendMessageTime(bubble, createdAt) {
    if (!bubble) return;
    const text = formatMessageTime(createdAt);
    if (!text) return;
    const meta = document.createElement("div");
    meta.className = "message-time";
    meta.textContent = text;
    bubble.appendChild(meta);
  }

  function showEmptyState() {
    if (!_messageList) return;
    _messageList.innerHTML =
      '<div id="emptyState" class="empty-state">' +
        '<img class="empty-avatar" src="images/avatar_transparent.png" alt="PawMate" />' +
        '<h2>今天想聊点什么？</h2>' +
        '<p>直接发送消息会创建一个新对话；打开左上角对话列表可以继续历史上下文。</p>' +
      '</div>';
    resetStreamingState();
  }

  /* ── Markdown (lightweight, no deps) ───────────── */
  function renderMarkdown(text) {
    let html = escapeHtml(text);
    // Code blocks
    html = html.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) => {
      const cls = lang ? ` class="lang-${escapeHtml(lang)}"` : "";
      return `<pre><code${cls}>${escapeHtml(code.trim())}</code></pre>`;
    });
    html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
    // Emphasis
    html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    html = html.replace(/\*([^*]+)\*/g, "<em>$1</em>");
    // Headings
    html = html.replace(/^### (.+)$/gm, "<h4>$1</h4>");
    html = html.replace(/^## (.+)$/gm, "<h3>$1</h3>");
    html = html.replace(/^# (.+)$/gm, "<h2>$1</h2>");
    // Lists
    html = html.replace(/^- (.+)$/gm, "<li>$1</li>");
    html = html.replace(/(<li>.*<\/li>\n?)+/g, "<ul>$&</ul>");
    html = html.replace(/^\d+\. (.+)$/gm, "<li>$1</li>");
    html = html.replace(/(<li>.*<\/li>\n?)+/g, (m) =>
      m.startsWith("<ul>") ? m : "<ol>" + m + "</ol>"
    );
    // Paragraphs
    const blockTags = /^(<h[234]>|<pre>|<ul>|<ol>|<li>)/;
    const lines = html.split("\n");
    let result = [], inP = false;
    for (const line of lines) {
      if (!line.trim()) {
        if (inP) { result.push("</p>"); inP = false; }
        continue;
      }
      if (blockTags.test(line)) {
        if (inP) { result.push("</p>"); inP = false; }
        result.push(line);
      } else {
        if (!inP) { result.push("<p>"); inP = true; }
        else result.push("<br>");
        result.push(line);
      }
    }
    if (inP) result.push("</p>");
    return result.join("\n");
  }

  /* ── Message Row ───────────────────────────────── */
  function createMessageRow(role, htmlContent, createdAt) {
    removeEmptyState();
    const row = document.createElement("div");
    row.className = "message-row " + role + " entering";
    if (role === "assistant") {
      const avatar = document.createElement("div");
      avatar.className = "avatar-wrap ai";
      avatar.innerHTML = '<img class="avatar-peek-img" src="images/avatar_transparent.png" alt="PawMate" />';
      row.appendChild(avatar);
    }
    const bubble = document.createElement("div");
    bubble.className = "bubble " + role;
    bubble.innerHTML = htmlContent;
    appendMessageTime(bubble, createdAt);
    row.appendChild(bubble);
    _messageList.appendChild(row);
    scrollToBottom();
    return row;
  }

  function createPresenceNudge(text, meta) {
    if (!_messageList || !text) return null;
    removeEmptyState();

    const row = document.createElement("div");
    row.className = "message-row assistant presence entering";
    row.dataset.transient = "presence";

    const avatar = document.createElement("div");
    avatar.className = "avatar-wrap ai";
    avatar.innerHTML = '<img class="avatar-peek-img" src="images/avatar_transparent.png" alt="PawMate" />';
    row.appendChild(avatar);

    const bubble = document.createElement("div");
    bubble.className = "bubble assistant presence";

    const label = document.createElement("div");
    label.className = "presence-label";
    label.textContent = (meta && meta.label) || "在线状态";
    bubble.appendChild(label);

    const body = document.createElement("div");
    body.className = "presence-text";
    body.textContent = text;
    bubble.appendChild(body);

    row.appendChild(bubble);
    _messageList.appendChild(row);
    scrollToBottom();
    return row;
  }

  /* ── Streaming ─────────────────────────────────── */
  function startStreamingBubbleForTurn(turnId) {
    const key = String(turnId || 0);
    removeEmptyState();
    if (_streamingByTurn[key] && _streamingByTurn[key].row) {
      finalizeStreamingBubbleForTurn(turnId);
    }
    const row = document.createElement("div");
    row.className = "message-row assistant streaming entering";
    row.dataset.createdAt = String(Date.now());
    row.dataset.turnId = key;
    const avatar = document.createElement("div");
    avatar.className = "avatar-wrap ai";
    avatar.innerHTML = '<img class="avatar-peek-img" src="images/avatar_transparent.png" alt="PawMate" />';
    row.appendChild(avatar);
    const bubble = document.createElement("div");
    bubble.className = "bubble assistant";
    row.appendChild(bubble);
    _messageList.appendChild(row);
    _streamingByTurn[key] = { row: row, text: "" };
    if (!turnId) {
      _streamingRow = row;
      _streamingText = "";
    }
    scrollToBottom();
  }

  function startStreamingBubble() {
    startStreamingBubbleForTurn(0);
  }

  function appendStreamingDeltaForTurn(turnId, text) {
    const key = String(turnId || 0);
    if (!_streamingByTurn[key] || !_streamingByTurn[key].row) {
      startStreamingBubbleForTurn(turnId);
    }
    const item = _streamingByTurn[key];
    item.text += text;
    const bubble = item.row.querySelector(".bubble");
    bubble.innerHTML = renderMarkdown(item.text);
    bubble.innerHTML += '<span class="stream-cursor">|</span>';
    if (!turnId) {
      _streamingRow = item.row;
      _streamingText = item.text;
    }
    scrollToBottom();
  }

  function appendStreamingDelta(text) {
    appendStreamingDeltaForTurn(0, text);
  }

  function finalizeStreamingBubbleForTurn(turnId) {
    const key = String(turnId || 0);
    const item = _streamingByTurn[key];
    if (!item || !item.row) return;
    const bubble = item.row.querySelector(".bubble");
    bubble.innerHTML = renderMarkdown(item.text);
    appendMessageTime(bubble, Number(item.row.dataset.createdAt || Date.now()));
    item.row.classList.remove("streaming");
    delete _streamingByTurn[key];
    if (!turnId || _streamingRow === item.row) {
      _streamingRow = null;
      _streamingText = "";
    }
    scrollToBottom();
  }

  function finalizeStreamingBubble() {
    finalizeStreamingBubbleForTurn(0);
  }

  function startStreamingBubbleLegacy() {
    removeEmptyState();
    if (_streamingRow) finalizeStreamingBubble();
    const row = document.createElement("div");
    row.className = "message-row assistant streaming entering";
    row.dataset.createdAt = String(Date.now());
    const avatar = document.createElement("div");
    avatar.className = "avatar-wrap ai";
    avatar.innerHTML = '<img class="avatar-peek-img" src="images/avatar_transparent.png" alt="PawMate" />';
    row.appendChild(avatar);
    const bubble = document.createElement("div");
    bubble.className = "bubble assistant";
    row.appendChild(bubble);
    _messageList.appendChild(row);
    _streamingRow = row;
    _streamingText = "";
    scrollToBottom();
  }

  function appendStreamingDeltaLegacy(text) {
    if (!_streamingRow) startStreamingBubble();
    _streamingText += text;
    const bubble = _streamingRow.querySelector(".bubble");
    bubble.innerHTML = renderMarkdown(_streamingText);
    bubble.innerHTML += '<span class="stream-cursor">|</span>';
    scrollToBottom();
  }

  function finalizeStreamingBubbleLegacy() {
    if (!_streamingRow) return;
    const bubble = _streamingRow.querySelector(".bubble");
    bubble.innerHTML = renderMarkdown(_streamingText);
    appendMessageTime(bubble, Number(_streamingRow.dataset.createdAt || Date.now()));
    _streamingRow.classList.remove("streaming");
    _streamingRow = null;
    _streamingText = "";
    scrollToBottom();
  }

  function clearAll() {
    if (_messageList) _messageList.innerHTML = "";
    resetStreamingState();
  }

  /* ── Public API ────────────────────────────────── */
  return {
    init,
    escapeHtml,
    renderMarkdown,
    formatMessageTime,
    appendMessageTime,
    scrollToBottom,
    createMessageRow,
    createPresenceNudge,
    startStreamingBubble,
    startStreamingBubbleForTurn,
    appendStreamingDelta,
    appendStreamingDeltaForTurn,
    finalizeStreamingBubble,
    finalizeStreamingBubbleForTurn,
    clearAll,
    showEmptyState,
  };
})();
