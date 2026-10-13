// Elefant diagram validator: parses Mermaid for the MCP diagram tools, with
// no browser.
//
// The MCP server asks this before it saves any diagram, so a source Mermaid
// rejects is refused with its line number instead of being stored. It is a
// small, always-on service. The optional diagram-renderer (headless Chromium)
// draws images; this only answers "does Mermaid accept it, and if not, where
// is the mistake", which needs no layout and so no browser.
//
//   GET  /health     {"status":"ok","mermaid":"<version>"}. No parsing.
//   POST /validate   {"source": "..."}
//                    200 {"ok":true,"type":"flowchart-v2"}
//                    422 {"ok":false,"error":{"message","line"}}  Mermaid said no
//                    400 bad request · 401 missing peer token · 413 too big
//                    503 busy, or the parser could not start
//
// Line numbers count in the source as given, front matter included (see
// mermaid-errors.mjs for why that takes work).
//
// HOW IT IS CONTAINED. Diagram source comes from agents and from whatever
// they were shown, so it is treated as hostile:
//   - Mermaid runs with securityLevel "strict", against linkedom: a DOM with
//     no script engine that loads nothing.
//   - Parsing happens in a worker thread with a heap cap and a hard
//     deadline. A parse that runs over has its worker terminated.
//   - The container runs as a non-root UID with a read-only root filesystem,
//     no capabilities, and no route out (Compose: the internal `diagrams`
//     network; Kubernetes: a NetworkPolicy that denies egress).
//   - Only peer Elefant services may call it: the X-Internal-Service header
//     must carry INTERNAL_SERVICE_TOKEN, as for every other internal service.

import http from 'node:http';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { timingSafeEqual } from 'node:crypto';
import { Worker } from 'node:worker_threads';
import { checkRequest, ParseQueue, MAX_SOURCE_BYTES } from './lib.mjs';

const PORT = Number(process.env.PORT || 8005);
const TIMEOUT_MS = Number(process.env.VALIDATE_TIMEOUT_MS || 5000);
const HEAP_MB = Number(process.env.VALIDATE_HEAP_MB || 128);
const TOKEN = (process.env.INTERNAL_SERVICE_TOKEN || '').trim();

const require = createRequire(import.meta.url);
const MERMAID_VERSION = JSON.parse(
  readFileSync(require.resolve('mermaid/package.json'), 'utf8')).version;

const log = (level, msg, extra = {}) =>
  console.log(JSON.stringify({ level, msg, ...extra, ts: new Date().toISOString() }));

if (!TOKEN) {
  log('critical', 'INTERNAL_SERVICE_TOKEN is not set: every validate request will be refused');
}

export const queue = new ParseQueue(
  () => new Worker(new URL('./parse-worker.mjs', import.meta.url), {
    resourceLimits: { maxOldGenerationSizeMb: HEAP_MB },
  }),
  { timeoutMs: TIMEOUT_MS },
);

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
    if (req.url !== '/validate') return send(res, 404, { error: 'not found' });
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

    let out;
    try {
      out = await queue.parse(checked.source);
    } catch (err) {
      if (err?.timeout) {
        return send(res, 422, { ok: false, error: {
          message: `Parsing took longer than ${TIMEOUT_MS / 1000}s and was stopped. `
            + 'Simplify the diagram or split it in two.', line: null } });
      }
      log('error', 'parse failed', { error: String(err?.message || err) });
      return send(res, 503, { error: err?.busy ? 'validator busy' : 'validator unavailable' });
    }
    if (out.crashed) {
      log('error', 'parser crashed', { error: out.error?.message });
      return send(res, 503, { error: 'validator unavailable' });
    }
    if (!out.ok) return send(res, 422, { ok: false, error: out.error });
    return send(res, 200, { ok: true, type: out.type });
  } catch (err) {
    log('error', 'request failed', { error: String(err?.message || err) });
    if (!res.headersSent) send(res, 500, { error: 'internal error' });
  }
});

if (process.argv[1] && import.meta.url === new URL(`file://${process.argv[1]}`).href) {
  server.listen(PORT, '0.0.0.0', () => log('info', 'diagram validator listening', { port: PORT, mermaid: MERMAID_VERSION }));
  // Load Mermaid now, so the first real request does not pay for it.
  queue.parse('flowchart TD\n  A --> B').catch((err) => log('error', 'warm-up parse failed', { error: String(err?.message || err) }));
  const stop = async () => {
    server.close();
    await queue.close();
    process.exit(0);
  };
  process.on('SIGTERM', stop);
  process.on('SIGINT', stop);
}
