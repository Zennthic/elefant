// The wiki page's HEADER in read mode (index.html: renderFileView).
//
// Four things landed together and all four live in the same few lines, so a
// refactor of the action bar can quietly undo any of them:
//   1. the bar is PINNED, so Edit/History stay reachable down a long page,
//   2. the rich/classic editor TOGGLE is gone (rich is simply the editor),
//   3. the bar states when the page was last edited and by whom,
//   4. a locked page says so, and offers Unlock instead of Edit.
//
// NOTE ON SCOPE: index.html is a ~5k-line SPA that expects a live backend, a
// session and a TipTap bundle, so it is not executed here. What is checked is
// structural and readable straight off the source — plus the two pure helpers
// (fmtEditStamp, isWikiPageLocked), which are evaluated in a jsdom window.

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { JSDOM } from 'jsdom';

const __dirname = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(__dirname, '../static/index.html'), 'utf8');

let ok = true;
const fail = (m) => { console.error('❌ ' + m); ok = false; };
const pass = (m) => console.log('✅ ' + m);
const check = (cond, good, bad) => cond ? pass(good) : fail(bad);

// ── 1. The bar is pinned ──────────────────────────────────────────────────

const fn = (src.match(/function renderFileView\(\)\s*\{[\s\S]*?\n\}/) || [''])[0];
if (!fn) fail('renderFileView() not found in index.html');

check(/wiki-sticky-bar/.test(fn),
  'read mode marks the action bar as the pinned one',
  'the action bar carries no sticky class — it will scroll away');

// The rule has to target the bar INSIDE the scrolling viewer; a bare
// `.file-path-bar { position: sticky }` would also pin the editor's bar and
// the memories tab's, which are in non-scrolling containers.
const stickyRule = (src.match(/#wiki-viewer\s*>\s*\.wiki-sticky-bar\s*\{[\s\S]*?\}/) || [''])[0];
check(/position:\s*sticky/.test(stickyRule),
  'the pinned bar is position:sticky inside the wiki viewer',
  'no sticky rule scoped to #wiki-viewer > .wiki-sticky-bar');
// Nothing may sit between the bar and the pane's top edge, or page text
// scrolls into view above it. A negative `top` cancelling the
// viewer's padding still left a few pixels of seam in real browsers, so a
// viewer holding the bar drops its top padding and the bar sticks at a plain
// `top: 0` — on desktop and in the phone layout alike.
const px = (rule, prop) => {
  const m = rule.match(new RegExp('(?:^|[\\s;{])' + prop + ':\\s*(-?\\d+)px'));
  return m ? Number(m[1]) : null;
};
const num = (rule, prop) => {
  const m = rule.match(new RegExp('(?:^|[\\s;{])' + prop + ':\\s*(-?\\d+)(?:px)?\\s*[;}]'));
  return m ? Number(m[1]) : null;
};
check(num(stickyRule, 'top') === 0,
  'it sticks at top: 0, not at a negative offset',
  `the sticky bar's top is ${num(stickyRule, 'top')}, not 0 — text can show above it`);
const marginTop = (stickyRule.match(/(?:^|[\s;{])margin:\s*(-?\d+)(?:px)?[\s;]/) || [])[1];
check(marginTop === '0',
  'and does not reach up into padding that is no longer there',
  `the sticky bar's top margin is ${marginTop}, not 0`);
// `#tab-wiki` in the selector outranks the phone layout's later
// `#tab-wiki .file-viewer` padding, which would otherwise win the tie.
const dropRule = (src.match(/#tab-wiki\s+#wiki-viewer:has\(>\s*\.wiki-sticky-bar\)\s*\{[^}]*\}/) || [''])[0];
check(num(dropRule, 'padding-top') === 0,
  'a viewer holding the bar drops its top padding, so no strip is left above it',
  'no `#tab-wiki #wiki-viewer:has(> .wiki-sticky-bar) { padding-top: 0 }` rule — text will show above the bar');

const mobile = (src.match(/@media \(max-width: 768px\)\s*\{[\s\S]*?\n    \}/) || [''])[0];
const mobilePad = px((mobile.match(/#tab-wiki \.file-viewer\s*\{[^}]*\}/) || [''])[0], 'padding-top');
const mobileBar = (mobile.match(/#wiki-viewer\s*>\s*\.wiki-sticky-bar\s*\{[^}]*\}/) || [''])[0];
check(mobilePad !== null && px(mobileBar, 'padding-top') === mobilePad
      && !/(?:^|[\s;{])(top|margin-top):/.test(mobileBar),
  `on a phone the bar carries the toggle's ${mobilePad}px itself and still sticks at top: 0`,
  `on a phone the sticky bar (${mobileBar.replace(/\s+/g, ' ')}) does not take over the viewer's ${mobilePad}px top padding, or re-offsets itself`);
check(/background:\s*var\(--/.test(stickyRule),
  'it paints an opaque background so content scrolls under it, not through it',
  'the sticky bar has no background — page text will show through it');
check(/z-index:\s*\d+/.test(stickyRule),
  'and sits above the page body',
  'the sticky bar declares no z-index');

// ── 2. The editor toggle is gone ──────────────────────────────────────────

check(!/cycleWikiEditor/.test(src),
  'the rich/classic toggle handler is gone',
  'cycleWikiEditor() still exists — the toggle was not removed');
check(!/wikiEditorPref/.test(src),
  'and so is the stored editor preference',
  'wikiEditorPref() still exists — the opt-out preference survived');
check(!/elefant_wiki_editor/.test(src),
  'nothing reads the old localStorage key any more',
  'the elefant_wiki_editor localStorage key is still referenced');

// The classic editor must SURVIVE as the fallback — removing the choice is not
// the same as removing the code path that handles a non-markdown file or a
// bundle that failed to load.
const start = (src.match(/function startInlineEdit\(\)\s*\{[\s\S]*?\n\}/) || [''])[0];
check(/startRichEdit\(\)/.test(start) && /startClassicEdit\(\)/.test(start),
  'Edit still dispatches to rich or, as a fallback, classic',
  'startInlineEdit() no longer offers both paths');
check(/wikiRichAvailable\(\)/.test(start),
  'and the fallback triggers when the rich bundle is missing',
  'startInlineEdit() assumes the rich bundle loaded');

// ── 3. Last edit is shown ─────────────────────────────────────────────────

check(/renderLastEditLine\(\)/.test(fn),
  'the header renders a last-edited line',
  'read mode shows no last-edit information');

const lastEdit = (src.match(/function renderLastEditLine\(\)\s*\{[\s\S]*?\n\}/) || [''])[0];
check(/last_edit/.test(lastEdit),
  'sourced from the last_edit the file endpoint returns',
  'renderLastEditLine() does not read the served last_edit');
check(/esc\(/.test(lastEdit),
  'and the editor name is escaped before it reaches innerHTML',
  'renderLastEditLine() interpolates a name unescaped');
check(/if \(!le \|\| !le\.at\) return ''/.test(lastEdit),
  'a page with no recorded revision shows no line rather than a wrong date',
  'renderLastEditLine() does not handle a page with no revision history');

// ── 4. A locked page ──────────────────────────────────────────────────────

check(/isWikiPageLocked\(\)/.test(fn),
  'read mode asks whether the page is locked',
  'read mode never checks the lock');
check(/renderLockBanner\(\)/.test(fn),
  'a locked page gets a banner explaining what the lock does',
  'no lock banner is rendered');
check(/toggleWikiLock\(\)/.test(fn),
  'the header offers a lock/unlock control',
  'there is no way to lock a page from the page');
check(/disabled/.test(fn) && /locked \?/.test(fn),
  'and Edit is disabled while the page is locked',
  'Edit stays live on a locked page — the save would just 423');

const toggle = (src.match(/async function toggleWikiLock\(\)\s*\{[\s\S]*?\n\}/) || [''])[0];
check(/'\/api\/wiki\/lock'/.test(toggle),
  'lock/unlock posts to the web-only lock endpoint',
  'toggleWikiLock() does not call /api/wiki/lock');
check(/renderFileView\(\)/.test(toggle),
  'and repaints from the state the server returned',
  'toggleWikiLock() does not re-render after the server answers');

const banner = (src.match(/function renderLockBanner\(\)\s*\{[\s\S]*?\n\}/) || [''])[0];
check(/esc\(lk\.locked_by_name\)/.test(banner) && /esc\(lk\.reason\)/.test(banner),
  'the banner escapes the holder name and the reason',
  'the lock banner interpolates user-supplied text unescaped');

// A typed refusal (423 Locked) has to reach the user as its sentence, not as
// a JSON envelope — that message is the whole explanation.
check(/async function errDetail\(res\)/.test(src),
  'API errors are unwrapped to their detail sentence',
  'no errDetail() helper — a 423 would surface as raw JSON');
check(/throw new Error\(await errDetail\(res\)\)/.test(src),
  'and apiFetch throws that sentence',
  'apiFetch still throws the raw response body');

// A save must refresh the attribution it just changed, or the header keeps
// naming the previous editor until the page is reloaded.
for (const saver of ['saveRichEdit', 'saveInlineEdit']) {
  const body = (src.match(new RegExp(`async function ${saver}\\(\\)\\s*\\{[\\s\\S]*?\\n\\}`)) || [''])[0];
  check(/currentFileMeta\.last_edit = saved\.last_edit/.test(body),
    `${saver}() refreshes the last-edit line from the save response`,
    `${saver}() leaves a stale editor name in the header after saving`);
}

// ── 5. The pure helpers actually run ──────────────────────────────────────

const dom = new JSDOM('<!doctype html><html><body></body></html>');
const win = dom.window;

const helpers = ['fmtEditStamp', 'isWikiPageLocked']
  .map(n => (src.match(new RegExp(`function ${n}\\([^)]*\\)\\s*\\{[\\s\\S]*?\\n\\}`)) || [''])[0])
  .join('\n');

try {
  const run = new win.Function('currentFileMeta', `${helpers}\nreturn { fmtEditStamp, isWikiPageLocked };`);

  const api = run({ lock: { locked: false } });
  check(api.isWikiPageLocked() === false,
    'an unlocked page reads as unlocked',
    'isWikiPageLocked() is true for an unlocked page');

  const api2 = run({ lock: { locked: true, locked_by_name: 'Jon' } });
  check(api2.isWikiPageLocked() === true,
    'a locked page reads as locked',
    'isWikiPageLocked() is false for a locked page');

  const api3 = run({});
  check(api3.isWikiPageLocked() === false,
    'and a page whose meta never arrived is not treated as locked',
    'missing lock meta throws or reads as locked');

  // A bad timestamp must not paint "Invalid Date" into the header.
  check(api.fmtEditStamp('not-a-date') === '',
    'an unparseable timestamp renders nothing, not "Invalid Date"',
    'fmtEditStamp() lets an invalid date through');

  const recent = api.fmtEditStamp(new Date(Date.now() - 5 * 60 * 1000).toISOString());
  check(/\(5m ago\)$/.test(recent),
    'a recent edit carries a relative hint',
    `a 5-minute-old edit rendered as "${recent}"`);
  check(recent.length > '(5m ago)'.length,
    'alongside the absolute date, so the line can still be dated',
    'the stamp is relative only — an old page could not be dated');

  const old = api.fmtEditStamp(new Date(Date.now() - 400 * 86400 * 1000).toISOString());
  check(!/ago\)/.test(old) && old.length > 0,
    'a year-old edit drops the relative hint and states the date',
    `a 400-day-old edit rendered as "${old}"`);
} catch (e) {
  fail('helper evaluation threw: ' + e.message);
}

console.log(ok ? '\nwiki-page-header: all checks passed'
               : '\nwiki-page-header: FAILED');
process.exit(ok ? 0 : 1);
