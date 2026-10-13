"""Immutable per-tenant card numbers: allocation, the never-reuse guarantee,
backfill, and disaster recovery.

A card's `number` is its short human handle — "#33" said out loud, and what a
permalink is built from. Two properties carry the whole design:

  per-tenant   the sequence is global within a tenant and every tenant counts
               separately, so two tenants both having a #1 is correct.
  never reused a number, once handed out, is spent forever — including by a
               card that is later archived.

The second one is the reason the counter is its own table. Archive is the
terminal state on this board (there is no card deletion), so MAX(number)+1
would re-issue an archived card's number to the next card created, and one
tenant's history would hold two different #33s with the older one already
quoted in a wiki page or a GitHub issue.

Pure logic plus SQL-string assertions, in the style of the other board tests.
The backfill runs exactly once, unattended, against data nobody is watching, so
it is driven against a fake connection.
"""
import inspect

import main


# ── storage ────────────────────────────────────────────────────────────────

def test_cards_table_gains_a_number_column():
    """This repo has no migration runner — the ADD COLUMN IF NOT EXISTS at
    startup is it."""
    src = inspect.getsource(main._ensure_schema)
    assert "ALTER TABLE cards ADD COLUMN IF NOT EXISTS number BIGINT" in src


def test_the_counter_is_its_own_table_not_derived_from_the_cards():
    """The whole never-reuse guarantee rests on this. A counter derived from
    the cards table cannot survive archiving the highest-numbered card."""
    src = inspect.getsource(main._ensure_schema)
    assert "CREATE TABLE IF NOT EXISTS card_number_seq" in src
    assert "last_number BIGINT NOT NULL DEFAULT 0" in src


def test_numbers_are_unique_within_a_tenant_not_globally():
    """Per-tenant, because each tenant counts separately — a global unique
    index would make the second tenant's #1 fail to insert."""
    src = inspect.getsource(main._ensure_schema)
    assert "cards_tenant_number_idx" in src
    assert "ON cards (tenant_id, number)" in src


def test_the_counter_table_is_tenant_isolated_like_the_cards():
    """It holds one row per tenant and is written on every card creation. Left
    without RLS it would be the one board table a cross-tenant read could
    reach."""
    src = inspect.getsource(main._ensure_schema)
    assert "ALTER TABLE card_number_seq ENABLE ROW LEVEL SECURITY" in src
    assert "ALTER TABLE card_number_seq FORCE ROW LEVEL SECURITY" in src
    assert "CREATE POLICY tenant_isolation ON card_number_seq" in src


def test_backfill_runs_only_when_the_column_is_new():
    """ADD COLUMN IF NOT EXISTS cannot report whether it did anything, so the
    schema asks information_schema first. Without that gate the backfill would
    re-run every startup — and since it only fills NULLs it would be a no-op,
    but the gate is what makes that guarantee readable rather than incidental."""
    src = inspect.getsource(main._ensure_schema)
    assert "number_existed" in src
    assert "if not number_existed:" in src
    assert "_backfill_card_numbers(conn)" in src


# ── allocation ─────────────────────────────────────────────────────────────

def test_allocation_never_reads_the_max_card_number():
    """MAX(number)+1 is the tempting allocator and it is exactly wrong: it
    re-issues an archived card's number. If this assertion ever fails, the
    never-reuse guarantee has been quietly traded away."""
    src = inspect.getsource(main._next_card_number)
    assert "MAX(" not in src.upper()
    # It must not consult the cards table at all: what the board currently
    # holds is exactly the thing the counter has to be independent of.
    assert "FROM cards" not in src


def test_allocation_is_a_single_atomic_statement():
    """Two cards created at the same moment must not be handed the same
    number. ON CONFLICT DO UPDATE takes a row lock on the tenant's counter, so
    the second waits; a read-then-write would race and the unique index would
    turn that into a 500 on an ordinary card creation."""
    src = inspect.getsource(main._next_card_number)
    assert "ON CONFLICT (tenant_id)" in src
    assert "DO UPDATE SET last_number = card_number_seq.last_number + 1" in src
    assert "RETURNING last_number" in src


def test_allocation_is_scoped_to_one_tenant():
    src = inspect.getsource(main._next_card_number)
    assert "card_number_seq (tenant_id, last_number)" in src
    assert '{"t": tenant}' in src


def test_card_add_allocates_and_stores_a_number():
    src = inspect.getsource(main.card_add)
    assert "number = _next_card_number(conn, slug)" in src
    # The INSERT names its columns explicitly, so number has to be in both lists.
    assert "position, source, number)" in src
    assert ":pos, :source, :number)" in src


def test_card_add_response_carries_the_number():
    """card_add hand-builds its response dict rather than going through
    _card_dict, so it is the one place the number can go missing on create —
    and the UI needs it to render the chip without a reload."""
    assert '"number": number' in inspect.getsource(main.card_add)


def test_card_dict_serialises_the_number_and_tolerates_its_absence():
    """A card written by an older image before the backfill ran has no number.
    That must serialise as null, not crash and not render as "#None"."""
    src = inspect.getsource(main._card_dict)
    assert 'int(row["number"]) if row["number"] is not None else None' in src


# ── disaster recovery ──────────────────────────────────────────────────────

def test_bcdr_restore_carries_the_number():
    """The restore INSERT enumerates every card column by hand, so a new one
    that isn't added here is silently dropped — the kind of loss nobody
    notices until they need the backup."""
    src = inspect.getsource(main._bcdr_insert_cards)
    assert "dispatch_ref, number," in src
    assert '"number": r.get("number")' in src


def test_bcdr_restore_advances_the_counter_past_what_it_restored():
    """Restoring cards without restoring the counter would leave it at 0, and
    the next card created after a restore would be handed #1 again — straight
    into the unique index, or worse, a duplicate of a number already quoted."""
    src = inspect.getsource(main._bcdr_insert_cards)
    assert "INSERT INTO card_number_seq" in src
    assert "GREATEST(card_number_seq.last_number, EXCLUDED.last_number)" in src


# ── backfill ───────────────────────────────────────────────────────────────

class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _FakeConn:
    """Enough of a SQLAlchemy connection to drive _backfill_card_numbers: it
    answers the two SELECTs it makes with canned rows and records every UPDATE
    and counter INSERT it is asked to make."""

    def __init__(self, unnumbered, existing_max=()):
        self._unnumbered = unnumbered
        self._existing = [{"tenant_id": t, "m": m} for t, m in existing_max]
        self.updates = []
        self.seeds = []

    def execute(self, statement, params=None):
        sql = " ".join(str(statement).split())
        if sql.upper().startswith("SELECT") and "GROUP BY tenant_id" in sql:
            return _FakeResult(self._existing)
        if sql.upper().startswith("SELECT"):
            return _FakeResult(self._unnumbered)
        if sql.upper().startswith("UPDATE"):
            self.updates.append(params)
            return _FakeResult([])
        self.seeds.append(params)
        return _FakeResult([])


def _run_backfill(cards, existing_max=()):
    conn = _FakeConn([{"id": i, "tenant_id": t} for i, t in cards], existing_max)
    filled = main._backfill_card_numbers(conn)
    numbers = {u["id"]: u["n"] for u in conn.updates}
    seeds = {s["t"]: s["n"] for s in conn.seeds}
    return filled, numbers, seeds


def test_backfill_numbers_a_board_from_one_in_creation_order():
    """The oldest card becomes #1, so the numbers read as the history they
    describe. Ordering is the SELECT's job; this pins that the allocation
    walks the rows in the order it received them."""
    filled, numbers, _ = _run_backfill(
        [("a", "acme"), ("b", "acme"), ("c", "acme")])
    assert filled == 3
    assert numbers == {"a": 1, "b": 2, "c": 3}


def test_backfill_orders_by_creation_so_the_numbers_match_the_history():
    assert "ORDER BY tenant_id, created_at, id" in inspect.getsource(
        main._backfill_card_numbers)


def test_each_tenant_counts_separately():
    """Two tenants both getting a #1 is the specified behaviour, not a
    collision — the sequence is global WITHIN a tenant."""
    _, numbers, _ = _run_backfill(
        [("a", "acme"), ("b", "acme"), ("x", "other"), ("y", "other")])
    assert numbers == {"a": 1, "b": 2, "x": 1, "y": 2}


def test_backfill_continues_past_numbers_already_handed_out():
    """A partial or re-run migration must not hand out a number twice. Numbers
    already present win, and the fill continues above them."""
    _, numbers, _ = _run_backfill([("c", "acme")], existing_max=[("acme", 7)])
    assert numbers == {"c": 8}


def test_backfill_seeds_the_counter_to_the_highest_number_it_issued():
    """Without this the first card created after the migration would be handed
    #1, colliding with the card that was just backfilled to #1."""
    _, _, seeds = _run_backfill([("a", "acme"), ("b", "acme"), ("x", "other")])
    assert seeds == {"acme": 2, "other": 1}


def test_backfill_seeding_never_lowers_an_existing_counter():
    """GREATEST, not assignment: a counter that has already run ahead (numbers
    burnt by rolled-back transactions) must not be wound back, or it would
    re-issue numbers."""
    src = inspect.getsource(main._backfill_card_numbers)
    assert "GREATEST(card_number_seq.last_number, EXCLUDED.last_number)" in src


def test_backfill_only_touches_cards_with_no_number():
    assert "WHERE number IS NULL" in inspect.getsource(main._backfill_card_numbers)


def test_archived_cards_are_numbered_too():
    """They are still cards and still quotable. Skipping them would leave gaps
    that look exactly like the reuse this design exists to prevent — and the
    backfill SELECT must therefore carry no archived_at filter."""
    src = inspect.getsource(main._backfill_card_numbers)
    assert "archived_at" not in src


def test_backfill_on_an_empty_board_does_nothing_and_says_so():
    filled, numbers, seeds = _run_backfill([])
    assert filled == 0
    assert numbers == {}
    assert seeds == {}
