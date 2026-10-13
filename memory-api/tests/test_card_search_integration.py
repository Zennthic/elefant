"""Card search against a REAL PostgreSQL.

The number-aware part of `card_list(search=…)` is entirely SQL: an extra match
clause and an ORDER BY whose NULL handling is the whole correctness argument.
A mocked connection would confirm the Python builds a string and tell you
nothing about what Postgres does with it, so these run the handler against an
actual database.

Skipped unless one is reachable, which is how a
laptop with no database sees them. CI always runs them, and fails rather than
skips without one (see require_postgres in conftest.py). Set
ELEFANT_TEST_PG_DSN to run them:

    initdb -D /tmp/pg -U postgres --auth=trust
    pg_ctl -D /tmp/pg -o "-p 5599 -k /tmp" start
    ELEFANT_TEST_PG_DSN="postgresql://postgres@/postgres?host=/tmp&port=5599" \\
        pytest memory-api/tests/test_card_search_integration.py
"""
import os
import uuid

import pytest
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
    return slug


def add(tenant, card_slug, title=None, body=""):
    return main.card_add(main.CardAddRequest(
        tenant_slug=tenant, title=title or card_slug.title(), slug=card_slug,
        body=body, column="Up Next", source="test"))


def found(tenant, query):
    """The slugs a search returns, in the order it returned them."""
    return [c["slug"] for c in main.card_list(tenant_slug=tenant, search=query)["cards"]]


def number_of(tenant, card_slug):
    return main.card_get(card_slug, tenant_slug=tenant)["card"]["number"]


# ── Finding a card by the number a person says out loud ──────────────────────

def test_a_number_finds_that_card_even_with_the_digits_nowhere_in_its_text(board):
    """The point of the feature. "#57" is how a person refers to a card, and the
    number lives in a column the free-text clause never looked at."""
    add(board, "alpha", title="Nothing numeric here")
    add(board, "bravo", title="Also nothing numeric")
    n = number_of(board, "bravo")

    assert found(board, str(n)) == ["bravo"]


def test_a_leading_hash_is_accepted_because_that_is_how_people_write_it(board):
    add(board, "alpha", title="Nothing numeric here")
    n = number_of(board, "alpha")

    assert found(board, f"#{n}") == ["alpha"]


def test_the_exact_number_outranks_a_card_that_merely_contains_the_digits(board):
    """Ranking, not just matching. A card whose title happens to contain the
    digits sits earlier on the board, and must still come second — otherwise
    typing a number hands you the wrong card first, which is the complaint."""
    add(board, "decoy", title="Chapter 2 of the plan")      # earlier position
    add(board, "target", title="Nothing numeric")
    n = number_of(board, "target")
    # Rig the decoy so its TEXT contains exactly the digits we will search for.
    main.card_update("decoy", main.CardUpdateRequest(
        tenant_slug=board, title=f"Chapter {n} of the plan", source="test"))

    order = found(board, str(n))
    assert order[0] == "target", f"exact number did not win: {order}"
    # And the substring hit is kept rather than thrown away — a card actually
    # titled with those digits is a real result, just a less likely one.
    assert "decoy" in order


def test_an_unnumbered_card_does_not_float_to_the_top(board):
    """The NULL trap. `c.number = :num` is NULL for a card with no number, and
    NULL sorts FIRST under DESC in Postgres — so the obvious ordering ranks
    every un-numbered card above the exact match it exists to surface."""
    add(board, "target", title="Nothing numeric")
    add(board, "ghost", title="Chapter x")
    n = number_of(board, "target")
    with main.get_engine().connect() as conn:
        main._set_tenant(conn, board)
        # A card that predates the number backfill, and one whose text matches
        # so it is in the result set at all.
        conn.execute(text("UPDATE cards SET number = NULL, title = :t "
                          "WHERE slug = 'ghost' AND tenant_id = :s"),
                     {"t": f"Chapter {n}", "s": board})
        conn.commit()

    order = found(board, str(n))
    assert order[0] == "target", f"an unnumbered card outranked the exact match: {order}"


def test_text_search_still_matches_titles_case_insensitively(board):
    """The existing behaviour the number work must not have disturbed."""
    add(board, "alpha", title="Extend MCP surface")
    add(board, "bravo", title="Something else")

    assert found(board, "extend mcp") == ["alpha"]


def test_a_number_with_no_card_finds_nothing_rather_than_everything(board):
    """A number clause OR-ed into the filter must not widen the result set when
    it matches nothing."""
    add(board, "alpha", title="Nothing numeric")
    add(board, "bravo", title="Also nothing")

    assert found(board, "999999") == []


def test_an_absurdly_long_digit_string_does_not_blow_up_the_query(board):
    """`number` is a 32-bit column and this is far wider than one. Whether the
    driver widens the comparison or the handler declines to treat it as a
    number, the contract is the same from out here: an empty result, never an
    error reaching the caller."""
    add(board, "alpha", title="Nothing numeric")

    assert found(board, "9" * 40) == []
