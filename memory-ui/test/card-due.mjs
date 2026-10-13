// A card's due date: the picker on the open card, and the chip on its face.
//
// The ways this goes wrong are all rendering or wiring ways:
//   * the chip states the wrong day because "2026-11-27" was parsed as UTC
//     midnight, which is the 26th for everybody west of Greenwich,
//   * a finished card painted red for being "late",
//   * an impossible date (Feb 30) quietly rolled into March,
//   * the picker not saving, or saving the previous card's date,
//   * the Clear button leaving the field set, or not sending a clear.

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

const pad = n => String(n).padStart(2, '0');
const day = (offset) => {
  const d = new Date(); d.setDate(d.getDate() + offset);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
};

const cols = {
  columns: [
    { id: 'c1', name: 'Up Next', stage: 'designed', position: 1, color: null },
    { id: 'c2', name: 'Done', stage: 'done', position: 2, color: null },
  ],
  stages: ['designed', 'done'], nudge_chars: 280,
};
const base = { body: '', source: 'ui', labels: [], repo: '', reporter: '', participants: [] };
const CARDS = [
  { ...base, slug: 'late', title: 'Late', column_id: 'c1', stage: 'designed', position: 1, due: day(-3) },
  { ...base, slug: 'soon', title: 'Soon', column_id: 'c1', stage: 'designed', position: 2, due: day(2) },
  { ...base, slug: 'later', title: 'Later', column_id: 'c1', stage: 'designed', position: 3, due: day(40) },
  { ...base, slug: 'undated', title: 'Undated', column_id: 'c1', stage: 'designed', position: 4, due: null },
  { ...base, slug: 'done-late', title: 'Done late', column_id: 'c2', stage: 'done', position: 1, due: day(-3) },
];

const dom = new JSDOM('<!doctype html><html><body><div id="boards-root"></div></body></html>', {
  runScripts: 'dangerously', pretendToBeVisual: true, url: 'http://localhost/',
});
const { window } = dom;
const sent = [];
window.fetch = async (url, opts) => {
  const u = String(url);
  if (opts && opts.method && opts.method !== 'GET') sent.push({ url: u, method: opts.method, body: JSON.parse(opts.body || '{}') });
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
      if (u.includes('/cards')) return { cards: CARDS, people: {} };
      return {};
    },
  };
};
window.Sortable = { create: () => ({ destroy() {} }), destroy() {} };

const ms = window.document.createElement('script');
ms.textContent = readFileSync(join(__dirname, '../static/marked.min.js'), 'utf8');
window.document.body.appendChild(ms);
const s = window.document.createElement('script');
s.textContent = src;
window.document.body.appendChild(s);

await window.Boards.init();
await new Promise(r => setTimeout(r, 50));
const root = window.document.getElementById('boards-root');
const chip = slug => root.querySelector(`.b-card[data-slug="${slug}"] .b-card-due`);

// ── The chip on the card face ─────────────────────────────────────────────
check(chip('late') && chip('late').classList.contains('overdue') && /overdue/.test(chip('late').textContent),
  'a past due date shows an overdue chip that says so in words',
  `late card chip: ${chip('late')?.outerHTML}`);
check(chip('soon') && chip('soon').classList.contains('soon'),
  'a date within a week shows the due-soon state',
  `soon card chip: ${chip('soon')?.outerHTML}`);
check(chip('later') && chip('later').classList.contains('normal'),
  'a date further out shows a plain chip',
  `later card chip: ${chip('later')?.outerHTML}`);
check(!chip('undated'),
  'a card with no due date has no chip',
  'an undated card rendered a due chip');
check(chip('done-late') && !chip('done-late').classList.contains('overdue'),
  'a finished card is never painted overdue',
  `done card chip: ${chip('done-late')?.outerHTML}`);
check((chip('late')?.title || '').includes(day(-3)),
  'the chip title carries the full date',
  `chip title: ${chip('late')?.title}`);

// ── Day arithmetic, pinned to a fixed "today" ─────────────────────────────
const info = window.Boards._dueInfo;
const NOON = new Date(2026, 10, 27, 12, 0, 0).getTime();   // 27 Nov 2026, local
check(info('2026-11-27', 'designed', false, NOON).rel === 'due today',
  'a card due today is due today in the viewer\'s own calendar, not UTC\'s',
  `2026-11-27 on 27 Nov reads: ${info('2026-11-27', 'designed', false, NOON).rel}`);
check(info('2026-11-26', 'designed', false, NOON).rel === 'overdue by 1 day',
  'yesterday is overdue by one day',
  `got: ${info('2026-11-26', 'designed', false, NOON).rel}`);
check(info('2026-12-04', 'designed', false, NOON).state === 'soon'
      && info('2026-12-05', 'designed', false, NOON).state === 'normal',
  'due-soon covers the next seven days and stops there',
  'the due-soon window is not seven days');
check(info('2026-02-30', 'designed', false, NOON) === null,
  'an impossible date renders nothing rather than rolling into March',
  'Feb 30 was accepted');
check(info('2026-11-20', 'designed', true, NOON).state === 'done',
  'a closed card is finished, not late',
  'a closed card was marked overdue');

// ── The picker on the open card ───────────────────────────────────────────
await window.Boards.cardClicked('late');
await new Promise(r => setTimeout(r, 30));
const input = root.querySelector('#b-ec-due');
check(input && input.type === 'date', 'the open card has a calendar date picker',
  'no <input type="date" id="b-ec-due"> in the card modal');
check(input.value === day(-3), 'the picker opens on the card\'s due date',
  `the picker opened on "${input.value}"`);
check(/overdue/.test(root.querySelector('#b-ec-due-state').textContent),
  'and says under it what the chip says',
  `state line: "${root.querySelector('#b-ec-due-state').textContent}"`);

sent.length = 0;
input.value = '2030-01-15';
input.dispatchEvent(new window.Event('change'));
await new Promise(r => setTimeout(r, 60));
const save = sent.find(x => x.method === 'PATCH' && x.url.includes('/cards/late'));
check(save && save.body.due === '2030-01-15',
  'picking a date saves it to the card at once',
  `PATCHes sent: ${JSON.stringify(sent)}`);

sent.length = 0;
window.Boards.clearDue();
await new Promise(r => setTimeout(r, 60));
const clr = sent.find(x => x.method === 'PATCH' && x.url.includes('/cards/late'));
check(input.value === '' && clr && clr.body.due === '',
  'Clear empties the picker and sends an explicit clear',
  `after Clear: value="${input.value}", sent ${JSON.stringify(sent)}`);

// The next card must not inherit this one's date.
await window.Boards.cardClicked('undated');
await new Promise(r => setTimeout(r, 30));
check(root.querySelector('#b-ec-due').value === '',
  'opening an undated card leaves the picker empty',
  `the picker carried "${root.querySelector('#b-ec-due').value}" over from the last card`);

// ── New card form ─────────────────────────────────────────────────────────
check(!!root.querySelector('#b-cm-due[type="date"]'),
  'the new-card form has a due date picker too',
  'no #b-cm-due in the new card form');
check(/due:\s*\(\$\('b-cm-due'\)/.test(src),
  'and submitCard() sends it',
  'submitCard() does not send the due date');

// ── Source-level guards ───────────────────────────────────────────────────
check(/c\.assignee, c\.due,/.test(src),
  'the due date is in the board signature, so an agent setting one shows up live',
  'c.due is missing from _sigOf()');
check(/\.b-card-due\.overdue/.test(css) && /\.b-card-due\.soon/.test(css),
  'the overdue and due-soon states are styled',
  'boards.css has no overdue/soon chip rules');

console.log(ok ? '\ncard-due: all checks passed' : '\ncard-due: FAILED');
process.exit(ok ? 0 : 1);
