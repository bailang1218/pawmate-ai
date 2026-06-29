/**
 * PawMate ClawHub Skills Hall Tab — browse, search, detail, install.
 * Features transparent error display, connection diagnostics, custom
 * install confirmation modal, and install progress/stage display.
 *
 * Async bridge pattern: callback_id + clawHubAsyncResponse signal.
 * No window.confirm, no alert, no prompt.
 */
(function () { "use strict";
  var B = window.PawMateBridge || null;
  var D = window.PawMateDom || null;

  var _container = null;
  var _mode = "browse";
  var _query = "";
  var _items = [];
  var _loading = false;
  var _error = null;          // { message, code, details }
  var _hasMore = false;
  var _nextCursor = null;

  var _detailLoading = false;
  var _detailError = null;
  var _detailData = null;
  var _detailMd = "";
  var _detailSlug = null;
  var _initialized = false;
  var _viewGeneration = 0;

  // Install state
  var _installModalOpen = false;
  var _installing = false;
  var _installResult = null;
  var _installSteps = [];       // steps from backend
  var _installCurrentStep = ""; // last completed/running step name

  // Diagnostics state
  var _diagLoading = false;
  var _diagResult = null;

  // ---- Async callback infrastructure ----
  var _cbId = 0;
  var _pending = {};

  function esc(v) { return D && D.escapeHtml ? D.escapeHtml(v) : String(v||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;").replace(/'/g,"&#39;"); }
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

  function isActiveTab() {
    var tab = document.querySelector('[data-hall-tab="clawhub"]');
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
    _loading = false;
    _detailLoading = false;
    _diagLoading = false;
  }

  /** Direct bridge call without PawMateBridge wrapping. */
  function callAsync(method) {
    var args = Array.prototype.slice.call(arguments, 1);
    return new Promise(function(resolve) {
      var cid = "ch_" + (++_cbId) + "_" + Date.now();
      _pending[cid] = resolve;
      var br = (window._state && window._state.bridge) || null;
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

  // ---- Connect to bridge async result signal ----
  function setupSignalHandler() {
    try {
      var br = (window._state && window._state.bridge) || null;
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
          } catch(e) {
            console.error("[ClawHub] signal parse error:", e);
          }
        });
        return true;
      }
    } catch(e) {
      console.warn("[ClawHub] signal setup:", e);
    }
    return false;
  }
  if (!setupSignalHandler()) {
    var _sigRetries = 0;
    var _sigTimer = setInterval(function() {
      _sigRetries++;
      if (setupSignalHandler() || _sigRetries > 20) {
        clearInterval(_sigTimer);
      }
    }, 200);
  }

  function makeErrorObj(r) {
    if (!r || r.ok) return null;
    var e = r.error || {};
    return {
      message: e.message || __("skillsErrorUnknown"),
      code: e.code || "",
      details: e.details || null
    };
  }

  // ------------------------------------------------------------------
  // Browse / Search
  // ------------------------------------------------------------------

  function browseFirstPage() {
    var generation = nextGeneration();
    _mode = "browse"; _query = ""; _items = []; _nextCursor = null; _hasMore = false;
    _detailSlug = null; _detailData = null; _detailMd = ""; _detailError = null; _detailLoading = false;
    _loading = true; _error = null; render();
    callAsync("listClawHubSkills").then(function(r) {
      if (!isCurrentGeneration(generation)) return;
      _loading = false;
      if (r.ok) { var d = r.data||r; _items = d.items||[]; _nextCursor = d.nextCursor||d.next_cursor||null; _hasMore = !!_nextCursor; }
      else { _error = makeErrorObj(r); }
      render();
    });
  }

  function searchFirstPage(q) {
    var generation = nextGeneration();
    _mode = "search"; _query = q; _items = []; _nextCursor = null; _hasMore = false;
    _detailSlug = null; _detailData = null; _detailMd = ""; _detailError = null; _detailLoading = false;
    _loading = true; _error = null; render();
    callAsync("searchClawHubSkills", q).then(function(r) {
      if (!isCurrentGeneration(generation)) return;
      _loading = false;
      if (r.ok) { var d = r.data||r; _items = d.items||[]; _nextCursor = d.nextCursor||d.next_cursor||null; _hasMore = !!_nextCursor; }
      else { _error = makeErrorObj(r); }
      render();
    });
  }

  function loadMore() {
    if (_loading || !_hasMore) return;
    var generation = _viewGeneration;
    _loading = true; render();
    var method = _mode === "search" ? "searchClawHubSkills" : "listClawHubSkills";
    var payload = _mode === "search" ? _query : undefined;
    callAsync(method, payload).then(function(r) {
      if (!isCurrentGeneration(generation)) return;
      _loading = false;
      if (r.ok) { var d=r.data||r; _items=_items.concat(d.items||[]); _nextCursor=d.nextCursor||d.next_cursor||null; _hasMore=!!_nextCursor; }
      else { _error = makeErrorObj(r); }
      render();
    });
  }

  // ------------------------------------------------------------------
  // Diagnostics
  // ------------------------------------------------------------------

  function runDiagnostics() {
    if (_diagLoading) return;
    var generation = _viewGeneration;
    _diagLoading = true;
    _diagResult = null;
    render();
    callAsync("diagnoseClawHubConnection").then(function(r) {
      if (!isCurrentGeneration(generation)) return;
      _diagLoading = false;
      _diagResult = r;
      render();
    });
  }

  // ------------------------------------------------------------------
  // Install Modal
  // ------------------------------------------------------------------

  /** Show a PawMate-styled installation confirmation modal.
   *  Returns a Promise that resolves with true (confirmed) or false (cancelled). */
  function showInstallConfirm(skillData) {
    return new Promise(function(resolve) {
      var slug = skillData.slug || skillData.slug || "";
      var displayName = skillData.displayName || skillData.name || slug;
      var version = skillData.version || "latest";
      var localPath = "data/skills/" + slug;

      // Create backdrop + card
      var backdrop = document.createElement("div");
      backdrop.className = "clawhub-modal-backdrop";
      backdrop.style.cssText = "position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.4);z-index:10000;display:flex;align-items:center;justify-content:center;";

      var card = document.createElement("div");
      card.className = "clawhub-modal-card";
      card.style.cssText = "background:#fff;border-radius:12px;width:440px;max-width:90vw;max-height:80vh;overflow-y:auto;box-shadow:0 8px 32px rgba(0,0,0,0.18);";

      card.innerHTML =
        '<div class="clawhub-modal-header" style="padding:18px 20px 12px;border-bottom:1px solid #e2e8f0;display:flex;align-items:center;justify-content:space-between;">' +
          '<h3 style="margin:0;font-size:16px;color:#1f2d35;">' + esc(__("skillsInstallClawHubTitle")) + '</h3>' +
          '<button class="clawhub-modal-close-btn" style="background:none;border:none;font-size:20px;cursor:pointer;color:#718096;line-height:1;">\u00D7</button>' +
        '</div>' +
        '<div class="clawhub-modal-body" style="padding:16px 20px;">' +
          '<table class="clawhub-install-info" style="width:100%;font-size:13px;border-collapse:collapse;">' +
            '<tr><td style="padding:4px 8px 4px 0;color:#718096;white-space:nowrap;">' + esc(__("skillsFieldSkill")) + '</td><td style="padding:4px 0;color:#1f2d35;">' + esc(displayName) + '</td></tr>' +
            '<tr><td style="padding:4px 8px 4px 0;color:#718096;">' + esc(__("skillsFieldSlug")) + '</td><td style="padding:4px 0;color:#1f2d35;">' + esc(slug) + '</td></tr>' +
            '<tr><td style="padding:4px 8px 4px 0;color:#718096;">' + esc(__("skillsFieldVersion")) + '</td><td style="padding:4px 0;color:#1f2d35;">' + esc(version) + '</td></tr>' +
            '<tr><td style="padding:4px 8px 4px 0;color:#718096;">' + esc(__("skillsFieldSource")) + '</td><td style="padding:4px 0;color:#1f2d35;">ClawHub</td></tr>' +
            '<tr><td style="padding:4px 8px 4px 0;color:#718096;">' + esc(__("skillsFieldInstallTo")) + '</td><td style="padding:4px 0;color:#1f2d35;"><code style="background:#f7fafc;padding:1px 5px;border-radius:3px;font-size:12px;">' + esc(localPath) + '</code></td></tr>' +
          '</table>' +
          '<div class="clawhub-install-safety" style="margin-top:12px;padding:10px 12px;background:#fffff0;border:1px solid #fefcbf;border-radius:8px;font-size:12px;color:#744210;">' +
            '<strong style="display:block;margin-bottom:4px;">' + esc(__("skillsSafetyNotice")) + '</strong>' +
            '<ul style="margin:0;padding-left:16px;">' +
              '<li>' + esc(__("skillsSafetyDownload")) + '</li>' +
              '<li>' + esc(__("skillsSafetyNoAutoRun")) + '</li>' +
              '<li>' + esc(__("skillsSafetyNoAutoEnable")) + '</li>' +
              '<li>' + esc(__("skillsSafetyNoDeps")) + '</li>' +
              '<li>' + esc(__("skillsSafetyReview")) + '</li>' +
              '<li>' + esc(__("skillsSafetyThirdParty")) + '</li>' +
            '</ul>' +
          '</div>' +
        '</div>' +
        '<div class="clawhub-modal-actions" style="padding:12px 20px 18px;border-top:1px solid #e2e8f0;display:flex;gap:8px;justify-content:flex-end;">' +
          '<button class="clawhub-btn-cancel" style="padding:7px 16px;border:1px solid #e2e8f0;border-radius:8px;background:#fff;color:#4a5568;font-size:13px;cursor:pointer;">' + esc(__("skillsCancel")) + '</button>' +
          '<button class="clawhub-btn-install" style="padding:7px 16px;border:1px solid #3182ce;border-radius:8px;background:#3182ce;color:#fff;font-size:13px;cursor:pointer;">' + esc(__("skillsInstallToLocal")) + '</button>' +
        '</div>';

      backdrop.appendChild(card);
      document.body.appendChild(backdrop);

      // Cleanup
      function closeModal(confirmed) {
        if (backdrop.parentNode) backdrop.parentNode.removeChild(backdrop);
        resolve(confirmed);
      }

      // Events
      backdrop.addEventListener("click", function(e) {
        if (e.target === backdrop) closeModal(false);
      });
      card.querySelector(".clawhub-modal-close-btn").addEventListener("click", function() { closeModal(false); });
      card.querySelector(".clawhub-btn-cancel").addEventListener("click", function() { closeModal(false); });
      card.querySelector(".clawhub-btn-install").addEventListener("click", function() {
        // Replace modal content with install progress
        showInstallProgress(card, skillData, closeModal);
      });
      // Esc key
      function keyHandler(e) { if (e.key === "Escape") { closeModal(false); document.removeEventListener("keydown", keyHandler); } }
      document.addEventListener("keydown", keyHandler);
    });
  }

  /** Show install progress inside the modal card. */
  function showInstallProgress(card, skillData, closeModalFn) {
    var slug = skillData.slug || "";
    var displayName = skillData.displayName || skillData.name || slug;

    _installing = true;
    _installCurrentStep = "";
    _installSteps = [];

    var steps = [
      "fetch_detail", "download_zip", "validate_zip", "extract",
      "validate_skill_md", "parse_manifest", "write_metadata", "register"
    ];
    var stepLabels = {
      fetch_detail: __("skillsStepFetchDetail"),
      download_zip: __("skillsStepDownloadZip"),
      validate_zip: __("skillsStepValidateZip"),
      extract: __("skillsStepExtract"),
      validate_skill_md: __("skillsStepValidateSkillMd"),
      parse_manifest: __("skillsStepParseManifest"),
      write_metadata: __("skillsStepWriteMetadata"),
      register: __("skillsStepRegister")
    };

    // Build progress HTML with step list
    var stepHtml = '';
    for (var i=0; i<steps.length; i++) {
      var st = steps[i];
      stepHtml += '<div class="ci-step" data-step="' + st + '" style="display:flex;align-items:center;gap:8px;padding:4px 0;font-size:12px;color:#a0aec0;">' +
        '<span class="ci-step-icon" style="width:16px;text-align:center;">\u25CB</span>' +
        '<span class="ci-step-label">' + esc(stepLabels[st] || st) + '</span>' +
        '</div>';
    }

    card.innerHTML =
      '<div class="clawhub-modal-header" style="padding:18px 20px 12px;border-bottom:1px solid #e2e8f0;">' +
        '<h3 style="margin:0;font-size:15px;color:#1f2d35;">' + esc(fmt("skillsInstalling", {name: displayName})) + '</h3>' +
      '</div>' +
      '<div class="clawhub-modal-body" style="padding:16px 20px;">' +
        '<div class="ci-progress-bar-track" style="height:6px;background:#e2e8f0;border-radius:3px;overflow:hidden;margin-bottom:16px;">' +
          '<div class="ci-progress-bar-fill" style="height:100%;width:0%;background:linear-gradient(90deg,#3182ce,#63b3ed);border-radius:3px;transition:width 0.5s;"></div>' +
        '</div>' +
        '<div class="ci-step-list">' + stepHtml + '</div>' +
        '<div class="ci-log" style="margin-top:12px;padding:8px;background:#f7fafc;border-radius:6px;font-size:11px;color:#718096;max-height:100px;overflow-y:auto;font-family:monospace;">' +
          '<div class="ci-log-item">\u2192 ' + esc(__("skillsStartingInstall")) + '</div>' +
        '</div>' +
      '</div>' +
      '<div class="clawhub-modal-actions ci-actions" style="padding:12px 20px 18px;border-top:1px solid #e2e8f0;display:flex;gap:8px;justify-content:flex-end;">' +
        '<button class="ci-close-btn" style="display:none;padding:7px 16px;border:1px solid #e2e8f0;border-radius:8px;background:#fff;color:#4a5568;font-size:13px;cursor:pointer;">' + esc(__("skillsClose")) + '</button>' +
        '<button class="ci-retry-btn" style="display:none;padding:7px 16px;border:1px solid #3182ce;border-radius:8px;background:#3182ce;color:#fff;font-size:13px;cursor:pointer;">' + esc(__("skillsRetry")) + '</button>' +
      '</div>';

    // Start the actual install
    _installResult = null;
    _installSteps = [];

    callAsync("installClawHubSkill", slug).then(function(r) {
      _installing = false;
      _installResult = r;

      var ok = r && r.ok;
      var installedData = ok ? (r.installed || r.data || {}) : null;
      var entry = installedData ? (installedData.entry || installedData) : null;
      var stepsData = installedData ? (installedData.steps || []) : [];
      var err = ok ? null : makeErrorObj(r);

      // If error has steps in details
      if (!ok && r.error && r.error.details && r.error.details.steps) {
        stepsData = r.error.details.steps;
      }

      _installSteps = stepsData;
      renderInstallComplete(card, steps, stepLabels, slug, entry, err, closeModalFn);
    });

    // Simulate progress animation: step through stages with indeterminate timing
    var stepIdx = 0;
    var tick = function() {
      if (stepIdx < steps.length && _installing) {
        updateStep(steps[stepIdx], "running", stepLabels[steps[stepIdx]] || steps[stepIdx]);
        // Update progress bar (approximately)
        var pct = Math.min(90, Math.round((stepIdx / steps.length) * 100));
        var fill = card.querySelector(".ci-progress-bar-fill");
        if (fill) fill.style.width = pct + "%";
        stepIdx++;
        setTimeout(tick, 600 + Math.random() * 400);
      }
    };
    tick();
  }

  /** Update a step's display state in the progress modal. */
  function updateStep(stepName, status, message) {
    var card = document.querySelector(".clawhub-modal-card");
    if (!card) return;
    var item = card.querySelector("[data-step='" + stepName + "']");
    if (!item) return;
    var icon = item.querySelector(".ci-step-icon");
    var label = item.querySelector(".ci-step-label");
    if (status === "running") {
      item.style.color = "#3182ce";
      if (icon) icon.innerHTML = "\u25E6";
    } else if (status === "done") {
      item.style.color = "#38a169";
      if (icon) icon.innerHTML = "\u2713";
    } else if (status === "failed") {
      item.style.color = "#e53e3e";
      if (icon) icon.innerHTML = "\u2717";
    }
    if (label && message) label.textContent = message;

    // Add to log
    var log = card.querySelector(".ci-log");
    if (log) {
      var logItem = document.createElement("div");
      logItem.className = "ci-log-item";
      logItem.style.cssText = "padding:1px 0;";
      logItem.textContent = (status === "done" ? "\u2713 " : status === "failed" ? "\u2717 " : "\u25E6 ") + (message || stepName);
      log.appendChild(logItem);
      log.scrollTop = log.scrollHeight;
    }
  }

  /** Render install completion state (success or failure) inside modal. */
  function renderInstallComplete(card, allSteps, stepLabels, slug, entry, err, closeModalFn) {
    // Mark steps based on actual backend steps or mark them all done/failed
    var knownSteps = {};
    for (var i=0; i<_installSteps.length; i++) {
      var s = _installSteps[i];
      knownSteps[s.name] = s;
    }

    // Update each step's display
    for (var i=0; i<allSteps.length; i++) {
      var st = allSteps[i];
      var known = knownSteps[st];
      if (known) {
        updateStep(st, known.status, known.message);
      } else {
        // If backend didn't reach this step, mark as pending
        // or done if install succeeded
        updateStep(st, err ? "pending" : "done", stepLabels[st] || st);
      }
    }

    // Update progress bar
    var fill = card.querySelector(".ci-progress-bar-fill");
    if (fill) {
      var pct = err ? 100 : 100;
      fill.style.width = pct + "%";
      fill.style.background = err ? "linear-gradient(90deg,#fc8181,#e53e3e)" : "linear-gradient(90deg,#38a169,#68d391)";
    }

    // Show result section
    var body = card.querySelector(".clawhub-modal-body");
    if (!body) return;

    var resultHtml = '';
    if (err) {
      resultHtml = '<div class="ci-result ci-result-fail" style="margin-top:12px;padding:12px;background:#fff5f5;border:1px solid #fed7d7;border-radius:8px;">' +
        '<div style="font-weight:600;color:#c53030;margin-bottom:4px;">\u2717 ' + esc(__("skillsInstallFailed")) + '</div>' +
        '<div style="font-size:12px;color:#742a2a;">' + esc(err.message) + '</div>';
      if (err.code) {
        resultHtml += '<div style="font-size:11px;color:#718096;margin-top:4px;">' + esc(__("skillsErrorCode")) + ': <code>' + esc(err.code) + '</code></div>';
      }
      if (err.details && err.details.body_prefix) {
        resultHtml += '<details style="margin-top:6px;"><summary style="font-size:11px;color:#718096;cursor:pointer;">' + esc(__("skillsDetails")) + '</summary>' +
          '<pre style="background:#1a202c;color:#e2e8f0;font-size:10px;padding:6px;border-radius:4px;margin:4px 0 0;max-height:120px;overflow:auto;">' + esc(err.details.body_prefix) + '</pre></details>';
      }
      resultHtml += '</div>';
    } else if (entry) {
      resultHtml = '<div class="ci-result ci-result-ok" style="margin-top:12px;padding:12px;background;#f0fff4;border:1px solid #c6f6d5;border-radius:8px;">' +
        '<div style="font-weight:600;color:#276749;margin-bottom:4px;">\u2713 ' + esc(__("skillsInstalledSuccessfully")) + '</div>' +
        '<table style="font-size:12px;border-collapse:collapse;width:100%;">' +
          '<tr><td style="padding:3px 8px 3px 0;color:#718096;">' + esc(__("skillsFieldPath")) + '</td><td style="padding:3px 0;color:#2d3748;"><code>data/skills/' + esc(entry.slug || slug) + '</code></td></tr>' +
          '<tr><td style="padding:3px 8px 3px 0;color:#718096;">' + esc(__("skillsFieldStatus")) + '</td><td style="padding:3px 0;color:#2d3748;"><span class="hall-badge status-' + esc(entry.status||'needs_review') + '">' + esc(statusLabel(entry.status||'needs_review')) + '</span></td></tr>' +
          '<tr><td style="padding:3px 8px 3px 0;color:#718096;">' + esc(__("skillsFieldVersion")) + '</td><td style="padding:3px 0;color:#2d3748;">' + esc(entry.version||'?') + '</td></tr>' +
        '</table>' +
        '</div>';
    }

    // Append result and show action buttons
    body.insertAdjacentHTML('beforeend', resultHtml);

    var actions = card.querySelector(".ci-actions");
    if (actions) {
      var closeBtn = actions.querySelector(".ci-close-btn");
      var retryBtn = actions.querySelector(".ci-retry-btn");
      if (closeBtn) {
        closeBtn.style.display = "";
        closeBtn.addEventListener("click", function() {
          closeModalFn(true);
          // Refresh skills hall
          var hall = window.PawMateSkillsHall;
          if (hall && typeof hall.refresh === 'function') try { hall.refresh(); } catch(e) {}
        });
      }
      if (retryBtn) {
        retryBtn.style.display = err ? "" : "none";
        // Retry button shown on failure only
        if (err) {
          retryBtn.addEventListener("click", function() {
            showInstallProgress(card, {slug: slug, displayName: (entry && entry.name) || slug}, closeModalFn);
          });
        }
      }
    }
  }

  function doInstall(slug, version) {
    if (_installing || _installModalOpen) return;
    // Find skill data for the modal
    var skillData = {slug: slug, version: version};
    // Look up display name from _items or _detailData
    if (_detailSlug === slug && _detailData) {
      var det = _detailData.skill || _detailData;
      skillData.displayName = det.displayName || det.name || slug;
      var lv = det.latestVersion || {};
      skillData.version = lv.version || version || "latest";
    } else {
      for (var i=0; i<_items.length; i++) {
        if (_items[i].slug === slug || _items[i].name === slug) {
          skillData.displayName = _items[i].displayName || _items[i].name || slug;
          skillData.version = _items[i].version || version || "latest";
          break;
        }
      }
    }
    _installModalOpen = true;
    showInstallConfirm(skillData).then(function(confirmed) {
      _installModalOpen = false;
      if (!confirmed) return;
      // Install already handled by showInstallProgress inside modal
    });
  }

  // ------------------------------------------------------------------
  // Rendering
  // ------------------------------------------------------------------

  function render() {
    if (!_container || !isActiveTab()) return;
    if (_detailSlug) { renderDetailPanel(); return; }
    var h = '';

    // --- Search row ---
    h += '<div class="hall-search-row">';
    h += '<input type="text" id="chSearchInput" class="hall-search-input" placeholder="' + esc(__("skillsSearchPlaceholder")) + '" value="' + esc(_query) + '">';
    h += '<button id="chSearchBtn" class="hall-btn hall-btn-prime">' + esc(__("skillsSearch")) + '</button>';
    if (_mode === "search") h += ' <button id="chBackBtn" class="hall-btn">' + esc(__("skillsBackBrowse")) + '</button>';
    h += ' <button id="chDiagBtn" class="hall-btn hall-btn-diag">&#x2699; ' + esc(__("skillsDiagnose")) + '</button>';
    h += '</div>';

    // --- Diagnostics panel ---
    if (_diagLoading) {
      h += '<div class="hall-diag"><div class="hall-spinner"></div><p>' + esc(__("skillsDiagnosing")) + '</p></div>';
    } else if (_diagResult) {
      h += renderDiagnosticsPanel();
    }

    // --- Main content ---
    if (_loading) {
      h += '<div class="hall-center"><div class="hall-spinner"></div><p>' + esc(__("skillsLoadingClawHub")) + '</p></div>';
    } else if (_error) {
      h += renderErrorPanel(_error);
    } else if (!_items.length) {
      h += '<div class="hall-empty"><p>' + esc(__("skillsNoFound")) + '</p></div>';
    } else {
      for (var i=0; i<_items.length; i++) h += renderCard(_items[i]);
      if (_hasMore) h += '<div class="hall-center" style="padding:12px;"><button id="chLoadMoreBtn" class="hall-btn">' + esc(__("skillsLoadMore")) + '</button></div>';
    }

    _container.innerHTML = h;
    bindEvents();
  }

  function bindEvents() {
    var sb = document.getElementById("chSearchBtn");
    if (sb) sb.addEventListener("click", function(){ var inp=document.getElementById("chSearchInput"); if(inp&&inp.value.trim()) searchFirstPage(inp.value.trim()); });
    var inp2 = document.getElementById("chSearchInput");
    if (inp2) inp2.addEventListener("keydown", function(e){ if(e.key==="Enter"){ var v=inp2.value.trim(); if(v)searchFirstPage(v); } });
    var bb = document.getElementById("chBackBtn"); if (bb) bb.addEventListener("click", browseFirstPage);
    var rt = document.getElementById("chRetryBtn"); if (rt) rt.addEventListener("click", browseFirstPage);
    var rt2 = document.getElementById("chDiagRetryBtn"); if (rt2) rt2.addEventListener("click", browseFirstPage);
    var lm = document.getElementById("chLoadMoreBtn"); if (lm) lm.addEventListener("click", loadMore);

    var db = document.getElementById("chDiagBtn");
    if (db) db.addEventListener("click", runDiagnostics);

    var dt = document.getElementById("chDiagToggle");
    if (dt) dt.addEventListener("click", function(){
      var body = document.getElementById("chDiagBody");
      if (body) body.style.display = body.style.display === "none" ? "block" : "none";
    });
    var ebt = document.getElementById("chErrBodyToggle");
    if (ebt) ebt.addEventListener("click", function(){
      var body = document.getElementById("chErrBody");
      if (body) body.style.display = body.style.display === "none" ? "block" : "none";
    });
  }

  // ------------------------------------------------------------------
  // Error panel (detailed)
  // ------------------------------------------------------------------

  function renderErrorPanel(err) {
    var h = '';
    h += '<div class="hall-error hall-error-detailed">';
    h += '<div class="hall-error-icon">&#x26A0;</div>';
    h += '<div class="hall-error-content">';
    h += '<div class="hall-error-title">' + esc(__("skillsRequestFailed")) + '</div>';
    h += '<div class="hall-error-message">' + esc(err.message) + '</div>';

    if (err.code) {
      h += '<table class="hall-error-table">';
      h += '<tr><td>' + esc(__("skillsErrorCode")) + '</td><td><code>' + esc(err.code) + '</code></td></tr>';
      if (err.details) {
        var d = err.details;
        if (d.url) h += '<tr><td>' + esc(__("skillsEndpoint")) + '</td><td><code>' + esc(d.url) + '</code></td></tr>';
        if (d.status) h += '<tr><td>' + esc(__("skillsHttpStatus")) + '</td><td><code>' + esc(String(d.status)) + '</code></td></tr>';
        if (d.retry_after) h += '<tr><td>' + esc(__("skillsRetryAfter")) + '</td><td><code>' + esc(String(d.retry_after)) + '</code></td></tr>';
        if (d.stage) h += '<tr><td>' + esc(__("skillsTimeoutStage")) + '</td><td><code>' + esc(d.stage) + '</code></td></tr>';
        if (d.reason) h += '<tr><td>' + esc(__("skillsReason")) + '</td><td><code>' + esc(d.reason) + '</code></td></tr>';
        if (d.elapsed) h += '<tr><td>' + esc(__("skillsElapsed")) + '</td><td><code>' + esc(String(d.elapsed)) + 's</code></td></tr>';
        if (d.body_prefix) {
          h += '<tr><td colspan="2"><button id="chErrBodyToggle" class="hall-btn hall-btn-small">&#x25BC; ' + esc(__("skillsShowResponseBody")) + '</button></td></tr>';
          h += '<tr id="chErrBody" style="display:none;"><td colspan="2"><pre class="hall-error-body">' + esc(d.body_prefix) + '</pre></td></tr>';
        }
      }
      h += '</table>';
    }

    h += '<div style="margin-top:8px;">';
    h += '<button id="chRetryBtn" class="hall-btn hall-btn-prime">' + esc(__("skillsRetry")) + '</button>';
    h += ' <button id="chDiagBtnFail" class="hall-btn hall-btn-diag">&#x2699; ' + esc(__("skillsDiagnose")) + '</button>';
    h += '</div></div></div>';
    return h;
  }

  // ------------------------------------------------------------------
  // Diagnostics panel
  // ------------------------------------------------------------------

  function renderDiagnosticsPanel() {
    var r = _diagResult;
    if (!r) return '';

    var eps = [];

    if (r.ok && Array.isArray(r.endpoints)) {
      for (var i=0; i<r.endpoints.length; i++) eps.push(r.endpoints[i]);
    } else if (!r.ok) {
      var err = r.error||{};
      return '<div class="hall-diag hall-diag-error"><div class="hall-diag-title">' + esc(__("skillsDiagnosticsError")) + '</div><p>' + esc(err.message||__("skillsDiagnosticsError")) + '</p></div>';
    }

    var h = '';
    h += '<div class="hall-diag">';
    h += '<div class="hall-diag-title">' + esc(__("skillsDiagnosticsTitle")) + '</div>';

    for (var i=0; i<eps.length; i++) {
      var ep = eps[i];
      var statusClass = ep.ok ? "diag-ok" : "diag-fail";
      h += '<div class="hall-diag-row ' + statusClass + '">';
      h += '<div class="diag-status">' + (ep.ok ? '&#x2713;' : '&#x2717;') + '</div>';
      h += '<div class="diag-info">';
      h += '<div class="diag-name">' + esc(ep.name) + '</div>';
      h += '<div class="diag-url"><code>' + esc(ep.url||"") + '</code></div>';
      if (ep.ok) {
        h += '<div class="diag-meta">HTTP ' + esc(String(ep.status)) + ' | ' + esc(String(ep.elapsed_ms||"?")) + 'ms | ' + esc(ep.content_type||"") + '</div>';
      } else {
        h += '<div class="diag-meta">' + esc(ep.error_code||"error") + (ep.status ? ' | HTTP ' + esc(String(ep.status)) : '') + ' | ' + esc(String(ep.elapsed_ms||"?")) + 'ms</div>';
        if (ep.body_prefix) h += '<div class="diag-meta"><pre class="hall-error-body">' + esc(ep.body_prefix) + '</pre></div>';
        if (ep.reason) h += '<div class="diag-meta">' + esc(ep.reason) + '</div>';
      }
      h += '</div></div>';
    }

    if (r.recommendation) {
      h += '<div class="hall-diag-recommend">' + esc(__("skillsRecommendation")) + ': <strong>' + esc(r.recommendation) + '</strong></div>';
    }

    h += '<div style="margin-top:8px;">';
    h += '<button id="chDiagCloseBtn" class="hall-btn">' + esc(__("skillsClose")) + '</button>';
    h += ' <button id="chDiagRefreshBtn" class="hall-btn hall-btn-diag">&#x2699; ' + esc(__("skillsRefresh")) + '</button>';
    h += '</div></div>';

    setTimeout(function(){
      var closeBtn = document.getElementById("chDiagCloseBtn");
      if (closeBtn) closeBtn.addEventListener("click", function(){ _diagResult = null; render(); });
      var refreshBtn = document.getElementById("chDiagRefreshBtn");
      if (refreshBtn) refreshBtn.addEventListener("click", runDiagnostics);
      var fb = document.getElementById("chDiagBtnFail");
      if (fb) fb.addEventListener("click", runDiagnostics);
    }, 0);

    return h;
  }

  // ------------------------------------------------------------------
  // Card and detail rendering
  // ------------------------------------------------------------------

  function renderCard(s) {
    var slug = s.slug||s.name||"";
    return '<div class="hall-card"><div class="hall-card-body"><div class="hall-card-info">'
      + '<h4>' + esc(s.displayName||s.name||slug) + '</h4>'
      + '<div class="hall-card-meta">' + esc(slug) + (s.version?' v'+esc(s.version):'') + '</div>'
      + '<div class="hall-card-meta">' + esc((s.summary||s.description||"").substring(0,140)) + '</div>'
      + '</div><div class="hall-card-actions">'
      + '<button class="hall-btn" data-ch-action="detail" data-ch-slug="' + esc(slug) + '">' + esc(__("skillsDetail")) + '</button>'
      + '<button class="hall-btn" data-ch-action="install" data-ch-slug="' + esc(slug) + '">' + esc(__("skillsInstallToLocal")) + '</button>'
      + '</div></div></div>';
  }

  function showDetail(slug) {
    var generation = _viewGeneration;
    _detailSlug = slug;
    _detailLoading = true;
    _detailError = null;
    _detailData = null;
    _detailMd = "";
    render();
    callAsync("getClawHubSkillDetail", slug).then(function(r) {
      if (!isCurrentGeneration(generation)) return;
      _detailLoading = false;
      if (r.ok) {
        _detailData = r.skill || r;
        _detailMd = r.skill_md || "";
      } else {
        _detailError = makeErrorObj(r);
      }
      render();
    });
  }

  function renderDetailPanel() {
    if (!_container || !isActiveTab()) return;
    if (_detailLoading) {
      _container.innerHTML = '<div class="hall-center"><div class="hall-spinner"></div><p>' + esc(__("skillsLoading")) + '</p></div>';
      return;
    }
    if (_detailError) {
      var h = renderErrorPanel(_detailError);
      h += '<div style="margin:10px 0;"><button class="hall-btn" id="chDetailBack">' + esc(__("skillsBackToList")) + '</button></div>';
      _container.innerHTML = h;
      document.getElementById("chDetailBack").addEventListener("click", backToList);
      var rt = document.getElementById("chRetryBtn");
      if (rt) rt.addEventListener("click", function(){ showDetail(_detailSlug); });
      return;
    }
    var s = (_detailData && _detailData.skill) || _detailData || {};
    var lv = s.latestVersion || {};
    var ow = s.owner || {};
    var slug = s.slug || _detailSlug || "";
    _container.innerHTML = '<div class="hall-detail">'
      + '<div style="margin-bottom:10px;"><button class="hall-btn" id="chDetailBack">' + esc(__("skillsBackToList")) + '</button></div>'
      + '<h5>' + esc(s.displayName || s.name || slug) + '</h5>'
      + '<div class="hall-card-meta">' + esc(slug) + (lv.version ? ' v' + esc(lv.version) : '') + ' | ' + esc(__("skillsBy")) + ' ' + esc(ow.handle || ow.displayName || "?") + '</div>'
      + '<div class="hall-card-meta">' + esc((s.summary || s.description || "").substring(0, 300)) + '</div>'
      + renderRequires(s)
      + '<button class="hall-btn hall-btn-prime" style="margin:8px 0;" data-ch-action="install" data-ch-slug="' + esc(slug) + '">' + esc(__("skillsInstallToDataSkills")) + '</button>'
      + '<p class="hall-safety">' + esc(__("skillsDetailSafety")) + '</p>'
      + '<pre class="hall-md">' + esc(_detailMd.substring(0, 3000)) + '</pre>'
      + '</div>';
    document.getElementById("chDetailBack").addEventListener("click", backToList);
  }

  function renderRequires(s) {
    var r = s.requires || s.requires_env || null;
    if (!r) return '';
    var h = '<div class="hall-card-meta" style="margin-top:4px;">';
    if (r.env && r.env.length) h += 'Env: ' + esc(String(r.env)) + ' ';
    if (r.binaries && r.binaries.length) h += 'Binaries: ' + esc(String(r.binaries)) + ' ';
    h += '</div>';
    return h;
  }

  function backToList() {
    _detailSlug = null;
    _detailData = null;
    _detailMd = "";
    _detailError = null;
    render();
  }

  // ------------------------------------------------------------------
  // Init
  // ------------------------------------------------------------------

  function init(options) {
    _container = (options&&options.container)||null;
    if (!_container) return;
    if (!_initialized) {
      _initialized = true;
      _container.addEventListener('click', function(e) {
        var btn = e.target.closest('[data-ch-action=detail]');
        if (btn) showDetail(btn.dataset.chSlug);
        btn = e.target.closest('[data-ch-action=install]');
        if (btn) doInstall(btn.dataset.chSlug, btn.dataset.chVersion||'');
      });
    }
    browseFirstPage();
  }

  window.PawMateSkillsClawHub = {
    init: init,
    deactivate: deactivate,
    browseFirstPage: browseFirstPage,
    searchFirstPage: searchFirstPage,
    runDiagnostics: runDiagnostics
  };
})();
