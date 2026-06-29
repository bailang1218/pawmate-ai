/**
 * PawMate Local Skills Import Tab — import from local folder or ZIP file.
 * Uses async bridge calls via callback_id + clawHubAsyncResponse signal.
 * No window.confirm, no alert, no prompt.
 */
(function () { "use strict";
  var _container = null;
  var _initialized = false;
  var _viewGeneration = 0;

  // Import state
  var _folderImporting = false;
  var _zipImporting = false;
  var _folderResult = null;  // {slug, name, status, local_path} or {error...}
  var _zipResult = null;

  var _cbId = 0;
  var _pending = {};

  function esc(v) { return String(v||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;").replace(/'/g,"&#39;"); }
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

  function isActiveTab() {
    var tab = document.querySelector('[data-hall-tab="import"]');
    var panel = document.getElementById("skillsHallPanel");
    return !!(
      _container &&
      tab &&
      tab.classList.contains("active") &&
      panel &&
      panel.style.display !== "none"
    );
  }

  function nextGeneration() {
    _viewGeneration += 1;
    return _viewGeneration;
  }

  function isCurrentGeneration(generation) {
    return generation === _viewGeneration && isActiveTab();
  }

  function deactivate() {
    _viewGeneration += 1;
    _container = null;
    _folderImporting = false;
    _zipImporting = false;
  }

  function bridge() { return (window._state && window._state.bridge) || null; }

  function callAsync(method) {
    var args = Array.prototype.slice.call(arguments, 1);
    return new Promise(function(resolve) {
      var cid = "im_" + (++_cbId) + "_" + Date.now();
      _pending[cid] = resolve;
      var br = bridge();
      if (!br || typeof br[method] !== "function") {
        delete _pending[cid];
        resolve({ok:false, error:{message:"Bridge method not available: " + method}});
        return;
      }
      try {
        br[method].apply(br, [cid].concat(args));
      } catch(e) {
        delete _pending[cid];
        resolve({ok:false, error:{message: String(e)}});
      }
    });
  }

  // Connect to async result signal
  function setupSignal() {
    try {
      var br = bridge();
      if (br && br.clawHubAsyncResponse && br.clawHubAsyncResponse.connect) {
        br.clawHubAsyncResponse.connect(function(jsonStr) {
          try {
            var data = JSON.parse(jsonStr);
            var cb = _pending[data.callback_id];
            if (cb) {
              delete _pending[data.callback_id];
              delete data.callback_id;
              cb(data);
            }
          } catch(e) { console.error("[Import] signal error:", e); }
        });
        return true;
      }
    } catch(e) { /* bridge not ready */ }
    return false;
  }
  if (!setupSignal()) {
    var _sr = 0, _st = setInterval(function() { _sr++; if (setupSignal() || _sr > 20) clearInterval(_st); }, 200);
  }

  // ---- Sync bridge call (for file dialogs) ----
  function callSync(method) {
    var br = bridge();
    if (!br || typeof br[method] !== "function") return Promise.resolve({ok:false, error:{message:"Bridge not available"}});
    // QWebChannel returns a Promise when called without a callback
    try {
      var p = br[method]();
      if (p && typeof p.then === "function") return p.then(function(r) { try { return JSON.parse(r); } catch(e) { return r||{}; } });
      return Promise.resolve({ok:false, error:{message:"Sync call failed"}});
    } catch(e) {
      return Promise.resolve({ok:false, error:{message:String(e)}});
    }
  }

  // ------------------------------------------------------------------
  // Import flows
  // ------------------------------------------------------------------

  function importFromFolder() {
    if (_folderImporting) return;
    var generation = _viewGeneration;
    callSync("chooseSkillFolder").then(function(r) {
      if (!isCurrentGeneration(generation)) return;
      if (!r.ok || r.cancelled) return;
      var path = r.path;
      if (!path) return;
      _folderImporting = true;
      _folderResult = null;
      render();
      callAsync("importLocalSkillFolder", path).then(function(res) {
        if (!isCurrentGeneration(generation)) return;
        _folderImporting = false;
        _folderResult = res;
        render();
        var hall = window.PawMateSkillsHall;
        if (hall && typeof hall.refresh === 'function') try { hall.refresh(); } catch(e) {}
      });
    });
  }

  function importFromZip() {
    if (_zipImporting) return;
    var generation = _viewGeneration;
    callSync("chooseSkillZip").then(function(r) {
      if (!isCurrentGeneration(generation)) return;
      if (!r.ok || r.cancelled) return;
      var path = r.path;
      if (!path) return;
      _zipImporting = true;
      _zipResult = null;
      render();
      callAsync("importLocalSkillZip", path).then(function(res) {
        if (!isCurrentGeneration(generation)) return;
        _zipImporting = false;
        _zipResult = res;
        render();
        var hall = window.PawMateSkillsHall;
        if (hall && typeof hall.refresh === 'function') try { hall.refresh(); } catch(e) {}
      });
    });
  }

  function renderResult(ok, data) {
    if (!data) return '<div class="hall-empty"><p>' + esc(__("skillsNoResult")) + '</p></div>';
    var h = '';
    if (ok) {
      var imported = data.imported || data.data || {};
      var entry = imported.entry || imported;
      h += '<div class="hall-import-result hall-import-ok">';
      h += '<div style="font-weight:600;color:#276749;margin-bottom:6px;">\u2713 ' + esc(__("skillsImportedSuccessfully")) + '</div>';
      h += '<table style="font-size:12px;border-collapse:collapse;">';
      h += '<tr><td style="padding:2px 10px 2px 0;color:#718096;">' + esc(__("skillsFieldSlug")) + '</td><td style="padding:2px 0;">' + esc(entry.slug || "?") + '</td></tr>';
      h += '<tr><td style="padding:2px 10px 2px 0;color:#718096;">' + esc(__("skillsFieldVersion")) + '</td><td style="padding:2px 0;">' + esc(entry.version || "?") + '</td></tr>';
      h += '<tr><td style="padding:2px 10px 2px 0;color:#718096;">' + esc(__("skillsFieldPath")) + '</td><td style="padding:2px 0;"><code>data/skills/' + esc(entry.slug || "?") + '</code></td></tr>';
      h += '<tr><td style="padding:2px 10px 2px 0;color:#718096;">' + esc(__("skillsFieldStatus")) + '</td><td style="padding:2px 0;"><span class="hall-badge status-' + esc(entry.status||'needs_review') + '">' + esc(statusLabel(entry.status||'needs_review')) + '</span></td></tr>';
      h += '<tr><td style="padding:2px 10px 2px 0;color:#718096;">' + esc(__("skillsFieldSource")) + '</td><td style="padding:2px 0;">' + esc(data.source || entry.source || "?") + '</td></tr>';
      h += '</table>';
      h += '</div>';
    } else {
      var err = data.error || {};
      h += '<div class="hall-import-result hall-import-err">';
      h += '<div style="font-weight:600;color:#c53030;margin-bottom:4px;">\u2717 ' + esc(__("skillsImportFailed")) + '</div>';
      h += '<div style="font-size:12px;color:#742a2a;">' + esc(err.message || __("skillsErrorUnknown")) + '</div>';
      if (err.code) h += '<div style="font-size:11px;color:#718096;margin-top:4px;">' + esc(__("skillsCode")) + ': <code>' + esc(err.code) + '</code></div>';
      h += '<div style="margin-top:8px;"><button class="hall-btn hall-btn-prime" data-hall-import-retry="1">' + esc(__("skillsRetry")) + '</button></div>';
      h += '</div>';
    }
    return h;
  }

  // ------------------------------------------------------------------
  // Render
  // ------------------------------------------------------------------

  function render() {
    if (!_container || !isActiveTab()) return;

    var h = '';
    // Header
    h += '<div class="hall-import-header">';
    h += '<h3>' + esc(__("skillsImportTitle")) + '</h3>';
    h += '<p style="font-size:12px;color:#718096;margin:2px 0;">' + esc(__("skillsImportDescription")) + '</p>';
    h += '</div>';

    // --- Folder import card ---
    h += '<div class="hall-import-card">';
    h += '<div class="hall-import-card-icon">\uD83D\uDCC1</div>';
    h += '<div class="hall-import-card-info">';
    h += '<h4>' + esc(__("skillsImportFromFolder")) + '</h4>';
    h += '<p style="font-size:12px;color:#718096;margin:4px 0 0;">' + esc(__("skillsImportFolderDescription")) + '</p>';
    h += '</div>';
    h += '<div class="hall-import-card-action">';
    if (_folderImporting) {
      h += '<div class="hall-spinner" style="width:18px;height:18px;margin:0;"></div>';
    } else {
      h += '<button class="hall-btn hall-btn-prime" data-hall-import="folder">' + esc(__("skillsSelectFolderImport")) + '</button>';
    }
    h += '</div></div>';
    if (_folderResult) h += renderResult(_folderResult.ok, _folderResult);

    // --- ZIP import card ---
    h += '<div class="hall-import-card">';
    h += '<div class="hall-import-card-icon">\uD83D\uDCE6</div>';
    h += '<div class="hall-import-card-info">';
    h += '<h4>' + esc(__("skillsImportFromZip")) + '</h4>';
    h += '<p style="font-size:12px;color:#718096;margin:4px 0 0;">' + esc(__("skillsImportZipDescription")) + '</p>';
    h += '</div>';
    h += '<div class="hall-import-card-action">';
    if (_zipImporting) {
      h += '<div class="hall-spinner" style="width:18px;height:18px;margin:0;"></div>';
    } else {
      h += '<button class="hall-btn hall-btn-prime" data-hall-import="zip">' + esc(__("skillsSelectZipImport")) + '</button>';
    }
    h += '</div></div>';
    if (_zipResult) h += renderResult(_zipResult.ok, _zipResult);

    // --- URL import card (disabled) ---
    h += '<div class="hall-import-card hall-import-card-disabled">';
    h += '<div class="hall-import-card-icon" style="opacity:0.4;">\uD83D\uDD17</div>';
    h += '<div class="hall-import-card-info">';
    h += '<h4 style="color:#a0aec0;">' + esc(__("skillsImportFromUrl")) + '</h4>';
    h += '<p style="font-size:12px;color:#cbd5e0;margin:4px 0 0;">' + esc(__("skillsImportUrlDescription")) + '</p>';
    h += '</div>';
    h += '<div class="hall-import-card-action">';
    h += '<button class="hall-btn" disabled style="opacity:0.5;">' + esc(__("skillsUnavailable")) + '</button>';
    h += '</div></div>';

    _container.innerHTML = h;
    bindEvents();
  }

  function bindEvents() {
    var el = document.getElementById("skillsHallContent");
    if (!el) return;
    if (el.__pawmateSkillsImportBound) return;
    el.__pawmateSkillsImportBound = true;

    // Click delegation for import buttons and retry
    el.addEventListener("click", function(e) {
      var btn = e.target.closest("button");
      if (!btn) return;

      // Retry on failed import
      if (btn.hasAttribute("data-hall-import-retry")) {
        var parentCard = btn.closest(".hall-import-card");
        if (parentCard) {
          var isZip = parentCard.querySelector("[data-hall-import=zip]");
          if (isZip) { _zipResult = null; importFromZip(); }
          else { _folderResult = null; importFromFolder(); }
        } else {
          // Can't tell which, clear both
          _folderResult = null;
          _zipResult = null;
          render();
        }
        return;
      }

      if (btn.getAttribute("data-hall-import") === "folder") importFromFolder();
      else if (btn.getAttribute("data-hall-import") === "zip") importFromZip();
    });
  }

  // ------------------------------------------------------------------
  // Init
  // ------------------------------------------------------------------

  function init(options) {
    _container = (options && options.container) || null;
    if (!_container) return;
    _initialized = true;
    nextGeneration();
    render();
  }

  window.PawMateSkillsImport = {
    init: init,
    deactivate: deactivate,
    importFromFolder: importFromFolder,
    importFromZip: importFromZip,
  };
})();
