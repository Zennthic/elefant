// The HTTP surface, with the real worker and the real parser behind it.
import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';

process.env.INTERNAL_SERVICE_TOKEN = 'test-token';
const { server, queue } = await import('../server.mjs');

let base;
before(async () => {
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  base = `http://127.0.0.1:${server.address().port}`;
});
after(async () => {
  server.close();
  await queue.close();
});

const post = (body, token = 'test-token') => fetch(`${base}/validate`, {
  method: 'POST',
  headers: { 'content-type': 'application/json', ...(token ? { 'x-internal-service': token } : {}) },
  body: typeof body === 'string' ? body : JSON.stringify(body),
});

test('health answers without a token', async () => {
  const r = await fetch(`${base}/health`);
  assert.equal(r.status, 200);
  assert.equal((await r.json()).mermaid, '11.16.0');
});

test('validate needs the peer token', async () => {
  assert.equal((await post({ source: 'flowchart TD' }, '')).status, 401);
  assert.equal((await post({ source: 'flowchart TD' }, 'wrong-token')).status, 401);
});

test('a valid source is 200 with its type', async () => {
  const r = await post({ source: 'sequenceDiagram\n  A->>B: hi' });
  assert.equal(r.status, 200);
  assert.deepEqual(await r.json(), { ok: true, type: 'sequence' });
});

test('a broken source is 422 with the stored line', async () => {
  const r = await post({ source: '---\ntitle: t\n---\nflowchart TD\n  A --> B[\n  B --> C' });
  assert.equal(r.status, 422);
  const { ok, error } = await r.json();
  assert.equal(ok, false);
  assert.equal(error.line, 5, error.message);
});

test('bad requests are 400', async () => {
  assert.equal((await post('not json')).status, 400);
  assert.equal((await post({ source: '' })).status, 400);
});
