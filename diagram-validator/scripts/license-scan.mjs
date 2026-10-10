// License scan for the diagram-validator image, run at build time against the
// lockfile that was actually installed.
//
//   node scripts/license-scan.mjs                 check only; exit 1 on a problem
//   node scripts/license-scan.mjs --write FILE    check, then write the notices
//
// Every package in package-lock.json is looked up in node_modules and its
// license checked against the allow list. A license outside it stops the
// build: it needs a person's decision, not a regenerate. The Dockerfile runs
// this with --write, so the image carries a notices file generated from what
// is really inside it rather than from a note somebody wrote once.
//
// Deliberately absent: @mermaid-js/layout-elk, and with it elkjs (EPL-2.0,
// weak copyleft). If it is ever added, EPL-2.0 has to be added here on
// purpose, and the notices will name it. This is a copy of the
// diagram-renderer's scan with the image description changed.

import { readFileSync, readdirSync, writeFileSync, existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');

// Permissive licenses compatible with shipping alongside Apache-2.0 code.
const ALLOWED = new Set([
  'MIT', 'ISC', 'BSD-2-Clause', 'BSD-3-Clause', 'Apache-2.0', '0BSD', 'Unlicense',
  'BlueOak-1.0.0', 'CC0-1.0',
]);

// "(MPL-2.0 OR Apache-2.0)" is fine when one branch is: we take that branch.
export function acceptable(id) {
  const branches = String(id).replace(/[()]/g, '').split(/\s+OR\s+/);
  return branches.find((b) => ALLOWED.has(b.trim())) || null;
}

function licenseText(dir) {
  const hit = readdirSync(dir).find((f) => /^(licen[sc]e|copying)([.-]|$)/i.test(f));
  return hit ? readFileSync(join(dir, hit), 'utf8').replace(/\r\n/g, '\n').trim() : '';
}

// A package.json with no `license` field (khroma, for one) still ships its
// LICENSE file; read the license off its first lines rather than guess.
function sniff(text) {
  const head = text.slice(0, 300);
  if (/\bMIT License\b|Permission is hereby granted, free of charge/i.test(head + text.slice(0, 600))) return 'MIT';
  if (/Apache License,?\s+Version 2\.0/i.test(head)) return 'Apache-2.0';
  if (/\bISC License\b/i.test(head)) return 'ISC';
  return '';
}

function licenseId(pkg, text) {
  const l = pkg.license ?? pkg.licenses;
  if (typeof l === 'string') return l;
  if (l && typeof l === 'object' && !Array.isArray(l) && l.type) return l.type;
  if (Array.isArray(l)) return l.map((x) => x.type || x).join(' OR ');
  return sniff(text) || 'UNKNOWN';
}

export function scan(dir = root) {
  const lock = JSON.parse(readFileSync(join(dir, 'package-lock.json'), 'utf8'));
  const out = [];
  const problems = [];
  for (const [key, meta] of Object.entries(lock.packages || {})) {
    if (!key || meta.dev) continue;
    const pdir = join(dir, key);
    if (!existsSync(join(pdir, 'package.json'))) {
      if (!meta.optional) problems.push(`${key}: in the lockfile but not installed; run npm ci`);
      continue;
    }
    const pkg = JSON.parse(readFileSync(join(pdir, 'package.json'), 'utf8'));
    const text = licenseText(pdir);
    const id = licenseId(pkg, text);
    const chosen = acceptable(id);
    if (!chosen) problems.push(`${pkg.name}@${pkg.version}: license "${id}" is not on the allow list`);
    if (/elkjs|layout-elk/.test(pkg.name)) problems.push(`${pkg.name}: EPL-2.0 layout engine is deliberately excluded`);
    out.push({ name: pkg.name, version: pkg.version, license: id, text });
  }
  out.sort((a, b) => a.name.localeCompare(b.name) || a.version.localeCompare(b.version));
  return { packages: out, problems };
}

export function render(packages) {
  const lines = [
    '# Third-party notices: Elefant diagram validator image',
    '',
    'Generated at image build time by `scripts/license-scan.mjs` from the',
    "image's own `package-lock.json` and installed `node_modules`.",
    '',
    'The Elefant code in this image is licensed under the Apache License 2.0.',
    'The image also contains:',
    '',
    '- The Debian base system, whose notices are under `/usr/share/doc/*/copyright`.',
    '- The npm packages below.',
    '',
    '| Package | Version | License |',
    '|---|---|---|',
    ...packages.map((p) => `| ${p.name} | ${p.version} | ${p.license} |`),
    '',
    '## License texts',
  ];
  const groups = new Map();
  for (const p of packages) {
    const key = p.text || `(no license file shipped; declared ${p.license})`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(`${p.name}@${p.version}`);
  }
  for (const [text, names] of groups) {
    lines.push('', `### ${names.join(', ')}`, '', '```', text, '```');
  }
  return lines.join('\n') + '\n';
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const { packages, problems } = scan();
  const at = process.argv.indexOf('--write');
  if (at > 0 && !problems.length) {
    writeFileSync(process.argv[at + 1], render(packages));
    console.log(`wrote ${process.argv[at + 1]}: ${packages.length} packages`);
  }
  for (const p of problems) console.error(`license scan: ${p}`);
  if (problems.length) process.exit(1);
  console.log(`license scan: ${packages.length} packages, all on the allow list`);
}
