// Smoke-test the COMMITTED bundle (static/tiptap.bundle.js) the way the browser
// loads it: execute the IIFE in a jsdom window, then drive the global surface.
// Proves the vendored artifact works end-to-end, not just the source modules.

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { JSDOM } from 'jsdom';

const __dirname = dirname(fileURLToPath(import.meta.url));
const bundle = readFileSync(join(__dirname, '../static/tiptap.bundle.js'), 'utf8');

const dom = new JSDOM(
  `<!doctype html><html><body><div id="host"></div></body></html>`,
  { runScripts: 'dangerously', pretendToBeVisual: true },
);
// Inject and execute the bundle exactly like a <script src> tag would.
const script = dom.window.document.createElement('script');
script.textContent = bundle;
dom.window.document.body.appendChild(script);

const api = dom.window.ElefantWikiEditor;
if (!api || typeof api.createWikiEditor !== 'function') {
  console.error('❌ window.ElefantWikiEditor.createWikiEditor missing from bundle');
  process.exit(1);
}

const host = dom.window.document.getElementById('host');
const md = [
  '# Smoke test',
  '',
  'A paragraph with **bold**, *italic* and a [link](https://example.com).',
  '',
  '- one',
  '- two',
  '',
  '| a | b |',
  '| --- | --- |',
  '| 1 | 2 |',
  '',
  '- [ ] todo',
  '- [x] done',
  '',
].join('\n');

let ok = true;
try {
  const inst = api.createWikiEditor(host, { markdown: md });
  const back = inst.getMarkdown();

  // Editor must have mounted a ProseMirror surface.
  const pm = host.querySelector('.ProseMirror');
  if (!pm) { console.error('❌ no .ProseMirror element mounted'); ok = false; }

  // Content must survive the round-trip (structural checks, not byte-equality).
  const checks = [
    ['heading', /^# Smoke test$/m],
    ['bold', /\*\*bold\*\*/],
    ['link', /\[link\]\(https:\/\/example\.com\)/],
    ['bullets', /^- one$/m],
    ['table row', /\| 1 \| 2 \|/],
    ['task unchecked', /- \[ \] todo/],
    ['task checked', /- \[x\] done/],
  ];
  for (const [name, re] of checks) {
    if (!re.test(back)) { console.error(`❌ round-trip lost: ${name}`); ok = false; }
  }

  // setMarkdown then getMarkdown must be stable.
  inst.setMarkdown(back);
  const back2 = inst.getMarkdown();
  if (back !== back2) { console.error('❌ setMarkdown/getMarkdown not stable'); ok = false; }

  inst.destroy();
} catch (e) {
  console.error('❌ bundle threw:', e && e.stack || e);
  ok = false;
}

console.log(ok
  ? '✅ bundle smoke test passed (mount + round-trip + stability)'
  : '❌ bundle smoke test FAILED');
process.exit(ok ? 0 : 1);
