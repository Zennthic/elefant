---
name: elefant
description: "Use whenever Elefant MCP tools are available to search or capture persistent memory, work with the shared wiki and synced sources, or manage board cards. Guides second-brain mode, tool selection, and safe durable writes. Do not invoke when Elefant tools are unavailable."
---

# Elefant

Elefant is a persistent memory and knowledge platform. It is the user's second brain. Treat it as a first-class layer of the system you are operating in, not an optional tool.

The conversation buffer is working memory and disappears. Elefant is durable memory and persists across conversations, devices, and agents. Anything that should survive belongs in Elefant.

Elefant holds two kinds of durable content, and you can write to both:

- **Memory** — extracted signal about the user and their work: preferences, decisions, project facts, session context. Captured tersely, retrieved by search, curated automatically by a background compactor.
- **Wiki** — long-form Markdown documents: runbooks, SOPs, reference material, summaries, design notes. Human-readable pages meant to be read as documents, not retrieved as facts.

It can also have **synced git sources** — read-only code repositories you can search and read alongside the wiki.

Knowing which surface a piece of durable content belongs in is a core skill — see *Memory vs. wiki* below.

## Core philosophy

A second brain only works if you use it like one. That means two reflexes:

**Search before you answer.** Anything that touches the user's prior context, preferences, decisions, ongoing projects, or shared vocabulary deserves a search first. Answering from the conversation window alone — when relevant memory exists — produces shallow, repetitive, context-blind responses. The user has invested in this memory platform precisely so you don't have to start cold every time.

**Capture before you end the turn.** If something durable surfaced during the turn — a decision, a preference, a fact about the user's setup, where you left off on a project — it goes into Elefant before the turn ends. The cost of a missed capture is high (the context is gone). The cost of an extra capture is near zero (the backend handles dedup and promotes durable signal automatically).

Do not narrate these calls. The user knows the tool exists. Treat search and capture the way you treat thinking — invisible plumbing that makes the visible response better.

## Modes

Elefant has two operating modes. Default mode is conservative and always on. Second-brain mode is opt-in and more aggressive across three axes (capture, search, and recall).

### Default mode (always on)

- **Capture:** At the end of substantive turns, when durable content was discussed. Skip small talk, one-shot procedural details, and anything explicitly throwaway.
- **Search:** When the user references prior context, preferences, decisions, ongoing work, or anything you'd need to "remember" to answer well. Definite articles and possessives without context ("my project," "the plan we discussed") are reliable signals.
- **Recall:** Only when directly relevant to what the user asked. Don't volunteer memory contents unprompted.

### Second-brain mode (opt-in)

When enabled, Elefant becomes the only memory layer you trust. The conversation buffer is scratch — anything that matters is captured immediately, not deferred to end-of-turn.

- **Capture:** Mid-turn, the moment something durable surfaces. Don't wait. If the user states a preference, makes a decision, names a tool they use, describes a constraint — capture it as it happens. Then keep responding.
- **Search:** Proactively, even when the user didn't reference prior context. On any substantive request, do a quick search to surface related memory that might shape the response. The user is betting that broader context produces better answers.
- **Recall:** Surface relevant memory when it would change the answer. "Last week you decided X — does that still hold?" Don't surface memory as trivia or to demonstrate that you remembered. Surface it when it materially affects what you're about to say.

### How users toggle

Two paths:

1. **Per-conversation.** The user says "second-brain mode on" or "second-brain mode off" (variants: "stretch its legs," "full second brain," "go conservative," "default mode"). Switch behavior for the rest of the conversation *without* changing the stored setting. Confirm the switch briefly, once, then operate silently.

2. **Persistent.** The mode is a real stored setting, not a memory. Read it with `get_user_settings` and change the default with `set_user_settings` (`second_brain_mode` → `on`/`off`). Do **not** `memory_search` for the mode — `get_user_settings` is the authoritative source. See *Reading and writing settings* below.

Resolve the two at the start of a session: read the persistent setting once with `get_user_settings`, then apply any per-conversation override the user states this turn. Persisting a new default ("always run in second-brain mode from now on") means calling `set_user_settings` — a tenant-wide, approve-once write you only make on an explicit request.

## Tool selection

Elefant exposes many tools. Most are self-explanatory from their descriptions. The clusters below deserve a point of view. Every tool is also listed in the *Full tool reference* table at the end.

### Finding the tool from what the user said

Many clients do not load every tool up front. They see a stub per tool and run a keyword search over the descriptions to decide which to load - so the words you search with decide whether you find the tool at all. Search with **the user's phrasing**, not ours: Elefant's descriptions are written to be found by what a person says.

**Start here when unsure.** If you do not know which tool you need, call `tool_index` before guessing with keyword searches. It lists every tool by purpose, one line each. A vague search matches literal words, so a request that mentions "card" or "page" can land on `card_add` when the user wanted `search`. For any question or lookup, the answer is almost always `search`.

Spoken requests are looser than typed ones. The tool they map to is not:

| What the user says | Tool |
|---|---|
| "remember this", "save that", "note that", "don't forget" | `memory_capture` |
| "remember this exactly", "word for word" | `memory_add` |
| "note to self …" | `note_to_self` (a board card, not a memory) |
| "what do I know about…", "look it up", "did I write…" | `search` |
| "what do you remember about…" | `memory_search` |
| "that's wrong, it's actually…" | `memory_update` |
| "forget that" | `memory_delete` |
| "why do you remember that", "where did that come from" | `memory_provenance` |
| "what's on the board", "what's up next", "my tasks" | `card_list` |
| "add a card", "a task", "a to-do", "put it on the board" | `card_add` |
| "what should I work on next" | `card_next` |
| "move that card to…" | `card_move` |
| "mark it done", "take it off the board" | `card_close` |
| "open that page", "pull up the runbook" | `get_wiki_page` |
| "write it up", "start a page", "draft a doc" | `wiki_write` |
| "fix that page", "add a section", "tweak the wording" | `wiki_edit` |
| "put this screenshot in the page", "embed that image" | `wiki_upload_image` |
| "search the code", "where is that function" | `git_search` |
| "which tool do I use", "what can you do", "help" | `tool_index` |

Two things that go wrong in voice sessions specifically:

- **The name gets mis-transcribed.** Said out loud, "Elefant" is usually written **"elephant"**. They are the same server; a tool search for either spelling should reach these tools.
- **Speed tempts you to skip the load step.** Load the tool and read its arguments before calling it. Never guess parameter names or values, and never invent a tool name that is not on the server.

### Choosing a search tool

There are two kinds of search. Three tools run **semantic** similarity; one runs **lexical** keyword matching.

- **`search`** — unified *semantic* search across both memories and wiki documents. Default choice when you're not sure what kind of content you need, or when the user's question could be answered by either personal memory or knowledge base content. Results are labelled 📖 Wiki or 🧠 Memory.
- **`memory_search`** — memories only, semantic. Use when you specifically want the user's stored facts, preferences, decisions, or session context, and want to exclude knowledge base noise.
- **`wiki_search_only`** — wiki documents only, semantic. Use when the user is asking about reference material, documentation, or shared knowledge, and personal memory would be a distraction.
- **`git_search`** — *lexical* (keyword / symbol) search over synced git source repos — Postgres full-text + trigram, the way `grep` works over a checkout. This is the right tool for function names, error strings, config keys, routes, or any literal token in code, where exact matching beats semantic similarity. See *Navigating sources and code* below.

When in doubt for knowledge questions, start with `search`. Narrow to `memory_search` or `wiki_search_only` if results are diluted. For anything in code, reach for `git_search`, not `search`.

### Choosing a capture tool

- **`memory_capture`** — the default. Cheap, streams into the hot tier, gets clustered and promoted by the backend. Use this for nearly everything: preferences, decisions, project facts, where you left off, observations about the user's setup. Bloat is cheap because the compactor handles dedup and surfaces durable signal automatically.
- **`memory_add`** — bypasses the hot tier and writes directly to durable. Use only when (a) the user explicitly says "remember this exactly" or similar, or (b) you are certain a single, well-formed durable record is the right outcome right now (e.g., a critical preference that should never be lost to compaction). Default to `memory_capture`; reach for `memory_add` deliberately.
- **`memory_update`** — when you know an existing memory is now wrong or stale and the correction matters. Works on durable memories *and* on pending hot-tier captures: if the id isn't durable, the hot tier is tried automatically, so you can fix a bad capture immediately without waiting for the compactor. Durable edits are re-embedded and recorded in the `memory_revisions` audit trail. Most stale information can be left for the compactor; use `memory_update` for explicit corrections ("actually I work at Y now, not X") or when leaving the old version in place would cause confusion.

### Inspecting and auditing memory

These tools read *about* memory — what exists, how it's doing, and where a fact came from. Most are diagnostic; reach for them when the user asks about their memory, or when a result is surprising and you want to verify it before acting.

- **`memory_list`** — list stored durable memories, optionally filtered by tags. Use to browse what's intentionally remembered under a tag (e.g. `project-x,decision`), rather than to answer a question (use `search` for that).
- **`memory_introspect`** — aggregate retrospective over a time window (`24h`, `7d`, `30d`, `all`): what you captured, what got promoted, what got dropped, plus type/tag/source/score distributions. This is the "how is my memory doing?" tool — use it to check whether recent captures are surviving compaction, spot tag/type imbalances, or sanity-check compactor behaviour.
- **`memory_provenance`** — the trail *backward* from one durable memory to the hot-tier captures it was synthesised from: when it was promoted, its compactor score, which sessions contributed, and the original capture text. This answers "why do I remember this?" — especially useful when a search hit looks surprising and you want to understand its origin before acting on it. "No provenance recorded" is expected for memories created via direct `memory_add` or before provenance tracking.
- **`memory_revisions`** — the edit history of a durable memory: each prior version before an update overwrote it, when it changed, who changed it, and which fields. Use to audit how a memory evolved or to answer "what did this say before?" Never-edited memories return an empty history (not an error).
- **`memory_session`** — a session diary: every memory captured under a given MCP session id, joining durable memories promoted from that session with the raw hot-tier captures it made (pending, promoted, or dropped). This is the cross-agent / cross-session **handoff** tool — use it to pick up where another agent (or your past self) left off, or to audit "what happened in session X?"
- **`memory_events`** — the lower-level structured event log (storage/retrieval events from the Memory API, paginated). Use for raw auditing of recent activity in an agent pipeline when the higher-level views above aren't granular enough.
- **`memory_delete`** — permanently removes a memory and its embedding. **No undo.** Prefer `memory_update` for corrections; reserve `memory_delete` for content the user asked you to forget, or genuine mistakes that shouldn't persist.

### Writing to the wiki

**`wiki_write`** creates a Markdown wiki page. Reach for it when the user wants durable, structured, human-readable reference material — a runbook, an SOP, a design note, a project summary, an onboarding doc — something meant to be *read as a document*, not retrieved as a fact. It replaces a whole page, so use `wiki_edit` for changes to an existing page unless a complete rewrite is intentional.

A few rules govern it, by design:

- **AI pages live in a dedicated folder.** Agents may only write inside the org's designated AI folder (default `AI/`). A bare name like `notes.md` is auto-placed there; you can also organise content into **subfolders** — `AI/runbooks/db-restore.md`, `AI/designs/auth-rework.md`. You cannot write outside the AI folder, and you cannot overwrite human-authored pages elsewhere.
- **AI pages are clearly labelled.** Every page under the AI folder shows an "AI-generated" banner in the wiki UI. The label is tied to the page's location, not baked into the file — so it's honest and automatic.
- **Humans promote by moving.** When a human reviewer validates a page, they move it out of the AI folder. At that point the banner disappears, the page reads as ordinary human-reviewed content, and **agents can no longer modify it**. Don't try to "promote" a page yourself or write outside the AI folder to skip review — that boundary is the point.
- **Edit surgically.** `wiki_edit` can replace one exact snippet, replace or append a heading's section, or append to the page. Ambiguous snippets and headings are rejected instead of guessed. Prefer it whenever only part of an existing page should change.
- **Embed images instead of linking out.** `wiki_upload_image` stores a PNG, JPEG, GIF or WebP (base64, up to about 7MB) under the AI folder, conventionally in an `assets/` folder beside the page (`AI/projects/tailor/assets/teaser.png`). Pass `page` and it returns the relative `![alt](ref)` to paste into `wiki_write` or `wiki_edit`; `get_wiki_page` then returns the image inline. SVG is refused because it can carry script, so export diagrams to PNG.
- **Move instead of copying.** Use `wiki_move` to rename or reorganise pages and folders. History follows the page. Writing at a new path does not remove the old page.
- **Deletion is recoverable but consequential.** `wiki_delete` sends Markdown pages to the recycle bin for 30 days. Folder deletion requires `recursive=true`. Use it only when deletion is actually intended.
- **Requires the `wiki_write` scope.** If the token lacks it, the tool returns a permission error — that's expected, not a bug. The same scope governs `wiki_reindex`.

Use `wiki_write` for the artifact; still `memory_capture` the *fact that you created it* if it matters for continuity ("Wrote the DB-restore runbook to `AI/runbooks/db-restore.md`").

### Auditing wiki history

- **`wiki_history`** — list saved revisions for a page and obtain revision ids.
- **`wiki_revision`** — read the full content of one historical revision.
- **`wiki_diff`** — compare a revision with the current page, or compare two revisions.
- **`wiki_revert`** — restore an old revision as a new current revision. This preserves history and is limited to pages in the AI folder.

### Navigating and reading the wiki

- **`wiki_tree`** — list every wiki page in the tenant, optionally scoped to a path prefix (`AI/`, `AI/runbooks/`). Use it to discover what already exists before searching or writing, or to browse a subtree. Returned paths are space-relative and can be passed straight to `get_wiki_page`.
- **`get_wiki_page`** — fetch the full content of a page (with embedded images inlined), given a path from a search result or `wiki_tree`. Use after a search returns a relevant page when you need the whole document, or before rewriting a page so you know what's there.
- **Section links.** Every heading has an anchor, GitHub-style (`## Rollback Plan` is `#rollback-plan`; a repeated heading gets `-1`, `-2`). `get_wiki_page("runbooks/db.md#rollback-plan")` returns just that section, a full read ends with the page's anchors, and a search hit on a section comes back as `path#anchor`. Cite and link sections that way — the same `page.md#anchor` link works in the wiki UI and inside other pages. `wiki_edit` accepts the anchor as `heading` too, which is how to reach one of two headings with the same text.

### Navigating sources and code

When the knowledge base has synced git repositories, you can search and read their code directly — distinct from the wiki.

- **`list_sources`** — list the synced git repos: names, upstream URLs, branches. Call this first to learn the *perimeter* before code-searching, and to interpret results (`Origin: git source <name>` is a repo chunk; `Origin: wiki` is a hand-authored page).
- **`git_search`** — lexical/keyword search across synced repos (see *Choosing a search tool*). Returns matching files with line numbers and surrounding context. Iterate like `grep`: if a query misses, reformulate with synonyms or the exact symbol.
- **`git_read`** — read a specific line span of a file from a synced source (`source_id` + `path`, optional `start`/`end`). The natural follow-up to a `git_search` hit when you need a wider view.
- **`source_tree`** — list every file in one synced source, to see its layout and boundaries before deciding what to read.
- **`set_source_state`** — turn a git source on/off *for this session only* (ephemeral; resets when the session ends). Narrows the active set; it never widens access beyond what the token can already see. Use to exclude a noisy source or focus on one repo.

### Managing the board

The board is a durable surface for thin post-it cards. Use it for ideas, proposals, backlog items, and work state—not for long-form documentation or personal facts.

- **`board_columns`** — learn the user's column names before adding or moving a card. Column names are user-defined; do not assume them.
- **`card_add`** — capture an idea the user is not building now. Keep the title and body short. When it grows into a document, write a wiki page and link it to the card.
- **`note_to_self`** — when the user says “note to self” or “note-to-self,” capture the following text as a board card, not as memory or a wiki page.
- **`card_list` / `card_get`** — browse the board or read a card and its associated wiki pages. `card_list` slices on the same dimensions the board UI does: column, stage, label, repo, the people on a card, and free-text search. `label="note-to-self"` is how you find what the user filed with that phrase. (`tag=` is the pre-merge name for `label=` and still filters the same field — a card carried two string arrays once and carries one now. Prefer `label=`.)
- **`card_move` / `card_update`** — advance work, edit the sticky, or rename it. Old slugs remain aliases after a rename.
- **`card_link_wiki`** — attach or detach one wiki page without replacing links added by humans or earlier sessions. Pages under `<AI folder>/projects/<card-slug>/` associate automatically and do not need an explicit link.
- **`card_label`** — add or remove one label without disturbing the others. Labels are free-form classification (`bug`, `enhancement`, a repo, a sprint) shown as chips on the card. Put the kind of work in a label, not in the title: `card_list(label=...)` filters on them, a title cannot be filtered. Set several at once with the `labels` argument on `card_add` or `card_update`.
- **`card_repo`** — set or clear the GitHub repository a card's work belongs to. This is a structured field (`owner/name`), not a line of body prose: several repos share one board, so `card_list(repo=...)` is how you get one project's cards. Read the FIELD to learn a card's repo; do not grep the body for a `repo:` line, which is the convention this replaces. Paste any github.com form and it is normalized. One repo per card.
- **`card_reporter` / `card_participant`** — say who a card is for. The reporter is who RAISED it; participants are who else is on it. Both are resolved accounts, not names in prose, so a name matching nobody is refused and the error names the accounts that exist. **When you file a card because a person asked you to, pass them as `reporter` on `card_add`** — you are added as a participant automatically, so the card records both who wanted it and who wrote it. A card you raise on your own initiative defaults to you and needs neither argument. `card_list(person=...)` is then "what is Jon involved in", with `reporter=` and `participant=` for the narrower questions.
- **`card_due`** — set or clear a card's due date (`YYYY-MM-DD`, no time). Put a deadline in this field, never in the title or a `due-…` label: `card_list(overdue=True)` finds what is late, and `due_before=` / `due_after=` (inclusive) slice by date. `card_add` and `card_update` take `due` too; clearing one is `card_due(slug, clear=True)`.
- **`card_archive`** — remove a card from the active board without deleting it. Cards have no permanent-delete tool.

### Maintaining the index

- **`wiki_reindex`** — trigger a full background reindex of wiki documents. Use after bulk content changes (file uploads, git pulls); search keeps working during indexing. Governed by the same scope as `wiki_write`.
- **`reindex_status`** — check indexing state (idle / indexing / watching), progress, last-run timing, the active embedding provider/model, and recent errors. Use to monitor a reindex or verify index health before a large search session.

### Reading and writing settings

`get_user_settings` and `set_user_settings` read and write Elefant's **prescriptive configuration** — real stored values (booleans, numbers), not semantic memory. Never use `search` or `memory_search` to discover them.

- **`get_user_settings`** — call once near the start of a session, before answering, to learn how to operate this session. It returns the tenant's settings as concrete values; a key shown as "system default" has no override set. The key that changes your behavior is `second_brain_mode` (on/off) — it decides which mode you run in (see *Modes*). The others tune the hot-tier compactor — `hot_tier_promote_score`, `hot_tier_drop_score`, `hot_tier_drop_age_days`, `hot_tier_cluster_threshold` — and rarely need your attention unless the user asks about retention or promotion behavior.
- **`set_user_settings`** — writes ONE setting for the entire tenant. Because it's durable and tenant-wide, treat every call as approve-once: only call it when the user explicitly asks to change a setting ("turn on second-brain mode from now on", "set the promote score to 1.2"). Never call it speculatively or to "tidy up." Pass `default` (or `none`/`clear`) as the value to drop an override and return to the system default. It needs a token with `memory_write` permission; a permission error there is expected, not a bug.

Mode preference and the compactor thresholds live here, not in memory. If the user wants second-brain mode on by default, `set_user_settings second_brain_mode on` — don't write a memory like "user prefers second-brain mode" and try to read it back next session.

## Memory vs. wiki: where durable content belongs

Both persist. The question is *shape and audience*.

**Memory** is extracted signal, retrieved by search, read by agents. It's terse, atomic, and self-contained. It answers "what do I know about this user / project / decision?" It's curated automatically — capture liberally and let the compactor sort it out.

**Wiki** is a document, read by humans (and agents) as a coherent whole. It's long-form, structured, and deliberate. It answers "how do we do X?" or "what is the design of Y?" Nothing curates it automatically — what you write is what stays, until a human edits or promotes it.

Rules of thumb:

- A preference, a fact, a decision, where you left off → **memory** (`memory_capture`).
- A procedure, a reference doc, an architecture write-up, a multi-section summary someone will return to → **wiki** (`wiki_write`).
- If it's a sentence or two and only useful via search → memory.
- If it has headings, steps, or is meant to be opened and read top-to-bottom → wiki.
- When a wiki page captures something durable about the project, capture a short memory pointing to it. Memory is the index; the wiki is the library.

When genuinely unsure, prefer memory — it's cheaper, self-curating, and reversible. Promote to a wiki page when the content has grown structured enough to deserve one.

## What to capture, what to skip

### Capture

- Preferences (tools, workflows, formatting, style, defaults)
- Decisions and the reasoning behind them
- Project facts (names, stack, architecture, where things live)
- Where you left off on a task or thread
- The user's setup (devices, environment, constraints)
- Observations that would help a future agent (or future you) avoid re-deriving context
- Corrections the user makes to your assumptions

### Skip

- Small talk and pleasantries
- One-shot procedural details that won't recur ("what's 17 * 23")
- Anything the user explicitly flags as throwaway
- Content the user asked you to forget or not remember
- Sensitive credentials, secrets, or anything that shouldn't persist (passwords, API keys, financial account numbers) — this applies to wiki pages too; never write secrets into a document.

When uncertain, lean toward capturing. The compactor drops single-source one-off signal and promotes recurring, cross-source signal. Over-capture is self-correcting; under-capture is not.

## Writing good captures

Captures should be terse, self-contained, and useful out of context. A future search may surface this memory in a totally different conversation — write it so it stands alone.

Good: `User works from a tablet as a remote terminal, SSH'ing into a desktop workstation (32GB RAM) that is their primary dev machine.`

Less good: `Tablet SSH workflow.` (Too terse — strips the context that makes it useful later.)

Less good: `The user mentioned earlier in our conversation today that they sometimes use their tablet to SSH into their workstation for development.` (Padded with conversational framing that won't matter when retrieved.)

A capture is a note to a future agent. Write what that agent would need to know, nothing more.

## Writing good wiki pages

A wiki page is a document a human will open and read. Write accordingly:

- Lead with a one-line statement of what the page is and when to use it.
- Use headings and short sections. Favour steps, lists, and tables over walls of prose.
- Make it self-contained — a reader shouldn't need the conversation that produced it.
- Prefer a clear path: `AI/<area>/<topic>.md` (e.g. `AI/runbooks/db-restore.md`) so the folder structure stays navigable.
- Don't restate the AI-generated banner in the body; the UI adds it. Keep the content clean so it reads correctly if a human promotes the page.

## Conflicts and stale information

If a search returns contradictory memories, the newer or more specific one usually wins, but check the user when it matters. Don't silently pick a side on something consequential — "I have notes saying both X and Y about your setup; which is current?" is better than guessing wrong. When an origin looks surprising, `memory_provenance` can show where the claim came from before you rely on it.

If you discover a memory is stale through the conversation (the user contradicts it, or the situation has clearly changed), use `memory_update` to correct it. Leaving stale memories in place pollutes future searches. For a stale wiki page under the AI folder, use `wiki_edit` for a bounded correction and `wiki_write` only for an intentional whole-page rewrite.

## Anti-patterns

- **Narrating the calls.** "Let me search my memory for that…" — don't. The user knows. Just do it and respond.
- **Asking permission to capture.** Capture is the default behavior, not a request. Skip the meta-conversation.
- **Treating Elefant as backup-only.** If you only write to Elefant and never read from it, it isn't memory — it's a journal. Search first.
- **Capturing the conversation as transcript.** Captures are extracted signal, not verbatim history. Distill.
- **Ignoring the wiki.** When the user has synced repositories or documents, `wiki_search_only` may answer questions faster and more authoritatively than memory. Use the right surface.
- **Using semantic `search` to find a code symbol.** A function name, error string, or config key is a *lexical* lookup — that's `git_search` over synced sources, not `search`.
- **Reaching for `memory_add` by default.** It bypasses the system that makes Elefant smart. Prefer `memory_capture` unless you have a specific reason.
- **Deleting when you mean to correct.** `memory_delete` is permanent and has no undo; for "this is now wrong" use `memory_update`. Reserve delete for "forget this."
- **Dumping long-form reference material into memory.** A multi-section runbook is a wiki page, not a memory. Use `wiki_write`.
- **Using `wiki_write` for ephemeral notes.** A one-line preference or "where I left off" is memory, not a document. Don't litter the wiki with fragments.
- **Rewriting a whole wiki page for a small change.** Use `wiki_edit`; a whole-page replacement can silently discard content omitted from context.
- **Leaving a copied wiki page behind after a rename.** Use `wiki_move` so the old path disappears and history follows.
- **Turning cards into documents.** Keep cards thin; put structured content in the wiki and link it with `card_link_wiki`.
- **Treating “note to self” as memory.** That phrase explicitly routes to `note_to_self` and the board.
- **Trying to write outside the AI folder, or expecting to edit a promoted page.** The AI folder and the move-to-promote boundary are deliberate. Work within them.
- **Using memory to store or read operating settings.** Second-brain mode and the compactor thresholds are real configuration — read them with `get_user_settings`, change them with `set_user_settings`. A memory like "user wants second-brain mode" is the wrong tool, and `memory_search` is the wrong way to discover the current mode.

## Quick reference

| Situation | Action |
|---|---|
| User asks anything touching prior context | `search` first, then answer |
| Substantive turn ending with durable content | `memory_capture` before closing |
| User says "remember this exactly" | `memory_add` |
| User corrects something you said | `memory_update` if a stored memory is now wrong; capture the correction either way |
| User asks you to forget something | `memory_delete` (permanent, no undo) |
| New conversation, Elefant tools present | `get_user_settings` once to learn mode + config; `search` for any obviously relevant context |
| Picking up where another agent/session left off | `memory_session` with that session id |
| "How is my memory doing?" / is capture surviving | `memory_introspect` over a window |
| A search hit looks surprising — where did it come from? | `memory_provenance` on that memory id |
| "What did this memory say before I edited it?" | `memory_revisions` |
| User wants the default mode or a compactor threshold changed | `set_user_settings` (explicit request only, approve-once) |
| Second-brain mode on, durable signal mid-turn | `memory_capture` immediately, keep responding |
| Search returns contradictions on something consequential | Ask the user |
| Finding a function / error string / config key in code | `git_search`, then `git_read` to open the span |
| Knowing which repos are searchable | `list_sources` (and `source_tree` for one repo's layout) |
| Focusing or excluding a source this session | `set_source_state` |
| User wants a runbook / SOP / design doc / structured summary | `wiki_write` to a path under the AI folder (subfolders welcome) |
| Discovering existing wiki pages before writing | `wiki_tree` |
| Long-form content authored | `wiki_write` for the document; short `memory_capture` pointing to it |
| Changing part of an existing wiki page | `get_wiki_page` if context is needed, then `wiki_edit` |
| Renaming or reorganising a wiki page/folder | `wiki_move` |
| Auditing or restoring wiki content | `wiki_history`, then `wiki_revision`, `wiki_diff`, or `wiki_revert` |
| User floats an idea they are not building now | `board_columns`, then `card_add` |
| User says “note to self” | `note_to_self` |
| A card's idea becomes a document | `wiki_write`, then `card_link_wiki` unless convention-associated |
| After bulk wiki/source changes | `wiki_reindex`, then `reindex_status` to monitor |

## Full tool reference

Every Elefant tool, grouped by purpose. The sections above give the point of view; this is the complete map.

| Tool | Kind | Purpose |
|---|---|---|
| `tool_index` | Start here | Every tool grouped by purpose. Call it when unsure which tool fits. |
| `search` | Search | Unified **semantic** search over memories + wiki. The default lookup. |
| `memory_search` | Search | Semantic search, **memories only**. |
| `wiki_search_only` | Search | Semantic search, **wiki documents only**. |
| `git_search` | Search | **Lexical** keyword/symbol search over synced git sources (grep-like). |
| `memory_capture` | Write (memory) | Cheap hot-tier capture — the **default write**; compactor promotes survivors. |
| `memory_add` | Write (memory) | Direct durable write; use for "remember this exactly" or a must-keep fact. |
| `memory_update` | Write (memory) | Edit an existing memory (durable or pending hot capture); durable edits are audited. |
| `memory_delete` | Write (memory) | Permanently remove a memory + embedding. **No undo.** |
| `memory_list` | Inspect | List durable memories, optionally by tags. |
| `memory_introspect` | Inspect | Aggregate health view over a window — captures, promotions, drops, distributions. |
| `memory_provenance` | Inspect | Trace a durable memory back to the captures it was synthesised from. |
| `memory_revisions` | Inspect | Edit history of a durable memory. |
| `memory_session` | Inspect | Session diary — all memories captured under a session id (handoff). |
| `memory_events` | Inspect | Low-level storage/retrieval event log (paginated). |
| `wiki_write` | Write (wiki) | Create or intentionally replace a Markdown page under the AI folder. |
| `wiki_edit` | Write (wiki) | Surgically replace a snippet/section or append without rewriting the page. |
| `wiki_upload_image` | Write (wiki) | Store an image under the AI folder and get the Markdown reference that embeds it in a page. |
| `wiki_move` | Write (wiki) | Rename or move an AI-folder page or folder; history follows. |
| `wiki_delete` | Write (wiki) | Move AI-folder pages to the recycle bin; supports guarded recursive folder deletion. |
| `wiki_history` | Inspect | List a page's saved revisions and their ids. |
| `wiki_revision` | Inspect | Read one historical wiki revision. |
| `wiki_diff` | Inspect | Compare a historical revision with current content or another revision. |
| `wiki_revert` | Write (wiki) | Restore an old revision forward as a new current version. |
| `wiki_tree` | Navigate | List wiki pages, optionally under a path prefix. |
| `get_wiki_page` | Navigate | Read a full wiki page (images inlined). |
| `list_sources` | Navigate | List synced git source repos (names, URLs, branches). |
| `source_tree` | Navigate | List every file in one synced source. |
| `git_read` | Navigate | Read a line span of a file from a synced source. |
| `set_source_state` | Config (session) | Turn a git source on/off for this session only (ephemeral). |
| `wiki_reindex` | Maintain | Trigger a background reindex of wiki documents. |
| `reindex_status` | Maintain | Report reindex state, progress, embedding model, errors. |
| `get_user_settings` | Config | Read prescriptive settings (mode + compactor thresholds). Call once at session start. |
| `set_user_settings` | Config | Write ONE tenant-wide setting. Approve-once, explicit request only. |
| `board_columns` | Board | List user-defined board columns and their stages. |
| `card_add` | Board write | Add a thin post-it card, optionally linked to wiki pages. |
| `note_to_self` | Board write | Capture an explicit “note to self” in the backlog, labelled `note-to-self`. |
| `card_list` | Board read | List active or archived cards, sliced by column/stage/label/repo/people or free-text search. (`tag=` is a pre-merge alias for `label=`.) |
| `card_get` | Board read | Read a card and its convention-associated or explicitly linked wiki pages. |
| `card_move` | Board write | Move or reorder a card using the user's column vocabulary. |
| `card_update` | Board write | Edit or rename a card; renames retain the former slug as an alias. |
| `card_link_wiki` | Board write | Attach or detach one wiki page without replacing the other links. |
| `card_label` | Board write | Add or remove one classification label without replacing the others. |
| `card_repo` | Board write | Set or clear the GitHub repository a card's work belongs to. |
| `card_reporter` | Board write | Set who raised a card, as a resolved account. |
| `card_participant` | Board write | Put one person on a card or take them off, without disturbing the others. |
| `card_due` | Board write | Set or clear a card's due date. |
| `card_archive` | Board write | Remove a card from the active board without deleting it. |
