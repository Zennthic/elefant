// Closed is a COLUMN on the board, not a mode the board switches into.
//
// The old "Archived" button replaced the whole board with an archived-only view:
// the columns vanished, nothing was draggable, and you could not see a closed
// card next to the work it came from. Closed is now an ordinary-looking column
// pinned to the right, shown or hidden by a toggle that remembers itself, with
// the rest of the board live and normal beside it.
//
// The things that would silently undo that, and so are asserted here:
//   * a closed card leaking back into its original column (it keeps column_id),
//   * the Closed column becoming a drop target (dropping a card would then mean
//     "close it" — a decision, performed by a mis-aimed drag),
//   * the rest of the board losing its drag handling while Closed is shown,
//     which is exactly what the old mode did,
//   * the preference not persisting, making it a look rather than a way to work.
//
// boards.js is an IIFE over a live API, so it runs here in jsdom against a
// mocked fetch — the same arrangement boards.mjs uses.

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

const cols = {
  columns: [
    { id: 'c1', name: 'Up Next', stage: 'designed', position: 1, color: '#818CF8' },
    { id: 'c2', name: 'In Progress', stage: 'building', position: 2, color: null },
  ], stages: ['designed', 'building', 'shipped'], nudge_chars: 280,
};

// `done-thing` is closed but still carries c1 — a closed card keeps the column
// it came from, which is what lets it be reopened back into place, and what
// makes "filter it out of the columns" a thing the board has to do on purpose.
const CARDS = [
  { slug: 'open-thing', title: 'Open Thing', body: '', column_id: 'c1', position: 1,
    source: 'ui', labels: [], repo: '', reporter: '', participants: [], closed: false },
  { slug: 'done-thing', title: 'Done Thing', body: '', column_id: 'c1', position: 2,
    source: 'ui', labels: [], repo: '', reporter: '', participants: [],
    closed: true, closed_at: '2026-09-20T10:00:00+00:00' },
  { slug: 'older-done', title: 'Older Done', body: '', column_id: 'c2', position: 1,
    source: 'agent', labels: [], repo: '', reporter: '', participants: [],
    closed: true, closed_at: '2026-09-01T10:00:00+00:00' },
];

async function boot({ showClosed }) {
  const dom = new JSDOM('<!doctype html><html><body><div id="boards-root"></div></body></html>', {
    runScripts: 'dangerously', pretendToBeVisual: true, url: 'http://localhost/',
  });
  const { window } = dom;
  window.localStorage.setItem('elefant_board_show_closed', showClosed ? '1' : '0');

  const requests = [];
  const posts = [];
  window.fetch = async (url, opts) => {
    const u = String(url);
    const method = (opts && opts.method) || 'GET';
    if (method === 'POST' || method === 'PATCH') posts.push({ method, url: u });
    else requests.push(u);
    return {
      ok: true, status: 200,
      json: async () => {
        if (u.includes('/columns')) return cols;
        if (u.includes('/labels')) return { labels: [], reserved: [] };
        if (u.includes('/people')) return { me: 'user:jon', people: [] };
        if (/\/cards\/[^/?]+/.test(u) && !u.includes('/cards?')) {
          const slug = decodeURIComponent(u.split('/cards/')[1].split(/[?/]/)[0]);
          return { card: CARDS.find(c => c.slug === slug) || null, people: {},
                   comments: [], relationships: { blocked_by: [], blocks: [], related: [] } };
        }
        if (u.includes('/cards')) {
          // The server hides closed cards unless asked, so the fixture must too
          // — otherwise the test would pass with the parameter never sent.
          const wantClosed = new URL(u, 'http://localhost').searchParams.get('closed') === 'true';
          return { cards: wantClosed ? CARDS : CARDS.filter(c => !c.closed), people: {} };
        }
        return {};
      },
    };
  };

  const sortables = [];
  window.Sortable = {
    create: (el, opts) => { sortables.push({ el, opts }); return { destroy() {} }; },
    destroy() {},
  };

  const ms = window.document.createElement('script');
  ms.textContent = readFileSync(join(__dirname, '../static/marked.min.js'), 'utf8');
  window.document.body.appendChild(ms);

  const s = window.document.createElement('script');
  s.textContent = src;
  window.document.body.appendChild(s);

  await window.Boards.init();
  await new Promise(r => setTimeout(r, 50));
  return { window, sortables, requests, posts,
           root: window.document.getElementById('boards-root') };
}

// ── Hidden by default ─────────────────────────────────────────────────────
{
  const { root, requests } = await boot({ showClosed: false });
  const closedCol = root.querySelector('.b-column-closed');
  check(!closedCol, 'with the preference off there is no Closed column',
    'the Closed column renders even when it is switched off');

  const asked = requests.some(u =>
    new URL(u, 'http://localhost').searchParams.get('closed') === 'true');
  check(!asked, 'and closed cards are not even fetched',
    'the board fetches closed cards it is not going to show');

  const titles = [...root.querySelectorAll('.b-card-title')].map(e => e.textContent);
  check(titles.includes('Open Thing') && !titles.includes('Done Thing'),
    'the board shows the open card and not the closed one',
    `board rendered: ${titles.join(', ')}`);

  const toggle = root.querySelector('#b-closed-toggle');
  check(!!toggle && toggle.getAttribute('aria-pressed') === 'false',
    'the toggle reports itself as off',
    'the Closed toggle is missing or does not report its state');
}

// ── Shown ─────────────────────────────────────────────────────────────────
{
  const { root, sortables, window } = await boot({ showClosed: true });

  const columns = [...root.querySelectorAll('.b-column')];
  const closedCol = root.querySelector('.b-column-closed');
  check(!!closedCol, 'with the preference on the Closed column renders',
    'the Closed column does not render when it is switched on');
  check(columns[columns.length - 1] === closedCol,
    'and it is pinned to the right-hand end of the board',
    'the Closed column is not last');
  check(columns.length === 3,
    'the real columns are still there beside it',
    `expected 2 real columns + Closed, got ${columns.length}`);

  // The regression the old mode had: a closed card keeps its column_id, so the
  // board has to exclude it explicitly or it shows up twice.
  const upNext = root.querySelector('.b-column[data-column-id="c1"]');
  const upNextTitles = [...upNext.querySelectorAll('.b-card-title')].map(e => e.textContent);
  check(!upNextTitles.includes('Done Thing'),
    'a closed card does not leak back into the column it came from',
    `Up Next rendered a closed card: ${upNextTitles.join(', ')}`);
  check(upNextTitles.includes('Open Thing'),
    'while the open card in that column is untouched',
    'the open card vanished from its column');

  const closedTitles = [...closedCol.querySelectorAll('.b-card-title')].map(e => e.textContent);
  check(closedTitles.length === 2,
    'every closed card is in the Closed column',
    `Closed holds ${closedTitles.length} card(s): ${closedTitles.join(', ')}`);
  check(closedTitles[0] === 'Done Thing',
    'most recently closed first',
    `Closed is ordered: ${closedTitles.join(', ')}`);

  const count = closedCol.querySelector('.b-column-count');
  check(count && count.textContent === '2',
    'and the column counts them',
    `the Closed count reads "${count && count.textContent}"`);

  // Reopen, not Restore — and it is the only action a closed card offers.
  const btns = [...closedCol.querySelectorAll('button')].map(b => b.textContent.trim());
  check(btns.some(t => t === 'Reopen'),
    'a closed card offers Reopen',
    `the Closed column's buttons are: ${btns.join(' | ')}`);
  check(!closedCol.querySelector('.b-col-add'),
    'and the column takes no new cards',
    'the Closed column offers "+ Card"');

  // ── Dragging ──
  const cardLists = sortables.filter(s => s.opts && s.opts.group === 'cards');
  check(cardLists.length === 2,
    'the two real columns are drop targets',
    `expected 2 card lists registered with Sortable, got ${cardLists.length}`);
  check(!cardLists.some(s => s.el.classList.contains('b-closed-cards')),
    'and the Closed column is not one — a drag cannot close a card',
    'the Closed column was registered as a drop target');

  const board = sortables.find(s => s.opts && s.opts.draggable);
  check(!!board && /:not\(\.b-column-closed\)/.test(board.opts.draggable),
    'the Closed column is out of the column reorder',
    `the board Sortable draggable selector is "${board && board.opts.draggable}"`);
  check(!!board,
    'and the rest of the board keeps its drag handling while Closed is shown',
    'showing Closed disabled dragging on the whole board, as the old mode did');

  const toggle = root.querySelector('#b-closed-toggle');
  check(toggle.getAttribute('aria-pressed') === 'true' && toggle.classList.contains('active'),
    'the toggle reports itself as on',
    'the Closed toggle does not reflect that the column is shown');

  // ── The preference persists ──
  window.Boards.toggleClosed();
  await new Promise(r => setTimeout(r, 30));
  check(window.localStorage.getItem('elefant_board_show_closed') === '0',
    'hiding the column is remembered',
    'toggling Closed does not persist the preference');
  check(!root.querySelector('.b-column-closed'),
    'and the column goes away',
    'the Closed column stayed after being toggled off');
}

// ── The routes the UI calls ───────────────────────────────────────────────
{
  const { window, posts } = await boot({ showClosed: true });
  await window.Boards.reopenCard('done-thing');
  await new Promise(r => setTimeout(r, 30));
  check(posts.some(p => p.method === 'POST' && /\/cards\/done-thing\/reopen$/.test(p.url)),
    'Reopen posts to /reopen',
    `posts seen: ${posts.map(p => p.url).join(', ') || 'none'}`);
}

// ── Source-level guards ───────────────────────────────────────────────────

check(!/_showArchived/.test(src),
  'the archived board mode is gone from the source',
  '_showArchived still exists — the mode was not replaced, only renamed around');

check(/\/close`/.test(src) && /\/reopen`/.test(src),
  'the UI calls close and reopen, not archive and unarchive',
  'the UI still posts to the old archive routes');

check(/Close this card\?/.test(src),
  'the confirmation says close, not archive',
  'the close confirmation still says "Archive this card?"');

// The stream events keep their old names on purpose (they are plumbing between
// our services, not vocabulary a person reads) — so the board must still listen
// for them, or closing a card would stop updating an open board.
check(/'card\.archived'/.test(src) && /'card\.unarchived'/.test(src),
  'the board still subscribes to the (unrenamed) close/reopen stream events',
  'the board stopped listening for card.archived — an open board would go stale');

check(/\.b-column-closed/.test(css) && /\.b-closed-cards/.test(css),
  'the Closed column is styled',
  'boards.css has no rules for the Closed column');

console.log(ok ? '\nboard-closed-column: all checks passed'
               : '\nboard-closed-column: FAILED');
process.exit(ok ? 0 : 1);
