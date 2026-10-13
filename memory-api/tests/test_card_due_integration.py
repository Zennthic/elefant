"""Due date on a card, against a REAL PostgreSQL.

The field is a column, a validator, three filters, an update path that has to
tell "leave it" from "clear it", a restore column, and a one-time migration
from the `due-YYYY-MM-DD` label convention it replaces. Every one of those
fails at the database, so a mocked connection would confirm none of them.

Skipped unless a database is reachable; CI always runs them (see
require_postgres in conftest.py). Set ELEFANT_TEST_PG_DSN to run them, as
described in test_card_assignee_integration.py.
"""
import os
import uuid
from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import text

import main
from conftest import require_postgres

pytestmark = pytest.mark.integration

DSN = os.environ.get("ELEFANT_TEST_PG_DSN", "").strip()

require_postgres(DSN)


@pytest.fixture(scope="module")
def db():
    prior_url, prior_engine = main.DATABASE_URL, main._engine
    main.DATABASE_URL = DSN
    main._engine = None
    main.get_engine()
    yield
    if main._engine is not None:
        main._engine.dispose()
    main.DATABASE_URL, main._engine = prior_url, prior_engine


@pytest.fixture
def board(db):
    slug = "t" + uuid.uuid4().hex[:12]
    main.column_add(main.ColumnCreateRequest(
        tenant_slug=slug, name="Up Next", stage="designed", position=10))
    main.column_add(main.ColumnCreateRequest(
        tenant_slug=slug, name="Done", stage="done", position=90))
    return slug


def add(tenant, card_slug, column="Up Next", **kw):
    return main.card_add(main.CardAddRequest(
        tenant_slug=tenant, title=card_slug.title(), slug=card_slug,
        column=column, source="test", **kw))


def get(tenant, card_slug):
    return main.card_get(card_slug, tenant_slug=tenant)["card"]


def update(tenant, card_slug, **kw):
    return main.card_update(card_slug, main.CardUpdateRequest(tenant_slug=tenant, **kw))


def slugs(result):
    return sorted(c["slug"] for c in result["cards"])


def iso(days_from_today):
    return (main._today_utc() + timedelta(days=days_from_today)).isoformat()


# ── Setting one ───────────────────────────────────────────────────────────────

def test_a_card_can_be_given_a_due_date_when_it_is_created(board):
    out = add(board, "alpha", due="2026-11-27")
    assert out["card"]["due"] == "2026-11-27"
    assert get(board, "alpha")["due"] == "2026-11-27"


def test_a_card_with_no_due_date_says_none(board):
    add(board, "alpha")
    assert get(board, "alpha")["due"] is None


def test_a_timestamp_is_cut_to_its_day(board):
    """A caller holding a datetime sends one. The day is the whole field."""
    add(board, "alpha", due="2026-11-27T15:30:00Z")
    assert get(board, "alpha")["due"] == "2026-11-27"


@pytest.mark.parametrize("bad", ["next friday", "2026-02-30", "27/11/2026", "2026-13-01"])
def test_a_due_date_that_is_not_a_date_is_refused(board, bad):
    with pytest.raises(HTTPException) as e:
        add(board, "alpha", due=bad)
    assert e.value.status_code == 400


# ── Changing one ──────────────────────────────────────────────────────────────

def test_a_due_date_can_be_set_and_changed(board):
    add(board, "alpha")
    update(board, "alpha", due="2026-11-27")
    assert get(board, "alpha")["due"] == "2026-11-27"
    update(board, "alpha", due="2026-12-01")
    assert get(board, "alpha")["due"] == "2026-12-01"


def test_an_empty_due_clears_it(board):
    add(board, "alpha", due="2026-11-27")
    update(board, "alpha", due="")
    assert get(board, "alpha")["due"] is None


def test_editing_something_else_leaves_the_due_date_alone(board):
    add(board, "alpha", due="2026-11-27")
    update(board, "alpha", title="Renamed")
    assert get(board, "alpha")["due"] == "2026-11-27"


def test_a_bad_date_on_update_changes_nothing(board):
    add(board, "alpha", due="2026-11-27")
    with pytest.raises(HTTPException):
        update(board, "alpha", due="soon", title="Should not land")
    card = get(board, "alpha")
    assert card["due"] == "2026-11-27"
    assert card["title"] == "Alpha"


# ── Finding by one ────────────────────────────────────────────────────────────

def test_due_before_and_after_are_inclusive_and_skip_undated_cards(board):
    add(board, "oct", due="2026-10-15")
    add(board, "nov", due="2026-11-27")
    add(board, "dec", due="2026-12-10")
    add(board, "undated")

    assert slugs(main.card_list(tenant_slug=board, due_before="2026-11-27")) == ["nov", "oct"]
    assert slugs(main.card_list(tenant_slug=board, due_after="2026-11-27")) == ["dec", "nov"]
    assert slugs(main.card_list(tenant_slug=board, due_after="2026-11-01",
                                due_before="2026-11-30")) == ["nov"]


def test_an_unparseable_due_filter_matches_nothing_and_says_why(board):
    add(board, "alpha", due="2026-11-27")
    out = main.card_list(tenant_slug=board, due_before="whenever")
    assert out["cards"] == []
    assert out["warnings"] and "due_before" in out["warnings"][0]


def test_overdue_is_past_due_and_not_done(board):
    add(board, "late", due=iso(-3))
    add(board, "today", due=iso(0))
    add(board, "later", due=iso(5))
    add(board, "undated")
    # Finished work is not late, whatever its date said.
    add(board, "late-but-done", column="Done", due=iso(-3))

    assert slugs(main.card_list(tenant_slug=board, overdue=True)) == ["late"]


def test_a_closed_card_is_not_overdue(board):
    add(board, "late", due=iso(-3))
    main.card_archive("late", main.CardArchiveRequest(tenant_slug=board))
    assert slugs(main.card_list(tenant_slug=board, overdue=True, closed=True)) == []


# ── Surviving a restore ───────────────────────────────────────────────────────

def test_the_due_date_survives_an_export_and_restore(board):
    add(board, "alpha", due="2026-11-27")
    exported = main.org_export(tenant_slug=board)
    main.org_import(main.OrgImportRequest(tenant_slug=board, tables=exported["tables"]))
    assert get(board, "alpha")["due"] == "2026-11-27"


def test_restoring_a_backup_taken_before_this_field_existed(board):
    add(board, "alpha", due="2026-11-27")
    exported = main.org_export(tenant_slug=board)
    for row in exported["tables"]["cards"]:
        row.pop("due", None)
    out = main.org_import(main.OrgImportRequest(tenant_slug=board, tables=exported["tables"]))
    assert out["restored"]["cards"] == 1
    assert get(board, "alpha")["due"] is None


# ── Migrating the label convention ────────────────────────────────────────────

def test_a_due_label_moves_into_the_field_and_the_label_goes(board):
    """The migration that runs once, on the board that has never had the
    column. Card #393 is the known case: `due-2026-11-27` becomes the field,
    and its other labels and its title are left alone."""
    add(board, "deadline", labels=["enhancement", "due-2026-11-27", "with-claude"])
    add(board, "impossible", labels=["due-2026-02-30"])
    add(board, "two", labels=["due-2026-12-01", "due-2026-12-05"])
    add(board, "already", labels=["due-2026-10-01"], due="2026-09-01")

    engine = main.get_engine()
    with engine.connect() as conn:
        # As schema startup runs it: no tenant set, so RLS would hide every
        # row if the backfill did not lift it.
        conn.execute(text("RESET app.tenant_id"))
        main._backfill_card_due_from_labels(conn)
        conn.execute(text("ALTER TABLE cards FORCE ROW LEVEL SECURITY"))
        conn.commit()

    card = get(board, "deadline")
    assert card["due"] == "2026-11-27"
    assert card["labels"] == ["enhancement", "with-claude"]
    assert card["title"] == "Deadline"
    # Not a real date: a wrong due date is worse than a label a person can read.
    impossible = get(board, "impossible")
    assert impossible["due"] is None and impossible["labels"] == ["due-2026-02-30"]
    # The first wins; the rest stay visible.
    two = get(board, "two")
    assert two["due"] == "2026-12-01" and two["labels"] == ["due-2026-12-05"]
    # A card that already has the field is not overwritten.
    already = get(board, "already")
    assert already["due"] == "2026-09-01" and already["labels"] == ["due-2026-10-01"]
