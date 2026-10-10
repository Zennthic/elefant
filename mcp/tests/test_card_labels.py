"""Card labels over MCP.

Labels are free-form classification on a board card — `bug`, `enhancement`, a
repo, a sprint. Before this the only classification an agent could apply was
baking a word into the title or body, which is neither filterable nor visible
as a chip.

The properties worth holding, in the order they bite:

  1. Labels are VISIBLE on read. A classification an agent cannot see is one it
     cannot act on, so `_fmt_card` renders them everywhere a card is rendered,
     not only on `card_get`.
  2. `card_label` edits the list IN PLACE. This is the same safety property as
     `card_link_wiki`: a human labels a card in the board UI, an agent adds a
     second label, and the human's label must survive. A read-modify-write that
     sent only the new label would silently drop it.
  3. `card_update(labels=...)` is a deliberate FULL replace, and stays absent
     from the request when the caller didn't pass it — otherwise every title
     edit would quietly clear the card's labels.
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


# ── read side: labels are visible ──────────────────────────────────────────

def test_fmt_card_shows_labels(helpers):
    out = helpers._fmt_card(_card(labels=["bug", "elefant"]))
    assert "Labels:" in out
    assert "`bug`" in out and "`elefant`" in out


def test_fmt_card_omits_label_line_when_none(helpers):
    assert "Labels:" not in helpers._fmt_card(_card())
    assert "Labels:" not in helpers._fmt_card(_card(labels=[]))


def test_fmt_card_list_shows_labels(helpers):
    """Scanning a column is where classification earns its keep, so the list
    view carries labels too — not just the single-card view."""
    out = helpers._fmt_card_list([_card(labels=["bug"]), _card(slug="other")])
    assert "`bug`" in out
    # The unlabelled card renders cleanly, with no stray backticks or spacing.
    assert "`other` — claude" in out


# ── write side: create / update carry labels ───────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_card_add_sends_labels(helpers, fake_ctx):
    respx.get("http://test-editor:8080/api/internal/tenant-settings").mock(
        return_value=httpx.Response(200, json={"settings": {}}))
    route = respx.post("http://test-memory-api:8002/board/cards").mock(
        return_value=httpx.Response(201, json={"card": _card()}))

    await helpers.card_add("Network discovery tool", labels=["bug", "elefant"],
                           ctx=fake_ctx(token_info=_RW))
    assert json.loads(route.calls.last.request.content)["labels"] == ["bug", "elefant"]


@respx.mock
@pytest.mark.asyncio
async def test_card_add_defaults_labels_to_empty(helpers, fake_ctx):
    respx.get("http://test-editor:8080/api/internal/tenant-settings").mock(
        return_value=httpx.Response(200, json={"settings": {}}))
    route = respx.post("http://test-memory-api:8002/board/cards").mock(
        return_value=httpx.Response(201, json={"card": _card()}))

    await helpers.card_add("Network discovery tool", ctx=fake_ctx(token_info=_RW))
    assert json.loads(route.calls.last.request.content)["labels"] == []


@respx.mock
@pytest.mark.asyncio
async def test_card_update_sends_labels_only_when_given(helpers, fake_ctx):
    """The regression this guards: if `labels` were always sent, editing a
    card's title would clear every label on it."""
    respx.get("http://test-editor:8080/api/internal/tenant-settings").mock(
        return_value=httpx.Response(200, json={"settings": {}}))
    route = respx.patch("http://test-memory-api:8002/board/cards/netdisco").mock(
        return_value=httpx.Response(200, json={"card": _card()}))

    await helpers.card_update("netdisco", title="Renamed", ctx=fake_ctx(token_info=_RW))
    assert "labels" not in json.loads(route.calls.last.request.content)

    await helpers.card_update("netdisco", labels=["bug"], ctx=fake_ctx(token_info=_RW))
    assert json.loads(route.calls.last.request.content)["labels"] == ["bug"]


@respx.mock
@pytest.mark.asyncio
async def test_card_update_can_clear_labels(helpers, fake_ctx):
    """An explicit empty list is a deliberate clear, and must reach the backend
    rather than being folded into "not given"."""
    respx.get("http://test-editor:8080/api/internal/tenant-settings").mock(
        return_value=httpx.Response(200, json={"settings": {}}))
    route = respx.patch("http://test-memory-api:8002/board/cards/netdisco").mock(
        return_value=httpx.Response(200, json={"card": _card()}))

    await helpers.card_update("netdisco", labels=[], ctx=fake_ctx(token_info=_RW))
    assert json.loads(route.calls.last.request.content)["labels"] == []


# ── card_label: surgical add / remove ──────────────────────────────────────

@pytest.mark.asyncio
async def test_card_label_requires_board_write(helpers, fake_ctx):
    out = await helpers.card_label("netdisco", "bug",
                                   ctx=fake_ctx(token_info={"board_write": False}))
    assert "board_write" in out.lower()


@respx.mock
@pytest.mark.asyncio
async def test_card_label_appends_without_clobbering(helpers, fake_ctx):
    """The safety property: adding one label must preserve labels already on
    the card, including any a human added in the board UI."""
    respx.get("http://test-memory-api:8002/board/cards/netdisco").mock(
        return_value=httpx.Response(200, json={"card": _card(labels=["from-a-human"])}))
    route = respx.patch("http://test-memory-api:8002/board/cards/netdisco").mock(
        return_value=httpx.Response(200, json={"card": _card()}))

    out = await helpers.card_label("netdisco", "bug", ctx=fake_ctx(token_info=_RW))
    assert json.loads(route.calls.last.request.content)["labels"] == ["from-a-human", "bug"]
    assert "Added label" in out


@respx.mock
@pytest.mark.asyncio
async def test_card_label_is_idempotent(helpers, fake_ctx):
    respx.get("http://test-memory-api:8002/board/cards/netdisco").mock(
        return_value=httpx.Response(200, json={"card": _card(labels=["bug"])}))
    route = respx.patch("http://test-memory-api:8002/board/cards/netdisco").mock(
        return_value=httpx.Response(200, json={"card": _card()}))

    out = await helpers.card_label("netdisco", "bug", ctx=fake_ctx(token_info=_RW))
    assert "already a label" in out.lower()
    assert not route.called          # no pointless write


@respx.mock
@pytest.mark.asyncio
async def test_card_label_add_is_case_insensitive(helpers, fake_ctx):
    """`Bug` and `bug` are the same label. Adding the other casing must not
    give the card two chips that read identically to a human."""
    respx.get("http://test-memory-api:8002/board/cards/netdisco").mock(
        return_value=httpx.Response(200, json={"card": _card(labels=["bug"])}))
    route = respx.patch("http://test-memory-api:8002/board/cards/netdisco").mock(
        return_value=httpx.Response(200, json={"card": _card()}))

    out = await helpers.card_label("netdisco", "Bug", ctx=fake_ctx(token_info=_RW))
    assert "already a label" in out.lower()
    assert not route.called


@respx.mock
@pytest.mark.asyncio
async def test_card_label_removes(helpers, fake_ctx):
    respx.get("http://test-memory-api:8002/board/cards/netdisco").mock(
        return_value=httpx.Response(200, json={"card": _card(labels=["bug", "elefant"])}))
    route = respx.patch("http://test-memory-api:8002/board/cards/netdisco").mock(
        return_value=httpx.Response(200, json={"card": _card()}))

    out = await helpers.card_label("netdisco", "bug", remove=True,
                                   ctx=fake_ctx(token_info=_RW))
    assert json.loads(route.calls.last.request.content)["labels"] == ["elefant"]
    assert "Removed label" in out


@respx.mock
@pytest.mark.asyncio
async def test_card_label_remove_is_case_insensitive(helpers, fake_ctx):
    respx.get("http://test-memory-api:8002/board/cards/netdisco").mock(
        return_value=httpx.Response(200, json={"card": _card(labels=["Bug", "elefant"])}))
    route = respx.patch("http://test-memory-api:8002/board/cards/netdisco").mock(
        return_value=httpx.Response(200, json={"card": _card()}))

    await helpers.card_label("netdisco", "bug", remove=True, ctx=fake_ctx(token_info=_RW))
    assert json.loads(route.calls.last.request.content)["labels"] == ["elefant"]


@respx.mock
@pytest.mark.asyncio
async def test_card_label_remove_missing_is_reported(helpers, fake_ctx):
    """Say nothing happened rather than reporting a successful no-op — the
    lesson from the wiki_edit silent-delete defect."""
    respx.get("http://test-memory-api:8002/board/cards/netdisco").mock(
        return_value=httpx.Response(200, json={"card": _card(labels=["elefant"])}))
    route = respx.patch("http://test-memory-api:8002/board/cards/netdisco").mock(
        return_value=httpx.Response(200, json={"card": _card()}))

    out = await helpers.card_label("netdisco", "bug", remove=True,
                                   ctx=fake_ctx(token_info=_RW))
    assert "not a label" in out.lower()
    assert not route.called


@respx.mock
@pytest.mark.asyncio
async def test_card_label_unknown_card(helpers, fake_ctx):
    respx.get("http://test-memory-api:8002/board/cards/nope").mock(
        return_value=httpx.Response(404, json={"detail": "not found"}))
    out = await helpers.card_label("nope", "bug", ctx=fake_ctx(token_info=_RW))
    assert "No card matches" in out


@respx.mock
@pytest.mark.asyncio
async def test_card_label_trims_whitespace(helpers, fake_ctx):
    respx.get("http://test-memory-api:8002/board/cards/netdisco").mock(
        return_value=httpx.Response(200, json={"card": _card(labels=[])}))
    route = respx.patch("http://test-memory-api:8002/board/cards/netdisco").mock(
        return_value=httpx.Response(200, json={"card": _card()}))

    await helpers.card_label("netdisco", "  bug  ", ctx=fake_ctx(token_info=_RW))
    assert json.loads(route.calls.last.request.content)["labels"] == ["bug"]


@pytest.mark.asyncio
async def test_card_label_rejects_empty(helpers, fake_ctx):
    out = await helpers.card_label("netdisco", "   ", ctx=fake_ctx(token_info=_RW))
    assert "label" in out.lower()


# ── card_list: filtering by label ──────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_card_list_sends_label_filter(helpers, fake_ctx):
    route = respx.get("http://test-memory-api:8002/board/cards").mock(
        return_value=httpx.Response(200, json={"cards": []}))

    await helpers.card_list(stage="designed", label="bug", ctx=fake_ctx(token_info=_RW))
    assert route.calls.last.request.url.params["label"] == "bug"
    assert route.calls.last.request.url.params["stage"] == "designed"


@respx.mock
@pytest.mark.asyncio
async def test_card_list_omits_empty_label_filter(helpers, fake_ctx):
    route = respx.get("http://test-memory-api:8002/board/cards").mock(
        return_value=httpx.Response(200, json={"cards": []}))

    await helpers.card_list(ctx=fake_ctx(token_info=_RW))
    assert "label" not in route.calls.last.request.url.params


# ── reserved labels: the guarantee that outlived the tags column ──────────
#
# `note-to-self` used to live in a separate `tags` array, and that separation
# was what stopped an agent's routine `card_update(labels=[...])` from wiping
# the record of how a card was captured. One field now, so the protection moved
# to the write path — memory-api re-attaches reserved labels on a replace
# unless it is told not to. `card_label(remove=True)` is what tells it: the
# caller named one label and asked for that one to go.

@respx.mock
@pytest.mark.asyncio
async def test_card_label_remove_opts_out_of_reserved_protection(helpers, fake_ctx):
    """Removing by name is deliberate, so it must be able to take off a
    reserved label — otherwise a marker applied by mistake is permanent."""
    respx.get("http://test-memory-api:8002/board/cards/netdisco").mock(
        return_value=httpx.Response(200, json={"card": _card(labels=["note-to-self", "bug"])}))
    route = respx.patch("http://test-memory-api:8002/board/cards/netdisco").mock(
        return_value=httpx.Response(200, json={"card": _card()}))

    out = await helpers.card_label("netdisco", "note-to-self", remove=True,
                                   ctx=fake_ctx(token_info=_RW))
    sent = json.loads(route.calls.last.request.content)
    assert sent["labels"] == ["bug"]
    assert sent["allow_reserved"] is True
    assert "Removed label" in out


@respx.mock
@pytest.mark.asyncio
async def test_card_label_add_does_not_opt_out(helpers, fake_ctx):
    """Only a removal waives the protection. An ADD travels the same PATCH and
    must leave the card's markers to the server's default handling."""
    respx.get("http://test-memory-api:8002/board/cards/netdisco").mock(
        return_value=httpx.Response(200, json={"card": _card(labels=["note-to-self"])}))
    route = respx.patch("http://test-memory-api:8002/board/cards/netdisco").mock(
        return_value=httpx.Response(200, json={"card": _card()}))

    await helpers.card_label("netdisco", "bug", ctx=fake_ctx(token_info=_RW))
    assert json.loads(route.calls.last.request.content)["allow_reserved"] is False
