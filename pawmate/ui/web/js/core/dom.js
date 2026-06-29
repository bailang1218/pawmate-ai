/**
 * PawMate DOM utilities.
 * Safe wrappers around document.getElementById etc.
 */
(function () { "use strict";
  function qs(sel, root) { return (root||document).querySelector(sel); }
  function qsa(sel, root) { return (root||document).querySelectorAll(sel); }
  function el(id) { return document.getElementById(id); }
  function show(id) { var e=typeof id==='string'?document.getElementById(id):id; if(e)e.style.display=''; }
  function hide(id) { var e=typeof id==='string'?document.getElementById(id):id; if(e)e.style.display='none'; }
  function escapeHtml(v){return String(v??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');}
  function setText(id,t){var e=typeof id==='string'?document.getElementById(id):id;if(e)e.textContent=t;}
  window.PawMateDom={qs,qsa,el,show,hide,escapeHtml,setText};
})();
