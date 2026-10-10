// Turning Mermaid's parse error into { message, line }, where line counts in
// the source as STORED: front matter, %%{init}%% directives and %% comments
// included. Shared by diagram-validator and diagram-renderer, and kept
// byte-identical in both (CI compares the two copies).
//
// Mermaid does not parse what it was given. preprocessDiagram() first drops
// the YAML front matter (and any blank lines after it), every directive, and
// every whole-line %% comment, then trims leading whitespace; it does this
// twice (parse(), then getDiagramFromText()). The flowchart parser then
// collapses whitespace after a `}`. So "Parse error on line 3" can mean line
// 8 of what the agent wrote. toSourceLine() replays those deletions while
// remembering, for each surviving character, where it came from.
//
// The patterns are copied from mermaid 11.16.0 (src/preprocess.ts,
// src/diagram-api/{frontmatter,comments,regexes}.ts, flowParser.ts). The
// tests run the real parser on sources with each kind of preamble, so a
// mermaid bump that changes them fails CI rather than misreporting lines.

const FRONT_MATTER = /^([^\S\n\r]*)-{3}\s*[\n\r](.*?)[\n\r]\1-{3}\s*[\n\r]+/s;
const DIRECTIVE = /%{2}{\s*(?:(\w+)\s*:|(\w+))\s*(?:(\w+)|((?:(?!}%{2}).|\r?\n)*))?\s*(?:}%{2})?/gi;
const COMMENT_LINE = /^\s*%%(?!{)[^\n]+\n?/gm;
const LEADING_SPACE = /^\s+/;
// flowParser: src.replace(/}\s*\n/g, "}\n"), written as the deletion it is.
const AFTER_BRACE = /(?<=})\s*(?=\n)/g;
const FLOWCHART = /^\s*(flowchart|graph)/;

// Delete every match of `re` from text, carrying the offsets along.
function drop(state, re) {
  const { text, pos } = state;
  let outText = '';
  const outPos = [];
  let last = 0;
  const g = re.global ? re : new RegExp(re.source, re.flags + 'g');
  g.lastIndex = 0;
  for (let m; (m = g.exec(text));) {
    outText += text.slice(last, m.index);
    for (let i = last; i < m.index; i++) outPos.push(pos[i]);
    last = m.index + m[0].length;
    if (!m[0].length) g.lastIndex++;
    if (!re.global) break;
  }
  outText += text.slice(last);
  for (let i = last; i < text.length; i++) outPos.push(pos[i]);
  return { text: outText, pos: outPos };
}

function preprocess(state) {
  state = drop(state, FRONT_MATTER);
  state = drop(state, DIRECTIVE);
  state = drop(state, COMMENT_LINE);
  return drop(state, LEADING_SPACE);
}

// What Mermaid's parser sees, and where each of its characters came from.
export function parsedText(source) {
  // cleanupText's \r\n -> \n; the diagram tools store \n already.
  let state = { text: '', pos: [] };
  for (let i = 0; i < source.length; i++) {
    if (source[i] === '\r' && source[i + 1] === '\n') continue;
    state.text += source[i] === '\r' ? '\n' : source[i];
    state.pos.push(i);
  }
  state = preprocess(preprocess(state));
  if (FLOWCHART.test(state.text)) state = drop(state, AFTER_BRACE);
  return state;
}

// Line `line` (1-based) of what Mermaid parsed, as a line of `source`.
export function toSourceLine(source, line) {
  const nLines = source.split('\n').length;
  const { text, pos } = parsedText(source);
  let at = 0;
  for (let n = 1; n < line; n++) {
    const nl = text.indexOf('\n', at);
    if (nl < 0) { at = text.length; break; }
    at = nl + 1;
  }
  // Past the end (jison counts an unclosed construct to there): the last line.
  if (at >= pos.length) return nLines;
  return source.slice(0, pos[at]).split('\n').length;
}

// The 1-based line of the first line that is not blank, a %% comment, or YAML
// front matter: where the diagram type is declared.
export function firstMeaningfulLine(source) {
  const lines = source.split('\n');
  let i = 0;
  if (lines[0]?.trim() === '---') {
    const end = lines.findIndex((l, k) => k > 0 && l.trim() === '---');
    i = end < 0 ? lines.length : end + 1;
  }
  for (; i < lines.length; i++) {
    const s = lines[i].trim();
    if (s && !s.startsWith('%%')) return i + 1;
  }
  return 1;
}

// Mermaid's failure as { message, line }. `err` is what Mermaid threw, made
// plain: { name, message, loc } where loc is the jison parser's hash.loc.
//
// The jison grammars (flowchart, sequence, ...) report "Parse error on line N"
// counted past the end of input when the problem is an unclosed construct, so
// their structured location is preferred. The Langium grammars (pie, packet,
// architecture, ...) carry only "line N, column M" in the text. An unknown
// diagram type has no line at all, and is always the declaration line. Bad
// YAML in the front matter says "(line:column)" counted inside the YAML.
export function describeError(err, source) {
  const message = String(err?.message || 'Mermaid could not parse the source').slice(0, 2000);
  const nLines = source.split('\n').length;
  const clamp = (n) => Math.min(Math.max(n, 1), nLines);
  if (err?.name === 'UnknownDiagramError') {
    return { message, line: firstMeaningfulLine(source) };
  }
  if (err?.name === 'YAMLException') {
    const m = message.match(/\((\d+):\d+\)/);
    return { message, line: m ? clamp(Number(m[1]) + 1) : 1 };
  }
  let raw = null;
  if (Number.isInteger(err?.loc?.first_line)) raw = err.loc.first_line;
  else {
    const m = message.match(/\bline (\d+)/i);
    if (m) raw = Number(m[1]);
  }
  if (raw === null) return { message, line: null };
  const line = clamp(toSourceLine(source, raw));
  // Keep the text in step with the number we report, or the agent is told
  // "line 8" above a message that says "line 3".
  const text = message
    .replace(/^Parse error on line \d+/, `Parse error on line ${line}`)
    .replace(/\b(error on line )(\d+)(, column)/g,
      (_, a, n, b) => `${a}${clamp(toSourceLine(source, Number(n)))}${b}`);
  return { message: text, line };
}
