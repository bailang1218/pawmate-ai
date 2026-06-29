/**
 * PawMate Event delegation utilities.
 */
(function () { "use strict";
  function delegate(root, selector, event, handler) {
    var el = typeof root === 'string' ? document.getElementById(root) : root;
    if (!el) return;
    el.addEventListener(event, function(e) {
      var target = e.target.closest(selector);
      if (target) handler(target, e);
    });
  }
  function on(root, event, handler) {
    var el = typeof root === 'string' ? document.getElementById(root) : root;
    if (!el) return;
    el.addEventListener(event, handler);
  }
  window.PawMateEvents = { delegate:delegate, on:on };
})();
