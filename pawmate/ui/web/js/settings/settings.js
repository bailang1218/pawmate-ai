/* =============================================================
   PawMate — Settings Module (settings.js)
   设置面板：打开/关闭、分区切换、各 section 渲染、保存

   依赖：PawChat.escapeHtml (for safe rendering)
   暴露：window.PawSettings
   ============================================================= */

window.PawSettings = (function () {
  "use strict";

  /* ── i18n helper (reuse global if available) ──── */
  let __uiLang = "zh-CN";

  function __(key) {
    const dicts = window.__PAWMATE_I18N;
    if (!dicts) return key;
    const d = dicts[__uiLang] || dicts["zh-CN"];
    return (d && d[key]) || key;
  }

  function applyI18n(root) {
    if (!root) root = document;
    root.querySelectorAll("[data-i18n]").forEach(function (el) {
      el.textContent = __(el.getAttribute("data-i18n"));
    });
  }

  /* ── State ─────────────────────────────────────── */
  const settingsState = {
    config: null,
    currentSection: "general",
    isOpen: false,
    setupMessage: null,
    uiLang: "zh-CN",
  };

  /* ── References ────────────────────────────────── */
  function getConfigBridge() {
    return window.configBridge || null;
  }
  function $id(id) {
    return document.getElementById(id);
  }
  const esc = (s) => PawChat ? PawChat.escapeHtml(String(s)) : String(s);

  /* ── Provider labels ───────────────────────────── */
  const PROVIDER_LABELS = {
    anthropic: "Anthropic (Claude)",
    openai: "OpenAI (GPT)",
    deepseek: "DeepSeek",
    qwen: "Qwen (DashScope)",
    gemini: "Google Gemini",
    minimaxi: "MiniMax",
  };

  const PROVIDER_ALIASES = {
    minimax: "minimaxi",
  };

  const ATTACH_MODE_LABELS = {
    auto: "自动（推荐）",
    prefer: "优先接管我已打开的浏览器",
    attach: "只接管已打开的浏览器（接不上则报错，不新开）",
    new: "总是新开一个浏览器",
  };

  const PROFILE_LABELS = {
    auto: "沿用我的浏览器登录状态",
    managed: "使用全新的空白浏览器",
  };

  const AUTOMATION_LEVEL_LABELS = {
    conservative: "保守 — 失败就停下报告，不碰你已打开的浏览器",
    standard: "标准（默认）— 失败时自动用更底层的方式重试",
    aggressive: "放开 — 允许像人一样直接点击屏幕兜底（每次会先征求你同意）",
  };

  function labelForStorage(labels, value, fallbackValue) {
    const normalized = Object.prototype.hasOwnProperty.call(labels, value) ? value : fallbackValue;
    return labels[normalized] || normalized || "";
  }

  function storageForLabel(labels, label) {
    const text = String(label || "").trim();
    return Object.keys(labels).find((key) => labels[key] === text) || "";
  }

  function normalizedAutomationLevel(value) {
    return Object.prototype.hasOwnProperty.call(AUTOMATION_LEVEL_LABELS, value) ? value : "standard";
  }

  function customSelectStorageValue(rootId, hiddenId, labels, fallbackValue) {
    const labelEl = document.querySelector("#" + rootId + " .custom-select-label");
    const fromLabel = storageForLabel(labels, labelEl ? labelEl.textContent : "");
    const hidden = $id(hiddenId);
    const fromHidden = hidden ? hidden.value : "";
    if (fromLabel) return fromLabel;
    if (Object.prototype.hasOwnProperty.call(labels, fromHidden)) return fromHidden;
    return fallbackValue;
  }

  const PROVIDER_MODEL_OPTIONS = {
    anthropic: ["claude-opus-4-8", "claude-sonnet-4-6", "claude-opus-4-7", "claude-opus-4-6", "claude-haiku-4-5-20251001", "claude-3-5-sonnet-20241022", "claude-3-5-haiku-20241022"],
    openai: ["gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.4-nano", "gpt-5.1", "gpt-5", "gpt-5-pro", "gpt-5-mini", "gpt-5-nano", "gpt-4.1", "gpt-4.1-mini", "gpt-4o", "gpt-4o-mini"],
    deepseek: ["deepseek-v4-flash", "deepseek-v4-pro", "deepseek-chat", "deepseek-reasoner"],
    qwen: ["qwen3.7-plus", "qwen3.7-max", "qwen3.6-plus", "qwen-plus", "qwen-plus-latest", "qwen3.5-plus", "qwen-max", "qwen3-max", "qwen-turbo", "qwen-flash", "qwen3.5-flash", "qwen3-coder-plus", "qwen3-vl-plus", "qwq-plus"],
    gemini: ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.5-flash-lite", "gemini-2.0-flash", "gemini-1.5-pro"],
    minimaxi: ["MiniMax-M3", "MiniMax-M2.7", "MiniMax-M2.7-highspeed", "MiniMax-Text-01", "MiniMax-VL-01", "MiniMax-M2.5", "MiniMax-M2.5-highspeed", "MiniMax-M2.1", "MiniMax-M2.1-highspeed", "MiniMax-M2"],
  };
  const CUSTOM_MODEL_VALUE = "__custom_model__";

  function normalizeProvider(provider) {
    const key = String(provider || "").trim().toLowerCase();
    return PROVIDER_ALIASES[key] || key || "deepseek";
  }

  function defaultModelFor(provider) {
    const options = PROVIDER_MODEL_OPTIONS[normalizeProvider(provider)] || [];
    return options[0] || "";
  }

  function modelOptionLabels(provider) {
    const labels = {};
    const options = PROVIDER_MODEL_OPTIONS[normalizeProvider(provider)] || [];
    options.forEach(function (modelName) {
      labels[modelName] = modelName;
    });
    labels[CUSTOM_MODEL_VALUE] = __("customModelOption");
    return labels;
  }

  // ================================================================
  // Drawer open / close
  // ================================================================

  function open() {
    if (settingsState.isOpen) return;
    if (window.PawPanelManager) window.PawPanelManager.requestOpen("settings");
    else if (window.PawMateSkillsHall && typeof window.PawMateSkillsHall.close === "function") window.PawMateSkillsHall.close();
    settingsState.isOpen = true;
    __uiLang = settingsState.uiLang || "zh-CN";

    $id("settingsOverlay").style.display = "block";
    $id("settingsDrawer").style.display = "flex";
    $id("settingsDrawer").classList.remove("closing");
    if (window.PawPanelManager) window.PawPanelManager.setBodyLocked(true);
    else document.body.style.overflow = "hidden";

    requestAnimationFrame(() =>
      requestAnimationFrame(() => {
        $id("settingsOverlay").classList.add("open");
        $id("settingsDrawer").classList.add("open");
      })
    );

    setTimeout(function () {
      const cb = getConfigBridge();
      if (cb) {
        cb.getConfig(function (raw) {
          try {
            settingsState.config = JSON.parse(raw);
          } catch (e) {
            settingsState.config = {};
          }
          settingsState.uiLang =
            (settingsState.config.ui && settingsState.config.ui.language_mode) ||
            "zh-CN";
          __uiLang = settingsState.uiLang;
          applyI18n($id("settingsDrawer"));
          _updateActiveTab(settingsState.currentSection);
          renderSection(settingsState.currentSection);
        });
      } else {
        settingsState.config = {};
        _updateActiveTab(settingsState.currentSection);
        renderSection(settingsState.currentSection);
      }
    }, 100);
  }

  function close() {
    if (!settingsState.isOpen) return;
    // Close sandbox confirm modal if open
    const so = $id("sandboxConfirmOverlay");
    const sm = $id("sandboxConfirmModal");
    if (so) so.classList.remove("open");
    if (sm) sm.classList.remove("open");

    settingsState.isOpen = false;
    const drawer = $id("settingsDrawer");
    const overlay = $id("settingsOverlay");
    overlay.classList.remove("open");
    drawer.classList.remove("open");
    drawer.classList.add("closing");
    if (window.PawPanelManager) {
      window.PawPanelManager.notifyClosed("settings");
      window.PawPanelManager.setBodyLocked(window.PawPanelManager.hasOpenPanel());
    } else {
      document.body.style.overflow = "";
    }
    setTimeout(function () {
      drawer.style.display = "none";
      overlay.style.display = "none";
      drawer.classList.remove("closing");
    }, 200);
  }

  function toggle() {
    settingsState.isOpen ? close() : open();
  }

  function openSection(section, message) {
    if (VALID_SECTIONS.indexOf(section) === -1) section = "general";
    settingsState.currentSection = section;
    settingsState.setupMessage = message || null;
    if (settingsState.isOpen) {
      _updateActiveTab(section);
      renderSection(section);
      return;
    }
    open();
  }

  // ================================================================
  // Section switching
  // ================================================================

  const VALID_SECTIONS = ["general", "llm", "browser", "tools", "security", "logs", "about"];

  function _updateActiveTab(section) {
    document.querySelectorAll(".nav-btn").forEach(function (btn) {
      btn.classList.toggle("active", btn.getAttribute("data-section") === section);
    });
  }

  function switchSection(section) {
    if (settingsState.currentSection === section) return;
    if (VALID_SECTIONS.indexOf(section) === -1) section = "general";
    settingsState.currentSection = section;
    _updateActiveTab(section);
    renderSection(section);
  }

  function renderSection(section) {
    const cfg = settingsState.config || {};
    const container = $id("settingsContent");
    if (VALID_SECTIONS.indexOf(section) === -1) {
      section = "general";
      settingsState.currentSection = "general";
    }
    switch (section) {
      case "general":  renderGeneralSection(container, cfg);  applyI18n(container); break;
      case "llm":      renderLLMSection(container, cfg);      applyI18n(container); break;
      case "browser":  renderBrowserSection(container, cfg);  applyI18n(container); break;
      case "tools":    renderToolsSection(container, cfg);    applyI18n(container); break;
      case "security": renderSecuritySection(container, cfg); applyI18n(container); break;
      case "logs":
        renderLogsSection(container);
        break;
      case "about":
        renderPlaceholder("about", container);
        break;
    }
  }

  // ================================================================
  // Custom select dropdown wiring
  // ================================================================

  function wireCustomSelect(rootId, hiddenId) {
    const root = $id(rootId);
    const hidden = $id(hiddenId);
    if (!root || !hidden) return;
    if (root.dataset.wired === "1") return;
    root.dataset.wired = "1";

    const btn = root.querySelector(".custom-select-btn");
    const label = root.querySelector(".custom-select-label");
    const opts = root.querySelectorAll(".custom-select-opt");

    btn.addEventListener("click", function (e) {
      e.stopPropagation();
      document.querySelectorAll(".custom-select.open").forEach(function (s) {
        if (s !== root) s.classList.remove("open");
      });
      root.classList.toggle("open");
    });

    opts.forEach(function (o) {
      o.addEventListener("click", function (e) {
        e.stopPropagation();
        const v = o.dataset.value;
        if (label) label.textContent = o.textContent;
        hidden.value = v;
        opts.forEach(function (opt) {
          opt.classList.toggle("active", opt.dataset.value === v);
        });
        hidden.dispatchEvent(new Event("change", { bubbles: true }));
        root.classList.remove("open");
      });
    });
  }

  // ================================================================
  // Section renderers
  // ================================================================

  function renderGeneralSection(container, cfg) {
    const uiLang = (cfg.ui && cfg.ui.language_mode) || "zh-CN";
    const assLang = (cfg.assistant && cfg.assistant.language_mode) || "auto";
    const desktopPet = cfg.desktop_pet || {};
    const petEnabled = !!desktopPet.enabled;
    const replySurface = desktopPet.reply_surface === "bubble" ? "bubble" : "window";

    const langLabels = { "zh-CN": "简体中文", "en-US": "English", "ja-JP": "日本語" };
    const assLabels = { auto: "Auto", "zh-CN": "简体中文", "en-US": "English" };
    const replySurfaceLabels = { window: "窗口", bubble: "气泡" };

    container.innerHTML = [
      '<h3 data-i18n="sectionGeneral">通用设置</h3>',
      '<section class="pet-toggle-card">',
      '  <div class="pet-toggle-copy">',
      '    <strong>桌宠</strong>',
      '    <span>显示 PawMate 桌面伙伴，并接收聊天/工具状态动作。</span>',
      '  </div>',
      '  <label class="pet-switch" for="cfgDesktopPetEnabled">',
      '    <input type="checkbox" id="cfgDesktopPetEnabled"' + (petEnabled ? " checked" : "") + ' />',
      '    <span class="pet-switch-track"><span class="pet-switch-thumb"></span></span>',
      '  </label>',
      '</section>',
      '<p class="field-hint pet-toggle-hint">关闭后不启动桌宠窗口，也不消费聊天动作信号。</p>',
      _fieldSelect("selPetReplySurface", "cfgPetReplySurface", "桌宠模式下回复显示在", replySurface, replySurfaceLabels, !petEnabled),
      '<p class="field-hint pet-toggle-hint">关闭桌宠时始终由主窗口显示；此项只决定桌宠开启时主动弹出的显示层。</p>',
      _fieldSelect("selUiLang", "cfgUiLang", __("uiLanguage"), uiLang, langLabels),
      '<p class="field-hint" data-i18n="uiLanguageHint">控制设置界面、按钮和提示文字的显示语言。</p>',
      _fieldSelect("selLangMode", "cfgLangMode", __("assistantLanguage"), assLang, assLabels),
      '<p class="field-hint" data-i18n="assistantLanguageHint">控制 AI 助手默认使用的回复语言。</p>',
      '<div class="field-group"><label>最大推理轮次</label>',
      '  <input type="number" id="cfgMaxTurns" value="' + (cfg.runtime?.max_turns ?? 30) + '" min="5" max="100" style="width:100px" /></div>',
      '<p class="field-hint">ReAct 推理循环的安全上限。LLM 完成任务会自然退出，此值仅在死循环时兜底。推荐 20~50。</p>',
      '<button class="save-btn" id="saveGeneralBtn" data-i18n="saveGeneralConfig">保存通用设置</button>',
    ].join("\n");

    wireCustomSelect("selUiLang", "cfgUiLang");
    wireCustomSelect("selLangMode", "cfgLangMode");
    wireCustomSelect("selPetReplySurface", "cfgPetReplySurface");

    $id("cfgDesktopPetEnabled").addEventListener("change", function () {
      const disabled = !$id("cfgDesktopPetEnabled").checked;
      document.querySelectorAll("#selPetReplySurface .custom-select-btn, #selPetReplySurface .custom-select-opt").forEach(function (el) {
        el.disabled = disabled;
      });
    });

    $id("saveGeneralBtn").addEventListener("click", function () {
      const newUiLang = $id("cfgUiLang").value;
      const newAssLang = $id("cfgLangMode").value.trim();
      const maxTurns = parseInt($id("cfgMaxTurns").value) || 30;
      settingsState.config.ui = settingsState.config.ui || {};
      settingsState.config.ui.language_mode = newUiLang;
      settingsState.config.assistant = settingsState.config.assistant || {};
      settingsState.config.assistant.language_mode = newAssLang;
      settingsState.config.runtime = settingsState.config.runtime || {};
      settingsState.config.runtime.max_turns = maxTurns;
      settingsState.config.desktop_pet = settingsState.config.desktop_pet || {};
      settingsState.config.desktop_pet.enabled = !!$id("cfgDesktopPetEnabled").checked;
      settingsState.config.desktop_pet.reply_surface = $id("cfgPetReplySurface").value === "bubble" ? "bubble" : "window";
      if (window.PawDesktopPetTop && typeof window.PawDesktopPetTop.sync === "function") {
        window.PawDesktopPetTop.sync(settingsState.config.desktop_pet.enabled);
      }
      __uiLang = newUiLang;
      settingsState.uiLang = newUiLang;
      renderSection(settingsState.currentSection);
      applyI18n($id("settingsDrawer"));
      saveConfig(__("settings_saved"));
    });
  }

  function renderLLMSection(container, cfg) {
    const llm = cfg.llm || {};
    const rawProvider = llm.provider || "deepseek";
    const provider = normalizeProvider(rawProvider);
    const provCfg = llm[provider] || llm[rawProvider] || {};
    const model = provCfg.model || defaultModelFor(provider);
    const modelOptions = PROVIDER_MODEL_OPTIONS[provider] || [];
    const modelIsKnown = modelOptions.indexOf(model) !== -1;
    const modelSelectValue = modelIsKnown ? model : CUSTOM_MODEL_VALUE;
    const customModelValue = modelIsKnown ? "" : model;
    const hasApiKey = String(provCfg.api_key || "").trim().length > 0;
    const providerHint = provider === "deepseek"
      ? __("setupGuideStepModelDeepSeek")
      : __("setupGuideStepModelDefault");
    const setupGuide = (!hasApiKey || settingsState.setupMessage) ? [
      '<div class="setup-guide-card">',
      '  <div class="setup-guide-kicker">' + esc(__("setupGuideKicker")) + '</div>',
      '  <strong>' + esc(settingsState.setupMessage || __("setupGuideTitle")) + '</strong>',
      '  <p>' + esc(__("setupGuideBody")) + '</p>',
      '  <ol>',
      '    <li>' + esc(__("setupGuideStepProvider")) + '</li>',
      '    <li>' + esc(__("setupGuideStepApiKey")) + '</li>',
      '    <li>' + esc(providerHint) + '</li>',
      '  </ol>',
      '</div>',
    ].join("\n") : "";

    container.innerHTML = [
      setupGuide,
      '<h3 data-i18n="sectionLLMProvider">模型服务</h3>',
      _fieldSelect("selLlmProvider", "cfgProvider", __("provider"), provider, PROVIDER_LABELS),
      '<div class="field-group"><label data-i18n="apiKey">API 密钥</label>',
      '  <input type="password" id="cfgApiKey" value="' + esc(provCfg.api_key || "") + '" placeholder="sk-..." /></div>',
      _fieldSelect("selLlmModel", "cfgModelSelect", __("model"), modelSelectValue, modelOptionLabels(provider)),
      '<p class="field-hint model-picker-hint">' + esc(__("modelPickerHint")) + '</p>',
      '<div class="field-group custom-model-group" id="customModelGroup"' + (modelSelectValue === CUSTOM_MODEL_VALUE ? "" : " hidden") + '><label data-i18n="customModel">自定义模型名</label>',
      '  <input type="text" id="cfgCustomModel" value="' + esc(customModelValue) + '" placeholder="' + esc(defaultModelFor(provider)) + '" />',
      '  <p class="field-hint" data-i18n="customModelHint">如果列表里没有你要用的模型，可以直接填写服务商文档里的 model id。</p></div>',
      '<button class="save-btn" id="saveLLMBtn" data-i18n="saveLLMConfig">保存模型配置</button>',
    ].join("\n");

    wireCustomSelect("selLlmProvider", "cfgProvider");
    wireCustomSelect("selLlmModel", "cfgModelSelect");

    function syncCustomModelGroup() {
      const group = $id("customModelGroup");
      if (!group) return;
      group.hidden = $id("cfgModelSelect").value !== CUSTOM_MODEL_VALUE;
      if (!group.hidden) {
        const input = $id("cfgCustomModel");
        if (input && !input.value.trim()) input.placeholder = defaultModelFor($id("cfgProvider").value);
      }
    }

    $id("cfgProvider").addEventListener("change", function () {
      const newProvider = normalizeProvider($id("cfgProvider").value);
      const nextCfg = (settingsState.config.llm || {})[newProvider] || {};
      settingsState.config.llm = settingsState.config.llm || {};
      settingsState.config.llm.provider = newProvider;
      settingsState.config.llm[newProvider] = settingsState.config.llm[newProvider] || nextCfg;
      renderSection(settingsState.currentSection);
      applyI18n($id("settingsDrawer"));
    });
    $id("cfgModelSelect").addEventListener("change", syncCustomModelGroup);
    syncCustomModelGroup();

    $id("saveLLMBtn").addEventListener("click", function () {
      const newProvider = normalizeProvider($id("cfgProvider").value);
      const apiKey = $id("cfgApiKey").value;
      const selectedModel = $id("cfgModelSelect").value;
      const model = selectedModel === CUSTOM_MODEL_VALUE
        ? ($id("cfgCustomModel").value.trim() || defaultModelFor(newProvider))
        : (selectedModel || defaultModelFor(newProvider));
      settingsState.config.llm = settingsState.config.llm || {};
      settingsState.config.llm.provider = newProvider;
      settingsState.config.llm[newProvider] = settingsState.config.llm[newProvider] || {};
      settingsState.config.llm[newProvider].api_key = apiKey;
      settingsState.config.llm[newProvider].model = model;
      if (apiKey.trim()) settingsState.setupMessage = null;
      renderSection(settingsState.currentSection);
      applyI18n($id("settingsDrawer"));
      saveConfig(__("settings_saved"));
    });
  }

  function renderBrowserSection(container, cfg) {
    const bu = (cfg.tools || {}).browser_use || {};
    const browser = bu.browser || "edge";
    const attachMode = Object.prototype.hasOwnProperty.call(ATTACH_MODE_LABELS, bu.attach_mode) ? bu.attach_mode : "auto";
    const automationLevel = normalizedAutomationLevel(bu.automation_level || "standard");
    const rawProfileDirectory = String(bu.profile_directory || "auto").trim() || "auto";
    const profileDirectory = rawProfileDirectory === "managed" ? "managed" : "auto";
    const advancedProfileDirectory = Object.prototype.hasOwnProperty.call(PROFILE_LABELS, rawProfileDirectory) ? "" : rawProfileDirectory;
    const browserLabels = { auto: __("auto"), edge: __("browserEdge"), chrome: __("browserChrome") };

    container.innerHTML = [
      '<h3 data-i18n="sectionBrowser">浏览器自动化</h3>',
      '<div class="checkbox-group">',
      '  <input type="checkbox" id="cfgBrowserEnabled"' + (bu.enabled !== false ? " checked" : "") + " />",
      '  <label for="cfgBrowserEnabled" data-i18n="enableBrowserAutomation">启用浏览器自动化</label>',
      "</div>",
      _fieldSelect("selBrowser", "cfgBrowserType", __("browser"), browser, browserLabels),
      _fieldSelect("selAttach", "cfgAttachMode", __("attachMode"), attachMode, ATTACH_MODE_LABELS),
      _fieldSelect("selAutomationLevel", "cfgAutomationLevel", __("automationLevel"), automationLevel, AUTOMATION_LEVEL_LABELS),
      _fieldSelect("selProfileDir", "cfgProfileDir", __("profileDirectory"), profileDirectory, PROFILE_LABELS),
      '<div class="checkbox-group">',
      '  <input type="checkbox" id="cfgBrowserHeadless"' + (bu.headless ? " checked" : "") + " />",
      '  <label for="cfgBrowserHeadless" data-i18n="headlessMode">静默后台运行</label>',
      "</div>",
      '<p class="field-hint" data-i18n="headlessModeHint">运行时不显示浏览器窗口</p>',
      '<details class="settings-subsection browser-advanced-section">',
      '  <summary data-i18n="advancedSettings">高级</summary>',
      '  <div class="checkbox-group">',
      '    <input type="checkbox" id="cfgUseCDP"' + (bu.cdp_url ? " checked" : "") + " />",
      '    <label for="cfgUseCDP" data-i18n="useCustomCdpUrl">手动指定浏览器调试地址</label>',
      "  </div>",
      '  <div class="field-group"><label data-i18n="cdpUrl">浏览器调试地址（CDP）</label>',
      '    <input type="text" id="cfgCdpUrl" value="' + esc(bu.cdp_url || "") + '" placeholder="' + esc(__("cdpUrlPlaceholder")) + '" />',
      '    <p class="field-hint" data-i18n="cdpUrlHint">一般无需填写，仅当你手动以调试模式启动了浏览器时使用。</p></div>',
      '  <div class="field-group"><label data-i18n="timeoutSeconds">超时时间（秒）</label>',
      '    <input type="text" id="cfgBrowserTimeout" value="' + (bu.timeout || 180) + '" /></div>',
      '  <div class="field-group"><label data-i18n="advancedProfileDirectory">高级登录状态值</label>',
      '    <input type="text" id="cfgAdvancedProfileDir" value="' + esc(advancedProfileDirectory) + '" placeholder="native / C:\\\\Path\\\\To\\\\Profile" />',
      '    <p class="field-hint" data-i18n="advancedProfileDirectoryHint">一般留空。只有需要指定 native 或自定义用户目录时才填写。</p></div>',
      "</details>",
      '<button class="save-btn" id="saveBrowserBtn" data-i18n="saveBrowserConfig">保存浏览器配置</button>',
    ].join("\n");

    wireCustomSelect("selBrowser", "cfgBrowserType");
    wireCustomSelect("selAttach", "cfgAttachMode");
    wireCustomSelect("selAutomationLevel", "cfgAutomationLevel");
    wireCustomSelect("selProfileDir", "cfgProfileDir");

    $id("saveBrowserBtn").addEventListener("click", function () {
      const t = (settingsState.config.tools = settingsState.config.tools || {});
      t.browser_use = t.browser_use || {};
      const b = t.browser_use;
      b.enabled = $id("cfgBrowserEnabled").checked;
      b.browser = $id("cfgBrowserType").value;
      b.headless = $id("cfgBrowserHeadless").checked;
      b.timeout = parseInt($id("cfgBrowserTimeout").value) || 180;
      b.attach_mode = customSelectStorageValue("selAttach", "cfgAttachMode", ATTACH_MODE_LABELS, "auto");
      b.automation_level = customSelectStorageValue("selAutomationLevel", "cfgAutomationLevel", AUTOMATION_LEVEL_LABELS, "standard");
      b.cdp_url = $id("cfgUseCDP").checked ? $id("cfgCdpUrl").value : "";
      b.profile_directory = ($id("cfgAdvancedProfileDir").value || "").trim() || customSelectStorageValue("selProfileDir", "cfgProfileDir", PROFILE_LABELS, "auto");
      saveConfig(__("settings_saved"));
    });
  }

  function renderToolsSection(container, cfg) {
    const approval = cfg.approval || {};

    container.innerHTML = [
      '<h3 data-i18n="sectionTools">工具</h3>',
      '<section class="settings-subsection approval-settings-section">',
      '  <h4>工具审批</h4>',
      '  <div class="checkbox-group">',
      '    <input type="checkbox" id="cfgApprovalLlmExplain"' + (approval.llm_explain_enabled !== false ? " checked" : "") + " />",
      '    <label for="cfgApprovalLlmExplain">审批单启用模型意图解释</label>',
      "  </div>",
      '  <div class="field-info">开启后，PawMate 会在需要放行工具前多调用一次当前模型，用人话说明调用目的、执行方式和注意点。关闭后只显示本地规则生成的说明。</div>',
      '  <div class="checkbox-group">',
      '    <input type="checkbox" id="cfgApprovalAutoApprove"' + (approval.auto_approve_tools === true ? " checked" : "") + " />",
      '    <label for="cfgApprovalAutoApprove">一键全同意工具调用</label>',
      "  </div>",
      '  <div class="field-info">开启后，需要审批的工具调用会自动放行，不再逐次弹出同意窗口。适合可信环境；涉及文件、浏览器、下载等操作时请确认你愿意让 PawMate 自行执行。</div>',
      "</section>",
      '<button class="save-btn" id="saveToolsBtn" data-i18n="saveToolsConfig">保存工具设置</button>',
    ].join("\n");

    $id("saveToolsBtn").addEventListener("click", function () {
      const a = (settingsState.config.approval = settingsState.config.approval || {});
      a.llm_explain_enabled = $id("cfgApprovalLlmExplain").checked;
      a.auto_approve_tools = $id("cfgApprovalAutoApprove").checked;
      saveConfig(__("settings_saved"));
    });
  }

  function renderSecuritySection(container, cfg) {
    const sec = cfg.security || {};
    const mode = sec.path_access_mode || sec.mode || "strict";
    const sandbox = sec.command_sandbox_mode || sec.sandbox_mode || "safe";
    const allowedRoots = Array.isArray(sec.allowed_roots) ? sec.allowed_roots : sec.allowed_path;
    const allowedRootsText = Array.isArray(allowedRoots) ? allowedRoots.join(", ") : (allowedRoots || "");

    const sandboxLabels = {
      safe: __("sandboxSafeLabel"), dev: __("sandboxDevLabel"),
      full: __("sandboxFullLabel"), off: __("sandboxOffLabel"),
    };
    const sandboxDescs = {
      safe: __("sandboxSafeDescription"), dev: __("sandboxDevDescription"),
      full: __("sandboxFullDescription"), off: __("sandboxOffDescription"),
    };

    container.innerHTML = [
      '<h3 data-i18n="sectionSecurity">安全</h3>',
      '<div class="checkbox-group">',
      '  <input type="checkbox" id="cfgStrictMode"' + (mode === "strict" ? " checked" : "") + " />",
      '  <label for="cfgStrictMode" data-i18n="strictSecurityMode">严格安全模式</label>',
      "</div>",
      '<div class="field-group"><label data-i18n="allowedRootDirectory">允许访问的根目录</label>',
      '  <input type="text" id="cfgAllowedPath" value="' +
        esc(allowedRootsText) +
        '" /></div>',
      _fieldSelect("selSandbox", "cfgSandboxMode", __("sandboxMode"), sandbox, sandboxLabels),
      '<div id="sandboxDescription" class="field-info sandbox-desc">' + (sandboxDescs[sandbox] || "") + "</div>",
      '<div id="sandboxFullWarning"' +
        (sandbox !== "full" && sandbox !== "off" ? ' style="display:none"' : "") +
        ' class="field-info warning">' +
        (sandbox === "off" ? __("sandboxOffDescription") : __("fullWarning")) +
        "</div>",
      '<button class="save-btn" id="saveSecurityBtn" data-i18n="saveSecurityConfig">保存安全配置</button>',
    ].join("\n");

    wireCustomSelect("selSandbox", "cfgSandboxMode");

    // Sandbox value change → update description + warning
    document.querySelectorAll("#selSandbox .custom-select-opt").forEach(function (o) {
      o.addEventListener("click", function () {
        const val = o.dataset.value;
        if (val === "off") {
          _showSandboxOffConfirm(function (confirmed) {
            if (confirmed) {
              $id("cfgSandboxMode").value = "off";
              _updateSandboxUI("off");
            } else {
              // Revert
              let prev = $id("cfgSandboxMode").value;
              if (prev === "off") prev = "safe";
              $id("cfgSandboxMode").value = prev;
              const label = sandboxLabels[prev] || prev;
              document.querySelector("#selSandbox .custom-select-label").textContent = label;
              document.querySelectorAll("#selSandbox .custom-select-opt").forEach(function (opt) {
                opt.classList.toggle("active", opt.dataset.value === prev);
              });
            }
          });
        } else {
          _updateSandboxUI(val);
        }
      });
    });

    $id("saveSecurityBtn").addEventListener("click", function () {
      const s = (settingsState.config.security = settingsState.config.security || {});
      s.path_access_mode = $id("cfgStrictMode").checked ? "strict" : "performance";
      s.command_sandbox_mode = $id("cfgSandboxMode").value;
      const paths = $id("cfgAllowedPath").value.split(",").map((x) => x.trim()).filter(Boolean);
      s.allowed_roots = paths.length ? paths : undefined;
      s.mode = s.path_access_mode;
      s.sandbox_mode = s.command_sandbox_mode;
      s.allowed_path = s.allowed_roots;
      saveConfig(__("settings_saved"));
    });
  }

  function renderLogsSection(container) {
    container.innerHTML = [
      '<div class="logs-panel">',
      '  <div class="logs-head">',
      '    <div>',
      '      <h3>日志</h3>',
      '      <p class="field-hint">记录 PawMate 操作、模型调用状态、工具执行和 I/O 输出。</p>',
      '    </div>',
      '    <div class="logs-actions">',
      '      <button class="memory-refresh-button" id="logsRefreshBtn">刷新</button>',
      '      <button class="memory-refresh-button logs-clear" id="logsClearBtn">清空</button>',
      '    </div>',
      '  </div>',
      '  <div class="logs-meta" id="logsMeta">加载中...</div>',
      '  <pre class="logs-viewer" id="logsViewer">加载中...</pre>',
      '</div>',
    ].join("\n");

    $id("logsRefreshBtn").addEventListener("click", loadLogs);
    $id("logsClearBtn").addEventListener("click", function () {
      const cb = getConfigBridge();
      if (!cb || typeof cb.clearAppLogs !== "function") return;
      cb.clearAppLogs(function () {
        loadLogs();
      });
    });
    loadLogs();
  }

  function loadLogs() {
    const meta = $id("logsMeta");
    const viewer = $id("logsViewer");
    const cb = getConfigBridge();
    if (!meta || !viewer) return;
    if (!cb || typeof cb.getAppLogs !== "function") {
      meta.textContent = "日志接口未就绪";
      viewer.textContent = "";
      return;
    }
    cb.getAppLogs("500", function (raw) {
      let data = {};
      try { data = JSON.parse(raw || "{}"); }
      catch (e) { data = { error: "日志解析失败" }; }
      if (data.error) {
        meta.textContent = data.error;
        viewer.textContent = "";
        return;
      }
      const size = typeof data.size === "number" ? Math.round(data.size / 1024) + " KB" : "-";
      meta.textContent = (data.path || "") + " · " + size + (data.modified_label ? " · " + data.modified_label : "");
      viewer.textContent = data.text || "暂无日志";
      viewer.scrollTop = viewer.scrollHeight;
    });
  }

  function _updateSandboxUI(val) {
    $id("cfgSandboxMode").value = val;
    const labels = { safe: __("sandboxSafeLabel"), dev: __("sandboxDevLabel"), full: __("sandboxFullLabel"), off: __("sandboxOffLabel") };
    const descs = { safe: __("sandboxSafeDescription"), dev: __("sandboxDevDescription"), full: __("sandboxFullDescription"), off: __("sandboxOffDescription") };
    const labelEl = document.querySelector("#selSandbox .custom-select-label");
    if (labelEl) labelEl.textContent = labels[val] || val;
    const descEl = $id("sandboxDescription");
    if (descEl) descEl.textContent = descs[val] || "";
    const warnEl = $id("sandboxFullWarning");
    if (warnEl) {
      warnEl.style.display = val === "full" || val === "off" ? "" : "none";
      warnEl.textContent = val === "off" ? __("sandboxOffDescription") : __("fullWarning");
    }
  }

  function _showSandboxOffConfirm(callback) {
    const overlay = $id("sandboxConfirmOverlay");
    const modal = $id("sandboxConfirmModal");
    if (!overlay || !modal) return;
    overlay.classList.add("open");
    modal.classList.add("open");
    $id("sandboxConfirmCancel").onclick = function () {
      overlay.classList.remove("open");
      modal.classList.remove("open");
      if (callback) callback(false);
    };
    $id("sandboxConfirmConfirm").onclick = function () {
      overlay.classList.remove("open");
      modal.classList.remove("open");
      if (callback) callback(true);
    };
  }

  function renderPlaceholder(section, container) {
    const labels = {
      memory: __("memory_coming"),
      logs: __("logs_coming"),
      about: "PawMate AI — Web UI v" + (window.__PAWMATE_WEB_VERSION__ || "?"),
    };
    const icons = { memory: "\ud83d\udcda", logs: "\ud83d\udccb", about: "\u2139" };
    container.innerHTML = [
      '<div class="placeholder-page">',
      '  <div class="ph-icon">' + (icons[section] || "?") + "</div>",
      "  <div>" + (labels[section] || section) + "</div>",
      "</div>",
    ].join("\n");
  }

  // ================================================================
  // Save & Toast
  // ================================================================

  function saveConfig(msg) {
    const cb = getConfigBridge();
    if (!cb) {
      showToast("configBridge not available", "error");
      return;
    }
    try {
      cb.saveConfig(JSON.stringify(settingsState.config, null, 2));
      showToast(msg || __("settings_saved"), "success");
    } catch (e) {
      showToast("Save failed: " + e, "error");
    }
  }

  function showToast(msg, type) {
    const toast = $id("settingsToast");
    if (!toast) return;
    toast.textContent = msg;
    toast.className = "settings-toast " + (type || "success") + " show";
    setTimeout(function () {
      toast.classList.remove("show");
    }, 2500);
  }

  // ================================================================
  // HTML helpers
  // ================================================================

  function _fieldSelect(rootId, hiddenId, label, currentValue, options, disabled) {
    const keys = Object.keys(options);
    const currentLabel = options[currentValue] || currentValue;
    const disabledAttr = disabled ? " disabled" : "";
    return [
      '<div class="field-group"><label>' + label + "</label>",
      '  <div class="custom-select" id="' + rootId + '">',
      '    <button type="button" class="custom-select-btn"' + disabledAttr + ">",
      '      <span class="custom-select-label">' + esc(currentLabel) + "</span>",
      '      <svg viewBox="0 0 24 24" width="16" height="16"><path d="M7 10l5 5 5-5z"/></svg>',
      "    </button>",
      '    <div class="custom-select-menu">',
      keys
        .map(
          (k) =>
            '      <button type="button" class="custom-select-opt' +
            (k === currentValue ? " active" : "") +
            '" data-value="' + k + '"' + disabledAttr + ">" + esc(options[k]) + "</button>"
        )
        .join("\n"),
      "    </div>",
      '    <input type="hidden" id="' + hiddenId + '" value="' + esc(currentValue) + '" />',
      "  </div></div>",
    ].join("\n");
  }

  // ================================================================
  // Event binding (call once from app.js)
  // ================================================================

  function bindEvents(settingsBtn) {
    if (window.PawPanelManager) window.PawPanelManager.register("settings", window.PawSettings);
    if (settingsBtn) {
      settingsBtn.addEventListener("pointerdown", function (e) { e.stopPropagation(); });
      settingsBtn.addEventListener("mousedown", function (e) { e.stopPropagation(); });
      settingsBtn.addEventListener("click", function (e) {
        e.stopPropagation();
        toggle();
      });
    }
    const closeBtn = $id("closeSettingsBtn");
    closeBtn.addEventListener("pointerdown", function (e) { e.stopPropagation(); });
    closeBtn.addEventListener("mousedown", function (e) { e.stopPropagation(); });
    closeBtn.addEventListener("click", function (e) {
      e.preventDefault();
      e.stopPropagation();
      close();
    });
    $id("settingsOverlay").addEventListener("click", close);
    $id("settingsNav").addEventListener("click", function (e) {
      const btn = e.target.closest(".nav-btn");
      if (btn) switchSection(btn.getAttribute("data-section"));
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && settingsState.isOpen) close();
    });
    // Global dropdown close
    document.addEventListener("click", function () {
      document.querySelectorAll(".custom-select.open").forEach(function (s) {
        s.classList.remove("open");
      });
    });
  }

  /* ── Public API ────────────────────────────────── */
  return {
    open,
    openSection,
    close,
    toggle,
    bindEvents,
    showToast,
    isOpen: () => settingsState.isOpen,
    getState: () => settingsState,
    browserLabels: () => ({
      attachMode: { ...ATTACH_MODE_LABELS },
      profile: { ...PROFILE_LABELS },
      automationLevel: { ...AUTOMATION_LEVEL_LABELS },
    }),
    labelForStorage,
    storageForLabel,
    normalizedAutomationLevel,
  };
})();
