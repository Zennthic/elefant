"""The agent-facing half of the Archived → Closed rename.

An MCP tool name is held by every session that has already listed the tools, so
renaming one and removing the old name breaks whatever is mid-conversation. The
new name is `card_close`; `card_archive` stays, deprecated, delegating to the
same body rather than to a second copy of it.

The other half is what an agent READS. A card that is off the board has to be
visibly off the board in card_get and in card_list, under whichever field name
the API happened to serve — a half-rolled deploy can hand one payload and then
the other.
"""
import inspect

import main


# ── The tools ─────────────────────────────────────────────────────────────

def test_card_close_exists():
    assert callable(main.card_close)


def test_card_archive_survives_as_a_deprecated_alias():
    assert callable(main.card_archive)
    assert "DEPRECATED" in (main.card_archive.__doc__ or "")
    assert "card_close" in (main.card_archive.__doc__ or ""), \
        "a deprecation that doesn't name the replacement leaves the caller stuck"


def test_both_tools_run_the_same_body():
    """Not two implementations that can drift — one, called twice."""
    for fn in (main.card_close, main.card_archive):
        assert "_card_close(slug, ctx)" in inspect.getsource(fn)


def test_closing_posts_to_the_close_route():
    body = inspect.getsource(main._card_close)
    assert "/close" in body
    assert "board_write" in body, "closing a card is a write and must be scoped"


def test_the_reply_says_the_card_is_closed_not_deleted():
    """There is no card deletion, so the confirmation has to say where the card
    went — otherwise 'closed' reads as 'gone'."""
    body = inspect.getsource(main._card_close)
    assert "closed" in body and "not deleted" in body


# ── What an agent reads ───────────────────────────────────────────────────

_CARD = {"slug": "old-thing", "title": "Old Thing", "column": "Backlog",
         "stage": "spark", "source": "ui"}


def test_card_get_flags_a_closed_card():
    assert "_closed_" in main._fmt_card({**_CARD, "closed": True})


def test_card_get_flags_it_under_the_old_field_name_too():
    assert "_closed_" in main._fmt_card({**_CARD, "archived": True})


def test_an_open_card_is_not_flagged():
    assert "_closed_" not in main._fmt_card(dict(_CARD))


def test_card_list_flags_a_closed_card():
    out = main._fmt_card_list([{**_CARD, "closed": True}], {})
    assert "_(closed)_" in out


def test_card_list_flags_it_under_the_old_field_name_too():
    out = main._fmt_card_list([{**_CARD, "archived": True}], {})
    assert "_(closed)_" in out


# ── The filter ────────────────────────────────────────────────────────────

def test_card_list_takes_closed_and_keeps_archived():
    params = inspect.signature(main.card_list).parameters
    assert "closed" in params and params["closed"].default is False
    assert "archived" in params and params["archived"].default is False


def test_either_filter_name_asks_the_api_for_closed_cards():
    """`archived=True` is what a session holding the older tool list sends. It
    has to keep meaning the same thing, and it is the API's NEW parameter that
    gets sent either way."""
    body = inspect.getsource(main.card_list)
    assert '"closed": "true" if (closed or archived) else None' in body


def test_the_docstring_says_closed():
    doc = main.card_list.__doc__ or ""
    assert "Closed cards are hidden unless closed=true" in doc
    assert "archived" in doc, "the old name still works, so it still has to be documented"
