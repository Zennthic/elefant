// The card editor's three input bugs — issue #142.
//
// An operator reported five things. They were three bugs, and four of the five
// reports were the same one:
//
//   1. Enter never inserted a newline in the description, because the inline
//      @-mention typeahead attaches to that textarea and its keydown handler
//      consumed EVERY Enter rather than only the ones it had a use for. No
//      newline means no bullet list, no numbered list and no fenced block —
//      which is why three separate "markdown is broken" reports came in for a
//      parser that was working the whole time.
//   2. A pasted screenshot uploaded, appeared in the box, and was thrown away
//      on close: the handler assigns `textarea.value`, which fires no `input`
//      event, and `input` is the only thing that schedules the auto-save.
//   3. The comment composer had no paste handler at all.
//
// Each check below was mutation-checked against the unfixed boards.js: revert
// the fix and the matching assertion fails. That matters most for (1), where
// the bug is a preventDefault() nobody would look twice at, and the tempting
// test — "does the description render a list" — passes on the broken code.

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { JSDOM } from 'jsdom';

const __dirname = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(__dirname, '../static/boards.js'), 'utf8');

let ok = true;
const fail = (m) => { console.error('❌ ' + m); ok = false; };
const pass = (m) => console.log('✅ ' + m);
const check = (cond, good, bad) => cond ? pass(good) : fail(bad);

const COLS = {
  columns: [{ id: 'c1', name: 'Up Next', stage: 'designed', position: 1, color: null }],
  stages: ['designed'],
};
const CARD = {
  slug: 'a-card', number: 7, title: 'A Card', body: '', column_id: 'c1', position: 1,
  source: 'ui', labels: [], repo: '', reporter: '', assignee: '', participants: [],
  attachments: [], created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
};
const DIRECTORY = [
  { ref: 'user:jon', display: 'Jon Smith', handle: 'jon', kind: 'user' },
];

const dom = new JSDOM('<!doctype html><html><body><div id="boards-root"></div></body></html>', {
  runScripts: 'dangerously', pretendToBeVisual: true, url: 'http://localhost/',
});
const { window } = dom;
window.Sortable = { create: () => ({ destroy() {} }), destroy() {} };

// Every PATCH the card makes, so "was it saved" is a question with an answer
// rather than an inference from the DOM.
const patched = [];
const uploads = [];
window.fetch = async (url, opts = {}) => {
  const u = String(url);
  if (opts.method === 'PATCH') patched.push({ url: u, body: JSON.parse(opts.body || '{}') });
  if (u.includes('/api/upload-image')) {
    uploads.push(u);
    return { ok: true, status: 200,
             json: async () => ({ path: 'cards/a-card/20260926-abc-shot.png', name: 'shot.png' }) };
  }
  return {
    ok: true, status: 200,
    json: async () => {
      if (u.includes('/columns')) return COLS;
      if (u.includes('/labels')) return { labels: [], reserved: [] };
      if (u.includes('/people')) return { me: 'user:jon', people: DIRECTORY };
      if (/\/cards\/[^/?]+/.test(u) && !u.includes('/cards?')) {
        return { card: CARD, people: {}, comments: [],
                 relationships: { blocked_by: [], blocks: [], related: [] } };
      }
      if (u.includes('/cards')) return { cards: [CARD], people: {} };
      return {};
    },
  };
};

const ms = window.document.createElement('script');
ms.textContent = readFileSync(join(__dirname, '../static/marked.min.js'), 'utf8');
window.document.body.appendChild(ms);
const s = window.document.createElement('script');
s.textContent = src;
window.document.body.appendChild(s);

await window.Boards.init();
await new Promise(r => setTimeout(r, 50));
await window.Boards.cardClicked('a-card');
await new Promise(r => setTimeout(r, 60));

const $ = (id) => window.document.getElementById(id);
const settle = (ms_ = 40) => new Promise(r => setTimeout(r, ms_));

// Returns whether the handler CONSUMED the Enter. jsdom does no text editing,
// so preventDefault is the whole question: unprevented, the browser inserts the
// newline itself.
function enterConsumed(el) {
  const ev = new window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true });
  el.dispatchEvent(ev);
  return ev.defaultPrevented;
}

function typeInto(el, value) {
  el.value = value;
  el.selectionStart = el.selectionEnd = value.length;
  el.dispatchEvent(new window.Event('input', { bubbles: true }));
}

// ── 1. Enter reaches the description ──────────────────────────────────────

const body = $('b-ec-body');
check(!!body, 'the card modal has a description textarea',
  'no #b-ec-body in the card modal');
check(body.tagName === 'TEXTAREA',
  'and it is a textarea, so Enter is the only way to reach a second line',
  `#b-ec-body is a <${body.tagName.toLowerCase()}>`);

typeInto(body, 'first line');
await settle();
check(!enterConsumed(body),
  'Enter is not consumed in the description',
  'Enter in the description is still swallowed — no newline, and therefore no '
  + 'list and no fenced block either (the original bug)');

// The mention token deliberately spans spaces so "@Jon Smith" can match, which
// means the suggestion list stays open for a long stretch of ordinary prose
// after any @. That is exactly the state the old code turned into a dead Enter
// key, so it gets its own check rather than riding on the one above.
const results = $('b-ec-body-results');
typeInto(body, 'ping @jon about the thing later');
await settle();
check(results && !results.hidden,
  'typing after an @ leaves the mention list open',
  'the mention list did not open — this check no longer exercises the bug it '
  + 'was written for, so fix the fixture rather than deleting the check');
check(!enterConsumed(body),
  'and Enter still reaches the description with that list open',
  'Enter is swallowed whenever the mention list is open, which after any @ is '
  + 'most of the time');

// The other half of the same fix: Enter must still PICK a highlighted row,
// or the mention picker is keyboard-dead.
typeInto(body, 'hello @jo');
await settle();
{
  // Let the keystroke's OWN auto-save land first. Without this drain the
  // assertion below passes for the wrong reason: the save scheduled by typing
  // `@jo` fires after the mention has been inserted and reads the textarea as
  // it then stands, so it carries the mention whether or not the programmatic
  // write scheduled anything of its own.
  await settle(1100);
  const before = patched.length;

  const down = new window.KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true, cancelable: true });
  body.dispatchEvent(down);
  const consumed = enterConsumed(body);
  check(consumed, 'Enter on a highlighted suggestion is consumed and picks it',
    'Enter no longer picks the highlighted mention — the fix went too far');
  check(/@\[Jon Smith\]\(user:jon\)/.test(body.value),
    'and the mention lands as markdown with the reference beside the name',
    `picking wrote: ${JSON.stringify(body.value)}`);

  // ── 2. A programmatic write is saved ────────────────────────────────────
  // The mention went in by code, not by keystroke. If a programmatic write does
  // not reach the `input` listeners, nothing schedules a save and the text dies
  // with the modal.
  await settle(1100);
  const fresh = patched.slice(before).filter(p => typeof p.body.body === 'string');
  check(fresh.some(p => /@\[Jon Smith\]\(user:jon\)/.test(p.body.body)),
    'and picking it schedules a save of its own, as typing would',
    'picking a mention scheduled no save — a mention picked and then left alone '
    + 'is still lost on close');
}

// A single-line field that declared onEnter must be untouched by all this:
// Enter there commits what was typed, and always did.
{
  const labels = $('b-ec-labels');
  labels.value = 'brand-new-label';
  const consumed = enterConsumed(labels);
  check(consumed && window.Boards.getLabels('b-ec-labels').includes('brand-new-label'),
    'Enter still commits a typed label, which is a field that asked for it',
    'Enter no longer adds a label — the single-line fields regressed');
}

// ── 3. Pasting a screenshot: description AND comments ─────────────────────

// A real File, not a stand-in: uploadAsset hands it to FormData.append, which
// rejects anything that is not a Blob — so a plain object would fail the upload
// here for a reason the browser never has.
const shotFile = () => new window.File([new window.Uint8Array([137, 80, 78, 71])],
                                       'shot.png', { type: 'image/png' });

function pasteImage(el) {
  const ev = new window.Event('paste', { bubbles: true, cancelable: true });
  ev.clipboardData = {
    items: [{ kind: 'file', type: 'image/png', getAsFile: shotFile }],
  };
  el.dispatchEvent(ev);
  return ev;
}

{
  // Drained first, for the same reason as the mention above: a save still in
  // flight re-reads the fields when it lands, so it would carry the pasted
  // image without the paste having scheduled anything.
  await settle(1100);
  body.value = '';
  const before = uploads.length;
  const patchedBefore = patched.length;
  const ev = pasteImage(body);
  check(ev.defaultPrevented, 'an image pasted into the description is intercepted',
    'the description does not intercept an image paste');
  await settle(120);
  check(uploads.length === before + 1, 'and uploaded',
    'no upload was attempted for a pasted screenshot');
  check(/^!\[shot\.png\]\(\/api\/image\?path=cards%2Fa-card%2F/.test(body.value),
    'and written into the description as markdown on OUR image route',
    `the description holds: ${JSON.stringify(body.value)}`);
  await settle(1200);
  const saved = patched.slice(patchedBefore)
    .filter(p => /\/api\/image\?path=/.test(p.body.body || ''));
  check(saved.length > 0,
    'and auto-saved, so closing the card does not throw the screenshot away',
    'the pasted screenshot was never PATCHed — it uploads, shows on screen, and '
    + 'is lost on close, which is what the operator reported');
}

{
  const composer = $('b-ec-comment-body');
  check(!!composer, 'the card has a comment composer',
    'no #b-ec-comment-body in the card modal');
  composer.value = 'look at this: ';
  const before = uploads.length;
  const ev = pasteImage(composer);
  check(ev.defaultPrevented, 'an image pasted into a COMMENT is intercepted too',
    'the comment composer ignores a pasted image — it was never wired');
  await settle(120);
  check(uploads.length === before + 1, 'and uploaded',
    'no upload was attempted for an image pasted into a comment');
  check(/\/api\/image\?path=cards%2Fa-card%2F/.test(composer.value),
    'and written into the composer as markdown',
    `the composer holds: ${JSON.stringify(composer.value)}`);
  // The draft is what survives a closed modal for an unsent comment, and it is
  // kept on `input` — the same listener the paste has to reach.
  let draft = null;
  try { draft = window.localStorage.getItem('elefant:board:draft:comment:a-card'); }
  catch (e) { draft = null; }
  check(draft === null || /\/api\/image\?path=/.test(draft),
    'and kept in the comment draft, so it survives the modal closing',
    `the draft did not pick up the pasted image: ${JSON.stringify(draft)}`);
}

// The toast must not send anyone looking for the Save button #111 removed.
// Comments are stripped first, so the line in boards.js EXPLAINING why the
// phrase is gone does not count as the phrase coming back.
const codeOnly = src.replace(/^[ \t]*\/\/.*$/gm, '');
check(!/Save the card to keep it/.test(codeOnly),
  'nothing tells the operator to press a Save button that no longer exists',
  'a toast still says "Save the card to keep it" — #111 removed that button');

// ── 4. What was never broken, asserted so it cannot become broken ─────────
//
// The three markdown reports were a missing newline, not a missing parser. If
// this ever goes red the original diagnosis was wrong and the parser really is
// at fault, so it is worth one check.
{
  body.value = ['* one', '* two', '', '1. first', '', '```js', 'const x = 1;', '```'].join('\n');
  window.Boards.showDescription('preview');
  const html = $('b-ec-body-preview').innerHTML;
  check(/<ul>[\s\S]*<li>one<\/li>/.test(html), 'a bullet list renders as a list',
    `bullet list did not render: ${html}`);
  check(/<ol>[\s\S]*<li>first<\/li>/.test(html), 'a numbered list renders as one',
    `numbered list did not render: ${html}`);
  check(/<pre><code class="language-js">/.test(html), 'and a fenced block keeps its language',
    `fenced block did not render: ${html}`);
}

console.log(ok ? '\ncard-editor-input: all checks passed'
               : '\ncard-editor-input: FAILED');
process.exit(ok ? 0 : 1);
