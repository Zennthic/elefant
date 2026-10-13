// ```mermaid blocks in the wiki reader and the share page (static/mermaid-view.js).
//
// A diagram is stored as Mermaid text in the page; the reader draws it. What
// is checked here:
//   1. a page with no diagram never fetches the (large) mermaid library;
//   2. a diagram block is replaced by mermaid's SVG, with the source kept one
//      click away, and mermaid is always set up with securityLevel "strict";
//   3. a broken diagram says so in words and shows its source, rather than
//      vanishing or leaving mermaid's error graphic behind;
//   4. the library is fetched once and only from our own origin;
//   5. every place that renders page markdown hands it to the viewer;
//   6. the vendored static/mermaid.min.js loads and parses for real;
//   7. the shared rich fixture (card #413) is drawn by the REAL vendored
//      mermaid through the reader, under securityLevel strict, with no
//      console errors: nested subgraph titles, classDef colours, subgraph
//      `style`, edge IDs with `animate`, and front matter config.

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { JSDOM, VirtualConsole } from 'jsdom';

const __dirname = dirname(fileURLToPath(import.meta.url));
const read = (p) => readFileSync(join(__dirname, p), 'utf8');
const viewerSrc = read('../static/mermaid-view.js');
const markedSrc = read('../static/marked.min.js');
const indexSrc = read('../static/index.html');
const shareSrc = read('../static/share.html');

let ok = true;
const fail = (m) => { console.error('❌ ' + m); ok = false; };
const pass = (m) => console.log('✅ ' + m);
const check = (cond, good, bad) => (cond ? pass(good) : fail(bad));

function page(md) {
  const dom = new JSDOM('<!doctype html><html data-theme="dark"><head></head><body><div class="md-body"></div></body></html>',
    { url: 'https://wiki.test/', runScripts: 'outside-only' });
  const win = dom.window;
  win.eval(markedSrc);
  win.eval(viewerSrc);
  const body = win.document.querySelector('.md-body');
  body.innerHTML = win.marked.parse(md);
  return { win, body };
}

function stubMermaid(win) {
  const calls = { init: [], render: [] };
  win.mermaid = {
    initialize: (cfg) => calls.init.push(cfg),
    render: async (id, src) => {
      calls.render.push(src);
      if (src.includes('BROKEN')) throw new Error('Parse error on line 2:\n...');
      return { svg: `<svg id="${id}" data-src="${src.length}"><g></g></svg>` };
    },
  };
  return calls;
}

// ── 1. no diagram, no fetch ────────────────────────────────────────────────
{
  const { win, body } = page('# Title\n\n```js\nconst a = 1;\n```\n');
  const n = await win.renderMermaidBlocks(body);
  check(n === 0 && !win.document.querySelector('script[src="/mermaid.min.js"]'),
    'a page without a mermaid block does not load mermaid',
    'a page without a diagram fetched the mermaid library');
}

// ── 2. a diagram is drawn, strictly ────────────────────────────────────────
{
  const { win, body } = page('Intro\n\n```mermaid\nflowchart TD\n  A[Log in] --> B[Home]\n```\n\nOutro\n');
  const calls = stubMermaid(win);
  const n = await win.renderMermaidBlocks(body);
  const fig = body.querySelector('figure.mermaid-diagram');
  check(n === 1 && fig && fig.querySelector('svg'), 'a mermaid block is replaced by its SVG',
    `expected one drawn diagram, got ${n}: ${body.innerHTML}`);
  check(!body.querySelector('pre > code.language-mermaid:not(details *)') || !!fig.querySelector('details pre'),
    'the source is kept in a collapsed "Mermaid source" section', 'the source was dropped');
  check(calls.render[0].trimEnd() === 'flowchart TD\n  A[Log in] --> B[Home]',
    'mermaid is given the block text exactly', `mermaid got ${JSON.stringify(calls.render[0])}`);
  check(calls.init.length && calls.init.every((c) => c.securityLevel === 'strict' && c.startOnLoad === false),
    'mermaid is initialised with securityLevel strict', `initialize got ${JSON.stringify(calls.init)}`);
  check(calls.init[0].theme === 'dark', 'the dark theme follows data-theme', `theme was ${calls.init[0].theme}`);
  check(body.textContent.includes('Intro') && body.textContent.includes('Outro'),
    'the rest of the page is untouched', 'surrounding content was lost');
}

// ── 2b. a second pass over a drawn page draws nothing more ──────────────────
{
  const { win, body } = page('```mermaid\nflowchart TD\n  A-->B\n```\n');
  stubMermaid(win);
  await win.renderMermaidBlocks(body);
  const again = await win.renderMermaidBlocks(body);
  check(again === 0 && body.querySelectorAll('figure').length === 1,
    'a second pass leaves a drawn diagram (and its source copy) alone',
    `second pass drew ${again}; figures: ${body.querySelectorAll('figure').length}`);
}

// ── 3. a broken diagram is explained, with its source ──────────────────────
{
  const { win, body } = page('```mermaid\nflowchart TD\n  BROKEN[\n```\n');
  stubMermaid(win);
  const n = await win.renderMermaidBlocks(body);
  const fig = body.querySelector('figure.mermaid-error');
  check(n === 0 && fig && /could not be drawn: Parse error on line 2/.test(fig.textContent),
    'a broken diagram says why, in words', `no error shown: ${body.innerHTML}`);
  check(fig && fig.querySelector('details[open] pre code'),
    'and shows its source, expanded', 'the broken source is hidden');
}

// ── 4. fetched lazily, once, from our own origin ───────────────────────────
{
  const { win, body } = page('```mermaid\nflowchart TD\n  A-->B\n```\n\n```mermaid\npie\n  "a": 1\n```\n');
  const pending = win.renderMermaidBlocks(body);
  const pending2 = win.renderMermaidBlocks(body);
  const tags = win.document.querySelectorAll('script[src]');
  check(tags.length === 1 && tags[0].getAttribute('src') === '/mermaid.min.js',
    'the library is requested once, from /mermaid.min.js',
    `script tags: ${[...tags].map((t) => t.getAttribute('src'))}`);
  stubMermaid(win);
  tags[0].dispatchEvent(new win.Event('load'));
  const [a, b] = await Promise.all([pending, pending2]);
  check(a + b === 2 && body.querySelectorAll('figure.mermaid-diagram').length === 2,
    'both diagrams are drawn once it arrives, and neither twice', `drew ${a} + ${b}`);
}

// ── 5. every renderer of page markdown calls the viewer ────────────────────
{
  check(/<script src="\/mermaid-view\.js"><\/script>/.test(indexSrc) && /<script src="\/mermaid-view\.js"><\/script>/.test(shareSrc),
    'index.html and share.html load mermaid-view.js', 'a page does not load mermaid-view.js');
  const fn = (name) => (indexSrc.match(new RegExp(`(?:async )?function ${name}\\([^)]*\\)\\s*\\{[\\s\\S]*?\\n\\}`)) || [''])[0];
  for (const name of ['renderFileView', 'renderWikiPreview', 'viewWikiRevision']) {
    check(fn(name).includes('renderMermaidBlocks('), `${name} draws diagrams`, `${name} never calls renderMermaidBlocks`);
  }
  check(/loadPage[\s\S]*renderMermaidBlocks\(/.test(shareSrc), 'the share page draws diagrams',
    'share.html never calls renderMermaidBlocks');
}

// ── 6. the vendored build is real ──────────────────────────────────────────
{
  const lib = read('../static/mermaid.min.js');
  const pkg = JSON.parse(read('../package.json'));
  const want = pkg.devDependencies.mermaid;
  check(lib.startsWith(`/*! mermaid v${want} `), `static/mermaid.min.js is mermaid ${want}`,
    'static/mermaid.min.js header does not match package.json');
  const dom = new JSDOM('<!doctype html><html><body></body></html>', { runScripts: 'dangerously', pretendToBeVisual: true });
  try {
    // Executed the way a <script src> tag would run it.
    const s = dom.window.document.createElement('script');
    s.textContent = lib;
    dom.window.document.body.appendChild(s);
    const m = dom.window.mermaid;
    m.initialize({ startOnLoad: false, securityLevel: 'strict' });
    const good = await m.parse('flowchart TD\n  A[Log in] --> B[Home]');
    let bad = null;
    try { await m.parse('flowchart TD\n  A --> B[\n  B --> C'); } catch (e) { bad = e; }
    check(good && bad && /Parse error/.test(String(bad.message)),
      'the vendored mermaid parses a good diagram and rejects a broken one',
      `parse results: good=${JSON.stringify(good)} bad=${bad}`);
  } catch (e) {
    fail(`the vendored mermaid did not load in jsdom: ${e.message}`);
  }
}

// ── 7. the rich fixture, drawn for real ─────────────────────────────────────
// The fixture lives with the validator (its image needs it, and a Docker build
// context cannot reach outside the service), and the renderer keeps a copy.
// The corpus page holds it as the second of three blocks, and must hold it
// byte for byte, or this would be testing something nobody else tests.
//
// jsdom has no layout engine, so getBBox is stubbed with a crude text
// measure. Positions are meaningless; what is checked is what mermaid decided
// to draw, which is what a mermaid bump or a strictness change would break.
{
  const fixture = read('../../diagram-validator/scripts/fixtures/rich-network.mmd');
  const copy = read('../../diagram-renderer/scripts/fixtures/rich-network.mmd');
  const corpus = read('corpus/mermaid-rich-network.md');
  const blocks = [...corpus.matchAll(/^```mermaid\n([\s\S]*?)^```$/gm)].map((b) => b[1]);
  check(copy === fixture, 'the renderer\'s copy of the rich fixture matches the validator\'s',
    'diagram-renderer/scripts/fixtures/rich-network.mmd differs from the validator\'s copy');
  check(blocks.length === 3 && blocks[1] === fixture,
    'the corpus page holds the rich fixture, byte for byte, as its second block',
    `corpus page blocks: ${blocks.length}; second matches: ${blocks[1] === fixture}`);

  const noise = [];
  const vc = new VirtualConsole();
  for (const level of ['error', 'warn']) vc.on(level, (...a) => noise.push(`${level}: ${a.join(' ')}`));
  vc.on('jsdomError', (e) => noise.push(`jsdomError: ${e.message}`));
  const dom = new JSDOM('<!doctype html><html data-theme="dark"><head></head><body><div class="md-body"></div></body></html>',
    { url: 'https://wiki.test/', runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc });
  const win = dom.window;
  win.SVGElement.prototype.getBBox = function () {
    return { x: 0, y: 0, width: 8 * (this.textContent || '').length || 10, height: 16 };
  };
  const lib = win.document.createElement('script');
  lib.textContent = read('../static/mermaid.min.js');
  win.document.head.appendChild(lib);   // as the reader's own <script src> would
  const inits = [];
  const realInit = win.mermaid.initialize.bind(win.mermaid);
  win.mermaid.initialize = (cfg) => { inits.push(cfg); return realInit(cfg); };
  win.eval(markedSrc);
  win.eval(viewerSrc);
  const body = win.document.querySelector('.md-body');
  body.innerHTML = win.marked.parse(corpus);
  const n = await win.renderMermaidBlocks(body);

  const figs = [...body.querySelectorAll('figure')];
  check(n === 3 && figs.length === 3 && figs.every((f) => !f.classList.contains('mermaid-error')),
    'all three diagrams on the page are drawn by the vendored mermaid',
    `drew ${n}; errors: ${figs.filter((f) => f.classList.contains('mermaid-error')).map((f) => f.textContent).join(' | ')}`);
  check(inits.length && inits.every((c) => c.securityLevel === 'strict'),
    'and drawn under securityLevel strict', `initialize got ${JSON.stringify(inits)}`);
  check(noise.length === 0, 'drawing the page logs no console errors or warnings', `console: ${noise.join('\n')}`);

  const svg = figs[1] && figs[1].querySelector('svg');
  const css = svg ? [...svg.querySelectorAll('style')].map((s) => s.textContent).join('\n') : '';
  const q = (sel) => (svg ? [...svg.querySelectorAll(sel)] : []);
  const text = (el) => (el.textContent || '').trim();

  const titles = q('g.cluster').map((g) => text(g.querySelector('.cluster-label') || g));
  check(['Example site', 'Front tier', 'Back tier', 'Data tier'].every((t) => titles.includes(t)),
    'the nested subgraph titles are drawn', `cluster titles: ${JSON.stringify(titles)}`);

  const nodeClass = (id) => (q('g.node').find((g) => new RegExp(`-flowchart-${id}-\\d+$`).test(g.id)) || { getAttribute: () => '' })
    .getAttribute('class') || '';
  check(['cdn', 'lb'].every((id) => /\bentry\b/.test(nodeClass(id)))
      && ['db', 'queue'].every((id) => /\bstore\b/.test(nodeClass(id)))
      && ['web1', 'web2', 'api', 'jobs'].every((id) => /\bworker\b/.test(nodeClass(id))),
    '`class` assigns each node its classDef', `node classes: ${q('g.node').map((g) => g.id + '=' + g.getAttribute('class')).join(', ')}`);
  check(/\.entry\s*(?:>|&gt;)\s*\*\s*\{[^}]*fill:\s*#ffd166/.test(css) && /\.store\s*(?:>|&gt;)\s*\*\s*\{[^}]*fill:\s*#06d6a0/.test(css)
      && /\.worker\s*(?:>|&gt;)\s*\*\s*\{[^}]*fill:\s*#118ab2/.test(css),
    'and the classDef colours are in the diagram\'s stylesheet', 'a classDef colour is missing from the SVG <style>');

  const clusterStyle = (id) => ((q('g.cluster').find((g) => g.id.endsWith(`-${id}`)) || { querySelector: () => null })
    .querySelector('rect') || { getAttribute: () => '' }).getAttribute('style') || '';
  check(/fill:\s*#fdf0d5/.test(clusterStyle('front')) && /stroke-dasharray:\s*5 5/.test(clusterStyle('data')),
    '`style` on a subgraph reaches its box', `front: ${clusterStyle('front')}; data: ${clusterStyle('data')}`);

  const animated = q('path').filter((p) => /\bedge-animation-fast\b/.test(p.getAttribute('class') || '')).map((p) => p.id);
  check(animated.length === 2 && animated.every((id) => /-e[12]$/.test(id)),
    'the two edges with `animate: true, animation: fast` carry the animation class, by their edge IDs',
    `animated paths: ${JSON.stringify(animated)}`);
  const dotted = q('path.flowchart-link').filter((p) => /\bedge-pattern-dotted\b/.test(p.getAttribute('class') || ''));
  const thick = q('path.flowchart-link').filter((p) => /\bedge-thickness-thick\b/.test(p.getAttribute('class') || ''));
  const labels = q('.edgeLabel').map(text).filter(Boolean);
  check(dotted.length === 5 && thick.length === 5 && ['HTTPS', 'cache miss', 'round robin', 'enqueue', 'dequeue'].every((l) => labels.includes(l)),
    'dotted and thick edges are drawn as such, with their labels',
    `dotted ${dotted.length}, thick ${thick.length}, labels ${JSON.stringify(labels)}`);

  // Each shape is told apart by its outline (`.label-container`), the way
  // mermaid 11 draws them: a hexagon is a 6-point polygon, a subroutine a
  // 10-point one (the box plus its inner bars), a cylinder a path of arcs,
  // and a stadium a group of rounded paths. A plain box would be a <rect>.
  const outline = (id) => {
    const g = q('g.node').find((n) => new RegExp(`-flowchart-${id}-\\d+$`).test(n.id));
    const c = g && g.querySelector('.label-container');
    if (!c) return 'missing';
    const tag = c.tagName.toLowerCase();
    if (tag === 'polygon') return `polygon${c.getAttribute('points').trim().split(/\s+/).length}`;
    if (tag === 'path') return / a[\d.]/.test(' ' + c.getAttribute('d')) ? 'arcs' : 'path';
    return tag;
  };
  const shapes = { cdn: 'polygon6', lb: 'polygon6', web1: 'polygon10', jobs: 'polygon10',
    db: 'arcs', queue: 'arcs', user: 'g', api: 'g' };
  const got = Object.fromEntries(Object.keys(shapes).map((id) => [id, outline(id)]));
  check(Object.entries(shapes).every(([id, want]) => got[id] === want),
    'hexagon, subroutine, cylinder and stadium nodes are each drawn in their own shape',
    `outlines: ${JSON.stringify(got)}`);

  // Front matter `config` under strict: HONOURED. securityLevel strict
  // guards the secure keys (securityLevel itself, startOnLoad, maxTextSize
  // and the like), not theming, so the fixture's `theme: base` and its
  // themeVariables replace the reader's dark theme for that one diagram,
  // and its `title` is drawn. docs/diagrams.md says so. If a mermaid bump or
  // a config change starts ignoring front matter, this check fails and the
  // docs need to change with it.
  const title = svg && svg.querySelector('.flowchartTitleText');
  check(/\.node rect[^{]*\{[^}]*fill:\s*#2a9d8f/i.test(css) && /e76f51/i.test(css) && title && text(title) === 'Example deployment',
    'front matter config is honoured under strict: theme base, its themeVariables and its title',
    `node fill rule: ${(css.match(/\.node rect[^{]*\{[^}]*\}/) || [''])[0]}; title: ${title && text(title)}`);
}

if (!ok) process.exit(1);
console.log('\nwiki mermaid: all checks passed');
