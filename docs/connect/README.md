# Connect your AI client

This page connects an AI assistant to Elefant. Every client uses the same MCP
endpoint, and each section below gives the config to paste, where the token
comes from, and the base prompt to add.

The examples use `https://elefant.example.com`. Put your own Elefant address in
its place.

- [Before you start](#before-you-start)
- [Claude on the web and on mobile (OAuth)](#claude-on-the-web-and-on-mobile-oauth)
- [Claude Desktop](#claude-desktop)
- [Claude Code](#claude-code)
- [ChatGPT](#chatgpt)
- [Cursor](#cursor)
- [Codex](#codex)
- [Open WebUI](#open-webui)
- [Check that it works](#check-that-it-works)
- [When it does not connect](#when-it-does-not-connect)

## Before you start

**The endpoint.** It is your Elefant address with `/mcp` added:

```
https://elefant.example.com/mcp
```

The transport is Streamable HTTP. Use HTTPS for any client that is not on the
same machine. The [install guides](../install/) show how to put TLS in front of
Elefant.

**How the client signs in.** There are two ways:

| Way | Clients | What you do |
|---|---|---|
| OAuth | claude.ai on the web, the Claude mobile apps, and other clients that add a remote connector by URL | Give the client the URL, then sign in to Elefant when it asks. |
| Personal API token | Claude Desktop, Claude Code, Cursor, Codex, Open WebUI, scripts | Create a token in Elefant and paste it into the client's config. |

**Create a personal API token.**

1. Sign in to Elefant.
2. Open **My Settings**: select your name at the top right.
3. In **API tokens**, select **+ New token**.
4. Give the token the name of the client and the machine, for example
   `Claude Code, laptop`. Use one token for each client, so that you can revoke
   one without the others.
5. Choose the permissions:
   - **Wiki read**, **Memory read** and **Board read** are on by default.
   - Turn on **Memory write**. It is off by default, and without it the
     assistant cannot capture anything.
   - **Wiki write** lets the assistant write pages, but only inside the org's AI
     folder (`AI/` by default).
   - Turn on **Board write** if the assistant should add and move cards.
6. Copy the token. Elefant shows it only once. If you lose it, delete it and
   create a new one.

The client sends the token in a header:

```
Authorization: Bearer <your-token>
```

A token is a password. Do not commit it to a repository, and do not paste it
into a chat.

**The base prompt.** Clients put their own system prompt ahead of the
instructions that the MCP server sends, so an assistant often forgets to search
and capture. The snippets in [`base-prompts/`](../../base-prompts/README.md)
repeat that behaviour at the client level. Each section below names the one to
use. [`base-prompts/README.md`](../../base-prompts/README.md) also explains
when the assistant should use `memory_capture` and when `memory_add`.

## Claude on the web and on mobile (OAuth)

claude.ai and the Claude apps for iOS and Android use OAuth. You do not need a
token.

1. In Claude, open **Settings**, then **Connectors**.
2. Add a custom connector, and enter `https://elefant.example.com/mcp` as its
   URL.
3. Claude opens Elefant's sign-in page. Sign in with your Elefant account and
   your MFA code, choose the permissions, and select **Authorise**.
4. You return to Claude, and the connector is active.

A connector that you add on the web is also available in the mobile apps.

To see or change what a connector can do, open **My Settings** in Elefant and
go to **OAuth sessions**. **Edit** adds or removes a permission, for example
wiki write, without pairing again. **Revoke** ends the session, and Claude asks
you to sign in again the next time it uses Elefant.

**Base prompt:** add the snippet from
[`base-prompts/claude-desktop.md`](../../base-prompts/claude-desktop.md) to a
Project's instructions, or to your profile preferences.

## Claude Desktop

Claude Desktop reads its servers from `claude_desktop_config.json`:

| System | File |
|---|---|
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |

1. Create a personal API token (see [Before you start](#before-you-start)).
2. Merge the `mcpServers` entry from
   [`mcp/examples/claude-desktop.json`](../../mcp/examples/claude-desktop.json)
   into that file. Change the URL to your own, and put your token after
   `Bearer `.
3. Restart Claude Desktop.

You can also add Elefant to Claude Desktop as a connector by URL, the same way
as on the web. Then it uses OAuth and does not need a token.

**Base prompt:**
[`base-prompts/claude-desktop.md`](../../base-prompts/claude-desktop.md), in
a Project's instructions or your profile preferences.

## Claude Code

The quickest way is the installer in this repository. It asks for a token,
adds the MCP server for your user, and installs the Elefant skill
([`skill/SKILL.md`](../../skill/SKILL.md)), which teaches Claude Code when to
search, capture and file cards:

```bash
make install-claude ELEFANT_URL=https://elefant.example.com
```

To do the same by hand:

```bash
claude mcp add --scope user --transport http elefant \
  https://elefant.example.com/mcp \
  --header "Authorization: Bearer <your-token>"
```

Restart Claude Code, then run `/mcp` to see the server and `/skills` to see the
skill.

If you put the server in a project's `.mcp.json` instead, the token is in the
repository's working tree. Add the file to `.gitignore`.

**Base prompt:** [`base-prompts/claude-code.md`](../../base-prompts/claude-code.md),
in `~/.claude/CLAUDE.md` for every project or `./CLAUDE.md` for one.
[`base-prompts/claude-code-hook.md`](../../base-prompts/claude-code-hook.md)
describes an optional hook that reminds Claude Code on every prompt.

## ChatGPT

ChatGPT connects to a remote MCP server as a custom connector, on the plans
that allow custom connectors. The menu names change between ChatGPT releases,
so look for these steps in your version:

1. In ChatGPT, open **Settings**, then **Connectors** (or **Apps and
   Connectors**). If your plan requires it, turn on developer mode there.
2. Create a connector. Enter `https://elefant.example.com/mcp` as its URL, and
   choose OAuth as the authentication.
3. ChatGPT opens Elefant's sign-in page. Sign in and select **Authorise**.
4. Turn the connector on in a chat.

Elefant registers OAuth clients automatically (RFC 7591), so you do not create
a client ID first.

**Base prompt:** [`base-prompts/chatgpt.md`](../../base-prompts/chatgpt.md),
in **Settings**, **Personalization**, **Custom instructions**, or in the
instructions of a GPT that uses the connector.

## Cursor

1. Create a personal API token (see [Before you start](#before-you-start)).
2. Copy [`mcp/examples/cursor.json`](../../mcp/examples/cursor.json) to
   `~/.cursor/mcp.json` for every project, or to `.cursor/mcp.json` in one
   project. Change the URL to your own, and put your token after `Bearer `.
3. In Cursor, open **Settings**, then **MCP**, and turn the server on.

Agent mode uses the tools without being asked. In chat, turn the tools on from
the tools icon in the input bar.

**Base prompt:** [`base-prompts/cursor.md`](../../base-prompts/cursor.md), as
`.cursor/rules/elefant-memory.mdc`.

## Codex

The installer in this repository asks for a token, adds the MCP server to
Codex, and installs the Elefant skill:

```bash
make install-codex ELEFANT_URL=https://elefant.example.com
```

The token is stored in `~/.codex/config.toml` (or `$CODEX_HOME/config.toml`),
and the installer sets that file's mode to 600. If an `elefant` server is
already set up, the installer keeps it and installs only the skill.

To do it by hand, add the server with `codex mcp add`, and give it the token
in an environment variable:

```bash
export ELEFANT_MCP_TOKEN=<your-token>
codex mcp add elefant --url https://elefant.example.com/mcp \
  --bearer-token-env-var ELEFANT_MCP_TOKEN
```

Restart Codex, then run `/mcp` to see the server and `/skills` to see the
skill.

**Base prompt:** the skill does this job for Codex. You can also put
[`base-prompts/claude-code.md`](../../base-prompts/claude-code.md) in your
`AGENTS.md`.

## Open WebUI

Open WebUI v0.6.31 or later can use MCP tool servers.

1. Create a personal API token (see [Before you start](#before-you-start)).
2. As an Open WebUI administrator, open **Admin Panel**, **Settings**,
   **Tools**, and add a tool server with:
   - URL `https://elefant.example.com/mcp`
   - transport Streamable HTTP
   - your token as the bearer token.

[`mcp/examples/open-webui.md`](../../mcp/examples/open-webui.md) has the full
steps, and a fix for the `406 Not Acceptable` error that Open WebUI can get
when it does not go through Elefant's nginx.

**Base prompt:** the Claude Desktop snippet,
[`base-prompts/claude-desktop.md`](../../base-prompts/claude-desktop.md),
works as the system prompt of an Open WebUI model.

## Check that it works

Ask the assistant: "Search Elefant for anything about this project." It should
call the `search` tool and say what it found, or say that it found nothing.

Then tell it a fact and ask it to remember it. It should call
`memory_capture`. Captures go into the hot tier first, and the compactor moves
them into durable memory in the background. An org admin can see the hot tier
in **Settings**, **Hot Tier**.

## When it does not connect

| What you see | What to do |
|---|---|
| `401` or "unauthorized" | The token is wrong, deleted or expired. Create a new one in **My Settings**, **API tokens**, and paste it again. Make sure the header reads `Bearer <token>`, with one space. |
| `403`, or a tool says it is not permitted | The token or the OAuth session does not have the permission. Edit it in **My Settings**. |
| `406 Not Acceptable` | The client is not sending `Accept: text/event-stream`. Send it through Elefant's nginx, which adds the header. |
| The OAuth sign-in page never opens | The client must reach Elefant over HTTPS at the address it was given. Check `ELEFANT_PUBLIC_DOMAIN` and your TLS proxy. |
| The tools are there, but the assistant never uses them | Add the base prompt for your client. |

[`TROUBLESHOOTING.md`](../../TROUBLESHOOTING.md) covers the server side.
