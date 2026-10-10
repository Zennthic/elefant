"""Card relationships against a REAL PostgreSQL.

Everything interesting about this feature is in SQL: the two-way read of a
single edge, the blocker traversal, the cycle guard, the RLS tenant boundary,
and the restore ordering. A mocked connection can tell you the Python around a
query is right and nothing at all about the query, so these run the actual
handlers against an actual database.

Skipped unless one is reachable, which is how a
laptop with no database sees them. CI always runs them, and fails rather than
skips without one (see require_postgres in conftest.py). Set
ELEFANT_TEST_PG_DSN to run them:

    initdb -D /tmp/pg -U postgres --auth=trust
    pg_ctl -D /tmp/pg -o "-p 5599 -k /tmp" start
    ELEFANT_TEST_PG_DSN="postgresql://postgres@/postgres?host=/tmp&port=5599" \\
        pytest memory-api/tests/test_card_links_integration.py
"""
import os
import uuid

import pytest

import main
from conftest import require_postgres

pytestmark = pytest.mark.integration

DSN = os.environ.get("ELEFANT_TEST_PG_DSN", "").strip()


require_postgres(DSN)


@pytest.fixture(scope="module")
def db():
    """Point main at the test database, and put it back afterwards so a mocked
    unit test later in the same process never finds a real engine."""
    prior_url, prior_engine = main.DATABASE_URL, main._engine
    main.DATABASE_URL = DSN
    main._engine = None
    main.get_engine()                    # builds the real schema
    yield
    if main._engine is not None:
        main._engine.dispose()
    main.DATABASE_URL, main._engine = prior_url, prior_engine


@pytest.fixture
def board(db):
    """A throwaway tenant with a working column and a terminal one.

    Its own tenant per test, so tests cannot leak into one another and each one
    also exercises the seed path a brand new board takes.
    """
    slug = "t" + uuid.uuid4().hex[:12]
    main.column_add(main.ColumnCreateRequest(
        tenant_slug=slug, name="Up Next", stage="designed", position=10))
    main.column_add(main.ColumnCreateRequest(
        tenant_slug=slug, name="Done", stage="shipped", position=20))
    return slug


def add(tenant, card_slug, title=None, column="Up Next"):
    return main.card_add(main.CardAddRequest(
        tenant_slug=tenant, title=title or card_slug.title(),
        slug=card_slug, column=column, source="test"))


def link(tenant, a, b, kind, remove=False):
    return main.card_link(a, main.CardLinkRequest(
        tenant_slug=tenant, other=b, kind=kind, remove=remove, source="test"))


def rel(tenant, card_slug):
    return main.card_get(card_slug, tenant_slug=tenant)["relationships"]


def is_blocked(tenant, card_slug):
    return main.card_get(card_slug, tenant_slug=tenant)["card"]["blocked"]


# ── One edge, two sides ───────────────────────────────────────────────────────

def test_blocked_by_and_blocks_are_the_same_edge(board):
    add(board, "alpha"); add(board, "bravo")
    link(board, "alpha", "bravo", "blocked-by")

    assert [b["slug"] for b in rel(board, "alpha")["blocked_by"]] == ["bravo"]
    # The claim the whole design rests on: nothing was written to bravo, and
    # bravo still knows.
    assert [b["slug"] for b in rel(board, "bravo")["blocks"]] == ["alpha"]
    assert is_blocked(board, "alpha") is True
    assert is_blocked(board, "bravo") is False


def test_linking_twice_leaves_one_edge(board):
    add(board, "alpha"); add(board, "bravo")
    link(board, "alpha", "bravo", "blocked-by")
    link(board, "alpha", "bravo", "blocked-by")
    assert len(rel(board, "alpha")["blocked_by"]) == 1


def test_a_link_can_be_removed_from_either_end(board):
    add(board, "alpha"); add(board, "bravo")
    link(board, "alpha", "bravo", "blocked-by")
    res = link(board, "bravo", "alpha", "blocks", remove=True)   # the other side
    assert res["removed"] is True
    assert is_blocked(board, "alpha") is False


def test_removing_a_link_that_is_not_there_says_so(board):
    add(board, "alpha"); add(board, "bravo")
    res = link(board, "alpha", "bravo", "blocked-by", remove=True)
    assert res["removed"] is False
    assert res["warnings"], "removing nothing must not report success in silence"


def test_related_is_symmetric_and_stored_once(board):
    add(board, "alpha"); add(board, "bravo")
    link(board, "alpha", "bravo", "related")
    assert [r["slug"] for r in rel(board, "alpha")["related"]] == ["bravo"]
    assert [r["slug"] for r in rel(board, "bravo")["related"]] == ["alpha"]
    link(board, "bravo", "alpha", "related")          # the other direction
    assert len(rel(board, "alpha")["related"]) == 1


def test_a_card_cannot_be_linked_to_itself(board):
    add(board, "alpha")
    with pytest.raises(main.HTTPException) as exc:
        link(board, "alpha", "alpha", "blocks")
    assert exc.value.status_code == 400


def test_linking_to_a_card_that_does_not_exist_is_a_404(board):
    add(board, "alpha")
    with pytest.raises(main.HTTPException) as exc:
        link(board, "alpha", "nope", "blocks")
    assert exc.value.status_code == 404


# ── Picking the next card ─────────────────────────────────────────────────────

def test_next_card_works_the_blocker_not_the_top_of_the_column(board):
    """The reason this feature exists. alpha is top of the column and echo is
    bottom, and alpha cannot start until echo is done."""
    for s in ("alpha", "bravo", "charlie", "delta", "echo"):
        add(board, s)
    link(board, "alpha", "echo", "blocked-by")

    nxt = main.card_next(tenant_slug=board, column="Up Next")
    assert nxt["card"]["slug"] == "echo"
    assert nxt["requested"] == "alpha"          # what the column order pointed at
    assert nxt["chain"] == ["alpha", "echo"]    # and how we got from there to here


def test_next_card_descends_transitively(board):
    for s in ("alpha", "bravo", "charlie"):
        add(board, s)
    link(board, "alpha", "bravo", "blocked-by")
    link(board, "bravo", "charlie", "blocked-by")
    nxt = main.card_next(tenant_slug=board, column="Up Next")
    assert nxt["card"]["slug"] == "charlie"
    assert nxt["chain"] == ["alpha", "bravo", "charlie"]


def test_an_unblocked_column_returns_its_top_card(board):
    add(board, "alpha"); add(board, "bravo")
    nxt = main.card_next(tenant_slug=board, column="Up Next")
    assert nxt["card"]["slug"] == "alpha"
    assert nxt["chain"] == ["alpha"]


def test_next_card_filters_the_same_way_the_list_does(board):
    add(board, "alpha"); add(board, "bravo")
    main.card_update("bravo", main.CardUpdateRequest(
        tenant_slug=board, repo="owner/thing", labels=["bug"]))
    assert main.card_next(tenant_slug=board, repo="owner/thing")["card"]["slug"] == "bravo"
    assert main.card_next(tenant_slug=board, label="bug")["card"]["slug"] == "bravo"


def test_next_card_says_so_when_nothing_matches(board):
    out = main.card_next(tenant_slug=board, column="Up Next")
    assert out["card"] is None
    assert out["warnings"]


# ── When a blocker stops blocking ─────────────────────────────────────────────

def test_shipping_a_blocker_unblocks_the_chain(board):
    add(board, "alpha"); add(board, "bravo")
    link(board, "alpha", "bravo", "blocked-by")
    assert is_blocked(board, "alpha") is True

    main.card_move("bravo", main.CardMoveRequest(
        tenant_slug=board, column="Done", source="test"))

    assert is_blocked(board, "alpha") is False
    assert main.card_next(tenant_slug=board, column="Up Next")["card"]["slug"] == "alpha"


def test_a_resolved_blocker_is_remembered_rather_than_forgotten(board):
    """"This was waiting on that" is the card's history. Dropping the edge once
    it resolves would make a dependency that WAS declared look like one that
    never was."""
    add(board, "alpha"); add(board, "bravo")
    link(board, "alpha", "bravo", "blocked-by")
    main.card_move("bravo", main.CardMoveRequest(
        tenant_slug=board, column="Done", source="test"))

    blockers = rel(board, "alpha")["blocked_by"]
    assert len(blockers) == 1
    assert blockers[0]["resolved"] is True
    assert is_blocked(board, "alpha") is False


def test_archiving_a_blocker_unblocks(board):
    add(board, "alpha"); add(board, "bravo")
    link(board, "alpha", "bravo", "blocked-by")
    main.card_archive("bravo", main.CardArchiveRequest(tenant_slug=board))
    assert is_blocked(board, "alpha") is False
    main.card_unarchive("bravo", main.CardArchiveRequest(tenant_slug=board))
    assert is_blocked(board, "alpha") is True


# ── Cycles ────────────────────────────────────────────────────────────────────

def test_closing_a_cycle_warns_but_is_allowed(board):
    add(board, "alpha"); add(board, "bravo")
    link(board, "alpha", "bravo", "blocked-by")
    res = link(board, "bravo", "alpha", "blocked-by")
    assert res["warnings"], "a cycle has to be reported to whoever can settle it"
    assert "cycle" in res["warnings"][0]


def test_next_card_steps_over_a_cycle_instead_of_hanging(board):
    """The failure this prevents is not a wrong answer, it is a routine that
    never returns."""
    add(board, "alpha"); add(board, "bravo"); add(board, "charlie")
    link(board, "alpha", "bravo", "blocked-by")
    link(board, "bravo", "alpha", "blocked-by")       # alpha ⇄ bravo

    nxt = main.card_next(tenant_slug=board, column="Up Next")
    assert nxt["card"]["slug"] == "charlie"
    assert any("cycle" in w for w in nxt["warnings"])


def test_a_board_that_is_entirely_a_cycle_returns_nothing_rather_than_hanging(board):
    add(board, "alpha"); add(board, "bravo")
    link(board, "alpha", "bravo", "blocked-by")
    link(board, "bravo", "alpha", "blocked-by")
    out = main.card_next(tenant_slug=board, column="Up Next")
    assert out["card"] is None
    assert any("cycle" in w for w in out["warnings"])


def test_a_longer_cycle_is_caught_too(board):
    add(board, "alpha"); add(board, "bravo"); add(board, "charlie"); add(board, "delta")
    link(board, "alpha", "bravo", "blocked-by")
    link(board, "bravo", "charlie", "blocked-by")
    link(board, "charlie", "alpha", "blocked-by")     # a three-card loop
    nxt = main.card_next(tenant_slug=board, column="Up Next")
    assert nxt["card"]["slug"] == "delta"


# ── What the other surfaces see ───────────────────────────────────────────────

def test_card_list_marks_blocked_cards_and_names_the_blocker(board):
    add(board, "alpha"); add(board, "bravo")
    link(board, "alpha", "bravo", "blocked-by")

    cards = {c["slug"]: c for c in main.card_list(tenant_slug=board)["cards"]}
    assert cards["alpha"]["blocked"] is True
    assert [b["slug"] for b in cards["alpha"]["blocked_by"]] == ["bravo"]
    assert cards["bravo"]["blocked"] is False


def test_a_link_survives_renaming_a_card(board):
    """Edges hold card ids, not slugs, precisely so this holds. A dependency
    that silently stopped pointing anywhere after a rename is the invisible
    failure the field exists to remove."""
    add(board, "alpha"); add(board, "bravo")
    link(board, "alpha", "bravo", "blocked-by")
    main.card_update("bravo", main.CardUpdateRequest(
        tenant_slug=board, new_slug="bravo-renamed"))

    blockers = rel(board, "alpha")["blocked_by"]
    assert [b["slug"] for b in blockers] == ["bravo-renamed"]
    assert is_blocked(board, "alpha") is True


# ── The tenant boundary ───────────────────────────────────────────────────────

def test_cards_cannot_be_linked_across_tenants(db):
    a = "t" + uuid.uuid4().hex[:12]
    b = "t" + uuid.uuid4().hex[:12]
    for t in (a, b):
        main.column_add(main.ColumnCreateRequest(
            tenant_slug=t, name="Up Next", stage="designed", position=10))
    add(a, "mine"); add(b, "theirs")

    with pytest.raises(main.HTTPException) as exc:
        link(a, "mine", "theirs", "blocks")
    # Not found rather than forbidden: from this tenant's side that card does
    # not exist, and saying "forbidden" would confirm it does.
    assert exc.value.status_code == 404


# ── Backup and restore ────────────────────────────────────────────────────────

def test_links_survive_an_export_and_restore(board):
    add(board, "alpha"); add(board, "bravo")
    link(board, "alpha", "bravo", "blocked-by")
    link(board, "alpha", "bravo", "related")

    exported = main.org_export(tenant_slug=board)
    assert len(exported["tables"]["card_links"]) == 2

    main.org_import(main.OrgImportRequest(
        tenant_slug=board, tables=exported["tables"]))

    restored = rel(board, "alpha")
    assert [b["slug"] for b in restored["blocked_by"]] == ["bravo"]
    assert [r["slug"] for r in restored["related"]] == ["bravo"]
    assert is_blocked(board, "alpha") is True


def test_restoring_a_snapshot_with_no_links_table_still_works(board):
    """An older backup, taken before this table existed. It must restore the
    cards it does have rather than refusing the whole org."""
    add(board, "alpha")
    exported = main.org_export(tenant_slug=board)
    exported["tables"].pop("card_links", None)
    out = main.org_import(main.OrgImportRequest(
        tenant_slug=board, tables=exported["tables"]))
    assert out["restored"]["cards"] == 1
    assert out["restored"]["card_links"] == 0
