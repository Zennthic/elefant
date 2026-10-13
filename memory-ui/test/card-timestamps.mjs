// A card's details view says when it happened.
//
// Created / Updated / Closed, bottom left of the open card. All three ride on
// the card payload already, so this is a rendering change, and the ways it goes
// wrong are rendering ways:
//   * "Invalid Date" painted into the card from a value that would not parse,
//   * a Closed row on a card that is open (or missing on one that is closed),
//   * a row rendered empty because the payload didn't carry that field,
//   * the stamps surviving from the previous card when a second one is opened,
//     which is how every other field in this modal has gone wrong before.
//
// The format is deliberately absolute AND relative: relative alone cannot date
// an old card, absolute alone makes the reader do the arithmetic. Both halves
// are asserted, because dropping either is the tempting simplification.

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

const ISO = (msAgo) => new Date(Date.now() - msAgo).toISOString();
const DAY = 86400 * 1000;

const cols = {
  columns: [{ id: 'c1', name: 'Up Next', stage: 'designed', position: 1, color: null }],
  stages: ['designed'], nudge_chars: 280,
};

const CARDS = [
  { slug: 'open-one', title: 'Open One', body: '', column_id: 'c1', position: 1,
    source: 'ui', labels: [], repo: '', reporter: '', participants: [],
    closed: false, closed_at: null,
    created_at: ISO(30 * DAY), updated_at: ISO(2 * DAY) },
  { slug: 'closed-one', title: 'Closed One', body: '', column_id: 'c1', position: 2,
    source: 'ui', labels: [], repo: '', reporter: '', participants: [],
    closed: true, closed_at: ISO(1 * DAY),
    created_at: ISO(60 * DAY), updated_at: ISO(1 * DAY) },
  // An older image, or a card written before a backfill: no timestamps at all.
  { slug: 'no-times', title: 'No Times', body: '', column_id: 'c1', position: 3,
    source: 'ui', labels: [], repo: '', reporter: '', participants: [] },
  // A value that will not parse. It must render as nothing, never "Invalid Date".
  { slug: 'bad-times', title: 'Bad Times', body: '', column_id: 'c1', position: 4,
    source: 'ui', labels: [], repo: '', reporter: '', participants: [],
    created_at: 'not-a-date', updated_at: ISO(3600 * 1000) },
];

const dom = new JSDOM('<!doctype html><html><body><div id="boards-root"></div></body></html>', {
  runScripts: 'dangerously', pretendToBeVisual: true, url: 'http://localhost/',
});
const { window } = dom;
window.fetch = async (url) => {
  const u = String(url);
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
      if (u.includes('/cards')) return { cards: CARDS.filter(c => !c.closed), people: {} };
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
const times = () => root.querySelector('#b-ec-times');
const rows = () => [...times().querySelectorAll('.b-ec-time')].map(el => ({
  label: el.querySelector('.b-ec-time-label').textContent,
  value: el.querySelector('.b-ec-time-value').textContent,
}));

async function open(slug) {
  await window.Boards.cardClicked(slug);
  await new Promise(r => setTimeout(r, 30));
}

check(!!times(), 'the details view has a timestamps block',
  'no #b-ec-times in the card modal');

// ── An open card ──────────────────────────────────────────────────────────
await open('open-one');
{
  const r = rows();
  check(r.map(x => x.label).join(',') === 'Created,Updated',
    'an open card shows Created and Updated',
    `an open card shows: ${r.map(x => x.label).join(', ') || 'nothing'}`);
  check(!r.some(x => x.label === 'Closed'),
    'and no Closed row',
    'an open card claims a close time');

  const updated = r.find(x => x.label === 'Updated').value;
  check(/\(2d ago\)$/.test(updated),
    'a recent stamp carries a relative hint',
    `"Updated" rendered as "${updated}"`);
  check(updated.replace(/\s*\(2d ago\)$/, '').length > 6,
    'alongside the absolute date, so the card can still be dated',
    `"Updated" is relative only: "${updated}"`);

  const created = r.find(x => x.label === 'Created').value;
  check(!/ago\)/.test(created) && created.length > 0,
    'a stamp past a month drops the hint and just states the date',
    `"Created" (30 days old) rendered as "${created}"`);
}

// ── A closed card ─────────────────────────────────────────────────────────
await open('closed-one');
{
  const r = rows();
  check(r.map(x => x.label).join(',') === 'Created,Updated,Closed',
    'a closed card adds a Closed row, in that order',
    `a closed card shows: ${r.map(x => x.label).join(', ')}`);
  check(/\(1d ago\)$/.test(r.find(x => x.label === 'Closed').value),
    'reading the close time from closed_at',
    `"Closed" rendered as "${r.find(x => x.label === 'Closed').value}"`);
}

// ── Going back to an open card ────────────────────────────────────────────
// Every other field in this modal has leaked from one card to the next at some
// point; the Closed row is the one here that would.
await open('open-one');
check(!rows().some(x => x.label === 'Closed'),
  'reopening an open card clears the previous card\'s Closed row',
  'the Closed row followed from the card opened before');

// ── Missing and unparseable values ────────────────────────────────────────
await open('no-times');
check(rows().length === 0 && times().textContent.trim() === '',
  'a card with no timestamps renders no rows at all',
  `a card with no timestamps rendered: ${times().innerHTML}`);

await open('bad-times');
{
  const r = rows();
  check(!times().textContent.includes('Invalid Date'),
    'an unparseable timestamp never reaches the card',
    `"Invalid Date" was rendered: ${times().textContent}`);
  check(r.length === 1 && r[0].label === 'Updated',
    'the bad row is dropped and the good one still shows',
    `rendered: ${r.map(x => x.label + '=' + x.value).join(', ')}`);
}

// ── Source-level guards ───────────────────────────────────────────────────

check(/esc\(fmtStamp\(iso\)\)/.test(src) && /title="\$\{esc\(iso\)\}"/.test(src),
  'the stamps are escaped before they reach innerHTML',
  'renderCardTimes() interpolates a server value unescaped');

check(/\(c\.closed \?\? c\.archived\)/.test(src),
  'the Closed row reads either field name, for a payload from before the rename',
  'renderCardTimes() reads only the new field name');

check(/\.b-ec-times/.test(css) && /\.b-ec-foot/.test(css),
  'the block is styled, bottom left',
  'boards.css has no rules for the timestamps block');

const foot = (css.match(/\.b-ec-foot\s*\{[^}]*\}/) || [''])[0];
check(/align-items:\s*flex-start/.test(foot),
  'and left-aligned within it',
  `the foot rule does not left-align: ${foot}`);

console.log(ok ? '\ncard-timestamps: all checks passed'
               : '\ncard-timestamps: FAILED');
process.exit(ok ? 0 : 1);
