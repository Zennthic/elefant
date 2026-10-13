// The renderer's browser-free parts. The browser path is exercised by
// scripts/selftest.mjs, which the Dockerfile runs inside the built image.

import { test } from 'node:test';
import { readFileSync } from 'node:fs';
import assert from 'node:assert/strict';
import {
  checkRequest, cacheKey, Lru, describeError, firstMeaningfulLine, limiter, MAX_SOURCE_BYTES,
  FONT_STACK,
} from '../lib.mjs';
import { acceptable } from '../scripts/license-scan.mjs';

test('a request needs a source and a known format', () => {
  assert.deepEqual(checkRequest({ source: 'flowchart TD' }), { source: 'flowchart TD', format: 'png' });
  assert.ok(checkRequest({ source: '' }).error);
  assert.ok(checkRequest({ source: 'pie', format: 'gif' }).error);
  assert.ok(checkRequest(null).error);
  assert.match(checkRequest({ source: 'x'.repeat(MAX_SOURCE_BYTES + 1) }).error, /KiB/);
});

test('the cache key separates formats and sources', () => {
  assert.notEqual(cacheKey('a', 'png'), cacheKey('a', 'svg'));
  assert.notEqual(cacheKey('a', 'png'), cacheKey('b', 'png'));
  assert.equal(cacheKey('a', 'png'), cacheKey('a', 'png'));
});

test('the LRU drops the least recently used entry', () => {
  const c = new Lru(2);
  c.set('a', 1); c.set('b', 2);
  c.get('a');
  c.set('c', 3);
  assert.equal(c.get('b'), undefined);
  assert.equal(c.get('a'), 1);
  assert.equal(c.size, 2);
});

test('a jison error uses the parser location, not the past-the-end count', () => {
  const src = 'flowchart TD\n A[x] --> B[\n B-->C';
  const err = { name: 'Error', message: 'Parse error on line 4:\n...', loc: { first_line: 2 } };
  assert.equal(describeError(err, src).line, 2);
  assert.match(describeError(err, src).message, /^Parse error on line 2:/);
});

test('a Langium error takes its line from the message', () => {
  const err = { name: 'Error', message: 'Parsing failed: Lexer error on line 2, column 7: ...' };
  assert.equal(describeError(err, 'pie\n "a": x').line, 2);
});

test('an unknown diagram type points at the declaration line', () => {
  const src = '%% note\n\nflowchat TD\n A-->B';
  assert.equal(describeError({ name: 'UnknownDiagramError', message: 'No diagram type' }, src).line, 3);
  assert.equal(firstMeaningfulLine('---\ntitle: x\n---\nflowchart TD'), 4);
});

test('a line Mermaid counted without the front matter is reported in the stored source', () => {
  const src = '---\ntitle: x\n---\nflowchart TD\n  A --> B\n  B -->>';
  const err = { name: 'Error', message: 'Parse error on line 3:\n...', loc: { first_line: 3 } };
  assert.equal(describeError(err, src).line, 6);
  assert.match(describeError(err, src).message, /^Parse error on line 6:/);
});

test('a line past the end is clamped to the last line', () => {
  assert.equal(describeError({ message: 'Parse error on line 9' }, 'a\nb').line, 2);
  assert.equal(describeError({ message: 'no line here' }, 'a').line, null);
});

test('the limiter never runs more than n at once', async () => {
  const run = limiter(2);
  let active = 0;
  let peak = 0;
  const job = () => run(async () => {
    active++; peak = Math.max(peak, active);
    await new Promise((r) => setTimeout(r, 5));
    active--;
  });
  await Promise.all([job(), job(), job(), job(), job()]);
  assert.equal(peak, 2);
});

test('the license scan takes an allowed branch of an OR expression', () => {
  assert.equal(acceptable('(MPL-2.0 OR Apache-2.0)'), 'Apache-2.0');
  assert.equal(acceptable('MIT'), 'MIT');
  assert.equal(acceptable('EPL-2.0'), null);
  assert.equal(acceptable('GPL-3.0'), null);
});

// Card #418: when Mermaid drops a diagram's font (it blanks any themeVariables
// value with a hyphen, such as `sans-serif`), labels inherit the page's font.
// The renderer's page must use the wiki's stack so the PNG and the wiki agree.
test('the renderer page uses the wiki font stack', () => {
  const css = readFileSync(new URL('../../memory-ui/static/appearance.css', import.meta.url), 'utf8');
  const wiki = css.match(/--font-sans:\s*([^;]+);/);
  assert.ok(wiki, 'no --font-sans in memory-ui/static/appearance.css');
  assert.equal(FONT_STACK, wiki[1].trim());
  assert.match(FONT_STACK, /^Inter,/);
});
