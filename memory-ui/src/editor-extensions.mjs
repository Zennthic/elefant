// Shared TipTap extension set for the Elefant wiki editor.
//
// This is imported BOTH by the browser bundle (build.mjs → static/tiptap.bundle.js)
// AND by the Node round-trip test (test/roundtrip.mjs). Keeping the schema in one
// place is the whole point: the serialization test must exercise the exact same
// extensions the editor ships, or "stable re-save" proves nothing.

import { mergeAttributes, getHTMLFromFragment } from '@tiptap/core';
import { Fragment } from '@tiptap/pm/model';
import StarterKit from '@tiptap/starter-kit';
import Link from '@tiptap/extension-link';
import Image from '@tiptap/extension-image';
import { Table, TableRow, TableCell, TableHeader } from '@tiptap/extension-table';
import { TaskList, TaskItem } from '@tiptap/extension-list';
import { Markdown } from 'tiptap-markdown';

// Wiki images are stored as relative markdown paths (e.g. ../assets/images/x.png),
// but a relative src won't render inside the editor (it resolves against the SPA
// URL, not the wiki file). WikiImage keeps the stored `src` relative — so markdown
// serialization is unchanged — while rendering the DOM <img> through a resolver
// that maps it to /api/image?path=… for a correct in-editor preview.
//
// It also supports RESIZE. Markdown has no image-width syntax, so a resized width
// is persisted with the Obsidian-style `![alt|width](src)` convention (kept inside
// markdown image syntax, so html:false stays and prose angle-brackets are safe).
// The read view understands `|width` too (applyImageWidths in index.html). A
// NodeView adds a drag handle; the width round-trips through the markdown.
const WikiImage = Image.extend({
  addOptions() {
    return { ...this.parent?.(), resolveSrc: (src) => src };
  },
  addAttributes() {
    return {
      ...this.parent?.(),
      // Width in px. Parsed from an explicit width attr or an `alt` "|NNN" suffix.
      width: {
        default: null,
        parseHTML: (el) => {
          const w = el.getAttribute('width');
          if (w && /^\d+$/.test(w)) return parseInt(w, 10);
          const m = (el.getAttribute('alt') || '').match(/\|(\d+)\s*$/);
          return m ? parseInt(m[1], 10) : null;
        },
        renderHTML: (attrs) => (attrs.width ? { width: attrs.width } : {}),
      },
      // Override alt so the "|NNN" width suffix never leaks into the alt text.
      alt: {
        default: null,
        parseHTML: (el) => ((el.getAttribute('alt') || '').replace(/\|\d+\s*$/, '') || null),
        renderHTML: (attrs) => (attrs.alt ? { alt: attrs.alt } : {}),
      },
    };
  },
  renderHTML({ HTMLAttributes }) {
    const attrs = { ...HTMLAttributes };
    if (attrs.src) attrs.src = this.options.resolveSrc(attrs.src);
    return ['img', mergeAttributes(this.options.HTMLAttributes, attrs)];
  },
  addStorage() {
    // Custom markdown serialize: emit ![alt|width](src) when a width is set,
    // else the plain ![alt](src). tiptap-markdown reads this off storage.markdown.
    return {
      markdown: {
        serialize(state, node) {
          const alt = node.attrs.alt || '';
          const altOut = node.attrs.width ? `${alt}|${node.attrs.width}` : alt;
          const title = node.attrs.title
            ? ' "' + String(node.attrs.title).replace(/"/g, '\\"') + '"' : '';
          state.write('![' + state.esc(altOut) + '](' + (node.attrs.src || '') + title + ')');
        },
        parse: {},
      },
    };
  },
  addNodeView() {
    const resolve = this.options.resolveSrc;
    return ({ node, editor, getPos }) => {
      let current = node;
      const wrap = document.createElement('span');
      wrap.className = 'wiki-img-wrap';
      const img = document.createElement('img');
      const apply = (n) => {
        img.setAttribute('src', resolve(n.attrs.src || ''));
        if (n.attrs.alt) img.setAttribute('alt', n.attrs.alt); else img.removeAttribute('alt');
        img.style.width = n.attrs.width ? n.attrs.width + 'px' : '';
      };
      apply(node);
      wrap.appendChild(img);
      const handle = document.createElement('span');
      handle.className = 'wiki-img-handle';
      handle.contentEditable = 'false';
      wrap.appendChild(handle);

      let startX = 0, startW = 0, dragging = false;
      const onDown = (e) => {
        e.preventDefault(); e.stopPropagation();
        dragging = true; startX = e.clientX;
        startW = img.getBoundingClientRect().width || current.attrs.width || 300;
        try { handle.setPointerCapture(e.pointerId); } catch (_) {}
      };
      const onMove = (e) => {
        if (!dragging) return;
        img.style.width = Math.max(40, Math.round(startW + (e.clientX - startX))) + 'px';
      };
      const onUp = () => {
        if (!dragging) return;
        dragging = false;
        const w = Math.max(40, Math.round(img.getBoundingClientRect().width));
        if (typeof getPos === 'function') {
          editor.chain().command(({ tr }) => {
            const pos = getPos();
            if (typeof pos !== 'number') return false;
            tr.setNodeMarkup(pos, undefined, { ...current.attrs, width: w });
            return true;
          }).run();
        }
      };
      handle.addEventListener('pointerdown', onDown);
      handle.addEventListener('pointermove', onMove);
      handle.addEventListener('pointerup', onUp);

      return {
        dom: wrap,
        ignoreMutation: (m) => m.type !== 'selection',
        update: (updated) => {
          if (updated.type.name !== current.type.name) return false;
          current = updated;
          apply(updated);
          return true;
        },
        destroy: () => {
          handle.removeEventListener('pointerdown', onDown);
          handle.removeEventListener('pointermove', onMove);
          handle.removeEventListener('pointerup', onUp);
        },
      };
    };
  },
});

// Tiptap 3 table cells carry an `align` attribute, parsed from the
// `:---:` delimiter row (markdown-it emits it as a text-align style). The
// tiptap-markdown 0.9 table serializer still writes a plain `---` delimiter,
// so the alignment would be dropped on save and the doc would not survive a
// re-load. WikiTable writes the delimiter from the header cells' alignment.
// Everything else follows tiptap-markdown's own table serializer: a table
// markdown cannot express (spans, header cells in the body, more than one
// block in a cell) falls back to HTML, or to a `[table]` placeholder when
// html is off.
const DELIMITER = { left: ':---', center: ':---:', right: '---:' };

function hasSpan(cell) {
  return cell.attrs.colspan > 1 || cell.attrs.rowspan > 1;
}

function isMarkdownTable(table) {
  let ok = true;
  table.forEach((row, _p, i) => {
    row.forEach((cell) => {
      const header = cell.type.name === 'tableHeader';
      if ((i === 0) !== header || hasSpan(cell) || cell.childCount > 1) ok = false;
    });
  });
  return ok;
}

const WikiTable = Table.extend({
  addStorage() {
    return {
      markdown: {
        serialize(state, node, parent) {
          if (!isMarkdownTable(node)) {
            if (this.editor.storage.markdown.options.html) {
              state.write(getHTMLFromFragment(Fragment.from(node), node.type.schema));
            } else {
              console.warn('Tiptap Markdown: "table" node is only available in html mode');
              state.write('[table]');
            }
            state.closeBlock(node);
            return;
          }
          state.inTable = true;
          node.forEach((row, _p, i) => {
            state.write('| ');
            row.forEach((cell, _q, j) => {
              if (j) state.write(' | ');
              if (cell.firstChild.textContent.trim()) state.renderInline(cell.firstChild);
            });
            state.write(' |');
            state.ensureNewLine();
            if (!i) {
              const cols = [];
              row.forEach((cell) => cols.push(DELIMITER[cell.attrs.align] || '---'));
              state.write(`| ${cols.join(' | ')} |`);
              state.ensureNewLine();
            }
          });
          state.closeBlock(node);
          state.inTable = false;
        },
        parse: {},
      },
    };
  },
});

// Returns the extension array. `opts` lets the browser pass UI-only bits
// (e.g. Placeholder) and the image src resolver that the headless test omits.
export function wikiExtensions(opts = {}) {
  const exts = [
    StarterKit.configure({
      // We keep StarterKit's defaults; codeBlock stays the plain fenced block
      // (no lowlight highlighting yet — that's a later polish, not a schema change).
      heading: { levels: [1, 2, 3, 4, 5, 6] },
      // Tiptap 3's StarterKit also bundles Link, Underline and TrailingNode.
      // Link is configured on its own below. Underline has no markdown syntax
      // and TrailingNode would add an empty paragraph to every doc, so both
      // stay off to keep the schema (and the saved markdown) as it was on v2.
      link: false,
      underline: false,
      trailingNode: false,
    }),
    Link.configure({
      openOnClick: false,      // in the editor, clicking a link should place the cursor
      autolink: true,
      linkOnPaste: true,
      HTMLAttributes: { rel: 'noopener noreferrer nofollow' },
    }),
    WikiImage.configure({
      // Images are inline in markdown by syntax (![...](...)); modelling them as
      // an inline node inside a paragraph is what keeps a standalone image
      // properly block-separated from the next heading on round-trip.
      inline: true,
      allowBase64: false,      // wiki images go through /api/upload-image, never inline base64
      resolveSrc: opts.resolveImageSrc || ((src) => src),
    }),
    WikiTable.configure({ resizable: false }),
    TableRow,
    TableHeader,
    TableCell,
    TaskList,
    TaskItem.configure({ nested: true }),
    // tiptap-markdown: gives editor.storage.markdown.getMarkdown() and lets
    // setContent(markdownString) parse markdown. tightLists matches how our
    // existing pages are written (no blank line between list items).
    Markdown.configure({
      html: false,             // don't pass raw HTML through — keep the doc in the schema
      tightLists: true,
      bulletListMarker: '-',
      linkify: false,
      breaks: false,
      transformPastedText: true,
      transformCopiedText: true,
    }),
  ];
  if (opts.placeholder) {
    // Placeholder is browser-only chrome; imported lazily by the bundle.
    exts.push(opts.placeholder);
  }
  return exts;
}
