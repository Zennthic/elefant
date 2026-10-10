// WikiImage contract: the stored (markdown) src stays relative so serialization
// is unchanged, while the in-editor DOM <img> is resolved to /api/image?path=…
// so a relative wiki image actually previews. External/data srcs pass through.

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { JSDOM } from 'jsdom';

const __dirname = dirname(fileURLToPath(import.meta.url));
const bundle = readFileSync(join(__dirname, '../static/tiptap.bundle.js'), 'utf8');

const dom = new JSDOM(`<!doctype html><html><body><div id="host"></div></body></html>`, {
  runScripts: 'dangerously', pretendToBeVisual: true,
});
const s = dom.window.document.createElement('script');
s.textContent = bundle;
dom.window.document.body.appendChild(s);

const host = dom.window.document.getElementById('host');
const inst = dom.window.ElefantWikiEditor.createWikiEditor(host, {
  markdown: 'x\n', filePath: 'AI/projects/elefant/foo.md',
});
const ed = inst.editor;

// Look up the image through its node view wrapper. Since Tiptap 3,
// prosemirror-view puts a zero-size `img.ProseMirror-separator` in front of an
// inline node view, so a bare `img` selector finds that instead.
let ok = true;
const fail = (m) => { console.error('❌ ' + m); ok = false; };

// Relative image: markdown keeps it relative; DOM resolves to /api/image.
ed.commands.setContent('');
ed.chain().setImage({ src: '../../../assets/images/pic.png' }).run();
const relMd = inst.getMarkdown();
if (!/!\[\]\(\.\.\/\.\.\/\.\.\/assets\/images\/pic\.png\)/.test(relMd)) fail(`relative src not preserved in md: ${JSON.stringify(relMd)}`);
const relImg = host.querySelector('.wiki-img-wrap img')?.getAttribute('src');
if (relImg !== '/api/image?path=' + encodeURIComponent('assets/images/pic.png')) fail(`DOM src not resolved to /api/image: ${relImg}`);

// External image: unchanged in md and DOM.
ed.commands.setContent('');
ed.chain().setImage({ src: 'https://x.example/y.png' }).run();
if (!/https:\/\/x\.example\/y\.png/.test(inst.getMarkdown())) fail('external md changed');
if (host.querySelector('.wiki-img-wrap img')?.getAttribute('src') !== 'https://x.example/y.png') fail('external DOM src changed');

// Re-serialize stability with an image present.
ed.commands.setContent('para\n\n![](../a/b.png)\n\ntail\n');
const m1 = inst.getMarkdown();
inst.setMarkdown(m1);
const m2 = inst.getMarkdown();
if (m1 !== m2) fail(`image round-trip not stable: ${JSON.stringify(m1)} vs ${JSON.stringify(m2)}`);

// Resize width persists via the Obsidian ![alt|width](src) convention, round-trips,
// and the "|NNN" suffix never leaks into the alt text or the DOM image width.
inst.setMarkdown('![diagram|240](../assets/images/d.png)\n');
const wMd = inst.getMarkdown();
if (!/!\[diagram\|240\]\(\.\.\/assets\/images\/d\.png\)/.test(wMd)) fail(`width not round-tripped: ${JSON.stringify(wMd)}`);
const wImg = host.querySelector('.wiki-img-wrap img');
if (wImg?.getAttribute('alt') !== 'diagram') fail(`alt leaked width suffix: ${wImg?.getAttribute('alt')}`);
if (wImg?.style.width !== '240px') fail(`DOM width not applied: ${wImg?.style.width}`);
// No-width image must NOT gain a |suffix.
inst.setMarkdown('![plain](../a/b.png)\n');
if (inst.getMarkdown().trim() !== '![plain](../a/b.png)') fail(`no-width image changed: ${JSON.stringify(inst.getMarkdown())}`);

inst.destroy();
console.log(ok ? '✅ WikiImage: relative src preserved, DOM resolved, external passthrough, stable' : '❌ WikiImage check FAILED');
process.exit(ok ? 0 : 1);
