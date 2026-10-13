# Claude Code: deterministic memory injection via `UserPromptSubmit`

Prose instructions (`CLAUDE.md`) ask Claude to search memory before
answering. A `UserPromptSubmit` hook *guarantees* it: the harness runs
the hook on every prompt and adds its stdout to the context of the next
turn, so the model can't forget.

## Setup

Set two environment variables (in `~/.zshenv` or similar). Both are
required; the hook stops with an error if either is missing.

```bash
export ELEFANT_MCP_URL="https://elefant.example.com/mcp"   # your MCP endpoint
export ELEFANT_MCP_TOKEN="<your-token>"                    # My Settings, then API tokens
```

`~/.claude/settings.json`:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "~/.claude/hooks/elefant-memory-search.sh"
          }
        ]
      }
    ]
  }
}
```

`~/.claude/hooks/elefant-memory-search.sh` (make it executable):

```bash
#!/usr/bin/env bash
# Read the user's prompt from stdin (JSON from the Claude Code harness),
# run memory_search on Elefant's MCP server, and print the result. Claude
# Code adds the output to the model's context for the upcoming turn.
set -euo pipefail

endpoint="${ELEFANT_MCP_URL:?set ELEFANT_MCP_URL, e.g. https://elefant.example.com/mcp}"
token="${ELEFANT_MCP_TOKEN:?set ELEFANT_MCP_TOKEN to an Elefant API token}"

prompt=$(jq -r '.prompt // empty')
[ -z "$prompt" ] && exit 0

headers=$(mktemp)
trap 'rm -f "$headers"' EXIT

# POST one JSON-RPC message. The server answers with plain JSON or with
# an SSE stream, so keep only the JSON payload either way. Quiet failure:
# never block the user's turn on hook problems.
rpc() {
  curl -fsS --max-time 5 -D "$headers" \
    -H "Authorization: Bearer $token" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    ${session:+-H "Mcp-Session-Id: $session"} \
    -d "$1" "$endpoint" 2>/dev/null | sed -n 's/^data: //p; t; /^{/p'
}

# The server keeps a session per client, so initialize first.
session=""
rpc '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"elefant-hook","version":"1"}}}' >/dev/null || exit 0
session=$(tr -d '\r' < "$headers" | awk -F': ' 'tolower($1)=="mcp-session-id"{print $2}')
[ -z "$session" ] && exit 0
rpc '{"jsonrpc":"2.0","method":"notifications/initialized"}' >/dev/null || true

result=$(rpc "$(jq -nc --arg q "$prompt" '{
      jsonrpc: "2.0", id: 2,
      method: "tools/call",
      params: {name: "memory_search", arguments: {query: $q, limit: 5}}
    }')" || echo '')

# End the session so the server does not keep it open.
curl -fsS --max-time 2 -X DELETE -H "Authorization: Bearer $token" \
  -H "Mcp-Session-Id: $session" "$endpoint" >/dev/null 2>&1 || true

text=$(printf '%s' "$result" | jq -r '.result.content[0].text // empty' 2>/dev/null || true)
[ -z "$text" ] && exit 0

# Emit one context block. Keep it tight so it doesn't dominate the
# context window.
echo "<elefant_memory_search>"
printf '%s' "$text" | head -c 4000
echo
echo "</elefant_memory_search>"
```

## Why this is better than prose alone

- Deterministic: the call always fires
- Token-efficient: only the top 5 results, capped at 4 KB
- Fails open: if the MCP server is down, the prompt goes through normally
- The same pattern works in any tool that supports a pre-prompt hook

## What this does *not* do

- It doesn't write memories. That still needs the model to call
  `memory_capture` (see `claude-code.md`). A `Stop` or `PostToolUse`
  hook could automate writes too, but that is a larger design decision
  than read-side injection.
