// Heading anchors in the wiki reader (index.html).
//
// Every heading on a rendered page gets an anchor, so `page.md#heading` links
// to one section, and a page can carry a table of contents. The slug rule is
// shared with the editor (wiki_edit sections, get_wiki_page#anchor) and the
// MCP (search hits); all three are checked against the same fixture,
// tests/fixtures/heading-anchors.json, so a link an agent writes is the link
// the reader resolves.
//
// The helpers are pulled out of index.html and run in a jsdom window with the
// real vendored marked, so what is checked is what the page renders.

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { JSDOM } from 'jsdom';

const __dirname = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(__dirname, '../static/index.html'), 'utf8');
const markedSrc = readFileSync(join(__dirname, '../static/marked.min.js'), 'utf8');
const fixture = JSON.parse(readFileSync(join(__dirname, '../../tests/fixtures/heading-anchors.json'), 'utf8'));

let ok = true;
const fail = (m) => { console.error('❌ ' + m); ok = false; };
const pass = (m) => console.log('✅ ' + m);
const check = (cond, good, bad) => cond ? pass(good) : fail(bad);

const grab = (name) => {
  const m = src.match(new RegExp(`function ${name}\\([^)]*\\)\\s*\\{[\\s\\S]*?\\n\\}`));
  if (!m) fail(`function ${name}() not found in index.html`);
  return m ? m[0] : '';
};
const names = ['headingSlug', 'addHeadingAnchors', '_decodeFrag', 'wikiHref',
               'scrollToWikiAnchor', 'wikiTocPref', 'renderWikiToc', 'parseHash'];

const dom = new JSDOM('<!doctype html><html><body><div id="wiki-viewer"></div></body></html>',
                      { url: 'https://wiki.test/', runScripts: 'outside-only' });
const win = dom.window;
win.eval(markedSrc);
const toasts = [];
win.toast = (m) => toasts.push(m);
win.Element.prototype.scrollIntoView = function () { win.__scrolled = this; };
win.eval(`const WIKI_TOC_MIN = ${(src.match(/const WIKI_TOC_MIN = (\d+)/) || [0, 4])[1]};\n`
  + names.map(grab).join('\n')
  + `\nwindow.api = { ${names.join(', ')} };`);
const api = win.api;

// What renderFileView does with a page body.
const render = (md) => {
  const div = win.document.createElement('div');
  div.className = 'md-body';
  div.innerHTML = win.marked.parse(md.replace(/^---[\s\S]*?---\n?/, ''));
  return div;
};

// ── 1. The shared fixture ─────────────────────────────────────────────────

let bad = fixture.headings.filter(([raw, want]) => {
  const got = api.addHeadingAnchors(render('## ' + raw)).map(h => h.anchor);
  if (got.length !== 1 || got[0] !== want) { console.error(`   "## ${raw}" → ${JSON.stringify(got)}, want "${want}"`); return true; }
  return false;
});
check(!bad.length, `all ${fixture.headings.length} fixture headings get the same anchor as the editor and MCP`,
  `${bad.length} fixture heading(s) anchor differently in the reader`);

bad = fixture.pages.filter(p => {
  const got = api.addHeadingAnchors(render(p.markdown)).map(h => h.anchor);
  if (JSON.stringify(got) !== JSON.stringify(p.anchors)) { console.error(`   page → ${JSON.stringify(got)}, want ${JSON.stringify(p.anchors)}`); return true; }
  return false;
});
check(!bad.length, 'repeated headings are counted -1, -2, … in page order, and code/frontmatter are skipped',
  'whole-page anchors disagree with the fixture');

// ── 2. Ids cannot collide with the app's own ──────────────────────────────

const clash = render('## Wiki viewer\n');
api.addHeadingAnchors(clash);
const h = clash.querySelector('h2');
check(h.id === 'wiki-h-wiki-viewer' && h.dataset.anchor === 'wiki-viewer',
  'the DOM id is prefixed, so a heading called "Wiki viewer" is not a second #wiki-viewer',
  `heading id is "${h.id}" — it can shadow an element of the app`);

// ── 3. Scrolling to a section, and failing soft ───────────────────────────

const page = render('# Runbook\n\n## Steps\n\n## Steps\n\n## Rollback Plan\n');
api.addHeadingAnchors(page);
const target = (a) => { win.__scrolled = null; api.scrollToWikiAnchor(a, { container: page }); return win.__scrolled && win.__scrolled.dataset.anchor; };
check(target('steps-1') === 'steps-1', 'an exact anchor scrolls to its heading', 'steps-1 did not scroll');
check(target('Rollback%20Plan') === 'rollback-plan' && target('#Rollback Plan') === 'rollback-plan',
  'a hand-written or percent-encoded fragment is re-slugged and still lands',
  'a hand-written fragment did not resolve');
toasts.length = 0;
check(target('renamed-long-ago') === null && toasts.length === 1 && /renamed/.test(toasts[0]),
  'a missing anchor stays at the top and says the section may have been renamed',
  'a missing anchor scrolled somewhere or failed silently');

// ── 4. The URL hash carries the section ───────────────────────────────────

const hashOf = (h) => { win.location.hash = h; return win.api.parseHash(); };
let r = hashOf(api.wikiHref('AI/runbooks/db restore.md', 'rollback-plan'));
check(r.tab === 'wiki' && r.path === 'AI/runbooks/db restore.md' && r.anchor === 'rollback-plan',
  'wikiHref() and parseHash() round-trip a page and its section',
  `round trip gave ${JSON.stringify(r)}`);
r = hashOf('#wiki/' + encodeURIComponent('AI/x.md#steps-1'));
check(r.path === 'AI/x.md' && r.anchor === 'steps-1',
  'a card link that encoded `page.md#anchor` as one path still opens the section',
  `encoded %23 gave ${JSON.stringify(r)}`);
r = hashOf(api.wikiHref('a.md', 'café-résumé'));
check(r.anchor === 'café-résumé', 'a non-ASCII anchor survives the hash', `got ${JSON.stringify(r)}`);
r = hashOf('#wiki/' + encodeURIComponent('AI/x.md'));
check(r.path === 'AI/x.md' && r.anchor === '', 'a plain page hash is unchanged', `got ${JSON.stringify(r)}`);
r = hashOf('#settings');
check(r.tab === 'settings' && r.path === '' && r.anchor === '', 'a bare tab hash is unchanged', `got ${JSON.stringify(r)}`);

// ── 5. Table of contents ──────────────────────────────────────────────────

const tocOf = (md, raw) => {
  const body = render(md);
  api.renderWikiToc(body, api.addHeadingAnchors(body), 'p.md', raw ?? md);
  return body;
};
const long = '# Title\n\nintro\n\n## One\n\n## Two\n\n### Two A\n\n## Three\n\n## Four\n';
let body = tocOf(long);
let toc = body.querySelector('nav.wiki-toc');
check(toc && [...toc.querySelectorAll('a')].map(a => a.textContent).join('|') === 'One|Two|Two A|Three|Four',
  'a long page gets a TOC of its sections, leaving out the page-title h1',
  `TOC was ${toc ? toc.textContent : 'missing'}`);
check(toc && body.querySelector('h1').nextElementSibling === toc,
  'the automatic TOC sits under the title', 'the TOC is not placed under the title');
check(toc && toc.querySelector('a').getAttribute('href') === '#wiki/p.md#one',
  'TOC entries link to page#anchor', `TOC href was ${toc && toc.querySelector('a').getAttribute('href')}`);
check(toc && toc.querySelectorAll('li.toc-d1').length === 1,
  'nested headings are indented under their parent', 'TOC depth classes are wrong');

check(!tocOf('# T\n\n## One\n\n## Two\n').querySelector('.wiki-toc'),
  'a short page gets no TOC unasked', 'a two-section page grew a TOC');
check(!!tocOf('# T\n\n## One\n', '---\ntoc: true\n---\n# T\n\n## One\n').querySelector('.wiki-toc'),
  '`toc: true` in the frontmatter asks for one anyway', 'toc: true was ignored');
check(!tocOf(long, '---\ntoc: false\n---\n' + long).querySelector('.wiki-toc'),
  '`toc: false` turns the automatic TOC off', 'toc: false was ignored');

body = tocOf('# T\n\nintro\n\n[[toc]]\n\n## One\n\n## Two\n');
toc = body.querySelector('.wiki-toc');
check(toc && toc.previousElementSibling && toc.previousElementSibling.textContent === 'intro'
      && !/\[\[toc\]\]/i.test(body.textContent),
  'a `[[toc]]` line places the TOC exactly there, on any length of page',
  'the [[toc]] marker was not replaced in place');
check(!!tocOf('## A\n\n[TOC]\n\n## B\n').querySelector('.wiki-toc'),
  '`[TOC]` works as the marker too', '[TOC] was not recognised');

// ── 6. Wiring (structural) ────────────────────────────────────────────────

const view = (src.match(/function renderFileView\(\)\s*\{[\s\S]*?\n\}/) || [''])[0];
check(/addHeadingAnchors\(mdBody\)/.test(view) && /renderWikiToc\(/.test(view) && /addHeadingLinks\(/.test(view),
  'the read view anchors headings, builds the TOC and adds the hover links',
  'renderFileView() does not wire the anchors up');
check(/addHeadingAnchors\(pre\)/.test(grab('renderWikiPreview')),
  'the split-mode preview anchors headings too', 'the preview has no anchors');
const load = grab('loadFileContent');
check(/opts\.anchor/.test(load) && /scrollToWikiAnchor/.test(load),
  'opening a page with an anchor scrolls to it', 'loadFileContent() ignores the anchor');
const links = grab('resolveWikiLinks');
check(/raw\.startsWith\('#'\)/.test(links) && /openWikiPage\(resolved, anchor\)/.test(links),
  'in-page `#heading` and cross-page `page.md#heading` links both resolve',
  'resolveWikiLinks() drops the fragment');
check(/\.heading-anchor::before\s*\{\s*content:\s*"#"/.test(src),
  'the hover "#" is drawn by CSS, so it adds nothing to the heading text',
  'the heading link adds text to the heading');

if (!ok) { console.error('\nwiki-heading-anchors: FAILED'); process.exit(1); }
console.log('\nwiki-heading-anchors: all checks passed');
