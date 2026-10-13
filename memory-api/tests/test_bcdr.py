"""Org BCDR export/import helpers + route registration.

The DB round-trip (RLS, arrays, jsonb) is exercised by the stage DR test against
real Postgres; here we cover the pure JSON-safe conversion + timestamp parsing
and that the internal endpoints are wired.
"""
import uuid
from datetime import datetime, timezone


def test_bcdr_jsonable_converts_uuid_and_datetime():
    import main
    u = uuid.uuid4()
    dt = datetime(2026, 5, 26, 12, 0, 0, tzinfo=timezone.utc)
    out = main._bcdr_jsonable({"id": u, "created_at": dt, "name": "x", "tags": ["a"], "n": 3})
    assert out["id"] == str(u)
    assert out["created_at"] == dt.isoformat()
    assert out["name"] == "x" and out["tags"] == ["a"] and out["n"] == 3


def test_bcdr_ts_parses_iso_z_and_offset_and_none():
    import main
    assert main._bcdr_ts(None) is None
    assert main._bcdr_ts("") is None
    assert main._bcdr_ts("not-a-date") is None
    d1 = main._bcdr_ts("2026-05-26T12:00:00+00:00")
    assert (d1.year, d1.month, d1.day, d1.hour) == (2026, 5, 26, 12)
    d2 = main._bcdr_ts("2026-05-26T12:00:00Z")
    assert d2.hour == 12
    # already a datetime → passthrough
    now = datetime.now(timezone.utc)
    assert main._bcdr_ts(now) is now


def test_bcdr_endpoints_registered():
    import main
    paths = {getattr(r, "path", None) for r in main.app.routes}
    assert "/internal/org-export" in paths
    assert "/internal/org-import" in paths


def test_bcdr_tables_cover_boards_hot_and_revisions():
    import main
    assert set(main._BCDR_TABLES) == {
        "board_columns", "cards", "card_links", "card_comments",
        "memory_hot", "memory_revisions"}


def test_bcdr_restores_card_links_in_dependency_order():
    """card_links references cards at both ends, so it has to be deleted BEFORE
    cards and inserted AFTER them. Get either backwards and the restore fails on
    a foreign key — at the one moment nobody wants to be debugging, in the
    middle of bringing an org back."""
    import inspect
    import main
    src = inspect.getsource(main.org_import)
    deletes = src.split("for tbl in (", 1)[1].split(")", 1)[0]
    assert deletes.index("card_links") < deletes.index('"cards"'), \
        "card_links must be deleted before the cards it references"
    assert src.index("_bcdr_insert_cards") < src.index("_bcdr_insert_links"), \
        "card_links must be inserted after the cards it references"


def test_bcdr_restores_card_comments_in_dependency_order():
    """Same argument as card_links: a comment references the card it is on, so
    it has to be deleted BEFORE cards and inserted AFTER them."""
    import inspect
    import main
    src = inspect.getsource(main.org_import)
    deletes = src.split("for tbl in (", 1)[1].split(")", 1)[0]
    assert deletes.index("card_comments") < deletes.index('"cards"'), \
        "card_comments must be deleted before the cards they are on"
    assert src.index("_bcdr_insert_cards") < src.index("_bcdr_insert_comments"), \
        "card_comments must be inserted after the cards they are on"


def test_a_card_link_to_a_missing_card_does_not_fail_the_restore():
    """An older snapshot, taken before this table existed or hand-edited since,
    should still come back as the cards it does have. One dangling edge is not
    a reason to refuse an entire org's data."""
    import inspect
    import main
    src = inspect.getsource(main._bcdr_insert_links)
    assert "EXISTS" in src, "the insert must skip an edge whose ends are missing"
    assert "except Exception" in src
