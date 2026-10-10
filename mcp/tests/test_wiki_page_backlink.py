"""get_wiki_page names the board card a page belongs to.

card_get already shows an agent a card's wiki thread. Without the reverse, an
agent reading `AI/projects/netdisco/design.md` had no way to know the page IS
the write-up for the `netdisco` card — so it would rewrite the page and leave
the card stale. The wiki UI shows a chip for this; the tool surface should say
the same thing.
"""
import httpx
import pytest
import respx

_RO = {"user_id": "u1", "tenant_slug": "acme", "wiki_read": True}

_CARD = {"slug": "netdisco", "title": "Network discovery tool",
         "column": "Building", "stage": "building", "color": "#6366F1",
         "archived": False, "via": "derived"}


@pytest.fixture
def helpers():
    import main
    return main


# ── the formatter ──────────────────────────────────────────────────────────

def test_backlink_line_names_slug_title_and_column(helpers):
    out = helpers._fmt_referring_cards([dict(_CARD)])
    assert "`netdisco`" in out
    assert "Network discovery tool" in out
    assert "Building" in out
    assert "card_get" in out, "the agent needs to be told how to read the card"


def test_no_cards_means_no_line(helpers):
    assert helpers._fmt_referring_cards([]) == ""
    assert helpers._fmt_referring_cards(None) == ""


def test_closed_card_is_flagged(helpers):
    out = helpers._fmt_referring_cards([{**_CARD, "closed": True}])
    assert "closed" in out


def test_a_card_served_under_the_old_field_name_is_still_flagged(helpers):
    """`archived` is what `closed` used to be called and is still served beside
    it. A formatter that only read the new name would silently stop flagging a
    closed card if it ever met an older payload."""
    out = helpers._fmt_referring_cards([{**_CARD, "archived": True}])
    assert "closed" in out


def test_several_cards_are_all_named(helpers):
    out = helpers._fmt_referring_cards([
        dict(_CARD), {**_CARD, "slug": "audit", "title": "Q3 audit"}])
    assert "`netdisco`" in out and "`audit`" in out
    assert "cards referencing" in out.lower()


# ── the lookup ─────────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_lookup_calls_the_editor_with_tenant_and_path(helpers):
    route = respx.post(
        "http://test-editor:8080/api/internal/board/page-cards").mock(
        return_value=httpx.Response(200, json={"cards": [dict(_CARD)]}))
    cards = await helpers._page_referring_cards("acme", "AI/projects/netdisco/design.md")
    assert [c["slug"] for c in cards] == ["netdisco"]
    import json as _json
    sent = _json.loads(route.calls.last.request.content)
    assert sent == {"tenant_slug": "acme", "path": "AI/projects/netdisco/design.md"}


@respx.mock
@pytest.mark.asyncio
async def test_lookup_failure_is_swallowed(helpers):
    """Reading a page must never fail because the back-link lookup did."""
    respx.post("http://test-editor:8080/api/internal/board/page-cards").mock(
        return_value=httpx.Response(500, text="boom"))
    assert await helpers._page_referring_cards("acme", "a.md") == []


@pytest.mark.asyncio
async def test_lookup_skipped_without_tenant_or_path(helpers):
    assert await helpers._page_referring_cards("", "a.md") == []
    assert await helpers._page_referring_cards("acme", "") == []


# ── end to end through the tool ────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_get_wiki_page_prepends_the_backlink(helpers, fake_ctx):
    respx.post("http://test-editor:8080/api/internal/wiki/read").mock(
        return_value=httpx.Response(200, json={"content": "# Design\n\nbody text"}))
    respx.post("http://test-editor:8080/api/internal/board/page-cards").mock(
        return_value=httpx.Response(200, json={"cards": [dict(_CARD)]}))

    out = await helpers.get_wiki_page("AI/projects/netdisco/design.md",
                                      ctx=fake_ctx(token_info=_RO))
    assert "`netdisco`" in out
    assert "body text" in out, "the page content still has to be there"
    # The card is named before the content, so it's read, not buried.
    assert out.index("netdisco`") < out.index("body text")


@respx.mock
@pytest.mark.asyncio
async def test_get_wiki_page_unchanged_when_no_card(helpers, fake_ctx):
    respx.post("http://test-editor:8080/api/internal/wiki/read").mock(
        return_value=httpx.Response(200, json={"content": "# Plain\n\nbody text"}))
    respx.post("http://test-editor:8080/api/internal/board/page-cards").mock(
        return_value=httpx.Response(200, json={"cards": []}))

    out = await helpers.get_wiki_page("notes/plain.md", ctx=fake_ctx(token_info=_RO))
    assert out == "# notes/plain.md\n\n# Plain\n\nbody text"


@respx.mock
@pytest.mark.asyncio
async def test_get_wiki_page_survives_a_board_outage(helpers, fake_ctx):
    respx.post("http://test-editor:8080/api/internal/wiki/read").mock(
        return_value=httpx.Response(200, json={"content": "body text"}))
    respx.post("http://test-editor:8080/api/internal/board/page-cards").mock(
        side_effect=httpx.ConnectError("refused"))

    out = await helpers.get_wiki_page("notes/plain.md", ctx=fake_ctx(token_info=_RO))
    assert "body text" in out


# ── discoverability ────────────────────────────────────────────────────────

def test_instructions_teach_that_the_link_reads_both_ways(helpers):
    """An agent only knows what the tool surface tells it — that's the whole
    reason this gap existed."""
    text = helpers.mcp.instructions
    assert "get_wiki_page` names the card" in text
    assert "BOTH ways" in text


def test_tool_docstring_mentions_the_card(helpers):
    doc = helpers.get_wiki_page.__doc__ or ""
    assert "board card" in doc.lower()
