// The worker thread server.mjs parses in, so a source that sends the parser
// into a loop can be killed without taking the HTTP server down with it.
import { parentPort } from 'node:worker_threads';
import { parseSource } from './parser.mjs';

parentPort.on('message', async ({ id, source }) => {
  let res;
  try {
    res = await parseSource(source);
  } catch (e) {
    res = { ok: false, crashed: true, error: { message: String(e?.message || e), line: null } };
  }
  parentPort.postMessage({ id, ...res });
});
