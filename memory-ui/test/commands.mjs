// Verify every toolbar command the SPA calls (wikiCmd in index.html) exists on
// the committed bundle's editor, and that the block-level commands actually
// transform the document. Catches a missing-extension regression, plus the
// onUpdate-listener crash, before either ships to stage.
//
// NOTE ON SCOPE: mark toggles (bold/italic/strike/code) need a DOM text
// selection, which jsdom syncs incompletely — so we assert their PRESENCE here
// and rely on roundtrip.mjs to prove **bold** / *italic* / ~~strike~~ / `code`
// serialize correctly. Block commands run on the cursor and are fully exercised.

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { JSDOM } from 'jsdom';

const __dirname = dirname(fileURLToPath(import.meta.url));
const bundle = readFileSync(join(__dirname, '../static/tiptap.bundle.js'), 'utf8');

const dom = new JSDOM(`<!doctype html><html><body><div id="host"></div></body></html>`, {
  runScripts: 'dangerously', pretendToBeVisual: true,
});
const script = dom.window.document.createElement('script');
script.textContent = bundle;
dom.window.document.body.appendChild(script);

const host = dom.window.document.getElementById('host');
const inst = dom.window.ElefantWikiEditor.createWikiEditor(host, { markdown: 'seed text\n' });
const ed = inst.editor;

let ok = true;
const fail = (m) => { console.error('❌ ' + m); ok = false; };

// 1) Presence: every command wikiCmd() invokes must exist.
const ALL = [
  'toggleHeading', 'toggleBold', 'toggleItalic', 'toggleStrike', 'toggleCode',
  'toggleBulletList', 'toggleOrderedList', 'toggleTaskList', 'toggleBlockquote',
  'toggleCodeBlock', 'setHorizontalRule', 'insertTable', 'undo', 'redo',
  // Phase 2 insert commands (links + image).
  'setLink', 'unsetLink', 'setImage',
];
for (const name of ALL) {
  if (typeof ed.commands[name] !== 'function') fail(`command missing: ${name}`);
}

// 2) Functional (block-level, headless-safe): reset, run on the cursor, assert md.
const BLOCK = [
  ['toggleHeading{1}', () => ed.chain().toggleHeading({ level: 1 }).run(), /^# seed text$/m],
  ['toggleHeading{3}', () => ed.chain().toggleHeading({ level: 3 }).run(), /^### seed text$/m],
  ['toggleBulletList', () => ed.chain().toggleBulletList().run(), /^- seed text$/m],
  ['toggleOrderedList', () => ed.chain().toggleOrderedList().run(), /^1\. seed text$/m],
  ['toggleTaskList', () => ed.chain().toggleTaskList().run(), /^- \[[ x]\] seed text$/m],
  ['toggleBlockquote', () => ed.chain().toggleBlockquote().run(), /^> seed text$/m],
  ['toggleCodeBlock', () => ed.chain().toggleCodeBlock().run(), /^```/m],
  ['setHorizontalRule', () => ed.chain().setHorizontalRule().run(), /^---$/m],
  ['insertTable', () => ed.chain().insertTable({ rows: 2, cols: 2, withHeaderRow: true }).run(), /\|.*\|/],
];
for (const [name, act, expect] of BLOCK) {
  try {
    ed.commands.setContent('seed text\n');   // emitUpdate=false → no crash, clean reset
    act();
    const md = inst.getMarkdown();
    if (!expect.test(md)) fail(`${name}: expected ${expect} → got ${JSON.stringify(md)}`);
  } catch (e) {
    fail(`${name} threw: ${e && e.message || e}`);
  }
}

// 3) undo/redo must run without throwing.
try {
  ed.commands.setContent('seed text\n');
  ed.chain().toggleBulletList().run();
  ed.chain().undo().run();
  ed.chain().redo().run();
} catch (e) { fail(`undo/redo threw: ${e && e.message || e}`); }

inst.destroy();
console.log(ok
  ? '✅ toolbar commands present; block commands transform correctly'
  : '❌ toolbar command check FAILED');
process.exit(ok ? 0 : 1);
