// The wiki page's path in READ mode (index.html: renderFileView).
//
// The path is the page's identity everywhere else — in a wiki_write call, in a
// link, in a message to somebody else — and it used to be visible only while
// editing, so reading a page meant opening the editor to find out what it was
// called.
//
// NOTE ON SCOPE: index.html is a 4k-line SPA that expects a live backend, a
// session and a TipTap bundle, so it is not executed here. The two things worth
// guarding are structural and can be read straight off the source: that read
// mode renders the path AND a copy control, and that the copy helper says what
// it copied. Both are what a refactor of the action bar would quietly drop.

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { JSDOM } from 'jsdom';

const __dirname = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(__dirname, '../static/index.html'), 'utf8');

let ok = true;
const fail = (m) => { console.error('❌ ' + m); ok = false; };

// The body of renderFileView(), which is what read mode paints.
const fn = (src.match(/function renderFileView\(\)\s*\{[\s\S]*?\n\}/) || [''])[0];
if (!fn) fail('renderFileView() not found in index.html');
else {
  // 1) The path is RENDERED in read mode.
  if (!/wiki-view-path/.test(fn))
    fail('read mode renders no path element — the path is still edit-only');
  if (!/\$\{esc\(currentFilePath\)\}/.test(fn))
    fail('read mode does not render currentFilePath');

  // 2) ESCAPED, not interpolated raw. A page path is user-controlled (anyone
  //    who can create a page picks it), and this is an innerHTML template.
  const pathLine = (fn.match(/.*wiki-view-path.*/) || [''])[0];
  if (/\$\{currentFilePath\}/.test(pathLine))
    fail(`the path is interpolated unescaped: ${pathLine.trim()}`);

  // 3) A COPY control, wired to something that copies the path. Showing the
  //    path without one just moves the retyping from the editor to read mode.
  if (!/copyWikiPath\(\)/.test(fn))
    fail('read mode offers no copy-path control');

  // 4) The old layout pinned everything to the right with justify-content:
  //    flex-end, which leaves no room for a path on the left.
  if (/justify-content:\s*flex-end/.test(fn))
    fail('the read-mode path bar still right-aligns everything — the path has nowhere to sit');
}

// 5) The helper copies the PATH and says so. copyText() used to hardcode
//    "Link copied", and being told a link was copied after copying a path is a
//    small lie that makes you go and check the clipboard.
const copier = (src.match(/function copyWikiPath\(\)[\s\S]*?\n\}/) ||
                src.match(/function copyWikiPath\(\)[^\n]*/) || [''])[0];
if (!copier) fail('copyWikiPath() is not defined');
else {
  if (!/currentFilePath/.test(copier)) fail('copyWikiPath() does not copy the current path');
  if (!/Path copied/.test(copier)) fail('copyWikiPath() does not say it copied a path');
}

// 6) copyText() takes a label at all, and still defaults for its old callers.
const copyText = (src.match(/function copyText\([\s\S]*?\n\}/) || [''])[0];
if (!copyText) fail('copyText() not found');
else {
  if (!/function copyText\(text,\s*\w+\)/.test(copyText))
    fail('copyText() takes no label — every caller would report "Link copied"');
  if (!/Link copied/.test(copyText))
    fail('copyText() lost the default label its existing callers rely on');
  // The async clipboard API needs a secure context and a permission that can be
  // refused. Swallowing that rejection looks exactly like a button that does
  // nothing, which is the worst outcome for a copy button.
  if (/\.catch\(\(\)\s*=>\s*\{\s*\}\)/.test(copyText))
    fail('a refused clipboard write is swallowed — the button silently does nothing');
}

// 7) The helper has to be reachable from an inline onclick, i.e. a top-level
//    function declaration rather than one nested inside another function.
{
  const dom = new JSDOM('<!doctype html><html><body></body></html>');
  const decl = /^function copyWikiPath\(\)/m.test(src);
  if (!decl) fail('copyWikiPath() is not declared at top level — an inline onclick cannot see it');
  dom.window.close();
}

console.log(ok
  ? '✅ wiki read mode: path rendered and escaped, copy control wired, label reaches the toast'
  : '❌ wiki path check FAILED');
process.exit(ok ? 0 : 1);
