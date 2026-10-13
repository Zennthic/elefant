## Elefant MCP: required behavior

You have an Elefant MCP server connected. Tools include `search`,
`memory_search`, `memory_list`, `memory_capture` (default for writes),
`memory_add` (explicit durable only), `memory_update`, `memory_events`,
`wiki_search_only`, `get_wiki_page`, `wiki_write`, `wiki_edit`,
`wiki_upload_image`, `git_search`, and the board tools (`board_columns`, `card_add`,
`note_to_self`, `card_list`, `card_get`, `card_move`, `card_update`,
`card_comment`, `card_close`). The MCP server's own instructions say
"search first, capture what's worth keeping". This block repeats them so
they actually fire.

1. **Before answering** any question that touches prior conversations,
   user preferences, project context, decisions or ongoing work, call
   `search` first. It covers the wiki and memories together. When unsure
   whether it applies, search anyway: the cost is one tool call. For an
   exact identifier or symbol in a synced git repository, use
   `git_search`.

2. **Before ending a turn** where you learned something durable, call
   `memory_capture` (NOT `memory_add`, see 3). Capture aggressively:
   preferences, decisions, project facts, "where we left off", recurring
   people, vetoed approaches. Bloat is cheap because the server runs a
   hot-tier compactor that clusters captures, scores recurrence and
   cross-source diversity, and promotes only the survivors to durable
   memory. Missing context is expensive.

   Examples worth capturing:
   - "User deploys with Docker Compose on a small ARM host; prefer memory-light defaults"
   - "User commits with lowercase imperative subjects"
   - "The project uses an external OIDC provider for login; do not propose password-only auth"
   - "Mid-way through the search refactor; the indexing half is merged, ranking is next"

   Pass an optional `type_hint` (fact / event / procedure / conversation
   / derived) and `tags` when they are obvious; the compactor infers
   them otherwise. Source attribution is automatic, read from the token.

3. **Use `memory_add` only when** the user explicitly says "remember
   this exactly", OR you are certain a single durable record is wanted
   right now (not a candidate that might dedupe with future captures).
   Most writes should be `memory_capture`.

4. **Do not announce these calls.** They are invisible plumbing, not a
   feature for the user to see.

## Board (Kanban)

Elefant has a per-tenant Kanban board, a third durable surface alongside
memory and the wiki. Don't pre-check permissions before using the board
tools. Just call them; a token that lacks access gets a clear error
saying so.

- **`card_add` is the reflex** when the user floats an idea, proposal or
  backlog item they are NOT building right now. It defaults to the
  backlog column.
- **"Note to self"** is an explicit signal: file it with `note_to_self`,
  not as a memory or a wiki page.
- **A card is a tracked issue, not a document.** When an idea needs its
  own page, write it with `wiki_write` under
  `<AI folder>/projects/<card-slug>/` (the AI folder is `AI` unless the
  user changed it; pages there are linked to the card automatically)
  instead of pasting the page into the card.
- **What happened to a card goes in a comment** (`card_comment`), not in
  an edit to its description.
- Column names are the **user's own vocabulary** ("Up Next", "Backlog").
  Call `board_columns` to learn them before `card_add` or `card_move`.
  Never assume a column name.

## What not to capture

- Small talk and acknowledgements
- One-shot procedural details that are only true for the next 30
  minutes ("we're editing file X right now")
- Anything the user explicitly said is throwaway
