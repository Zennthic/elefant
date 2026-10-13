"""The tags-into-labels migration against a REAL PostgreSQL.

This is the one change in the collapse that a mocked connection cannot say
anything useful about. `_merge_card_tags_into_labels` is a single UPDATE whose
whole job is a case-insensitive de-dup that preserves order, written in SQL
because it has to run over every tenant's rows at once — asserting on its text
proves it was spelled as intended and not that Postgres agrees.

It is also the one change that cannot be undone. The column is dropped in the
same transaction, so if the merge is wrong the values are gone, and the board
they came from is the backup. That asymmetry is why these build the PRE-merge
schema by hand and migrate it for real, rather than trusting the fresh-schema
path where the column never exists and the function returns immediately.

Skipped locally unless a database is reachable, and never skipped under CI —
see the sibling integration modules:

    ELEFANT_TEST_PG_DSN="postgresql://elefant@127.0.0.1:5433/elefant_test" \\
        pytest memory-api/tests/test_card_tag_merge_integration.py
"""
import os
import uuid

import pytest
from sqlalchemy import create_engine, text

import main
from conftest import require_postgres

pytestmark = pytest.mark.integration

DSN = os.environ.get("ELEFANT_TEST_PG_DSN", "").strip()


require_postgres(DSN)


@pytest.fixture
def legacy_cards():
    """A `cards` table shaped the way it was BEFORE the merge: both arrays.

    Deliberately minimal — the migration touches two columns and nothing else,
    and a full-fidelity copy of the old schema would mostly be an invitation to
    drift from the real one in ways that hide a failure rather than cause one.
    Its own table name per run, so a failed run never poisons the next.
    """
    eng = create_engine(DSN)
    name = f"cards_legacy_{uuid.uuid4().hex[:8]}"
    with eng.connect() as conn:
        conn.execute(text(f"""
            CREATE TABLE {name} (
                id     UUID PRIMARY KEY,
                slug   TEXT NOT NULL,
                tags   TEXT[] NOT NULL DEFAULT '{{}}',
                labels TEXT[] NOT NULL DEFAULT '{{}}'
            )
        """))
        conn.commit()
    yield eng, name
    with eng.connect() as conn:
        conn.execute(text(f"DROP TABLE IF EXISTS {name}"))
        conn.commit()
    eng.dispose()


def _migrate(conn, table: str):
    """Run the real migration body against `table`.

    The function is hard-coded to `cards`, which is correct in production and
    useless for a fixture — so its SQL is lifted verbatim and retargeted. Read
    from the source rather than copied into this file, so a change to the
    migration that this test does not cover fails here instead of passing
    against a stale duplicate.
    """
    import inspect
    src = inspect.getsource(main._merge_card_tags_into_labels)
    update = src[src.index("UPDATE cards SET labels"):src.index('"""))')]
    conn.execute(text(update.replace("UPDATE cards", f"UPDATE {table}")))
    conn.execute(text(f"ALTER TABLE {table} DROP COLUMN tags"))
    conn.commit()


def _rows(eng, table: str) -> dict:
    with eng.connect() as conn:
        return {r[0]: list(r[1]) for r in conn.execute(
            text(f"SELECT slug, labels FROM {table}")).all()}


def _insert(conn, table: str, slug: str, tags: list, labels: list):
    conn.execute(
        text(f"INSERT INTO {table} (id, slug, tags, labels) "
             f"VALUES (:id, :slug, :tags, :labels)"),
        {"id": str(uuid.uuid4()), "slug": slug, "tags": tags, "labels": labels})


def test_tags_are_merged_into_labels(legacy_cards):
    eng, table = legacy_cards
    with eng.connect() as conn:
        _insert(conn, table, "both", ["infra", "q3"], ["bug"])
        _insert(conn, table, "tags-only", ["note-to-self"], [])
        _insert(conn, table, "labels-only", [], ["enhancement"])
        _insert(conn, table, "neither", [], [])
        conn.commit()
        _migrate(conn, table)

    rows = _rows(eng, table)
    # Labels first, then tags, in the order they were already in.
    assert rows["both"] == ["bug", "infra", "q3"]
    # The marker survives as an ordinary label — which is the whole point of
    # reserving it at the write path instead of in the schema.
    assert rows["tags-only"] == ["note-to-self"]
    assert rows["labels-only"] == ["enhancement"]
    assert rows["neither"] == []


def test_a_value_spelled_both_ways_survives_once(legacy_cards):
    """The de-dup is case-insensitive and the LABEL's casing wins, matching
    _clean_tags on the write path — a card must not come out of the migration
    showing `Bug` and `bug` as two chips."""
    eng, table = legacy_cards
    with eng.connect() as conn:
        _insert(conn, table, "dupe", ["bug", "INFRA"], ["Bug", "infra"])
        conn.commit()
        _migrate(conn, table)

    assert _rows(eng, table)["dupe"] == ["Bug", "infra"]


def test_order_is_preserved_not_alphabetised(legacy_cards):
    """DISTINCT ON has to sort by its own key to pick a winner. Without the
    outer re-sort every card on the board would come out alphabetised, which
    looks like a cosmetic bug and is actually the migration reordering data."""
    eng, table = legacy_cards
    with eng.connect() as conn:
        _insert(conn, table, "ordered", ["alpha"], ["zulu", "mike"])
        conn.commit()
        _migrate(conn, table)

    assert _rows(eng, table)["ordered"] == ["zulu", "mike", "alpha"]


def test_blank_and_padded_values_are_cleaned_out(legacy_cards):
    """Rows written before the hygiene existed can hold padding or empties.
    They are trimmed on the way through rather than migrated as-is, so the
    merge does not carry a chip rendering as an empty box onto the board."""
    eng, table = legacy_cards
    with eng.connect() as conn:
        _insert(conn, table, "messy", ["  spaced  ", "", "   "], ["bug"])
        conn.commit()
        _migrate(conn, table)

    assert _rows(eng, table)["messy"] == ["bug", "spaced"]


def test_the_column_is_gone_afterwards(legacy_cards):
    eng, table = legacy_cards
    with eng.connect() as conn:
        _insert(conn, table, "x", ["infra"], [])
        conn.commit()
        _migrate(conn, table)

    with eng.connect() as conn:
        cols = {r[0] for r in conn.execute(
            text("SELECT column_name FROM information_schema.columns "
                 "WHERE table_name = :t"), {"t": table}).all()}
    assert "tags" not in cols
    assert "labels" in cols


def test_migration_is_a_no_op_once_the_column_is_gone(legacy_cards):
    """Every start after the first calls this. It has to cost nothing and
    raise nothing — an unguarded DROP COLUMN would fail the second boot and
    take the service down with it."""
    eng, table = legacy_cards
    with eng.connect() as conn:
        _insert(conn, table, "x", ["infra"], ["bug"])
        conn.commit()
        _migrate(conn, table)

    before = _rows(eng, table)
    with eng.connect() as conn:
        main._merge_card_tags_into_labels(conn)   # real `cards`, already merged
        conn.commit()
    assert _rows(eng, table) == before


def test_real_schema_build_leaves_no_tags_column():
    """End to end: the schema the service actually builds on startup has one
    classification field, whichever side of the migration it came from."""
    prior_url, prior_engine = main.DATABASE_URL, main._engine
    main.DATABASE_URL, main._engine = DSN, None
    try:
        eng = main.get_engine()            # builds/upgrades the real schema
        with eng.connect() as conn:
            cols = {r[0] for r in conn.execute(
                text("SELECT column_name FROM information_schema.columns "
                     "WHERE table_name = 'cards'")).all()}
    finally:
        if main._engine is not None:
            main._engine.dispose()
        main.DATABASE_URL, main._engine = prior_url, prior_engine
    assert "tags" not in cols
    assert "labels" in cols


# ── row-level security ─────────────────────────────────────────────────────
#
# The migration's real hazard, and the one a plain fixture cannot show. `cards`
# is FORCE ROW LEVEL SECURITY with a policy of
# `tenant_id = current_setting('app.tenant_id', true)`, and _ensure_schema is
# the one code path that deliberately sets no tenant — it is fixing up every
# tenant at once rather than serving one. With the policy live the UPDATE
# matches nothing, the DROP still succeeds, and every tag on every board is
# gone with no error anywhere.
#
# FORCE is what puts it in reach: it applies the policy to the table's OWNER,
# so owning the table is not an escape. A SUPERUSER connection bypasses RLS and
# would never reproduce this, which is why these tests own their table as a
# deliberately unprivileged role — the bug is invisible from the account most
# people run psql as.

@pytest.fixture
def rls_role():
    """A non-superuser role that owns its tables, like a provisioned app user."""
    eng = create_engine(DSN)
    name = f"rls_role_{uuid.uuid4().hex[:8]}"
    with eng.connect() as conn:
        conn.execute(text(f"CREATE ROLE {name} LOGIN"))
        conn.execute(text(f"GRANT CREATE ON SCHEMA public TO {name}"))
        conn.commit()
    yield eng, name
    with eng.connect() as conn:
        # RESET first: a pooled connection can still be wearing the role that
        # the test SET, and a role cannot drop itself.
        conn.execute(text("RESET ROLE"))
        conn.execute(text(f"DROP OWNED BY {name}"))
        conn.execute(text(f"DROP ROLE IF EXISTS {name}"))
        conn.commit()
    eng.dispose()


def _rls_table(conn, table: str, role: str):
    """A `cards` stand-in with the protection the real table carries."""
    conn.execute(text(f"SET ROLE {role}"))
    conn.execute(text(f"""
        CREATE TABLE {table} (
            id UUID PRIMARY KEY, tenant_id TEXT NOT NULL, slug TEXT NOT NULL,
            tags TEXT[] NOT NULL DEFAULT '{{}}', labels TEXT[] NOT NULL DEFAULT '{{}}'
        )
    """))
    for slug, tenant in (("acme-card", "acme"), ("other-card", "other")):
        conn.execute(
            text(f"INSERT INTO {table} (id, tenant_id, slug, tags, labels) "
                 f"VALUES (:id, :t, :slug, '{{infra}}', '{{bug}}')"),
            {"id": str(uuid.uuid4()), "t": tenant, "slug": slug})
    conn.execute(text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
    conn.execute(text(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY"))
    conn.execute(text(
        f"CREATE POLICY tenant_isolation ON {table} "
        f"USING (tenant_id = current_setting('app.tenant_id', true))"))


def test_the_policy_would_hide_every_row_from_the_migration(rls_role):
    """The bug itself, pinned. Without the ALTER the migration issues, an
    UPDATE from a tenant-less connection touches nothing — and reports success.

    This asserts the DANGER rather than the feature, so that removing the
    mitigation as dead weight fails here with the reason written down."""
    eng, role = rls_role
    table = f"cards_rls_{uuid.uuid4().hex[:8]}"
    with eng.connect() as conn:
        _rls_table(conn, table, role)
        touched = conn.execute(
            text(f"UPDATE {table} SET labels = labels || tags WHERE tags <> '{{}}'")).rowcount
        conn.execute(text("RESET ROLE"))
        conn.commit()
    assert touched == 0, "RLS no longer hides rows here — re-check why the migration lifts it"


def test_the_migration_reaches_every_tenant_under_forced_rls(rls_role):
    """And with the mitigation, both tenants' cards migrate from a connection
    that names no tenant at all."""
    eng, role = rls_role
    table = f"cards_rls_{uuid.uuid4().hex[:8]}"
    with eng.connect() as conn:
        _rls_table(conn, table, role)
        conn.execute(text("RESET ROLE"))
        conn.commit()

    import inspect
    src = inspect.getsource(main._merge_card_tags_into_labels)
    body = src[src.index('conn.execute(text("ALTER TABLE cards DISABLE'):]
    with eng.connect() as conn:
        conn.execute(text(f"SET ROLE {role}"))
        for stmt in ("ALTER TABLE cards DISABLE ROW LEVEL SECURITY",
                     "ALTER TABLE cards ENABLE ROW LEVEL SECURITY",
                     "ALTER TABLE cards DROP COLUMN tags"):
            assert stmt in body, f"migration no longer issues: {stmt}"
        conn.execute(text(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY"))
        update = body[body.index("UPDATE cards SET labels"):body.index('"""))')]
        conn.execute(text(update.replace("UPDATE cards", f"UPDATE {table}")))
        conn.execute(text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
        conn.execute(text("RESET ROLE"))
        conn.commit()

    with eng.connect() as conn:
        rows = {r[0]: list(r[1]) for r in conn.execute(
            text(f"SELECT slug, labels FROM {table}")).all()}
    assert rows == {"acme-card": ["bug", "infra"], "other-card": ["bug", "infra"]}


def test_the_table_is_protected_again_afterwards(rls_role):
    """Lifting the policy is a window, not a state. A migration that left RLS
    off would silently disable tenant isolation on the busiest table on the
    board — a far worse outcome than the one it was avoiding."""
    eng, role = rls_role
    table = f"cards_rls_{uuid.uuid4().hex[:8]}"
    with eng.connect() as conn:
        _rls_table(conn, table, role)
        conn.execute(text(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY"))
        conn.execute(text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
        conn.execute(text("RESET ROLE"))
        conn.commit()
    with eng.connect() as conn:
        on, forced = conn.execute(
            text("SELECT relrowsecurity, relforcerowsecurity "
                 "FROM pg_class WHERE relname = :t"), {"t": table}).one()
    assert on, "row security was left off by the migration"
    # FORCE survives a DISABLE/ENABLE cycle; _ensure_schema re-asserts it
    # unconditionally further down regardless.
    assert forced
