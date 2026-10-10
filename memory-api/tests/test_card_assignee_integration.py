"""Assignee on a card, against a REAL PostgreSQL.

The field is a column, a filter, an update path and a restore column, and the
interesting failures are all at the database: a filter that matches nothing, an
update that cannot clear, a restore that silently drops the field because the
INSERT enumerates columns by hand. A mocked connection would confirm none of
those.

The directory is stubbed — identity lives in the editor and is not what these
tests are about — but every write and read below goes to a real database.

Skipped unless one is reachable, which is how a
laptop with no database sees them. CI always runs them, and fails rather than
skips without one (see require_postgres in conftest.py). Set
ELEFANT_TEST_PG_DSN to run them:

    initdb -D /tmp/pg -U postgres --auth=trust
    pg_ctl -D /tmp/pg -o "-p 5599 -k /tmp" start
    ELEFANT_TEST_PG_DSN="postgresql://postgres@/postgres?host=/tmp&port=5599" \\
        pytest memory-api/tests/test_card_assignee_integration.py
"""
import os
import uuid

import pytest

import main
from conftest import require_postgres

pytestmark = pytest.mark.integration

DSN = os.environ.get("ELEFANT_TEST_PG_DSN", "").strip()

# The accounts the stubbed directory knows about. `claude` is an agent on
# purpose: this board is majority agent-worked, and a field that could only
# name humans could not say who is doing most of these cards.
DIRECTORY = {
    "jon": "user:jon-1", "user:jon-1": "user:jon-1", "Jon Smith": "user:jon-1",
    "jan": "user:jan-2", "user:jan-2": "user:jan-2",
    "claude": "agent:claude-9", "agent:claude-9": "agent:claude-9",
}


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


@pytest.fixture(autouse=True)
def directory(monkeypatch):
    """Resolve like the editor would, without being the editor."""
    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"resolved": dict(self._resolved), "errors": dict(self._errors)}

    def fake_post(url, json=None, headers=None, timeout=None):
        r = _Resp()
        r._resolved, r._errors = {}, {}
        for want in (json or {}).get("resolve", []):
            if want in DIRECTORY:
                r._resolved[want] = DIRECTORY[want]
            else:
                r._errors[want] = f"no member matches {want!r}"
        return r

    monkeypatch.setattr(main.httpx, "post", fake_post)


@pytest.fixture
def board(db):
    slug = "t" + uuid.uuid4().hex[:12]
    main.column_add(main.ColumnCreateRequest(
        tenant_slug=slug, name="Up Next", stage="designed", position=10))
    return slug


def add(tenant, card_slug, **kw):
    return main.card_add(main.CardAddRequest(
        tenant_slug=tenant, title=kw.pop("title", None) or card_slug.title(),
        slug=card_slug, column="Up Next", source="test", **kw))


def get(tenant, card_slug):
    return main.card_get(card_slug, tenant_slug=tenant)["card"]


def update(tenant, card_slug, **kw):
    return main.card_update(card_slug, main.CardUpdateRequest(
        tenant_slug=tenant, source="test", **kw))


def slugs(result):
    return sorted(c["slug"] for c in result["cards"])


# ── Setting one ───────────────────────────────────────────────────────────────

def test_a_card_can_be_assigned_when_it_is_created(board):
    add(board, "alpha", assignee="jon")
    assert get(board, "alpha")["assignee"] == "user:jon-1"


def test_an_assignee_is_stored_as_a_reference_not_the_name_typed(board):
    """Names rot on rename and split every filter in two. What a person types
    is resolved; what is stored is canonical."""
    add(board, "alpha", assignee="Jon Smith")
    assert get(board, "alpha")["assignee"] == "user:jon-1"


def test_an_agent_can_be_assigned(board):
    """Most of this board is agent-worked. A field that could only hold humans
    could not express what is true of most of its cards."""
    add(board, "alpha", assignee="claude")
    assert get(board, "alpha")["assignee"] == "agent:claude-9"


def test_an_unassigned_card_says_so_rather_than_guessing(board):
    add(board, "alpha")
    assert get(board, "alpha")["assignee"] == ""


def test_assignee_does_not_default_to_whoever_created_the_card(board):
    """The decision this field turns on. `reporter` defaults to the actor
    because whoever files a card did raise it. Whoever files a card very often
    is NOT whoever will do it, so the same default here would assign nearly
    every card to whoever typed it — and a probable assignee is worse than a
    blank one, because blank is visibly unanswered and a guess looks decided."""
    add(board, "alpha", actor="jon")
    card = get(board, "alpha")
    assert card["reporter"] == "user:jon-1"        # the actor DOES become reporter
    assert card["assignee"] == ""                  # and does NOT become assignee


def test_being_assigned_does_not_silently_add_you_to_participants(board):
    """One told fact should write one fact. Deriving a second would make the
    card assert something nobody said."""
    add(board, "alpha", assignee="jan")
    assert get(board, "alpha")["participants"] == []


def test_an_unresolvable_assignee_is_refused_rather_than_stored(board):
    """Fail closed, like every other people field: a card quietly carrying an
    assignee nothing can resolve is the free-text failure wearing a colon."""
    with pytest.raises(Exception):
        add(board, "alpha", assignee="ghost")


# ── Changing one ──────────────────────────────────────────────────────────────

def test_an_assignee_can_be_set_on_an_existing_card(board):
    add(board, "alpha")
    update(board, "alpha", assignee="jan")
    assert get(board, "alpha")["assignee"] == "user:jan-2"


def test_a_card_can_be_handed_from_one_account_to_another(board):
    add(board, "alpha", assignee="jon")
    update(board, "alpha", assignee="claude")
    assert get(board, "alpha")["assignee"] == "agent:claude-9"


def test_an_empty_assignee_unassigns_rather_than_being_ignored(board):
    """The None-vs-"" distinction a scalar needs. Without it a card could never
    be handed back once it had been given out."""
    add(board, "alpha", assignee="jon")
    update(board, "alpha", assignee="")
    assert get(board, "alpha")["assignee"] == ""


def test_editing_something_else_leaves_the_assignee_alone(board):
    """None means "not mentioned". Renaming a card must not quietly unassign
    it."""
    add(board, "alpha", assignee="jon")
    update(board, "alpha", title="Renamed")
    card = get(board, "alpha")
    assert card["title"] == "Renamed"
    assert card["assignee"] == "user:jon-1"


# ── Finding by one ────────────────────────────────────────────────────────────

def test_card_list_filters_by_assignee(board):
    add(board, "alpha", assignee="jon")
    add(board, "bravo", assignee="jan")
    add(board, "charlie")

    assert slugs(main.card_list(tenant_slug=board, assignee="jon")) == ["alpha"]


def test_the_assignee_filter_accepts_a_name_and_matches_the_reference(board):
    """Filtering by what you would say, matching what is stored."""
    add(board, "alpha", assignee="jon")
    assert slugs(main.card_list(tenant_slug=board, assignee="Jon Smith")) == ["alpha"]


def test_assignee_and_reporter_are_different_questions(board):
    """A card Jon raised for Jan answers "who raised it" and "who is doing it"
    differently, which is the entire reason the field exists."""
    add(board, "alpha", reporter="jon", assignee="jan")

    assert slugs(main.card_list(tenant_slug=board, reporter="jon")) == ["alpha"]
    assert slugs(main.card_list(tenant_slug=board, assignee="jon")) == []
    assert slugs(main.card_list(tenant_slug=board, assignee="jan")) == ["alpha"]


def test_person_counts_being_assigned_as_being_involved(board):
    """`person` is the filter somebody actually asks for — "what is Jon
    involved in at all". A card assigned to Jon and nothing else has to count,
    or the filter contradicts what the field means."""
    add(board, "assigned-to-jon", assignee="jon")
    add(board, "raised-by-jon", reporter="jon")
    add(board, "jon-is-on-it", participants=["jon"])
    add(board, "nothing-to-do-with-jon", reporter="jan")

    assert slugs(main.card_list(tenant_slug=board, person="jon")) == [
        "assigned-to-jon", "jon-is-on-it", "raised-by-jon"]


# ── Surviving a restore ───────────────────────────────────────────────────────

def test_the_assignee_survives_an_export_and_restore(board):
    """The restore INSERT enumerates every column by hand, so a field missed
    there is dropped silently — the backup looks fine and the data is gone."""
    add(board, "alpha", assignee="claude")

    exported = main.org_export(tenant_slug=board)
    main.org_import(main.OrgImportRequest(tenant_slug=board, tables=exported["tables"]))

    assert get(board, "alpha")["assignee"] == "agent:claude-9"


def test_restoring_a_backup_taken_before_this_field_existed(board):
    """An older snapshot simply has no assignee to restore, and "" is exactly
    what that means — it must not fail the restore."""
    add(board, "alpha", assignee="jon")
    exported = main.org_export(tenant_slug=board)
    for row in exported["tables"]["cards"]:
        row.pop("assignee", None)

    out = main.org_import(main.OrgImportRequest(tenant_slug=board, tables=exported["tables"]))

    assert out["restored"]["cards"] == 1
    assert get(board, "alpha")["assignee"] == ""
