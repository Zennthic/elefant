// The parts of the renderer that need no browser: request checking, the cache,
// and (re-exported) turning Mermaid's error into a message with a line number.
// Kept apart from server.mjs so they are tested without Chromium.

import { createHash } from 'node:crypto';

export const FORMATS = new Set(['png', 'svg', 'none']);
export const MAX_SOURCE_BYTES = 64 * 1024;

// The wiki's own sans stack (--font-sans in memory-ui/static/appearance.css;
// a test keeps the two equal). Mermaid writes the diagram's font onto its SVG,
// but it silently blanks a themeVariables value with a hyphen in it, so
// `fontFamily: Inter, Arial, sans-serif` comes out as no font at all. In the
// wiki the labels then inherit the page's font; here they inherit this one,
// so both draw the same face. Without it Chromium's default is a serif.
// Inter is installed in the image (fonts-inter, OFL-1.1).
export const FONT_STACK = "Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif";

// What a caller sent, checked. Returns { source, format } or { error }.
export function checkRequest(body) {
  if (!body || typeof body !== 'object') return { error: 'expected a JSON object' };
  const { source, format = 'png' } = body;
  if (typeof source !== 'string' || !source.trim()) return { error: '`source` must be a non-empty string' };
  if (Buffer.byteLength(source, 'utf8') > MAX_SOURCE_BYTES) {
    return { error: `\`source\` is over ${MAX_SOURCE_BYTES / 1024} KiB` };
  }
  if (!FORMATS.has(format)) return { error: '`format` must be png, svg or none' };
  return { source, format };
}

export function cacheKey(source, format) {
  return createHash('sha256').update(format).update('\0').update(source).digest('hex');
}

// A small LRU. Diagrams are re-rendered far more often than they change (every
// read, every patch preview), so the same source is asked for repeatedly.
export class Lru {
  constructor(max) {
    this.max = max;
    this.map = new Map();
  }
  get(key) {
    if (!this.map.has(key)) return undefined;
    const v = this.map.get(key);
    this.map.delete(key);
    this.map.set(key, v);
    return v;
  }
  set(key, value) {
    this.map.delete(key);
    this.map.set(key, value);
    while (this.map.size > this.max) this.map.delete(this.map.keys().next().value);
  }
  get size() {
    return this.map.size;
  }
}

// Mermaid's error as { message, line }, with the line counted in the stored
// source. Lives in its own file, shared byte for byte with diagram-validator.
export { describeError, firstMeaningfulLine } from './mermaid-errors.mjs';

// A promise that rejects after `ms`, for racing a render against.
export function deadline(ms, what) {
  let timer;
  const p = new Promise((_, reject) => {
    timer = setTimeout(() => reject(Object.assign(new Error(`${what} timed out after ${ms} ms`), { timeout: true })), ms);
  });
  return { promise: p, cancel: () => clearTimeout(timer) };
}

// At most `n` renders at once; the rest wait their turn.
export function limiter(n) {
  let active = 0;
  const queue = [];
  const next = () => {
    if (active >= n || !queue.length) return;
    active++;
    const { fn, resolve, reject } = queue.shift();
    Promise.resolve().then(fn).then(resolve, reject).finally(() => {
      active--;
      next();
    });
  };
  return (fn) => new Promise((resolve, reject) => {
    queue.push({ fn, resolve, reject });
    next();
  });
}
