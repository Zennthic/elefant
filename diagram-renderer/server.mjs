// Elefant diagram renderer: parses and renders Mermaid in headless Chromium.
//
// An OPTIONAL sidecar of the MCP server. It exists as its own image so the
// core images stay light: Chromium is several hundred megabytes, and a
// deployment that does not want server-side rendering simply leaves this
// service out. The MCP diagram tools then still store and patch diagrams, and
// the wiki still draws them in the browser; only the image an agent gets back
// (and Mermaid's own validation on write) needs this.
//
//   GET  /health   {"status":"ok"}. No browser work, so a liveness probe
//                  cannot be failed by a slow render.
//   POST /render   {"source": "...", "format": "png"|"svg"|"none"}
//                  200 {"ok":true,"format","mime","data","sha256","cached"}
//                      data is base64 for png, SVG text for svg, absent for none
//                  422 {"ok":false,"error":{"message","line"}}  Mermaid said no
//                  400 bad request · 401 missing peer token · 413 too big
//                  503 the browser could not be started
//
// HOW IT IS CONTAINED. Diagram source comes from agents and from whatever
// they were shown, so it is treated as hostile:
//   - Mermaid runs with securityLevel "strict": labels are sanitised, click
//     handlers and scripts in the diagram are refused.
//   - The page can fetch nothing. Every request except the page itself is
//     aborted, and Chromium is pointed at a dead proxy and a resolver that
//     resolves nothing, so even a request that slipped past goes nowhere.
//     In Compose the container is also on an internal network with no route
//     out; in Kubernetes a NetworkPolicy denies egress.
//   - Each render gets a fresh browser context (no shared cookies, storage
//     or cache) that is closed afterwards, and a hard deadline. A render
//     that does not finish in time has its context, and if need be the whole
//     browser, killed.
//   - The container runs as a non-root UID with a read-only root filesystem
//     and no capabilities. Chromium's own setuid/namespace sandbox cannot
//     start without privileges the container deliberately lacks, so it runs
//     with --no-sandbox and the container is the sandbox.
//   - Only peer Elefant services may call it: the X-Internal-Service header
//     must carry INTERNAL_SERVICE_TOKEN, as for every other internal service.

import http from 'node:http';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { timingSafeEqual } from 'node:crypto';
import puppeteer from 'puppeteer-core';
import {
  checkRequest, cacheKey, Lru, describeError, deadline, limiter, MAX_SOURCE_BYTES, FONT_STACK,
} from './lib.mjs';

const PORT = Number(process.env.PORT || 8004);
const CHROMIUM_PATH = process.env.CHROMIUM_PATH || '/usr/bin/chromium';
const RENDER_TIMEOUT_MS = Number(process.env.RENDER_TIMEOUT_MS || 15000);
const CONCURRENCY = Number(process.env.RENDER_CONCURRENCY || 2);
const CACHE_ENTRIES = Number(process.env.RENDER_CACHE_ENTRIES || 256);
const TOKEN = (process.env.INTERNAL_SERVICE_TOKEN || '').trim();

const require = createRequire(import.meta.url);
const MERMAID_JS = readFileSync(require.resolve('mermaid/dist/mermaid.min.js'), 'utf8');
const MERMAID_VERSION = JSON.parse(
  readFileSync(require.resolve('mermaid/package.json'), 'utf8')).version;

// #out is as wide as the diagram, not the viewport: see sizeForScreenshot.
const SHELL = '<!doctype html><html><head><meta charset="utf-8">'
  + `<style>html,body{margin:0;padding:0;background:#fff;font-family:${FONT_STACK}}`
  + '#out{display:inline-block;width:max-content;padding:16px}</style>'
  + '</head><body><div id="out"></div></body></html>';

// PNGs are taken at twice the diagram's own size, so 16px labels come out as
// 32 device pixels. A diagram wider than this many CSS pixels is scaled down
// to it, which keeps the PNG under Chromium's screenshot size limit.
const DEVICE_SCALE = 2;
const MAX_CSS_WIDTH = 6000;

const CHROMIUM_ARGS = [
  '--no-sandbox',                       // see HOW IT IS CONTAINED above
  '--disable-dev-shm-usage',
  '--disable-gpu',
  '--no-first-run',
  '--no-default-browser-check',
  '--disable-extensions',
  '--disable-background-networking',
  '--disable-component-update',
  '--disable-default-apps',
  '--disable-sync',
  '--mute-audio',
  // Belt and braces with the request interception below: nothing resolves,
  // and anything that tries anyway is sent to a port nobody listens on.
  '--proxy-server=http://127.0.0.1:9',
  '--proxy-bypass-list=<-loopback>',
  '--host-resolver-rules=MAP * ~NOTFOUND',
];

const log = (level, msg, extra = {}) =>
  console.log(JSON.stringify({ level, msg, ...extra, ts: new Date().toISOString() }));

if (!TOKEN) {
  log('critical', 'INTERNAL_SERVICE_TOKEN is not set: every render request will be refused');
}

// ── the browser ─────────────────────────────────────────────────────────────

let browserPromise = null;

function getBrowser() {
  if (!browserPromise) {
    browserPromise = puppeteer.launch({
      executablePath: CHROMIUM_PATH,
      headless: true,
      args: CHROMIUM_ARGS,
    }).then((b) => {
      b.on('disconnected', () => {
        log('warn', 'browser disconnected; it will be relaunched on the next render');
        browserPromise = null;
      });
      return b;
    }, (err) => {
      browserPromise = null;
      throw err;
    });
  }
  return browserPromise;
}

async function killBrowser() {
  const p = browserPromise;
  browserPromise = null;
  if (!p) return;
  try {
    const b = await p;
    b.process()?.kill('SIGKILL');
  } catch { /* already gone */ }
}

// Runs inside the page. Returns { ok, svg } or { ok:false, error }.
async function renderInPage(source) {
  /* global mermaid, document */
  mermaid.initialize({ startOnLoad: false, securityLevel: 'strict', theme: 'default' });
  try {
    await mermaid.parse(source);
    const { svg } = await mermaid.render('elefant-diagram', source);
    const out = document.getElementById('out');
    out.innerHTML = svg;
    const label = out.querySelector('.nodeLabel, .label, text');
    return { ok: true, svg, font: label ? getComputedStyle(label).fontFamily : null };
  } catch (e) {
    return {
      ok: false,
      error: { name: e?.name || 'Error', message: String(e?.message || e), loc: e?.hash?.loc || null },
    };
  }
}

// Runs inside the page, before a PNG is taken. Mermaid gives its SVG
// width="100%" and a max-width, which inside a shrink-to-fit box resolves to
// Chromium's 300px default for a replaced element: a large diagram came back
// squeezed to a few hundred pixels with unreadable labels. Draw it at the
// size its viewBox says instead. The SVG text returned to callers is
// Mermaid's own and is not changed.
function sizeForScreenshot(maxWidth) {
  /* global document */
  const svg = document.querySelector('#out svg');
  const vb = svg?.viewBox?.baseVal;
  if (!vb || !vb.width || !vb.height) return null;
  const scale = Math.min(1, maxWidth / vb.width);
  const width = Math.ceil(vb.width * scale);
  const height = Math.ceil(vb.height * scale);
  svg.setAttribute('width', String(width));
  svg.setAttribute('height', String(height));
  svg.style.maxWidth = 'none';
  return { width, height };
}

async function renderOnce(source, format) {
  const browser = await getBrowser();
  const context = await browser.createBrowserContext();
  const limit = deadline(RENDER_TIMEOUT_MS, 'render');
  const work = (async () => {
    const page = await context.newPage();
    await page.setRequestInterception(true);
    page.on('request', (req) => {
      const url = req.url();
      if (url === 'about:blank' || url.startsWith('data:')) req.continue();
      else req.abort('blockedbyclient');
    });
    await page.setViewport({ width: 1600, height: 1200, deviceScaleFactor: DEVICE_SCALE });
    await page.setContent(SHELL);
    await page.addScriptTag({ content: MERMAID_JS });
    const res = await page.evaluate(renderInPage, source);
    if (!res.ok) return res;
    if (format !== 'png') return { ok: true, svg: res.svg, font: res.font };
    const size = await page.evaluate(sizeForScreenshot, MAX_CSS_WIDTH);
    const el = await page.$('#out');
    const png = await el.screenshot({ type: 'png', omitBackground: false });
    return { ok: true, svg: res.svg, png: Buffer.from(png), font: res.font, size };
  })();
  try {
    return await Promise.race([work, limit.promise]);
  } catch (err) {
    if (err?.timeout) {
      // A render that will not stop can hold the browser; take it all down.
      await Promise.race([context.close().catch(() => {}), new Promise((r) => setTimeout(r, 1000))]);
      await killBrowser();
    }
    throw err;
  } finally {
    limit.cancel();
    context.close().catch(() => {});
  }
}

const cache = new Lru(CACHE_ENTRIES);
const runLimited = limiter(CONCURRENCY);

export async function render(source, format) {
  // Validation is a render: Mermaid errors that only show up at layout time
  // (not at parse time) are errors too. "none" shares the SVG's cache entry.
  const fmt = format === 'none' ? 'svg' : format;
  const key = cacheKey(source, fmt);
  const hit = cache.get(key);
  if (hit) return { ...hit, cached: true };
  const res = await runLimited(() => renderOnce(source, fmt));
  if (!res.ok) return res;
  const out = { ok: true, sha256: key, svg: res.svg, png: res.png, font: res.font, size: res.size };
  cache.set(key, out);
  return { ...out, cached: false };
}

// ── HTTP ────────────────────────────────────────────────────────────────────

function send(res, status, body) {
  const data = JSON.stringify(body);
  res.writeHead(status, { 'content-type': 'application/json', 'content-length': Buffer.byteLength(data) });
  res.end(data);
}

function authorised(req) {
  if (!TOKEN) return false;
  const got = Buffer.from(String(req.headers['x-internal-service'] || ''));
  const want = Buffer.from(TOKEN);
  return got.length === want.length && timingSafeEqual(got, want);
}

function readBody(req, max) {
  return new Promise((resolve, reject) => {
    let size = 0;
    const chunks = [];
    req.on('data', (c) => {
      size += c.length;
      if (size > max) {
        reject(Object.assign(new Error('too large'), { status: 413 }));
        req.destroy();
      } else chunks.push(c);
    });
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    req.on('error', reject);
  });
}

export const server = http.createServer(async (req, res) => {
  try {
    if (req.method === 'GET' && req.url === '/health') {
      return send(res, 200, { status: 'ok', mermaid: MERMAID_VERSION });
    }
    if (req.url !== '/render') return send(res, 404, { error: 'not found' });
    if (req.method !== 'POST') return send(res, 405, { error: 'POST only' });
    if (!authorised(req)) return send(res, 401, { error: 'peer token required' });

    let body;
    try {
      body = JSON.parse(await readBody(req, MAX_SOURCE_BYTES * 2 + 1024));
    } catch (err) {
      return send(res, err?.status || 400, { error: err?.status ? 'request too large' : 'invalid JSON' });
    }
    const checked = checkRequest(body);
    if (checked.error) return send(res, 400, { error: checked.error });
    const { source, format } = checked;

    let out;
    try {
      out = await render(source, format);
    } catch (err) {
      if (err?.timeout) {
        return send(res, 422, { ok: false, error: {
          message: `Rendering took longer than ${RENDER_TIMEOUT_MS / 1000}s and was stopped. `
            + 'Simplify the diagram or split it in two.', line: null } });
      }
      log('error', 'render failed', { error: String(err?.message || err) });
      return send(res, 503, { error: 'renderer unavailable' });
    }
    if (!out.ok) return send(res, 422, { ok: false, error: describeError(out.error, source) });
    const reply = { ok: true, format, sha256: out.sha256, cached: out.cached };
    if (format === 'png') Object.assign(reply, { mime: 'image/png', data: out.png.toString('base64') });
    if (format === 'svg') Object.assign(reply, { mime: 'image/svg+xml', data: out.svg });
    return send(res, 200, reply);
  } catch (err) {
    log('error', 'request failed', { error: String(err?.message || err) });
    if (!res.headersSent) send(res, 500, { error: 'internal error' });
  }
});

if (process.argv[1] && import.meta.url === new URL(`file://${process.argv[1]}`).href) {
  server.listen(PORT, '0.0.0.0', () => log('info', 'diagram renderer listening', { port: PORT, mermaid: MERMAID_VERSION }));
  const stop = async () => {
    server.close();
    await killBrowser();
    process.exit(0);
  };
  process.on('SIGTERM', stop);
  process.on('SIGINT', stop);
}
