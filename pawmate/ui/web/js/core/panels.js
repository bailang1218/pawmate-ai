/* =============================================================
   PawMate top-level panel coordinator
   Keeps Settings, Skills Hall and Memory Hall from stacking.
   ============================================================= */

window.PawPanelManager = (function () {
  "use strict";

  var apis = {};
  var activePanel = null;

  function register(name, api) {
    if (!name || !api) return;
    apis[name] = api;
  }

  function isOpen(name) {
    var api = apis[name];
    if (!api || typeof api.isOpen !== "function") return false;
    try { return !!api.isOpen(); }
    catch (e) { return false; }
  }

  function close(name) {
    var api = apis[name];
    if (!api || typeof api.close !== "function") return;
    if (!isOpen(name)) return;
    try { api.close({ fromPanelManager: true }); }
    catch (e) { console.error("[PanelManager] close failed:", name, e); }
  }

  function requestOpen(name) {
    Object.keys(apis).forEach(function (key) {
      if (key !== name) close(key);
    });
    activePanel = name;
  }

  function notifyClosed(name) {
    if (activePanel === name) activePanel = null;
  }

  function hasOpenPanel() {
    return Object.keys(apis).some(isOpen);
  }

  function setBodyLocked(locked) {
    document.body.style.overflow = locked ? "hidden" : "";
  }

  return {
    register: register,
    requestOpen: requestOpen,
    notifyClosed: notifyClosed,
    hasOpenPanel: hasOpenPanel,
    setBodyLocked: setBodyLocked,
    isOpen: isOpen,
  };
})();
