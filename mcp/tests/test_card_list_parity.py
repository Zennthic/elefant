"""Filter parity between the board UI and the agent-facing `card_list`.

The board-filtering card states the requirement plainly: what a human can
slice, the dispatch routine should be able to query too. Without that, an
agent's only way to find a card outside its filters is to list the whole board
and read it — which is the "grep the body and hope" pattern the structured
fields exist to remove, one layer along.

`tag` is the sharpest case, and now also the oldest. It named a second string
array that carried the board's OWN markers — `note-to-self`, set at capture
time when the user says that phrase — which was unreachable from the surface
that WRITES it until this filter existed.

That array has since been merged into `labels`, and `tag=` kept: it is
documented in the published skill, so it is live in every session running a
copy of it, and a filter that stopped answering would break agents that never
asked to be upgraded. The assertions below are therefore a COMPATIBILITY
contract rather than a description of the data model — `tag=` and `label=` now
slice the same field, and `label=` is the name to reach for.
"""
import inspect

import httpx
import pytest
import respx


@pytest.fixture
def helpers():
    import main
    return main


_RW = {"user_id": "u1", "tenant_slug": "acme", "source": "claude",
       "board_read": True, "board_write": True}
_CARDS = "http://test-memory-api:8002/board/cards"


@respx.mock
@pytest.mark.asyncio
async def test_an_agent_can_list_the_users_notes_to_self(helpers, fake_ctx):
    """The marker the board sets itself, from the tool that sets it. Still
    reachable under its pre-merge name, which is the point of keeping it."""
    route = respx.get(_CARDS).mock(return_value=httpx.Response(200, json={"cards": []}))

    await helpers.card_list(tag="note-to-self", ctx=fake_ctx(token_info=_RW))
    assert route.calls.last.request.url.params["tag"] == "note-to-self"


@respx.mock
@pytest.mark.asyncio
async def test_an_agent_can_search_the_board_by_free_text(helpers, fake_ctx):
    """For when you know roughly what a card said but not its slug."""
    route = respx.get(_CARDS).mock(return_value=httpx.Response(200, json={"cards": []}))

    await helpers.card_list(search="fan curve", ctx=fake_ctx(token_info=_RW))
    assert route.calls.last.request.url.params["search"] == "fan curve"


@respx.mock
@pytest.mark.asyncio
async def test_the_new_filters_are_omitted_when_not_asked(helpers, fake_ctx):
    """An always-sent empty filter is harmless today and a bug the day the
    backend starts reading "" as "cards with no label"."""
    route = respx.get(_CARDS).mock(return_value=httpx.Response(200, json={"cards": []}))

    await helpers.card_list(ctx=fake_ctx(token_info=_RW))
    params = route.calls.last.request.url.params
    assert "tag" not in params
    assert "search" not in params


def test_card_list_offers_every_dimension_the_board_api_does():
    """The parity requirement itself, checked rather than remembered.

    Both signatures are written by hand in different services, so a dimension
    added to the API and forgotten here is invisible: nothing errors, the agent
    simply cannot ask a question a human can. Reads memory-api's signature
    where it is checked out beside this service, and skips rather than failing
    where it is not — this is a parity check, not a reason to fail mcp's suite
    in isolation.
    """
    from pathlib import Path
    import main
    api = Path(main.__file__).resolve().parents[1] / "memory-api" / "main.py"
    if not api.exists():
        pytest.skip("memory-api is not checked out beside mcp")
    src = api.read_text()
    block = src.split('@app.get("/board/cards")', 1)[1].split("):", 1)[0]
    theirs = {ln.split(":")[0].strip() for ln in block.splitlines() if ":" in ln}
    # Not dimensions: the tenant is implied by the agent's token, and the
    # function's own name is not a parameter.
    theirs -= {"tenant_slug", "def card_list("}
    theirs = {t for t in theirs if t.isidentifier()}
    ours = set(inspect.signature(main.card_list).parameters) - {"ctx"}
    missing = sorted(theirs - ours)
    assert not missing, (
        f"the board API can filter by {missing} and card_list cannot, so an "
        f"agent cannot ask a question a human can. Add them to card_list.")
