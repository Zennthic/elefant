"""A card taken off the board is CLOSED — the rename, and what it must not break.

The stored fact did not move: `cards.archived_at` is still the close time. What
changed is every surface a person or an agent reads. The risk in a rename is
never the new name; it is the old one quietly ceasing to work while something
is still calling it — a half-rolled deploy, a session holding an older tool
list, an external caller. So the guards here are mostly about the ALIASES:

  1. the payload carries `closed`/`closed_at` AND still carries `archived`,
  2. both route names reach the same handler,
  3. the `archived=` filter still includes closed cards,
  4. column archiving, which is a different concept, is untouched.

The SQL is exercised against a real PostgreSQL in the integration suites; this
file needs no database.
"""
import inspect
from datetime import datetime, timezone

import main


def _row(**over):
    """The mapping shape _card_dict() reads, with sane defaults."""
    row = {
        "id": "11111111-1111-1111-1111-111111111111",
        "number": 7,
        "slug": "a-card",
        "title": "A card",
        "body": "",
        "aliases": [], "labels": [], "repo": "",
        "reporter": "", "assignee": "", "participants": [],
        "wiki_links": [], "attachments": [],
        "column_id": "22222222-2222-2222-2222-222222222222",
        "position": 1.0,
        "source": "ui",
        "dispatch_ref": None,
        "archived_at": None,
        "eval_lock_run": None,
        "eval_lock_at": None,
        "created_at": datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 1, 2, 3, 5, tzinfo=timezone.utc),
    }
    row.update(over)
    return row


# ── 1. The payload ────────────────────────────────────────────────────────

def test_an_open_card_is_not_closed():
    d = main._card_dict(_row(), "Up Next", "designed")
    assert d["closed"] is False
    assert d["closed_at"] is None


def test_a_closed_card_carries_when_it_was_closed():
    """The card details show this, so it has to be on the payload —
    a boolean alone cannot answer "when"."""
    when = datetime(2026, 3, 4, 5, 6, tzinfo=timezone.utc)
    d = main._card_dict(_row(archived_at=when), "Up Next", "designed")
    assert d["closed"] is True
    assert d["closed_at"] == when.isoformat()


def test_archived_stays_on_the_payload_as_an_alias():
    """A UI loaded before this deploy reads `archived`. Dropping it would blank
    the closed state on every page open at the moment of the rollout."""
    for archived_at in (None, datetime(2026, 3, 4, tzinfo=timezone.utc)):
        d = main._card_dict(_row(archived_at=archived_at), "Up Next", "designed")
        assert d["archived"] == d["closed"]


def test_created_and_updated_are_still_served():
    """The card details show all three timestamps; only one of them was added here."""
    d = main._card_dict(_row(), "Up Next", "designed")
    assert d["created_at"] == "2026-01-02T03:04:00+00:00"
    assert d["updated_at"] == "2026-01-02T03:05:00+00:00"


# ── 2. The routes ─────────────────────────────────────────────────────────

def _paths():
    return {getattr(r, "path", "") for r in main.app.routes}


def test_close_and_reopen_routes_exist():
    assert "/board/cards/{card_slug}/close" in _paths()
    assert "/board/cards/{card_slug}/reopen" in _paths()


def test_the_old_route_names_still_exist():
    """`/archive` and `/unarchive` are what an older image and any external
    caller post to. They stay, on the same handlers."""
    assert "/board/cards/{card_slug}/archive" in _paths()
    assert "/board/cards/{card_slug}/unarchive" in _paths()


def test_both_names_reach_one_handler():
    """Two routes, one implementation — otherwise the alias is a second code
    path that can drift from the real one."""
    by_path = {}
    for r in main.app.routes:
        p = getattr(r, "path", "")
        if p.startswith("/board/cards/{card_slug}/"):
            by_path[p.rsplit("/", 1)[-1]] = getattr(r, "endpoint", None)
    assert by_path["close"] is by_path["archive"]
    assert by_path["reopen"] is by_path["unarchive"]


def test_the_close_response_carries_both_names():
    """For the caller that reads the reply rather than re-fetching the card:
    the new key, and the old one beside it."""
    body = inspect.getsource(main.card_archive)
    assert '"closed": True' in body and '"archived": True' in body
    reopen = inspect.getsource(main.card_unarchive)
    assert '"closed": False' in reopen and '"archived": False' in reopen


# ── 3. The list filter ────────────────────────────────────────────────────

def test_card_list_takes_closed_and_still_takes_archived():
    params = inspect.signature(main.card_list).parameters
    assert "closed" in params
    assert "archived" in params
    assert params["closed"].default is False
    assert params["archived"].default is False


def test_either_filter_name_includes_closed_cards():
    """`closed=true` is the name; `archived=true` is what a caller written
    against the old API sends, and it has to keep meaning the same thing."""
    assert "if not (closed or archived):" in inspect.getsource(main.card_list)


# ── 4. Columns are a different thing ──────────────────────────────────────

def test_column_archiving_is_not_renamed():
    """`board_columns.archived_at` means "this column is gone from the board",
    which is not a card status. Renaming it alongside the card would have made
    two unrelated concepts share a word."""
    assert "/board/columns/{column_id}/archive" in _paths()
    assert "/board/columns/{column_id}/close" not in _paths()


def test_a_closed_card_is_still_a_resolved_blocker():
    """A card that is closed cannot block anything — that was true when it was
    called archived and must stay true under the new name."""
    assert main._link_resolved("designed", datetime(2026, 1, 1, tzinfo=timezone.utc)) is True
    assert main._link_resolved("designed", None) is False
    assert main._link_resolved("shipped", None) is True
