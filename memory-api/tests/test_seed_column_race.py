"""Issue #93 — board_columns returned every column twice.

_ensure_seed_columns is a check-then-insert with no unique constraint behind
it, and it runs from nearly every board endpoint. Two concurrent calls against
a fresh board both read count=0 and both insert the seed rows, so every
column appears twice — which is exactly what an agent trying all the tools at
once would trigger.
"""
import main


def test_seeding_takes_a_per_tenant_lock_before_counting(mock_engine):
    """The lock is the fix: without it the count both callers read is stale by
    the time either inserts."""
    engine, conn = mock_engine
    conn.execute.return_value.scalar.return_value = 0
    main._ensure_seed_columns(conn, "acme")

    sql = [str(c[0][0]).strip() for c in conn.execute.call_args_list]
    assert any("pg_advisory_xact_lock" in s for s in sql), \
        "seeding must serialise per tenant"
    lock_at = next(i for i, s in enumerate(sql) if "pg_advisory_xact_lock" in s)
    count_at = next(i for i, s in enumerate(sql) if "count(*)" in s)
    assert lock_at < count_at, "the lock has to be taken BEFORE the count is read"


def test_the_lock_is_keyed_on_the_tenant(mock_engine):
    """A global lock would serialise every board in the deployment."""
    engine, conn = mock_engine
    conn.execute.return_value.scalar.return_value = 0
    main._ensure_seed_columns(conn, "acme")
    lock_call = next(c for c in conn.execute.call_args_list
                     if "pg_advisory_xact_lock" in str(c[0][0]))
    assert lock_call[0][1] == {"t": "acme"}


def test_a_seeded_board_inserts_nothing(mock_engine):
    engine, conn = mock_engine
    conn.execute.return_value.scalar.return_value = 5
    main._ensure_seed_columns(conn, "acme")
    assert not any("INSERT INTO board_columns" in str(c[0][0])
                   for c in conn.execute.call_args_list)


def test_a_seeded_board_still_heals_past_duplicates(mock_engine):
    """The lock stops new duplicates; boards that already raced need the rows
    cleaning up, and first touch is when we can do it."""
    engine, conn = mock_engine
    conn.execute.return_value.scalar.return_value = 10
    main._ensure_seed_columns(conn, "acme")
    assert any("DELETE FROM board_columns" in str(c[0][0])
               for c in conn.execute.call_args_list)


def test_an_empty_board_seeds_one_column_per_stage(mock_engine):
    engine, conn = mock_engine
    conn.execute.return_value.scalar.return_value = 0
    main._ensure_seed_columns(conn, "acme")
    inserts = [c for c in conn.execute.call_args_list
               if "INSERT INTO board_columns" in str(c[0][0])]
    assert len(inserts) == len(main._SEED_COLUMNS) == len(main.BOARD_STAGES)


# ── the repair must not destroy real work ──────────────────────────────────

def _heal_sql(conn):
    main._heal_duplicate_seed_columns(conn, "acme")
    return " ".join(str(conn.execute.call_args[0][0]).split())


def test_healing_never_touches_a_column_holding_cards(mock_engine):
    """A duplicate from the race is empty by construction. Two same-named
    columns that hold cards are the user's own, and merging them would move
    their work — so the delete is guarded on emptiness."""
    engine, conn = mock_engine
    sql = _heal_sql(conn)
    assert "NOT EXISTS (SELECT 1 FROM cards c WHERE c.column_id = bc.id)" in sql


def test_healing_keeps_exactly_one_of_each_name(mock_engine):
    """The survivor is chosen deterministically, so two concurrent heals agree
    on which row lives."""
    engine, conn = mock_engine
    sql = _heal_sql(conn)
    assert "lower(keep.name) = lower(bc.name)" in sql
    assert "(keep.position, keep.id) < (bc.position, bc.id)" in sql


def test_healing_is_tenant_scoped(mock_engine):
    engine, conn = mock_engine
    _heal_sql(conn)
    assert conn.execute.call_args[0][1] == {"t": "acme"}
    sql = " ".join(str(conn.execute.call_args[0][0]).split())
    assert "bc.tenant_id = :t" in sql
    assert "keep.tenant_id = bc.tenant_id" in sql


def test_healing_ignores_archived_columns(mock_engine):
    """An archived column shares a name with its replacement legitimately."""
    engine, conn = mock_engine
    sql = _heal_sql(conn)
    assert sql.count("archived_at IS NULL") >= 2
