"""Card comments against a REAL PostgreSQL.

Everything that makes a comment thread correct is in the database: that it
hangs off the card's ID rather than its slug, that a delete is soft, that a
count is one query for a page, that the tenant boundary holds, and that a
restore puts it back in dependency order. A mocked connection can confirm the
Python around those and none of them.

The directory is stubbed — identity lives in the editor and is not what these
are about — but every read and write below goes to a real database.

Skipped unless one is reachable, which is how a
laptop with no database sees them. CI always runs them, and fails rather than
skips without one (see require_postgres in conftest.py). Set
ELEFANT_TEST_PG_DSN to run them:

    initdb -D /tmp/pg -U postgres --auth=trust
    pg_ctl -D /tmp/pg -o "-p 5599 -k /tmp" start
    ELEFANT_TEST_PG_DSN="postgresql://postgres@/postgres?host=/tmp&port=5599" \\
        pytest memory-api/tests/test_card_comments_integration.py
"""
import os
import uuid

import pytest
from fastapi import HTTPException

import main
from conftest import require_postgres

pytestmark = pytest.mark.integration

DSN = os.environ.get("ELEFANT_TEST_PG_DSN", "").strip()

DIRECTORY = {
    "jon": "user:jon-1", "user:jon-1": "user:jon-1",
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


def add(tenant, card_slug):
    return main.card_add(main.CardAddRequest(
        tenant_slug=tenant, title=card_slug.title(), slug=card_slug,
        column="Up Next", source="test"))


def post(tenant, card_slug, body, author="jon"):
    return main.card_comment_add(card_slug, main.CommentAddRequest(
        tenant_slug=tenant, body=body, author=author, source="test"))["comment"]


def thread(tenant, card_slug):
    return main.card_comment_list(card_slug, tenant_slug=tenant)["comments"]


def edit(tenant, card_slug, cid, body, actor="jon"):
    return main.card_comment_update(card_slug, cid, main.CommentUpdateRequest(
        tenant_slug=tenant, body=body, actor=actor, source="test"))["comment"]


def remove(tenant, card_slug, cid, actor="jon"):
    return main.card_comment_delete(card_slug, cid, main.CommentDeleteRequest(
        tenant_slug=tenant, actor=actor, source="test"))


# ── Posting and reading ───────────────────────────────────────────────────────

def test_a_comment_can_be_posted_and_read_back(board):
    add(board, "alpha")
    post(board, "alpha", "the deploy failed on the minio pull")
    got = thread(board, "alpha")
    assert len(got) == 1
    assert got[0]["body"] == "the deploy failed on the minio pull"
    assert got[0]["author"] == "user:jon-1"


def test_a_thread_is_oldest_first(board):
    """It is a conversation, not a feed."""
    add(board, "alpha")
    post(board, "alpha", "first")
    post(board, "alpha", "second")
    post(board, "alpha", "third")
    assert [c["body"] for c in thread(board, "alpha")] == ["first", "second", "third"]


def test_an_agent_posts_as_itself(board):
    """The agent case is as strong as the human one — a dispatch run that gets
    stuck on a judgement call has somewhere to say so, under its own name."""
    add(board, "alpha")
    c = post(board, "alpha", "blocked: needs a decision on the column name", author="claude")
    assert c["author"] == "agent:claude-9"


def test_comments_are_per_card(board):
    add(board, "alpha")
    add(board, "bravo")
    post(board, "alpha", "on alpha")
    assert thread(board, "bravo") == []


def test_an_empty_comment_is_refused(board):
    add(board, "alpha")
    with pytest.raises(HTTPException) as e:
        post(board, "alpha", "   ")
    assert e.value.status_code == 400


def test_an_enormous_comment_is_refused_readably(board):
    """Long enough to be a wiki page, and the message says so rather than
    truncating silently."""
    add(board, "alpha")
    with pytest.raises(HTTPException) as e:
        post(board, "alpha", "x" * (main.COMMENT_MAX_CHARS + 1))
    assert e.value.status_code == 400
    assert str(main.COMMENT_MAX_CHARS) in e.value.detail


def test_commenting_on_a_card_that_does_not_exist_is_a_404(board):
    with pytest.raises(HTTPException) as e:
        post(board, "no-such-card", "hello")
    assert e.value.status_code == 404


# ── The rename case, which is why card_id and not slug ───────────────────────

def test_a_thread_survives_the_card_being_renamed(board):
    """The slug is the one thing about a card that changes, so hanging a thread
    off it would orphan the conversation on the day somebody tidies a name."""
    add(board, "alpha")
    post(board, "alpha", "still here?")
    main.card_update("alpha", main.CardUpdateRequest(
        tenant_slug=board, new_slug="alpha-renamed", source="test"))

    assert [c["body"] for c in thread(board, "alpha-renamed")] == ["still here?"]
    # …and the old slug still resolves, so the thread is reachable either way.
    assert [c["body"] for c in thread(board, "alpha")] == ["still here?"]


# ── Editing: the author, visibly ─────────────────────────────────────────────

def test_the_author_can_edit_their_own_comment(board):
    add(board, "alpha")
    c = post(board, "alpha", "frist")
    out = edit(board, "alpha", c["id"], "first")
    assert out["body"] == "first"


def test_an_edit_is_visible_as_an_edit(board):
    """Editing that rewrites history invisibly is worse than not being able to
    edit at all."""
    add(board, "alpha")
    c = post(board, "alpha", "frist")
    assert c["edited"] is False
    assert edit(board, "alpha", c["id"], "first")["edited"] is True


def test_nobody_else_can_edit_a_comment(board):
    """Putting words in somebody else's mouth is the one thing a comment thread
    must not allow."""
    add(board, "alpha")
    c = post(board, "alpha", "mine", author="jon")
    with pytest.raises(HTTPException) as e:
        edit(board, "alpha", c["id"], "not mine", actor="jan")
    assert e.value.status_code == 403
    assert thread(board, "alpha")[0]["body"] == "mine"


def test_an_unidentified_caller_cannot_edit_anything(board):
    """No actor is not the author of anything."""
    add(board, "alpha")
    c = post(board, "alpha", "mine")
    with pytest.raises(HTTPException) as e:
        edit(board, "alpha", c["id"], "hijacked", actor="")
    assert e.value.status_code == 403


def test_a_comment_cannot_be_edited_through_another_card(board):
    """The comment id is scoped to the card it is on, so knowing an id is not
    enough to reach it from somewhere else."""
    add(board, "alpha")
    add(board, "bravo")
    c = post(board, "alpha", "on alpha")
    with pytest.raises(HTTPException) as e:
        edit(board, "bravo", c["id"], "moved?")
    assert e.value.status_code == 404


# ── Deleting: soft, and it leaves a marker ───────────────────────────────────

def test_a_deleted_comment_leaves_a_marker_rather_than_a_hole(board):
    """A thread that silently loses a message reads as one that never had it."""
    add(board, "alpha")
    post(board, "alpha", "first")
    c = post(board, "alpha", "withdrawn")
    post(board, "alpha", "third")
    remove(board, "alpha", c["id"])

    got = thread(board, "alpha")
    assert len(got) == 3
    assert got[1]["deleted"] is True
    assert got[1]["body"] == ""


def test_nobody_else_can_delete_a_comment(board):
    add(board, "alpha")
    c = post(board, "alpha", "mine", author="jon")
    with pytest.raises(HTTPException) as e:
        remove(board, "alpha", c["id"], actor="jan")
    assert e.value.status_code == 403


def test_a_deleted_comment_cannot_be_edited_back(board):
    add(board, "alpha")
    c = post(board, "alpha", "gone")
    remove(board, "alpha", c["id"])
    with pytest.raises(HTTPException) as e:
        edit(board, "alpha", c["id"], "back again")
    assert e.value.status_code == 400


# ── The count on the sticky ──────────────────────────────────────────────────

def test_card_list_carries_a_comment_count(board):
    """"3 comments" is useful on a sticky; the text is not."""
    add(board, "alpha")
    add(board, "bravo")
    post(board, "alpha", "one")
    post(board, "alpha", "two")

    by_slug = {c["slug"]: c for c in main.card_list(tenant_slug=board)["cards"]}
    assert by_slug["alpha"]["comment_count"] == 2
    assert by_slug["bravo"]["comment_count"] == 0


def test_a_deleted_comment_does_not_count(board):
    """The sticky should say how much conversation there is, not how much there
    has been."""
    add(board, "alpha")
    c = post(board, "alpha", "one")
    post(board, "alpha", "two")
    remove(board, "alpha", c["id"])

    by_slug = {x["slug"]: x for x in main.card_list(tenant_slug=board)["cards"]}
    assert by_slug["alpha"]["comment_count"] == 1


def test_the_count_is_one_query_for_the_whole_page():
    """The board draws a count per sticky, so a query per card would be a query
    per card on every poll."""
    import inspect
    src = inspect.getsource(main._comment_counts)
    assert "GROUP BY card_id" in src
    assert src.count("conn.execute") == 1


def test_card_get_carries_the_thread_itself(board):
    add(board, "alpha")
    post(board, "alpha", "hello")
    got = main.card_get("alpha", tenant_slug=board)
    assert [c["body"] for c in got["comments"]] == ["hello"]
    assert got["card"]["comment_count"] == 1


# ── The tenant boundary ──────────────────────────────────────────────────────

def test_comments_do_not_cross_the_tenant_boundary(db):
    """RLS, matching cards and card_links. A comment is as private as the card
    it is on."""
    a = "t" + uuid.uuid4().hex[:12]
    b = "t" + uuid.uuid4().hex[:12]
    for t in (a, b):
        main.column_add(main.ColumnCreateRequest(
            tenant_slug=t, name="Up Next", stage="designed", position=10))
    add(a, "shared-slug")
    add(b, "shared-slug")
    post(a, "shared-slug", "tenant a only")

    assert [c["body"] for c in thread(a, "shared-slug")] == ["tenant a only"]
    assert thread(b, "shared-slug") == []


# ── Surviving a restore ──────────────────────────────────────────────────────

def test_a_thread_survives_an_export_and_restore(board):
    add(board, "alpha")
    post(board, "alpha", "first")
    c = post(board, "alpha", "withdrawn")
    remove(board, "alpha", c["id"])

    exported = main.org_export(tenant_slug=board)
    assert len(exported["tables"]["card_comments"]) == 2
    main.org_import(main.OrgImportRequest(tenant_slug=board, tables=exported["tables"]))

    got = thread(board, "alpha")
    assert [x["body"] for x in got] == ["first", ""]
    # The withdrawal is history too, and comes back as a withdrawal.
    assert got[1]["deleted"] is True


def test_restoring_a_snapshot_with_no_comments_table_still_works(board):
    """An older backup, taken before this table existed."""
    add(board, "alpha")
    exported = main.org_export(tenant_slug=board)
    exported["tables"].pop("card_comments", None)
    out = main.org_import(main.OrgImportRequest(tenant_slug=board, tables=exported["tables"]))
    assert out["restored"]["cards"] == 1
    assert out["restored"]["card_comments"] == 0


def test_a_comment_whose_card_is_missing_is_skipped_not_fatal(board):
    """Same rule as a dangling link: a hand-edited or partial snapshot should
    still come back as the cards it does have."""
    add(board, "alpha")
    post(board, "alpha", "hello")
    exported = main.org_export(tenant_slug=board)
    exported["tables"]["cards"] = []

    out = main.org_import(main.OrgImportRequest(tenant_slug=board, tables=exported["tables"]))
    assert out["restored"]["card_comments"] == 1   # attempted
    # …and nothing landed, because the card it belongs to is not there.
    assert main.card_list(tenant_slug=board)["cards"] == []
