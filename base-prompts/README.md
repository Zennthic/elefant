# Base prompts

Bootstrap snippets that make a connected agent use Elefant's MCP server
well: search before answering, capture durable facts before ending a
turn, and do both without narrating the calls to the user.

The MCP server (`mcp/main.py`) ships with an `instructions` block that
says "search first, capture what's worth keeping." In practice the host
(Claude Code, Claude Desktop, ChatGPT, Cursor) puts its own system prompt
ahead of those instructions, and they get out-prioritized. The snippets
here repeat the behavior at the host level so it actually fires.

Every snippet uses the same placeholders. Replace them with your own
values:

| Placeholder                     | Meaning                                           |
|---------------------------------|---------------------------------------------------|
| `https://elefant.example.com/mcp` | Your Elefant MCP endpoint                       |
| `<your-token>`                  | An API token from My Settings, then API tokens          |

## Two-tier memory: what to use when

Elefant stores memory in two tiers, and that sets the default for
writes:

- **`memory_capture` (default).** Cheap streaming capture with no
  embedding cost. Captures land in a hot tier. A background compactor
  clusters them, scores recurrence, cross-source diversity and recency,
  and promotes the survivors into durable memory. Bloat is cheap;
  missing context is expensive.
- **`memory_add` (explicit only).** A direct write to durable memory.
  Use it only when the user says "remember this exactly", or when one
  high-confidence record is wanted now rather than a candidate that may
  dedupe with later captures.
- **`search`.** Always call it before answering when prior context,
  user preferences, project facts or ongoing work matter. It covers the
  wiki and memories together.

Source attribution is structural. Every memory carries a `source` field
that is filled in from the calling token's source (set when the token
was issued in the admin UI). Agents never pass it.

## Files

| File                    | Where to put it                                           |
|-------------------------|-----------------------------------------------------------|
| `claude-code.md`        | `~/.claude/CLAUDE.md` (user-wide) or `./CLAUDE.md` (repo) |
| `claude-code-hook.md`   | Notes for the optional `UserPromptSubmit` hook            |
| `claude-desktop.md`     | Claude Desktop project or system instructions             |
| `chatgpt.md`            | ChatGPT settings, custom instructions                     |
| `cursor.md`             | `./.cursor/rules/elefant-memory.mdc`                      |

Each file is short on purpose. It has to fit in the host's instruction
budget without dominating context.

## What the snippets enforce

The same behavior on every host:

1. Call `search` (or `memory_search` for memories only) before answering
   when prior context matters.
2. Call `memory_capture` aggressively: preferences, decisions, project
   facts, where we left off. When in doubt, capture.
3. Keep `memory_add` for explicit "remember this exactly" cases.
4. File ideas the user is not building yet as board cards (`card_add`),
   and treat "note to self" as `note_to_self`.
5. Do not narrate any of these calls. They are invisible plumbing.

## One token per host

To get the most out of source attribution and the compactor's
cross-source recurrence signal, issue **a separate API token per host**
in the editor (Settings, then API Tokens, then Generate Token) and set
its `Source` field:

| Token name     | Source         |
|----------------|----------------|
| Claude Code    | claude_code    |
| Claude Desktop | claude_desktop |
| ChatGPT        | chatgpt        |
| Cursor         | cursor         |
| Open WebUI     | open_webui     |

One fact remembered from three sources is a much stronger durability
signal than the same fact captured three times from one source. That is
how the compactor's scoring rewards corroboration.
