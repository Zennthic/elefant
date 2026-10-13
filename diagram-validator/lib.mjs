// The parts of the validator that need neither Mermaid nor a worker, kept
// apart from server.mjs so they are tested on their own.

export const MAX_SOURCE_BYTES = 64 * 1024;

// What a caller sent, checked. Returns { source } or { error }.
export function checkRequest(body) {
  if (!body || typeof body !== 'object') return { error: 'expected a JSON object' };
  const { source } = body;
  if (typeof source !== 'string' || !source.trim()) return { error: '`source` must be a non-empty string' };
  if (Buffer.byteLength(source, 'utf8') > MAX_SOURCE_BYTES) {
    return { error: `\`source\` is over ${MAX_SOURCE_BYTES / 1024} KiB` };
  }
  return { source };
}

// One worker, one parse at a time, each with a deadline. A parse that runs
// over has its worker terminated; the next request starts a fresh one.
// `spawn` returns a Worker-like object (postMessage, on, terminate), so the
// tests can hand in a fake.
export class ParseQueue {
  constructor(spawn, { timeoutMs = 5000, maxQueued = 32 } = {}) {
    this.spawn = spawn;
    this.timeoutMs = timeoutMs;
    this.maxQueued = maxQueued;
    this.worker = null;
    this.queue = [];
    this.current = null;
    this.nextId = 1;
  }

  parse(source) {
    if (this.queue.length >= this.maxQueued) {
      return Promise.reject(Object.assign(new Error('too many parses waiting'), { busy: true }));
    }
    return new Promise((resolve, reject) => {
      this.queue.push({ id: this.nextId++, source, resolve, reject });
      this.pump();
    });
  }

  ensureWorker() {
    if (this.worker) return this.worker;
    const w = this.spawn();
    w.on('message', (msg) => {
      if (w !== this.worker || !this.current || msg.id !== this.current.id) return;
      this.finish(null, msg);
    });
    const gone = (err) => {
      if (w !== this.worker) return;
      this.worker = null;
      if (this.current) this.finish(err || new Error('the parser stopped'));
    };
    w.on('error', gone);
    w.on('exit', () => gone(null));
    this.worker = w;
    return w;
  }

  pump() {
    if (this.current || !this.queue.length) return;
    const job = this.queue.shift();
    this.current = job;
    job.timer = setTimeout(() => {
      const w = this.worker;
      this.worker = null;
      w?.terminate();
      this.finish(Object.assign(new Error(`parse timed out after ${this.timeoutMs} ms`), { timeout: true }));
    }, this.timeoutMs);
    try {
      this.ensureWorker().postMessage({ id: job.id, source: job.source });
    } catch (err) {
      this.finish(err);
    }
  }

  finish(err, msg) {
    const job = this.current;
    this.current = null;
    clearTimeout(job.timer);
    if (err) job.reject(err);
    else job.resolve(msg);
    this.pump();
  }

  async close() {
    const w = this.worker;
    this.worker = null;
    await w?.terminate();
  }
}
