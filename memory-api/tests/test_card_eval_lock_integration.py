"""The card evaluation lock, against a REAL PostgreSQL.

This suite has to be integration rather than unit, and not as a matter of
thoroughness. The lock's single load-bearing property is that acquire is an
atomic compare-and-set, and that property lives entirely in one SQL statement's
WHERE clause and in how Postgres re-evaluates it under a concurrent update. A
mocked connection would confirm the handler calls `execute`, which is not the
thing that can be wrong. The two concurrency tests at the bottom are the only
tests in the file that would catch the lock being reintroduced as a
read-then-write, and they run real connections against each other to do it.

The structural half of the guard -- that acquire is still ONE conditional
statement -- is in test_card_eval_lock.py, which needs no database and therefore
runs even where no database is reachable.

The directory is stubbed (identity lives in the editor and is not what these
tests are about), but every write and read below goes to a real database.

Skipped unless one is reachable, which is how a
laptop with no database sees them. CI always runs them, and fails rather than
skips without one (see require_postgres in conftest.py). Set
ELEFANT_TEST_PG_DSN to run them:

    initdb -D /tmp/pg -U postgres --auth=trust
    pg_ctl -D /tmp/pg -o "-p 5599 -k /tmp" start
    ELEFANT_TEST_PG_DSN="postgresql://postgres@/postgres?host=/tmp&port=5599" \\
        pytest memory-api/tests/test_card_eval_lock_integration.py
"""
import os
import threading
import uuid

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
    return slug


def add(tenant, card_slug, **kw):
    return main.card_add(main.CardAddRequest(
        tenant_slug=tenant, title=kw.pop("title", None) or card_slug.title(),
        slug=card_slug, column="Up Next", source="test", **kw))


def get(tenant, card_slug):
    return main.card_get(card_slug, tenant_slug=tenant)["card"]


def lock(tenant, card_slug, run_id):
    return main.card_eval_lock(
        card_slug, main.CardEvalLockRequest(tenant_slug=tenant, run_id=run_id))


def unlock(tenant, card_slug, run_id):
    return main.card_eval_unlock(
        card_slug, main.CardEvalLockRequest(tenant_slug=tenant, run_id=run_id))


def age_lock(tenant, card_slug, seconds):
    """Backdate a held lock, so staleness is testable without waiting half an
    hour. Reaches past the API deliberately: there is no supported way to
    pre-age a lock, and there should not be one."""
    with main.get_engine().connect() as conn:
        main._set_tenant(conn, tenant)
        conn.execute(
            text("UPDATE cards SET eval_lock_at = now() - make_interval(secs => :s) "
                 "WHERE tenant_id = :t AND lower(slug) = lower(:c)"),
            {"s": float(seconds), "t": tenant, "c": card_slug})
        conn.commit()


# ── Acquire ───────────────────────────────────────────────────────────────────

def test_a_new_card_is_not_locked(board):
    add(board, "alpha")
    assert get(board, "alpha")["eval_lock"] is None


def test_acquiring_names_the_holder_and_when_it_was_taken(board):
    add(board, "alpha")
    res = lock(board, "alpha", "run-1")
    assert res["ok"] is True
    assert res["eval_lock"]["run_id"] == "run-1"
    assert res["eval_lock"]["since"]
    assert res["already_held"] is False
    assert res["broke_stale_lock_of"] == ""


def test_the_lock_rides_on_the_ordinary_card_read(board):
    """The reason the lock lives on the card: no caller needs a second call to
    find out whether a card is in somebody's hands."""
    add(board, "alpha")
    lock(board, "alpha", "run-1")
    held = get(board, "alpha")["eval_lock"]
    assert held["run_id"] == "run-1"
    assert held["stale"] is False
    assert held["since"]


def test_the_lock_shows_on_the_card_list_and_the_status_chip(board):
    """Every read path, not just card_get. The list is what the board renders
    and the status chip is what a wiki page renders."""
    add(board, "alpha")
    lock(board, "alpha", "run-1")
    listed = {c["slug"]: c for c in main.card_list(tenant_slug=board)["cards"]}
    assert listed["alpha"]["eval_lock"]["run_id"] == "run-1"
    assert main.card_status("alpha", tenant_slug=board)["eval_lock"]["run_id"] == "run-1"


def test_a_conflicting_acquire_is_refused_and_names_the_current_holder(board):
    """"Locked" on its own leaves the caller nothing to do. A run id is
    something an operator can go and look up."""
    add(board, "alpha")
    lock(board, "alpha", "run-1")
    with pytest.raises(HTTPException) as e:
        lock(board, "alpha", "run-2")
    assert e.value.status_code == 409
    assert "run-1" in e.value.detail
    assert e.value.headers["X-Eval-Lock-Holder"] == "run-1"
    # And the refusal changed nothing.
    assert get(board, "alpha")["eval_lock"]["run_id"] == "run-1"


def test_reacquiring_your_own_lock_succeeds_without_moving_the_timestamp(board):
    """A reviewer retrying a step should not have to care whether its earlier
    acquire landed. The timestamp deliberately does not move, so `since` keeps
    meaning when this holder actually took the card and a wedged run cannot hold
    one forever by re-acquiring in a loop."""
    add(board, "alpha")
    first = lock(board, "alpha", "run-1")["eval_lock"]["since"]
    again = lock(board, "alpha", "run-1")
    assert again["ok"] is True
    assert again["already_held"] is True
    assert again["eval_lock"]["since"] == first


def test_a_lock_needs_a_run_id(board):
    add(board, "alpha")
    with pytest.raises(HTTPException) as e:
        lock(board, "alpha", "   ")
    assert e.value.status_code == 400
    assert get(board, "alpha")["eval_lock"] is None


def test_an_unknown_card_cannot_be_locked(board):
    with pytest.raises(HTTPException) as e:
        lock(board, "nope", "run-1")
    assert e.value.status_code == 404


def test_an_archived_card_cannot_be_locked(board):
    """A card off the board is not evaluable, and locking one would show an
    indicator nobody is looking at."""
    add(board, "alpha")
    main.card_archive("alpha", main.CardArchiveRequest(tenant_slug=board))
    with pytest.raises(HTTPException) as e:
        lock(board, "alpha", "run-1")
    assert e.value.status_code == 404


def test_the_lock_is_per_card_not_per_run(board):
    """One run evaluating a column holds several cards at once."""
    add(board, "alpha")
    add(board, "beta")
    lock(board, "alpha", "run-1")
    lock(board, "beta", "run-1")
    assert get(board, "alpha")["eval_lock"]["run_id"] == "run-1"
    assert get(board, "beta")["eval_lock"]["run_id"] == "run-1"


def test_a_lock_does_not_leak_between_tenants(db):
    """Two boards, the same card slug, the same run: locking one must not lock
    the other. RLS plus the tenant predicate, asserted rather than assumed."""
    a = "t" + uuid.uuid4().hex[:12]
    b = "t" + uuid.uuid4().hex[:12]
    for t in (a, b):
        main.column_add(main.ColumnCreateRequest(
            tenant_slug=t, name="Up Next", stage="designed", position=10))
        add(t, "alpha")
    lock(a, "alpha", "run-1")
    assert get(a, "alpha")["eval_lock"]["run_id"] == "run-1"
    assert get(b, "alpha")["eval_lock"] is None


# ── Release ───────────────────────────────────────────────────────────────────

def test_the_holder_can_release(board):
    add(board, "alpha")
    lock(board, "alpha", "run-1")
    res = unlock(board, "alpha", "run-1")
    assert res["ok"] is True and res["was_locked"] is True
    assert res["eval_lock"] is None
    assert get(board, "alpha")["eval_lock"] is None


def test_releasing_an_unlocked_card_is_a_no_op_that_succeeds(board):
    """A reviewer's cleanup path has to be safe to run twice, and safe to run
    after a stale-break already took the lock away."""
    add(board, "alpha")
    res = unlock(board, "alpha", "run-1")
    assert res["ok"] is True and res["was_locked"] is False
    res = unlock(board, "alpha", "run-1")
    assert res["ok"] is True and res["was_locked"] is False


def test_another_run_cannot_release_your_lock(board):
    """A release any run could call on any card is not a lock. Breaking one is
    acquire's job, and only once it has gone stale."""
    add(board, "alpha")
    lock(board, "alpha", "run-1")
    with pytest.raises(HTTPException) as e:
        unlock(board, "alpha", "run-2")
    assert e.value.status_code == 409
    assert "run-1" in e.value.detail
    assert e.value.headers["X-Eval-Lock-Holder"] == "run-1"
    assert get(board, "alpha")["eval_lock"]["run_id"] == "run-1"


def test_an_archived_card_can_still_be_released(board):
    """Unlike acquire. A card can be archived while a run holds it, and
    refusing the release would leave a lock nobody can clear."""
    add(board, "alpha")
    lock(board, "alpha", "run-1")
    main.card_archive("alpha", main.CardArchiveRequest(tenant_slug=board))
    assert unlock(board, "alpha", "run-1")["was_locked"] is True
    assert get(board, "alpha")["eval_lock"] is None


def test_a_released_card_can_be_taken_by_anybody(board):
    add(board, "alpha")
    lock(board, "alpha", "run-1")
    unlock(board, "alpha", "run-1")
    assert lock(board, "alpha", "run-2")["eval_lock"]["run_id"] == "run-2"


# ── Stale locks ───────────────────────────────────────────────────────────────

def test_a_lock_is_reported_stale_once_past_the_ceiling(board):
    """Computed by Elefant, not left to the caller: the ceiling is Elefant's
    policy, and a caller doing that arithmetic would be applying its own."""
    add(board, "alpha")
    lock(board, "alpha", "run-1")
    assert get(board, "alpha")["eval_lock"]["stale"] is False
    age_lock(board, "alpha", main.CARD_EVAL_LOCK_STALE_SECONDS + 60)
    assert get(board, "alpha")["eval_lock"]["stale"] is True


def test_a_stale_lock_is_broken_by_a_new_acquire_and_the_break_is_recorded(board):
    """A run can die mid-evaluation, so Elefant must not depend on the reviewer
    always releasing cleanly. The break is reported to the caller rather than
    happening silently: it means a run died."""
    add(board, "alpha")
    lock(board, "alpha", "run-1")
    age_lock(board, "alpha", main.CARD_EVAL_LOCK_STALE_SECONDS + 60)
    res = lock(board, "alpha", "run-2")
    assert res["eval_lock"]["run_id"] == "run-2"
    assert res["broke_stale_lock_of"] == "run-1"
    assert res["already_held"] is False
    assert get(board, "alpha")["eval_lock"]["run_id"] == "run-2"


def test_breaking_a_stale_lock_publishes_its_own_event(board):
    """Its own type, not a plain lock event: a break means a run died, and that
    is worth being able to find afterwards."""
    add(board, "alpha")
    lock(board, "alpha", "run-1")
    age_lock(board, "alpha", main.CARD_EVAL_LOCK_STALE_SECONDS + 60)
    seen = []
    original = main._board_hub.publish
    main._board_hub.publish = lambda tenant, type_, **f: seen.append((type_, f))
    try:
        lock(board, "alpha", "run-2")
    finally:
        main._board_hub.publish = original
    types = [t for t, _ in seen]
    assert "card.eval_lock_broken" in types
    assert "card.eval_locked" in types
    broke = dict(next(f for t, f in seen if t == "card.eval_lock_broken"))
    assert broke["broke_run_id"] == "run-1" and broke["run_id"] == "run-2"


def test_a_stale_break_resets_the_timestamp_to_the_new_holder(board):
    """The new holder gets its own full window. Inheriting the dead run's
    timestamp would let a third run break a lock seconds after it was taken."""
    add(board, "alpha")
    lock(board, "alpha", "run-1")
    age_lock(board, "alpha", main.CARD_EVAL_LOCK_STALE_SECONDS + 60)
    lock(board, "alpha", "run-2")
    assert get(board, "alpha")["eval_lock"]["stale"] is False


def test_a_lock_inside_the_ceiling_is_not_breakable(board):
    add(board, "alpha")
    lock(board, "alpha", "run-1")
    age_lock(board, "alpha", main.CARD_EVAL_LOCK_STALE_SECONDS - 60)
    with pytest.raises(HTTPException) as e:
        lock(board, "alpha", "run-2")
    assert e.value.status_code == 409


def test_the_ceiling_is_configuration_not_a_request_field(board, monkeypatch):
    """A caller that could name its own ceiling could pass zero and walk through
    every lock on the board, which is the one thing the lock prevents. So the
    request model must not carry one."""
    assert set(main.CardEvalLockRequest.model_fields) == {"tenant_slug", "run_id"}
    add(board, "alpha")
    lock(board, "alpha", "run-1")
    # And the module-level ceiling is what acquire actually reads.
    monkeypatch.setattr(main, "CARD_EVAL_LOCK_STALE_SECONDS", 0)
    assert lock(board, "alpha", "run-2")["broke_stale_lock_of"] == "run-1"


# ── Atomicity: the one property the whole thing rests on ──────────────────────

def test_two_concurrent_acquires_cannot_both_win(board):
    """THE test in this file. Twenty rounds of two real connections racing for
    the same card, from two threads, with a barrier so they overlap. Exactly one
    must win each round.

    A read-then-write implementation passes every other test here and fails this
    one, which is the entire reason acquire is a single conditional UPDATE. The
    engine is a connection pool, so each thread gets its own connection and the
    race is real rather than serialised by sharing one.

    This asserts TWO separate things, and each one was confirmed to fail on its
    own by mutating the implementation:

      * exactly one winner -- fails with 2 winners if acquire becomes a
        read-then-write;
      * the loser is told WHO beat it -- fails with an empty holder if the row
        lock is dropped, because the loser then reports the row as it was before
        the winner committed.

    Do not weaken either assertion. They are what makes "acquire is atomic and a
    conflict names the current holder" a tested claim rather than a comment.
    """
    for i in range(20):
        card = f"race{i}"
        add(board, card)
        ready = threading.Barrier(2)
        results = {}

        def attempt(run):
            ready.wait()
            try:
                results[run] = lock(board, card, run)
            except HTTPException as exc:
                results[run] = exc

        threads = [threading.Thread(target=attempt, args=(f"run-{n}",)) for n in ("a", "b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
            assert not t.is_alive(), "an acquire deadlocked"

        won = [r for r, out in results.items() if not isinstance(out, HTTPException)]
        lost = [r for r, out in results.items() if isinstance(out, HTTPException)]
        assert len(won) == 1, f"round {i}: {len(won)} winners, expected exactly 1"
        assert len(lost) == 1
        assert results[lost[0]].status_code == 409
        # The loser was told who beat it, and the winner is who the card says.
        # Asserted on the header as well as the message: the header is what a
        # caller reads programmatically, and an empty one is the symptom of the
        # row lock having been dropped.
        assert won[0] in results[lost[0]].detail
        assert results[lost[0]].headers["X-Eval-Lock-Holder"] == won[0]
        assert get(board, card)["eval_lock"]["run_id"] == won[0]


def test_a_concurrent_release_and_acquire_leave_a_coherent_lock(board):
    """The holder releasing while somebody else tries to take it. Whichever
    order they land in, the card must not end up locked by a run that was
    refused, or unlocked while a caller was told it had the lock."""
    for i in range(20):
        card = f"mix{i}"
        add(board, card)
        lock(board, card, "run-holder")
        ready = threading.Barrier(2)
        out = {}

        def releaser():
            ready.wait()
            try:
                out["release"] = unlock(board, card, "run-holder")
            except HTTPException as exc:
                out["release"] = exc

        def taker():
            ready.wait()
            try:
                out["acquire"] = lock(board, card, "run-other")
            except HTTPException as exc:
                out["acquire"] = exc

        threads = [threading.Thread(target=releaser), threading.Thread(target=taker)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
            assert not t.is_alive(), "a lock operation deadlocked"

        held = get(board, card)["eval_lock"]
        if isinstance(out["acquire"], HTTPException):
            # Refused, so it never held it -- the card is either still the
            # holder's or free, never run-other's.
            assert out["acquire"].status_code == 409
            assert held is None or held["run_id"] == "run-holder"
        else:
            # It was told it holds the lock, so the card must say so. The
            # release cannot have cleared a lock it did not hold.
            assert held is not None and held["run_id"] == "run-other"
