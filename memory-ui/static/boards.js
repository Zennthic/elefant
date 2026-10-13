/* Native Board tab. Ported from the retired /editor/board.html into memory-ui.
 * Wrapped in an IIFE so nothing leaks to the global scope (no collision with the
 * SPA's esc/load/render/etc.); the only global is `window.Boards`, whose methods
 * are the inline onclick handlers. All element classes are b-prefixed and scoped
 * under #boards-root (see boards.css). Same /api/board/* backend, same-origin
 * session auth. Drag uses the same SortableJS config as the original board. */
(function () {
  const API = '/api/board';
  // Column colours a person picks. Literal on purpose: they are DATA — the
  // colour is stored on the column and has to mean the same column in every
  // palette — and they only ever paint a thin top stripe, never text.
  // (Allow-listed in test_ui_palettes.py.)
  const SWATCHES = ['#818CF8', '#34d399', '#f472b6', '#fbbf24', '#60a5fa', '#f87171', '#a78bfa', '#2dd4bf'];

  let COLUMNS = [], CARDS = [], STAGES = [];
  // ref → display record for every person the loaded cards name, plus the
  // directory the pickers are built from. People are stored on a card as
  // opaque references and named here, at render time, so renaming somebody
  // renames them everywhere at once.
  let PEOPLE = {}, DIRECTORY = [], ME_REF = '', _directoryLoaded = false;
  // Whether the Closed column is on the board. A preference, not a mode: the
  // rest of the board stays live and normal while it is shown, and the choice
  // survives a reload because "show me what I closed" is a way of working, not
  // a one-off look.
  let _editSlug = null, _editColId = null, _dragging = false;
  let _showClosed = (() => {
    try { return localStorage.getItem('elefant_board_show_closed') === '1'; }
    catch (e) { return false; }   // private window / blocked storage
  })();
  let _search = '', _labelFilter = '', _repoFilter = '', _personFilter = '', _colColor = null, _searchTimer = null, _boardSortable = null;
  let _editCard = null, _wikiPagesLoaded = false, _built = false, _pendingCard = null;
  let _pollTimer = null, _sig = '';
  // Live stream state. `_streamLive` is only true once the server has actually
  // said hello on the socket — an EventSource object exists the instant you
  // construct one, long before anything proves the stream works, so trusting
  // its existence would slow the poll down on the strength of a connection that
  // may never deliver anything.
  let _stream = null, _streamLive = false, _streamRetry = 0, _streamRetryTimer = null;
  let _deferredRefresh = false;

  // The name to show for a principal reference. Falls back to the reference
  // itself: the enrichment is best-effort, and a card that renders a raw
  // `user:9f2c…` is worse to read but still tells the truth, where a blank
  // chip would say the card names nobody.
  const personRec = ref => (PEOPLE && PEOPLE[ref]) || (DIRECTORY.find(p => p.ref === ref)) || null;
  function personName(ref) {
    const rec = personRec(ref);
    return rec ? (rec.display || ref) : ref;
  }
  function personTitle(ref) {
    const rec = personRec(ref);
    if (!rec) return ref;
    const bits = [rec.display || ref];
    if (rec.handle) bits.push('@' + rec.handle);
    if (rec.kind === 'agent') bits.push('agent');
    if (rec.known && !rec.active) bits.push('no longer active');
    if (!rec.known) bits.push('no longer on this board');
    return bits.join(' · ');
  }

  const esc = s => (s == null ? '' : String(s)).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const $ = id => document.getElementById(id);

  // A programmatic write to a field is INVISIBLE to the `input` listeners that
  // carry auto-save and the comment draft, so text put there by code was never
  // kept: a pasted screenshot uploaded, appeared, and died with the modal
  //. Dispatching the event the browser would have sent keeps one
  // save path rather than growing a second one beside it — anything wired to
  // `input` later is covered by construction.
  function fireInput(el) {
    el?.dispatchEvent(new Event('input', { bubbles: true }));
  }

  // ── Rendering a description ─────────────────────────────────────────────────
  // A description is written by agents as often as by people, and rendering
  // markdown means the board stops escaping it and starts putting it in the
  // DOM as markup. So the output is SANITISED rather than trusted: marked
  // decides what the markdown means, and this decides what is allowed to
  // survive that. An allowlist, because a blocklist is a list of the attacks
  // somebody already thought of.
  //
  // marked v9 has no sanitize option of its own (it was removed in v5, on the
  // grounds that a markdown parser is the wrong place for it), so this is not
  // duplicating something the library does.
  const MD_TAGS = new Set([
    'P', 'BR', 'HR', 'EM', 'STRONG', 'DEL', 'CODE', 'PRE', 'BLOCKQUOTE',
    'UL', 'OL', 'LI', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'A', 'IMG',
    'TABLE', 'THEAD', 'TBODY', 'TR', 'TH', 'TD',
  ]);
  // An image may only come from OUR authenticated asset route. An arbitrary
  // src would let anything written into a description — by an agent, or by
  // anybody who can edit a card — beacon out to a host of their choosing the
  // moment somebody opens it, and would sidestep the rule that seeing an
  // attachment requires being able to see the board.
  const MD_IMG_SRC = /^\/api\/image\?path=/;
  // Only what a rendered description actually needs: a link's target, and the
  // language class marked puts on a fenced block so it can be styled. Every
  // event handler attribute is therefore gone by construction rather than by
  // being listed.
  const MD_ATTRS = { A: ['href'], CODE: ['class'], PRE: ['class'], IMG: ['src', 'alt'] };
  const MD_SAFE_SCHEME = /^(https?:|mailto:|#|\/)/i;
  const MENTION_HREF = /^(user|agent):[A-Za-z0-9._:-]+$/;

  // Turn `<a href="user:…">Jon Smith</a>` into a chip, in place.
  function mentionChip(a) {
    const ref = (a.getAttribute('href') || '').trim();
    const doc = a.ownerDocument;
    // The literal `@` sits in the text node before the link (the stored form is
    // `@[Name](ref)`), so it is folded into the chip rather than left stranded
    // beside it as a loose character.
    const prev = a.previousSibling;
    if (prev && prev.nodeType === 3 && /@$/.test(prev.nodeValue)) {
      prev.nodeValue = prev.nodeValue.slice(0, -1);
    }
    const rec = personRec(ref);
    if (!rec) {
      // Fail OPEN, like the rest of the people work: an account that is gone
      // renders as the name that was stored. Worse to read, never wrong — and
      // never a chip that claims to point at somebody who is not there.
      a.replaceWith(doc.createTextNode('@' + (a.textContent || ref)));
      return;
    }
    const span = doc.createElement('span');
    span.className = 'b-person-chip b-mention' + (rec.kind === 'agent' ? ' agent' : '');
    span.setAttribute('title', personTitle(ref));
    span.setAttribute('data-person', ref);
    span.textContent = '@' + personName(ref);
    a.replaceWith(span);
  }

  function sanitizeMarkdown(html) {
    const doc = new DOMParser().parseFromString(
      '<div id="r">' + html + '</div>', 'text/html');
    const root = doc.getElementById('r');
    // Walk a static list: the live tree is being edited underneath us.
    [...root.querySelectorAll('*')].forEach(el => {
      if (!MD_TAGS.has(el.tagName)) {
        // Unwrap rather than delete, so a description wrapped in a stray tag
        // still READS — except for the elements whose whole content is code,
        // which must not be promoted into the document.
        if (el.tagName === 'SCRIPT' || el.tagName === 'STYLE') el.remove();
        else el.replaceWith(...el.childNodes);
        return;
      }
      // A MENTION is stored as markdown's own link shape, `@[Name](user:…)`,
      // so it arrives here as an ordinary link with a principal reference for
      // an href. It becomes a chip, resolved NOW rather than from the stored
      // name — which is the whole point of storing the reference beside the
      // name: the text can go stale, the chip cannot.
      if (el.tagName === 'A' && MENTION_HREF.test((el.getAttribute('href') || '').trim())) {
        mentionChip(el);
        return;
      }
      if (el.tagName === 'IMG' && !MD_IMG_SRC.test((el.getAttribute('src') || '').trim())) {
        // Removed rather than stripped of its src: a src-less <img> is a
        // broken-image icon that says nothing about why.
        el.remove();
        return;
      }
      const allowed = MD_ATTRS[el.tagName] || [];
      [...el.attributes].forEach(a => {
        if (!allowed.includes(a.name.toLowerCase())) return el.removeAttribute(a.name);
        // `javascript:` is the one a link can still carry while looking
        // ordinary, and it is why href is checked rather than merely kept.
        if (a.name.toLowerCase() === 'href' && !MD_SAFE_SCHEME.test(a.value.trim())) {
          el.removeAttribute('href');
        }
      });
      if (el.tagName === 'A' && el.getAttribute('href')) {
        el.setAttribute('target', '_blank');
        el.setAttribute('rel', 'noopener noreferrer');
      }
    });
    return root.innerHTML;
  }

  // Falls back to escaped plain text when marked is not on the page, which is
  // a rendering downgrade rather than a hole.
  function renderMarkdown(text) {
    const src = text || '';
    if (!src.trim()) return '';
    if (typeof marked === 'undefined' || !marked.parse) {
      return `<p>${esc(src)}</p>`;
    }
    return sanitizeMarkdown(marked.parse(src, { breaks: true, gfm: true }));
  }

  function alertMsg(type, msg, ttl) {
    const el = $('b-alerts'); if (!el) return;
    const div = document.createElement('div');
    div.className = 'b-alert b-alert-' + type; div.textContent = msg;
    el.appendChild(div);
    setTimeout(() => div.remove(), ttl || (type === 'error' ? 6000 : 2500));
  }

  // ── API ────────────────────────────────────────────────────────────────────
  async function apiGet(path, params) {
    const qs = params ? '?' + new URLSearchParams(Object.entries(params).filter(([, v]) => v)).toString() : '';
    const res = await fetch(API + path + qs);
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || ('HTTP ' + res.status));
    return res.json();
  }
  async function apiSend(method, path, body) {
    const res = await fetch(API + path, {
      method, headers: { 'Content-Type': 'application/json' },
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || ('HTTP ' + res.status));
    return data;
  }

  // ── Shell ───────────────────────────────────────────────────────────────────
  function shellHTML() {
    return `
      <div class="b-topbar">
        <span class="b-heading">Board</span>
        <input class="b-search" id="b-search" type="search" placeholder="Search cards…" oninput="Boards.onSearch()">
        <span class="b-spacer"></span>
        <button class="b-btn b-btn-secondary b-btn-sm" id="b-closed-toggle" onclick="Boards.toggleClosed()"
                title="Show or hide the Closed column" aria-pressed="false">Closed</button>
        <button class="b-btn b-btn-secondary b-btn-sm" onclick="Boards.openColumnModal()">+ Column</button>
        <button class="b-btn b-btn-primary b-btn-sm" onclick="Boards.openCardModal()">+ New card</button>
      </div>
      <div class="b-filterbar" id="b-filterbar"><span>Filtered by</span> <span id="b-filter-chips"></span></div>
      <div class="b-alerts" id="b-alerts"></div>
      <div class="b-board" id="b-board"><div class="b-empty">Loading board…</div></div>

      <div id="b-card-modal" class="b-modal-overlay"><div class="b-modal">
        <h3>New card</h3>
        <div class="b-form-group"><label>Title</label>
          <input type="text" id="b-cm-title" placeholder="A short idea you're not building yet" maxlength="200"></div>
        <div class="b-form-group"><label>Description <span class="b-hint" style="float:right">paste a screenshot straight in</span></label>
          <div class="b-seg b-md-tabs" role="group" aria-label="Description view">
            <button type="button" class="sel" aria-pressed="true" onclick="Boards.showDescription('write', 'new')">Write</button>
            <button type="button" aria-pressed="false" onclick="Boards.showDescription('preview', 'new')">Preview</button></div>
          <textarea id="b-cm-body" class="b-quick-editor" placeholder="What is this, and what would done look like? Markdown and @mentions work here."></textarea>
          <div id="b-cm-body-preview" class="b-md-preview b-quick-preview" hidden></div></div>
        <div class="b-form-group"><label>Labels <span class="b-hint" style="float:right">comma or Enter adds one</span></label>
          ${labelFieldHTML('b-cm-labels', 'e.g. bug, enhancement, elefant')}</div>
        <div class="b-form-group"><label>GitHub repo <span class="b-hint" style="float:right">owner/name</span></label>
          <input type="text" id="b-cm-repo" placeholder="e.g. zennthic/elefant"></div>
        <div class="b-form-group"><label>Due date <span class="b-hint" style="float:right">optional</span></label>
          <input type="date" id="b-cm-due" class="b-due-input"></div>
        <div class="b-form-group"><label>Reporter <span class="b-hint" style="float:right">who raised it</span></label>
          ${peopleFieldHTML('b-cm-reporter', 'Type a name…')}
          <div class="b-hint">Starts as you. Pick someone else and you stay on the card as a participant.</div></div>
        <div class="b-form-group"><label>Assignee <span class="b-hint" style="float:right">who is doing it</span></label>
          ${peopleFieldHTML('b-cm-assignee', 'Type a name…')}
          <div class="b-hint">Leave unassigned if nobody has picked it up yet.</div></div>
        <div class="b-form-group"><label>Participants <span class="b-hint" style="float:right">add as many as you need</span></label>
          ${peopleFieldHTML('b-cm-participants', 'Type a name…')}</div>
        <div class="b-form-group"><label>Column</label><select id="b-cm-column"></select></div>
        <div class="b-modal-actions">
          <button class="b-btn b-btn-secondary" onclick="Boards.closeModal('b-card-modal')">Cancel</button>
          <button class="b-btn b-btn-primary" id="b-cm-submit" onclick="Boards.submitCard()">Add card</button>
        </div>
      </div></div>

      <div id="b-edit-card-modal" class="b-modal-overlay"><div class="b-modal b-modal-wide">
        <h3><span id="b-ec-number" class="b-ec-number" hidden title="Copy permalink to this card"
          onclick="Boards.copyPermalink(this.dataset.permalink)"></span>Edit card <span id="b-ec-slug" style="font-family:monospace;font-size:12px;color:var(--accent)"></span></h3>
        <div class="b-ec-split">
          <div class="b-ec-main">
        <div class="b-form-group"><label>Title</label><input type="text" id="b-ec-title" maxlength="200"></div>
        <div class="b-form-group"><label>Description</label>
          <div class="b-seg b-md-tabs" role="group" aria-label="Description view">
            <button type="button" class="sel" aria-pressed="true" onclick="Boards.showDescription('write')">Write</button>
            <button type="button" aria-pressed="false" onclick="Boards.showDescription('preview')">Preview</button></div>
          <div class="b-md-wrap">
            <textarea id="b-ec-body" class="b-md-editor"></textarea>
            <div class="b-ta-results" id="b-ec-body-results" role="listbox" hidden></div></div>
          <div id="b-ec-body-preview" class="b-md-preview" hidden></div>
          <div id="b-ec-mention-offer" class="b-mention-offer" hidden></div></div>
        <div class="b-form-group"><label>Wiki thread</label><div id="b-ec-wiki"></div>
          <datalist id="b-ec-wiki-pages"></datalist></div>
        <div class="b-form-group"><label>Related cards <span class="b-hint" style="float:right">blockers decide what can start</span></label>
          <div id="b-ec-rel"></div></div>
        <div class="b-form-group"><label>Comments <span class="b-hint" style="float:right">what happened to this card</span></label>
          <div id="b-ec-comments"></div>
          <div class="b-comment-new">
            <textarea id="b-ec-comment-body" rows="2" placeholder="Add a comment… markdown and @mentions work here too"></textarea>
            <button type="button" class="b-btn b-btn-secondary b-btn-sm" id="b-ec-comment-add">Comment</button>
          </div></div>
        <div class="b-form-group"><label>Attachments <span class="b-hint" style="float:right">paste a screenshot into the description or a comment</span></label>
          <div id="b-ec-attach"></div>
          <input type="file" id="b-ec-attach-file" style="display:none">
          <button type="button" class="b-btn b-btn-secondary b-btn-sm" id="b-ec-attach-btn">Attach a file</button></div>
          </div>
          <div class="b-ec-rail">
        <div class="b-form-group"><label>Column <span class="b-hint" style="float:right">same as a drag</span></label>
          <select id="b-ec-column" class="b-ec-select"></select></div>
        <div class="b-form-group"><label>Due date <span class="b-hint" style="float:right">no time, just the day</span></label>
          <div class="b-due-row">
            <input type="date" id="b-ec-due" class="b-due-input" aria-label="Due date">
            <button type="button" class="b-btn b-btn-secondary b-btn-sm" id="b-ec-due-clear" title="Take the due date off" onclick="Boards.clearDue()">Clear</button>
          </div>
          <div id="b-ec-due-state" class="b-hint"></div></div>
        <div class="b-form-group"><label>Labels <span class="b-hint" style="float:right">comma or Enter adds one</span></label>
          ${labelFieldHTML('b-ec-labels', 'e.g. bug, enhancement, elefant')}</div>
        <div class="b-form-group"><label>Reporter <span class="b-hint" style="float:right">who raised it</span></label>
          ${peopleFieldHTML('b-ec-reporter', 'Type a name…')}</div>
        <div class="b-form-group"><label>Assignee <span class="b-hint" style="float:right">who is doing it</span></label>
          ${peopleFieldHTML('b-ec-assignee', 'Type a name…')}</div>
        <div class="b-form-group"><label>Participants <span class="b-hint" style="float:right">add as many as you need</span></label>
          ${peopleFieldHTML('b-ec-participants', 'Type a name…')}</div>
        <div class="b-form-group"><label>GitHub repo <span class="b-hint" style="float:right">owner/name</span></label>
          <input type="text" id="b-ec-repo" placeholder="e.g. zennthic/elefant">
          <a id="b-ec-repo-link" class="b-ec-repo-link" target="_blank" rel="noopener noreferrer" hidden></a></div>
          </div>
        </div>
        <div class="b-modal-actions">
          <div class="spread b-ec-foot">
            <button class="b-btn b-btn-danger" onclick="Boards.closeCard()">Close card</button>
            <div id="b-ec-times" class="b-ec-times"></div>
          </div>
          <span id="b-ec-status" class="b-ec-status" role="status" aria-live="polite"></span>
          <button class="b-btn b-btn-secondary" onclick="Boards.closeModal('b-edit-card-modal')">Close</button>
        </div>
      </div></div>

      <div id="b-column-modal" class="b-modal-overlay"><div class="b-modal">
        <h3 id="b-col-modal-title">New column</h3>
        <div class="b-form-group"><label>Name <span class="b-hint" style="float:right">your own label</span></label>
          <input type="text" id="b-col-name" placeholder="e.g. Up Next" maxlength="80"></div>
        <div class="b-form-group"><label>Stage</label><select id="b-col-stage"></select>
          <div class="b-hint">The architected stage this column maps to (fixed vocabulary). The name is yours.</div></div>
        <div class="b-form-group"><label>Colour</label><div class="b-swatches" id="b-col-swatches"></div></div>
        <div class="b-modal-actions">
          <button class="b-btn b-btn-danger spread" id="b-col-archive" style="display:none" onclick="Boards.archiveColumn()">Archive</button>
          <button class="b-btn b-btn-secondary" onclick="Boards.closeModal('b-column-modal')">Cancel</button>
          <button class="b-btn b-btn-primary" id="b-col-submit" onclick="Boards.submitColumn()">Add column</button>
        </div>
      </div></div>`;
  }

  // Both of these dismiss a modal without going through closeModal(), so both
  // have to flush a refresh that was held back for it — otherwise dismissing a
  // card with Escape leaves the board frozen until the next tick.
  function wireMentions() {
    bodyMentions.attach();
    wireAttachments();
    $('b-ec-comment-add')?.addEventListener('click', addComment);
    $('b-ec-comment-body')?.addEventListener('input', saveCommentDraft);

    // Auto-save. The comment composer is POINTEDLY not in this list — it is a
    // draft until its own button posts it.
    ['b-ec-title', 'b-ec-body', 'b-ec-repo']
      .forEach(id => $(id)?.addEventListener('input', () => scheduleCardSave()));
    $('b-ec-repo')?.addEventListener('input', updateRepoLink);
    // A picked date is a discrete edit, like picking a person, so it saves at
    // once rather than waiting out a debounce meant for typing.
    $('b-ec-due')?.addEventListener('change', () => { updateDueState(); scheduleCardSave(0); });
    // Not part of auto-save: a column change is its own endpoint and its own
    // event, and it is discrete, so it goes at once.
    $('b-ec-column')?.addEventListener('change', e => {
      if (!_filling) moveCardToColumn(e.target.value);
    });
    // Ctrl/Cmd-S is muscle memory. It saves immediately rather than letting the
    // browser offer to save the page.
    $('b-edit-card-modal')?.addEventListener('keydown', e => {
      if ((e.ctrlKey || e.metaKey) && (e.key === 's' || e.key === 'S')) {
        e.preventDefault(); saveCard();
      }
    });
  }

  function wireModals() {
    document.querySelectorAll('#boards-root .b-modal-overlay').forEach(m =>
      m.addEventListener('click', e => {
        if (e.target === m) { flushCardSave(); m.classList.remove('open'); flushDeferredRefresh(); }
      }));
    // Escape closes the detail view. With auto-save it is now the ORDINARY way
    // out rather than a discard, so a debounce still ticking has to be fired
    // first — otherwise the last thing you typed is the one thing not saved.
    document.addEventListener('keydown', e => {
      if (e.key !== 'Escape') return;
      const open = document.querySelectorAll('#boards-root .b-modal-overlay.open');
      if (!open.length) return;
      flushCardSave();
      open.forEach(m => m.classList.remove('open'));
      flushDeferredRefresh();
    });
    // Closing the tab or following a link out is the same moment, minus the
    // chance to await anything — fire the pending save and let it race.
    window.addEventListener('beforeunload', () => { flushCardSave(); });
  }

  // ── Load + render ─────────────────────────────────────────────────────────
  // Signature of what's on the board, so polling only re-renders on real change.
  function _sigOf(cols, cards) {
    return JSON.stringify([
      (cols || []).map(c => [c.id, c.name, c.stage, c.position, c.color]),
      // `number` is in the signature so the migration that backfills numbers
      // surfaces on an open board. It never changes afterwards, so this costs
      // one comparison and buys the one moment it matters.
      //
      // People are here for the ordinary case rather than a migration: an
      // agent putting somebody on a card changes nothing else, so without
      // this an open board would never show it.
      // `blocked` and its blockers are here for the same reason people are:
      // linking two cards changes nothing else about either of them, so without
      // this an open board would show a card as startable long after something
      // was put in its way — and the chip exists precisely to stop that.
      (cards || []).map(c => [c.slug, c.column_id, c.position, c.title, c.body, (c.labels || []).join('|'), c.repo, c.reporter, c.assignee, c.due, (c.participants || []).join('|'), c.number, c.closed, c.blocked, c.comment_count, (c.blocked_by || []).map(b => b.slug).join('|')]),
    ]);
  }

  // ── Search syntax ───────────────────────────────────────────────────────────
  // The search bar takes `label: bug`, `repo: elefant`, `assignee: jon`
  // alongside ordinary words, so a board can be sliced without reaching for a
  // chip that may not be on screen. Every key here is a filter /board/cards
  // already takes — this is a second way to say the same thing, not a second
  // filtering implementation.
  //
  // Whitespace after the colon is allowed because it is what people type, and
  // the card asking for this wrote it that way. A quoted value carries spaces
  // ( label:"needs design" ); an unquoted one is a single word, the same rule
  // GitHub and Jira use, so `label: bug urgent` filters on bug and searches
  // for urgent rather than silently filtering on something nobody typed.
  const SEARCH_KEYS = ['label', 'repo', 'assignee', 'reporter', 'participant',
                       'person', 'column', 'stage'];
  // key: then either "quoted value" or one bare word.
  const SEARCH_TOKEN_RE = new RegExp(
    '\\b(' + SEARCH_KEYS.join('|') + ')\\s*:\\s*(?:"([^"]*)"|(\\S+))', 'gi');

  function parseSearch(raw) {
    const out = { text: '', filters: {} };
    const text = (raw || '').replace(SEARCH_TOKEN_RE, (m, key, quoted, bare) => {
      const val = (quoted !== undefined ? quoted : bare).trim();
      // An empty value (`label:` alone, or `label:""`) is someone mid-type, not
      // a filter on the empty string — which would match nothing and look
      // broken while they are still typing the rest.
      if (!val) return '';
      // First one wins. Two values for one key can only mean an edit in
      // progress, and the API takes one value per key anyway.
      if (out.filters[key.toLowerCase()] === undefined) out.filters[key.toLowerCase()] = val;
      return '';
    });
    out.text = text.replace(/\s+/g, ' ').trim();
    return out;
  }

  // A typed filter wins for the key it names; a chip still covers every key the
  // query is silent about, so clicking a label chip and then typing
  // `assignee: jon` narrows rather than replacing.
  const cardParams = () => {
    const p = parseSearch(_search);
    const f = p.filters;
    return {
      search: p.text,
      label: f.label || _labelFilter,
      repo: f.repo || _repoFilter,
      person: f.person || _personFilter,
      assignee: f.assignee || '',
      reporter: f.reporter || '',
      participant: f.participant || '',
      column: f.column || '',
      stage: f.stage || '',
      // A single request rather than a second one — ask for the closed cards
      // alongside the open ones and split them here. The board's columns
      // filter them out; the Closed column is the only thing that shows them.
      closed: _showClosed ? 'true' : '',
    };
  };

  async function load() {
    try {
      const [colData, cardData] = await Promise.all([
        apiGet('/columns'),
        apiGet('/cards', cardParams()),
      ]);
      COLUMNS = colData.columns || []; STAGES = colData.stages || [];
      CARDS = cardData.cards || [];
      PEOPLE = cardData.people || {};
      _sig = _sigOf(COLUMNS, CARDS);
      renderFilters(); render();
      loadDirectory();
      // Refetched with the board rather than cached for the session: a label
      // coined on one card should be offered on the next without a reload,
      // and one that just left its last card should stop being offered.
      refreshLabelsInUse();
      if (_pendingCard) { const s = _pendingCard; _pendingCard = null; cardClicked(s); }
    } catch (e) {
      const b = $('b-board'); if (b) b.innerHTML = '<div class="b-empty">Could not load the board: ' + esc(e.message) + '</div>';
    }
  }

  // The people this board can attribute a card to. Fetched once — what the
  // picker offers is exactly what the store will accept, so a card can never
  // be filed against somebody who is not really there.
  async function loadDirectory() {
    if (_directoryLoaded) return;
    try {
      const res = await apiGet('/people');
      DIRECTORY = res.people || [];
      ME_REF = res.me || '';
      _directoryLoaded = true;
    } catch (e) { /* pickers degrade to what the card already holds */ }
  }

  // Re-read the board and re-render ONLY when something actually changed. This
  // is what both the live stream and the poll call: the server's event says
  // only THAT the board changed, and the board is re-read through the same
  // endpoints a fresh load uses. One code path, one shape, no second
  // representation of a card to drift out of step with the first.
  async function refresh() {
    if (!_built) return;
    // Deferred, not dropped. A change landing while you are mid-drag or have a
    // card open still has to appear once you are done — and on a pushed stream
    // there is no next tick to catch it, because the push already happened.
    if (_dragging || document.hidden ||
        document.querySelector('#boards-root .b-modal-overlay.open')) {
      _deferredRefresh = true;
      return;
    }
    _deferredRefresh = false;
    try {
      const [colData, cardData] = await Promise.all([
        apiGet('/columns'),
        apiGet('/cards', cardParams()),
      ]);
      const cols = colData.columns || [], cards = cardData.cards || [];
      const sig = _sigOf(cols, cards);
      if (sig === _sig) return;              // nothing changed → leave the view alone
      COLUMNS = cols; STAGES = colData.stages || STAGES;
      CARDS = cards; _sig = sig;
      PEOPLE = cardData.people || PEOPLE;
      renderFilters(); render();
    } catch (e) { /* transient — the stream or the next tick will come back to it */ }
  }

  // Run a refresh that an open modal, a drag or a hidden tab made us postpone.
  function flushDeferredRefresh() { if (_deferredRefresh) refresh(); }

  // ── Live updates ────────────────────────────────────────────────────────────
  // The board is pushed, not polled: memory-api announces every card and column
  // change on an SSE stream, so an agent's edit lands on an open board in the
  // time it takes to re-read it rather than on the next poll.
  //
  // The poll stays, at a much slower beat, as the safety net. A stream can fail
  // in ways it cannot report — a proxy that buffers, a sleeping laptop, a
  // corporate middlebox that quietly holds the connection open and delivers
  // nothing — and every one of those looks like a board that has stopped
  // updating. So the poll is slowed when the stream proves itself and restored
  // the moment it stops, and the board keeps working with no stream at all.
  const POLL_FAST_MS = 6000;       // no stream: the poll IS the liveness
  const POLL_SAFETY_MS = 60000;    // stream live: a backstop, not the mechanism
  const STREAM_RETRY_MAX_MS = 60000;

  function pollInterval() { return _streamLive ? POLL_SAFETY_MS : POLL_FAST_MS; }

  function startPolling() {
    stopPolling();
    _pollTimer = setInterval(refresh, pollInterval());
  }
  function stopPolling() { if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; } }

  // Called whenever the stream's liveness changes, so the poll's beat always
  // matches whether the stream is actually carrying anything.
  function setStreamLive(live) {
    if (_streamLive === live) return;
    _streamLive = live;
    if (_pollTimer) startPolling();      // re-arm at the other interval
  }

  function startStream() {
    if (_stream || typeof EventSource === 'undefined') return;
    let es;
    try { es = new EventSource(API + '/stream'); }
    catch (e) { return; }                // no stream available; the poll covers us
    _stream = es;

    // `hello` is the server's own first line. Treating THAT as proof of life,
    // rather than the EventSource opening, is the difference between knowing
    // the whole chain delivers and knowing a socket connected.
    es.addEventListener('hello', () => { _streamRetry = 0; setStreamLive(true); });

    // Every change event means the same thing — go and look. Listening on
    // `message` alone would miss them all, because the server names each event
    // (card.added, column.updated, …) and a named event never fires `message`.
    // The three lock events matter more here than most: the indicator exists so
    // a human does not start working a card a reviewer already has, and one that
    // only appeared on the next poll would let them start it in the meantime.
    ['card.added', 'card.updated', 'card.moved', 'card.archived', 'card.unarchived',
     'card.linked', 'card.commented',
     'card.eval_locked', 'card.eval_unlocked', 'card.eval_lock_broken',
     'column.added', 'column.updated', 'column.archived'].forEach(name =>
      es.addEventListener(name, () => { setStreamLive(true); refresh(); }));

    // The relay reports an upstream failure on the stream rather than as a
    // status code, because by then the response has already begun.
    es.addEventListener('stream.error', () => { closeStream(); scheduleStreamRetry(); });

    es.onerror = () => {
      // EventSource reconnects on its own, but only for a transport blip: a 401
      // after a session expires, or a proxy that refuses the route, leaves it
      // retrying a route that will never work. Falling back to the fast poll
      // costs a request every six seconds and keeps the board correct, which is
      // the trade worth making.
      setStreamLive(false);
      if (es.readyState === EventSource.CLOSED) { closeStream(); scheduleStreamRetry(); }
    };
  }

  function closeStream() {
    if (_stream) { try { _stream.close(); } catch (e) { /* already gone */ } _stream = null; }
    setStreamLive(false);
  }

  // Backing off matters more here than it looks: a board left open overnight
  // against a stream that cannot work would otherwise reconnect thousands of
  // times before anyone sees it.
  function scheduleStreamRetry() {
    if (_streamRetryTimer || !_pollTimer) return;   // not watching → don't reconnect
    const wait = Math.min(STREAM_RETRY_MAX_MS, 2000 * Math.pow(2, _streamRetry++));
    _streamRetryTimer = setTimeout(() => {
      _streamRetryTimer = null;
      if (_pollTimer) startStream();                // still on the Board tab
    }, wait);
  }

  function startLive() { startPolling(); startStream(); }
  function stopLive() {
    stopPolling(); closeStream();
    if (_streamRetryTimer) { clearTimeout(_streamRetryTimer); _streamRetryTimer = null; }
    _streamRetry = 0;
  }

  // Coming back to a backgrounded tab should show the board as it is NOW, not
  // as it was when you left. Both paths skip work while hidden, so without this
  // you would stare at a stale board for up to a whole interval.
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && _pollTimer) refresh();
  });

  // A closed card keeps its column_id — it came from somewhere and can go back
  // there — so the board's columns have to filter it out explicitly. Only the
  // Closed column shows it.
  const cardsFor = colId =>
    CARDS.filter(c => !c.closed && c.column_id === colId).sort((a, b) => a.position - b.position);

  // Most-recently closed first: the one you just closed is the one you might
  // have closed by mistake, and position means nothing once a card is off the
  // board.
  const closedCards = () =>
    CARDS.filter(c => c.closed)
         .sort((a, b) => String(b.closed_at || '').localeCompare(String(a.closed_at || '')));

  function cardHTML(c) {
    const closed = !!c.closed;
    // The status dot. It says who wrote the card -- EXCEPT while an automated
    // reviewer holds the card's evaluation lock, when the same slot says that
    // instead.
    //
    // Reusing the slot rather than adding a second dot is the design's call, and
    // the reason it does not lose anything is that the two facts are not equally
    // urgent. Who typed a card is permanent and answerable by opening it; a held
    // lock is transient and is the one thing that should stop you touching the
    // card at all. The source meaning comes back the moment the lock clears.
    //
    // (The design brief said this slot was "purely cosmetic, wired to nothing".
    // It was not -- it has always been wired to `source`, which is why the
    // unlocked case below is left exactly as it was.)
    const held = c.eval_lock || null;
    const srcClass = held ? 'b-src-eval'
      : (c.source === 'ui' ? 'b-src-ui' : 'b-src-agent');
    // Said in the imperative, not as a field name: the point of the indicator is
    // that somebody scanning the board does not start working this card. The run
    // is named because it is what an operator would go and look up, and a stale
    // lock says so, since the honest reading of one is "that reviewer probably
    // died" rather than "busy".
    const srcTitle = held
      ? ('Under evaluation by ' + esc(held.run_id || 'an automated reviewer')
         + (held.since ? ', since ' + esc(String(held.since).slice(0, 16).replace('T', ' ')) : '')
         + (held.stale
            ? ' — that lock has gone stale, so the run holding it has probably died'
            : ' — leave it alone until it clears'))
      : (c.source === 'ui' ? 'Added by a person (this UI)' : 'Written by an agent (' + esc(c.source) + ')');
    const labels = (c.labels || []).map(l =>
      `<span class="b-card-label" data-label="${esc(l)}" onclick="event.stopPropagation(); Boards.filterLabel(this.dataset.label)">${esc(l)}</span>`).join('');
    const reopen = closed
      ? `<button class="b-btn b-btn-secondary b-btn-sm" onclick="event.stopPropagation(); Boards.reopenCard('${esc(c.slug)}')">Reopen</button>` : '';
    // The number is the card's immutable handle — the thing you say out loud
    // ("look at #33") and the thing a permalink is built from. It sits top-right
    // and copies the permalink on click. Absent on a card written before the
    // backfill, in which case no chip renders at all rather than "#null".
    // The value rides in a data attribute, not the onclick argument, for the
    // same reason the label and repo chips do.
    const number = c.number == null ? '' :
      `<span class="b-card-number" data-permalink="${esc(c.slug)}" title="Copy permalink to this card"` +
      ` onclick="event.stopPropagation(); Boards.copyPermalink(this.dataset.permalink)">#${esc(c.number)}</span>`;
    // Who raised the card and who is on it. The reporter leads and is marked
    // as such, because "whose card is this" is the question the row answers;
    // participants follow. Each filters on click, which is the whole point of
    // resolving people rather than writing names in the body.
    //
    // As with the label and repo chips, the value rides in a data attribute
    // rather than the onclick argument: an HTML attribute is entity-decoded
    // before the JS inside it is parsed, so esc() alone would not contain a
    // hostile value. References are validated server-side, but this renders
    // whatever the API hands it and must not lean on that for its own safety.
    // `role` is '' for a participant, 'reporter', or 'assignee'. The assignee
    // leads: "who has this" is the question a board is mostly scanned for, and
    // it is marked differently from the reporter because the two say opposite
    // things about a card and a single mark would blur them.
    const ROLE_MARK = { reporter: 'R', assignee: 'A' };
    const personChip = (ref, role) =>
      `<span class="b-card-person${role ? ' ' + role : ''}" data-person="${esc(ref)}" title="${esc(personTitle(ref))}"` +
      ` onclick="event.stopPropagation(); Boards.filterPerson(this.dataset.person)">` +
      `${role ? `<span class="b-person-mark">${ROLE_MARK[role]}</span>` : ''}${esc(personName(ref))}</span>`;
    const people = [
      ...(c.assignee ? [personChip(c.assignee, 'assignee')] : []),
      ...(c.reporter ? [personChip(c.reporter, 'reporter')] : []),
      ...(c.participants || []).map(p => personChip(p, '')),
    ].join('');
    // A blocked card stays exactly where it is — the design is explicit that it
    // must not migrate to a Blocked column — so the chip is the only thing that
    // says it cannot be started. It names the blocker rather than just saying
    // "blocked", because the useful next move is to go and open that card, and
    // clicking it does exactly that.
    //
    // As with every other chip here, the slug rides in a data attribute rather
    // than the onclick argument: an HTML attribute is entity-decoded before the
    // JS inside it is parsed, so esc() alone would not contain a hostile value.
    const blockers = (c.blocked_by || []);
    const blocked = !c.blocked || !blockers.length ? '' :
      `<div class="b-card-blocked" title="Blocked until ${esc(blockers.map(b => b.title || b.slug).join(', '))} ${blockers.length > 1 ? 'are' : 'is'} done">` +
      `<span class="b-blocked-mark">⛔</span>blocked by ` +
      blockers.map(b =>
        `<span class="b-blocker" data-slug="${esc(b.slug)}" title="${esc(b.title || b.slug)}"` +
        ` onclick="event.stopPropagation(); Boards.openCard(this.dataset.slug)">` +
        `${b.number == null ? esc(b.slug) : '#' + esc(b.number)}</span>`).join(' ') +
      `</div>`;
    return `<div class="b-card ${c.blocked ? 'blocked' : ''} ${held ? 'under-eval' : ''} ${closed ? 'closed' : ''}" data-slug="${esc(c.slug)}" data-position="${c.position}" onclick="Boards.cardClicked('${esc(c.slug)}')">
      <div class="b-card-head"><span class="b-src-dot ${srcClass}" title="${srcTitle}"></span><span class="b-card-title">${esc(c.title)}</span>${number}</div>
      <div class="b-card-body">${esc(c.body || '')}</div>
      ${held ? `<div class="b-card-eval" title="${srcTitle}"><span class="b-eval-mark">⏳</span>under evaluation${held.stale ? ' <span class="b-eval-stale">(stale)</span>' : ''}</div>` : ''}
      ${blocked}
      ${people ? `<div class="b-card-people">${people}</div>` : ''}
      ${labels ? `<div class="b-card-labels">${labels}</div>` : ''}
      ${c.repo ? `<div class="b-card-repo" data-repo="${esc(c.repo)}" title="${esc(c.repo)}" onclick="event.stopPropagation(); Boards.filterRepo(this.dataset.repo)">${esc(c.repo)}</div>` : ''}
      <div class="b-card-foot">
        <span class="b-slug-chip" title="${esc(c.slug)}">${esc(c.slug)}</span>
        ${dueChipHTML(c)}
        ${(c.comment_count || 0) ? `<span class="b-comment-count" title="${esc(c.comment_count)} comment${c.comment_count === 1 ? '' : 's'} — open the card to read them">💬 ${esc(c.comment_count)}</span>` : ''}
        ${closed ? reopen : ''}
      </div>
    </div>`;
  }

  // Closed is a column, not a board mode. It is not a board_columns row — it is
  // a view of every card whose close time is set, which is why it is rendered
  // here rather than seeded into the database. It sits at the right-hand end,
  // it cannot be dragged or dropped into, and it takes no new cards: you close
  // a card from the card, and you reopen it from here.
  function closedColumnHTML() {
    const cs = closedCards();
    return `<div class="b-column b-column-closed" data-closed-column="1">
      <div class="b-column-header">
        <div class="b-column-title">
          <span class="b-column-name" title="Cards taken off the board. Reopen one to put it back.">Closed</span>
          <span class="b-column-stage">closed</span>
        </div>
        <span class="b-column-count">${cs.length}</span>
        <button class="b-btn b-btn-ghost" title="Hide the Closed column" onclick="event.stopPropagation(); Boards.toggleClosed()">×</button>
      </div>
      <div class="b-closed-cards">${
        cs.length ? cs.map(cardHTML).join('')
                  : '<div class="b-empty-col">Nothing closed yet.</div>'}</div>
    </div>`;
  }

  function render() {
    const board = $('b-board'); if (!board) return;
    if (!COLUMNS.length) { board.innerHTML = '<div class="b-empty">No columns yet.</div>'; return; }
    board.innerHTML = COLUMNS.map(col => {
      const cs = cardsFor(col.id);
      const accent = col.color || 'var(--line)';
      return `<div class="b-column" data-column-id="${col.id}" style="border-top-color:${esc(accent)}">
        <div class="b-column-header">
          <div class="b-column-title">
            <span class="b-column-name" title="${esc(col.name)}">${esc(col.name)}</span>
            <span class="b-column-stage">${esc(col.stage)}</span>
          </div>
          <span class="b-column-count">${cs.length}</span>
          <button class="b-btn b-btn-ghost" title="Edit column" onclick="event.stopPropagation(); Boards.openColumnModal('${col.id}')">⋯</button>
        </div>
        <div class="b-column-cards" data-column-id="${col.id}">${cs.map(cardHTML).join('')}</div>
        <button class="b-btn b-btn-secondary b-btn-sm b-col-add" onclick="Boards.openCardModal('${col.id}')">+ Card</button>
      </div>`;
    }).join('') + (_showClosed ? closedColumnHTML() : '');
    syncClosedToggle();
    initSortables();
  }

  function syncClosedToggle() {
    const btn = $('b-closed-toggle'); if (!btn) return;
    btn.classList.toggle('active', _showClosed);
    btn.setAttribute('aria-pressed', _showClosed ? 'true' : 'false');
  }

  function renderFilters() {
    const bar = $('b-filterbar'), chips = $('b-filter-chips'); if (!bar || !chips) return;
    const parts = [];
    if (_labelFilter) parts.push(`<span class="b-filter-chip">label: ${esc(_labelFilter)} <span class="x" onclick="Boards.filterLabel('')">×</span></span>`);
    if (_repoFilter) parts.push(`<span class="b-filter-chip">repo: ${esc(_repoFilter)} <span class="x" onclick="Boards.filterRepo('')">×</span></span>`);
    // Named, not referenced: "person: user:9f2c…" would be a filter nobody can
    // read. The filter still carries the reference; only the chip is human.
    if (_personFilter) parts.push(`<span class="b-filter-chip">person: ${esc(personName(_personFilter))} <span class="x" onclick="Boards.filterPerson('')">×</span></span>`);
    // What the QUERY is filtering on, shown the same way a clicked chip is —
    // otherwise a typed `assignee: jon` silently narrows the board and the
    // filter bar claims nothing is applied.
    const parsed = parseSearch(_search);
    Object.keys(parsed.filters).forEach(k => {
      // The key rides in a data attribute rather than the onclick argument, for
      // the reason the label and repo chips already do: an HTML attribute is
      // entity-decoded before the JS inside it is parsed.
      parts.push(`<span class="b-filter-chip">${esc(k)}: ${esc(parsed.filters[k])} ` +
        `<span class="x" data-unfilter="${esc(k)}" title="Remove this filter">×</span></span>`);
    });
    if (parsed.text) parts.push(`<span class="b-filter-chip">“${esc(parsed.text)}” <span class="x" onclick="Boards.clearSearch()">×</span></span>`);
    chips.innerHTML = parts.join(' ');
    chips.querySelectorAll('[data-unfilter]').forEach(el =>
      el.addEventListener('click', () => dropSearchFilter(el.dataset.unfilter)));
    bar.classList.toggle('on', parts.length > 0);
  }

  // Removing a typed filter rewrites the QUERY, because the query is where it
  // came from. Clearing only the applied filter would leave the text sitting in
  // the box, and the next keystroke would put it straight back.
  function dropSearchFilter(key) {
    if (!SEARCH_KEYS.includes(key)) return;
    let dropped = false;
    const next = (_search || '').replace(SEARCH_TOKEN_RE, (m, k) => {
      // Only the first occurrence is the one that was applied (parseSearch
      // takes the first), so only that one comes out.
      if (!dropped && k.toLowerCase() === key) { dropped = true; return ''; }
      return m;
    }).replace(/\s+/g, ' ').trim();
    _search = next;
    const box = $('b-search'); if (box) box.value = next;
    load();
  }

  // ── Drag ────────────────────────────────────────────────────────────────────
  // On touch, a finger dragging a scrollable stack means SCROLL — that is what it
  // means everywhere else on a phone. Letting the drag handler infer a card move
  // from it made a tall column unscrollable without accidentally reordering it, or
  // worse, flinging a card sideways into another column. A column change is a
  // stage change, so that is not a cosmetic slip.
  //
  // So scrolling is the DEFAULT reading of a touch drag, and moving a card has to
  // be armed deliberately: press and hold. `delayOnTouchOnly` keeps the mouse
  // immediate, so desktop drag is untouched by this.
  const TOUCH_ARM_MS = 400;      // firm enough not to fire on a scroll flick, short enough not to feel broken
  const TOUCH_WOBBLE_PX = 9;     // finger drift tolerated during the hold before it counts as a scroll

  // Shared by cards and columns. Both live in a scroller (the column body scrolls
  // vertically, the board scrolls horizontally), so both had the same defect.
  const touchArming = {
    delay: TOUCH_ARM_MS,
    delayOnTouchOnly: true,
    touchStartThreshold: TOUCH_WOBBLE_PX,
    // Once a drag IS armed, a tall stack is still taller than the screen, so the
    // drag needs to carry the list with it towards an off-screen target.
    scroll: true, bubbleScroll: true, scrollSensitivity: 64, scrollSpeed: 12,
    onChoose: armFeedback,
  };

  // The hold has to announce itself, or a user who held long enough cannot tell
  // that from a gesture that simply failed. Sortable fires `choose` only once the
  // delay has elapsed, so this is exactly the arming moment. The chosenClass
  // lifts the card (see boards.css); this adds the haptic tick where there is one.
  function armFeedback() {
    try { if (navigator.vibrate) navigator.vibrate(12); } catch (e) { /* no haptics here */ }
  }

  function initSortables() {
    if (typeof Sortable === 'undefined') { setTimeout(initSortables, 100); return; }
    if (_boardSortable) { _boardSortable.destroy(); _boardSortable = null; }
    // The Closed column's cards live under `.b-closed-cards`, which this
    // selector deliberately does not match: a closed card is not draggable and
    // the column is not a drop target. Dropping a card there would have to mean
    // "close it", and closing is a decision taken on the card, not a gesture
    // that a mis-aimed drag can perform.
    document.querySelectorAll('#boards-root .b-column-cards').forEach(el => Sortable.create(el, {
      ...touchArming,
      group: 'cards', animation: 140, ghostClass: 'sortable-ghost', chosenClass: 'sortable-chosen', dragClass: 'sortable-drag',
      onEnd: onCardDropped,
    }));
    _boardSortable = Sortable.create($('b-board'), {
      ...touchArming,
      // Closed is pinned to the right-hand end and has no position of its own,
      // so it is not in the column reorder.
      animation: 140, handle: '.b-column-header',
      draggable: '.b-column:not(.b-column-closed)', onEnd: onColumnDropped,
    });
  }

  function midpoint(prev, next, fallbackEnd) {
    if (prev !== null && next !== null) return (prev + next) / 2;
    if (prev !== null) return prev + 1;
    if (next !== null) return next - 1;
    return fallbackEnd;
  }

  // A drag that ends where it began is not a move. That used to be unreachable
  // (a mouse drag needs travel to start at all), but arming by hold means a press
  // that never travels now opens and closes a real drag — which would PATCH the
  // card to the position it is already in. `_dragging` still gets set, so the
  // press-and-release is swallowed rather than opening the card.
  const droppedInPlace = evt => evt.from === evt.to && evt.oldIndex === evt.newIndex;

  async function onCardDropped(evt) {
    _dragging = true;
    setTimeout(() => { _dragging = false; flushDeferredRefresh(); }, 250);
    if (droppedInPlace(evt)) return;
    const item = evt.item, slug = item.dataset.slug, toCol = evt.to.dataset.columnId;
    const sibs = Array.from(evt.to.children), idx = sibs.indexOf(item);
    const prev = idx > 0 ? parseFloat(sibs[idx - 1].dataset.position) : null;
    const next = idx < sibs.length - 1 ? parseFloat(sibs[idx + 1].dataset.position) : null;
    const pos = midpoint(prev, next, 1);
    try {
      const res = await apiSend('PATCH', `/cards/${encodeURIComponent(slug)}/move`, { column: toCol, position: pos });
      const card = CARDS.find(c => c.slug === slug);
      if (card && res.card) { card.column_id = res.card.column_id; card.position = res.card.position; card.stage = res.card.stage; }
      item.dataset.position = res.card ? res.card.position : pos;
      refreshCounts();
      // Re-sign the board after the optimistic update, so the stream event our
      // OWN move provokes compares equal and leaves the drop where the user put
      // it. Without this every drag is followed by a full re-render of a board
      // that already shows the right thing.
      _sig = _sigOf(COLUMNS, CARDS);
    } catch (e) { alertMsg('error', 'Move failed: ' + e.message); load(); }
  }

  async function onColumnDropped(evt) {
    if (droppedInPlace(evt)) return;
    const cols = Array.from(document.querySelectorAll('#boards-root .b-column'));
    const id = evt.item.dataset.columnId, idx = cols.indexOf(evt.item);
    const posOf = el => { const c = COLUMNS.find(x => x.id === el.dataset.columnId); return c ? c.position : null; };
    const prev = idx > 0 ? posOf(cols[idx - 1]) : null;
    const next = idx < cols.length - 1 ? posOf(cols[idx + 1]) : null;
    const pos = midpoint(prev, next, 1);
    try {
      await apiSend('PATCH', `/columns/${id}`, { position: pos });
      const c = COLUMNS.find(x => x.id === id); if (c) c.position = pos;
      // Same reason as onCardDropped, with one extra step: render() lays columns
      // out in array order while the API returns them in position order, so the
      // array has to be re-sorted before re-signing or the signature would still
      // differ from the server's and re-render a board that is already right.
      COLUMNS.sort((a, b) => a.position - b.position);
      _sig = _sigOf(COLUMNS, CARDS);
    } catch (e) { alertMsg('error', 'Reorder failed: ' + e.message); load(); }
  }

  function refreshCounts() {
    document.querySelectorAll('#boards-root .b-column').forEach(colEl => {
      const badge = colEl.querySelector('.b-column-count');
      if (badge) badge.textContent = colEl.querySelectorAll('.b-column-cards .b-card').length;
    });
  }

  // ── Search / filter ─────────────────────────────────────────────────────────
  function onSearch() {
    clearTimeout(_searchTimer);
    _searchTimer = setTimeout(() => { _search = $('b-search').value.trim(); load(); }, 250);
  }
  function clearSearch() { _search = ''; const s = $('b-search'); if (s) s.value = ''; load(); }
  function filterLabel(l) { _labelFilter = l; load(); }
  function filterRepo(r) { _repoFilter = r; load(); }
  function filterPerson(p) { _personFilter = p; load(); }
  function toggleClosed() {
    _showClosed = !_showClosed;
    try { localStorage.setItem('elefant_board_show_closed', _showClosed ? '1' : '0'); }
    catch (e) { /* the column still toggles; it just won't survive a reload */ }
    syncClosedToggle();
    load();   // the closed cards are a different query, so this is a refetch
  }

  // ── Cards ─────────────────────────────────────────────────────────────────
  // There is no length to count. A card is a tracked issue now, not a sticky
  // note, so the description field stopped arguing with what was typed into it
  // — no running count, no tip, no badge on the tile.

  // Write/Preview rather than a live split: a description is mostly read or
  // mostly edited, rarely both at once, and a permanent preview would spend
  // half the widest part of the modal showing the same words twice.
  //
  // The preview is rendered on DEMAND, not kept in step as you type — so what
  // is on screen is always the text as it currently stands, and there is no
  // second copy to fall out of date.
  //
  // Both card modals have one: the new-card form had neither the
  // tabs nor a way to see a pasted screenshot, so the first description a card
  // ever gets was the one written blind. `modal` picks which form's fields.
  const DESC_MODALS = {
    edit: { ta: 'b-ec-body', box: 'b-ec-body-preview', modal: 'b-edit-card-modal' },
    new:  { ta: 'b-cm-body', box: 'b-cm-body-preview', modal: 'b-card-modal' },
  };
  function showDescription(which, modal) {
    const preview = which === 'preview';
    const ids = DESC_MODALS[modal] || DESC_MODALS.edit;
    const box = $(ids.box), ta = $(ids.ta);
    if (!box || !ta) return;
    if (preview) {
      const html = renderMarkdown(ta.value);
      box.innerHTML = html || '<span class="b-hint">Nothing to preview yet.</span>';
    }
    ta.hidden = preview;
    box.hidden = !preview;
    document.querySelectorAll(`#${ids.modal} .b-md-tabs button`).forEach((b, i) => {
      const on = (i === 1) === preview;
      b.classList.toggle('sel', on);
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
  }

  // ── People fields ───────────────────────────────────────────────────────────
  // These were <select multiple size="4">. That works with three accounts and
  // fails with thirty, and it fails badly rather than gradually: a scrolling
  // box you ctrl-click and hope, where one misclick silently replaces the
  // whole selection. So all three people fields are the pattern everybody
  // already knows — type, pick from the matches, get a chip, remove it with
  // its ×; and all three are the SAME control, so naming a person is one
  // gesture rather than three.
  //
  // Built on the card type-ahead rather than beside it. Different data source,
  // identical mechanics.
  const peopleFieldHTML = (id, placeholder) =>
    `<div class="b-people" id="${id}-field">` +
    `<span class="b-people-chips" id="${id}-chips"></span>` +
    `<input id="${id}" class="b-people-input" autocomplete="off" placeholder="${placeholder}"` +
    ` role="combobox" aria-expanded="false" aria-autocomplete="list" aria-controls="${id}-results">` +
    `<div class="b-ta-results" id="${id}-results" role="listbox" hidden></div></div>`;

  // What each field currently holds, as REFERENCES. Holding refs rather than
  // reading them back off DOM options is what makes the "somebody who has
  // left" case work by construction: a card keeps naming them, the chip
  // renders from the reference, and an untouched save writes back what it
  // read — no option list to fall out of.
  const _picked = {};
  const _peopleTA = {};

  const getPeople = id => (_picked[id] || []).slice();

  function setPeople(id, refs) {
    _picked[id] = (refs || []).filter(Boolean);
    renderPeopleChips(id);
  }

  function renderPeopleChips(id) {
    const box = $(id + '-chips'); if (!box) return;
    box.innerHTML = (_picked[id] || []).map(ref => {
      const rec = personRec(ref);
      const agent = rec && rec.kind === 'agent';
      // A reference the directory no longer offers still renders, and says so
      // in its tooltip. Dropping it would quietly rewrite who a card names.
      const gone = rec && rec.known === false;
      return `<span class="b-person-chip${agent ? ' agent' : ''}${gone ? ' gone' : ''}"` +
        ` title="${esc(personTitle(ref))}">${esc(personName(ref))}` +
        `<span class="x" data-unpick="${esc(ref)}" title="Remove">×</span></span>`;
    }).join('');
    box.querySelectorAll('[data-unpick]').forEach(el => el.addEventListener('mousedown', e => {
      e.preventDefault(); e.stopPropagation();
      removePerson(id, el.dataset.unpick);
    }));
  }

  function addPerson(id, ref, multi) {
    const held = _picked[id] || [];
    // A single-value field REPLACES: a reporter or an assignee is one account,
    // and the field should not be able to say otherwise even for a moment.
    _picked[id] = multi ? (held.includes(ref) ? held : held.concat([ref])) : [ref];
    renderPeopleChips(id);
    const el = $(id); if (el) { el.value = ''; el.focus(); }
    // Discrete, so there is no burst to wait out — a debounce here just looks
    // like the click did nothing.
    if (id.startsWith('b-ec-')) scheduleCardSave(0);
  }

  function removePerson(id, ref) {
    _picked[id] = (_picked[id] || []).filter(r => r !== ref);
    renderPeopleChips(id);
    if (id.startsWith('b-ec-')) scheduleCardSave(0);
  }

  // Case-insensitive over display name and handle. Exact first, then prefix,
  // then substring — typing "jo" should offer Jon before Jonathan-Smithson, and
  // typing a full name should not bury it under everyone who contains it.
  function matchPeople(q) {
    const needle = q.replace(/^@/, '').trim().toLowerCase();
    if (!needle) return [];
    const rank = (p) => {
      const d = (p.display || '').toLowerCase(), h = (p.handle || '').toLowerCase();
      if (d === needle || h === needle) return 0;
      if (d.startsWith(needle) || h.startsWith(needle)) return 1;
      if (d.includes(needle) || h.includes(needle)) return 2;
      return 99;
    };
    return DIRECTORY
      .map(p => ({ p, r: rank(p) })).filter(x => x.r < 99)
      .sort((a, b) => a.r - b.r || (a.p.display || '').localeCompare(b.p.display || ''))
      .map(x => x.p).slice(0, 8);
  }

  function peopleField(id, multi) {
    if (_peopleTA[id]) return _peopleTA[id];
    const ta = makeTypeahead({
      input: id, box: id + '-results',
      // NEVER INVENT. No free-text fallback: the whole point of resolved people
      // is that a card cannot name somebody who is not there, so a query
      // matching nobody offers nothing and Enter adds nothing.
      empty: 'No accounts match.',
      debounce: 0,                       // the directory is already in memory
      source: async (q) => matchPeople(q),
      disabled: (p) => (_picked[id] || []).includes(p.ref),
      row: (p, already) =>
        `<span class="b-ta-title">${esc(p.display || p.ref)}</span>` +
        (p.handle ? `<span class="b-ta-num">@${esc(p.handle)}</span>` : '') +
        (p.kind === 'agent' ? '<span class="b-ta-tag">agent</span>' : '') +
        (already ? '<span class="b-ta-tag">on card</span>' : ''),
      pick: (p) => addPerson(id, p.ref, multi),
      onKey: (e) => {
        // Backspace on an empty input takes the last chip off, which is the
        // one thing people try without being told.
        if (e.key === 'Backspace' && !$(id).value && (_picked[id] || []).length) {
          e.preventDefault();
          removePerson(id, _picked[id][_picked[id].length - 1]);
        }
      },
    });
    ta.attach();
    _peopleTA[id] = ta;
    return ta;
  }

  // ── Label pills ─────────────────────────────────────────────────────────────
  // Same control as the people fields, different data source — type, pick or
  // invent, get a pill, remove it with its ×. Labels differ from people in the
  // one way that matters: people are RESOLVED and a card must never name an
  // account that is not there, so that field never invents. A label is the
  // user's own word for something, so this one does: Enter or a comma commits
  // whatever was typed.
  //
  // Autocomplete is over labels IN USE on some card, which is what makes this
  // a vocabulary rather than a registry. There is nothing to create and
  // nothing to prune — a label is offered because a card carries it, and a
  // typo stops being offered the moment it is taken off the one card that had
  // it. The board's own labels are the list.
  const labelFieldHTML = (id, placeholder) =>
    `<div class="b-people" id="${id}-field">` +
    `<span class="b-people-chips" id="${id}-chips"></span>` +
    `<input id="${id}" class="b-people-input" autocomplete="off" placeholder="${placeholder}"` +
    ` role="combobox" aria-expanded="false" aria-autocomplete="list" aria-controls="${id}-results">` +
    `<div class="b-ta-results" id="${id}-results" role="listbox" hidden></div></div>`;

  const _labels = {};        // id -> the pills the field currently holds
  const _labelDropped = {};  // id -> labels taken off BY HAND since it was filled
  const _labelTA = {};
  // Labels the board sets itself, served by the API so this file does not hold
  // a second copy to go stale. Empty until the first fetch, which only means
  // no pill is marked reserved yet — never that a removal is waved through.
  let RESERVED_LABELS = new Set();
  let _labelsInUse = [];

  const getLabels = id => (_labels[id] || []).slice();

  function setLabels(id, list) {
    _labels[id] = (list || []).filter(Boolean);
    // Filling the field is not editing it: a card opened and closed again must
    // not look like somebody removed anything.
    _labelDropped[id] = [];
    renderLabelChips(id);
  }

  // Did a person take a RESERVED label off this field by hand? That is the one
  // thing that waives the server's re-attach, and it has to be asked for.
  const droppedReserved = id =>
    (_labelDropped[id] || []).some(l => RESERVED_LABELS.has(l.toLowerCase()));

  function renderLabelChips(id) {
    const box = $(id + '-chips'); if (!box) return;
    box.innerHTML = (_labels[id] || []).map(l => {
      const held = RESERVED_LABELS.has(l.toLowerCase());
      // A reserved pill says what it is on hover. It is removable like any
      // other — the protection is against a replace that never mentions it,
      // not against a person who meant it.
      const title = held
        ? `${l} — set by the board when this card was captured`
        : l;
      return `<span class="b-label-chip${held ? ' reserved' : ''}" title="${esc(title)}">` +
        `${esc(l)}<span class="x" data-unlabel="${esc(l)}" title="Remove">×</span></span>`;
    }).join('');
    box.querySelectorAll('[data-unlabel]').forEach(el => el.addEventListener('mousedown', e => {
      e.preventDefault(); e.stopPropagation();
      removeLabel(id, el.dataset.unlabel);
    }));
  }

  function addLabel(id, name) {
    const v = (name || '').trim();
    if (!v) return;
    const held = _labels[id] || [];
    // Case-insensitive, first spelling wins — the same hygiene the API applies,
    // so the field never shows two pills the server will collapse into one.
    if (!held.some(l => l.toLowerCase() === v.toLowerCase())) _labels[id] = held.concat([v]);
    renderLabelChips(id);
    const el = $(id); if (el) { el.value = ''; el.focus(); }
    if (id.startsWith('b-ec-')) scheduleCardSave(0);
  }

  function removeLabel(id, name) {
    _labels[id] = (_labels[id] || []).filter(l => l.toLowerCase() !== name.toLowerCase());
    (_labelDropped[id] = _labelDropped[id] || []).push(name);
    renderLabelChips(id);
    if (id.startsWith('b-ec-')) scheduleCardSave(0);
  }

  // Labels already on THIS card are offered as taken rather than hidden: a
  // label you cannot find because you already used it reads like the
  // autocomplete is broken.
  function matchLabels(q) {
    const needle = (q || '').trim().toLowerCase();
    if (!needle) return [];
    const rank = (l) => {
      const v = l.toLowerCase();
      if (v === needle) return 0;
      if (v.startsWith(needle)) return 1;
      if (v.includes(needle)) return 2;
      return 99;
    };
    return _labelsInUse
      .map(l => ({ l, r: rank(l) })).filter(x => x.r < 99)
      .sort((a, b) => a.r - b.r || a.l.localeCompare(b.l))
      .map(x => x.l).slice(0, 8);
  }

  async function refreshLabelsInUse() {
    try {
      const data = await apiGet('/labels');
      _labelsInUse = data.labels || [];
      RESERVED_LABELS = new Set((data.reserved || []).map(l => l.toLowerCase()));
      // Pills already on screen may need their reserved marking now.
      Object.keys(_labels).forEach(renderLabelChips);
    } catch (e) {
      // A field that cannot suggest still accepts anything typed, which is the
      // part people cannot work around. Failing quietly beats blocking input.
    }
  }

  function labelField(id) {
    if (_labelTA[id]) return _labelTA[id];
    const ta = makeTypeahead({
      input: id, box: id + '-results',
      empty: 'No labels in use match — Enter adds it as a new one.',
      debounce: 0,                       // the list is already in memory
      source: async (q) => matchLabels(q),
      disabled: (l) => (_labels[id] || []).some(x => x.toLowerCase() === l.toLowerCase()),
      row: (l, already) =>
        `<span class="b-ta-title">${esc(l)}</span>` +
        (RESERVED_LABELS.has(l.toLowerCase()) ? '<span class="b-ta-tag">board</span>' : '') +
        (already ? '<span class="b-ta-tag">on card</span>' : ''),
      pick: (l) => addLabel(id, l),
      // Nothing highlighted in the list means Enter commits what was typed.
      onEnter: () => addLabel(id, $(id)?.value),
      onKey: (e) => {
        if (e.key === 'Backspace' && !$(id).value && (_labels[id] || []).length) {
          e.preventDefault();
          removeLabel(id, _labels[id][_labels[id].length - 1]);
        }
      },
    });
    ta.attach();
    // A comma commits, which is what anyone who used the old comma-separated
    // field will try first. Handled on `input` rather than keydown so a comma
    // that arrives by paste or from a phone keyboard counts too — and a pasted
    // `a, b, c` lands as three pills instead of one.
    $(id)?.addEventListener('input', () => {
      const el = $(id);
      if (!el || el.value.indexOf(',') === -1) return;
      const parts = el.value.split(',');
      const rest = parts.pop();          // what follows the last comma is still being typed
      parts.forEach(v => addLabel(id, v));
      // AFTER the commits, not before: addLabel clears the input — which is
      // what picking from the list wants — and would otherwise swallow the
      // half-typed tail of a pasted `bug, infra, urg`.
      el.value = rest;
      el.focus();
    });
    _labelTA[id] = ta;
    return ta;
  }

  // ── Inline mentions in a description ────────────────────────────────────────
  // Typing `@jo` mid-sentence offers the same directory the people fields do.
  // What is STORED is markdown's own link shape, `@[Jon Smith](user:9f2c…)`:
  // readable raw (including to an agent writing through MCP), resolvable, and
  // already parsed by marked. The name in the text is a snapshot that may go
  // stale; the reference beside it is what the chip renders from, so staleness
  // is cosmetic and corrects itself on the next render.
  //
  // The alternatives both fail: a bare display name is the free-text problem
  // the people fields exist to remove, and a bare reference makes the raw
  // markdown unreadable to whoever is editing it.
  const MENTION_TOKEN = /@([\p{L}\p{N} ._-]{0,40})$/u;

  // The `@word` under the caret, or null when the caret is not in one.
  function mentionQuery(id) {
    const el = $(id); if (!el) return null;
    const upto = (el.value || '').slice(0, el.selectionStart ?? 0);
    const m = upto.match(MENTION_TOKEN);
    if (!m) return null;
    // An @ straight after a word character is an email or a handle already in
    // prose, not somebody starting a mention.
    const before = upto[upto.length - m[0].length - 1];
    if (before && /[\w@]/.test(before)) return null;
    return m[1];
  }

  function insertMention(id, person) {
    const el = $(id); if (!el) return;
    const caret = el.selectionStart ?? 0;
    const upto = (el.value || '').slice(0, caret);
    const m = upto.match(MENTION_TOKEN);
    if (!m) return;
    const text = `@[${person.display || person.ref}](${person.ref})`;
    el.value = upto.slice(0, upto.length - m[0].length) + text + el.value.slice(caret);
    const pos = caret - m[0].length + text.length;
    el.selectionStart = el.selectionEnd = pos;
    el.focus();
    // Otherwise a mention picked and then left alone is lost on close: the
    // write above fires no `input`, and `input` is what schedules the save.
    fireInput(el);
    offerParticipant(person);
  }

  // MENTIONING SOMEBODY DOES NOT PUT THEM ON THE CARD. Jira does not either —
  // it notifies, and we have no notifications. Silently editing a structured
  // field because of something written in prose is the kind of magic that
  // surprises people later, so intent is made explicit instead: the offer is
  // one line, and doing nothing about it leaves the card alone.
  function offerParticipant(person) {
    const box = $('b-ec-mention-offer'); if (!box) return;
    if (getPeople('b-ec-participants').includes(person.ref)) return box.hidden = true;
    box.innerHTML = `<span>Also add ${esc(person.display || person.ref)} as a participant?</span>` +
      `<button type="button" class="b-btn b-btn-secondary b-btn-sm" data-add="1">Add</button>` +
      `<button type="button" class="b-btn b-btn-secondary b-btn-sm" data-dismiss="1">No</button>`;
    box.hidden = false;
    box.querySelector('[data-add]').addEventListener('click', () => {
      addPerson('b-ec-participants', person.ref, true);
      box.hidden = true;
    });
    box.querySelector('[data-dismiss]').addEventListener('click', () => { box.hidden = true; });
  }

  const bodyMentions = makeTypeahead({
    input: 'b-ec-body', box: 'b-ec-body-results',
    empty: 'No accounts match.',
    debounce: 0,
    allowEmptyQuery: true,            // a bare `@` should already offer everyone
    query: () => mentionQuery('b-ec-body'),
    source: async (q) => (q ? matchPeople(q) : DIRECTORY.slice(0, 8)),
    row: (p) =>
      `<span class="b-ta-title">${esc(p.display || p.ref)}</span>` +
      (p.handle ? `<span class="b-ta-num">@${esc(p.handle)}</span>` : '') +
      (p.kind === 'agent' ? '<span class="b-ta-tag">agent</span>' : ''),
    pick: (p) => insertMention('b-ec-body', p),
  });

  // ── Comments ────────────────────────────────────────────────────────────────
  // A card records what the work IS; the thread is the conversation about it.
  // Rendered through renderMarkdown like the description, which is how comments
  // get markdown, the sanitiser and @-mentions without a second implementation.
  let _commentsNow = [];
  let _editingComment = null;

  // ── Comment drafts ──────────────────────────────────────────────────────────
  // A comment typed but never sent used to die with the modal — close the card,
  // follow a link, reload, and the text was simply gone. That is the one thing
  // a composer must not do, because the work it loses is the work somebody had
  // just finished thinking about.
  //
  // Kept per card in localStorage: the draft belongs to the operator and their
  // browser, it is worthless to anyone else, and a server-side draft would mean
  // a write on every keystroke for something that is not yet a comment.
  //
  // Every access is wrapped: localStorage throws outright in a Safari private
  // window and where site data is blocked, and a composer that throws on
  // keypress is far worse than one that forgets.
  const DRAFT_KEY = slug => `elefant:board:draft:comment:${slug}`;

  function readDraft(slug) {
    if (!slug) return '';
    try { return window.localStorage.getItem(DRAFT_KEY(slug)) || ''; }
    catch (e) { return ''; }
  }

  function writeDraft(slug, text) {
    if (!slug) return;
    try {
      // An emptied composer clears the key rather than storing '' — otherwise
      // every card ever opened leaves a row behind forever.
      if (text) window.localStorage.setItem(DRAFT_KEY(slug), text);
      else window.localStorage.removeItem(DRAFT_KEY(slug));
    } catch (e) { /* no storage: the draft simply doesn't survive the modal */ }
  }

  // Called on every keystroke in the composer. An EDIT of an existing comment is
  // deliberately not persisted: restoring one into a composer that is no longer
  // in edit mode would post somebody's edit as a brand-new comment, which is a
  // worse failure than losing an in-flight edit.
  function saveCommentDraft() {
    if (_editingComment) return;
    const ta = $('b-ec-comment-body'); if (!ta) return;
    writeDraft(_editSlug, ta.value || '');
  }

  const ME = () => ME_REF;

  function renderComments(list) {
    const box = $('b-ec-comments'); if (!box) return;
    _commentsNow = list || [];
    if (!_commentsNow.length) {
      box.innerHTML = '<span class="b-hint">No comments yet.</span>';
      return;
    }
    box.innerHTML = _commentsNow.map(c => {
      const who = esc(personName(c.author || ''));
      const when = esc((c.created_at || '').slice(0, 16).replace('T', ' '));
      if (c.deleted) {
        // A withdrawn comment keeps its place. A thread that silently loses a
        // message reads as one that never had it.
        return `<div class="b-comment deleted"><div class="b-comment-head">` +
          `<span class="b-comment-who">${who}</span>` +
          `<span class="b-comment-when">${when}</span></div>` +
          `<div class="b-comment-body"><em>withdrawn</em></div></div>`;
      }
      const mine = c.author && c.author === ME();
      const edited = c.edited ? '<span class="b-comment-when">edited</span>' : '';
      return `<div class="b-comment" data-id="${esc(c.id)}"><div class="b-comment-head">` +
        `<span class="b-comment-who">${who}</span>` +
        `<span class="b-comment-when">${when}</span>${edited}` +
        // Only the author is offered the controls, because only the author can
        // use them — offering them to everybody would be a button that 403s.
        (mine ? `<span class="b-comment-acts">` +
          `<button type="button" data-edit="${esc(c.id)}">Edit</button>` +
          `<button type="button" data-del="${esc(c.id)}">Delete</button></span>` : '') +
        `</div><div class="b-comment-body">${renderMarkdown(c.body || '')}</div></div>`;
    }).join('');
    box.querySelectorAll('[data-edit]').forEach(b =>
      b.addEventListener('click', () => startEditComment(b.dataset.edit)));
    box.querySelectorAll('[data-del]').forEach(b =>
      b.addEventListener('click', () => deleteComment(b.dataset.del)));
  }

  async function reloadComments() {
    if (!_editSlug) return;
    try {
      const res = await apiGet(`/cards/${encodeURIComponent(_editSlug)}/comments`);
      if (res.people) PEOPLE = Object.assign({}, PEOPLE, res.people);
      renderComments(res.comments || []);
    } catch (e) { /* the thread is best-effort; the card still opens */ }
  }

  async function addComment() {
    const ta = $('b-ec-comment-body'); if (!ta || !_editSlug) return;
    const body = (ta.value || '').trim();
    if (!body) return;
    const btn = $('b-ec-comment-add'); btn.disabled = true;
    try {
      if (_editingComment) {
        await apiSend('PATCH',
          `/cards/${encodeURIComponent(_editSlug)}/comments/${encodeURIComponent(_editingComment)}`,
          { body });
        _editingComment = null;
        btn.textContent = 'Comment';
      } else {
        await apiSend('POST', `/cards/${encodeURIComponent(_editSlug)}/comments`, { body });
        // Posted — the draft has become a comment, so the stored copy goes.
        // Only on this branch: an edit never had a stored draft to clear, and
        // clearing here would throw away a new-comment draft the operator
        // parked to go and fix an earlier comment first.
        writeDraft(_editSlug, '');
      }
      ta.value = '';
      await reloadComments();
      refresh();                        // the count on the sticky behind
    } catch (e) { alertMsg('error', e.message); } finally { btn.disabled = false; }
  }

  function startEditComment(id) {
    const c = _commentsNow.find(x => x.id === id); if (!c) return;
    _editingComment = id;
    const ta = $('b-ec-comment-body');
    ta.value = c.body || '';
    ta.focus();
    $('b-ec-comment-add').textContent = 'Save edit';
  }

  async function deleteComment(id) {
    if (!confirm('Withdraw this comment? It stays in the thread marked as withdrawn.')) return;
    try {
      await apiSend('DELETE',
        `/cards/${encodeURIComponent(_editSlug)}/comments/${encodeURIComponent(id)}`);
      await reloadComments();
      refresh();
    } catch (e) { alertMsg('error', e.message); }
  }

  // ── Attachments ─────────────────────────────────────────────────────────────
  // The bug report that produced this feature was a screenshot pasted into a
  // chat window, because there was nowhere on the card to put it. So pasting
  // into the description is the case this exists for, and attaching a file is
  // the same plumbing with a different button.
  //
  // Uploads reuse the editor's existing asset routes rather than growing a
  // second upload path with its own auth and its own bucket layout — which is
  // how one of the two ends up wrong. Cards get their own key prefix so a
  // card's objects are identifiable as such.
  let _attachNow = [];

  // The original filename, recovered from the key the server built. Mirrors
  // _attachment_display_name in the editor; the key is the only place the name
  // is kept, so the two cannot drift apart into disagreeing.
  function attachName(key) {
    const base = String(key).split('/').pop();
    const parts = base.split('-');
    return parts.length >= 3 ? parts.slice(2).join('-') : base;
  }

  const attachPrefix = () => 'cards/' + (_editSlug || '');
  // A card being drafted in the new-card form has no slug yet — the server
  // derives it from the title on create — so its screenshots are filed under a
  // shared drafts prefix instead. The reference written into the description
  // is what ties the image to the card, and it resolves the same either way.
  const NEW_CARD_PREFIX = 'cards/new';

  async function uploadAsset(route, file, prefix) {
    const form = new FormData();
    form.append('file', file, file.name || 'pasted');
    form.append('prefix', prefix);
    const res = await fetch(route, { method: 'POST', body: form });
    const data = await res.json().catch(() => ({}));
    // The server's message names the limit and the reason. Passing it through
    // rather than replacing it with "upload failed" is the difference between
    // knowing to scale a screenshot down and guessing.
    if (!res.ok) throw new Error(data.detail || ('Upload failed (HTTP ' + res.status + ')'));
    return data;
  }

  function renderAttachments(list) {
    const box = $('b-ec-attach'); if (!box) return;
    _attachNow = list || [];
    if (!_attachNow.length) {
      box.innerHTML = '<span class="b-hint">Nothing attached yet.</span>';
      return;
    }
    box.innerHTML = `<div class="b-rel-chips">` + _attachNow.map(k =>
      `<span class="b-filter-chip b-attach-chip">` +
      // Downloads go through the authenticated route, so the link is a link
      // and not a credential.
      `<a href="/api/attachment?path=${encodeURIComponent(k)}" download>${esc(attachName(k))}</a>` +
      `<span class="x" data-detach="${esc(k)}" title="Remove this attachment">×</span></span>`
    ).join('') + `</div>`;
    box.querySelectorAll('[data-detach]').forEach(el => el.addEventListener('click', e => {
      e.stopPropagation(); detachFile(el.dataset.detach);
    }));
  }

  async function saveAttachments(list) {
    if (!_editSlug) return;
    try {
      const res = await apiSend('PATCH', `/cards/${encodeURIComponent(_editSlug)}`,
        { attachments: list });
      // Re-read from the server rather than trusting the local list: it
      // validates the keys, and a key it refused must not linger on screen
      // looking attached.
      renderAttachments((res.card && res.card.attachments) || []);
    } catch (e) { alertMsg('error', e.message); }
  }

  async function attachFile(file) {
    if (!file) return;
    if (!_editSlug) return alertMsg('error', 'Open a card before attaching to it.');
    try {
      alertMsg('info', `Uploading ${file.name || 'file'}…`, 2000);
      const up = await uploadAsset('/api/upload-file', file, attachPrefix());
      await saveAttachments(_attachNow.concat([up.path]));
      alertMsg('success', `Attached ${up.name}.`);
    } catch (e) { alertMsg('error', e.message); }
  }

  function detachFile(key) {
    // The object itself is left in the bucket. Detaching is reversible in the
    // way deleting is not, and collecting orphans is its own job.
    saveAttachments(_attachNow.filter(k => k !== key));
  }

  // PASTE A SCREENSHOT. The one that matters.
  //
  // Bound to BOTH composers, because "there was nowhere on the card to put it"
  // is as true of a comment as of the description — a screenshot is usually the
  // answer to something already in the thread. The target comes from the event
  // rather than being named here, so the two cannot drift apart into one that
  // works and one that quietly does not.
  async function onDescriptionPaste(e) {
    const items = [...((e.clipboardData && e.clipboardData.items) || [])];
    const shot = items.find(i => i.kind === 'file' && /^image\//.test(i.type || ''));
    if (!shot) return;                       // ordinary text paste, leave it alone
    e.preventDefault();
    const ta = e.currentTarget || e.target;
    if (!ta) return;
    // The new-card form is the one composer that belongs to no card yet
    //. Every other one needs the open card to file under.
    const drafting = ta.id === 'b-cm-body';
    if (!drafting && !_editSlug) return alertMsg('error', 'Open a card before pasting into it.');
    const file = shot.getAsFile();
    if (!file) return;
    const at = ta.selectionStart ?? ta.value.length;
    try {
      alertMsg('info', 'Uploading screenshot…', 2000);
      const up = await uploadAsset('/api/upload-image', file,
        drafting ? NEW_CARD_PREFIX : attachPrefix());
      // NOTHING IS WRITTEN UNTIL THE UPLOAD SUCCEEDS. A reference inserted
      // optimistically and then left behind by a failed upload is a
      // permanently broken image in the description, and the person who pasted
      // it has no way to tell that from a slow one.
      const md = `![${attachName(up.path)}](/api/image?path=${encodeURIComponent(up.path)})`;
      ta.value = ta.value.slice(0, at) + md + ta.value.slice(at);
      ta.selectionStart = ta.selectionEnd = at + md.length;
      // The write above is invisible to the `input` listeners, so without this
      // the description is never auto-saved and a comment draft is never kept.
      fireInput(ta);
      // No longer "Save the card to keep it": that button went with #111, and
      // the description now saves itself. A comment still has to be posted, so
      // the two say different things because they ARE different.
      alertMsg('success', ta.id === 'b-ec-comment-body'
        ? 'Screenshot uploaded — add it with Comment.'
        : drafting ? 'Screenshot uploaded — Add card to keep it.'
        : 'Screenshot uploaded.');
    } catch (err) {
      alertMsg('error', err.message);
    }
  }

  function wireAttachments() {
    $('b-ec-body')?.addEventListener('paste', onDescriptionPaste);
    $('b-ec-comment-body')?.addEventListener('paste', onDescriptionPaste);
    $('b-cm-body')?.addEventListener('paste', onDescriptionPaste);
    $('b-ec-attach-btn')?.addEventListener('click', () => $('b-ec-attach-file')?.click());
    $('b-ec-attach-file')?.addEventListener('change', (e) => {
      const f = e.target.files && e.target.files[0];
      e.target.value = '';               // so the same file can be attached twice
      attachFile(f);
    });
  }

  function fillPeoplePickers(reporterId, participantsId, card, assigneeId) {
    peopleField(reporterId, false);
    peopleField(participantsId, true);
    if (assigneeId) peopleField(assigneeId, false);
    setPeople(reporterId, [(card && card.reporter) || ''].filter(Boolean));
    if (assigneeId) setPeople(assigneeId, [(card && card.assignee) || ''].filter(Boolean));
    setPeople(participantsId, (card && card.participants) || []);
  }

  const selectedPeople = id => getPeople(id);

  function openCardModal(colId) {
    const sel = $('b-cm-column');
    sel.innerHTML = COLUMNS.map(c => `<option value="${c.id}">${esc(c.name)} · ${esc(c.stage)}</option>`).join('');
    if (colId) sel.value = colId;
    $('b-cm-title').value = ''; $('b-cm-body').value = '';
    setLabels('b-cm-labels', []); $('b-cm-repo').value = '';
    if ($('b-cm-due')) $('b-cm-due').value = '';
    fillPeoplePickers('b-cm-reporter', 'b-cm-participants', null, 'b-cm-assignee');
    prefillReporter();
    labelField('b-cm-labels');
    // Open on Write, for the same reason the open card does: left on Preview,
    // the form would show the last draft's rendered text over an empty box.
    showDescription('write', 'new');
    openModal('b-card-modal');
    setTimeout(() => $('b-cm-title').focus(), 50);
  }

  // The form says the reporter defaults to you, so it shows you.
  // Leaving the field empty and letting the server fill it in on save was the
  // same card, but it meant the form disagreed with itself until Add card.
  // `me` is the server's answer, the same reference the create call defaults
  // to, so a card saved untouched is the card it always was. On a cold board
  // the directory may still be loading, so fill once it lands, unless the
  // person has already picked someone or closed the form.
  function prefillReporter() {
    const fill = () => {
      if (ME() && !getPeople('b-cm-reporter').length) setPeople('b-cm-reporter', [ME()]);
    };
    if (ME()) return fill();
    loadDirectory().then(() => {
      if ($('b-card-modal')?.classList.contains('open')) fill();
    });
  }

  async function submitCard() {
    const title = $('b-cm-title').value.trim();
    if (!title) { alertMsg('error', 'A title is required.'); return; }
    const btn = $('b-cm-submit'); btn.disabled = true;
    try {
      const res = await apiSend('POST', '/cards', {
        title, body: $('b-cm-body').value,
        labels: getLabels('b-cm-labels'),
        repo: $('b-cm-repo').value.trim(), column: $('b-cm-column').value,
        due: ($('b-cm-due')?.value || '').trim(),
        reporter: getPeople('b-cm-reporter')[0] || '', assignee: getPeople('b-cm-assignee')[0] || '',
        participants: selectedPeople('b-cm-participants'),
      });
      (res.warnings || []).forEach(w => alertMsg('info', w, 5000));
      closeModal('b-card-modal'); await load();
    } catch (e) { alertMsg('error', e.message); } finally { btn.disabled = false; }
  }

  function fillCardModal(c) {
    // Programmatic writes are not edits. Assigning .value fires no input event,
    // but the people pickers render through the same helpers a human click
    // uses, so the guard is what keeps opening a card from saving it.
    _filling = true;
    try { fillCardFields(c); } finally { _filling = false; }
    setSaveStatus('', '');
  }

  function fillCardFields(c) {
    fillColumnPicker(c);
    $('b-ec-slug').textContent = c.slug || '';
    // The number leads the open card for the same reason it sits on the tile:
    // it is the handle you quote ("look at #33") and the thing a permalink is
    // built from, and the open card is where you are actually reading one.
    // Hidden rather than blank when the card predates the number backfill, so
    // the header doesn't reserve space for a chip that will never render.
    //
    // The slug rides in a data attribute rather than the onclick argument —
    // an HTML attribute is entity-decoded before the JS inside it is parsed,
    // so escaping alone would not contain a hostile value.
    const num = $('b-ec-number');
    if (num) {
      num.hidden = c.number == null;
      num.textContent = c.number == null ? '' : '#' + c.number;
      num.dataset.permalink = c.slug || '';
    }
    $('b-ec-title').value = c.title || '';
    $('b-ec-body').value = c.body || '';
    setLabels('b-ec-labels', c.labels || []);
    $('b-ec-repo').value = c.repo || '';
    updateRepoLink();
    const due = $('b-ec-due');
    if (due) { due.value = c.due || ''; due.dataset.stage = c.stage || ''; due.dataset.closed = (c.closed ?? c.archived) ? '1' : ''; }
    updateDueState();
    fillPeoplePickers('b-ec-reporter', 'b-ec-participants', c, 'b-ec-assignee');
    labelField('b-ec-labels');
    // Always open on Write. Left on Preview, the next card would open showing
    // the PREVIOUS card's rendered text until something re-rendered it. The
    // participant offer is cleared for the same reason — it belongs to a
    // mention somebody typed on a different card.
    showDescription('write');
    const offer = $('b-ec-mention-offer'); if (offer) { offer.hidden = true; offer.innerHTML = ''; }
    // The composer belongs to the card it was opened on — an unsent draft or a
    // half-finished edit must not follow you to the next one.
    _editingComment = null;
    // An unsent draft, on the other hand, IS this card's and must come back —
    // that is the whole point. Restored after the clear, so a card with no
    // draft still opens with an empty box rather than the last card's text.
    const cta = $('b-ec-comment-body');
    if (cta) cta.value = readDraft(c.slug || '');
    const cbtn = $('b-ec-comment-add'); if (cbtn) cbtn.textContent = 'Comment';
    renderCardTimes(c);
  }

  // ── How old is this card? ───────────────────────────────────────────────────
  // The details view said everything about a card except when any of it
  // happened. Bottom left, under the destructive action, because it is
  // reference rather than something to act on.
  //
  // Absolute AND relative, not one of them: relative alone ("8 months ago")
  // cannot date a card, and absolute alone makes you do the arithmetic on the
  // question actually being asked, which is "has this been sitting here?".
  function fmtStamp(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (isNaN(d)) return '';   // never paint "Invalid Date" into the card
    const secs = Math.round((Date.now() - d.getTime()) / 1000);
    const rel =
      secs < 0        ? ''                                   // clock skew — say nothing
      : secs < 60     ? 'just now'
      : secs < 3600   ? `${Math.floor(secs / 60)}m ago`
      : secs < 86400  ? `${Math.floor(secs / 3600)}h ago`
      : secs < 2592000 ? `${Math.floor(secs / 86400)}d ago`
      : '';
    const abs = d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
    return rel ? `${abs} (${rel})` : abs;
  }

  function renderCardTimes(c) {
    const host = $('b-ec-times'); if (!host) return;
    // `closed_at` only exists on a closed card; `created_at`/`updated_at` are
    // absent on nothing current, but a card served by an older image could
    // still arrive without them — a missing row is left out rather than
    // rendered as an empty one.
    const rows = [
      ['Created', c.created_at],
      ['Updated', c.updated_at],
      ['Closed',  (c.closed ?? c.archived) ? c.closed_at : null],
    ].filter(([, iso]) => fmtStamp(iso));
    host.innerHTML = rows.map(([label, iso]) =>
      `<div class="b-ec-time"><span class="b-ec-time-label">${label}</span>` +
      `<span class="b-ec-time-value" title="${esc(iso)}">${esc(fmtStamp(iso))}</span></div>`
    ).join('');
  }

  // ── Due date ────────────────────────────────────────────────────────────────
  // A due date is a DAY (`YYYY-MM-DD`), so it is compared as one: parsed into
  // the viewer's local calendar rather than as UTC midnight, which would put a
  // card due "today" a day early for everybody west of Greenwich.
  //
  // Three states on the chip, each told by a word as well as a colour, like
  // the blocked and under-evaluation chips: overdue (past, and the card is not
  // done), soon (today or within a week), and plain. Finished work is never
  // late, whatever its date said.
  const DUE_RE = /^(\d{4})-(\d{2})-(\d{2})$/;
  const DONE_STAGES = ['done', 'shipped', 'dropped'];
  const DUE_SOON_DAYS = 7;

  function parseDue(due) {
    const m = DUE_RE.exec(String(due || ''));
    if (!m) return null;
    const d = new Date(+m[1], +m[2] - 1, +m[3]);
    // new Date(2026, 1, 30) quietly becomes 2 March; refuse it instead.
    return d.getMonth() === +m[2] - 1 ? d : null;
  }

  function dueInfo(due, stage, closed, now) {
    const d = parseDue(due);
    if (!d) return null;
    const t = now ? new Date(now) : new Date();
    const today = new Date(t.getFullYear(), t.getMonth(), t.getDate());
    const days = Math.round((d - today) / 86400000);
    const done = !!closed || DONE_STAGES.includes(stage);
    const state = done ? 'done' : days < 0 ? 'overdue' : days <= DUE_SOON_DAYS ? 'soon' : 'normal';
    const sameYear = d.getFullYear() === today.getFullYear();
    const short = d.toLocaleDateString(undefined, sameYear
      ? { day: 'numeric', month: 'short' } : { day: 'numeric', month: 'short', year: 'numeric' });
    const n = Math.abs(days), unit = n === 1 ? 'day' : 'days';
    const rel = done ? 'done'
      : days < 0 ? `overdue by ${n} ${unit}`
      : days === 0 ? 'due today'
      : `due in ${n} ${unit}`;
    return { state, days, short, rel, iso: due };
  }

  function dueChipHTML(c) {
    const info = dueInfo(c.due, c.stage, c.closed ?? c.archived);
    if (!info) return '';
    const word = info.state === 'overdue' ? 'overdue ' : '';
    return `<span class="b-card-due ${info.state}" title="Due ${esc(info.iso)} — ${esc(info.rel)}">` +
      `<span class="b-due-mark" aria-hidden="true">📅</span>${word}${esc(info.short)}</span>`;
  }

  // The words under the picker, so the open card says what the chip says.
  function updateDueState() {
    const el = $('b-ec-due-state'), input = $('b-ec-due');
    if (!el || !input) return;
    const info = dueInfo(input.value, input.dataset.stage, input.dataset.closed === '1');
    el.className = 'b-hint b-due-state' + (info ? ' ' + info.state : '');
    el.textContent = info ? info.rel : '';
    const clr = $('b-ec-due-clear'); if (clr) clr.disabled = !input.value;
  }

  function clearDue() {
    const input = $('b-ec-due');
    if (!input || !input.value) return;
    input.value = '';
    updateDueState();
    scheduleCardSave(0);
  }

  // ── Repo link ───────────────────────────────────────────────────────────────
  // The stored value is canonical `owner/name` — memory-api's _clean_repo
  // rejects anything else on write. This still validates before building a URL,
  // for the same reason every other chip escapes: the UI renders whatever the
  // API hands it and must not lean on the server for its own safety.
  //
  // The pattern is deliberately narrow. Anything with a scheme, a host, a colon
  // or a path beyond one slash gets no link at all rather than a guess — a
  // `javascript:` value that rendered as an anchor would be the whole bug.
  const REPO_RE = /^[A-Za-z0-9._-]+\/[A-Za-z0-9._-]+$/;

  function repoUrl(repo) {
    const s = (repo || '').trim().replace(/\.git$/i, '');
    if (!REPO_RE.test(s)) return '';
    // `.` and `..` are legal under the pattern and would walk the path.
    const parts = s.split('/');
    if (parts.some(seg => seg === '.' || seg === '..')) return '';
    return 'https://github.com/' + parts.map(encodeURIComponent).join('/');
  }

  // Follows the field as it is typed, so the link is right at the moment the
  // repo is — not one save behind it.
  function updateRepoLink() {
    const a = $('b-ec-repo-link'); if (!a) return;
    const raw = ($('b-ec-repo')?.value || '').trim();
    const url = repoUrl(raw);
    a.hidden = !url;
    if (!url) { a.removeAttribute('href'); a.textContent = ''; return; }
    a.href = url;
    // textContent, not innerHTML: the value is the card's and is rendered, not
    // parsed.
    a.textContent = `Open ${raw} on GitHub ↗`;
  }

  // ── Column ──────────────────────────────────────────────────────────────────
  // Where a card IS is board state, not a card field, so it moves through the
  // same /move endpoint a drag uses rather than riding along with auto-save.
  // One way for a card to change column, one event on the stream.
  //
  // A dropdown is the accessible half of the drag: a drag needs a pointer, a
  // visible target column and a board you can see, and none of those are true
  // with the card open in front of you.
  function fillColumnPicker(c) {
    const sel = $('b-ec-column'); if (!sel) return;
    const here = c.column_id || '';
    // Columns are rendered left-to-right, the order they are read on the board.
    const opts = COLUMNS.map(col =>
      `<option value="${esc(col.id)}"${col.id === here ? ' selected' : ''}>${esc(col.name)}</option>`);
    // A card whose column is not in the list (archived column, a board still
    // loading) keeps its place rather than silently reading as the first
    // column — which is a move the operator never made.
    if (here && !COLUMNS.some(col => col.id === here))
      opts.unshift(`<option value="${esc(here)}" selected>(current column)</option>`);
    sel.innerHTML = opts.join('');
    sel.value = here;
  }

  async function moveCardToColumn(colId) {
    if (!_editSlug || !colId) return;
    const slug = _editSlug;
    try {
      // No position: the server appends to the end of the target column, which
      // is where a drag to an unspecified spot would land it too.
      const res = await apiSend('PATCH', `/cards/${encodeURIComponent(slug)}/move`, { column: colId });
      const card = CARDS.find(x => x.slug === slug);
      if (card && res.card) {
        card.column_id = res.card.column_id;
        card.position = res.card.position;
        card.stage = res.card.stage;
      }
      // Re-signed for the same reason a drag re-signs: the stream event our own
      // move provokes must compare equal, or the board re-renders under an open
      // card for a change it already shows.
      _sig = _sigOf(COLUMNS, CARDS);
      refresh();
    } catch (e) {
      alertMsg('error', 'Move failed: ' + e.message);
      // Put the control back where the card actually is, or it keeps claiming a
      // move that never happened.
      const card = CARDS.find(x => x.slug === slug);
      if (card) fillColumnPicker(card);
    }
  }

  async function cardClicked(slug) {
    if (_dragging) return;
    // Before anything is overwritten: a debounce still ticking belongs to the
    // card currently in the fields, and firing it after the refill would write
    // this card's text onto that one.
    flushCardSave();
    _editSlug = slug;
    const cached = CARDS.find(x => x.slug === slug || (x.aliases || []).includes(slug));
    fillCardModal(cached || { slug, labels: [], repo: '', participants: [] });
    $('b-ec-wiki').innerHTML = '<span class="b-hint">Loading wiki thread…</span>';
    // Cleared too, or the previous card's links sit there looking like this
    // card's while the fetch is in flight — and a stale blocker is exactly the
    // thing somebody might act on.
    $('b-ec-rel').innerHTML = '<span class="b-hint">Loading linked cards…</span>';
    openModal('b-edit-card-modal');
    try {
      const res = await apiGet(`/cards/${encodeURIComponent(slug)}`);
      // Merge rather than replace: this card's people may include somebody no
      // other card on the board names, and dropping the board-wide map would
      // un-name everyone else's chips behind the modal.
      if (res.people) PEOPLE = Object.assign({}, PEOPLE, res.people);
      if (res.card) {
        _editSlug = res.card.slug; fillCardModal(res.card); renderWikiThread(res.card);
        renderRelationships(res.relationships);
        renderAttachments(res.card.attachments || []);
        renderComments(res.comments || []);
      } else $('b-ec-wiki').innerHTML = '<span class="b-hint">Card not found.</span>';
    } catch (e) {
      $('b-ec-wiki').innerHTML = '<span class="b-hint">Wiki thread unavailable.</span>';
      const rb = $('b-ec-rel');
      if (rb) rb.innerHTML = '<span class="b-hint">Linked cards unavailable.</span>';
    }
  }

  // Native (in-SPA): set the top-level hash and let memory-ui's hashchange router
  // switch to the Wiki tab and open the page in place.
  function openWiki(path) { window.location.hash = 'wiki/' + encodeURIComponent(path); }

  // The permalink is the deep-link the board already routes on, so pasting one
  // lands on the card with the board's own #boards/<slug> handler — no new
  // resolution path to keep working. It is an ordinary app URL and grants
  // nothing: opening it still goes through Elefant's auth gate, exactly as
  // typing the board address does.
  //
  // Keyed on the slug rather than the number because that route exists today
  // and aliases keep an old slug resolving after a rename. The number is what
  // you QUOTE; the link is what you paste.
  function permalinkFor(slug) {
    return location.origin + '/#boards/' + encodeURIComponent(slug);
  }

  async function copyPermalink(slug) {
    if (!slug) return;
    const url = permalinkFor(slug);
    try {
      // The async clipboard API needs a secure context. Elefant is served over
      // TLS, but a plain-http dev origin would leave it undefined, so fall back
      // rather than throwing away the copy.
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(url);
      } else {
        const ta = document.createElement('textarea');
        ta.value = url;
        ta.setAttribute('readonly', '');
        ta.style.position = 'fixed'; ta.style.top = '-1000px';
        document.body.appendChild(ta);
        ta.select();
        const ok = document.execCommand('copy');
        ta.remove();
        if (!ok) throw new Error('copy rejected');
      }
      alertMsg('success', 'Permalink copied.');
    } catch (e) {
      // Never leave the user with nothing: show the URL so it can be copied by
      // hand. A silent failure here looks identical to a chip that does nothing.
      alertMsg('info', url, 8000);
    }
  }

  function renderWikiThread(c) {
    _editCard = c;
    const box = $('b-ec-wiki');
    const pages = c.wiki_pages || [];
    const chip = p => {
      const rm = p.kind === 'linked' ? ` <span class="x" data-rm="${esc(p.path)}" title="Unlink from card">×</span>` : '';
      const label = esc(p.title || p.path) + (p.missing ? ' (missing)' : '');
      return `<span class="b-filter-chip" style="${p.missing ? 'opacity:.6;' : ''}"><span data-wiki="${esc(p.path)}" style="cursor:pointer">${label}</span>${rm}</span>`;
    };
    const chips = pages.length ? pages.map(chip).join(' ') : '<span class="b-hint">No linked pages yet.</span>';
    const create = pages.some(p => p.kind === 'derived') ? '' :
      `<button class="b-btn b-btn-secondary b-btn-sm" id="b-ec-create-wiki">Create wiki page</button>`;
    box.innerHTML =
      `<div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px">${chips}</div>` +
      `<div style="display:flex;gap:6px;align-items:center">${create}` +
      `<input id="b-ec-wiki-add" list="b-ec-wiki-pages" placeholder="Link an existing wiki page…" ` +
      `style="flex:1;background:var(--field);border:1px solid var(--line);color:var(--text);padding:5px 8px;border-radius:6px;font-size:12px">` +
      `<button class="b-btn b-btn-secondary b-btn-sm" id="b-ec-wiki-link">Link</button></div>` +
      `<div class="b-hint">Derived pages under <code>AI/projects/${esc(c.slug)}/</code> are self-healing; “Link” points the card at any other existing page.</div>`;
    box.querySelectorAll('[data-wiki]').forEach(el => el.addEventListener('click', () => openWiki(el.dataset.wiki)));
    box.querySelectorAll('[data-rm]').forEach(el => el.addEventListener('click', e => { e.stopPropagation(); removeWikiLink(el.dataset.rm); }));
    const cb = $('b-ec-create-wiki'); if (cb) cb.addEventListener('click', () => createWiki(c.slug));
    $('b-ec-wiki-link').addEventListener('click', addWikiLink);
    $('b-ec-wiki-add').addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); addWikiLink(); } });
    loadWikiPagesList();
  }
  function addWikiLink() {
    const inp = $('b-ec-wiki-add');
    const path = (inp.value || '').trim().replace(/^\/+/, '');
    if (!path) return;
    const cur = (_editCard && _editCard.wiki_links) || [];
    if (cur.some(p => p.toLowerCase() === path.toLowerCase())) { inp.value = ''; return; }
    saveWikiLinks([...cur, path]);
  }
  function removeWikiLink(path) {
    saveWikiLinks(((_editCard && _editCard.wiki_links) || []).filter(p => p !== path));
  }
  async function saveWikiLinks(links) {
    try {
      await apiSend('PATCH', `/cards/${encodeURIComponent(_editSlug)}`, { wiki_links: links });
      const res = await apiGet(`/cards/${encodeURIComponent(_editSlug)}`);
      if (res.card) renderWikiThread(res.card);
    } catch (e) { alertMsg('error', e.message); }
  }
  async function loadWikiPagesList() {
    if (_wikiPagesLoaded) return;
    try {
      const res = await apiGet('/wiki-pages');
      const dl = $('b-ec-wiki-pages');
      if (dl) dl.innerHTML = (res.pages || []).map(p => `<option value="${esc(p)}"></option>`).join('');
      _wikiPagesLoaded = true;
    } catch (e) { /* picker is optional */ }
  }
  // ── Relationships ───────────────────────────────────────────────────────────
  const REL_KINDS = [
    { kind: 'related', label: 'relates to' },
    { kind: 'blocks', label: 'blocks' },
    { kind: 'blocked-by', label: 'blocked by' },
  ];
  const REL_DEFAULT_KIND = 'related';

  // Three groups rather than one list, because the three say different things:
  // what is holding this card up (the one that decides whether you can start),
  // what this card is holding up (the one that decides whether it is urgent),
  // and what merely reads alongside it.
  // What this card is currently tied to, kept so the type-ahead can show an
  // already-linked card as linked rather than offering it again.
  let _relNow = null;

  function renderRelationships(rel) {
    const box = $('b-ec-rel'); if (!box) return;
    rel = rel || { blocked_by: [], blocks: [], related: [] };
    _relNow = rel;
    const chip = (r, kind, blocking) => {
      const label = (r.number == null ? '' : '#' + esc(r.number) + ' ') + esc(r.title || r.slug);
      // A blocker that is already done is shown struck through rather than
      // hidden: "this WAS waiting on that" is the history of the card, and
      // dropping it silently would make a resolved dependency look like one
      // that was never declared.
      const done = r.resolved ? ' b-rel-done' : '';
      return `<span class="b-filter-chip b-rel-chip${blocking && !r.resolved ? ' blocking' : ''}${done}" title="${esc(r.title || r.slug)} · ${esc(r.stage || '')}">` +
        `<span data-open="${esc(r.slug)}" style="cursor:pointer">${label}</span>` +
        `<span class="x" data-unlink="${esc(r.slug)}" data-kind="${esc(kind)}" title="Remove this link">×</span></span>`;
    };
    const group = (title, items, kind, blocking) => !items.length ? '' :
      `<div class="b-rel-group"><div class="b-rel-label">${title}</div>` +
      `<div class="b-rel-chips">${items.map(r => chip(r, kind, blocking)).join('')}</div></div>`;

    const groups =
      group('Blocked by', rel.blocked_by || [], 'blocked-by', true) +
      group('Blocks', rel.blocks || [], 'blocks', false) +
      group('Related to', rel.related || [], 'related', false);

    box.innerHTML =
      (groups || '<span class="b-hint">No linked cards yet.</span>') +
      // Wraps rather than squeezes: on a phone the kind buttons and the Link
      // button leave the search field a few characters wide, and the field is
      // the part you actually type into.
      `<div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-top:8px">` +
      // "relates to" leads and is preselected: relating two cards is the common,
      // reversible action, while declaring a dependency is the deliberate one —
      // so the default is the one that is harmless to get wrong.
      `<div class="b-seg" id="b-ec-rel-kind" role="group" aria-label="Link type">` +
      REL_KINDS.map(k =>
        `<button type="button" data-kind="${esc(k.kind)}"` +
        `${k.kind === REL_DEFAULT_KIND ? ' class="sel" aria-pressed="true"' : ' aria-pressed="false"'}` +
        `>${esc(k.label)}</button>`).join('') +
      `</div>` +
      // The results panel is absolutely positioned, so the input needs a
      // positioned parent — and that parent is the flex child that has to take
      // the rest of the row.
      `<div class="b-ta-wrap" style="flex:1;position:relative">` +
      `<input id="b-ec-rel-add" autocomplete="off" placeholder="Find a card by number or title…" ` +
      `role="combobox" aria-expanded="false" aria-autocomplete="list" aria-controls="b-ec-rel-results" ` +
      `style="width:100%;box-sizing:border-box;background:var(--field);border:1px solid var(--line);color:var(--text);padding:5px 8px;border-radius:6px;font-size:12px">` +
      `<div class="b-ta-results" id="b-ec-rel-results" role="listbox" hidden></div></div>` +
      `<button class="b-btn b-btn-secondary b-btn-sm" id="b-ec-rel-link">Link</button></div>` +
      `<div class="b-hint">A blocked card stays in its column and carries a marker — it is never moved for you.</div>`;

    box.querySelectorAll('[data-open]').forEach(el => el.addEventListener('click', () => {
      closeModal('b-edit-card-modal'); cardClicked(el.dataset.open);
    }));
    box.querySelectorAll('[data-unlink]').forEach(el => el.addEventListener('click', e => {
      e.stopPropagation(); saveLink(el.dataset.unlink, el.dataset.kind, true);
    }));
    box.querySelectorAll('#b-ec-rel-kind button').forEach(btn => btn.addEventListener('click', () => {
      box.querySelectorAll('#b-ec-rel-kind button').forEach(b => {
        const sel = b === btn;
        b.classList.toggle('sel', sel);
        b.setAttribute('aria-pressed', sel ? 'true' : 'false');
      });
    }));
    $('b-ec-rel-link').addEventListener('click', addRelationship);
    // The row is re-rendered on every relationship change, so the listeners go
    // on with it rather than once at startup.
    cardTypeahead.attach();
  }

  // ── Type-ahead, shared ──────────────────────────────────────────────────────
  // Two fields on this modal want the same interaction over different data:
  // find a CARD to link, and find a PERSON to name. They were going to be two
  // suggestion lists with two sets of keyboard handling and two ways of being
  // subtly wrong, so this is the one list, parameterised by where the items
  // come from and what picking one does.
  //
  // Each instance keeps its OWN state. Several people pickers are open at once
  // on this modal, and shared module-level state would have them fighting over
  // one active row.
  function makeTypeahead(o) {
    let timer = null, items = [], active = -1, seq = 0;
    const input = () => $(o.input), box = () => $(o.box);
    const isOpen = () => { const b = box(); return !!b && !b.hidden; };

    function close() {
      const b = box(); if (!b) return;
      b.hidden = true; b.innerHTML = ''; items = []; active = -1;
      input()?.setAttribute('aria-expanded', 'false');
    }

    function render(list, emptyMsg) {
      const b = box(); if (!b) return;
      items = list; active = -1;
      if (!list.length) {
        // Saying "nothing matched" is not the same as showing an empty box: one
        // is an answer, the other looks like the search never ran.
        b.innerHTML = `<div class="b-ta-empty">${esc(emptyMsg || o.empty)}</div>`;
      } else {
        b.innerHTML = list.map((it, i) => {
          const off = o.disabled ? o.disabled(it) : false;
          return `<div class="b-ta-row${off ? ' linked' : ''}" role="option" aria-selected="false"` +
            ` data-i="${i}"${off ? ' aria-disabled="true"' : ''}>${o.row(it, off)}</div>`;
        }).join('');
        b.querySelectorAll('.b-ta-row').forEach(row => {
          row.addEventListener('mousedown', e => {
            // mousedown, not click: the input's blur would otherwise tear the
            // row down before the click completed.
            e.preventDefault();
            if (row.classList.contains('linked')) return;
            pick(items[+row.dataset.i]);
          });
        });
      }
      b.hidden = false;
      input()?.setAttribute('aria-expanded', 'true');
    }

    function pick(it) {
      if (!it) return;
      close();
      o.pick(it);
    }

    async function run() {
      // A plain field queries on its whole value. A textarea queries on the
      // token under the caret, so `query` lets the caller say which.
      const q = o.query ? o.query() : (input()?.value || '').trim();
      if (q == null) return close();
      if (!q && !o.allowEmptyQuery) return close();
      // Responses can land out of order, and an older one overwriting a newer
      // one shows results for a query the field no longer holds. Latest wins.
      const mine = ++seq;
      try {
        const found = await o.source(q);
        if (mine !== seq) return;
        render(found);
      } catch (e) {
        if (mine === seq) render([], o.errorText || 'Search unavailable.');
      }
    }

    function onInput() {
      clearTimeout(timer);
      // A local list needs no debounce; a request does. Same 250ms the board's
      // own search box uses — a keystroke is not a request.
      if (!o.debounce) return run();
      timer = setTimeout(run, o.debounce);
    }

    function move(step) {
      const rows = [...(box()?.querySelectorAll('.b-ta-row') || [])];
      if (!rows.length) return;
      active = (active + step + rows.length) % rows.length;
      rows.forEach((r, i) => {
        r.classList.toggle('active', i === active);
        r.setAttribute('aria-selected', i === active ? 'true' : 'false');
      });
      // Optional: keeping the active row in view is a nicety, and an
      // environment without scrollIntoView must not lose keyboard navigation.
      rows[active].scrollIntoView?.({ block: 'nearest' });
    }

    function onKeydown(e) {
      const open = isOpen();
      if (e.key === 'ArrowDown' && open) { e.preventDefault(); return move(1); }
      if (e.key === 'ArrowUp' && open) { e.preventDefault(); return move(-1); }
      if (e.key === 'Escape' && open) {
        // The modal also listens for Escape, on `document`. Closing just the
        // list here and stopping the event is what makes the first Escape
        // dismiss the suggestions and the second close the card.
        e.preventDefault(); e.stopPropagation();
        return close();
      }
      // ENTER IS CONSUMED ONLY WHEN IT HAS SOMETHING TO DO. This used to
      // preventDefault() unconditionally, which is invisible on the single-line
      // fields this was written for and fatal on the description TEXTAREA,
      // where the inline-mention typeahead also attaches: Enter never reached
      // the textarea, so there was no way to type a second line — and so no way
      // to type a bullet list, a numbered list or a fenced block either, since
      // every one of those is a newline away from being a paragraph.
      if (e.key === 'Enter') {
        const it = open && active >= 0 ? items[active] : null;
        if (it && !(o.disabled && o.disabled(it))) { e.preventDefault(); return pick(it); }
        // Nothing highlighted. A field that declared onEnter commits what was
        // typed; a field that did not gets the browser's own behaviour, which
        // for a textarea is the newline.
        if (o.onEnter) { e.preventDefault(); close(); return o.onEnter(); }
        if (open) close();
        return;
      }
      if (o.onKey) o.onKey(e);
    }

    function attach() {
      const el = input(); if (!el) return;
      el.addEventListener('input', onInput);
      el.addEventListener('keydown', onKeydown);
      // Leaving closes the list, but not before a click on one of the rows has
      // landed — blur fires first, and closing immediately would remove the row
      // out from under the pointer.
      el.addEventListener('blur', () => setTimeout(close, 150));
    }

    return { attach, close, isOpen };
  }

  // ── Card type-ahead ─────────────────────────────────────────────────────────
  // Linking used to mean knowing a slug: the field was an <input list> over a
  // datalist of every slug on the board, matched by the browser's own prefix
  // rule. People do not hold slugs in their heads — they say "#57" or remember
  // a few words of the title — so the search runs server-side over the same
  // `search` filter card_list already exposes, and the number lookup that
  // filter gained is what makes "57" mean card #57 rather than any card with
  // 57 somewhere in it.

  // Which cards this one is already tied to, so they are shown as linked rather
  // than offered again — re-linking is a no-op the person cannot see the point of.
  function linkedSlugs() {
    const r = _relNow || {};
    return new Set([...(r.blocked_by || []), ...(r.blocks || []), ...(r.related || [])]
      .map(x => x.slug));
  }

  const cardTypeahead = makeTypeahead({
    input: 'b-ec-rel-add', box: 'b-ec-rel-results',
    empty: 'No cards match.', errorText: 'Card search unavailable.',
    debounce: 250,
    source: async (q) => {
      const res = await apiGet('/cards', { search: q });
      // A card cannot link to itself, so offering it is offering an error.
      return (res.cards || []).filter(c => c.slug !== _editSlug).slice(0, 8);
    },
    disabled: (c) => linkedSlugs().has(c.slug),
    row: (c, already) =>
      (c.number == null ? '' : `<span class="b-ta-num">#${esc(c.number)}</span>`) +
      `<span class="b-ta-title">${esc(c.title || c.slug)}</span>` +
      (already ? '<span class="b-ta-tag">linked</span>' : ''),
    pick: (c) => pickCard(c.slug),
    onEnter: () => addRelationship(),
  });

  function pickCard(slug) {
    const sel = document.querySelector('#b-ec-rel-kind button.sel');
    saveLink(slug, sel ? sel.dataset.kind : REL_DEFAULT_KIND, false);
  }

  function addRelationship() {
    const inp = $('b-ec-rel-add');
    const other = (inp.value || '').trim();
    if (!other) return;
    const sel = document.querySelector('#b-ec-rel-kind button.sel');
    saveLink(other, sel ? sel.dataset.kind : REL_DEFAULT_KIND, false);
  }

  async function saveLink(other, kind, remove) {
    if (!_editSlug) return;
    try {
      const res = await apiSend('POST', `/cards/${encodeURIComponent(_editSlug)}/link`,
        { other, kind, remove });
      (res.warnings || []).forEach(w => alertMsg('info', w, 8000));
      const add = $('b-ec-rel-add'); if (add) add.value = '';
      // Re-read rather than patch the local copy: the server decides what the
      // edge became (blocked-by is stored as the other card's blocks), and
      // guessing here is how the modal and the board start disagreeing.
      const fresh = await apiGet(`/cards/${encodeURIComponent(_editSlug)}`);
      renderRelationships(fresh.relationships);
      refresh();                      // the blocked chip on the board behind
    } catch (e) { alertMsg('error', e.message); }
  }

  async function createWiki(slug) {
    try {
      const res = await apiSend('POST', `/cards/${encodeURIComponent(slug)}/wiki`);
      alertMsg('success', 'Wiki page created — opening it.');
      if (res.path) openWiki(res.path);
    } catch (e) { alertMsg('error', e.message); }
  }

  // ── Auto-save ───────────────────────────────────────────────────────────────
  // The detail view has no Save button: an edit is saved as it is made. A Save
  // button on a card is a trap — it looks optional, so people close the card and
  // lose the edit, which is the same failure the comment drafts fix (#108) and
  // the same answer.
  //
  // Nothing here touches the comment composer. A comment is posted by its own
  // button to its own endpoint, and auto-save deliberately never sends one: a
  // half-typed comment being published because you paused is exactly the
  // accident this feature would otherwise introduce.
  const SAVE_DEBOUNCE = 800;
  let _saveTimer = null;     // pending debounce
  let _saveInFlight = false; // a PATCH is out
  let _saveAgain = false;    // an edit arrived while it was out
  let _filling = false;      // fillCardModal is writing the fields, not a human

  function setSaveStatus(kind, text) {
    const el = $('b-ec-status'); if (!el) return;
    el.className = 'b-ec-status' + (kind ? ' ' + kind : '');
    el.textContent = text || '';
  }

  // Reads the fields as they stand. Called at FIRE time rather than at schedule
  // time so a burst of keystrokes sends one body, not one per keystroke.
  function cardEdits() {
    return {
      title: $('b-ec-title').value.trim(), body: $('b-ec-body').value,
      labels: getLabels('b-ec-labels'),
      // Only ever true because somebody took a RESERVED pill off by hand. A
      // whole-list replace that merely fails to mention one is exactly what
      // the server's protection is for, and this must not waive it — so the
      // flag is driven by the removal, not by the fact that the UI is saving.
      allow_reserved: droppedReserved('b-ec-labels'),
      repo: $('b-ec-repo').value.trim(),
      // '' is a deliberate clear, which is what the Clear button leaves.
      due: ($('b-ec-due')?.value || '').trim(),
      reporter: getPeople('b-ec-reporter')[0] || '',
      assignee: getPeople('b-ec-assignee')[0] || '',
      participants: selectedPeople('b-ec-participants'),
    };
  }

  // `delay` of 0 is for discrete edits — picking a person, removing a chip —
  // where there is no burst to wait out and waiting just looks broken.
  function scheduleCardSave(delay) {
    if (_filling || !_editSlug) return;
    if (_saveTimer) clearTimeout(_saveTimer);
    setSaveStatus('pending', 'Saving…');
    _saveTimer = setTimeout(() => { _saveTimer = null; saveCardNow(); },
                            delay === undefined ? SAVE_DEBOUNCE : delay);
  }

  // The slug is captured BEFORE the await and the body is read before it too,
  // so a save can never land the fields of one card onto another. The same
  // reason flushCardSave() runs before the modal is refilled for a new card.
  async function saveCardNow() {
    if (!_editSlug) return;
    if (_saveInFlight) { _saveAgain = true; return; }
    const slug = _editSlug;
    const payload = cardEdits();
    _saveInFlight = true;
    setSaveStatus('pending', 'Saving…');
    try {
      const res = await apiSend('PATCH', `/cards/${encodeURIComponent(slug)}`, payload);
      (res.warnings || []).forEach(w => alertMsg('info', w, 5000));
      setSaveStatus('ok', 'Saved');
      // Deferred while the card is open (refresh() holds back for an open
      // modal) and flushed on close, so the board behind stays in step without
      // re-rendering under the cursor.
      refresh();
    } catch (e) {
      // Loud, because there is no Save button left to retry with. The text
      // stays in the fields, so closing is the only way to lose it.
      setSaveStatus('err', 'Not saved');
      alertMsg('error', e.message);
    } finally {
      _saveInFlight = false;
      if (_saveAgain) { _saveAgain = false; saveCardNow(); }
    }
  }

  // Fire a pending save NOW. Called when the card is closed and before the modal
  // is refilled for another card — a debounce still ticking when either happens
  // would otherwise be dropped, or worse, fire against the next card.
  function flushCardSave() {
    if (!_saveTimer) return;
    clearTimeout(_saveTimer); _saveTimer = null;
    saveCardNow();
  }

  // Kept as the explicit "save now" entry point: Ctrl/Cmd-S, and the callers
  // that still ask for a save outright.
  async function saveCard() {
    if (_saveTimer) { clearTimeout(_saveTimer); _saveTimer = null; }
    await saveCardNow();
  }

  async function closeCard() {
    if (!_editSlug) return;
    if (!confirm('Close this card? It comes off the board (not deleted) — find it in the Closed column.')) return;
    try { await apiSend('POST', `/cards/${encodeURIComponent(_editSlug)}/close`); closeModal('b-edit-card-modal'); await load(); }
    catch (e) { alertMsg('error', e.message); }
  }
  async function reopenCard(slug) {
    try { await apiSend('POST', `/cards/${encodeURIComponent(slug)}/reopen`); alertMsg('success', 'Card reopened.'); await load(); }
    catch (e) { alertMsg('error', e.message); }
  }

  // ── Columns ─────────────────────────────────────────────────────────────────
  function renderSwatches() {
    const box = $('b-col-swatches');
    box.innerHTML =
      `<div class="b-swatch none ${_colColor ? '' : 'sel'}" title="No colour" onclick="Boards.pickColor(null)">∅</div>` +
      SWATCHES.map(c => `<div class="b-swatch ${_colColor === c ? 'sel' : ''}" style="background:${c}" onclick="Boards.pickColor('${c}')"></div>`).join('');
  }
  function pickColor(c) { _colColor = c; renderSwatches(); }

  function openColumnModal(colId) {
    _editColId = colId || null;
    const col = colId ? COLUMNS.find(c => c.id === colId) : null;
    $('b-col-modal-title').textContent = col ? 'Edit column' : 'New column';
    $('b-col-submit').textContent = col ? 'Save' : 'Add column';
    $('b-col-archive').style.display = col ? '' : 'none';
    $('b-col-name').value = col ? col.name : '';
    $('b-col-stage').innerHTML = STAGES.map(s => `<option value="${s}">${esc(s)}</option>`).join('');
    // A new column starts on the board's first stage. The fallback matters only
    // if /columns answered without a stage list at all; `backlog` is the first
    // of the current vocabulary.
    $('b-col-stage').value = col ? col.stage : (STAGES[0] || 'backlog');
    _colColor = col ? (col.color || null) : null;
    renderSwatches(); openModal('b-column-modal');
    setTimeout(() => $('b-col-name').focus(), 50);
  }

  async function submitColumn() {
    const name = $('b-col-name').value.trim();
    const stage = $('b-col-stage').value;
    if (!name) { alertMsg('error', 'A column name is required.'); return; }
    const btn = $('b-col-submit'); btn.disabled = true;
    const body = { name, stage, color: _colColor };
    try {
      if (_editColId) await apiSend('PATCH', `/columns/${_editColId}`, body);
      else await apiSend('POST', '/columns', body);
      closeModal('b-column-modal'); await load();
    } catch (e) { alertMsg('error', e.message); } finally { btn.disabled = false; }
  }

  async function archiveColumn() {
    if (!_editColId) return;
    if (!confirm('Archive this column? Its cards move to the nearest column of the same stage.')) return;
    try {
      const res = await apiSend('POST', `/columns/${_editColId}/archive`);
      if (res.moved_count) alertMsg('info', `Moved ${res.moved_count} card(s) to “${res.moved_to}”.`, 4000);
      closeModal('b-column-modal'); await load();
    } catch (e) { alertMsg('error', e.message); }
  }

  // ── Modal + init ──────────────────────────────────────────────────────────
  function openModal(id) { const m = $(id); if (m) m.classList.add('open'); }
  // Closing a modal is the moment a refresh held back for it becomes safe.
  function closeModal(id) {
    if (id === 'b-edit-card-modal') flushCardSave();
    const m = $(id); if (m) m.classList.remove('open');
    flushDeferredRefresh();
  }

  // Called by the SPA when the Board tab is activated. Builds the UI once, then
  // (re)loads. `card` optionally opens a card by slug once data is loaded.
  function init(card) {
    const root = document.getElementById('boards-root');
    if (!root) return;
    if (!_built) { root.innerHTML = shellHTML(); wireModals(); wireMentions(); _built = true; }
    if (card) _pendingCard = card;
    load();
  }

  // Open a card by slug once the board is on-screen. If the board data is already
  // loaded, open immediately; otherwise queue it for the in-flight load (init()
  // was just triggered by the tab switch). Used by wiki card-chip deep-links.
  function openCard(slug) {
    if (!slug) return;
    if (COLUMNS.length) cardClicked(slug);
    else _pendingCard = slug;
  }

  window.Boards = {
    // startPolling/stopPolling keep their names because the SPA calls them on
    // every tab switch; they now start and stop the whole live path (stream
    // plus the poll behind it) rather than the poll alone.
    init, openCard, refresh,
    startPolling: startLive, stopPolling: stopLive,
    onSearch, clearSearch, filterLabel, filterRepo, filterPerson, toggleClosed,
    addLabel, removeLabel, getLabels, droppedReserved,
    openColumnModal, submitColumn, archiveColumn,
    openCardModal, submitCard, cardClicked, saveCard, closeCard, reopenCard,
    showDescription, pickColor, closeModal, copyPermalink, clearDue,
    // Exposed for the tests, which have to pin "today" to check the states.
    _dueInfo: dueInfo,
  };

  // Landing straight on the Board tab (deep-link / refresh / the board.html
  // redirect shim) — the SPA's on-load tab restore ran BEFORE this deferred
  // script defined window.Boards, so its Boards.init() was skipped. Self-init
  // here, honouring a #boards/<slug> card deep-link.
  //
  // This path has to start the live updates too. The SPA's tab restore is what
  // normally starts them, and it was skipped for the same reason init() was —
  // so a board opened by its own URL used to load once and then never change
  // again, which is exactly the "needs a hard refresh" this is here to fix.
  const _hashParts = (location.hash || '').replace(/^#/, '').split('/');
  const _activeTab = document.querySelector('.tab-btn.active');
  if (_hashParts[0] === 'boards' || (_activeTab && _activeTab.dataset.tab === 'boards')) {
    const _card = _hashParts.slice(1).join('/');
    init(_card ? decodeURIComponent(_card) : undefined);
    startLive();
  }
})();
