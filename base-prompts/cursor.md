# Cursor: `.cursor/rules/elefant-memory.mdc`

Save the block below as `.cursor/rules/elefant-memory.mdc` in any
project where you want Cursor to use the connected Elefant MCP server.
Put it in `~/.cursor/rules/` instead if you want it everywhere.

---

```mdc
---
description: Use the connected Elefant MCP server for memory and context
alwaysApply: true
---

# Elefant MCP

The connected MCP server exposes `search` (wiki and memories together),
`memory_search`, `memory_capture` (default for writes), `memory_add`
(explicit durable only), `memory_list`, `memory_events`,
`wiki_search_only`, `get_wiki_page` and `git_search` (exact search over
synced git repositories).

Always:
- Call `search` before answering questions that depend on prior
  conversations, user preferences, project decisions, or facts about
  ongoing work.
- Call `memory_capture` when you learn something durable: preferences,
  architectural decisions, project facts, where we left off. Capture
  aggressively. The server runs a compactor that clusters captures,
  scores recurrence and cross-source diversity, and promotes the
  survivors to durable memory.

Use `memory_add` only when the user explicitly says "remember this
exactly," or a single high-confidence durable record is wanted right now
(not a candidate that might dedupe with future captures).

Never:
- Trust training data when this MCP may have the answer.
- End a turn with novel durable info uncaptured.
- Narrate the calls to the user.

Source attribution is automatic from the token, so never pass it. When
in doubt, capture.
```
