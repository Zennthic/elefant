"""GET /board/labels against a REAL PostgreSQL.

The endpoint is one query and all of its behaviour is in that query: unnest an
array column, de-dup case-insensitively, exclude archived cards, stay inside
the tenant. Asserting on its text proves it was spelled as intended; only a
database says it does what that spelling is supposed to mean.

Skipped locally unless one is reachable, and never skipped under CI — see the
sibling integration modules.
"""
import os
import uuid

import pytest

import main
from conftest import require_postgres

pytestmark = pytest.mark.integration

DSN = os.environ.get("ELEFANT_TEST_PG_DSN", "").strip()


require_postgres(DSN)


@pytest.fixture
def board():
    """A tenant of its own per test, so one test's vocabulary is never another's
    — which is also the isolation the endpoint itself is being asked about."""
    prior_url, prior_engine = main.DATABASE_URL, main._engine
    main.DATABASE_URL, main._engine = DSN, None
    main.get_engine()
    slug = f"t{uuid.uuid4().hex[:10]}"
    yield slug
    if main._engine is not None:
        main._engine.dispose()
    main.DATABASE_URL, main._engine = prior_url, prior_engine


def _add(slug: str, title: str, labels: list, archived: bool = False):
    card = main.card_add(main.CardAddRequest(
        tenant_slug=slug, title=title, labels=labels))["card"]
    if archived:
        main.card_archive(card["slug"], main.CardArchiveRequest(tenant_slug=slug))
    return card


def _labels(slug: str) -> list:
    return main.board_labels(tenant_slug=slug)["labels"]


def test_labels_come_back_from_the_cards_that_carry_them(board):
    _add(board, "One", ["bug", "infra"])
    _add(board, "Two", ["enhancement"])
    assert _labels(board) == ["bug", "enhancement", "infra"]


def test_a_label_on_two_cards_is_listed_once(board):
    _add(board, "One", ["bug"])
    _add(board, "Two", ["bug"])
    assert _labels(board) == ["bug"]


def test_two_spellings_collapse_to_one_suggestion(board):
    """The write path already de-dups within a card. Across cards it cannot, so
    this is the only place `Bug` and `bug` could show up as two."""
    _add(board, "One", ["Bug"])
    _add(board, "Two", ["bug"])
    assert _labels(board) == ["Bug"]


def test_the_list_is_sorted_the_way_a_human_scans_it(board):
    _add(board, "One", ["Zebra", "apple", "Mango"])
    assert _labels(board) == ["apple", "Mango", "Zebra"]


def test_a_label_only_on_an_archived_card_is_not_offered(board):
    """Archive is how this board retires things, so retired vocabulary stops
    being suggested. It is still ON the card and still filterable."""
    _add(board, "Live", ["bug"])
    _add(board, "Gone", ["obsolete"], archived=True)
    assert _labels(board) == ["bug"]


def test_a_label_on_both_a_live_and_an_archived_card_survives(board):
    """The rule is about the LABEL, not the card: archiving one card carrying
    it must not retire a word another card is still using."""
    _add(board, "Live", ["shared"])
    _add(board, "Gone", ["shared"], archived=True)
    assert _labels(board) == ["shared"]


def test_one_tenants_vocabulary_does_not_reach_another(board):
    other = f"t{uuid.uuid4().hex[:10]}"
    _add(board, "Mine", ["mine"])
    _add(other, "Theirs", ["theirs"])
    assert _labels(board) == ["mine"]
    assert _labels(other) == ["theirs"]


def test_an_empty_board_offers_nothing_rather_than_failing(board):
    assert _labels(board) == []


def test_the_reserved_set_is_served_alongside(board):
    assert main.board_labels(tenant_slug=board)["reserved"] == sorted(
        main.RESERVED_CARD_LABELS)


def test_a_note_to_self_card_puts_the_marker_in_the_vocabulary(board):
    """It is an ordinary label now, so it is suggestible like any other — the
    client marks it reserved from the `reserved` list, not by its absence."""
    _add(board, "Remember", ["note-to-self"])
    assert "note-to-self" in _labels(board)
