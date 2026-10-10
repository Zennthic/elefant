// Copies mermaid's self-contained browser build into static/mermaid.min.js.
//
// memory-ui ships as a plain nginx copy of static/ with no runtime build step,
// so mermaid is vendored (committed) exactly like marked.min.js and the Tiptap
// bundle. The version is the one package-lock.json pins; the header written
// here is what third-party-notices.mjs reads the version and copyright from.
//
//   npm run vendor:mermaid           rewrite static/mermaid.min.js
//   npm run vendor:mermaid -- --check exit 1 if the committed copy differs
//
// The diagram-renderer service pins the same mermaid version, so the wiki and
// the server-side renderer draw a diagram the same way. CI checks they agree.

import { readFileSync, writeFileSync, existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const uiDir = join(dirname(fileURLToPath(import.meta.url)), '..');
const require = createRequire(join(uiDir, 'package.json'));
const pkg = JSON.parse(readFileSync(require.resolve('mermaid/package.json'), 'utf8'));
const dist = readFileSync(require.resolve('mermaid/dist/mermaid.min.js'), 'utf8');
const header = `/*! mermaid v${pkg.version} | MIT License | https://github.com/mermaid-js/mermaid | Copyright (c) 2014 - 2022 Knut Sveidqvist */\n`;
const out = header + dist;
const target = join(uiDir, 'static', 'mermaid.min.js');

if (process.argv.includes('--check')) {
  const current = existsSync(target) ? readFileSync(target, 'utf8') : '';
  if (current !== out) {
    console.error(`static/mermaid.min.js does not match mermaid ${pkg.version} from package-lock.json. Run \`npm run vendor:mermaid\` and commit it.`);
    process.exit(1);
  }
  console.log(`static/mermaid.min.js is mermaid ${pkg.version}, as package-lock.json pins`);
} else {
  writeFileSync(target, out);
  console.log(`wrote static/mermaid.min.js: mermaid ${pkg.version}, ${(out.length / 1024).toFixed(0)} KiB`);
}
