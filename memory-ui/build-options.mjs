// The esbuild options for static/tiptap.bundle.js, shared by build.mjs and
// scripts/third-party-notices.mjs. The notice list is generated from what
// esbuild actually pulls into the bundle, so both must bundle the same thing.

import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));

export const bundleOptions = {
  absWorkingDir: __dirname,
  entryPoints: [join(__dirname, 'src/wiki-editor-entry.mjs')],
  bundle: true,
  format: 'iife',
  outfile: join(__dirname, 'static/tiptap.bundle.js'),
  minify: true,
  sourcemap: false,
  target: ['es2020'],
  legalComments: 'none',
  logLevel: 'info',
  metafile: true,
};
