// The page Move dialog's destination picker (issue #123).
//
// The destination used to be one free-text path, so a typo in the folder half
// silently created a new folder — S3 has no directories, and writing to
// "Hadware/x.md" just makes one. This runs the dialog's real functions in
// jsdom against a stub folder list and checks that the folder half can only
// ever be something that already exists.

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { JSDOM } from 'jsdom';

const __dirname = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(join(__dirname, '../static/index.html'), 'utf8');

let ok = true;
const fail = (m) => { console.error('❌ ' + m); ok = false; };
const pass = (m) => console.log('✅ ' + m);
const eq = (actual, expected, m) =>
  actual === expected ? pass(m) : fail(`${m} — got ${JSON.stringify(actual)}, wanted ${JSON.stringify(expected)}`);
const deep = (actual, expected, m) =>
  eq(JSON.stringify(actual), JSON.stringify(expected), m);

// ── Carve the dialog out of the SPA, markup and logic ────────────────────────
function slice(from, to, label) {
  const a = html.indexOf(from);
  if (a < 0) throw new Error(`${label}: cannot find ${JSON.stringify(from)}`);
  const b = html.indexOf(to, a);
  if (b < 0) throw new Error(`${label}: cannot find ${JSON.stringify(to)}`);
  return html.slice(a, b);
}

const modalMarkup = slice('<!-- Move-file modal', '<!-- Drag-and-drop reorganise', 'modal markup');
const logicSrc = slice('function moveModalEls() {', '// FOLDER MANAGEMENT (tree hover icons)', 'move logic')
  .replace(/\/\/ ═+\s*$/, '');
// buildWikiIndex is what fills the folder list, so it is under test too.
const indexSrc = slice('function buildWikiIndex(', 'function _joinWikiPath(', 'buildWikiIndex');

const dom = new JSDOM(`<!doctype html><html><body>${modalMarkup}</body></html>`,
  { url: 'https://example.test/', runScripts: 'outside-only' });
const { window } = dom;

const toasts = [];
const apiCalls = [];
let apiReject = null;
window.__stub = {
  toast: (msg, kind) => toasts.push({ msg, kind }),
  apiFetch: async (url, opts) => {
    apiCalls.push({ url, body: JSON.parse(opts.body) });
    if (apiReject) { const e = apiReject; apiReject = null; throw e; }
    return { status: 'ok' };
  },
  noop: () => {},
  asyncNoop: async () => {},
};

window.eval(`
  const HIDDEN_TREE_DIRS = new Set(['assets']);
  let _wikiDirs = [''];
  let _wikiPathMap = null;
  let _wikiStemMap = null;
  let currentFilePath = null;
  let currentFileRaw = null;
  let wikiLoaded = false;
  const toast = __stub.toast;
  const apiFetch = __stub.apiFetch;
  const ensurePathVisible = __stub.noop;
  const loadWikiTree = __stub.asyncNoop;
  const loadFileContent = __stub.noop;

  ${indexSrc}
  ${logicSrc}

  window.__t = {
    buildWikiIndex, openMoveModal, confirmMove, moveModalDestination,
    renderMoveModalFolders, moveModalEls,
    get dirs() { return _wikiDirs; },
    setOpenFile(p) { currentFilePath = p; },
    get openFile() { return currentFilePath; },
  };
`);

const T = window.__t;
const els = T.moveModalEls();
const optionValues = () => [...els.folder.options].map(o => o.value);
const optionLabels = () => [...els.folder.options].map(o => o.textContent);

// ── The tree the dialog sees ─────────────────────────────────────────────────
T.buildWikiIndex({
  children: [
    { type: 'file', name: 'index.md', path: 'index.md' },
    { type: 'directory', name: 'AI', path: 'AI', children: [
      { type: 'directory', name: 'runbooks', path: 'AI/runbooks', children: [
        { type: 'file', name: 'db-restore.md', path: 'AI/runbooks/db-restore.md' },
      ] },
    ] },
    { type: 'directory', name: 'hardware', path: 'hardware', children: [
      { type: 'file', name: 'ESP32-C3.md', path: 'hardware/ESP32-C3.md' },
    ] },
    // Internal, hidden from the tree — and so from the picker.
    { type: 'directory', name: 'assets', path: 'assets', children: [] },
  ],
});

// ── 1. The folder list is built from the tree ────────────────────────────────
deep(T.dirs, ['', 'AI', 'AI/runbooks', 'hardware'],
  'every real folder is offered, root first, and assets is not');

// ── 2. The dialog opens on the page's current folder ─────────────────────────
T.setOpenFile('hardware/ESP32-C3.md');
T.openMoveModal();
eq(els.folder.value, 'hardware', 'the picker opens on the folder the page is in');
eq(els.name.value, 'ESP32-C3.md', 'the page name is prefilled');
eq(els.preview.textContent, 'hardware/ESP32-C3.md', 'the preview starts at the current path');
eq(els.confirm.disabled, true, 'moving a page to where it already is offers nothing to do');

// ── 3. There is no way to type a folder that does not exist ──────────────────
eq(els.folder.tagName, 'SELECT', 'the destination folder is a picker, not a text field');
{
  const typed = [...els.folder.options].every(o => T.dirs.includes(o.value));
  eq(typed, true, 'every option is a folder that exists');
}

// ── 4. Picking a folder rebuilds the destination ─────────────────────────────
els.folder.value = 'AI/runbooks';
els.folder.dispatchEvent(new window.Event('change'));
eq(els.preview.textContent, 'AI/runbooks/ESP32-C3.md', 'the preview follows the picked folder');
eq(els.confirm.disabled, false, 'a real move is offered');

// ── 5. The root is a destination, and reads as one ───────────────────────────
els.folder.value = '';
els.folder.dispatchEvent(new window.Event('change'));
eq(els.preview.textContent, 'ESP32-C3.md', 'the wiki root gives a bare path');
eq(optionLabels()[0], '/  (wiki root)', 'the root option is labelled, not blank');

// ── 6. Filtering narrows the list without losing the current folder ──────────
T.setOpenFile('hardware/ESP32-C3.md');
T.openMoveModal();
els.filter.value = 'runbook';
els.filter.dispatchEvent(new window.Event('input'));
eq(optionValues().includes('AI/runbooks'), true, 'the filter finds a nested folder');
eq(optionValues().includes('hardware'), true,
   "the page's own folder survives a filter that excludes it");
eq(optionValues().includes(''), false, 'an unrelated folder is filtered out');

els.filter.value = 'zzz-nothing';
els.filter.dispatchEvent(new window.Event('input'));
eq(els.nomatch.style.display, '', 'a filter matching nothing says so');
eq(optionValues().length, 1, 'and still leaves the current folder selectable');

els.filter.value = '';
els.filter.dispatchEvent(new window.Event('input'));
eq(els.nomatch.style.display, 'none', 'clearing the filter clears the message');
deep(optionValues(), ['', 'AI', 'AI/runbooks', 'hardware'], 'and restores the full list');

// ── 7. The page name is still free text, and still gets its extension ────────
T.setOpenFile('hardware/ESP32-C3.md');
T.openMoveModal();
els.name.value = 'ESP32-C6';
els.name.dispatchEvent(new window.Event('input'));
eq(els.preview.textContent, 'hardware/ESP32-C6.md', '.md is added when it is left off');
els.name.value = '   ';
els.name.dispatchEvent(new window.Event('input'));
eq(els.preview.textContent, '—', 'an empty name has no destination');
eq(els.confirm.disabled, true, 'and cannot be moved');

// ── 8. Confirming sends the assembled path ───────────────────────────────────
T.setOpenFile('hardware/ESP32-C3.md');
T.openMoveModal();
els.folder.value = 'AI/runbooks';
els.folder.dispatchEvent(new window.Event('change'));
await T.confirmMove();
eq(apiCalls.length, 1, 'one call');
eq(apiCalls[0].url, '/api/move-file', 'to the move endpoint');
eq(apiCalls[0].body.from_path, 'hardware/ESP32-C3.md', 'from the old path');
eq(apiCalls[0].body.to_path, 'AI/runbooks/ESP32-C3.md', 'to the picked folder');

// ── 9. A 409 is readable and the dialog stays open ───────────────────────────
apiCalls.length = 0;
T.setOpenFile('hardware/ESP32-C3.md');
T.openMoveModal();
els.folder.value = 'AI';
els.folder.dispatchEvent(new window.Event('change'));
apiReject = new Error('409: Destination already exists');
await T.confirmMove();
eq(toasts.at(-1).msg, 'That folder already has a page by that name.', 'a 409 names the clash');
eq(window.document.getElementById('move-modal').style.display, 'flex',
   'a failed move leaves the dialog open to retry');
eq(els.confirm.disabled, false, 'the button is usable again');

console.log(ok ? '\nwiki-move-picker: all checks passed' : '\nwiki-move-picker: FAILED');
process.exit(ok ? 0 : 1);
