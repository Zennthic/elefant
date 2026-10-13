// Native Boards tab (static/boards.js): the IIFE must expose window.Boards with
// every method its inline onclicks reference, and init() must build the shell +
// render columns/cards from the /api/board API. Guards the de-iframe port against
// a missing handler or a broken render (which can't be seen from here otherwise).

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { JSDOM } from 'jsdom';

const __dirname = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(__dirname, '../static/boards.js'), 'utf8');

const css = readFileSync(join(__dirname, '../static/boards.css'), 'utf8');

let ok = true;
const fail = (m) => { console.error('❌ ' + m); ok = false; };

// 1b) The two-column card modal is a STYLESHEET feature — jsdom does no layout,
// so the rules themselves are what can be checked here. Each of these is a way
// the modal breaks that no assertion about the DOM would catch.
{
  const wide = (css.match(/\.b-modal\.b-modal-wide\s*\{[^}]*\}/) || [''])[0];
  // A fixed width would overflow a narrow viewport: the cap has to be relative.
  if (!/min\(|max-width:\s*\d+vw/.test(wide))
    fail(`the wide modal has no viewport-relative cap: ${wide}`);
  const split = (css.match(/\.b-ec-split\s*\{[^}]*\}/) || [''])[0];
  if (!/grid-template-columns/.test(split)) fail('the card modal split is not a grid');
  // Without align-items:start the rail stretches to a long description, and the
  // modal's single scroll region turns into columns of mismatched height.
  if (!/align-items:\s*start/.test(split))
    fail('the metadata rail will stretch to the height of the content column');
  // The design called this out: boards.css had NO media queries at all, and one
  // max-width:92vw is enough for a single column and not for two.
  const mq = css.match(/@media\s*\([^)]*max-width:\s*720px[^)]*\)\s*\{([\s\S]*?)\n\}/);
  if (!mq) fail('no 720px breakpoint — the two-column modal never collapses on a phone');
  else if (!/grid-template-columns:\s*minmax\(0,\s*1fr\)\s*;/.test(mq[1]))
    fail('the 720px breakpoint does not collapse the split to one column');
}

// 1) Every Boards.X() used in an inline onclick must be exposed on window.Boards.
const used = [...new Set([...src.matchAll(/Boards\.(\w+)\s*\(/g)].map(m => m[1]))];
const objBody = (src.match(/window\.Boards\s*=\s*\{([\s\S]*?)\};/) || [])[1] || '';
const exposed = new Set([...objBody.matchAll(/\b(\w+)\b/g)].map(m => m[1]));
for (const u of used) if (!exposed.has(u)) fail(`Boards.${u}() used in onclick but not exposed`);

// 2) init() builds the shell and renders columns + cards from mocked API.
const dom = new JSDOM('<!doctype html><html><body><div id="boards-root"></div></body></html>', {
  runScripts: 'dangerously', pretendToBeVisual: true, url: 'http://localhost/',
});
const { window } = dom;
const cols = {
  columns: [
    { id: 'c1', name: 'Spark', stage: 'spark', position: 1, color: '#818CF8' },
    { id: 'c2', name: 'Building', stage: 'building', position: 2, color: null },
  ], stages: ['spark', 'building', 'shipped'], nudge_chars: 280,
};
const cards = {
  cards: [
    { slug: 'idea-one', title: 'Idea One', body: 'hi', column_id: 'c1', position: 1, source: 'ui', tags: ['infra'], labels: ['bug', 'elefant'], repo: 'zennthic/elefant', number: 7, reporter: 'user:jon', assignee: 'agent:bot', participants: ['agent:bot'], comment_count: 3 },
    // Labels are free-form and agent-writable, so a hostile one must stay inert.
    // Labels AND the repo are agent-writable, so a hostile value in either must
    // stay inert. The repo is validated server-side, but the UI renders whatever
    // the API hands it and must not depend on that validation for its safety.
    // A principal reference is the same story: validated server-side, rendered
    // here from whatever the API returned.
    { slug: 'nasty', title: 'Nasty', body: '', column_id: 'c1', position: 3, source: 'claude', tags: [], labels: ["x'); window.__pwned = 1; ('"], repo: "y'); window.__pwned_repo = 1; ('", reporter: "user:z'); window.__pwned_person = 1; ('", participants: [] },
    // `related` is read only by the single-card payload (the board list ignores
    // it), and it is what lets a test open a card that is ALREADY linked to
    // another card the search can return.
    { slug: 'do-thing', title: 'Do Thing', body: 'x', column_id: 'c2', position: 1, source: 'agent', tags: [], labels: [], repo: '', reporter: '', participants: [], related: [{ slug: 'idea-one', number: 7, title: 'Idea One' }] },
    // A blocked card. The blocker's slug is agent-writable like every other
    // value the board renders, so a hostile one must stay inert here too.
    {
      slug: 'waiting', title: 'Waiting', body: '', column_id: 'c2', position: 2, source: 'claude',
      tags: [], labels: [], repo: '', reporter: '', participants: [],
      blocked: true,
      blocked_by: [{ slug: "b'); window.__pwned_blocker = 1; ('", number: 42, title: 'The Blocker' }],
    },
    // Under evaluation: an automated reviewer holds this card's lock. `run_id`
    // comes from an external reviewer over the API, so like every other value
    // the board renders it must stay inert.
    {
      slug: 'locked', title: 'Locked', body: '', column_id: 'c2', position: 3, source: 'claude',
      tags: [], labels: [], repo: '', reporter: '', participants: [],
      eval_lock: { run_id: "r'); window.__pwned_run = 1; ('", since: '2026-09-19T09:30:00+00:00', stale: false },
    },
    // A STALE lock: held past the ceiling, so the run holding it has probably
    // died. Distinct from a live one, because the useful reading differs.
    {
      slug: 'stale-locked', title: 'Stale Locked', body: '', column_id: 'c2', position: 4, source: 'claude',
      tags: [], labels: [], repo: '', reporter: '', participants: [],
      eval_lock: { run_id: 'run-dead', since: '2026-09-19T08:00:00+00:00', stale: true },
    },
  ],
  // The display map the editor attaches. People are stored on a card as opaque
  // references and named at render time, so a person renamed once is renamed
  // on every card at once.
  people: {
    'user:jon': { ref: 'user:jon', kind: 'user', display: 'Jon Smith', handle: 'jon', active: true, known: true },
    'agent:bot': { ref: 'agent:bot', kind: 'agent', display: 'Dispatch routine', handle: 'claude_code', active: true, known: true },
  },
};
// Two members and an agent account — what the pickers are built from, so an
// option that exists is an account that exists.
const directory = {
  // The server says who the caller is; the browser does not decide.
  me: 'user:jon',
  people: [
    { ref: 'user:jon', kind: 'user', display: 'Jon Smith', handle: 'jon', active: true, known: true },
    { ref: 'user:jan', kind: 'user', display: 'Jan Novak', handle: 'jan', active: true, known: true },
    { ref: 'agent:bot', kind: 'agent', display: 'Dispatch routine', handle: 'claude_code', active: true, known: true },
  ],
};
// GET /cards/<slug> is a DIFFERENT payload from the list — `{card, relationships}`,
// not `{cards}` — and boards.js renders the modal only when `res.card` is there.
// Serving the list for both leaves the modal on "Card not found" while every
// assertion about it still reads a stale or defaulted control, so the fixture
// has to tell the two apart.
const oneCard = (url) => {
  const slug = decodeURIComponent(String(url).split('/cards/')[1].split('?')[0]);
  const card = cards.cards.find(c => c.slug === slug);
  if (!card) return { card: null };
  return {
    card,
    people: cards.people,
    comments,
    relationships: {
      blocked_by: card.blocked_by || [], blocks: [], related: card.related || [],
    },
  };
};
// The card list is also what the type-ahead queries, so the fixture has to
// honour `search` — matching the server's own rule closely enough to tell a
// real query from a missing one: an all-digits query is the card NUMBER, and
// anything else is a case-insensitive substring of the title.
const searches = [];
const listFor = (url) => {
  const q = (new URL(String(url), 'http://localhost').searchParams.get('search') || '').trim();
  if (!q) return cards;
  searches.push(q);
  const digits = q.replace(/^#/, '');
  const hits = /^\d+$/.test(digits)
    ? cards.cards.filter(c => String(c.number) === digits)
    : cards.cards.filter(c => (c.title || '').toLowerCase().includes(q.toLowerCase()));
  return { cards: hits, people: cards.people };
};
// A card's thread. `user:jon` is the caller (see directory.me above), so one of
// these is the caller's own and one is somebody else's — which is what the
// author-only controls are read against.
let comments = [
  { id: 'c-1', author: 'user:jon', body: 'mine, with **markdown**', deleted: false,
    edited: false, created_at: '2026-09-11T10:00:00' },
  { id: 'c-2', author: 'agent:bot', body: 'theirs', deleted: false,
    edited: true, created_at: '2026-09-11T11:00:00' },
  { id: 'c-3', author: 'user:jon', body: '', deleted: true,
    edited: false, created_at: '2026-09-11T12:00:00' },
];
const commentCalls = [];

// What /board/labels serves: the vocabulary in use on some card, plus the
// labels the board reserves. `note-to-self` is both — in use AND reserved.
const labelsInUse = {
  labels: ['bug', 'elefant', 'enhancement', 'infra', 'note-to-self'],
  reserved: ['note-to-self'],
};

// Links written by the picker, so a test can assert what it actually sent
// rather than that it merely looked like it did something.
const linkPosts = [];
window.fetch = async (url, opts) => {
  const u = String(url);
  if (/\/cards\/[^/?]+\/comments/.test(u)) {
    const method = (opts && opts.method) || 'GET';
    if (method !== 'GET') {
      commentCalls.push({ method, url: u, body: JSON.parse((opts && opts.body) || '{}') });
      return { ok: true, status: 200, json: async () => ({ comment: {} }) };
    }
    return { ok: true, status: 200,
             json: async () => ({ comments, people: cards.people }) };
  }
  if (opts && opts.method === 'POST' && /\/cards\/[^/?]+\/link$/.test(u)) {
    linkPosts.push({ url: u, body: JSON.parse(opts.body || '{}') });
    return { ok: true, status: 200, json: async () => ({ warnings: [] }) };
  }
  return {
    ok: true, status: 200,
    json: async () => (u.includes('/columns') ? cols
      : (u.includes('/labels') ? labelsInUse
        : (u.includes('/people') ? directory
          : (/\/cards\/[^/?]+/.test(u) ? oneCard(u)
            : (u.includes('/cards') ? listFor(u) : {}))))),
  };
};
// Capture what boards.js asks Sortable for. The touch-arming config is the whole
// mobile scroll fix, and it is invisible from the rendered DOM — the only place
// it can be checked from here is the options object handed to Sortable.create.
const sortables = [];
window.Sortable = { create: (el, opts) => { sortables.push({ el, opts }); return { destroy() {} }; }, destroy() {} };

// The REAL marked, the same build the page loads. Descriptions are rendered
// from agent-written text, so the sanitiser has to be tested against what the
// parser actually emits rather than against a stub that emits what the test
// expected — the interesting cases are exactly the ones a stub would miss.
const markedSrc = readFileSync(join(__dirname, '../static/marked.min.js'), 'utf8');
const ms = window.document.createElement('script');
ms.textContent = markedSrc;
window.document.body.appendChild(ms);
if (typeof window.marked === 'undefined') fail('marked did not load into the test page');

const s = window.document.createElement('script');
s.textContent = src;
window.document.body.appendChild(s);

if (!window.Boards || typeof window.Boards.init !== 'function') fail('window.Boards.init missing');
else {
  await window.Boards.init();
  await new Promise(r => setTimeout(r, 50));
  const root = window.document.getElementById('boards-root');
  const columns = root.querySelectorAll('.b-column');
  const cardEls = root.querySelectorAll('.b-card');
  const names = [...root.querySelectorAll('.b-column-name')].map(e => e.textContent);
  const titles = [...root.querySelectorAll('.b-card-title')].map(e => e.textContent);
  if (columns.length !== 2) fail(`expected 2 columns, got ${columns.length}`);
  if (cardEls.length !== 6) fail(`expected 6 cards, got ${cardEls.length}`);
  if (!names.includes('Spark')) fail('column "Spark" not rendered');
  if (!titles.includes('Idea One')) fail('card "Idea One" not rendered');
  if (!window.document.getElementById('b-card-modal')) fail('card modal not built');
  if (!window.document.getElementById('b-column-modal')) fail('column modal not built');

  // 2a-000) The description: its name, its size, and markdown — including the
  // part that matters most, which is that rendering markdown must not turn an
  // agent-written field into a way to put markup in the board's DOM.
  {
    const $id = (id) => window.document.getElementById(id);
    const labelOf = (id) => $id(id)?.closest('.b-form-group')?.querySelector('label')?.textContent || '';

    // "Body" is an implementation word. Nowhere a person reads it.
    for (const id of ['b-cm-body', 'b-ec-body']) {
      const label = labelOf(id);
      if (!/description/i.test(label)) fail(`#${id} is labelled "${label.trim()}", not Description`);
      if (/\bbody\b/i.test(label)) fail(`#${id} still says Body: "${label.trim()}"`);
    }

    const render = (md) => {
      window.Boards.cardClicked('idea-one');
      $id('b-ec-body').value = md;
      window.Boards.showDescription('preview');
      return $id('b-ec-body-preview');
    };

    // Markdown renders at all.
    {
      const box = render('a **bold** word');
      if (!box.querySelector('strong')) fail(`markdown did not render: ${box.innerHTML}`);
    }
    // A fenced block is a block, and its whitespace is preserved rather than
    // reflowed — a quoted traceback is read by its lines.
    {
      const box = render('```\nline one\n  indented\n```');
      const pre = box.querySelector('pre code');
      if (!pre) fail(`a fenced block did not render as a code block: ${box.innerHTML}`);
      else if (!pre.textContent.includes('  indented'))
        fail('a fenced block lost its leading whitespace');
    }

    // ── The security cases. Each of these is a description an agent could
    // write, and none of them may survive into the DOM as live markup.
    const hostile = [
      ['a script tag', '<script>window.__pwned_md = 1;</script>hello'],
      ['an inline handler', '<img src=x onerror="window.__pwned_md = 1">'],
      ['an svg handler', '<svg onload="window.__pwned_md = 1"></svg>'],
      ['a javascript: link', '[click me](javascript:window.__pwned_md=1)'],
      ['an iframe', '<iframe src="javascript:window.__pwned_md=1"></iframe>'],
      ['a raw event attribute on a allowed tag', '<p onclick="window.__pwned_md=1">text</p>'],
    ];
    for (const [what, md] of hostile) {
      const box = render(md);
      const html = box.innerHTML;
      if (window.__pwned_md) fail(`${what} in a description EXECUTED`);
      if (/<script|<iframe|<svg/i.test(html)) fail(`${what} survived into the DOM: ${html}`);
      if (/\son\w+\s*=/i.test(html)) fail(`${what} left an event handler in the DOM: ${html}`);
      if (/javascript:/i.test(html)) fail(`${what} left a javascript: URL in the DOM: ${html}`);
      window.__pwned_md = 0;
    }
    // An ordinary link survives, or the sanitiser is just deleting everything.
    {
      const box = render('[elefant](https://example.com/x)');
      const a = box.querySelector('a');
      if (!a) fail('a normal link did not survive sanitising');
      else {
        if (a.getAttribute('href') !== 'https://example.com/x')
          fail(`a normal link lost its href: ${a.getAttribute('href')}`);
        if (a.getAttribute('rel') !== 'noopener noreferrer')
          fail('an external link has no rel=noopener');
      }
    }
    // Text inside a stripped tag is kept rather than dropped: the description
    // should still READ after sanitising.
    {
      const box = render('<span>kept text</span>');
      if (!box.textContent.includes('kept text')) fail('sanitising dropped ordinary text');
    }

    // Write/Preview, and opening a card lands on Write — left on Preview, the
    // next card would open showing the previous card's rendered text.
    window.Boards.showDescription('write');
    if ($id('b-ec-body').hidden) fail('Write mode does not show the editor');
    if (!$id('b-ec-body-preview').hidden) fail('Write mode still shows the preview');
    window.Boards.showDescription('preview');
    if (!$id('b-ec-body').hidden) fail('Preview mode still shows the editor');
    window.Boards.cardClicked('idea-one');
    await new Promise(r => setTimeout(r, 40));
    if ($id('b-ec-body').hidden) fail('reopening a card left it on Preview');

    // The card FACE stays plain text. A sticky should stay a sticky, and this
    // is also the escaping path that has always been there.
    const faceHTML = [...root.querySelectorAll('.b-card-body')].map(e => e.innerHTML).join('');
    if (/<strong>|<pre>/.test(faceHTML)) fail('the board face renders markdown — it should stay a sticky');
    window.Boards.closeModal('b-edit-card-modal');
  }

  // 2a-000000) Comments. A card records what the work IS; the thread is the
  // conversation about it, which otherwise happens in a chat window and is gone
  // by the next session.
  {
    const $id = (id) => window.document.getElementById(id);
    const shown = () => [...($id('b-ec-comments')?.querySelectorAll('.b-comment') || [])];

    window.Boards.cardClicked('idea-one');
    await new Promise(r => setTimeout(r, 40));

    if (shown().length !== 3) fail(`expected 3 comments in the thread, got ${shown().length}`);
    else {
      // Authors are NAMED, like every other person on a card. A thread of raw
      // `user:9f2c…` would be the one place the board showed references.
      if (!shown()[0].textContent.includes('Jon Smith'))
        fail(`a comment does not name its author: ${shown()[0].textContent}`);
      if (!shown()[1].textContent.includes('Dispatch routine'))
        fail('an agent comment does not name the agent');
      // Markdown renders, through the same path as the description — which is
      // how comments get the sanitiser and @-mentions without a second
      // implementation.
      if (!shown()[0].querySelector('.b-comment-body strong'))
        fail(`comment markdown did not render: ${shown()[0].innerHTML}`);
      // An edit is visible as an edit.
      if (!/edited/i.test(shown()[1].textContent)) fail('an edited comment does not say so');
      // A withdrawn comment KEEPS ITS PLACE. A thread that silently loses a
      // message reads as one that never had it.
      if (!shown()[2].classList.contains('deleted')) fail('a withdrawn comment was not marked');
      if (!/withdrawn/i.test(shown()[2].textContent)) fail('a withdrawn comment says nothing');
      // AUTHOR ONLY, and only offered to the author — a control everybody can
      // see and only one person can use is a button that 403s.
      if (!shown()[0].querySelector('[data-edit]')) fail('the author cannot edit their own comment');
      if (shown()[1].querySelector('[data-edit]'))
        fail("somebody else's comment offers an Edit button");
      if (shown()[1].querySelector('[data-del]'))
        fail("somebody else's comment offers a Delete button");
    }

    // Hostile markdown in a comment is inert, same as in a description.
    {
      const hostile = [...shown()];
      comments = [{ id: 'c-x', author: 'user:jon', deleted: false, edited: false,
                    created_at: '2026-09-11T10:00:00',
                    body: '<script>window.__pwned_c=1;</script>[x](javascript:window.__pwned_c=1)' }];
      window.Boards.cardClicked('idea-one');
      await new Promise(r => setTimeout(r, 40));
      const box = $id('b-ec-comments');
      if (box.querySelector('script')) fail('a script in a comment reached the DOM');
      if (box.querySelector('[href^="javascript:" i]')) fail('a javascript: link survived in a comment');
      if (window.__pwned_c) fail('a comment EXECUTED');
      void hostile;
    }

    // POSTING sends the text and clears the box.
    comments = [];
    window.Boards.cardClicked('idea-one');
    await new Promise(r => setTimeout(r, 40));
    if (!/no comments yet/i.test($id('b-ec-comments').textContent))
      fail('an empty thread said nothing');
    commentCalls.length = 0;
    $id('b-ec-comment-body').value = 'found the cause';
    $id('b-ec-comment-add').click();
    await new Promise(r => setTimeout(r, 60));
    if (!commentCalls.length) fail('posting a comment sent nothing');
    else {
      const sent = commentCalls[0];
      if (sent.method !== 'POST') fail(`posting used ${sent.method}`);
      if (sent.body.body !== 'found the cause') fail(`posted the wrong text: ${sent.body.body}`);
      // The AUTHOR is not in the body: the server stamps it from the session,
      // or author-only editing means nothing.
      if ('author' in sent.body) fail('the browser claimed an author for the comment');
    }
    if ($id('b-ec-comment-body').value !== '') fail('the composer kept the text after posting');

    // An empty comment posts nothing.
    commentCalls.length = 0;
    $id('b-ec-comment-body').value = '   ';
    $id('b-ec-comment-add').click();
    await new Promise(r => setTimeout(r, 40));
    if (commentCalls.length) fail('an empty comment was posted');

    // EDITING goes to that comment, by id, as a PATCH.
    comments = [{ id: 'c-9', author: 'user:jon', body: 'frist', deleted: false,
                  edited: false, created_at: '2026-09-11T10:00:00' }];
    window.Boards.cardClicked('idea-one');
    await new Promise(r => setTimeout(r, 40));
    shown()[0].querySelector('[data-edit]').click();
    if ($id('b-ec-comment-body').value !== 'frist')
      fail('editing did not load the comment into the composer');
    commentCalls.length = 0;
    $id('b-ec-comment-body').value = 'first';
    $id('b-ec-comment-add').click();
    await new Promise(r => setTimeout(r, 60));
    if (!commentCalls.length) fail('editing a comment sent nothing');
    else {
      if (commentCalls[0].method !== 'PATCH') fail(`editing used ${commentCalls[0].method}`);
      if (!commentCalls[0].url.includes('/comments/c-9'))
        fail(`the edit went to ${commentCalls[0].url}`);
    }

    // The composer belongs to the card it was opened on — a half-finished edit
    // must not follow you to the next card and overwrite a comment there.
    shown()[0]?.querySelector('[data-edit]')?.click();
    window.Boards.cardClicked('do-thing');
    await new Promise(r => setTimeout(r, 40));
    if ($id('b-ec-comment-body').value !== '') fail('an unsent comment followed the card change');
    if ($id('b-ec-comment-add').textContent !== 'Comment')
      fail('a half-finished edit followed the card change');

    // DRAFTS SURVIVE. A comment typed but never sent used to die with the modal.
    // The text is persisted per card as it is typed and restored on the way back
    // in, so closing the card, following a link or reloading no longer throws
    // away the thing somebody had just finished thinking about.
    {
      const type = (text) => {
        const ta = $id('b-ec-comment-body');
        ta.value = text;
        ta.dispatchEvent(new window.Event('input', { bubbles: true }));
      };
      const stored = (slug) => window.localStorage.getItem(`elefant:board:draft:comment:${slug}`);

      comments = [];
      window.Boards.cardClicked('idea-one');
      await new Promise(r => setTimeout(r, 40));
      type('half a thought');
      if (stored('idea-one') !== 'half a thought')
        fail(`the draft was not persisted as it was typed: ${stored('idea-one')}`);

      // Closing the card and coming back must bring it with you.
      window.Boards.closeModal('b-edit-card-modal');
      window.Boards.cardClicked('idea-one');
      await new Promise(r => setTimeout(r, 40));
      if ($id('b-ec-comment-body').value !== 'half a thought')
        fail(`the draft did not come back: "${$id('b-ec-comment-body').value}"`);

      // A draft belongs to ITS card. Restoring one card's draft into another's
      // composer would be worse than losing it.
      window.Boards.cardClicked('do-thing');
      await new Promise(r => setTimeout(r, 40));
      if ($id('b-ec-comment-body').value !== '')
        fail(`one card's draft leaked into another: "${$id('b-ec-comment-body').value}"`);

      // Emptying the composer clears the key rather than storing '' — otherwise
      // every card ever opened leaves a row behind forever.
      window.Boards.cardClicked('idea-one');
      await new Promise(r => setTimeout(r, 40));
      type('');
      if (stored('idea-one') !== null)
        fail(`an emptied composer left a row behind: ${JSON.stringify(stored('idea-one'))}`);

      // POSTING consumes the draft: the text became a comment, so the stored
      // copy must not come back on the next open.
      type('found the cause');
      commentCalls.length = 0;
      $id('b-ec-comment-add').click();
      await new Promise(r => setTimeout(r, 60));
      if (!commentCalls.length) fail('the restored draft did not post');
      if (stored('idea-one') !== null)
        fail(`a posted draft was not cleared: ${JSON.stringify(stored('idea-one'))}`);
      window.Boards.cardClicked('idea-one');
      await new Promise(r => setTimeout(r, 40));
      if ($id('b-ec-comment-body').value !== '')
        fail('a posted comment came back as a draft');

      // An EDIT of an existing comment is deliberately NOT persisted. Restoring
      // one into a composer no longer in edit mode would post somebody's edit as
      // a brand-new comment — a worse failure than losing an in-flight edit.
      comments = [{ id: 'c-9', author: 'user:jon', body: 'frist', deleted: false,
                    edited: false, created_at: '2026-09-11T10:00:00' }];
      window.Boards.cardClicked('idea-one');
      await new Promise(r => setTimeout(r, 40));
      type('parked draft');
      shown()[0].querySelector('[data-edit]').click();
      type('first');
      if (stored('idea-one') !== 'parked draft')
        fail(`an in-flight edit overwrote the card's draft: ${JSON.stringify(stored('idea-one'))}`);
      // ...and the parked draft is still there after the edit is saved.
      $id('b-ec-comment-add').click();
      await new Promise(r => setTimeout(r, 60));
      if (stored('idea-one') !== 'parked draft')
        fail(`saving an edit threw away the parked draft: ${JSON.stringify(stored('idea-one'))}`);
      window.localStorage.clear();

      // NO STORAGE AT ALL. localStorage throws outright in a Safari private
      // window and where site data is blocked. A composer that throws on every
      // keypress is far worse than one that forgets.
      const realStorage = Object.getOwnPropertyDescriptor(window, 'localStorage');
      Object.defineProperty(window, 'localStorage', {
        configurable: true,
        get() { throw new Error('site data blocked'); },
      });
      // An exception inside an event listener never reaches dispatchEvent's
      // caller — jsdom reports it on window instead — so the listener path has
      // to be watched rather than merely wrapped, or a throwing writeDraft
      // would pass this test silently.
      const listenerErrors = [];
      const onErr = (e) => listenerErrors.push(e.error || e.message);
      window.addEventListener('error', onErr);
      try {
        comments = [];
        // AWAITED: cardClicked is async, so an unguarded read would surface as
        // an unhandled rejection that a bare call would swallow whole.
        await window.Boards.cardClicked('idea-one');
        await new Promise(r => setTimeout(r, 40));
        type('still typeable');
        if ($id('b-ec-comment-body').value !== 'still typeable')
          fail('typing broke where localStorage is unavailable');
      } catch (e) {
        fail(`opening a card threw where localStorage is unavailable: ${e.message}`);
      } finally {
        window.removeEventListener('error', onErr);
        if (realStorage) Object.defineProperty(window, 'localStorage', realStorage);
      }
      if (listenerErrors.length)
        fail(`typing threw where localStorage is unavailable: ${listenerErrors[0]}`);
      window.localStorage.clear();
      comments = [];
    }

    window.Boards.closeModal('b-edit-card-modal');
    comments = [];

    // THE STICKY GETS A COUNT, not the comments. "3 comments" is useful while
    // scanning a column; the conversation itself would drown it.
    const counted = [...root.querySelectorAll('.b-card')]
      .find(e => e.dataset.slug === 'idea-one');
    const chip = counted?.querySelector('.b-comment-count');
    if (!chip) fail('a card with comments shows no count');
    else if (!chip.textContent.includes('3')) fail(`the count chip reads "${chip.textContent}"`);
    if (counted?.textContent.includes('mine, with')) fail('the sticky rendered comment text');
    // A card with none grows no chip at all.
    const quiet = [...root.querySelectorAll('.b-card')].find(e => e.dataset.slug === 'do-thing');
    if (quiet?.querySelector('.b-comment-count')) fail('a card with no comments grew a count chip');

    // A new comment changes nothing else about a card, so without the count in
    // the poll signature an open board would never show one arriving.
    const sig = src.split('function _sigOf(')[1].split('}')[0];
    if (!sig.includes('c.comment_count')) fail('the comment count is not in the poll signature');
  }

  // 2a-00000) Attachments. The bug report that produced this whole batch was a
  // screenshot pasted into a chat window, because there was nowhere on the card
  // to put it — so pasting into the description is the case under test, and
  // attaching a file is the same plumbing with a different button.
  {
    const $id = (id) => window.document.getElementById(id);
    const uploads = [];
    // A clipboard carrying a screenshot, as the browser hands it over.
    const pasteEvent = (type, name) => {
      const ev = new window.Event('paste', { bubbles: true, cancelable: true });
      ev.clipboardData = {
        // A real File: FormData refuses anything that is not a Blob, so a
        // hand-rolled stand-in would fail before the upload is even attempted.
        items: [{ kind: 'file', type,
                  getAsFile: () => new window.File([new Uint8Array([1, 2, 3])], name, { type }) }],
      };
      return ev;
    };

    window.Boards.cardClicked('idea-one');
    await new Promise(r => setTimeout(r, 40));

    const _f = window.fetch;
    const stubUpload = (ok, payload) => {
      window.fetch = async (u, o) => {
        if (String(u).includes('/api/upload-')) {
          uploads.push({ url: String(u), prefix: o?.body?.get?.('prefix') });
          return { ok, status: ok ? 200 : 413, json: async () => payload };
        }
        return _f(u, o);
      };
    };

    // A SUCCESSFUL PASTE uploads under the card's own prefix and writes the
    // reference into the description at the caret.
    stubUpload(true, { path: 'cards/idea-one/1757000000-abcd1234-shot.png' });
    $id('b-ec-body').value = 'before after';
    $id('b-ec-body').selectionStart = $id('b-ec-body').selectionEnd = 'before '.length;
    $id('b-ec-body').dispatchEvent(pasteEvent('image/png', 'shot.png'));
    await new Promise(r => setTimeout(r, 60));
    if (!uploads.length) fail('pasting an image uploaded nothing');
    else {
      if (!uploads[0].url.includes('/api/upload-image'))
        fail(`a pasted image went to ${uploads[0].url}`);
      // Its own key prefix, so a card's objects are identifiable as such.
      if (uploads[0].prefix !== 'cards/idea-one')
        fail(`a pasted image was not filed under the card: ${uploads[0].prefix}`);
    }
    const written = $id('b-ec-body').value;
    if (!written.includes('![shot.png](/api/image?path=cards%2Fidea-one%2F1757000000-abcd1234-shot.png)'))
      fail(`the pasted reference was not written at the caret: ${written}`);
    if (!/^before !\[/.test(written)) fail(`the reference did not land at the caret: ${written}`);

    // A FAILED UPLOAD MUST NOT LEAVE A BROKEN REFERENCE. A reference written
    // optimistically and then orphaned is a permanently broken image, and the
    // person who pasted it cannot tell that from a slow one.
    uploads.length = 0;
    stubUpload(false, { detail: 'That image is 40MB. The limit is 10MB — link to it instead, or scale it down.' });
    $id('b-ec-body').value = 'untouched';
    $id('b-ec-body').selectionStart = $id('b-ec-body').selectionEnd = 'untouched'.length;
    $id('b-ec-body').dispatchEvent(pasteEvent('image/png', 'huge.png'));
    await new Promise(r => setTimeout(r, 60));
    if ($id('b-ec-body').value !== 'untouched')
      fail(`a failed upload wrote into the description: ${$id('b-ec-body').value}`);
    // …and the server's message is shown, not replaced with "upload failed".
    const alerts = [...window.document.querySelectorAll('#boards-root .b-alert')]
      .map(e => e.textContent).join(' | ');
    if (!alerts.includes('40MB') || !alerts.includes('10MB'))
      fail(`the upload error did not reach the person who pasted: ${alerts}`);

    // Pasting TEXT is left completely alone.
    uploads.length = 0;
    const textPaste = new window.Event('paste', { bubbles: true, cancelable: true });
    textPaste.clipboardData = { items: [{ kind: 'string', type: 'text/plain' }] };
    $id('b-ec-body').dispatchEvent(textPaste);
    await new Promise(r => setTimeout(r, 20));
    if (uploads.length) fail('pasting text tried to upload something');
    if (textPaste.defaultPrevented) fail('pasting text was swallowed by the image handler');
    window.fetch = _f;

    // THE NEW-CARD FORM TAKES A SCREENSHOT TOO. It has no card to
    // file under yet, so it used to refuse the paste outright, and it had no
    // Preview to see the result in.
    {
      window.Boards.closeModal('b-edit-card-modal');
      window.Boards.openCardModal('c1');
      uploads.length = 0;
      stubUpload(true, { path: 'cards/new/1757000000-abcd1234-draft.png' });
      const nb = $id('b-cm-body');
      nb.value = 'see ';
      nb.selectionStart = nb.selectionEnd = nb.value.length;
      nb.dispatchEvent(pasteEvent('image/png', 'draft.png'));
      await new Promise(r => setTimeout(r, 60));
      if (!uploads.length) fail('pasting an image into a new card uploaded nothing');
      else if (uploads[0].prefix !== 'cards/new')
        fail(`a new card's screenshot was not filed under the drafts prefix: ${uploads[0].prefix}`);
      if (!nb.value.includes('![draft.png](/api/image?path=cards%2Fnew%2F1757000000-abcd1234-draft.png)'))
        fail(`the new card's description did not get the reference: ${nb.value}`);
      window.fetch = _f;

      const tabs = $id('b-card-modal').querySelectorAll('.b-md-tabs button');
      if (tabs.length !== 2) fail('the new-card form has no Write/Preview tabs');
      window.Boards.showDescription('preview', 'new');
      const pv = $id('b-cm-body-preview');
      if (!pv || pv.hidden || !pv.querySelector('img'))
        fail('the new-card Preview did not show the pasted screenshot');
      if (!nb.hidden) fail('the new-card editor stayed visible under its preview');
      // …and the next time the form opens it is back on Write, empty.
      window.Boards.closeModal('b-card-modal');
      window.Boards.openCardModal('c1');
      if (nb.hidden || !$id('b-cm-body-preview').hidden)
        fail('the new-card form reopened on Preview');
      window.Boards.closeModal('b-card-modal');
      window.Boards.cardClicked('idea-one');
      await new Promise(r => setTimeout(r, 40));
    }

    // AN IMAGE MAY ONLY COME FROM OUR AUTHENTICATED ROUTE. An arbitrary src
    // would beacon out to a host of somebody's choosing the moment a card is
    // opened, and would route around "seeing an attachment requires seeing the
    // board".
    const renders = (md) => {
      $id('b-ec-body').value = md;
      window.Boards.showDescription('preview');
      return $id('b-ec-body-preview');
    };
    {
      const ours = renders('![shot](/api/image?path=cards%2Fidea-one%2Fx.png)');
      if (!ours.querySelector('img')) fail('an image from our own route did not render');
    }
    for (const [what, md] of [
      ['an external host', '![x](https://evil.example.com/beacon.png)'],
      ['a data: URI', '![x](data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=)'],
      ['a protocol-relative host', '![x](//evil.example.com/b.png)'],
      ['the attachment route', '![x](/api/attachment?path=cards%2Fa%2Fb.html)'],
    ]) {
      const box = renders(md);
      if (box.querySelector('img')) fail(`${what} was allowed as an image src: ${box.innerHTML}`);
    }

    // THE ATTACHMENT LIST. Files are listed, downloadable, and removable.
    window.Boards.showDescription('write');
    {
      const box = $id('b-ec-attach');
      if (!box) fail('the card has no attachments section');
      if (!$id('b-ec-attach-btn')) fail('there is no way to attach a file');
      // An empty list says so rather than showing nothing at all.
      if (!/nothing attached/i.test(box.textContent))
        fail(`an empty attachment list said nothing: ${box.textContent}`);
    }
  }

  // 2a-0000) Inline @-mentions in a description. The picker is #67's; what is
  // new here is TEXT — what gets stored when somebody types a mention, and what
  // gets rendered when somebody reads it.
  {
    const $id = (id) => window.document.getElementById(id);
    const ta = () => $id('b-ec-body');
    const mentionRows = () =>
      [...($id('b-ec-body-results')?.querySelectorAll('.b-ta-row') || [])];
    const typeBody = async (text) => {
      const el = ta();
      el.value = text;
      el.selectionStart = el.selectionEnd = text.length;
      el.dispatchEvent(new window.Event('input'));
      await new Promise(r => setTimeout(r, 20));
      return mentionRows();
    };

    window.Boards.cardClicked('idea-one');
    await new Promise(r => setTimeout(r, 40));

    // Typing @ mid-sentence offers the directory.
    {
      const rows = await typeBody('spoke to @jo');
      if (!rows.length) fail('typing @ in a description offered nobody');
      else if (!rows[0].textContent.includes('Jon Smith'))
        fail(`the inline picker is not directory-backed: ${rows[0].textContent}`);
    }
    // An @ inside a word is an email or a handle already in prose, not somebody
    // starting a mention.
    if ((await typeBody('mail me at jon@')).length)
      fail('an @ inside a word opened the mention picker');

    // WHAT GETS STORED is markdown's own link shape: readable raw, and
    // carrying a reference that survives the person being renamed.
    {
      await typeBody('spoke to @jo');
      mentionRows()[0].dispatchEvent(new window.MouseEvent('mousedown', { bubbles: true, cancelable: true }));
      const stored = ta().value;
      if (!stored.includes('@[Jon Smith](user:jon)'))
        fail(`a mention did not store a resolvable reference: ${stored}`);
      if (/@jon\b/.test(stored.replace('@[Jon Smith](user:jon)', '')))
        fail('the typed fragment was left behind next to the mention');
    }

    // WHAT GETS RENDERED is a chip, resolved NOW rather than from the stored
    // name — which is the entire reason the reference is stored beside it.
    {
      ta().value = 'spoke to @[Stale Old Name](user:jon) about it';
      window.Boards.showDescription('preview');
      const chip = $id('b-ec-body-preview').querySelector('.b-mention');
      if (!chip) fail(`a mention did not render as a chip: ${$id('b-ec-body-preview').innerHTML}`);
      else {
        if (chip.textContent !== '@Jon Smith')
          fail(`a mention rendered the stored name rather than the current one: "${chip.textContent}"`);
        if (chip.dataset.person !== 'user:jon') fail('the mention chip lost its reference');
        // The literal @ is folded into the chip rather than left beside it.
        if ($id('b-ec-body-preview').textContent.includes('@@'))
          fail('the mention left a stray @ next to the chip');
      }
    }
    // An agent reads as an agent, same as everywhere else people are named.
    {
      ta().value = 'ask @[claude](agent:bot)';
      window.Boards.showDescription('preview');
      const chip = $id('b-ec-body-preview').querySelector('.b-mention');
      if (!chip?.classList.contains('agent')) fail('an agent mention is not marked as an agent');
    }
    // An account that is gone renders as the stored name, in plain text. Worse
    // to read, never wrong — and never a chip pointing at nobody.
    {
      ta().value = 'ask @[Departed Person](user:departed) about it';
      window.Boards.showDescription('preview');
      const box = $id('b-ec-body-preview');
      if (box.querySelector('.b-mention')) fail('an unresolvable mention rendered as a chip');
      if (!box.textContent.includes('@Departed Person'))
        fail(`an unresolvable mention lost its stored name: ${box.textContent}`);
      // …and it must not have survived as a link carrying a bare reference.
      if (box.querySelector('a[href^="user:"]')) fail('an unresolvable mention left a user: link in the DOM');
    }
    // A mention is not a way to smuggle markup past the link sanitiser. The
    // check is on ATTRIBUTES rather than on the HTML string: this text may well
    // render as inert prose, and asserting on the serialisation would call that
    // a failure while missing a real handler on an element.
    for (const attempt of [
      '[click](user:x" onmouseover="window.__pwned_m=1)',
      '@[<img src=x onerror=window.__pwned_m=1>](user:jon)',
      '@[Jon](javascript:window.__pwned_m=1)',
    ]) {
      ta().value = attempt;
      window.Boards.showDescription('preview');
      const box = $id('b-ec-body-preview');
      const handlers = [...box.querySelectorAll('*')].filter(
        el => [...el.attributes].some(a => /^on/i.test(a.name)));
      if (handlers.length) fail(`a mention smuggled an event handler in: ${box.innerHTML}`);
      if (box.querySelector('[href^="javascript:" i]'))
        fail(`a mention smuggled a javascript: href in: ${box.innerHTML}`);
      if (window.__pwned_m) fail(`a mention EXECUTED: ${attempt}`);
    }

    // MENTIONING SOMEBODY DOES NOT PUT THEM ON THE CARD — it offers to.
    {
      const before = [...($id('b-ec-participants-chips')
        ?.querySelectorAll('.b-person-chip') || [])].length;
      await typeBody('ping @jan');
      mentionRows()[0].dispatchEvent(new window.MouseEvent('mousedown', { bubbles: true, cancelable: true }));
      const after = [...($id('b-ec-participants-chips')
        ?.querySelectorAll('.b-person-chip') || [])].length;
      if (after !== before) fail('mentioning somebody silently added them to the card');
      const offer = $id('b-ec-mention-offer');
      if (offer.hidden) fail('mentioning somebody did not offer to add them');
      else {
        offer.querySelector('[data-add]').click();
        const now = [...($id('b-ec-participants-chips')
          ?.querySelectorAll('.b-person-chip') || [])].length;
        if (now !== before + 1) fail('accepting the offer did not add the participant');
        if (!offer.hidden) fail('the offer stayed up after being accepted');
      }
    }
    // The offer belongs to the card it was raised on.
    window.Boards.cardClicked('idea-one');
    await new Promise(r => setTimeout(r, 40));
    if (!$id('b-ec-mention-offer').hidden) fail('the participant offer carried over to the next card');
    window.Boards.closeModal('b-edit-card-modal');
  }

  // 2a-00) The assignee. Reporter says who raised a card and participants say
  // who is involved; neither answers the question a board is mostly scanned
  // for, which is who has this.
  {
    const chips = [...root.querySelectorAll('.b-card-person')];
    const asg = chips.filter(e => e.classList.contains('assignee'));
    if (asg.length !== 1) fail(`expected 1 assignee chip, got ${asg.length}`);
    else {
      // Marked DIFFERENTLY from the reporter, not merely marked. The two say
      // opposite things about a card and are frequently different people, so
      // one shared treatment would blur exactly what the field was added for.
      const mark = asg[0].querySelector('.b-person-mark')?.textContent;
      const repMark = chips.find(e => e.classList.contains('reporter'))
        ?.querySelector('.b-person-mark')?.textContent;
      if (!mark) fail('the assignee chip carries no mark');
      else if (mark === repMark) fail(`assignee and reporter share the mark "${mark}"`);
      if (!asg[0].textContent.includes('Dispatch routine'))
        fail(`the assignee chip does not name the account: ${asg[0].textContent}`);
      // It leads: scanning a column is mostly asking who has what.
      if (chips[0] !== asg[0]) fail('the assignee chip does not come first');
      // And it filters on click, which is the point of resolving people at all.
      if (asg[0].dataset.person !== 'agent:bot')
        fail(`the assignee chip lost its reference: ${asg[0].dataset.person}`);
    }
    // Assigning somebody changes nothing else about a card, so without the
    // assignee in the poll signature an open board would never show it.
    const sig = src.split('function _sigOf(')[1].split('}')[0];
    if (!sig.includes('c.assignee')) fail('the assignee is not in the poll signature');
    // Both modals can set it, or the field exists everywhere except where a
    // person would use it.
    for (const id of ['b-cm-assignee', 'b-ec-assignee']) {
      if (!window.document.getElementById(id)) fail(`modal is missing #${id}`);
    }
    // Unassigned is a real answer and has to stay reachable: a field that can
    // only ever name somebody cannot hand a card back.
    window.Boards.cardClicked('idea-one');
    await new Promise(r => setTimeout(r, 40));
    const held = [...(window.document.getElementById('b-ec-assignee-chips')
      ?.querySelectorAll('.b-person-chip') || [])];
    if (held.length !== 1) fail(`the edit modal did not show the assignee: ${held.length} chips`);
    else {
      if (!held[0].textContent.includes('Dispatch routine'))
        fail(`the assignee chip names the wrong account: ${held[0].textContent}`);
      if (!held[0].querySelector('.x')) fail('the assignee chip cannot be removed — a card could never be handed back');
    }
    window.Boards.closeModal('b-edit-card-modal');
  }

  // 2a-0) The edit-card modal's two-column split. A card has a little content
  // you read and a lot of metadata you set once and then ignore; at one width
  // the repo field carried the same weight as the description.
  {
    const ecModal = window.document.querySelector('#b-edit-card-modal .b-modal');
    if (!ecModal?.classList.contains('b-modal-wide'))
      fail('the edit-card modal is not the wide variant');
    const split = window.document.querySelector('#b-edit-card-modal .b-ec-split');
    const main = window.document.querySelector('#b-edit-card-modal .b-ec-main');
    const rail = window.document.querySelector('#b-edit-card-modal .b-ec-rail');
    if (!split || !main || !rail) fail('the edit-card modal has no content/metadata split');
    else {
      // What you work with on the left, what you set and forget on the right.
      for (const id of ['b-ec-title', 'b-ec-body', 'b-ec-wiki', 'b-ec-rel']) {
        if (!main.querySelector('#' + id)) fail(`#${id} is not in the content column`);
      }
      for (const id of ['b-ec-labels', 'b-ec-reporter',
                        'b-ec-participants', 'b-ec-repo']) {
        if (!rail.querySelector('#' + id)) fail(`#${id} is not in the metadata rail`);
      }
      // DOM order IS the mobile order: one column puts the rail below the
      // content, and on a phone the first thing on screen has to be what the
      // card says rather than its labels. Reordering these in the markup would
      // silently invert that with every desktop test still passing.
      if (!(main.compareDocumentPosition(rail) & window.Node.DOCUMENT_POSITION_FOLLOWING))
        fail('the metadata rail precedes the content — it would sit above the card text on a phone');
    }
    // The split is scoped: the column modal is a handful of fields and must not
    // grow a metadata rail, and .b-modal is shared by all three modals.
    const colModal = window.document.querySelector('#b-column-modal .b-modal');
    if (colModal?.classList.contains('b-modal-wide'))
      fail('the column modal was widened along with the card modal');
    if (window.document.querySelector('#b-column-modal .b-ec-rail'))
      fail('the column modal grew a metadata rail');
    if (window.document.querySelector('#b-card-modal .b-ec-rail'))
      fail('the new-card modal grew a metadata rail (this card left it single-column)');
  }

  // 2a) Labels render as chips, and a card with no labels grows no empty chip
  // row. There was a second, rounder chip row for `tags` under this one until
  // the two fields merged; a card carries one classification row now.
  const labelEls = [...root.querySelectorAll('.b-card-label')].map(e => e.textContent);
  if (labelEls.length !== 3) fail(`expected 3 label chips, got ${labelEls.length}`);
  if (!labelEls.includes('bug') || !labelEls.includes('elefant')) fail(`label chips wrong: ${labelEls}`);
  if (root.querySelectorAll('.b-card-labels').length !== 2) fail('an unlabelled card grew a label row');

  // A label carrying a quote must not break out of the chip's onclick. esc()
  // alone does not achieve that: an HTML attribute is entity-decoded before the
  // JS inside it is parsed, so the value rides in a data attribute instead.
  const nasty = [...root.querySelectorAll('.b-card-label')].find(e => e.textContent.startsWith('x'));
  if (!nasty) fail('the hostile label did not render at all');
  nasty.click();
  if (window.__pwned) fail('a label escaped its onclick and executed — chip value is being interpolated into JS');
  if (nasty.dataset.label !== "x'); window.__pwned = 1; ('") fail('label chip lost its value on the way into the data attribute');
  // The tag chip row is gone with the field. A card fixture below still
  // carries a `tags` array, standing in for a board whose API has not been
  // redeployed yet — the UI must simply not render it rather than break.
  if (root.querySelectorAll('.b-card-tag').length)
    fail('a tag chip rendered — the tags field was merged into labels');
  // Both modals offer a Labels field, so a human can set what an agent reads.
  if (!window.document.getElementById('b-cm-labels')) fail('new-card modal has no Labels field');
  if (!window.document.getElementById('b-ec-labels')) fail('edit-card modal has no Labels field');

  // 2a-ii) The repo is the dimension that lets several projects share one
  // board, so it renders on the card and clicking it filters. A card with no
  // repo renders no repo line at all.
  const repoEls = [...root.querySelectorAll('.b-card-repo')].map(e => e.textContent);
  if (repoEls.length !== 2) fail(`expected 2 repo chips, got ${repoEls.length}`);
  if (!repoEls.includes('zennthic/elefant')) fail(`repo chip wrong: ${repoEls}`);
  const nastyRepo = [...root.querySelectorAll('.b-card-repo')].find(e => e.textContent.startsWith('y'));
  if (!nastyRepo) fail('the hostile repo did not render at all');
  else {
    nastyRepo.click();
    if (window.__pwned_repo) fail('a repo escaped its onclick and executed — chip value is being interpolated into JS');
    if (nastyRepo.dataset.repo !== "y'); window.__pwned_repo = 1; ('") fail('repo chip lost its value on the way into the data attribute');
  }
  // Clicking a repo chip filters the board to that repo, and the filter reaches
  // the API as a query param — a chip that only sets local state would show a
  // filter chip over an unfiltered board.
  let lastCardsUrl = '';
  const _fetch = window.fetch;
  // Forward every argument: a wrapper that takes only `url` silently strips the
  // method and body off each request, and any later test asserting on what was
  // POSTed then sees nothing at all.
  window.fetch = async (url, opts) => { if (String(url).includes('/cards')) lastCardsUrl = String(url); return _fetch(url, opts); };
  window.Boards.filterRepo('zennthic/elefant');
  await new Promise(r => setTimeout(r, 30));
  if (!lastCardsUrl.includes('repo=zennthic%2Felefant') && !lastCardsUrl.includes('repo=zennthic/elefant'))
    fail(`repo filter did not reach the API: ${lastCardsUrl}`);
  window.Boards.filterRepo('');
  await new Promise(r => setTimeout(r, 30));
  // Both modals offer a repo field, so a human can set what an agent reads.
  if (!window.document.getElementById('b-cm-repo')) fail('new-card modal has no repo field');
  if (!window.document.getElementById('b-ec-repo')) fail('edit-card modal has no repo field');

  // 2a-iii) People. A card says who raised it and who is on it, by NAME —
  // the card stores an opaque reference, and a chip showing `user:9f2c…` would
  // be the same unreadable thing as the body convention this replaced.
  const personEls = [...root.querySelectorAll('.b-card-person')].map(e => e.textContent);
  if (!personEls.some(t => t.includes('Jon Smith'))) fail(`reporter not named: ${personEls}`);
  if (!personEls.some(t => t.includes('Dispatch routine'))) fail(`participant not named: ${personEls}`);
  // The reporter is marked, because "whose card is this" is the question the
  // row exists to answer and a reporter that looked like just another
  // participant would bury it.
  if (!root.querySelector('.b-card-person.reporter')) fail('the reporter is not distinguished from the participants');
  if (root.querySelectorAll('.b-card-people').length !== 2) fail('a card with nobody on it grew a people row');
  // A reference the display map does not name still renders — worse to read,
  // never wrong. A blank chip would claim the card names nobody.
  const unnamed = [...root.querySelectorAll('.b-card-person')].find(e => e.textContent.includes('user:z'));
  if (!unnamed) fail('an unresolved reference rendered nothing at all');
  else {
    unnamed.click();
    if (window.__pwned_person) fail('a person chip escaped its onclick and executed — the reference is being interpolated into JS');
  }
  // Clicking a person filters the board to them, and the filter reaches the
  // API — that is the whole reason people are resolved rather than free text.
  let lastPeopleUrl = '';
  const _fetchP = window.fetch;
  window.fetch = async (url, opts) => { if (String(url).includes('/cards')) lastPeopleUrl = String(url); return _fetchP(url, opts); };
  window.Boards.filterPerson('user:jon');
  await new Promise(r => setTimeout(r, 30));
  if (!lastPeopleUrl.includes('person=user%3Ajon') && !lastPeopleUrl.includes('person=user:jon'))
    fail(`person filter did not reach the API: ${lastPeopleUrl}`);
  // …and the filter chip names them rather than showing the reference.
  const chipText = window.document.getElementById('b-filter-chips').textContent;
  if (!chipText.includes('Jon Smith')) fail(`filter chip shows a reference rather than a name: ${chipText}`);
  window.Boards.filterPerson('');
  await new Promise(r => setTimeout(r, 30));
  window.fetch = _fetch;

  // ── People fields: @-mention pickers, not multi-selects ────────────────────
  // A <select multiple> works with three accounts and fails with thirty, and
  // it fails badly rather than gradually: one misclick silently replaces the
  // whole selection. All three fields are now the same chip-and-suggestions
  // control.
  const chipsOf = (id) => [...(window.document.getElementById(id + '-chips')
    ?.querySelectorAll('.b-person-chip') || [])];
  const typeInField = async (id, text) => {
    const el = window.document.getElementById(id);
    el.value = text;
    el.dispatchEvent(new window.Event('input'));
    await new Promise(r => setTimeout(r, 20));
    return [...(window.document.getElementById(id + '-results')
      ?.querySelectorAll('.b-ta-row') || [])];
  };
  const keyIn = (id, k) => {
    const e = new window.KeyboardEvent('keydown', { key: k, bubbles: true, cancelable: true });
    window.document.getElementById(id).dispatchEvent(e);
    return e;
  };

  // All three fields on both modals, so naming a person is ONE gesture rather
  // than three different controls.
  for (const id of ['b-cm-reporter', 'b-cm-assignee', 'b-cm-participants',
                    'b-ec-reporter', 'b-ec-assignee', 'b-ec-participants']) {
    const el = window.document.getElementById(id);
    if (!el) fail(`modal is missing #${id}`);
    else if (el.tagName === 'SELECT') fail(`#${id} is still a <select>`);
    if (!window.document.getElementById(id + '-results')) fail(`#${id} has no suggestion list`);
  }

  window.Boards.openCardModal('c1');
  {
    // The form says the reporter defaults to you, so it shows you before Add
    // card rather than after it. `me` is the server's answer.
    const pre = chipsOf('b-cm-reporter');
    if (pre.length !== 1 || !pre[0].textContent.includes('Jon Smith'))
      fail(`a new card's reporter is not pre-filled with the viewer: ${pre.map(c => c.textContent)}`);
    // Directory-backed: what the picker offers is what the store will accept.
    const rows = await typeInField('b-cm-reporter', 'jo');
    const text = rows.map(r => r.textContent).join('|');
    if (!text.includes('Jon Smith')) fail(`the people picker is not directory-backed: ${text}`);
    // Agents are people here, and are marked as agents.
    const agentRows = await typeInField('b-cm-reporter', 'dispatch');
    if (!agentRows.length) fail('an agent account is not offered');
    else if (!/agent/i.test(agentRows[0].textContent))
      fail(`an agent is offered without being marked as one: ${agentRows[0].textContent}`);
    // NEVER INVENT. A name matching nobody offers nothing and adds nothing —
    // the whole point of resolved people is that a card cannot name somebody
    // who is not there.
    const none = await typeInField('b-cm-reporter', 'nobody-by-that-name');
    if (none.length) fail('the picker offered something for a name matching nobody');
    keyIn('b-cm-reporter', 'Enter');
    const after = chipsOf('b-cm-reporter');
    if (after.length !== 1 || !after[0].textContent.includes('Jon Smith'))
      fail('Enter on an unmatched name changed the reporter');
    // Nothing is invented on a NEW card either: the reporter shown is the
    // server's `me`, and an assignee must not be guessed at all.
    if (chipsOf('b-cm-assignee').length) fail('a new card arrives pre-assigned');
  }
  window.Boards.closeModal('b-card-modal');

  // Opening a card shows the people it already has, or saving an untouched
  // card would silently take everybody off it.
  window.Boards.cardClicked('idea-one');
  await new Promise(r => setTimeout(r, 30));
  {
    const rep = chipsOf('b-ec-reporter');
    if (rep.length !== 1 || !rep[0].textContent.includes('Jon Smith'))
      fail(`the edit modal did not show the card's reporter: ${rep.map(c => c.textContent)}`);
    if (!chipsOf('b-ec-participants').some(c => c.textContent.includes('Dispatch routine')))
      fail("the edit modal did not show the card's participants");
    if (!chipsOf('b-ec-assignee').some(c => c.textContent.includes('Dispatch routine')))
      fail("the edit modal did not show the card's assignee");

    // Picking replaces on a single-value field — a reporter is one account and
    // the field must not be able to say otherwise even for a moment.
    const rows = await typeInField('b-ec-reporter', 'jan');
    if (!rows.length) fail('the reporter picker found nobody for "jan"');
    else {
      rows[0].dispatchEvent(new window.MouseEvent('mousedown', { bubbles: true, cancelable: true }));
      const now = chipsOf('b-ec-reporter');
      if (now.length !== 1) fail(`picking a second reporter left ${now.length} chips`);
      else if (!now[0].textContent.includes('Jan')) fail('picking a reporter did not replace the old one');
    }
    // …and appends on the multi-value one.
    const pRows = await typeInField('b-ec-participants', 'jan');
    pRows[0]?.dispatchEvent(new window.MouseEvent('mousedown', { bubbles: true, cancelable: true }));
    if (chipsOf('b-ec-participants').length !== 2)
      fail('picking a second participant replaced the first');
    // Somebody already on the card is shown as such rather than offered again.
    const again = await typeInField('b-ec-participants', 'jan');
    if (again.length && !again[0].classList.contains('linked'))
      fail('a person already on the card was offered as if they were not');
    // Backspace on an empty input takes the last chip off — the one thing
    // people try without being told.
    const before = chipsOf('b-ec-participants').length;
    window.document.getElementById('b-ec-participants').value = '';
    keyIn('b-ec-participants', 'Backspace');
    if (chipsOf('b-ec-participants').length !== before - 1)
      fail('Backspace on an empty people field did not remove the last chip');
    // And the × removes a named one.
    const chip = chipsOf('b-ec-participants')[0];
    chip.querySelector('.x').dispatchEvent(new window.MouseEvent('mousedown', { bubbles: true, cancelable: true }));
    if (chipsOf('b-ec-participants').length !== before - 2)
      fail('clicking × did not remove the chip');
    // Escape closes the suggestions and stops there, or the first dismissal
    // would shut the card as well.
    await typeInField('b-ec-reporter', 'jo');
    keyIn('b-ec-reporter', 'Escape');
    if (!window.document.getElementById('b-ec-reporter-results').hidden)
      fail('Escape did not close the people suggestions');
    if (!window.document.getElementById('b-edit-card-modal').classList.contains('open'))
      fail('Escape on the people suggestions closed the card modal too');
  }
  window.Boards.closeModal('b-edit-card-modal');

  // SOMEBODY WHO HAS LEFT. A card keeps naming them, so the chip has to render
  // and an untouched save has to write them back. The old control handled this
  // by re-adding a held reference as an option; holding references directly
  // makes it true by construction, and this is the test that says so either
  // way.
  {
    cards.cards.push({
      slug: 'ghost-card', title: 'Ghost Card', body: '', column_id: 'c1', position: 9,
      source: 'ui', tags: [], labels: [], repo: '', number: 99,
      reporter: 'user:departed', participants: ['user:departed'],
    });
    await window.Boards.refresh();
    await new Promise(r => setTimeout(r, 20));
    window.Boards.cardClicked('ghost-card');
    await new Promise(r => setTimeout(r, 40));
    const rep = chipsOf('b-ec-reporter');
    if (rep.length !== 1) fail(`a departed reporter did not render: ${rep.length} chips`);
    else if (!rep[0].textContent.includes('user:departed'))
      fail(`a departed reporter rendered as something else: ${rep[0].textContent}`);
    // The save must carry them back unchanged rather than dropping them.
    const posts = [];
    const _f = window.fetch;
    window.fetch = async (u, o) => {
      if (o && o.method === 'PATCH') posts.push(JSON.parse(o.body || '{}'));
      return _f(u, o);
    };
    await window.Boards.saveCard();
    await new Promise(r => setTimeout(r, 40));
    window.fetch = _f;
    if (!posts.length) fail('saving the card sent nothing');
    else if (posts[0].reporter !== 'user:departed')
      fail(`an untouched save dropped a departed person: ${JSON.stringify(posts[0].reporter)}`);
    // Saving no longer closes the card — the detail view auto-saves and stays
    // put — and refresh() holds back while a modal is open, so the teardown has
    // to close it or every later assertion reads a board frozen at this point.
    window.Boards.closeModal('b-edit-card-modal');
    cards.cards = cards.cards.filter(c => c.slug !== 'ghost-card');
    await window.Boards.refresh();
    await new Promise(r => setTimeout(r, 20));
  }

  // 2a-0000000) AUTO-SAVE. The detail view has no Save button: an edit is saved
  // as it is made. A Save button on a card is a trap — it looks optional, so
  // people close the card and lose the edit.
  {
    const $id = (id) => window.document.getElementById(id);
    const patches = [];
    const posts = [];
    const _f = window.fetch;
    window.fetch = async (u, o) => {
      const m = (o && o.method) || 'GET';
      if (m === 'PATCH' && /\/cards\/[^/?]+$/.test(String(u)))
        patches.push({ url: String(u), body: JSON.parse((o && o.body) || '{}') });
      if (m === 'POST' && /\/comments$/.test(String(u)))
        posts.push({ url: String(u), body: JSON.parse((o && o.body) || '{}') });
      return _f(u, o);
    };
    const type = (id, text) => {
      const el = $id(id);
      el.value = text;
      el.dispatchEvent(new window.Event('input', { bubbles: true }));
    };
    const settle = () => new Promise(r => setTimeout(r, 950));

    try {
      // The button is GONE, not merely ignored. Leaving it would say the edit
      // is optional, which is the confusion this removes.
      if ($id('b-ec-submit')) fail('the detail view still has a Save button');

      // OPENING a card must not save it. fillCardModal writes every field, and
      // without the guard just looking at a card would PATCH it — and would
      // stamp a card the operator never touched as edited.
      comments = [];
      patches.length = 0;
      window.Boards.cardClicked('idea-one');
      await settle();
      if (patches.length) fail(`opening a card saved it: ${JSON.stringify(patches[0].body)}`);

      // Typing saves, without a button.
      type('b-ec-title', 'Idea One, revised');
      if (!/saving/i.test($id('b-ec-status').textContent))
        fail(`no feedback while saving: "${$id('b-ec-status').textContent}"`);
      await settle();
      if (!patches.length) fail('typing in the detail view never saved');
      else {
        if (patches[0].body.title !== 'Idea One, revised')
          fail(`auto-save sent the wrong title: ${JSON.stringify(patches[0].body.title)}`);
        if (!patches[0].url.endsWith('/cards/idea-one'))
          fail(`auto-save went to ${patches[0].url}`);
      }
      // With no Save button left, the status line is the ONLY thing saying the
      // edit landed.
      if (!/saved/i.test($id('b-ec-status').textContent))
        fail(`nothing said the edit was saved: "${$id('b-ec-status').textContent}"`);

      // A BURST is one save, not one per keystroke.
      patches.length = 0;
      // Spaced deliberately: three .value writes in one synchronous turn are
      // coalesced by ANY debounce including none, so they would prove nothing.
      // 120ms apart is well inside the real debounce and well outside zero.
      type('b-ec-body', 'a');
      await new Promise(r => setTimeout(r, 120));
      type('b-ec-body', 'ab');
      await new Promise(r => setTimeout(r, 120));
      type('b-ec-body', 'abc');
      await settle();
      if (patches.length !== 1) fail(`a burst of typing sent ${patches.length} saves, not 1`);
      else if (patches[0].body.body !== 'abc')
        fail(`the debounced save sent a stale body: ${JSON.stringify(patches[0].body.body)}`);

      // AUTO-SAVE MUST NOT TOUCH THE COMMENT DRAFT (#108). A comment is a draft
      // until its own button posts it — publishing a half-typed one because the
      // operator paused is exactly the accident this could introduce.
      patches.length = 0; posts.length = 0;
      const cta = $id('b-ec-comment-body');
      cta.value = 'half a thought';
      cta.dispatchEvent(new window.Event('input', { bubbles: true }));
      await settle();
      if (posts.length) fail(`auto-save posted an unsent comment: ${JSON.stringify(posts[0].body)}`);
      if (patches.some(p => JSON.stringify(p.body).includes('half a thought')))
        fail('the comment draft was sent as part of the card');
      // And typing a comment must not save the CARD either. The card's fields
      // have not changed, so a PATCH here is a write nobody asked for on every
      // keystroke of a comment — and it would stamp the card as edited.
      if (patches.length)
        fail(`typing a comment triggered ${patches.length} card save(s)`);
      if (cta.value !== 'half a thought') fail('auto-save cleared the comment composer');

      // ESCAPE closes the detail view, and because it is now the ordinary way
      // out rather than a discard, it must FIRE a save still waiting out its
      // debounce — otherwise the last thing typed is the one thing lost.
      patches.length = 0;
      type('b-ec-title', 'Escaped before the debounce');
      window.document.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
      await new Promise(r => setTimeout(r, 60));
      if ($id('b-edit-card-modal').classList.contains('open'))
        fail('Escape did not close the detail view');
      if (!patches.length) fail('Escape dropped an edit that had not yet debounced');
      else if (patches[0].body.title !== 'Escaped before the debounce')
        fail(`Escape flushed the wrong text: ${JSON.stringify(patches[0].body.title)}`);

      // SWITCHING CARDS while a save is still pending must land on the card the
      // text was typed into. Firing after the refill would write one card's
      // fields onto another — silent, and unrecoverable without a backup.
      patches.length = 0;
      window.Boards.cardClicked('idea-one');
      await new Promise(r => setTimeout(r, 40));
      patches.length = 0;
      type('b-ec-title', 'belongs to idea-one');
      window.Boards.cardClicked('do-thing');
      await settle();
      const stray = patches.find(p => p.url.endsWith('/cards/do-thing') &&
                                      p.body.title === 'belongs to idea-one');
      if (stray) fail("a pending save landed one card's title on another card");
      const landed = patches.find(p => p.url.endsWith('/cards/idea-one'));
      if (!landed) fail('switching cards dropped the pending save entirely');
      else if (landed.body.title !== 'belongs to idea-one')
        fail(`the flushed save carried the wrong title: ${JSON.stringify(landed.body.title)}`);

      window.Boards.closeModal('b-edit-card-modal');
      await new Promise(r => setTimeout(r, 40));
    } finally {
      window.fetch = _f;
      window.localStorage.clear();
      comments = [];
    }
  }

  // 2a-0000000000) ATTRIBUTE SEARCH. The search bar takes `label: bug`,
  // `repo: elefant`, `assignee: jon` alongside ordinary words, so a board can be
  // sliced without reaching for a chip that may not be on screen.
  {
    const $id = (id) => window.document.getElementById(id);
    let lastUrl = '';
    const _f = window.fetch;
    window.fetch = async (u, o) => {
      if (/\/cards\?/.test(String(u)) || /\/cards$/.test(String(u))) lastUrl = String(u);
      return _f(u, o);
    };
    const params = () => new URL(lastUrl, 'http://localhost/').searchParams;
    const searchFor = async (text) => {
      const box = $id('b-search');
      box.value = text;
      window.Boards.onSearch();
      await new Promise(r => setTimeout(r, 320));
    };
    try {
      // Start from a board with no CHIP filters on it. Earlier blocks leave
      // some set, and a chip still covers every key the query is silent about —
      // which is the designed behaviour, and would make these assertions read a
      // filter they did not ask for.
      for (const fn of ['filterLabel', 'filterRepo', 'filterPerson'])
        window.Boards[fn]('');
      await new Promise(r => setTimeout(r, 40));

      // Each key reaches the API as its own query param, not as free text.
      await searchFor('label: bug');
      if (params().get('label') !== 'bug')
        fail(`label: did not reach the API as a filter: ${lastUrl}`);
      if ((params().get('search') || '') !== '')
        fail(`the filter token was also sent as free text: ${params().get('search')}`);

      await searchFor('assignee: jon');
      if (params().get('assignee') !== 'jon')
        fail(`assignee: did not reach the API: ${lastUrl}`);

      await searchFor('repo: elefant');
      if (params().get('repo') !== 'elefant')
        fail(`repo: did not reach the API: ${lastUrl}`);

      // Filters and free text COMBINE — the whole point is slicing a board while
      // still searching inside the slice.
      await searchFor('label: bug cache');
      if (params().get('label') !== 'bug') fail(`mixed query lost the filter: ${lastUrl}`);
      if (params().get('search') !== 'cache')
        fail(`mixed query lost the free text: ${params().get('search')}`);

      // Several keys at once.
      await searchFor('label: bug assignee: jon repo: elefant');
      if (params().get('label') !== 'bug' || params().get('assignee') !== 'jon' ||
          params().get('repo') !== 'elefant')
        fail(`a multi-key query did not reach the API intact: ${lastUrl}`);

      // No space after the colon works too — it is what half the world types.
      await searchFor('label:bug');
      if (params().get('label') !== 'bug') fail(`label:bug (no space) did not parse: ${lastUrl}`);

      // A QUOTED value carries spaces. Unquoted takes one word, so
      // `label: needs design` filters on "needs" and searches for "design"
      // rather than silently filtering on something nobody typed.
      await searchFor('label: "needs design"');
      if (params().get('label') !== 'needs design')
        fail(`a quoted value did not survive: ${params().get('label')}`);
      await searchFor('label: needs design');
      if (params().get('label') !== 'needs') fail(`unquoted value took more than one word: ${params().get('label')}`);
      if (params().get('search') !== 'design') fail(`the rest was not left as free text: ${params().get('search')}`);

      // An UNKNOWN key is free text, not a dropped filter. "ratio: 3" is a
      // thing somebody might search for, and swallowing it would return a board
      // filtered on nothing with no sign why.
      await searchFor('ratio: 3');
      if (!/ratio/.test(params().get('search') || ''))
        fail(`an unknown key was swallowed instead of searched for: ${params().get('search')}`);

      // A key with NO value is somebody mid-type, not a filter on the empty
      // string — which matches nothing and looks broken while they are typing.
      await searchFor('label:');
      if (params().get('label')) fail(`a bare key filtered on empty: ${lastUrl}`);
      // Explicitly-empty is the same: `label:""` must not become a filter, and
      // must not leave an empty chip sitting in the filter bar claiming one.
      await searchFor('label:""');
      if (params().get('label')) fail(`an explicitly empty value filtered: ${lastUrl}`);
      if ([...$id('b-filter-chips').querySelectorAll('[data-unfilter]')].length)
        fail(`an empty value rendered a filter chip: "${$id('b-filter-chips').textContent}"`);

      // The applied filters are VISIBLE. A typed filter that silently narrowed
      // the board while the filter bar claimed nothing was applied would be the
      // worst version of this feature.
      await searchFor('label: bug assignee: jon');
      const chipText = $id('b-filter-chips').textContent;
      if (!/label:\s*bug/.test(chipText)) fail(`no chip for the typed label filter: "${chipText}"`);
      if (!/assignee:\s*jon/.test(chipText)) fail(`no chip for the typed assignee filter: "${chipText}"`);

      // And removing one rewrites the QUERY, because that is where it came from
      // — clearing only the applied filter would leave the text in the box and
      // the next keystroke would put it straight back.
      const x = [...$id('b-filter-chips').querySelectorAll('[data-unfilter]')]
        .find(e => e.dataset.unfilter === 'label');
      if (!x) fail('a typed filter chip offers no way to remove it');
      else {
        x.click();
        await new Promise(r => setTimeout(r, 60));
        if (/label/.test($id('b-search').value))
          fail(`removing the chip left the token in the box: "${$id('b-search').value}"`);
        if (!/assignee/.test($id('b-search').value))
          fail(`removing one filter took the others with it: "${$id('b-search').value}"`);
      }

      // Case-insensitive keys, because nobody remembers which.
      await searchFor('Label: bug');
      if (params().get('label') !== 'bug') fail(`a capitalised key did not parse: ${lastUrl}`);

      // A plain search is untouched by any of this.
      await searchFor('just words');
      if (params().get('search') !== 'just words')
        fail(`an ordinary search was mangled: ${params().get('search')}`);
      for (const k of ['label', 'repo', 'assignee']) {
        if (params().get(k)) fail(`an ordinary search set a ${k} filter: ${lastUrl}`);
      }

      window.Boards.clearSearch();
      await new Promise(r => setTimeout(r, 40));
    } finally {
      window.fetch = _f;
    }
  }

  // 2a-000000000) THE REPO LINK. A card names a repo; clicking it should get you
  // to the repo. The chip on the tile filters the board (a different question),
  // so the link lives on the detail view's field.
  {
    const $id = (id) => window.document.getElementById(id);
    const setRepo = (v) => {
      const el = $id('b-ec-repo');
      el.value = v;
      el.dispatchEvent(new window.Event('input', { bubbles: true }));
    };
    comments = [];
    window.Boards.cardClicked('idea-one');
    await new Promise(r => setTimeout(r, 40));
    const link = $id('b-ec-repo-link');
    if (!link) fail('the repo field offers no link');
    else {
      if (link.hidden) fail('a card with a repo shows no link to it');
      if (link.getAttribute('href') !== 'https://github.com/zennthic/elefant')
        fail(`the repo link points at ${link.getAttribute('href')}`);
      // New tab, and without handing the opener to the page it opens.
      if (link.getAttribute('target') !== '_blank') fail('the repo link does not open a new tab');
      const rel = link.getAttribute('rel') || '';
      if (!/noopener/.test(rel)) fail(`the repo link does not set noopener: "${rel}"`);

      // It FOLLOWS the field, so the link is right at the moment the repo is
      // rather than one save behind it.
      setRepo('acme/widgets');
      if (link.getAttribute('href') !== 'https://github.com/acme/widgets')
        fail(`the link did not follow the field: ${link.getAttribute('href')}`);

      // NO REPO, NO LINK — a link to https://github.com/ is worse than none.
      setRepo('');
      if (!link.hidden) fail('an empty repo still rendered a link');
      if (link.getAttribute('href')) fail('an empty repo left a stale href behind');

      // A VALUE THAT IS NOT A REPO GETS NO LINK, rather than a guess. This is
      // the hostile case: the UI renders whatever the API hands it, and a
      // javascript: value rendered as an anchor would be the whole bug.
      // `../..` is the one that gets PAST the pattern — dots are legal in a repo
      // name, so only the dot-segment check stops it walking back up to the
      // bare host. Without it the link goes to github.com root, not the repo.
      for (const bad of ['javascript:alert(1)', 'https://evil.example/zennthic/elefant',
                         'zennthic', 'zennthic/elefant/extra', '../../etc/passwd',
                         '../..', './.', 'zennthic/..',
                         "y'); window.__pwned_repo = 1; ('"]) {
        setRepo(bad);
        if (!link.hidden || link.getAttribute('href'))
          fail(`"${bad}" was turned into a link: ${link.getAttribute('href')}`);
      }

      // The value is RENDERED, not parsed — the label is text, not markup.
      setRepo('zennthic/elefant');
      if (link.querySelector('*')) fail('the repo link built markup out of the value');
    }
    window.Boards.closeModal('b-edit-card-modal');
    await new Promise(r => setTimeout(r, 40));
    comments = [];
  }

  // 2a-00000000) THE COLUMN PICKER. A drag needs a pointer, a visible target
  // column and a board you can see — none of which are true with the card open
  // in front of you. The dropdown is the accessible half of the same move.
  {
    const $id = (id) => window.document.getElementById(id);
    const moves = [];
    const _f = window.fetch;
    window.fetch = async (u, o) => {
      if (o && o.method === 'PATCH' && /\/move$/.test(String(u)))
        moves.push({ url: String(u), body: JSON.parse((o && o.body) || '{}') });
      return _f(u, o);
    };
    try {
      comments = [];
      window.Boards.cardClicked('idea-one');
      await new Promise(r => setTimeout(r, 40));
      const sel = $id('b-ec-column');
      if (!sel) fail('the detail view has no column picker');
      else {
        // It SHOWS where the card is — that is half the ask, and the half a
        // move button alone would not answer.
        const names = [...sel.options].map(o => o.textContent);
        if (names.length !== cols.columns.length)
          fail(`the picker offers ${names.length} columns, the board has ${cols.columns.length}`);
        if (sel.value !== 'c1')
          fail(`the picker does not show the card's own column: ${sel.value}`);

        // Changing it moves the card, through the SAME endpoint a drag uses —
        // one way for a card to change column, one event on the stream.
        moves.length = 0;
        sel.value = 'c2';
        sel.dispatchEvent(new window.Event('change', { bubbles: true }));
        await new Promise(r => setTimeout(r, 60));
        if (!moves.length) fail('changing the column picker moved nothing');
        else {
          if (!moves[0].url.endsWith('/cards/idea-one/move'))
            fail(`the move went to ${moves[0].url}`);
          if (moves[0].body.column !== 'c2')
            fail(`the move named the wrong column: ${JSON.stringify(moves[0].body.column)}`);
        }
        // A column change is NOT a card field — it must not ride along with
        // auto-save, or the card gets PATCHed for a move it already sent.
        if (moves.some(m => 'title' in (m.body || {})))
          fail('the move carried card fields with it');

        // OPENING a card must not move it. The picker is populated on fill, and
        // an unguarded change handler would fire on every open — moving every
        // card somebody merely looked at.
        moves.length = 0;
        window.Boards.cardClicked('do-thing');
        await new Promise(r => setTimeout(r, 60));
        if (moves.length)
          fail(`opening a card moved it: ${JSON.stringify(moves[0].body)}`);
        if ($id('b-ec-column').value !== 'c2')
          fail(`the picker does not follow the card it opened: ${$id('b-ec-column').value}`);
      }
      window.Boards.closeModal('b-edit-card-modal');
      await new Promise(r => setTimeout(r, 40));
    } finally {
      window.fetch = _f;
      comments = [];
    }
  }

  // 2b) Live refresh: a card added out-of-band (e.g. an MCP "note to self")
  // must surface on the next refresh() WITHOUT a reload — and refresh() must
  // not throw (it referenced an out-of-scope `root`, which broke polling).
  cards.cards.push({ slug: 'from-mcp', title: 'From MCP', body: '', column_id: 'c1', position: 2, source: 'claude', tags: ['note-to-self'], labels: [], repo: '' });
  await window.Boards.refresh();
  await new Promise(r => setTimeout(r, 20));
  const titles2 = [...root.querySelectorAll('.b-card-title')].map(e => e.textContent);
  if (!titles2.includes('From MCP')) fail('refresh() did not surface the out-of-band card');

  // 2c) A label added out-of-band changes nothing but the label array, so the
  // poll signature has to include labels or the board never re-renders.
  cards.cards[1].labels = ['enhancement'];
  await window.Boards.refresh();
  await new Promise(r => setTimeout(r, 20));
  const labels2 = [...root.querySelectorAll('.b-card-label')].map(e => e.textContent);
  if (!labels2.includes('enhancement')) fail('refresh() did not surface a label-only change');

  // 2d) Same for the repo: an agent calling card_repo changes nothing but that
  // one scalar, so it has to be in the poll signature or an open board never
  // shows the repo it was just given.
  cards.cards[3].repo = 'acme/widgets';
  await window.Boards.refresh();
  await new Promise(r => setTimeout(r, 20));
  const repos2 = [...root.querySelectorAll('.b-card-repo')].map(e => e.textContent);
  if (!repos2.includes('acme/widgets')) fail('refresh() did not surface a repo-only change');

  // 2d-ii) Same again for people: an agent calling card_participant changes
  // nothing but that one array, so it has to be in the poll signature or an
  // open board never shows who was just put on the card.
  cards.cards[2].participants = ['user:jan'];
  cards.people['user:jan'] = { ref: 'user:jan', kind: 'user', display: 'Jan Novak', handle: 'jan', active: true, known: true };
  await window.Boards.refresh();
  await new Promise(r => setTimeout(r, 20));
  const people2 = [...root.querySelectorAll('.b-card-person')].map(e => e.textContent);
  if (!people2.some(t => t.includes('Jan Novak'))) fail('refresh() did not surface a participant-only change');

  // 2e) Touch drag must be ARMED, not inferred. On a phone a column taller than
  // the screen could not be scrolled: the finger drag was read as a card move,
  // so scrolling reordered cards, and a sideways slip changed a card's column —
  // which is a stage change. Scrolling is now the default reading of a touch
  // drag and moving needs a press-and-hold, so every sortable the board creates
  // has to carry the arming config. None of this is visible in the DOM; the
  // options object handed to Sortable.create is the only place to check it.
  const cardLists = sortables.filter(s => s.el && s.el.classList && s.el.classList.contains('b-column-cards'));
  if (!cardLists.length) fail('no sortable was created for the card lists');
  for (const { opts } of sortables) {
    if (!(opts.delay > 0)) fail('a sortable has no hold delay — a touch drag still starts on any movement');
    // Without delayOnTouchOnly the same delay lands on the mouse, and every
    // desktop drag gains a dead pause. The fix is a touch-input fix only.
    if (opts.delayOnTouchOnly !== true) fail('the hold delay is not limited to touch — desktop drag would be delayed too');
    // A finger never holds perfectly still. With no tolerance the arming is
    // cancelled by the wobble and the gesture just appears to fail.
    if (!(opts.touchStartThreshold > 0)) fail('no movement tolerance during the hold — a finger wobble cancels the arm');
    // Sortable fires `choose` only once the delay elapses, so it is the arming
    // moment and the one chance to tell the user the hold worked.
    if (typeof opts.onChoose !== 'function') fail('nothing signals that the drag armed — a successful hold is indistinguishable from a failed one');
    // Once armed, the target may still be off-screen in the same tall stack.
    if (opts.scroll !== true) fail('an armed drag cannot scroll the stack towards an off-screen target');
  }
  // The arming feedback must survive a browser with no vibration API — most
  // desktops, and iOS Safari. Throwing here would break every armed drag.
  const noHaptics = cardLists[0].opts.onChoose;
  const savedVibrate = window.navigator.vibrate;
  try {
    delete window.navigator.vibrate;
    noHaptics({});
  } catch (e) { fail('arming feedback threw where the vibration API is absent: ' + e.message); }
  finally { if (savedVibrate) window.navigator.vibrate = savedVibrate; }

  // 2g) The card number is the immutable handle a human quotes ("look at #7").
  // It renders top-right, and a card that predates the backfill has no number
  // and must render no chip at all rather than "#null" / "#undefined".
  const numberEls = [...root.querySelectorAll('.b-card-number')].map(e => e.textContent);
  if (numberEls.length !== 1) fail(`expected 1 number chip, got ${numberEls.length}: ${numberEls}`);
  if (numberEls[0] !== '#7') fail(`number chip wrong: ${numberEls[0]}`);
  // It must sit inside the card head, which is what puts it beside the title
  // and (with margin-left:auto) in the upper right corner.
  if (!root.querySelector('.b-card-head .b-card-number')) fail('number chip is not in the card head — it will not land top-right');

  // Clicking it copies a permalink rather than opening the card, and the click
  // must not bubble into the card's own onclick.
  let copied = null;
  window.navigator.clipboard = { writeText: async (t) => { copied = t; } };
  Object.defineProperty(window, 'isSecureContext', { value: true, configurable: true });
  window.document.getElementById('b-edit-card-modal').classList.remove('open');
  root.querySelector('.b-card-number').click();
  await new Promise(r => setTimeout(r, 20));
  if (copied === null) fail('clicking the number copied nothing');
  if (!/#boards\/idea-one$/.test(copied || '')) fail(`permalink is not the board deep-link: ${copied}`);
  // The permalink has to be an absolute URL — a bare "#boards/..." fragment
  // pasted into a chat message links nowhere.
  if (!/^https?:\/\//.test(copied || '')) fail(`permalink is not absolute: ${copied}`);
  if (window.document.getElementById('b-edit-card-modal').classList.contains('open'))
    fail('clicking the number opened the card — the click escaped into the card onclick');

  // A card with no number at all renders no chip (the other two fixtures carry
  // none), which the count above already pins. Guard the null case explicitly
  // too, since `0` and `null` behave differently under a loose check.
  if (root.innerHTML.includes('#null') || root.innerHTML.includes('#undefined'))
    fail('a card with no number rendered a placeholder chip');

  // 2g-i) UNDER EVALUATION. An automated reviewer holds the card's lock, and the
  // board has to say so clearly enough that a human does not start working a
  // card it already has in hand -- which is the entire point of the feature.
  {
    const card = s => [...root.querySelectorAll('.b-card')].find(
      e => e.dataset.slug === s);
    const live = card('locked');
    const stale = card('stale-locked');
    const plain = card('do-thing');
    if (!live || !stale || !plain) fail('the lock fixtures did not render');

    // The design reuses the EXISTING status slot rather than adding a new
    // element, so the dot in the card head is what must change.
    const dot = el => el && el.querySelector('.b-card-head .b-src-dot');
    if (!dot(live) || !dot(live).classList.contains('b-src-eval'))
      fail('a locked card does not show the under-evaluation treatment in the status slot');
    if (!dot(stale) || !dot(stale).classList.contains('b-src-eval'))
      fail('a stale-locked card does not show the under-evaluation treatment');

    // And the unlocked case is UNCHANGED. The brief called the slot "purely
    // cosmetic, wired to nothing"; it is actually wired to `source`, and
    // repurposing it outright would have deleted that signal. This is the
    // assertion that says it did not.
    if (!dot(plain) || !dot(plain).classList.contains('b-src-agent'))
      fail('an unlocked card lost its source dot — the slot was repurposed rather than shared');
    if (dot(plain).classList.contains('b-src-eval'))
      fail('an unlocked card shows the under-evaluation treatment');
    const uiDot = dot(card('idea-one'));
    if (!uiDot || !uiDot.classList.contains('b-src-ui'))
      fail('a person-added card lost its source dot');

    // A 9px dot is not something you read while scanning a column, and a hover
    // title does not exist on a touch screen, so the state is also a word.
    const chip = el => el && el.querySelector('.b-card-eval');
    if (!chip(live)) fail('a locked card has no under-evaluation chip — the dot alone is not unambiguous');
    if (!/under evaluation/i.test(chip(live).textContent))
      fail(`the chip does not say what the state is: ${chip(live).textContent}`);
    if (chip(plain)) fail('an unlocked card renders an under-evaluation chip');

    // A stale lock reads differently on purpose: the honest interpretation is
    // "that run died", not "busy".
    if (!stale.querySelector('.b-eval-stale'))
      fail('a stale lock is not distinguished from a live one');
    if (live.querySelector('.b-eval-stale'))
      fail('a live lock is marked stale');

    // The card edge, which is what catches the eye in a column.
    if (!live.classList.contains('under-eval')) fail('a locked card has no under-evaluation edge');
    if (plain.classList.contains('under-eval')) fail('an unlocked card has the under-evaluation edge');

    // The holder is named. "Locked" on its own leaves a human nothing to do;
    // a run id is something they can go and look up.
    const title = live.querySelector('.b-card-eval').getAttribute('title') || '';
    if (!/under evaluation by/i.test(title)) fail(`the indicator does not name the holder: ${title}`);
    if (!/leave it alone/i.test(title)) fail(`the indicator does not say what to do: ${title}`);
    const staleTitle = stale.querySelector('.b-card-eval').getAttribute('title') || '';
    if (!/stale/i.test(staleTitle)) fail(`a stale lock's title does not say so: ${staleTitle}`);
    if (!/run-dead/.test(staleTitle)) fail(`a stale lock does not name its holder: ${staleTitle}`);

    // `run_id` arrives from an external reviewer over the API. Like every other
    // agent-writable value the board renders, a hostile one must stay inert.
    if (window.__pwned_run) fail('a hostile run_id executed — the lock holder is not escaped');

    // The dot's treatment is a STYLESHEET feature, so the rules are what can be
    // checked from jsdom, which does no layout.
    const evalDot = (css.match(/\.b-src-eval\s*\{[^}]*\}/) || [''])[0];
    if (!/background:/.test(evalDot)) fail('the under-evaluation dot has no colour of its own');
    // Colour alone fails a colour-blind reader at 9px. The ring is the second
    // channel.
    if (!/box-shadow:/.test(evalDot)) fail('the under-evaluation dot is distinguished by hue alone');
    // Not the source dot's green and not the blocked red: a locked card is
    // neither idle nor broken, and must be told apart from both.
    if (/--success|--danger/.test(evalDot))
      fail(`the under-evaluation dot reuses another state's colour: ${evalDot}`);
    if (!/var\(--warning\)/.test(evalDot))
      fail(`the under-evaluation dot is not the warning amber: ${evalDot}`);
    // The pulse is what says "right now", but motion is the one signal that is
    // optional, so it must be droppable without losing the state.
    if (!/animation:/.test(evalDot)) fail('nothing says the evaluation is happening now');
    const reduced = css.match(/@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{([\s\S]*?)\n\}/g) || [];
    if (!reduced.some(b => /b-src-eval[^}]*animation:\s*none/.test(b)))
      fail('the under-evaluation pulse ignores prefers-reduced-motion');
    // Both modes, in every palette. The board used to carry a private palette,
    // where a token defined only in the dark block was invisible on white. Its
    // colours now come from the generated appearance.css, so the check is that
    // EVERY palette block there — light and dark alike — defines the warning.
    const appearance = readFileSync(new URL('../static/appearance.css', import.meta.url), 'utf8');
    const blocks = appearance.match(/[^{}]+\{[^}]*\}/g) || [];
    const paletteBlocks = blocks.filter(b => /data-palette=/.test(b.split('{')[0]));
    if (!paletteBlocks.length) fail('appearance.css has no palette blocks');
    for (const mode of ['light', 'dark']) {
      const ofMode = paletteBlocks.filter(b => b.split('{')[0].includes(`data-theme='${mode}'`));
      if (!ofMode.length) fail(`appearance.css has no ${mode} palette blocks`);
      const missing = ofMode.filter(b => !/--warning:\s*#/.test(b));
      if (missing.length) fail(`the under-evaluation colour has no ${mode} value in ${missing.length} palette(s)`);
    }
  }

  // 2g-ii) The OPEN card carries the same handle, top-left. The tile's chip is
  // no help once the card is open and covering it, and the open card is where
  // you actually read one well enough to want to quote or link it.
  {
    const $id = (id) => window.document.getElementById(id);
    window.Boards.cardClicked('idea-one');
    await new Promise(r => setTimeout(r, 40));
    const chip = $id('b-ec-number');
    if (!chip) fail('the open card has no number chip');
    else {
      if (chip.hidden) fail('the open card hides the number of a card that has one');
      if (chip.textContent !== '#7') fail(`open-card number wrong: ${chip.textContent}`);
      // Top-left means first in the modal's heading — the heading is the first
      // thing in the modal, and the chip is the first thing in the heading.
      const h3 = window.document.querySelector('#b-edit-card-modal h3');
      if (!h3 || !h3.contains(chip))
        fail('the open-card number is not in the modal heading — it will not land top-left');
      else if (h3.firstElementChild !== chip)
        fail('the open-card number does not lead the heading — it will not land top-left');

      // And it copies the permalink, the same one the tile's chip copies.
      copied = null;
      chip.click();
      await new Promise(r => setTimeout(r, 20));
      if (copied === null) fail('clicking the open card number copied nothing');
      else if (!/^https?:\/\/.*#boards\/idea-one$/.test(copied))
        fail(`open-card permalink is not the absolute board deep-link: ${copied}`);
    }

    // A card written before the number backfill must show no chip at all — an
    // empty one still reserves space in the heading and invites a click that
    // would copy nothing.
    window.Boards.cardClicked('do-thing');
    await new Promise(r => setTimeout(r, 40));
    if (chip && !chip.hidden)
      fail(`a card with no number still shows a chip in the open card: ${chip.textContent}`);
    window.Boards.closeModal('b-edit-card-modal');
  }

  // 2e-ii) A blocked card. The design is explicit that it does NOT move to a
  // Blocked column — it stays where it is and carries a marker. That makes the
  // marker the only thing separating a card you can start from one you cannot,
  // so its absence is not cosmetic, it is the feature missing.
  const blockedEls = root.querySelectorAll('.b-card-blocked');
  if (blockedEls.length !== 1) fail(`expected 1 blocked marker, got ${blockedEls.length}`);
  if (root.querySelectorAll('.b-card.blocked').length !== 1) fail('the blocked card is not marked as a whole');
  // It has to NAME the blocker: "blocked" alone leaves you no next move.
  const blockerChip = root.querySelector('.b-blocker');
  if (!blockerChip) fail('the blocked marker does not name the blocker');
  else {
    if (!blockerChip.textContent.includes('42')) fail(`blocker chip does not quote the number: ${blockerChip.textContent}`);
    // Same rule as every other chip: a hostile slug in a data attribute, never
    // interpolated into the onclick argument.
    blockerChip.click();
    await new Promise(r => setTimeout(r, 20));
    if (window.__pwned_blocker) fail('a blocker slug escaped its onclick and executed');
    if (blockerChip.dataset.slug !== "b'); window.__pwned_blocker = 1; ('")
      fail('blocker chip lost its value on the way into the data attribute');
  }
  // A card nobody is waiting on grows no marker at all.
  const unblockedCard = [...root.querySelectorAll('.b-card')].find(e => e.dataset.slug === 'do-thing');
  if (unblockedCard?.querySelector('.b-card-blocked')) fail('an unblocked card grew a blocked marker');
  // The edit modal offers the relationship editor, so a human can set what an
  // agent reads.
  if (!window.document.getElementById('b-ec-rel')) fail('the edit-card modal has no relationship editor');

  // 2e-ii-b) The link-type control. This was a native <select>, and a native
  // popup list is drawn by the OS, so it never obeyed #boards-root CSS — for a
  // real user it opened as an overlapping strip of single characters and made
  // linking cards unusable. What is pinned here is therefore the ABSENCE of a
  // native menu, not the styling: the control has to stay inside our own DOM.
  {
    window.Boards.cardClicked('idea-one');
    await new Promise(r => setTimeout(r, 40));
    const kindCtl = window.document.getElementById('b-ec-rel-kind');
    if (!kindCtl) fail('the relationship row has no link-type control');
    else if (kindCtl.tagName === 'SELECT')
      fail('the link type is a native <select> again — its popup is drawn by the OS and cannot be styled');
    else {
      const btns = [...kindCtl.querySelectorAll('button')];
      // The values have to be exactly memory-api's LINK_KINDS. A label reworded
      // into the value is a link that 400s at the server with a readable button.
      const kinds = btns.map(b => b.dataset.kind);
      if (JSON.stringify(kinds) !== JSON.stringify(['related', 'blocks', 'blocked-by']))
        fail(`link kinds are not memory-api's LINK_KINDS: ${JSON.stringify(kinds)}`);
      // Exactly one preselected, and it has to be the harmless one: relating two
      // cards is reversible, declaring a dependency is the deliberate act. The
      // old control defaulted to "is blocked by" purely by list order.
      const sel = btns.filter(b => b.classList.contains('sel'));
      if (sel.length !== 1) fail(`expected exactly 1 preselected link type, got ${sel.length}`);
      else if (sel[0].dataset.kind !== 'related')
        fail(`link type defaults to ${sel[0].dataset.kind}, not related`);
      // And picking another has to move the selection, or the control reads as
      // working while every link is quietly filed under the default.
      const blocks = btns.find(b => b.dataset.kind === 'blocks');
      blocks.click();
      if (!blocks.classList.contains('sel')) fail('clicking a link type did not select it');
      if (blocks.getAttribute('aria-pressed') !== 'true')
        fail('the selected link type does not report aria-pressed');
      if (btns.filter(b => b.classList.contains('sel')).length !== 1)
        fail('selecting a link type left more than one selected');
      // The card input keeps the rest of the row — the old select was sized for
      // its longest label and squeezed it down to nothing. The input sits in a
      // positioned wrapper now (the type-ahead hangs off it), so the wrapper is
      // the flex child that has to grow.
      const wrap = window.document.getElementById('b-ec-rel-add')?.closest('.b-ta-wrap');
      if (!/flex:\s*1/.test(wrap?.getAttribute('style') || ''))
        fail('the card input no longer takes the remaining width of the row');
    }
    window.Boards.closeModal('b-edit-card-modal');
  }

  // 2e-ii-c) The card type-ahead. Linking used to require knowing a slug, which
  // is the one thing about a card nobody remembers. These pin the two ways a
  // person actually refers to a card — its number and a few words of its title
  // — and the edges that make the control safe to use with the keyboard.
  {
    const $id = (id) => window.document.getElementById(id);
    const typeInto = async (text) => {
      const inp = $id('b-ec-rel-add');
      inp.value = text;
      inp.dispatchEvent(new window.Event('input'));
      await new Promise(r => setTimeout(r, 320));   // past the 250ms debounce
    };
    const rows = () => [...($id('b-ec-rel-results')?.querySelectorAll('.b-ta-row') || [])];
    const key = (k) => {
      const e = new window.KeyboardEvent('keydown', { key: k, bubbles: true, cancelable: true });
      $id('b-ec-rel-add').dispatchEvent(e);
      return e;
    };

    // A keystroke is not a request. Typing a word means several input events in
    // quick succession, and they have to collapse into ONE query — asserting
    // "nothing fired synchronously" would pass without any debounce at all,
    // since the request goes out on a microtask either way.
    window.Boards.cardClicked('do-thing');
    await new Promise(r => setTimeout(r, 40));
    const before = searches.length;
    for (const partial of ['i', 'id', 'ide', 'idea']) {
      $id('b-ec-rel-add').value = partial;
      $id('b-ec-rel-add').dispatchEvent(new window.Event('input'));
      await new Promise(r => setTimeout(r, 25));
    }
    if (searches.length !== before)
      fail(`typing fired ${searches.length - before} queries before the debounce elapsed`);
    await new Promise(r => setTimeout(r, 320));
    if (searches.length !== before + 1)
      fail(`four keystrokes produced ${searches.length - before} queries, expected 1`);

    // Text: a few words of the title finds the card.
    await typeInto('idea');
    if (rows().length !== 1) fail(`a title search returned ${rows().length} rows, expected 1`);
    else if (!rows()[0].textContent.includes('Idea One'))
      fail(`a title search did not show the card's title: ${rows()[0].textContent}`);

    // A number is how a person says a card out loud, and it has to reach the
    // server AS the query — the number lookup lives in card_list, not here.
    await typeInto('7');
    if (!searches.includes('7')) fail('a numeric query was not sent to the server');
    if (rows().length !== 1) fail(`"7" returned ${rows().length} rows, expected card #7`);
    else {
      if (!rows()[0].textContent.includes('#7')) fail('the result does not show the card number');
      // do-thing is already linked to idea-one, so it is shown as linked rather
      // than offered again — and picking it must do nothing.
      if (!rows()[0].classList.contains('linked'))
        fail('a card that is already linked was offered as if it were not');
      const posts = linkPosts.length;
      rows()[0].dispatchEvent(new window.MouseEvent('mousedown', { bubbles: true, cancelable: true }));
      await new Promise(r => setTimeout(r, 30));
      if (linkPosts.length !== posts) fail('clicking an already-linked card linked it again');
    }

    // Nothing matched is an ANSWER. An empty box looks like a search that never ran.
    await typeInto('zzzznothing');
    if (rows().length) fail('a search with no matches still rendered rows');
    if (!$id('b-ec-rel-results').textContent.toLowerCase().includes('no cards match'))
      fail('a search with no matches said nothing');

    // Arrow keys move through results and Enter picks the active one, with the
    // kind taken from the segmented control — never typed.
    await typeInto('nasty');
    if (!rows().length) fail('the type-ahead found nothing to pick');
    else {
      const posts = linkPosts.length;
      key('ArrowDown');
      if (!rows()[0].classList.contains('active')) fail('ArrowDown did not move to the first result');
      key('Enter');
      await new Promise(r => setTimeout(r, 30));
      if (linkPosts.length !== posts + 1) fail('Enter did not link the active result');
      else {
        const sent = linkPosts[linkPosts.length - 1];
        if (!sent.url.includes('/cards/do-thing/link')) fail(`the link was posted to ${sent.url}`);
        if (sent.body.other !== 'nasty') fail(`the link named ${sent.body.other}, not the picked card`);
        if (sent.body.kind !== 'related') fail(`the link used kind ${sent.body.kind}, not the selected one`);
      }
    }

    // Escape closes the RESULTS and stops there. If it reached the modal the
    // card would close under the person the first time they dismissed a list.
    await typeInto('idea');
    if ($id('b-ec-rel-results').hidden) fail('the results did not open');
    key('Escape');
    if (!$id('b-ec-rel-results').hidden) fail('Escape did not close the results');
    // The outcome is what matters, and it is observable: the modal's Escape
    // handler sits on `document`, so if the event were allowed to bubble the
    // card would have closed along with the list.
    if (!$id('b-edit-card-modal').classList.contains('open'))
      fail('Escape on the results closed the card modal too — it bubbled to the modal');

    // Out-of-order responses. Two queries are in flight whenever someone keeps
    // typing, and the network decides the order they come back in. A slow early
    // answer landing after a fast later one must not repaint the list, or the
    // person sees results for a query the field no longer holds.
    {
      const _f = window.fetch;
      window.fetch = async (u, o) => {
        const r = await _f(u, o);
        if (String(u).includes('search=slowquery')) await new Promise(x => setTimeout(x, 400));
        return r;
      };
      $id('b-ec-rel-add').value = 'slowquery';       // matches nothing
      $id('b-ec-rel-add').dispatchEvent(new window.Event('input'));
      await new Promise(r => setTimeout(r, 280));    // its request is now in flight
      $id('b-ec-rel-add').value = 'idea';            // supersede it
      $id('b-ec-rel-add').dispatchEvent(new window.Event('input'));
      await new Promise(r => setTimeout(r, 300));    // the fast answer has rendered
      if (rows().length !== 1) fail(`the newer query did not render: ${rows().length} rows`);
      await new Promise(r => setTimeout(r, 450));    // now the stale answer lands
      if (rows().length !== 1 || !rows()[0].textContent.includes('Idea One'))
        fail('a stale search response overwrote the results of a newer query');
      window.fetch = _f;
    }

    // A card can never link to itself, so it must not appear in its own results.
    window.Boards.closeModal('b-edit-card-modal');
    window.Boards.cardClicked('idea-one');
    await new Promise(r => setTimeout(r, 40));
    await typeInto('7');
    if (rows().length) fail('the card being edited was offered in its own search results');

    window.Boards.closeModal('b-edit-card-modal');
  }

  // 2e-iii) Live updates. The poll signature has to include blocked-ness, or
  // linking two cards — which changes nothing else about either of them —
  // would never surface on a board somebody already has open.
  {
    const sig = src.split('function _sigOf(')[1].split('}')[0];
    if (!sig.includes('c.blocked')) fail('blocked-ness is not in the poll signature');
  }

  // 2f) Arming by hold makes a press that never travels a real drag: it opens
  // and closes with the card where it started. That must not PATCH the card to
  // the position it is already in.
  let moveCalls = 0;
  const _fetch2 = window.fetch;
  window.fetch = async (url, init) => { if (String(url).includes('/move')) moveCalls++; return _fetch2(url, init); };
  const listEl = cardLists[0].el, onEnd = cardLists[0].opts.onEnd;
  const someCard = root.querySelector('.b-card');
  await onEnd({ item: someCard, from: listEl, to: listEl, oldIndex: 1, newIndex: 1 });
  await new Promise(r => setTimeout(r, 20));
  if (moveCalls !== 0) fail('a press-and-release that moved nothing still sent a move to the API');
  // A drag that genuinely lands somewhere else must still move, or the guard
  // above would have quietly broken every real reorder.
  const otherList = cardLists.find(s => s.el !== listEl)?.el || listEl;
  await onEnd({ item: someCard, from: listEl, to: otherList, oldIndex: 1, newIndex: 0 });
  await new Promise(r => setTimeout(r, 20));
  if (moveCalls !== 1) fail(`a real drop did not reach the API (move calls: ${moveCalls})`);
  window.fetch = _fetch2;
}

// 3) Self-init on a fresh load onto #boards/<slug> (the board.html redirect-shim
// path): boards.js must build + load itself and open the deep-linked card, since
// the SPA's on-load tab restore runs before this deferred script is defined.
{
  const d2 = new JSDOM('<!doctype html><html><body><button class="tab-btn active" data-tab="boards"></button><div id="boards-root"></div></body></html>', {
    runScripts: 'dangerously', pretendToBeVisual: true, url: 'http://localhost/#boards/idea-one',
  });
  const w = d2.window;
  const one = { card: { slug: 'idea-one', title: 'Idea One', body: 'hi', column_id: 'c1', position: 1, source: 'ui', tags: [], wiki_pages: [] } };
  w.fetch = async (url) => ({
    ok: true, status: 200,
    json: async () => (url.includes('/columns') ? cols : (url.includes('/cards/') ? one : (url.includes('/cards') ? cards : {}))),
  });
  w.Sortable = { create: () => ({ destroy() {} }) };
  const sc = w.document.createElement('script'); sc.textContent = src; w.document.body.appendChild(sc);
  await new Promise(r => setTimeout(r, 60));
  if (!w.document.getElementById('b-card-modal')) fail('self-init did not build the shell on #boards deep-link');
  if (!w.document.getElementById('b-edit-card-modal')?.classList.contains('open')) fail('self-init did not open the deep-linked card');
}

// 4) Live updates. The board is pushed over SSE, and the poll sits behind it as
// the fallback. Both halves are checkable only from here: whether the stream is
// opened at all, whether a pushed event actually re-reads the board, and whether
// the poll survives a stream that fails.
{
  const d3 = new JSDOM('<!doctype html><html><body><div id="boards-root"></div></body></html>', {
    runScripts: 'dangerously', pretendToBeVisual: true, url: 'http://localhost/',
  });
  const w = d3.window;
  const streams = [];
  // A stand-in for the browser's EventSource, so the test can BE the server.
  w.EventSource = class {
    constructor(url) {
      this.url = url; this.readyState = 0; this.listeners = {};
      streams.push(this);
    }
    addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
    close() { this.readyState = 2; }
    emit(name) { (this.listeners[name] || []).forEach(fn => fn({ type: name })); }
  };
  w.EventSource.CLOSED = 2;

  let cardFetches = 0;
  w.fetch = async (url) => {
    if (String(url).includes('/cards')) cardFetches++;
    return {
      ok: true, status: 200,
      json: async () => (String(url).includes('/columns') ? cols
        : (String(url).includes('/people') ? directory : cards)),
    };
  };
  w.Sortable = { create: () => ({ destroy() {} }) };
  const sc = w.document.createElement('script'); sc.textContent = src; w.document.body.appendChild(sc);

  await w.Boards.init();
  w.Boards.startPolling();                 // what the SPA calls on tab activation
  await new Promise(r => setTimeout(r, 40));

  if (streams.length !== 1) fail(`expected the board to open one stream, got ${streams.length}`);
  else {
    const es = streams[0];
    if (!String(es.url).endsWith('/api/board/stream')) fail(`stream opened the wrong URL: ${es.url}`);

    // A change on the stream has to actually re-read the board. An event the
    // client hears and does nothing with is the same as no stream at all.
    const before = cardFetches;
    es.emit('hello');
    es.emit('card.moved');
    await new Promise(r => setTimeout(r, 40));
    if (cardFetches <= before) fail('a pushed change did not re-read the board');

    // Every event memory-api can send has to be listened for. A NAMED SSE event
    // never fires `message`, so one the client does not name is simply not
    // heard: that operation stops updating the board, silently.
    for (const name of ['card.added', 'card.updated', 'card.moved', 'card.archived',
                        'card.unarchived', 'card.linked', 'card.commented',
                        'column.added', 'column.updated', 'column.archived']) {
      if (!es.listeners[name]) fail(`the board does not listen for ${name}`);
    }

    // And the poll has to survive the stream dying — a stream can fail in ways
    // it cannot report, and every one of them looks like a board that stopped.
    const afterDeath = cardFetches;
    es.readyState = 2;
    if (es.onerror) es.onerror();
    await new Promise(r => setTimeout(r, 40));
    w.Boards.refresh();
    await new Promise(r => setTimeout(r, 40));
    if (cardFetches <= afterDeath) fail('the board stopped re-reading once the stream died');
  }

  // Leaving the Boards tab has to close the stream, or every tab switch leaks
  // a connection held open for the life of the page.
  w.Boards.stopPolling();
  await new Promise(r => setTimeout(r, 20));
  if (streams[0] && streams[0].readyState !== 2) fail('leaving the board left its stream open');
}

// 5) A browser with no EventSource at all (or one that throws on construction)
// must still get a working board, on the poll alone.
{
  const d4 = new JSDOM('<!doctype html><html><body><div id="boards-root"></div></body></html>', {
    runScripts: 'dangerously', pretendToBeVisual: true, url: 'http://localhost/',
  });
  const w = d4.window;
  w.EventSource = undefined;
  let cardFetches = 0;
  w.fetch = async (url) => {
    if (String(url).includes('/cards')) cardFetches++;
    return {
      ok: true, status: 200,
      json: async () => (String(url).includes('/columns') ? cols
        : (String(url).includes('/people') ? directory : cards)),
    };
  };
  w.Sortable = { create: () => ({ destroy() {} }) };
  const sc = w.document.createElement('script'); sc.textContent = src; w.document.body.appendChild(sc);

  await w.Boards.init();
  w.Boards.startPolling();
  await new Promise(r => setTimeout(r, 40));
  const before = cardFetches;
  w.Boards.refresh();
  await new Promise(r => setTimeout(r, 40));
  if (cardFetches <= before) fail('with no EventSource the board does not refresh at all');
  if (!w.document.querySelector('#boards-root .b-card')) fail('with no EventSource the board did not render');
  w.Boards.stopPolling();
}

// ── Label pills ──────────────────────────────────────────────────────────────
// Labels were a comma-separated text input. They are now the same chip control
// the people fields use, with the one difference that matters: a label is the
// user's own word, so this field INVENTS where the people field refuses to.
//
// The autocomplete is over labels in use on some card — a vocabulary, not a
// registry — and the reserved marker gets its own treatment, because taking
// `note-to-self` off has to be a deliberate act rather than a side effect.
{
  const w = window;
  const $id = (id) => w.document.getElementById(id);
  const pillsOf = (id) => [...($id(id + '-chips')?.querySelectorAll('.b-label-chip') || [])];
  const pillText = (id) => pillsOf(id).map(e => e.textContent.replace(/×$/, ''));
  const type = async (id, text) => {
    const el = $id(id);
    el.value = text;
    el.dispatchEvent(new w.Event('input'));
    await new Promise(r => setTimeout(r, 20));
    return [...($id(id + '-results')?.querySelectorAll('.b-ta-row') || [])];
  };
  const press = (id, k) => $id(id).dispatchEvent(
    new w.KeyboardEvent('keydown', { key: k, bubbles: true, cancelable: true }));

  cards.cards.push({
    slug: 'pill-card', title: 'Pill Card', body: '', column_id: 'c1', position: 9,
    source: 'claude', labels: ['note-to-self', 'infra', "q\"); window.__pwned_pill = 1; (\""],
    repo: '', reporter: '',
    participants: [], number: 41,
  });
  await w.Boards.refresh();
  await new Promise(r => setTimeout(r, 40));
  w.Boards.cardClicked('pill-card');
  await new Promise(r => setTimeout(r, 60));

  // 1) The card's labels arrive as pills, not as comma-separated text.
  if (pillText('b-ec-labels').slice(0, 2).join('|') !== 'note-to-self|infra')
    fail(`labels did not render as pills: ${JSON.stringify(pillText('b-ec-labels'))}`);
  if ($id('b-ec-labels').value !== '')
    fail('the label input was pre-filled — the pills are the value, the input is for typing');

  // 2) A comma commits what was typed. This is what anyone who used the old
  // comma-separated field will try first, so it has to keep working.
  await type('b-ec-labels', 'urgent,');
  if (!pillText('b-ec-labels').includes('urgent')) fail('a comma did not commit a pill');
  if ($id('b-ec-labels').value !== '') fail('the comma left its text behind in the input');

  // ...and a pasted list lands as several pills rather than one.
  await type('b-ec-labels', 'alpha, beta, part');
  const after = pillText('b-ec-labels');
  if (!after.includes('alpha') || !after.includes('beta'))
    fail(`a pasted list did not split into pills: ${JSON.stringify(after)}`);
  if ($id('b-ec-labels').value !== ' part')
    fail(`text after the last comma should still be being typed: ${JSON.stringify($id('b-ec-labels').value)}`);
  $id('b-ec-labels').value = '';

  // 3) Autocomplete offers labels IN USE, and invents when nothing matches.
  const rows = await type('b-ec-labels', 'ele');
  if (!rows.length || !rows[0].textContent.includes('elefant'))
    fail(`in-use labels are not suggested: ${rows.map(r => r.textContent)}`);
  const none = await type('b-ec-labels', 'zzzznotalabel');
  if (none.length) fail('a label nobody uses was suggested');
  // Enter with nothing highlighted commits the typed text — the people field
  // refuses to invent, this one is supposed to.
  press('b-ec-labels', 'Enter');
  await new Promise(r => setTimeout(r, 20));
  if (!pillText('b-ec-labels').includes('zzzznotalabel'))
    fail('Enter did not commit a brand-new label');
  w.Boards.removeLabel?.('b-ec-labels', 'zzzznotalabel');

  // A label already on the card is offered as taken, not hidden: one you
  // cannot find because you already used it reads like a broken field.
  const dup = await type('b-ec-labels', 'infra');
  if (!dup.length) fail('a label already on the card vanished from the suggestions');
  else if (!dup[0].classList.contains('linked'))
    fail('a label already on the card was offered as if it could be added again');
  $id('b-ec-labels').value = '';

  // 3b) A hostile label renders as a pill and stays inert. The card chips
  // learned this the hard way: an HTML attribute is entity-decoded before the
  // JS inside it is parsed, so a quote in a label breaks out of an onclick.
  // These pills carry no inline handler at all and the value rides in a data
  // attribute — this asserts it stays that way.
  const hostile = pillsOf('b-ec-labels').find(e => e.textContent.startsWith('q"'));
  if (!hostile) fail('the hostile label did not render as a pill');
  else {
    hostile.querySelector('.x').dispatchEvent(
      new w.MouseEvent('mousedown', { bubbles: true, cancelable: true }));
    if (w.__pwned_pill) fail('a label escaped its pill and executed');
    if (pillText('b-ec-labels').some(l => l.startsWith('q"')))
      fail('the hostile pill did not actually remove');
  }

  // 4) The reserved label is marked, and is still removable.
  const reserved = pillsOf('b-ec-labels').find(e => e.textContent.startsWith('note-to-self'));
  if (!reserved) fail('the reserved label did not render as a pill');
  else if (!reserved.classList.contains('reserved'))
    fail('note-to-self rendered as an ordinary label — nothing says the board set it');

  // 5) An ordinary removal must NOT waive the server's protection...
  const patches = [];
  const _f = w.fetch;
  w.fetch = async (u, o) => {
    if (o && o.method === 'PATCH' && /\/cards\/[^/?]+$/.test(String(u)))
      patches.push(JSON.parse(o.body || '{}'));
    return _f(u, o);
  };
  w.Boards.removeLabel('b-ec-labels', 'infra');
  await new Promise(r => setTimeout(r, 60));
  if (!patches.length) fail('removing a label saved nothing');
  else if (patches[patches.length - 1].allow_reserved !== false)
    fail('removing an ORDINARY label waived the reserved-label protection');

  // ...and taking the reserved one off by hand is what does.
  w.Boards.removeLabel('b-ec-labels', 'note-to-self');
  await new Promise(r => setTimeout(r, 60));
  const last = patches[patches.length - 1];
  if (!last || last.allow_reserved !== true)
    fail(`removing a reserved pill did not ask the server to allow it: ${JSON.stringify(last)}`);
  if ((last.labels || []).some(l => l.toLowerCase() === 'note-to-self'))
    fail('the removed reserved label was still sent in the list');
  w.fetch = _f;

  // 6) Re-opening the card must not look like a removal. Filling the field is
  // not editing it, and a stale flag would waive the protection on the next
  // save somebody made for an unrelated reason.
  w.Boards.closeModal('b-edit-card-modal');
  w.Boards.cardClicked('pill-card');
  await new Promise(r => setTimeout(r, 60));
  if (w.Boards.droppedReserved?.('b-ec-labels') !== false)
    fail('merely opening a card counted as dropping its reserved label');

  w.Boards.closeModal('b-edit-card-modal');
  cards.cards = cards.cards.filter(c => c.slug !== 'pill-card');
  await w.Boards.refresh();
  await new Promise(r => setTimeout(r, 40));
}

console.log(ok ? '✅ native board: handlers exposed, shell renders, self-init deep-link opens card, blocked cards marked, live stream pushes with a polling fallback' : '❌ native board check FAILED');
process.exit(ok ? 0 : 1);
