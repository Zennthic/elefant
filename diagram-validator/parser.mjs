// Mermaid's own parser, run in Node with a linkedom window instead of a
// browser.
//
// Parsing needs no layout, so it needs no Chromium: what it does need is a
// `window` for DOMPurify, which Mermaid's diagram databases use to sanitise
// labels as they parse. linkedom supplies one. It is a DOM and nothing more:
// it has no script engine and fetches nothing, so a label that smuggles in
// <script> or <img src=...> is inert text.
//
// Why not jsdom: every current jsdom pulls in @csstools packages licensed
// MIT-0, which is not on the allow list (CONTRIBUTING.md: a new license needs
// a person's decision). linkedom and its dependencies are all ISC/MIT/BSD.

import { parseHTML } from 'linkedom';
import { describeError } from './mermaid-errors.mjs';

let ready = null;

function load() {
  if (!ready) {
    ready = (async () => {
      const dom = parseHTML('<!doctype html><html><body></body></html>');
      globalThis.window = dom.window;
      globalThis.document = dom.document;
      const { default: mermaid } = await import('mermaid');
      mermaid.initialize({ startOnLoad: false, securityLevel: 'strict' });
      return mermaid;
    })();
  }
  return ready;
}

// { ok: true, type } or { ok: false, error: { message, line } }.
export async function parseSource(source) {
  const mermaid = await load();
  try {
    const { diagramType } = await mermaid.parse(source);
    return { ok: true, type: diagramType };
  } catch (e) {
    const plain = { name: e?.name || 'Error', message: String(e?.message || e), loc: e?.hash?.loc || null };
    return { ok: false, error: describeError(plain, source) };
  }
}
