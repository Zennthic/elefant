/* Shared theme bootstrap for every Elefant page.
 *
 * Load it in <head>, BEFORE any stylesheet, so the theme is applied before
 * first paint and there's no flash of the wrong one.
 *
 * CANONICAL COPY: editor/static/theme.js. memory-ui/static/theme.js is a
 * mirror written by tools/build_ui_assets.py — edit this one and rerun it.
 *
 * Two independent preferences, both written to localStorage by the SPA:
 *   elefant-theme    dark | light | auto   (auto follows the OS)
 *   elefant-palette  indigo | forest | …   (ids in palettes.json)
 * These pages are same-origin with the SPA — including when embedded as an
 * iframe (Settings, Platform Admin) — so they read the values directly, and
 * the `storage` event restyles an embedded page live when the user changes
 * either one in the parent frame.
 *
 * Sets on <html>:
 *   data-theme="dark|light"   the RESOLVED mode; what appearance.css keys off
 *   data-theme-pref           the preference, including "auto"
 *   data-palette              the palette id
 */
(function () {
  var THEME_KEY = 'elefant-theme';
  var PALETTE_KEY = 'elefant-palette';
  var DEFAULT_PALETTE = 'indigo';
  var mq = window.matchMedia('(prefers-color-scheme: light)');

  function read(key) {
    try { return localStorage.getItem(key); } catch (e) { return null; }  // private mode / blocked storage
  }

  function preference() {
    var v = read(THEME_KEY);
    return (v === 'light' || v === 'auto' || v === 'dark') ? v : 'dark';  // default: the Zennthic theme
  }

  // Not validated against the palette list: that lives in palettes.json and
  // this runs before anything could fetch it. It does not need to be — an id
  // with no matching block in appearance.css simply falls through to the
  // default palette's tokens. The shape check keeps a hostile stored value
  // from reaching the attribute as anything but a plain word.
  function palette() {
    var v = read(PALETTE_KEY);
    return (v && /^[a-z][a-z0-9-]{0,23}$/.test(v)) ? v : DEFAULT_PALETTE;
  }

  function apply() {
    var pref = preference();
    var resolved = pref === 'auto' ? (mq.matches ? 'light' : 'dark') : pref;
    var root = document.documentElement;
    root.setAttribute('data-theme', resolved);
    root.setAttribute('data-theme-pref', pref);
    root.setAttribute('data-palette', palette());
  }

  apply();

  // Fired in OTHER same-origin contexts when the SPA writes a preference —
  // i.e. exactly the case where this page is the embedded Settings iframe.
  window.addEventListener('storage', function (e) {
    if (!e || e.key === null || e.key === THEME_KEY || e.key === PALETTE_KEY) apply();
  });

  // Keep "auto" honest if the OS flips to night mode with the page open.
  if (mq.addEventListener) {
    mq.addEventListener('change', function () {
      if (preference() === 'auto') apply();
    });
  }
})();
