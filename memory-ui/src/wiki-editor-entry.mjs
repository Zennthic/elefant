// Browser entry for the memory-ui wiki editor bundle.
//
// esbuild bundles this (IIFE) into static/tiptap.bundle.js, which is COMMITTED
// and served by the plain nginx static image — memory-ui has no runtime build
// step. The SPA (a no-module inline-JS page) consumes the global below.
//
// This file is the reusable editor CORE (create / get markdown / set markdown /
// destroy). Phase 1 builds the toolbar, mode toggle, frontmatter meta bar and
// save wiring in the SPA on top of this surface.

import { Editor } from '@tiptap/core';
import { Placeholder } from '@tiptap/extensions';
import { wikiExtensions } from './editor-extensions.mjs';

// Resolve a stored (relative) image src to a URL that renders in the editor.
// Absolute / data / already-API srcs pass through; relative srcs are resolved
// against the current file's directory and served via /api/image.
function makeImageResolver(filePath) {
  return (src) => {
    if (!src || /^(https?:|data:|blob:)/i.test(src) || src.startsWith('/api/')) return src;
    const dir = (filePath && filePath.includes('/')) ? filePath.split('/').slice(0, -1) : [];
    const stack = [...dir];
    for (const part of src.split('/')) {
      if (part === '..') stack.pop();
      else if (part && part !== '.') stack.push(part);
    }
    return '/api/image?path=' + encodeURIComponent(stack.join('/'));
  };
}

function createWikiEditor(element, opts = {}) {
  const extensions = wikiExtensions({
    placeholder: opts.placeholder
      ? Placeholder.configure({ placeholder: opts.placeholder })
      : null,
    resolveImageSrc: makeImageResolver(opts.filePath || ''),
  });
  const config = {
    element,
    extensions,
    content: opts.markdown || '',   // parsed as markdown by tiptap-markdown
    autofocus: opts.autofocus ?? false,
    editable: opts.editable ?? true,
  };
  // Only set onUpdate when it's a real callback. Passing `undefined` here
  // OVERRIDES TipTap's default no-op listener with undefined, which then throws
  // (`undefined.apply`) the first time the doc mutates. Callers that want update
  // events should use the returned editor's `.on('update', …)` instead.
  if (typeof opts.onUpdate === 'function') config.onUpdate = opts.onUpdate;
  const editor = new Editor(config);
  return {
    editor,
    getMarkdown: () => editor.storage.markdown.getMarkdown(),
    setMarkdown: (md) => editor.commands.setContent(md || ''),
    isEditable: () => editor.isEditable,
    setEditable: (v) => editor.setEditable(!!v),
    focus: () => editor.commands.focus(),
    destroy: () => editor.destroy(),
  };
}

// Global surface for the no-build SPA.
window.ElefantWikiEditor = { createWikiEditor };

export { createWikiEditor };
