// Elefant has exactly one Kanban board per tenant, so the product names it in
// the singular: the tab, the board's own heading and the legacy redirect page
// all read "Board", never "Boards". Internal identifiers (window.Boards, the
// #boards deep link, boards.js) are deliberately unchanged so links keep working.

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { JSDOM } from 'jsdom';

const __dirname = dirname(fileURLToPath(import.meta.url));
const read = (p) => readFileSync(join(__dirname, p), 'utf8');

let ok = true;
const fail = (m) => { console.error('❌ ' + m); ok = false; };

// The tab button in the SPA shell.
{
  const doc = new JSDOM(read('../static/index.html')).window.document;
  const tab = doc.querySelector('.tab-btn[data-tab="boards"]');
  if (!tab) fail('no tab button for the board (data-tab="boards" is the deep-link key)');
  else if (tab.textContent.trim() !== 'Board') fail(`the board tab reads "${tab.textContent.trim()}", not "Board"`);
}

// The heading boards.js renders at the top of the board.
{
  const m = read('../static/boards.js').match(/<span class="b-heading">([^<]*)<\/span>/);
  if (!m) fail('boards.js no longer renders a b-heading');
  else if (m[1].trim() !== 'Board') fail(`the board heading reads "${m[1].trim()}", not "Board"`);
}

// The retired standalone page still redirects to the tab; its title and
// fallback link are user-visible too.
{
  const doc = new JSDOM(read('../../editor/static/board.html')).window.document;
  if (!/^Board\b/.test(doc.title)) fail(`board.html title reads "${doc.title}", not "Board · Elefant"`);
  const a = doc.querySelector('noscript a');
  if (a && /Boards/.test(a.textContent)) fail(`board.html fallback link still says "${a.textContent}"`);
}

if (!ok) process.exit(1);
console.log('✅ board naming: the tab, heading and legacy page all say "Board"');
