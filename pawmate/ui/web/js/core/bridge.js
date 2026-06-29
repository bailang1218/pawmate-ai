/**
 * PawMate WebBridge Client — unified QWebChannel wrapper.
 */
(function () { "use strict";
  function parseResponse(raw) {
    if (typeof raw !== 'string') return raw||{};
    try { return JSON.parse(raw); }
    catch(e) { return {ok:false, error:{code:'parse', message:'Invalid response'}, raw:raw}; }
  }
  function call(method /*, ...args */) {
    var args = Array.prototype.slice.call(arguments, 1);
    var bridge = (window._state && window._state.bridge) || null;
    return new Promise(function (resolve) {
      if (!bridge || typeof bridge[method] !== 'function') {
        resolve({ok:false, error:{code:'no_bridge', message:'Bridge not available: '+method}});
        return;
      }
      try {
        var callArgs = args.concat([function(raw) { resolve(parseResponse(raw)); }]);
        bridge[method].apply(bridge, callArgs);
      } catch(e) {
        resolve({ok:false, error:{code:'exception', message:(e&&e.message)||String(e)}});
      }
    });
  }
  function isReady() {
    var bridge = (window._state && window._state.bridge) || null;
    return !!bridge;
  }
  window.PawMateBridge = { call:call, parseResponse:parseResponse, isReady:isReady };
})();
