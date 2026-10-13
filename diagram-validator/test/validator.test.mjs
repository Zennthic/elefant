import { test } from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { checkRequest, ParseQueue, MAX_SOURCE_BYTES } from '../lib.mjs';
import { parseSource } from '../parser.mjs';
import { toSourceLine, parsedText, describeError, firstMeaningfulLine } from '../mermaid-errors.mjs';
import { acceptable } from '../scripts/license-scan.mjs';
import { RICH_FLOWCHART, RICH_NETWORK, BROKEN } from '../scripts/fixtures.mjs';

// ── Mermaid's real parser ──────────────────────────────────────────────────

test('the card\'s rich flowchart parses', async () => {
  assert.deepEqual(await parseSource(RICH_FLOWCHART), { ok: true, type: 'flowchart-v2' });
});

test('the shared rich fixture (card #413) parses', async () => {
  assert.deepEqual(await parseSource(RICH_NETWORK), { ok: true, type: 'flowchart-v2' });
});

test('the rich fixture really uses edge IDs and `@{ }` (or the test above proves nothing)', async () => {
  assert.match(RICH_NETWORK, /^\s*\w+ e1@==>/m);
  assert.match(RICH_NETWORK, /^\s*e1@\{ animate: true, animation: fast \}$/m);
  assert.match(RICH_NETWORK, /^config:\n(?: .*\n)*  themeVariables:/m);
  // And taking the edge-ID support away breaks it: the same text with the
  // `@{ }` line in a form Mermaid does not know is refused, so the parser
  // really reads that syntax rather than skipping it.
  const res = await parseSource(RICH_NETWORK.replace('e1@{ animate: true, animation: fast }', 'e1@{ animate: true, animation: fast'));
  assert.equal(res.ok, false);
});

test('the worked example in docs/diagrams.md (card #414) parses', async () => {
  const { readFileSync } = await import('node:fs');
  const doc = readFileSync(new URL('../../docs/diagrams.md', import.meta.url), 'utf8');
  const example = doc.split('## A worked example')[1]?.match(/```mermaid\n([\s\S]*?)\n```\n````/)?.[1];
  assert.ok(example, 'docs/diagrams.md lost its worked example');
  assert.match(example, /e1@\{ animate: true \}/);
  assert.deepEqual(await parseSource(example), { ok: true, type: 'flowchart-v2' });
});

for (const [what, src, line] of BROKEN) {
  test(`a mistake after ${what} is reported at line ${line} of the stored source`, async () => {
    const res = await parseSource(src);
    assert.equal(res.ok, false);
    assert.equal(res.error.line, line, res.error.message);
    if (/^Parse error on line/.test(res.error.message)) {
      assert.match(res.error.message, new RegExp(`^Parse error on line ${line}:`));
    }
  });
}

const EVERY_KIND = {
  flowchart: 'flowchart LR\n  A --> B',
  graph: 'graph TD\n  A --> B',
  sequence: 'sequenceDiagram\n  A->>B: hi',
  class: 'classDiagram\n  class Animal',
  state: 'stateDiagram-v2\n  [*] --> On',
  er: 'erDiagram\n  A ||--o{ B : has',
  journey: 'journey\n  title Day\n  section Work\n    Code: 5: Me',
  gantt: 'gantt\n  title P\n  section S\n    Task :a1, 2026-01-01, 3d',
  pie: 'pie title Pets\n  "Dogs" : 3\n  "Cats" : 2',
  quadrant: 'quadrantChart\n  x-axis Low --> High\n  y-axis Low --> High\n  A: [0.3, 0.6]',
  requirement: 'requirementDiagram\n  requirement r1 {\n    id: 1\n    text: t\n  }',
  gitGraph: 'gitGraph\n  commit\n  branch dev\n  commit',
  mindmap: 'mindmap\n  root\n    a\n    b',
  timeline: 'timeline\n  2025 : a\n  2026 : b',
  sankey: 'sankey-beta\n  A,B,10',
  xychart: 'xychart-beta\n  x-axis [a, b]\n  bar [1, 2]',
  block: 'block-beta\n  columns 2\n  a b',
  packet: 'packet-beta\n  0-15: "Source"',
  architecture: 'architecture-beta\n  service db(database)[DB]',
  kanban: 'kanban\n  todo[To do]\n    t1[Write it]',
  c4: 'C4Context\n  Person(u, "User")',
};
for (const [kind, src] of Object.entries(EVERY_KIND)) {
  test(`a ${kind} diagram parses without a browser`, async () => {
    const res = await parseSource(src);
    assert.equal(res.ok, true, JSON.stringify(res.error));
  });
}

test('markup in a label is inert: there is no script engine to run it', async () => {
  globalThis.__pwned = false;
  const res = await parseSource('flowchart TD\n  A["<img src=x onerror=globalThis.__pwned=true><script>globalThis.__pwned=true</script>"] --> B');
  assert.equal(res.ok, true);
  await new Promise((r) => setTimeout(r, 50));
  assert.equal(globalThis.__pwned, false);
});

// ── line mapping on its own ────────────────────────────────────────────────

test('nothing stripped means the line is unchanged', () => {
  assert.equal(toSourceLine('flowchart TD\n  A-->B\n  B-->C', 3), 3);
  assert.equal(parsedText('flowchart TD\n  A-->B').text, 'flowchart TD\n  A-->B');
});

test('a line past what was parsed is the last line', () => {
  assert.equal(toSourceLine('---\ntitle: x\n---\nflowchart TD\n  A-->B', 9), 5);
});

test('the message carries the line we report', () => {
  const src = '%% a\n%% b\npie\n  "a": 1\n  "b" x';
  const err = { message: 'Parsing failed: Lexer error on line 3, column 6: x. Parse error on line 3, column 7: y' };
  const out = describeError(err, src);
  assert.equal(out.line, 5);
  assert.match(out.message, /Lexer error on line 5, column 6/);
  assert.match(out.message, /Parse error on line 5, column 7/);
});

test('an error with no line says so', () => {
  assert.equal(describeError({ message: 'no line here' }, 'a').line, null);
  assert.equal(firstMeaningfulLine('---\ntitle: x\n---\n%% c\nflowchart TD'), 5);
});

// ── requests and the queue ─────────────────────────────────────────────────

test('a request needs a non-empty source under the size limit', () => {
  assert.deepEqual(checkRequest({ source: 'flowchart TD' }), { source: 'flowchart TD' });
  assert.ok(checkRequest({ source: '  ' }).error);
  assert.ok(checkRequest(null).error);
  assert.match(checkRequest({ source: 'x'.repeat(MAX_SOURCE_BYTES + 1) }).error, /KiB/);
});

class FakeWorker extends EventEmitter {
  constructor(reply) { super(); this.reply = reply; this.terminated = false; }
  postMessage(msg) { this.reply(this, msg); }
  terminate() { this.terminated = true; this.emit('exit', 1); return Promise.resolve(); }
}

test('parses run one at a time, in order', async () => {
  let active = 0;
  let peak = 0;
  const q = new ParseQueue(() => new FakeWorker((w, { id, source }) => {
    active++; peak = Math.max(peak, active);
    setTimeout(() => { active--; w.emit('message', { id, ok: true, type: source }); }, 5);
  }));
  const out = await Promise.all(['a', 'b', 'c'].map((s) => q.parse(s)));
  assert.deepEqual(out.map((r) => r.type), ['a', 'b', 'c']);
  assert.equal(peak, 1);
});

test('a parse that runs over kills its worker, and the next gets a new one', async () => {
  const spawned = [];
  const q = new ParseQueue(() => {
    const w = new FakeWorker((self, { id, source }) => {
      if (source !== 'hang') self.emit('message', { id, ok: true, type: 'x' });
    });
    spawned.push(w);
    return w;
  }, { timeoutMs: 30 });
  await assert.rejects(q.parse('hang'), (e) => e.timeout === true);
  assert.equal(spawned[0].terminated, true);
  assert.equal((await q.parse('fine')).ok, true);
  assert.equal(spawned.length, 2);
});

test('a worker that dies mid-parse fails that parse, not the next', async () => {
  let n = 0;
  const q = new ParseQueue(() => new FakeWorker((w, { id }) => {
    if (n++ === 0) w.emit('error', new Error('out of memory'));
    else w.emit('message', { id, ok: true, type: 'x' });
  }));
  await assert.rejects(q.parse('a'), /out of memory/);
  assert.equal((await q.parse('b')).ok, true);
});

test('a full queue is refused rather than growing', async () => {
  const q = new ParseQueue(() => new FakeWorker(() => {}), { timeoutMs: 1000, maxQueued: 1 });
  const first = q.parse('a');
  const second = q.parse('b');
  await assert.rejects(q.parse('c'), (e) => e.busy === true);
  await q.close();
  first.catch(() => {}); second.catch(() => {});
});

test('the license scan takes an allowed branch of an OR expression', () => {
  assert.equal(acceptable('(MPL-2.0 OR Apache-2.0)'), 'Apache-2.0');
  assert.equal(acceptable('MIT-0'), null);
  assert.equal(acceptable('EPL-2.0'), null);
});
