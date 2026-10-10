// Phase 0 spike: prove markdown ⇆ ProseMirror is a STABLE round-trip.
//
// Gate: for every corpus page, md → doc → md → doc must be a fixed point —
//   docStable  (doc1 deep-equals doc2)   AND
//   mdStable   (md2 === md3)
// If both hold for every page, saving a page never churns unrelated lines and
// whatever the WYSIWYG shows is exactly what we persist. That is the make-or-break
// for the whole initiative.
//
// It also reports, per page, which *extended* markdown constructs (footnotes,
// ==highlight==, ~sub~, etc.) don't survive the initial parse — the known-lossy
// set that Source mode / custom nodes must cover later. Fidelity of SUPPORTED
// constructs is checked by eyeballing the md1↔md2 diff this prints.

import { readFileSync, readdirSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { isDeepStrictEqual } from 'node:util';

// ---- jsdom globals so TipTap (a browser editor) can run headless ----
import { JSDOM } from 'jsdom';
const dom = new JSDOM('<!doctype html><html><body></body></html>', {
  pretendToBeVisual: true,
});
globalThis.window = dom.window;
globalThis.document = dom.window.document;
// Node 22 ships a read-only global `navigator`; override it with jsdom's.
Object.defineProperty(globalThis, 'navigator', {
  value: dom.window.navigator, configurable: true, writable: true,
});
globalThis.DOMParser = dom.window.DOMParser;
globalThis.Node = dom.window.Node;
globalThis.HTMLElement = dom.window.HTMLElement;
globalThis.getComputedStyle = dom.window.getComputedStyle;
if (!dom.window.requestAnimationFrame) {
  dom.window.requestAnimationFrame = (cb) => setTimeout(cb, 0);
  dom.window.cancelAnimationFrame = (id) => clearTimeout(id);
}
globalThis.requestAnimationFrame = dom.window.requestAnimationFrame;
globalThis.cancelAnimationFrame = dom.window.cancelAnimationFrame;

import { Editor } from '@tiptap/core';
import { wikiExtensions } from '../src/editor-extensions.mjs';

const __dirname = dirname(fileURLToPath(import.meta.url));
const CORPUS = join(__dirname, 'corpus');

// Strip a leading YAML frontmatter block; return the body only. The editor
// never renders frontmatter — Phase 1 splits it into a meta bar and re-emits it.
function stripFrontmatter(raw) {
  const m = raw.match(/^---\n[\s\S]*?\n---\n?/);
  return m ? raw.slice(m[0].length) : raw;
}

// Extended-syntax markers CommonMark/TipTap won't model natively.
const LOSSY_MARKERS = [
  ['footnote-ref', /\[\^\w+\]/],
  ['footnote-def', /^\[\^\w+\]:/m],
  ['highlight ==', /==[^=]+==/],
  ['subscript ~x~', /(?<!~)~[^~\s]+~(?!~)/],
  ['superscript ^x^', /\^[^\s^]+\^/],
  ['definition-list', /^: /m],
  ['heading-id {#}', /\{#[\w-]+\}/],
  ['emoji :shortcode:', /:[a-z_]+:/],
];

function detectLossy(md) {
  return LOSSY_MARKERS.filter(([, re]) => re.test(md)).map(([name]) => name);
}

// Tiny line diff for eyeballing what the serializer changed.
function miniDiff(a, b, max = 24) {
  const A = a.split('\n'), B = b.split('\n');
  const out = [];
  const n = Math.max(A.length, B.length);
  for (let i = 0; i < n; i++) {
    if (A[i] !== B[i]) {
      if (A[i] !== undefined) out.push(`  - ${JSON.stringify(A[i])}`);
      if (B[i] !== undefined) out.push(`  + ${JSON.stringify(B[i])}`);
    }
    if (out.length >= max) { out.push('  … (diff truncated)'); break; }
  }
  return out.join('\n');
}

// Pages already written in the editor's own output form. For these the first
// save must give the page back byte for byte, not just settle on a fixed point
// after one rewrite: they pin serialization itself, so a dependency bump that
// changes how a table, task list or hard break is written fails here.
const EXACT = new Set(['tiptap-v3-fixed-point.md']);

const editor = new Editor({ extensions: wikiExtensions() });

const files = readdirSync(CORPUS).filter((f) => f.endsWith('.md')).sort();
let allStable = true;
const report = [];

for (const file of files) {
  const raw = readFileSync(join(CORPUS, file), 'utf8');
  const body = stripFrontmatter(raw);

  editor.commands.setContent(body);
  const doc1 = editor.getJSON();
  const md2 = editor.storage.markdown.getMarkdown();

  editor.commands.setContent(md2);
  const doc2 = editor.getJSON();
  const md3 = editor.storage.markdown.getMarkdown();

  const docStable = isDeepStrictEqual(doc1, doc2);
  const mdStable = md2 === md3;
  const exact = !EXACT.has(file) || md2.replace(/\n+$/, '') === body.replace(/\n+$/, '');
  const stable = docStable && mdStable && exact;
  if (!stable) allStable = false;

  report.push({ file, stable, docStable, mdStable, exact, body, md2, md3, lossy: detectLossy(body) });
}

// ---- print report ----
console.log('\n=== Phase 0 round-trip report ===\n');
for (const r of report) {
  const badge = r.stable ? '✅ STABLE' : '❌ UNSTABLE';
  console.log(`${badge}  ${r.file}   (docStable=${r.docStable} mdStable=${r.mdStable})`);
  if (r.lossy.length) console.log(`   ⚠️  extended syntax not modeled: ${r.lossy.join(', ')}`);
  if (!r.exact) {
    console.log('   page → md2 changed (this page must re-save byte for byte):');
    console.log(miniDiff(r.body.replace(/\n+$/, ''), r.md2.replace(/\n+$/, '')));
  }
  if (!r.mdStable) {
    console.log('   md2 → md3 churn (serializer not idempotent):');
    console.log(miniDiff(r.md2, r.md3));
  }
}

// ---- print the body→md2 diff for manual fidelity inspection ----
if (process.env.SHOW_DIFF) {
  console.log('\n=== body → serialized (md2) diffs, for manual inspection ===');
  for (const r of report) {
    console.log(`\n--- ${r.file} ---`);
    const d = miniDiff(r.body.replace(/\n+$/, ''), r.md2.replace(/\n+$/, ''), 200);
    console.log(d || '  (identical)');
  }
}

editor.destroy();

console.log('\n' + (allStable
  ? '✅ GATE PASSED: every page is a stable re-save fixed point.'
  : '❌ GATE FAILED: at least one page churns on re-save (see above).') + '\n');

process.exit(allStable ? 0 : 1);
