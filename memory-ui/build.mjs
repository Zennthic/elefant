// Bundles the wiki editor into a single committed file: static/tiptap.bundle.js.
//
// memory-ui ships as `FROM nginx:alpine; COPY static/ …` — there is NO runtime
// build step, so the bundle is vendored (committed) exactly like static/marked.min.js.
// Run `npm run build:editor` after changing anything under src/ and commit the result.

import { build } from 'esbuild';
import { bundleOptions } from './build-options.mjs';

const result = await build(bundleOptions);

// Report the bundle size so regressions are visible in CI logs.
const out = result.metafile.outputs['static/tiptap.bundle.js'];
if (out) {
  console.log(`\ntiptap.bundle.js = ${(out.bytes / 1024).toFixed(1)} KiB (minified)`);
}
