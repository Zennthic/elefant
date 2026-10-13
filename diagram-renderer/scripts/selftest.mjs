// End-to-end check of the real browser path: a flowchart renders to a PNG and
// an SVG, a broken source comes back with its line number, and the page
// cannot reach the network. The Dockerfile runs this inside the built image,
// so an image whose Chromium and puppeteer-core do not get on fails to build
// rather than failing its first render on a server.
//
//   CHROMIUM_PATH=/path/to/chrome node scripts/selftest.mjs

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createServer } from 'node:http';

process.env.INTERNAL_SERVICE_TOKEN ||= 'selftest-token';
const { render } = await import('../server.mjs');
const { describeError, FONT_STACK } = await import('../lib.mjs');

const flow = 'flowchart TD\n  A[Log in] --> B[Check password]\n  B --> C[Home]';

const png = await render(flow, 'png');
assert.ok(png.ok, JSON.stringify(png.error));
assert.equal(png.png.subarray(0, 8).toString('hex'), '89504e470d0a1a0a', 'not a PNG');
assert.ok(png.png.length > 2000, `suspiciously small PNG (${png.png.length} bytes)`);

const svg = await render(flow, 'svg');
assert.ok(svg.ok && svg.svg.startsWith('<svg') && svg.svg.includes('Check password'));

// Card #413: the shared rich fixture (an identical copy of the validator's,
// compared in CI) draws to a real PNG and an SVG that carries what makes it
// rich: nested subgraph titles, the animated edges and the classDef colours.
const rich = readFileSync(new URL('./fixtures/rich-network.mmd', import.meta.url), 'utf8');
const richPng = await render(rich, 'png');
assert.ok(richPng.ok, `rich fixture PNG: ${JSON.stringify(richPng.error)}`);
assert.equal(richPng.png.subarray(0, 8).toString('hex'), '89504e470d0a1a0a', 'rich fixture: not a PNG');
assert.ok(richPng.png.length > 10000, `rich fixture: suspiciously small PNG (${richPng.png.length} bytes)`);
const richSvg = await render(rich, 'svg');
assert.ok(richSvg.ok && richSvg.svg.startsWith('<svg'), `rich fixture SVG: ${JSON.stringify(richSvg.error)}`);
for (const want of ['Front tier', 'Back tier', 'Data tier', 'Example site', 'edge-animation-fast', '#ffd166']) {
  assert.ok(richSvg.svg.includes(want), `rich fixture SVG is missing ${want}`);
}

// Card #418: the PNG is drawn at the diagram's own size, twice over, not
// squeezed into Chromium's 300px default for an SVG with width="100%".
const pngWidth = (buf) => buf.readUInt32BE(16);
assert.ok(richPng.size && richPng.size.width > 600, `rich fixture laid out at ${JSON.stringify(richPng.size)}`);
assert.ok(pngWidth(richPng.png) >= 2 * richPng.size.width,
  `rich fixture PNG is ${pngWidth(richPng.png)}px wide for a ${richPng.size.width}px diagram`);

// Card #418: Mermaid blanks a themeVariables value with a hyphen in it, so
// this stack reaches the SVG as no font at all. Labels must then inherit the
// page's sans stack (the wiki's), never Chromium's default serif.
const hyphenFont = '---\nconfig:\n  themeVariables:\n    fontFamily: Inter, Arial, sans-serif\n---\n' + flow;
const fontPng = await render(hyphenFont, 'png');
assert.ok(fontPng.ok, JSON.stringify(fontPng.error));
assert.equal(fontPng.font, FONT_STACK.replace("'Segoe UI'", '"Segoe UI"'), `labels drew in ${fontPng.font}`);

const again = await render(flow, 'png');
assert.equal(again.cached, true, 'a repeated render should come from the cache');

const broken = 'flowchart TD\n  A[Log in] --> B[Check password\n  B --> C[Home]';
const bad = await render(broken, 'none');
assert.equal(bad.ok, false);
const err = describeError(bad.error, broken);
assert.equal(err.line, 2, `expected line 2, got ${err.line}: ${err.message}`);

// A label that tries to load something. Strict mode should neutralise it, and
// if anything did try, request interception would refuse it: the listener
// below must never be hit.
let hits = 0;
const trap = createServer((_req, res) => { hits++; res.end(); });
await new Promise((r) => trap.listen(0, '127.0.0.1', r));
const { port } = trap.address();
const sneaky = `flowchart TD\n  A["<img src='http://127.0.0.1:${port}/x'>"] --> B\n  click A href "http://127.0.0.1:${port}/y"`;
const res = await render(sneaky, 'png');
assert.ok(res.ok || res.error, 'render returned nothing');
await new Promise((r) => setTimeout(r, 300));
trap.close();
assert.equal(hits, 0, 'the page reached the network');

console.log('selftest: render (simple and rich fixture), PNG size and font, cache, line-numbered parse error and network isolation all OK');
process.exit(0);
