// Generates THIRD_PARTY_NOTICES.md at the repo root: the attribution for every
// third-party component that Elefant ships inside its own tree.
//
//   npm run notices         rewrite the file
//   npm run notices:check   exit 1 if the file is out of date, or if a bundled
//                           package carries a license not on the allow list
//
// The Tiptap bundle's list is not kept by hand. It comes from esbuild's
// metafile for the same options build.mjs uses, so it names exactly the
// packages (and the lockfile versions) that end up in static/tiptap.bundle.js.
// A dependency bump in package-lock.json that changes what is bundled changes
// this file, and CI fails until it is regenerated and committed.

import { build } from 'esbuild';
import { readFileSync, readdirSync, writeFileSync, existsSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { bundleOptions } from '../build-options.mjs';

const __dirname = dirname(fileURLToPath(import.meta.url));
const uiDir = join(__dirname, '..');
const repoRoot = join(uiDir, '..');
const outFile = join(repoRoot, 'THIRD_PARTY_NOTICES.md');

// Licenses that are compatible with distributing Elefant under Apache-2.0.
// Anything else stops the check: it needs a human decision, not a regenerate.
const ALLOWED = new Set(['MIT', 'ISC', 'BSD-2-Clause', 'BSD-3-Clause', 'Apache-2.0', '0BSD', 'Unlicense']);

// "(MPL-2.0 OR Apache-2.0)" (DOMPurify) is fine: we take the allowed branch.
function allowed(id) {
  return String(id).replace(/[()]/g, '').split(/\s+OR\s+/).some((b) => ALLOWED.has(b.trim()));
}

// Vendored as a committed file rather than built from the lockfile. The
// version and copyright are read from the upstream header kept at the top of
// the file, so updating the file updates the notice.
const VENDORED = [
  {
    file: 'memory-ui/static/marked.min.js',
    name: 'marked',
    license: 'MIT',
    url: 'https://github.com/markedjs/marked',
    // Upstream LICENSE.md from the same release. It also carries John
    // Gruber's BSD-style notice for the original Markdown.pl.
    licenseText: 'memory-ui/licenses/marked.LICENSE.md',
  },
  {
    // Written by scripts/vendor-mermaid.mjs from the version package-lock.json
    // pins; the diagram-renderer service pins the same one.
    file: 'memory-ui/static/mermaid.min.js',
    name: 'mermaid',
    license: 'MIT',
    url: 'https://github.com/mermaid-js/mermaid',
    licenseText: 'memory-ui/licenses/mermaid.LICENSE',
  },
];

// Packages whose npm tarball ships no license file. Their text comes from the
// upstream repository instead, committed under memory-ui/licenses/.
const LICENSE_FALLBACK = [
  { match: /^@tiptap\//, file: 'memory-ui/licenses/tiptap.LICENSE.md' },
];

function readText(path) {
  return readFileSync(path, 'utf8').replace(/\r\n/g, '\n').trim();
}

function licenseFile(dir, name) {
  const hit = readdirSync(dir).find((f) => /^(licen[sc]e|copying)([.-]|$)/i.test(f));
  if (hit) return readText(join(dir, hit));
  const fb = LICENSE_FALLBACK.find((f) => f.match.test(name));
  return fb ? readText(join(repoRoot, fb.file)) : '';
}

function copyrightOf(text, fallback) {
  const lines = text.split('\n').map((l) => l.trim());
  // A real notice ("Copyright (c) 2019 ...", "Copyright 2025 ...") before any
  // line that merely starts with the word, which in an Apache-2.0 text is
  // boilerplate about copyright notices rather than one.
  const line = lines.find((l) => /^copyright\s+(\(c\)|©|\d{4})/i.test(l))
    || lines.find((l) => /^(\(c\)\s*\d|copyright\b)/i.test(l) && !/^copyright (notice|license)|\[yyyy\]/i.test(l));
  return line || fallback || '';
}

function licenseId(pkg, text = '') {
  if (typeof pkg.license === 'string') return pkg.license;
  if (pkg.license && pkg.license.type) return pkg.license.type;
  if (Array.isArray(pkg.licenses)) return pkg.licenses.map((l) => l.type || l).join(' OR ');
  // No license field (khroma, for one) but a LICENSE file: read it off that.
  if (/Permission is hereby granted, free of charge/.test(text)) return 'MIT';
  return 'UNKNOWN';
}

function authorOf(pkg) {
  const a = pkg.author;
  if (!a) return '';
  return typeof a === 'string' ? a : a.name || '';
}

// A package directory for a bundled input path such as
// node_modules/@tiptap/core/dist/index.js or node_modules/a/node_modules/b/x.js.
function packageDir(input) {
  const parts = input.split('/');
  const i = parts.lastIndexOf('node_modules');
  if (i < 0) return null;
  const n = parts[i + 1].startsWith('@') ? 2 : 1;
  return parts.slice(0, i + 1 + n).join('/');
}

async function bundledPackages() {
  const result = await build({ ...bundleOptions, write: false, logLevel: 'silent' });
  const dirs = new Set();
  for (const input of Object.keys(result.metafile.inputs)) {
    const d = packageDir(input);
    if (d) dirs.add(d);
  }
  const pkgs = [];
  for (const d of dirs) {
    const dir = join(uiDir, d);
    const pkg = JSON.parse(readFileSync(join(dir, 'package.json'), 'utf8'));
    const text = licenseFile(dir, pkg.name);
    pkgs.push({
      name: pkg.name,
      version: pkg.version,
      license: licenseId(pkg),
      copyright: copyrightOf(text, authorOf(pkg) && `Copyright (c) ${authorOf(pkg)}`),
      url: `https://www.npmjs.com/package/${pkg.name}`,
      text,
    });
  }
  pkgs.sort((a, b) => a.name.localeCompare(b.name) || a.version.localeCompare(b.version));
  return pkgs;
}

// mermaid.min.js is mermaid's own self-contained browser build, so the
// packages inside it are mermaid's runtime dependency tree. That tree is read
// from package-lock.json (resolved the way node resolves it), so a mermaid bump
// that pulls in something new changes this list, and a license outside the
// allow list fails the check.
function mermaidDependencies() {
  const lock = JSON.parse(readFileSync(join(uiDir, 'package-lock.json'), 'utf8')).packages || {};
  const resolve = (from, name) => {
    for (let base = from; ;) {
      if (lock[`${base}/node_modules/${name}`]) return `${base}/node_modules/${name}`;
      const i = base.lastIndexOf('/node_modules/');
      if (i < 0) break;
      base = base.slice(0, i);
    }
    return lock[`node_modules/${name}`] ? `node_modules/${name}` : null;
  };
  const seen = new Set(['node_modules/mermaid']);
  const queue = ['node_modules/mermaid'];
  const pkgs = [];
  while (queue.length) {
    const key = queue.shift();
    const meta = lock[key] || {};
    for (const name of Object.keys({ ...(meta.dependencies || {}), ...(meta.optionalDependencies || {}) })) {
      const dep = resolve(key, name);
      if (!dep || seen.has(dep)) continue;
      seen.add(dep);
      queue.push(dep);
      const dir = join(uiDir, dep);
      const pkg = JSON.parse(readFileSync(join(dir, 'package.json'), 'utf8'));
      const text = licenseFile(dir, pkg.name);
      pkgs.push({
        name: pkg.name,
        version: pkg.version,
        license: licenseId(pkg, text),
        copyright: copyrightOf(text, authorOf(pkg) && `Copyright (c) ${authorOf(pkg)}`),
        url: `https://www.npmjs.com/package/${pkg.name}`,
        text,
      });
    }
  }
  pkgs.sort((a, b) => a.name.localeCompare(b.name) || a.version.localeCompare(b.version));
  return pkgs;
}

function vendored() {
  return VENDORED.map((v) => {
    const head = readFileSync(join(repoRoot, v.file), 'utf8').slice(0, 1000);
    const version = (head.match(new RegExp(`${v.name} v(\\d[^\\s]*)`)) || [])[1] || 'UNKNOWN';
    const copyright = (head.match(/Copyright[^\n*]*/) || [''])[0].trim();
    const text = readText(join(repoRoot, v.licenseText));
    return { ...v, version, copyright, text };
  });
}

function cell(s) {
  return String(s).replace(/\|/g, '\\|');
}

function render(vend, pkgs, mdeps) {
  const out = [];
  out.push('# Third-party notices');
  out.push('');
  out.push('<!-- Generated by memory-ui/scripts/third-party-notices.mjs. Do not edit by hand:');
  out.push('     run `npm run notices` in memory-ui and commit the result. CI runs');
  out.push('     `npm run notices:check` and fails when this file is out of date. -->');
  out.push('');
  out.push('Elefant is licensed under the Apache License 2.0 (see `LICENSE` and `NOTICE`).');
  out.push('It ships the third-party components below inside its own tree. Each remains');
  out.push('under its own license, reproduced at the end of this file.');
  out.push('');
  out.push('## Vendored files');
  out.push('');
  out.push('| Component | Version | License | Copyright | File |');
  out.push('|---|---|---|---|---|');
  for (const v of vend) {
    out.push(`| [${v.name}](${v.url}) | ${v.version} | ${v.license} | ${cell(v.copyright)} | \`${v.file}\` |`);
  }
  out.push('');
  out.push('The upstream license header is kept at the top of each vendored file.');
  out.push('');
  out.push('## Bundled into `memory-ui/static/tiptap.bundle.js`');
  out.push('');
  out.push('Built by `memory-ui/build.mjs` from `memory-ui/package-lock.json`. This list is');
  out.push("taken from esbuild's metafile, so it names only the packages the bundle contains.");
  out.push('');
  out.push('| Package | Version | License | Copyright |');
  out.push('|---|---|---|---|');
  for (const p of pkgs) {
    out.push(`| [${p.name}](${p.url}) | ${p.version} | ${p.license} | ${cell(p.copyright)} |`);
  }
  out.push('');
  out.push('## Bundled into `memory-ui/static/mermaid.min.js`');
  out.push('');
  out.push("mermaid's self-contained browser build includes its runtime dependencies. This");
  out.push("list is mermaid's dependency tree as `memory-ui/package-lock.json` resolves it.");
  out.push('Where a package offers a choice of licenses, Elefant takes the permissive one');
  out.push('(DOMPurify: Apache-2.0).');
  out.push('');
  out.push('| Package | Version | License | Copyright |');
  out.push('|---|---|---|---|');
  for (const p of mdeps) {
    out.push(`| [${p.name}](${p.url}) | ${p.version} | ${p.license} | ${cell(p.copyright)} |`);
  }
  out.push('');
  out.push('## The optional `diagram-renderer` image');
  out.push('');
  out.push('`ghcr.io/zennthic/elefant/diagram-renderer` is a separate, optional image that');
  out.push('renders Mermaid diagrams for the MCP diagram tools. It contains mermaid (same');
  out.push('version as above, MIT), puppeteer-core (Apache-2.0), their npm dependencies,');
  out.push("and Chromium from Debian's package archive. Its own notices are generated at");
  out.push('image build time by `diagram-renderer/scripts/license-scan.mjs` from the');
  out.push('lockfile actually installed, written to `/app/THIRD_PARTY_NOTICES.md` inside');
  out.push("the image, and the build fails on a license outside the allow list. Chromium's");
  out.push('notices are at `/usr/share/doc/chromium/copyright` in the image. It does not use');
  out.push('`@mermaid-js/mermaid-cli`, whose 11.x line depends on the EPL-2.0 `elkjs`');
  out.push('layout engine; it drives mermaid in headless Chromium directly instead.');
  out.push('');
  out.push('## The `diagram-validator` image');
  out.push('');
  out.push('`ghcr.io/zennthic/elefant/diagram-validator` is a separate image that parses');
  out.push('Mermaid diagrams for the MCP diagram tools, with no browser. It contains mermaid');
  out.push('(same version as above, MIT), linkedom (ISC) and their npm dependencies on');
  out.push("Debian's Node base image. Its own notices are generated at image build time by");
  out.push('`diagram-validator/scripts/license-scan.mjs` from the lockfile actually');
  out.push('installed, written to `/app/THIRD_PARTY_NOTICES.md` inside the image, and the');
  out.push('build fails on a license outside the allow list. It does not use jsdom, whose');
  out.push('dependencies include MIT-0 packages, a license not on that list.');
  out.push('');
  out.push('## Loaded at runtime, not redistributed');
  out.push('');
  out.push('The web UI loads these from a public CDN in the browser. Elefant does not ship');
  out.push('copies of them, so they are listed for information only:');
  out.push('SortableJS 1.15.6 (MIT), three.js 0.158.0 (MIT), three-spritetext 1.8.2 (MIT),');
  out.push('3d-force-graph 1.74.5 (MIT).');
  out.push('');
  out.push('## License texts');
  out.push('');
  for (const v of vend) {
    out.push(`### ${v.name}@${v.version}`);
    out.push('');
    out.push('```');
    out.push(v.text);
    out.push('```');
  }
  // One block per distinct license text, naming every package that uses it.
  const groups = new Map();
  for (const p of [...pkgs, ...mdeps]) {
    const key = p.text;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(`${p.name}@${p.version}`);
  }
  for (const [text, names] of groups) {
    out.push('');
    out.push(`### ${names.join(', ')}`);
    out.push('');
    out.push('```');
    out.push(text);
    out.push('```');
  }
  out.push('');
  return out.join('\n');
}

const vend = vendored();
const pkgs = await bundledPackages();
const mdeps = mermaidDependencies();
const text = render(vend, pkgs, mdeps);

const problems = [];
for (const c of mdeps) {
  if (!allowed(c.license)) problems.push(`${c.name}@${c.version} (inside mermaid.min.js): license "${c.license}" is not on the allow list`);
  if (!c.text) problems.push(`${c.name}@${c.version} (inside mermaid.min.js): no license text found`);
}
for (const c of [...vend, ...pkgs]) {
  if (!allowed(c.license)) problems.push(`${c.name}@${c.version}: license "${c.license}" is not on the allow list`);
  if (c.version === 'UNKNOWN') problems.push(`${c.name}: version could not be read`);
  if (!c.text) problems.push(`${c.name}@${c.version}: no license text found; add one under memory-ui/licenses/`);
  if (!c.copyright) problems.push(`${c.name}@${c.version}: no copyright line found`);
}

const rel = relative(process.cwd(), outFile);
if (process.argv.includes('--check')) {
  const current = existsSync(outFile) ? readFileSync(outFile, 'utf8') : '';
  if (current !== text) {
    problems.push(`${rel} is out of date with package-lock.json. Run \`npm run notices\` in memory-ui and commit the result.`);
  }
} else {
  writeFileSync(outFile, text);
  console.log(`wrote ${rel}: ${vend.length} vendored, ${pkgs.length} bundled packages, ${mdeps.length} inside mermaid`);
}

if (problems.length) {
  for (const p of problems) console.error(`third-party notices: ${p}`);
  process.exit(1);
}
if (process.argv.includes('--check')) console.log(`${rel} is up to date (${pkgs.length} bundled packages)`);
