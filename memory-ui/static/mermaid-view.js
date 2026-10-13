/*
 * Draws ```mermaid code blocks in a rendered wiki page as diagrams.
 *
 * A diagram is stored as Mermaid TEXT inside the page (that is what agents
 * create and patch through the MCP diagram tools, and what History diffs), so
 * the reader turns it into a picture here, in the browser. Pages without a
 * mermaid block never load mermaid at all: /mermaid.min.js is several
 * megabytes and is fetched on first use, then reused.
 *
 * securityLevel "strict": labels are sanitised and click handlers, links and
 * scripts in the diagram are refused, because diagram text can come from an
 * agent that read something untrusted. Mermaid's SVG is inserted only after
 * it has been through mermaid's own DOMPurify pass.
 *
 * Used by index.html (the reader, the split-mode preview and old revisions)
 * and share.html. Exposes one function:
 *
 *   renderMermaidBlocks(root) -> Promise<number of diagrams drawn>
 */
(function () {
  'use strict';

  var loading = null;
  var seq = 0;

  function loadMermaid() {
    if (window.mermaid) return Promise.resolve(window.mermaid);
    if (!loading) {
      loading = new Promise(function (resolve, reject) {
        var s = document.createElement('script');
        s.src = '/mermaid.min.js';
        s.async = true;
        s.onload = function () {
          if (window.mermaid) resolve(window.mermaid);
          else reject(new Error('the diagram library did not load'));
        };
        s.onerror = function () {
          loading = null;   // let the next page try again
          reject(new Error('the diagram library could not be loaded'));
        };
        document.head.appendChild(s);
      });
    }
    return loading;
  }

  function mermaidTheme() {
    return document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'default';
  }

  function sourceDetails(pre, open) {
    var d = document.createElement('details');
    d.className = 'mermaid-source';
    if (open) d.open = true;
    var s = document.createElement('summary');
    s.textContent = 'Mermaid source';
    d.appendChild(s);
    var copy = pre.cloneNode(true);
    copy.setAttribute('data-mermaid', 'source');   // shown as text, never drawn
    d.appendChild(copy);
    return d;
  }

  async function renderMermaidBlocks(root) {
    if (!root || !root.querySelectorAll) return 0;
    // Claimed synchronously, so two passes over the same page (the split
    // preview re-renders while an earlier pass is still drawing) never draw
    // one block twice.
    var codes = Array.prototype.slice.call(root.querySelectorAll('pre > code.language-mermaid'))
      .filter(function (c) {
        if (c.parentElement.hasAttribute('data-mermaid')) return false;
        c.parentElement.setAttribute('data-mermaid', 'claimed');
        return true;
      });
    if (!codes.length) return 0;

    var mermaid;
    try {
      mermaid = await loadMermaid();
    } catch (e) {
      codes.forEach(function (c) {
        c.parentElement.removeAttribute('data-mermaid');   // a later page view may retry
        c.parentElement.title = 'Diagram not drawn: ' + e.message;
      });
      return 0;
    }
    mermaid.initialize({
      startOnLoad: false,
      securityLevel: 'strict',
      theme: mermaidTheme(),
      // A bad diagram is reported below, in words, rather than as mermaid's
      // own "syntax error" bomb graphic appended to the page.
      suppressErrorRendering: true,
    });

    var drawn = 0;
    for (var i = 0; i < codes.length; i++) {
      var pre = codes[i].parentElement;
      // The split-mode preview re-renders on every keystroke; a block from an
      // earlier render may already be gone by the time its turn comes.
      if (!pre || !pre.isConnected) continue;
      var id = 'wiki-mermaid-' + (++seq);
      var fig = document.createElement('figure');
      fig.className = 'mermaid-diagram';
      try {
        var out = await mermaid.render(id, codes[i].textContent);
        fig.innerHTML = out.svg;
        fig.appendChild(sourceDetails(pre, false));
        drawn++;
      } catch (e) {
        fig.classList.add('mermaid-error');
        var msg = document.createElement('div');
        msg.className = 'mermaid-error-msg';
        msg.textContent = 'This diagram could not be drawn: '
          + String((e && e.message) || e).split('\n')[0];
        fig.appendChild(msg);
        fig.appendChild(sourceDetails(pre, true));
        var stray = document.getElementById('d' + id);   // mermaid's scratch node
        if (stray) stray.remove();
      }
      if (pre.isConnected) pre.replaceWith(fig);
    }
    return drawn;
  }

  window.renderMermaidBlocks = renderMermaidBlocks;
})();
