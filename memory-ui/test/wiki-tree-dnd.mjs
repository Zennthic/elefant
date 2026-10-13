// Wiki tree drag-and-drop reorganise (issue #121).
//
// The tree's move rules are pure enough to run for real: this pulls renderTree
// and the drag-and-drop module out of the single-file SPA, evaluates them in a
// jsdom window over a stub tree, and fires actual drag events at the rows. What
// it is guarding is the part a structural assertion cannot see — that a folder
// cannot be dropped into its own subtree, that a drop opens the confirmation
// instead of moving anything, and that confirming picks the right endpoint.

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

// ── Carve the two regions under test out of the SPA ──────────────────────────
function slice(from, to, label) {
  const a = html.indexOf(from);
  if (a < 0) throw new Error(`${label}: cannot find start marker ${JSON.stringify(from)}`);
  const b = html.indexOf(to, a);
  if (b < 0) throw new Error(`${label}: cannot find end marker ${JSON.stringify(to)}`);
  return html.slice(a, b);
}

const renderTreeSrc = slice('function renderTree(', 'async function loadFileContent(', 'renderTree');
const dndSrc = slice('// WIKI TREE DRAG-AND-DROP REORGANISE', '// SETTINGS SUB-TABS', 'drag-and-drop module')
  // Drop the trailing banner rule of the NEXT section, which the slice catches.
  .replace(/\/\/ ═+\s*$/, '');

// ── A window with just enough SPA around them ────────────────────────────────
const dom = new JSDOM(`<!doctype html><html><body>
  <div id="wiki-tree"></div>
  <div class="modal-overlay" id="tree-move-modal" style="display:none;">
    <div class="modal-title" id="tree-move-modal-title"></div>
    <code id="tree-move-from"></code>
    <code id="tree-move-to"></code>
    <button id="tree-move-confirm"></button>
  </div>
</body></html>`, { url: 'https://example.test/', runScripts: 'outside-only' });

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
  loadWikiTree: async () => {},
  loadFileContent: () => {},
  noop: () => {},
};

window.eval(`
  const HIDDEN_TREE_DIRS = new Set(['assets']);
  let wikiExpandedDirs = new Set();
  let wikiLoaded = false;
  let currentFilePath = null;
  let currentFileRaw = null;
  const saveExpandedDirs = __stub.noop;
  const loadExpandedDirs = () => new Set();
  const ensureDirVisible = __stub.noop;
  const ensurePathVisible = __stub.noop;
  const closeWikiSidebarOnMobile = __stub.noop;
  const addFileInFolder = __stub.noop;
  const addSubfolder = __stub.noop;
  const openShareModal = __stub.noop;
  const renameDir = __stub.noop;
  const deleteDir = __stub.noop;
  const toast = __stub.toast;
  const apiFetch = __stub.apiFetch;
  const loadWikiTree = __stub.loadWikiTree;
  const loadFileContent = __stub.loadFileContent;
  const highlightActiveTreeItem = __stub.noop;

  ${renderTreeSrc}
  ${dndSrc}

  window.__t = {
    renderTree, wireTreeRootDropZone, wikiDropRefusal, wikiDropDestination,
    get pending() { return wikiPendingMove; },
    get dragItem() { return wikiDragItem; },
    setOpenFile(p) { currentFilePath = p; },
  };
`);

const T = window.__t;

// ── The tree under test ──────────────────────────────────────────────────────
const tree = {
  children: [
    { type: 'file', name: 'index.md', path: 'index.md' },
    { type: 'directory', name: 'AI', path: 'AI', children: [
      { type: 'file', name: 'scratch.md', path: 'AI/scratch.md' },
      { type: 'directory', name: 'runbooks', path: 'AI/runbooks', children: [
        { type: 'file', name: 'db-restore.md', path: 'AI/runbooks/db-restore.md' },
      ] },
    ] },
    { type: 'directory', name: 'hardware', path: 'hardware', children: [
      { type: 'file', name: 'ESP32-C3.md', path: 'hardware/ESP32-C3.md' },
    ] },
  ],
};

const treeEl = window.document.getElementById('wiki-tree');
T.wireTreeRootDropZone();
T.renderTree(tree, treeEl);

const rowFor = (path) => treeEl.querySelector(`.tree-item[data-path="${path}"]`);
// A header's own name is its bare text node — the rest of textContent is the
// arrow glyph and the hover action buttons.
const headerFor = (name) =>
  [...treeEl.querySelectorAll('.tree-dir-header')].find(h =>
    [...h.childNodes].some(n => n.nodeType === 3 && n.textContent.trim() === name));

// ── Fake drag events ─────────────────────────────────────────────────────────
function dragEvent(type, target, relatedTarget = null) {
  const e = new window.Event(type, { bubbles: true, cancelable: true });
  e.dataTransfer = { setData() {}, effectAllowed: '', dropEffect: '' };
  if (relatedTarget) Object.defineProperty(e, 'relatedTarget', { value: relatedTarget });
  else Object.defineProperty(e, 'relatedTarget', { value: null });
  target.dispatchEvent(e);
  return e;
}
const drag = (el) => dragEvent('dragstart', el);
const over = (el) => dragEvent('dragover', el);
const drop = (el) => dragEvent('drop', el);
const end  = (el) => dragEvent('dragend', el);

// ── 1. Everything the tree renders is draggable ──────────────────────────────
eq(rowFor('AI/scratch.md').draggable, true, 'a page row is draggable');
eq(headerFor('AI').draggable, true, 'a folder header is draggable');

// ── 2. The pure rules ────────────────────────────────────────────────────────
const dir = (p) => ({ kind: 'dir', path: p });
const file = (p) => ({ kind: 'file', path: p });
eq(T.wikiDropRefusal(dir('AI'), 'AI'), 'A folder cannot be moved into itself.',
   'a folder refuses itself as a destination');
eq(T.wikiDropRefusal(dir('AI'), 'AI/runbooks'), 'A folder cannot be moved inside itself.',
   'a folder refuses its own descendant');
eq(T.wikiDropRefusal(dir('AI'), 'AI-archive'), null,
   'a sibling whose name merely starts with the folder name is NOT a descendant');
eq(T.wikiDropRefusal(file('AI/scratch.md'), 'AI'), 'Already in "AI".',
   'a page refuses the folder it is already in');
eq(T.wikiDropRefusal(file('index.md'), ''), 'Already at the wiki root.',
   'a root page refuses the root');
eq(T.wikiDropRefusal(file('AI/scratch.md'), 'hardware'), null, 'a real move is allowed');
eq(T.wikiDropDestination(file('AI/scratch.md'), 'hardware'), 'hardware/scratch.md',
   'the destination keeps the basename');
eq(T.wikiDropDestination(dir('AI/runbooks'), ''), 'runbooks', 'a folder moved to the root loses its parent');

// ── 3. A legal hover is accepted and painted; an illegal one is not ──────────
drag(rowFor('AI/scratch.md'));
const legal = over(headerFor('hardware'));
eq(legal.defaultPrevented, true, 'a legal folder hover is accepted');
eq(headerFor('hardware').classList.contains('tree-drop-into'), true, 'a legal target is painted');
const illegal = over(headerFor('AI'));
eq(illegal.defaultPrevented, false, "hovering the page's own folder is refused");
eq(headerFor('AI').classList.contains('tree-drop-into'), false, 'a refused target is not painted');
// The regression this ordering exists for: a refused row used to let the event
// bubble to the tree background, which armed "move to the wiki root" under a
// pointer aimed at a folder.
eq(treeEl.classList.contains('tree-drop-root'), false,
   'a refused row does not fall through and arm the root zone');

// ── 4. A drop confirms rather than moves ─────────────────────────────────────
drop(headerFor('hardware'));
eq(apiCalls.length, 0, 'a drop calls no API on its own');
eq(window.document.getElementById('tree-move-modal').style.display, 'flex', 'a drop opens the confirmation');
eq(window.document.getElementById('tree-move-from').textContent, 'AI/scratch.md', 'the confirmation names the source');
eq(window.document.getElementById('tree-move-to').textContent, 'hardware/scratch.md', 'the confirmation names the result');
eq(window.document.getElementById('tree-move-modal-title').textContent, 'Move page', 'a page move says so');
end(rowFor('AI/scratch.md'));

// ── 5. Confirming a page move hits /api/move-file ────────────────────────────
await window.document.getElementById('tree-move-confirm').onclick();
eq(apiCalls.length, 1, 'confirming makes exactly one call');
eq(apiCalls[0].url, '/api/move-file', 'a page goes to /api/move-file');
eq(apiCalls[0].body.from_path, 'AI/scratch.md', 'the call carries the source');
eq(apiCalls[0].body.to_path, 'hardware/scratch.md', 'the call carries the destination');
eq(window.document.getElementById('tree-move-modal').style.display, 'none', 'the confirmation closes on success');

// ── 6. A folder move hits /api/rename-dir ────────────────────────────────────
apiCalls.length = 0;
drag(headerFor('runbooks'));
drop(headerFor('hardware'));
eq(window.document.getElementById('tree-move-modal-title').textContent, 'Move folder', 'a folder move says so');
eq(window.document.getElementById('tree-move-to').textContent, 'hardware/runbooks', 'a folder keeps its own name');
end(headerFor('runbooks'));
await window.document.getElementById('tree-move-confirm').onclick();
eq(apiCalls[0].url, '/api/rename-dir', 'a folder goes to /api/rename-dir');
eq(apiCalls[0].body.from_path, 'AI/runbooks', 'the folder call carries the source prefix');

// ── 7. A folder dropped into its own subtree never reaches the confirmation ──
apiCalls.length = 0;
toasts.length = 0;
drag(headerFor('AI'));
const selfDrop = over(headerFor('runbooks'));
eq(selfDrop.defaultPrevented, false, 'a descendant refuses the hover');
drop(headerFor('runbooks'));
eq(window.document.getElementById('tree-move-modal').style.display, 'none',
   'a refused drop opens no confirmation');
eq(apiCalls.length, 0, 'a refused drop calls no API');
end(headerFor('AI'));

// ── 8. Dropping a page onto another page does not fall through to the root ───
apiCalls.length = 0;
drag(rowFor('AI/runbooks/db-restore.md'));
const onPage = drop(rowFor('hardware/ESP32-C3.md'));
eq(onPage.defaultPrevented, true, 'a page row swallows the drop');
eq(window.document.getElementById('tree-move-modal').style.display, 'none',
   'dropping on a page row moves nothing to the root');
end(rowFor('AI/runbooks/db-restore.md'));

// ── 9. The tree background IS the root ───────────────────────────────────────
drag(rowFor('AI/scratch.md'));
over(treeEl);
eq(treeEl.classList.contains('tree-drop-root'), true, 'the root zone paints');
drop(treeEl);
eq(window.document.getElementById('tree-move-to').textContent, 'scratch.md', 'the root destination is the bare name');
end(rowFor('AI/scratch.md'));
eq(treeEl.classList.contains('tree-drop-root'), false, 'dragend clears the root paint');
eq(treeEl.classList.contains('tree-dragging'), false, 'dragend clears the root drop band');

// ── 10. A 409 from the API is readable, and the confirmation stays open ──────
apiCalls.length = 0;
toasts.length = 0;
drag(rowFor('AI/runbooks/db-restore.md'));
drop(headerFor('hardware'));
end(rowFor('AI/runbooks/db-restore.md'));
apiReject = new Error('409: Destination already exists');
await window.document.getElementById('tree-move-confirm').onclick();
eq(toasts.at(-1).msg, 'The destination already has something by that name.', 'a 409 reads as a name clash');
eq(window.document.getElementById('tree-move-modal').style.display, 'flex',
   'a failed move leaves the confirmation open to retry');
eq(window.document.getElementById('tree-move-confirm').disabled, false, 'the button is usable again');

// ── 11. The folder row stays draggable in WebKit ─────────────────────────────
// Folders rendered, wired and passed every rule above and still would not drag
// in macOS Safari, because the header declared `user-select: none` and WebKit
// will not start a drag from an element it treats as unselectable. Page rows
// never had the property, which is why they dragged and folders did not. jsdom
// cannot act that out, so what is pinned here is the declaration itself.
const styleCss = [...html.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/g)]
  .map(m => m[1]).join('\n')
  .replace(/\/\*[\s\S]*?\*\//g, '');       // comments name the property; rules mean it
const cssRules = [...styleCss.matchAll(/([^{}]+)\{([^{}]*)\}/g)]
  .map(m => ({ selector: m[1].trim(), body: m[2] }));

const declaresOn = (selectorSubstring, property) => cssRules.some(r =>
  r.selector.split(',').some(sel => sel.trim().includes(selectorSubstring)) &&
  new RegExp(`(^|[;\\s])${property}\\s*:`).test(r.body));

eq(declaresOn('.tree-dir-header', 'user-select'), false,
   'no rule makes a folder header unselectable — WebKit would refuse to drag it');
eq(declaresOn('.tree-item', 'user-select'), false,
   'nor a page row, which is the arrangement that already dragged');
eq(declaresOn('.tree-dir-header[draggable="true"]', '-webkit-user-drag'), true,
   'a draggable folder header states -webkit-user-drag');
eq(declaresOn('.tree-item[draggable="true"]', '-webkit-user-drag'), true,
   'a draggable page row states it too');
// Keyed on the attribute, so clearing `draggable` for the inline rename input
// takes the drag intent away with it. A bare `.tree-dir-header` rule declaring
// it would leave the rename input sitting inside a live drag handle.
const unconditionalDragIntent = cssRules.filter(r => /-webkit-user-drag\s*:/.test(r.body))
  .flatMap(r => r.selector.split(','))
  .map(sel => sel.trim())
  .filter(sel => /^\.tree-(item|dir-header)$/.test(sel));
eq(unconditionalDragIntent.length, 0,
   'no tree row declares -webkit-user-drag unconditionally, only when draggable');

// ── 12. Double-click on a folder row selects nothing, and drags survive ──────
const mouseDown = (target, detail) => {
  const e = new window.MouseEvent('mousedown', { bubbles: true, cancelable: true, detail });
  target.dispatchEvent(e);
  return e;
};
const aiHeader = headerFor('AI');
eq(mouseDown(aiHeader, 1).defaultPrevented, false,
   'the first click is untouched — it is the one a drag starts from');
eq(mouseDown(aiHeader, 2).defaultPrevented, true,
   'the second click of a double-click selects no label text');
const renameInput = window.document.createElement('input');
aiHeader.appendChild(renameInput);
eq(mouseDown(renameInput, 2).defaultPrevented, false,
   'the inline rename input keeps double-click word selection');
renameInput.remove();

console.log(ok ? '\nwiki-tree-dnd: all checks passed' : '\nwiki-tree-dnd: FAILED');
process.exit(ok ? 0 : 1);
