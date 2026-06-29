/**
 * PawMate I18n — UI language translations.
 */
(function () { "use strict";
  var _lang = "zh-CN";
  function __(key) {
    var dicts = window.__PAWMATE_I18N;
    if (!dicts) return key;
    var d = dicts[_lang] || dicts["zh-CN"];
    return d[key] || key;
  }
  function applyI18n(root) {
    if (!root) root = document;
    root.querySelectorAll("[data-i18n]").forEach(function(el) {
      el.textContent = __(el.getAttribute("data-i18n"));
    });
  }
  function init(lang) { if (lang) _lang = lang; }
  function setLang(lang) { _lang = lang || "zh-CN"; }
  window.PawMateI18n = { __: __, applyI18n: applyI18n, init: init, setLang: setLang };
  window.__ = __;  // backward compat
})();
