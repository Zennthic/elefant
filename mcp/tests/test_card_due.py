"""The card `due` date over MCP.

A due date used to live in a card's title and a `due-YYYY-MM-DD` label, where
nothing could sort, filter or warn on it. The properties worth holding:

  1. The due date is VISIBLE on read, on the card and on the list row, and it
     says when it is late or close, so an agent does not have to do the
     arithmetic to notice.
  2. `card_list` passes `due_before`, `due_after` and `overdue` through, and
     sends none of them when not asked.
  3. `card_add` / `card_update` carry `due`, and an empty one is never sent by
     `card_update` — a title edit must not clear a deadline.
  4. Clearing is its own named action, `card_due(clear=True)`, and a call that
     names neither a date nor `clear` changes nothing and says so.
"""
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx


@pytest.fixture
def helpers():
    import main
    return main


_RW = {"user_id": "u1", "tenant_slug": "acme", "source": "claude",
       "board_read": True, "board_write": True}

_CARD = {"title": "Ship the thing", "slug": "ship", "column": "Up Next",
         "stage": "designed", "source": "claude", "aliases": [], "body": ""}

_SETTINGS = "http://test-editor:8080/api/internal/tenant-settings"
_PEOPLE = "http://test-editor:8080/api/internal/board/people"
_CARDS = "http://test-memory-api:8002/board/cards"


def _card(**over):
    c = dict(_CARD)
    c.update(over)
    return c


def _day(offset):
    return (datetime.now(timezone.utc).date() + timedelta(days=offset)).isoformat()


def _mock_editor():
    respx.get(_SETTINGS).mock(return_value=httpx.Response(200, json={"settings": {}}))
    respx.post(_PEOPLE).mock(return_value=httpx.Response(
        200, json={"people": {}, "resolved": {}, "errors": {}}))


# ── read side ──────────────────────────────────────────────────────────────

def test_fmt_card_shows_the_due_date(helpers):
    out = helpers._fmt_card(_card(due="2999-11-27"))
    assert "- Due: 2999-11-27" in out


def test_fmt_card_omits_due_when_there_is_none(helpers):
    assert "Due:" not in helpers._fmt_card(_card())
    assert "Due:" not in helpers._fmt_card(_card(due=None))


def test_a_late_card_says_it_is_overdue(helpers):
    out = helpers._fmt_card(_card(due=_day(-3)))
    assert "overdue" in out and "3 days" in out


def test_a_card_due_soon_says_so(helpers):
    assert "due today" in helpers._fmt_card(_card(due=_day(0)))
    assert "due in 2 days" in helpers._fmt_card(_card(due=_day(2)))
    assert "due in" not in helpers._fmt_card(_card(due=_day(30)))


def test_finished_work_is_never_late(helpers):
    assert "overdue" not in helpers._fmt_card(_card(due=_day(-3), stage="done"))
    assert "overdue" not in helpers._fmt_card(_card(due=_day(-3), closed=True))


def test_the_list_row_carries_the_due_date(helpers):
    out = helpers._fmt_card_list([_card(due=_day(-1)), _card(slug="undated")])
    assert f"due {_day(-1)}" in out and "overdue" in out
    assert "`undated` — claude" in out


# ── filtering ──────────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_card_list_passes_the_due_filters_through(helpers, fake_ctx):
    _mock_editor()
    route = respx.get(_CARDS).mock(return_value=httpx.Response(200, json={"cards": []}))
    await helpers.card_list(due_before="2026-11-30", due_after="2026-11-01",
                            overdue=True, ctx=fake_ctx(token_info=_RW))
    params = route.calls.last.request.url.params
    assert params["due_before"] == "2026-11-30"
    assert params["due_after"] == "2026-11-01"
    assert params["overdue"] == "true"


@respx.mock
@pytest.mark.asyncio
async def test_card_list_sends_no_due_filter_when_not_asked(helpers, fake_ctx):
    _mock_editor()
    route = respx.get(_CARDS).mock(return_value=httpx.Response(200, json={"cards": []}))
    await helpers.card_list(ctx=fake_ctx(token_info=_RW))
    params = route.calls.last.request.url.params
    for k in ("due_before", "due_after", "overdue"):
        assert k not in params


# ── create / update ────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_card_add_sends_due(helpers, fake_ctx):
    _mock_editor()
    route = respx.post(_CARDS).mock(
        return_value=httpx.Response(201, json={"card": _card(due="2026-11-27")}))
    out = await helpers.card_add("Ship the thing", due="2026-11-27",
                                 ctx=fake_ctx(token_info=_RW))
    assert json.loads(route.calls.last.request.content)["due"] == "2026-11-27"
    assert "Due: 2026-11-27" in out


@respx.mock
@pytest.mark.asyncio
async def test_card_update_sends_due_only_when_given(helpers, fake_ctx):
    _mock_editor()
    route = respx.patch(f"{_CARDS}/ship").mock(
        return_value=httpx.Response(200, json={"card": _card()}))
    await helpers.card_update("ship", title="Renamed", ctx=fake_ctx(token_info=_RW))
    assert "due" not in json.loads(route.calls.last.request.content)
    await helpers.card_update("ship", due="2026-12-01", ctx=fake_ctx(token_info=_RW))
    assert json.loads(route.calls.last.request.content)["due"] == "2026-12-01"


# ── card_due ───────────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_card_due_sets_a_date(helpers, fake_ctx):
    _mock_editor()
    route = respx.patch(f"{_CARDS}/ship").mock(
        return_value=httpx.Response(200, json={"card": _card(due="2026-11-27")}))
    out = await helpers.card_due("ship", due="2026-11-27", ctx=fake_ctx(token_info=_RW))
    assert json.loads(route.calls.last.request.content)["due"] == "2026-11-27"
    assert "now due 2026-11-27" in out


@respx.mock
@pytest.mark.asyncio
async def test_card_due_clear_sends_an_explicit_empty(helpers, fake_ctx):
    _mock_editor()
    route = respx.patch(f"{_CARDS}/ship").mock(
        return_value=httpx.Response(200, json={"card": _card(due=None)}))
    out = await helpers.card_due("ship", clear=True, ctx=fake_ctx(token_info=_RW))
    assert json.loads(route.calls.last.request.content)["due"] == ""
    assert "no due date" in out


@pytest.mark.asyncio
async def test_card_due_with_neither_changes_nothing(helpers, fake_ctx):
    out = await helpers.card_due("ship", ctx=fake_ctx(token_info=_RW))
    assert "Nothing changed" in out


@respx.mock
@pytest.mark.asyncio
async def test_card_due_reports_a_refused_date(helpers, fake_ctx):
    _mock_editor()
    respx.patch(f"{_CARDS}/ship").mock(return_value=httpx.Response(
        400, json={"detail": "'soon' is not a date."}))
    out = await helpers.card_due("ship", due="soon", ctx=fake_ctx(token_info=_RW))
    assert "Could not set the due date" in out and "not a date" in out
