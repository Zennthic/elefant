"""The card `repo` field over MCP.

`repo` is a card's GitHub repository promoted from a line of body prose
(`repo: github.com/acme/widgets`) to a structured, filterable attribute. It
exists because several projects share one board: the field is what lets an
agent ask for one project's cards instead of reading every card's body and
hoping the convention was followed.

The properties worth holding, in the order they bite:

  1. The repo is VISIBLE on read, on every rendered card and not only on
     `card_get`. An agent that cannot see which repo a card belongs to will
     fall back to grepping the body, which is exactly what this replaces.
  2. `card_list(repo=...)` filters, and passes the value through rather than
     dropping it — the filter is the whole point of the field.
  3. Setting and CLEARING are distinguishable. A scalar has no equivalent of
     the empty list, so `card_repo` takes an explicit `clear`, and a call that
     specifies neither changes nothing and says so.
  4. `card_update(repo=...)` stays absent from the request when the caller
     didn't pass it — otherwise every title edit would strip the card's repo.
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

_SETTINGS = "http://test-editor:8080/api/internal/tenant-settings"
_CARDS = "http://test-memory-api:8002/board/cards"


def _card(**over):
    c = dict(_CARD)
    c.update(over)
    return c


def _mock_settings():
    respx.get(_SETTINGS).mock(return_value=httpx.Response(200, json={"settings": {}}))


# ── read side: the repo is visible ─────────────────────────────────────────

def test_fmt_card_shows_repo(helpers):
    out = helpers._fmt_card(_card(repo="zennthic/elefant"))
    assert "Repo:" in out
    assert "`zennthic/elefant`" in out


def test_fmt_card_omits_repo_line_when_none(helpers):
    assert "Repo:" not in helpers._fmt_card(_card())
    assert "Repo:" not in helpers._fmt_card(_card(repo=""))


def test_fmt_card_list_shows_repo(helpers):
    """Scanning a column is where a shared board earns the field: the list view
    has to say which project each card belongs to, not just the detail view."""
    out = helpers._fmt_card_list([_card(repo="zennthic/elefant"), _card(slug="other")])
    assert "zennthic/elefant" in out
    # The card with no repo renders cleanly, with no stray separator.
    assert "`other` — claude" in out


def test_fmt_card_list_renders_repo_and_labels_together(helpers):
    out = helpers._fmt_card_list([_card(repo="zennthic/elefant", labels=["bug"])])
    assert "`bug` · zennthic/elefant — claude" in out


# ── filtering ──────────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_card_list_passes_the_repo_filter_through(helpers, fake_ctx):
    route = respx.get(_CARDS).mock(
        return_value=httpx.Response(200, json={"cards": [_card()]}))

    await helpers.card_list(repo="zennthic/elefant", ctx=fake_ctx(token_info=_RW))
    assert route.calls.last.request.url.params["repo"] == "zennthic/elefant"


@respx.mock
@pytest.mark.asyncio
async def test_card_list_omits_the_repo_filter_when_not_asked(helpers, fake_ctx):
    """An always-sent empty filter would be harmless today and a bug the day
    the backend starts treating "" as "cards with no repo"."""
    route = respx.get(_CARDS).mock(
        return_value=httpx.Response(200, json={"cards": [_card()]}))

    await helpers.card_list(ctx=fake_ctx(token_info=_RW))
    assert "repo" not in route.calls.last.request.url.params


# ── create / update carry the repo ─────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_card_add_sends_repo(helpers, fake_ctx):
    _mock_settings()
    route = respx.post(_CARDS).mock(
        return_value=httpx.Response(201, json={"card": _card()}))

    await helpers.card_add("Network discovery tool", repo="zennthic/elefant",
                           ctx=fake_ctx(token_info=_RW))
    assert json.loads(route.calls.last.request.content)["repo"] == "zennthic/elefant"


@respx.mock
@pytest.mark.asyncio
async def test_card_add_defaults_repo_to_empty(helpers, fake_ctx):
    _mock_settings()
    route = respx.post(_CARDS).mock(
        return_value=httpx.Response(201, json={"card": _card()}))

    await helpers.card_add("Network discovery tool", ctx=fake_ctx(token_info=_RW))
    assert json.loads(route.calls.last.request.content)["repo"] == ""


@respx.mock
@pytest.mark.asyncio
async def test_card_update_sends_repo_only_when_given(helpers, fake_ctx):
    """The regression this guards: if `repo` were always sent, editing a card's
    title would strip the repo off it."""
    _mock_settings()
    route = respx.patch(f"{_CARDS}/netdisco").mock(
        return_value=httpx.Response(200, json={"card": _card()}))

    await helpers.card_update("netdisco", title="Renamed", ctx=fake_ctx(token_info=_RW))
    assert "repo" not in json.loads(route.calls.last.request.content)

    await helpers.card_update("netdisco", repo="zennthic/elefant",
                              ctx=fake_ctx(token_info=_RW))
    assert json.loads(route.calls.last.request.content)["repo"] == "zennthic/elefant"


# ── card_repo: set and clear ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_card_repo_requires_board_write(helpers, fake_ctx):
    out = await helpers.card_repo("netdisco", "zennthic/elefant",
                                  ctx=fake_ctx(token_info={"board_write": False}))
    assert "board_write" in out.lower()


@respx.mock
@pytest.mark.asyncio
async def test_card_repo_sets_the_repo(helpers, fake_ctx):
    respx.get(f"{_CARDS}/netdisco").mock(
        return_value=httpx.Response(200, json={"card": _card()}))
    route = respx.patch(f"{_CARDS}/netdisco").mock(
        return_value=httpx.Response(200, json={"card": _card(repo="zennthic/elefant")}))

    out = await helpers.card_repo("netdisco", "zennthic/elefant",
                                  ctx=fake_ctx(token_info=_RW))
    assert json.loads(route.calls.last.request.content)["repo"] == "zennthic/elefant"
    assert "Set repo" in out


@respx.mock
@pytest.mark.asyncio
async def test_card_repo_clears_the_repo(helpers, fake_ctx):
    """Clearing sends an explicit empty string, which the backend reads as a
    deliberate clear rather than "leave it alone"."""
    respx.get(f"{_CARDS}/netdisco").mock(
        return_value=httpx.Response(200, json={"card": _card(repo="zennthic/elefant")}))
    route = respx.patch(f"{_CARDS}/netdisco").mock(
        return_value=httpx.Response(200, json={"card": _card()}))

    out = await helpers.card_repo("netdisco", clear=True, ctx=fake_ctx(token_info=_RW))
    assert json.loads(route.calls.last.request.content)["repo"] == ""
    assert "Cleared the repo" in out
    # The message names what was lost, so the caller can put it back.
    assert "zennthic/elefant" in out


@respx.mock
@pytest.mark.asyncio
async def test_card_repo_clearing_nothing_says_so(helpers, fake_ctx):
    """The wiki_edit lesson: never report success when nothing happened."""
    respx.get(f"{_CARDS}/netdisco").mock(
        return_value=httpx.Response(200, json={"card": _card()}))
    route = respx.patch(f"{_CARDS}/netdisco").mock(
        return_value=httpx.Response(200, json={"card": _card()}))

    out = await helpers.card_repo("netdisco", clear=True, ctx=fake_ctx(token_info=_RW))
    assert "no repo" in out.lower()
    assert not route.called          # no pointless write


@respx.mock
@pytest.mark.asyncio
async def test_card_repo_with_neither_repo_nor_clear_changes_nothing(helpers, fake_ctx):
    """A scalar field has no empty-list equivalent, so an omitted value must
    NOT be read as "clear it" — that would make a typo destructive."""
    route = respx.patch(f"{_CARDS}/netdisco").mock(
        return_value=httpx.Response(200, json={"card": _card()}))

    out = await helpers.card_repo("netdisco", ctx=fake_ctx(token_info=_RW))
    assert "clear=true" in out.lower()
    assert not route.called


@respx.mock
@pytest.mark.asyncio
async def test_card_repo_refuses_set_and_clear_at_once(helpers, fake_ctx):
    """Two contradictory instructions get an error, not a guess about which one
    the caller meant."""
    route = respx.patch(f"{_CARDS}/netdisco").mock(
        return_value=httpx.Response(200, json={"card": _card()}))

    out = await helpers.card_repo("netdisco", "zennthic/elefant", clear=True,
                                  ctx=fake_ctx(token_info=_RW))
    assert "not both" in out.lower()
    assert not route.called


@respx.mock
@pytest.mark.asyncio
async def test_card_repo_reports_an_unknown_card(helpers, fake_ctx):
    respx.get(f"{_CARDS}/nope").mock(return_value=httpx.Response(404, json={}))

    out = await helpers.card_repo("nope", "zennthic/elefant", ctx=fake_ctx(token_info=_RW))
    assert "no card matches" in out.lower()


@respx.mock
@pytest.mark.asyncio
async def test_card_repo_surfaces_a_rejected_value(helpers, fake_ctx):
    """The backend is the authority on what a repo reference is. Its complaint
    has to reach the agent, or a refused write reads as a successful one."""
    respx.get(f"{_CARDS}/netdisco").mock(
        return_value=httpx.Response(200, json={"card": _card()}))
    respx.patch(f"{_CARDS}/netdisco").mock(
        return_value=httpx.Response(400, json={"detail": "'nonsense' is not a GitHub repo reference."}))

    out = await helpers.card_repo("netdisco", "nonsense", ctx=fake_ctx(token_info=_RW))
    assert "not a github repo reference" in out.lower()


@respx.mock
@pytest.mark.asyncio
async def test_card_repo_says_when_the_stored_value_was_normalized(helpers, fake_ctx):
    """A caller that pastes a browser URL gets back `owner/name`. Saying so
    stops it looking like the write landed on the wrong value."""
    respx.get(f"{_CARDS}/netdisco").mock(
        return_value=httpx.Response(200, json={"card": _card()}))
    respx.patch(f"{_CARDS}/netdisco").mock(
        return_value=httpx.Response(200, json={"card": _card(repo="zennthic/elefant")}))

    out = await helpers.card_repo("netdisco", "https://github.com/zennthic/elefant",
                                  ctx=fake_ctx(token_info=_RW))
    assert "normalized" in out.lower()
    assert "`zennthic/elefant`" in out


@respx.mock
@pytest.mark.asyncio
async def test_card_repo_stays_quiet_when_no_normalization_happened(helpers, fake_ctx):
    respx.get(f"{_CARDS}/netdisco").mock(
        return_value=httpx.Response(200, json={"card": _card()}))
    respx.patch(f"{_CARDS}/netdisco").mock(
        return_value=httpx.Response(200, json={"card": _card(repo="zennthic/elefant")}))

    out = await helpers.card_repo("netdisco", "zennthic/elefant",
                                  ctx=fake_ctx(token_info=_RW))
    assert "normalized" not in out.lower()
