// End-to-end check of the image as built: the server parses through its
// worker, a valid source passes, a broken one comes back with the line it is
// on in the stored source, and nothing in a label reaches the network. The
// Dockerfile runs this as the runtime user, so an image whose Mermaid cannot
// load fails to build rather than failing its first request on a server.

import assert from 'node:assert/strict';
import { createServer } from 'node:http';

process.env.INTERNAL_SERVICE_TOKEN ||= 'selftest-token';
const { server, queue } = await import('../server.mjs');
import { RICH_FLOWCHART, RICH_NETWORK } from './fixtures.mjs';

await new Promise((r) => server.listen(0, '127.0.0.1', r));
const url = `http://127.0.0.1:${server.address().port}/validate`;
const validate = async (source) => {
  const r = await fetch(url, {
    method: 'POST',
    headers: { 'content-type': 'application/json', 'x-internal-service': process.env.INTERNAL_SERVICE_TOKEN },
    body: JSON.stringify({ source }),
  });
  return { status: r.status, body: await r.json() };
};

const good = RICH_FLOWCHART;
const ok = await validate(good);
assert.equal(ok.status, 200, JSON.stringify(ok.body));
const rich = await validate(RICH_NETWORK);
assert.equal(rich.status, 200, `the shared rich fixture did not parse: ${JSON.stringify(rich.body)}`);

const bad = await validate('---\ntitle: Login\n---\nflowchart TD\n  A[Log in] --> B[Check password\n  B --> C[Home]');
assert.equal(bad.status, 422);
assert.equal(bad.body.error.line, 5, `expected line 5, got ${bad.body.error.line}: ${bad.body.error.message}`);

let hits = 0;
const trap = createServer((_req, res) => { hits++; res.end(); });
await new Promise((r) => trap.listen(0, '127.0.0.1', r));
const { port } = trap.address();
await validate(`flowchart TD\n  A["<img src='http://127.0.0.1:${port}/x'>"] --> B\n  click A href "http://127.0.0.1:${port}/y"`);
await new Promise((r) => setTimeout(r, 300));
trap.close();
assert.equal(hits, 0, 'parsing reached the network');

console.log(`selftest: valid source, line-numbered error and network isolation all OK (rss ${Math.round(process.memoryUsage().rss / 1e6)} MB)`);
server.close();
await queue.close();
process.exit(0);
