// The card description stopped arguing with what is typed into it.
//
// Cards started as post-its and became fully-formed issue tracking. The field
// had four things left over from the first life: a running character count, a
// tip saying a card is not a document, a placeholder saying the same, and a
// four-line box. On the board a tile carried the matching "long" badge and a
// fade over the body.
//
// All of that is gone. What is asserted here is mostly ABSENCE, which is the
// awkward kind of test — so each check also names the thing that would bring it
// back, and the two positive requirements (a bigger box, a draggable corner)
// are checked against the stylesheet, since jsdom does no layout.

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { JSDOM } from 'jsdom';

const __dirname = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(__dirname, '../static/boards.js'), 'utf8');
const css = readFileSync(join(__dirname, '../static/boards.css'), 'utf8');

let ok = true;
const fail = (m) => { console.error('❌ ' + m); ok = false; };
const pass = (m) => console.log('✅ ' + m);
const check = (cond, good, bad) => cond ? pass(good) : fail(bad);

// ── Nothing counts ────────────────────────────────────────────────────────

check(!/updateCount|updateEditCount/.test(src),
  'the character counters are gone',
  'updateCount()/updateEditCount() still exist — the readout is still wired');
check(!/b-char-count/.test(src) && !/b-char-count/.test(css),
  'and so is the element they wrote into',
  'the b-char-count element or its styling survives');
check(!/\bNUDGE\b/.test(src),
  'the board no longer carries a nudge threshold at all',
  'NUDGE is still read in boards.js — something can still measure the body');
check(!/nudge_chars/.test(src),
  'and no longer asks the server for one',
  'boards.js still reads nudge_chars from /columns');

// ── Nothing nags ──────────────────────────────────────────────────────────

check(!/A card is a post-it, not a document/.test(src),
  'the post-it tip below the box is gone',
  'the "a card is a post-it" tip is still rendered');
check(!/Outgrows the sticky/.test(src),
  'and so is the placeholder that said the same thing',
  'the description placeholder still argues about length');
check(!/b-over-badge|over-nudge/.test(src) && !/b-over-badge|over-nudge/.test(css),
  'the "long" badge and the faded tile body are gone with it',
  'the board still marks a card as too long');

// ── The box ───────────────────────────────────────────────────────────────

const editor = (css.match(/#boards-root textarea\.b-md-editor\s*\{[^}]*\}/) || [''])[0];
check(/resize:\s*vertical/.test(editor),
  'the description box has a corner to drag',
  `the editor rule does not allow resizing: ${editor}`);
check(!/resize:\s*both/.test(editor),
  'vertically only — dragging it wider would push it out of the modal grid',
  'the box resizes horizontally and will break the two-column modal');

const minH = (editor.match(/min-height:\s*(\d+)px/) || [])[1];
check(minH && Number(minH) >= 300,
  `the box starts big enough to write in (${minH}px)`,
  `the description box is still only ${minH}px tall`);

const preview = (css.match(/#boards-root \.b-md-preview\s*\{[^}]*\}/) || [''])[0];
const prevH = (preview.match(/min-height:\s*(\d+)px/) || [])[1];
check(prevH === minH,
  'and Preview stands exactly where Write stands, so switching moves nothing',
  `Write is ${minH}px and Preview is ${prevH}px — switching will jump the modal`);

// The quick-add modal is 460px wide and scrolls. Giving it the open card's
// height would push Labels, Repo and the people pickers below the fold on the
// one flow whose whole point is being fast — so it is sized for its own
// context, and still draggable when somebody wants more room there.
const quick = (css.match(/#boards-root textarea\.b-quick-editor\s*\{[^}]*\}/) || [''])[0];
const quickH = Number((quick.match(/min-height:\s*(\d+)px/) || [])[1]);
check(quick && /resize:\s*vertical/.test(quick),
  'the quick-add box is draggable too',
  `the quick-add editor rule is missing or fixed-height: ${quick}`);
check(quickH > 70 && quickH < Number(minH),
  `the quick-add box is bigger than a plain field but shorter than the open card's (${quickH}px)`,
  `the quick-add box is ${quickH}px against the open card's ${minH}px`);
check(/id="b-cm-body" class="b-quick-editor"/.test(src),
  'and the New card modal uses it',
  'the New card modal still uses the open card\'s editor sizing');

// ── Both modals still work ────────────────────────────────────────────────
// The counters were called from four places; removing a call site and leaving
// a caller is how this lands as a TypeError on opening a card.

const dom = new JSDOM('<!doctype html><html><body><div id="boards-root"></div></body></html>', {
  runScripts: 'dangerously', pretendToBeVisual: true, url: 'http://localhost/',
});
const { window } = dom;
const cols = {
  columns: [{ id: 'c1', name: 'Backlog', stage: 'backlog', position: 1, color: null }],
  stages: ['backlog', 'done'],
};
const CARD = { slug: 'a-card', title: 'A Card', body: 'x'.repeat(5000), column_id: 'c1',
               position: 1, source: 'ui', labels: [], repo: '', reporter: '',
               participants: [], closed: false,
               created_at: new Date().toISOString(), updated_at: new Date().toISOString() };
window.fetch = async (url) => {
  const u = String(url);
  return { ok: true, status: 200, json: async () => {
    if (u.includes('/columns')) return cols;
    if (u.includes('/labels')) return { labels: [], reserved: [] };
    if (u.includes('/people')) return { me: 'user:jon', people: [] };
    if (/\/cards\/[^/?]+/.test(u) && !u.includes('/cards?')) {
      return { card: CARD, people: {}, comments: [],
               relationships: { blocked_by: [], blocks: [], related: [] } };
    }
    if (u.includes('/cards')) return { cards: [CARD], people: {} };
    return {};
  } };
};
window.Sortable = { create: () => ({ destroy() {} }), destroy() {} };
const errors = [];
window.addEventListener('error', e => errors.push(e.message));

const ms = window.document.createElement('script');
ms.textContent = readFileSync(join(__dirname, '../static/marked.min.js'), 'utf8');
window.document.body.appendChild(ms);
const s = window.document.createElement('script');
s.textContent = src;
window.document.body.appendChild(s);

await window.Boards.init();
await new Promise(r => setTimeout(r, 50));

const root = window.document.getElementById('boards-root');

try {
  window.Boards.openCardModal('c1');
  await new Promise(r => setTimeout(r, 20));
  pass('the New card modal opens with no counter to update');
} catch (e) { fail('opening the New card modal threw: ' + e.message); }

check(!root.querySelector('#b-cm-count') && !root.querySelector('#b-ec-count'),
  'neither modal has a counter element left behind',
  'a counter element is still in the markup, now never updated');

try {
  await window.Boards.cardClicked('a-card');
  await new Promise(r => setTimeout(r, 30));
  const body = root.querySelector('#b-ec-body');
  check(body && body.value.length === 5000,
    'a 5,000-character description opens intact',
    `the description came back as ${body && body.value.length} characters`);
} catch (e) { fail('opening a card with a long description threw: ' + e.message); }

check(errors.length === 0,
  'and nothing threw while doing it',
  `uncaught errors: ${errors.join(' | ')}`);

// The tile must render a long body without reaching for a threshold.
const tile = root.querySelector('.b-card');
check(tile && !tile.className.includes('over-nudge'),
  'a long card renders on the board with no length class',
  `the tile carries: ${tile && tile.className}`);

console.log(ok ? '\ncard-description: all checks passed'
               : '\ncard-description: FAILED');
process.exit(ok ? 0 : 1);
