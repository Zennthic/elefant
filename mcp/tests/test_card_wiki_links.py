"""Card ↔ wiki-page linking over MCP.

The board UI has always let a human link a wiki page to a card, and the field
(`cards.wiki_links`) round-trips through memory-api — but the MCP tools neither
accepted nor displayed it, so agents pasted page URLs into the card body
instead. These cover the agent-facing surface: links are visible on read,
settable at create/update, and `card_link_wiki` edits the list in place without
clobbering links somebody else added.
"""
import json

import httpx
import pytest
import respx


@pytest.fixture
def helpers():
    import main
    return main


_RW = {"user_id": "u1", "tenant_slug": "acme", "source": "claude",
       "board_read": True, "board_write": True}

_CARD = {"title": "Network discovery tool", "slug": "netdisco",
         "column": "Backlog", "stage": "spark", "source": "claude",
         "aliases": [], "body": "an idea"}


def _card(**over):
    c = dict(_CARD)
    c.update(over)
    return c


# ── read side: links are visible ───────────────────────────────────────────

def test_fmt_card_shows_linked_wiki_pages(helpers):
    out = helpers._fmt_card(_card(wiki_links=["AI/projects/netdisco/design.md"]))
    assert "AI/projects/netdisco/design.md" in out


def test_fmt_card_omits_link_line_when_none(helpers):
    out = helpers._fmt_card(_card())
    assert "Linked wiki pages" not in out


# ── write side: create / update carry links ────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_card_add_sends_wiki_links(helpers, fake_ctx):
    respx.get("http://test-editor:8080/api/internal/tenant-settings").mock(
        return_value=httpx.Response(200, json={"settings": {}}))
    route = respx.post("http://test-memory-api:8002/board/cards").mock(
        return_value=httpx.Response(201, json={"card": _card()}))

    await helpers.card_add("Network discovery tool",
                           wiki_links=["AI/projects/netdisco/design.md"],
                           ctx=fake_ctx(token_info=_RW))
    sent = json.loads(route.calls.last.request.content)
    assert sent["wiki_links"] == ["AI/projects/netdisco/design.md"]


@respx.mock
@pytest.mark.asyncio
async def test_card_update_sends_wiki_links_only_when_given(helpers, fake_ctx):
    respx.get("http://test-editor:8080/api/internal/tenant-settings").mock(
        return_value=httpx.Response(200, json={"settings": {}}))
    route = respx.patch("http://test-memory-api:8002/board/cards/netdisco").mock(
        return_value=httpx.Response(200, json={"card": _card()}))

    await helpers.card_update("netdisco", title="Renamed", ctx=fake_ctx(token_info=_RW))
    assert "wiki_links" not in json.loads(route.calls.last.request.content)

    await helpers.card_update("netdisco", wiki_links=["a.md"], ctx=fake_ctx(token_info=_RW))
    assert json.loads(route.calls.last.request.content)["wiki_links"] == ["a.md"]


# ── card_link_wiki ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_card_link_wiki_requires_board_write(helpers, fake_ctx):
    out = await helpers.card_link_wiki("netdisco", "a.md",
                                       ctx=fake_ctx(token_info={"board_write": False}))
    assert "board_write" in out.lower()


@respx.mock
@pytest.mark.asyncio
async def test_card_link_wiki_appends_without_clobbering(helpers, fake_ctx):
    """The safety property: linking one page must preserve links already on the
    card — including any a human added in the board UI."""
    respx.get("http://test-memory-api:8002/board/cards/netdisco").mock(
        return_value=httpx.Response(200, json={
            "card": _card(wiki_links=["AI/notes/from-a-human.md"])}))
    route = respx.patch("http://test-memory-api:8002/board/cards/netdisco").mock(
        return_value=httpx.Response(200, json={"card": _card()}))

    out = await helpers.card_link_wiki("netdisco", "AI/projects/netdisco/design.md",
                                       ctx=fake_ctx(token_info=_RW))
    sent = json.loads(route.calls.last.request.content)
    assert sent["wiki_links"] == ["AI/notes/from-a-human.md",
                                  "AI/projects/netdisco/design.md"]
    assert "Linked" in out


@respx.mock
@pytest.mark.asyncio
async def test_card_link_wiki_is_idempotent(helpers, fake_ctx):
    respx.get("http://test-memory-api:8002/board/cards/netdisco").mock(
        return_value=httpx.Response(200, json={"card": _card(wiki_links=["a.md"])}))
    route = respx.patch("http://test-memory-api:8002/board/cards/netdisco").mock(
        return_value=httpx.Response(200, json={"card": _card()}))

    out = await helpers.card_link_wiki("netdisco", "a.md", ctx=fake_ctx(token_info=_RW))
    assert "already linked" in out.lower()
    assert not route.called          # no pointless write


@respx.mock
@pytest.mark.asyncio
async def test_card_link_wiki_unlinks(helpers, fake_ctx):
    respx.get("http://test-memory-api:8002/board/cards/netdisco").mock(
        return_value=httpx.Response(200, json={
            "card": _card(wiki_links=["a.md", "b.md"])}))
    route = respx.patch("http://test-memory-api:8002/board/cards/netdisco").mock(
        return_value=httpx.Response(200, json={"card": _card()}))

    out = await helpers.card_link_wiki("netdisco", "a.md", unlink=True,
                                       ctx=fake_ctx(token_info=_RW))
    assert json.loads(route.calls.last.request.content)["wiki_links"] == ["b.md"]
    assert "Unlinked" in out


@respx.mock
@pytest.mark.asyncio
async def test_card_link_wiki_unlink_missing_is_reported(helpers, fake_ctx):
    respx.get("http://test-memory-api:8002/board/cards/netdisco").mock(
        return_value=httpx.Response(200, json={"card": _card(wiki_links=["b.md"])}))
    route = respx.patch("http://test-memory-api:8002/board/cards/netdisco").mock(
        return_value=httpx.Response(200, json={"card": _card()}))

    out = await helpers.card_link_wiki("netdisco", "a.md", unlink=True,
                                       ctx=fake_ctx(token_info=_RW))
    assert "not linked" in out.lower()
    assert not route.called


@respx.mock
@pytest.mark.asyncio
async def test_card_link_wiki_unknown_card(helpers, fake_ctx):
    respx.get("http://test-memory-api:8002/board/cards/nope").mock(
        return_value=httpx.Response(404, json={"detail": "not found"}))
    out = await helpers.card_link_wiki("nope", "a.md", ctx=fake_ctx(token_info=_RW))
    assert "No card matches" in out


@respx.mock
@pytest.mark.asyncio
async def test_card_link_wiki_normalises_leading_slash(helpers, fake_ctx):
    respx.get("http://test-memory-api:8002/board/cards/netdisco").mock(
        return_value=httpx.Response(200, json={"card": _card(wiki_links=[])}))
    route = respx.patch("http://test-memory-api:8002/board/cards/netdisco").mock(
        return_value=httpx.Response(200, json={"card": _card()}))

    await helpers.card_link_wiki("netdisco", "/AI/projects/netdisco/design.md",
                                 ctx=fake_ctx(token_info=_RW))
    assert json.loads(route.calls.last.request.content)["wiki_links"] == [
        "AI/projects/netdisco/design.md"]


@pytest.mark.asyncio
async def test_card_link_wiki_rejects_empty_path(helpers, fake_ctx):
    out = await helpers.card_link_wiki("netdisco", "   ", ctx=fake_ctx(token_info=_RW))
    assert "path" in out.lower()


# ── card_get: the full wiki thread (derived + linked) ──────────────────────

def test_fmt_card_prefers_enriched_thread_over_raw_links(helpers):
    """card_get carries the editor's enriched view; the write tools carry only
    memory-api's raw wiki_links. Render whichever is present."""
    out = helpers._fmt_card(_card(
        wiki_links=["AI/notes/x.md"],
        wiki_pages=[
            {"path": "AI/projects/netdisco/design.md", "kind": "derived"},
            {"path": "AI/notes/x.md", "kind": "linked"},
            {"path": "AI/gone.md", "kind": "linked", "missing": True},
        ]))
    assert "AI/projects/netdisco/design.md" in out and "(auto)" in out
    assert "(linked)" in out
    assert "missing" in out
    # The raw-links fallback line must not double up.
    assert "Linked wiki pages" not in out


@respx.mock
@pytest.mark.asyncio
async def test_card_get_enriches_with_wiki_thread(helpers, fake_ctx):
    respx.get("http://test-memory-api:8002/board/cards/netdisco").mock(
        return_value=httpx.Response(200, json={"card": _card(wiki_links=["AI/notes/x.md"])}))
    thread = respx.post("http://test-editor:8080/api/internal/board/wiki-pages").mock(
        return_value=httpx.Response(200, json={"wiki_pages": [
            {"path": "AI/projects/netdisco/design.md", "kind": "derived"}]}))

    out = await helpers.card_get("netdisco", ctx=fake_ctx(token_info=_RW))
    assert thread.called
    sent = json.loads(thread.calls.last.request.content)
    assert sent["slug"] == "netdisco"
    assert sent["wiki_links"] == ["AI/notes/x.md"]
    # The convention-associated page is now visible to the agent.
    assert "AI/projects/netdisco/design.md" in out


@respx.mock
@pytest.mark.asyncio
async def test_card_get_survives_thread_lookup_failure(helpers, fake_ctx):
    """A card read must not fail because the enrichment call did."""
    respx.get("http://test-memory-api:8002/board/cards/netdisco").mock(
        return_value=httpx.Response(200, json={"card": _card(wiki_links=["AI/notes/x.md"])}))
    respx.post("http://test-editor:8080/api/internal/board/wiki-pages").mock(
        return_value=httpx.Response(500, json={"detail": "boom"}))

    out = await helpers.card_get("netdisco", ctx=fake_ctx(token_info=_RW))
    assert "netdisco" in out
    # Falls back to the raw links rather than hiding them.
    assert "AI/notes/x.md" in out
