/* Runs before first paint (blocking, no inline script allowed by the page policy): applies a saved theme choice. */
(function () {
  'use strict';
  try {
    var t = window.localStorage.getItem('lead-desk-theme');
    if (t === 'dark' || t === 'light') document.documentElement.setAttribute('data-theme', t);
  } catch (e) { /* storage blocked: follow the system setting */ }
})();
