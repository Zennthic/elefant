"""The evaluation lock's pure logic, and a structural guard on the SQL.

The lock's behaviour is tested against a real Postgres in
test_card_eval_lock_integration.py. That file SKIPS in CI, because CI has no
database for it, so everything here is deliberately chosen to need none and
therefore to actually run on every push.

Two kinds of test:

  * `_eval_lock_dict`, which is where the API decides what "locked" and "stale"
    mean to every caller. Pure, so it is unit-testable as itself.
  * A structural guard that acquire is still ONE conditional statement. That is
    unusual and worth justifying: the property it protects (a conflicting
    acquire cannot succeed) is invisible to a single-threaded test and is
    exactly what a well-meaning refactor into "read the lock, decide, write it"
    would destroy. The integration test catches that with real concurrency and
    does not run here, so without this guard CI would be blind to the one bug
    that matters. It asserts the shape rather than the exact SQL, so formatting
    and added conditions are free.
"""
import inspect
import re
from datetime import datetime, timedelta, timezone

import main
from conftest import card_insert_lists


def _row(run="", at=None):
    """A card row as far as `_eval_lock_dict` is concerned. A plain dict, which
    supports `in` and `[]` the same way a SQLAlchemy RowMapping does."""
    return {"eval_lock_run": run, "eval_lock_at": at}


# ── What "locked" and "stale" mean to every caller ────────────────────────────

def test_an_unlocked_card_reports_no_lock_at_all():
    """None rather than an object with a falsey holder: one field, one fact, and
    a caller writing `if card["eval_lock"]` gets the right answer."""
    assert main._eval_lock_dict(_row()) is None
    assert main._eval_lock_dict(_row(run="", at=datetime.now(timezone.utc))) is None


def test_a_held_lock_names_its_holder_and_when_it_was_taken():
    now = datetime.now(timezone.utc)
    held = main._eval_lock_dict(_row(run="run-1", at=now))
    assert held["run_id"] == "run-1"
    assert held["since"] == now.isoformat()
    assert held["stale"] is False


def test_staleness_is_computed_here_rather_than_left_to_the_caller():
    """The ceiling is Elefant's policy. A caller doing this arithmetic itself
    would be applying its own to Elefant's lock."""
    now = datetime.now(timezone.utc)
    fresh = now - timedelta(seconds=main.CARD_EVAL_LOCK_STALE_SECONDS - 60)
    old = now - timedelta(seconds=main.CARD_EVAL_LOCK_STALE_SECONDS + 60)
    assert main._eval_lock_dict(_row(run="r", at=fresh))["stale"] is False
    assert main._eval_lock_dict(_row(run="r", at=old))["stale"] is True


def test_a_lock_with_no_timestamp_is_reported_but_not_stale():
    """Should not arise -- the two columns are written together -- but the field
    is nullable, and a row from an older image must render rather than 500."""
    held = main._eval_lock_dict(_row(run="run-1", at=None))
    assert held["run_id"] == "run-1"
    assert held["since"] is None
    assert held["stale"] is False


def test_a_naive_timestamp_does_not_take_the_whole_board_down():
    """This helper runs on every card read, so an unexpected naive datetime must
    cost one field's accuracy rather than every board request. The column is
    TIMESTAMPTZ, so this should not arise -- which is exactly why it would not be
    noticed until it did."""
    # A naive UTC value, built from an aware one rather than from
    # `datetime.now()`: the latter is local time, and this assertion would then
    # pass or fail depending on the runner's timezone.
    naive = (datetime.now(timezone.utc)
             - timedelta(seconds=main.CARD_EVAL_LOCK_STALE_SECONDS + 60)).replace(tzinfo=None)
    held = main._eval_lock_dict(_row(run="run-1", at=naive))
    assert held["run_id"] == "run-1"
    assert held["stale"] is True


def test_a_row_without_the_columns_reads_as_unlocked():
    """A payload assembled by something that selected columns by hand, rather
    than `c.*`. Unlocked is the safe reading: it shows no indicator and blocks
    nobody."""
    assert main._eval_lock_dict({}) is None


def test_the_default_ceiling_is_half_an_hour():
    """A small multiple of the longest check a reviewer is expected to run, and
    long enough to span a whole evaluation rather than one step of one, because
    re-acquiring deliberately does not refresh the timestamp."""
    assert main.CARD_EVAL_LOCK_STALE_SECONDS == 1800


# ── The staleness ceiling is Elefant's, not the caller's ──────────────────────

def test_the_request_cannot_carry_its_own_staleness_ceiling():
    """A caller that could name its own ceiling could pass zero and walk through
    every lock on the board, which is the one thing the lock exists to stop. The
    ceiling is configuration, so the request model must stay this small."""
    assert set(main.CardEvalLockRequest.model_fields) == {"tenant_slug", "run_id"}


def test_the_ceiling_is_read_from_the_environment():
    src = inspect.getsource(main)
    assert 'os.getenv("CARD_EVAL_LOCK_STALE_SECONDS"' in src


# ── Acquire must stay one conditional statement ───────────────────────────────

def _acquire_sql() -> str:
    """The UPDATE inside card_eval_lock, whitespace-collapsed."""
    src = inspect.getsource(main.card_eval_lock)
    at = src.index("UPDATE cards")
    end = src.index("RETURNING", at)
    return " ".join(src[at:end].split())


def test_acquire_decides_in_the_where_clause_not_in_python():
    """The compare and the set are one statement. If this fails because the
    implementation now reads the lock, decides in Python and then writes, that
    is the read-then-write the design forbids: two concurrent acquires will both
    succeed, and the integration test proves it with real connections."""
    sql = _acquire_sql()
    # It is an UPDATE with a WHERE, and the WHERE is what tests the lock.
    assert sql.startswith("UPDATE cards")
    where = sql[sql.index("WHERE"):]
    assert "eval_lock_run = ''" in where, "the free case must be tested in SQL"
    assert "eval_lock_run = :run" in where, "re-acquire must be tested in SQL"
    assert "eval_lock_at <" in where, "the stale case must be tested in SQL"


def test_acquire_takes_the_row_lock_so_a_conflict_can_name_the_holder():
    """Dropping the FOR UPDATE does not break exclusion -- the conditional
    UPDATE still admits one winner -- it breaks the ERROR MESSAGE: the loser
    reads the row as it was before the winner committed and reports the holder
    as ''. The definition of done requires the holder to be named, so the row
    lock is load-bearing too, for a different reason."""
    src = inspect.getsource(main.card_eval_lock)
    assert "FOR UPDATE" in src


def test_release_also_checks_the_holder_in_sql():
    """Same reasoning one step down: the holder check is the WHERE clause, so it
    cannot be raced between a read and a write."""
    src = inspect.getsource(main.card_eval_unlock)
    at = src.index("UPDATE cards")
    sql = " ".join(src[at:src.index("RETURNING", at)].split())
    assert "eval_lock_run = :run" in sql[sql.index("WHERE"):]


def test_the_stale_ceiling_is_bound_as_a_parameter_not_interpolated():
    """It comes from the environment, so it is a value rather than something
    formatted into the statement."""
    sql = _acquire_sql()
    assert "make_interval(secs => :stale)" in sql


# ── Schema and restore ───────────────────────────────────────────────────────

def test_both_lock_columns_are_added_on_startup():
    src = inspect.getsource(main)
    for col in ("eval_lock_run", "eval_lock_at"):
        assert re.search(
            r"ALTER TABLE cards ADD COLUMN IF NOT EXISTS " + col, src), col


def test_a_restore_brings_cards_back_unlocked():
    """Deliberate, and the one card field the "a restore is history" rule does
    not cover. A lock is a live claim by a run that is definitionally not
    running by the time anybody restores a snapshot; restoring it would show a
    human "under evaluation" on a card no run can ever release. The export is
    SELECT * so the columns ARE in the snapshot -- this asserts the restore
    keeps ignoring them.

    Asserted on the INSERT's own column list rather than on the source text,
    which mentions both columns in the comment explaining why they are absent."""
    cols, values = card_insert_lists(inspect.getsource(main._bcdr_insert_cards))
    assert "eval_lock_run" not in cols and "eval_lock_at" not in cols
    assert not any("eval_lock" in v for v in values)


def test_the_lock_is_on_the_card_payload():
    """Every surface that renders a card gets the lock from `_card_dict`, so
    none of them needs a special call and none can disagree about it."""
    assert "_eval_lock_dict(row)" in inspect.getsource(main._card_dict)
