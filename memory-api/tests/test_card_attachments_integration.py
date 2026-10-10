"""Card attachments against a REAL PostgreSQL.

The column is a list of OBJECT KEYS, and the interesting failures are at the
database and at the edge of what a client is allowed to put in it: a key
pointing somewhere else in the org's bucket would turn a card edit into a way
to read the rest of it through the download route, and a restore that misses
the column drops the attachments silently while the backup still looks fine.

Skipped unless a database is reachable, which is how a
laptop with no database sees them. CI always runs them, and fails rather than
skips without one (see require_postgres in conftest.py). Set
ELEFANT_TEST_PG_DSN to run them:

    initdb -D /tmp/pg -U postgres --auth=trust
    pg_ctl -D /tmp/pg -o "-p 5599 -k /tmp" start
    ELEFANT_TEST_PG_DSN="postgresql://postgres@/postgres?host=/tmp&port=5599" \\
        pytest memory-api/tests/test_card_attachments_integration.py
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


def add(tenant, card_slug):
    return main.card_add(main.CardAddRequest(
        tenant_slug=tenant, title=card_slug.title(), slug=card_slug,
        column="Up Next", source="test"))


def attach(tenant, card_slug, keys):
    return main.card_update(card_slug, main.CardUpdateRequest(
        tenant_slug=tenant, attachments=keys, source="test"))


def held(tenant, card_slug):
    return main.card_get(card_slug, tenant_slug=tenant)["card"]["attachments"]


KEY = "cards/alpha/1757000000-ab12cd34-screenshot.png"


# ── Holding them ──────────────────────────────────────────────────────────────

def test_a_card_starts_with_no_attachments(board):
    add(board, "alpha")
    assert held(board, "alpha") == []


def test_attachments_round_trip(board):
    add(board, "alpha")
    attach(board, "alpha", [KEY])
    assert held(board, "alpha") == [KEY]


def test_editing_something_else_leaves_attachments_alone(board):
    """None means "not mentioned" — renaming a card must not drop its files."""
    add(board, "alpha")
    attach(board, "alpha", [KEY])
    main.card_update("alpha", main.CardUpdateRequest(
        tenant_slug=board, title="Renamed", source="test"))
    assert held(board, "alpha") == [KEY]


def test_an_empty_list_detaches_everything(board):
    """The deliberate clear, as distinct from not mentioning the field."""
    add(board, "alpha")
    attach(board, "alpha", [KEY])
    attach(board, "alpha", [])
    assert held(board, "alpha") == []


def test_the_same_key_twice_is_held_once(board):
    add(board, "alpha")
    attach(board, "alpha", [KEY, KEY])
    assert held(board, "alpha") == [KEY]


def test_order_is_kept(board):
    add(board, "alpha")
    a = "cards/alpha/1757000000-aaaaaaaa-first.png"
    b = "cards/alpha/1757000001-bbbbbbbb-second.log"
    attach(board, "alpha", [a, b])
    assert held(board, "alpha") == [a, b]


# ── Refusing the ones that are not a card's business ─────────────────────────

@pytest.mark.parametrize("bad", [
    "assets/images/paste-1-aaaa.png",          # the wiki's own assets
    "cards/alpha/../../assets/images/x.png",   # traversal out of the prefix
    "/etc/passwd",                             # absolute
    "../secrets.env",
    "backups/org-export.json",                 # anything else in the bucket
    "cards//x.png",                            # empty slug segment
    "cards/alpha/sub/dir/file.png",            # deeper than a card's own folder
])
def test_a_key_outside_the_card_prefix_is_refused(board, bad):
    """A card may only name objects under its own prefix. Otherwise a card edit
    becomes a way to read the rest of the org's bucket through the
    authenticated download route — the caller chooses the key, so the key is
    checked rather than trusted."""
    add(board, "alpha")
    attach(board, "alpha", [bad])
    assert held(board, "alpha") == [], f"{bad!r} was accepted"


def test_a_good_key_still_lands_alongside_a_refused_one(board):
    """Dropping the bad one rather than failing the whole edit: the card still
    gets the file that was fine."""
    add(board, "alpha")
    attach(board, "alpha", [KEY, "/etc/passwd"])
    assert held(board, "alpha") == [KEY]


def test_the_list_is_capped(board):
    add(board, "alpha")
    many = [f"cards/alpha/17570000{i:02d}-aaaaaaaa-f{i}.png" for i in range(80)]
    attach(board, "alpha", many)
    assert len(held(board, "alpha")) == 50


# ── Surviving a restore ───────────────────────────────────────────────────────

def test_attachments_survive_an_export_and_restore(board):
    """The restore INSERT enumerates every column by hand, so a field missed
    there is dropped silently — the backup looks fine and the files are
    orphaned."""
    add(board, "alpha")
    attach(board, "alpha", [KEY])

    exported = main.org_export(tenant_slug=board)
    main.org_import(main.OrgImportRequest(tenant_slug=board, tables=exported["tables"]))

    assert held(board, "alpha") == [KEY]


def test_restoring_a_backup_taken_before_this_field_existed(board):
    add(board, "alpha")
    attach(board, "alpha", [KEY])
    exported = main.org_export(tenant_slug=board)
    for row in exported["tables"]["cards"]:
        row.pop("attachments", None)

    out = main.org_import(main.OrgImportRequest(tenant_slug=board, tables=exported["tables"]))

    assert out["restored"]["cards"] == 1
    assert held(board, "alpha") == []
