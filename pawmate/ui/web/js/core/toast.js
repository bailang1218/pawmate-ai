/**
 * PawMate Toast notifications.
 */
(function () { "use strict";
  function show(msg, type) {
    var el = document.getElementById('settingsToast');
    if (!el) return;
    el.textContent = msg;
    el.className = 'settings-toast ' + (type||'success') + ' show';
    setTimeout(function () { el.classList.remove('show'); }, 2500);
  }
  window.PawMateToast = { show: show, success: function(m){show(m,'success');}, error: function(m){show(m,'error');}, info: function(m){show(m,'info');} };
})();
