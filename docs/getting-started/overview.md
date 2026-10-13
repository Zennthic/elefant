# Your first ten minutes

This page is for the first person who signs in to a new Elefant. It assumes
that Elefant is running. If it is not, start with an install guide in the
[documentation index](../index.md#install).

The examples use `http://localhost:8321`, the address of a default Docker
Compose install. Use your own address if it is different.

## 1. Create the first account (1 minute)

Open `http://localhost:8321/setup` and enter a username and a password. When you
sign in, Elefant asks you to set up MFA with an authenticator app. MFA is
required for every account.

The first account is the org admin. You invite other people later from
**Settings**, **Users**.

## 2. Write a wiki page (2 minutes)

Open the **Wiki** tab.

1. Create a page, for example `projects/garden.md`. The file tree on the left
   shows folders and pages, and you can create and move both from it.
2. Write a few lines. The editor has three modes: **Rich**, **Split** and
   **Markdown**. The toolbar adds headings, lists, tables, links to other wiki
   pages, and images.
3. Press `Ctrl+S` (`Cmd+S` on a Mac) or select **Save**.

Elefant keeps the history of every page, so you can see and restore earlier
versions. It indexes the page for semantic search a few seconds
after you save it.

Agents can write pages too, but only inside the org's AI folder (`AI/` by
default). A page that a person moves out of that folder becomes read-only to
agents.

## 3. Put a card on the board (2 minutes)

Open the **Board** tab. The board is a Kanban board for work and ideas.

1. Select **+ New card**, give it a title, and select **Add card**.
2. Drag the card to another column. Columns are yours to name and reorder.
3. Open the card. You can write a description, add comments and attachments,
   add labels, link it to other cards (for example "blocks" or "blocked by"),
   and link it to wiki pages.

The board updates live, so you see a change that another person or an agent
makes without reloading. Agents work the board with the MCP `card_*` tools.
When you tell an assistant "note to self", it files a card.

## 4. Connect an AI assistant (3 minutes)

Follow [Connect your AI client](../connect/README.md) for your client. The
short version:

- **claude.ai, or the Claude mobile apps:** add a custom connector with the
  URL `https://<your-host>/mcp`, and sign in to Elefant when Claude asks.
- **Claude Desktop, Claude Code, Cursor, Codex or Open WebUI:** create a token
  in **My Settings** (your name, top right), **API tokens**, **+ New token**.
  Turn on **Memory write** so that the assistant can capture. Then paste the
  token into the client's config.

Then add the client's [base prompt](../../base-prompts/README.md), so that the
assistant searches Elefant before it answers and captures what it learns.

## 5. Watch it remember (2 minutes)

In your AI client:

1. Ask: "Search Elefant for my garden project." The assistant calls `search`
   and finds the page that you wrote in step 2.
2. Tell it something new, for example "The tomatoes go in after the last
   frost." It calls `memory_capture`.
3. Start a new conversation, maybe in another client, and ask about the
   tomatoes.

Captures go into a hot tier first. A background compactor groups them, scores
how often a fact comes back, from how many sources and how recently, and moves
the facts that pass into durable memory. The **Memories** tab shows durable
memory. Use `memory_add`, or say "remember this exactly", when a fact must go
straight into durable memory.

## Next

- Use **Search** to search the wiki, memories and git sources together.
- An org admin can add a git repository in **Settings**, **Sources**. Its
  Markdown is then searchable next to the wiki.
- Back up the wiki and memories from **Settings**, **Backup & Restore**.
- Read the [user guide](../onboarding/user-guide.html) and the
  [admin guide](../onboarding/admin-guide.html) for more detail.
