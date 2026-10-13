# Open WebUI MCP Setup

## Requirements

Open WebUI **v0.6.31 or later** is required for MCP (Model Context Protocol) tool support.

---

## Steps

1. Log in to Open WebUI as an administrator.
2. Navigate to **Admin Panel → Settings → Tools**.
3. Click **+ Add Tool Server** (or the "MCP Servers" section in your version).
4. Fill in the form:
   - **Name:** `Knowledge Base`
   - **URL:** `https://your-domain.example.com/mcp`
   - **Transport:** Streamable HTTP (default in v0.6.31+)
   - **API Key / Bearer Token:** `<your-token-here>` (Open WebUI sends this as `Authorization: Bearer <token>`)
5. Click **Save** / **Connect**.
6. Open WebUI negotiates the MCP session and lists all available tools.

---

## Verifying the Connection

After saving, the tool list should show all tools. Example subset here:

| Tool | Description |
|------|-------------|
| `search` | Default: combined wiki + memory search (use this first) |
| `wiki_search_only` | Semantic search over wiki documents only |
| `memory_list` | List stored memories |
| `memory_search` | Semantic search over memories only |
| `memory_add` | Store a new memory |
| `memory_update` | Update an existing memory |
| `memory_delete` | Delete a memory |
| `memory_events` | View the structured event log |
| `reindex_status` | Check index health and progress |
| `wiki_reindex` | Trigger a full wiki reindex |

---

## Accept Header Note

Open WebUI sends `Accept: application/json`, but the FastMCP Streamable HTTP
transport also requires `text/event-stream` in the Accept header (MCP spec
2025-03-26).

If your reverse proxy (Traefik, nginx, etc.) injects
`Accept: application/json, text/event-stream` at the proxy layer, this is handled
transparently. Otherwise — for example when hitting the MCP server directly on
its port — Open WebUI may receive a `406 Not Acceptable`; configure your proxy
to add the header.

---

## Token Permissions

Generate a token in My Settings → API tokens. For Open WebUI use,
a typical token should have:

- **Memory Read:** ✅
- **Memory Write:** ✅ (if you want the model to store memories)
- **Wiki Read:** ✅
- **Wiki Write:** ❌ (unless you specifically want reindex access)
- **Label Scope:** Leave empty (unrestricted) or set to your project tags
- **Classification Access:** Grant PHI/PII/Sensitive only if required

---

## Using in a Chat

In a chat session, enable the Knowledge Base tool server by clicking the tools icon.
The model will automatically call tools when relevant, or you can prompt explicitly:

- "Search the knowledge base for VPN configuration"
- "Remember that we decided to use PostgreSQL for the events table"
- "What memories do we have tagged with project-x?"
