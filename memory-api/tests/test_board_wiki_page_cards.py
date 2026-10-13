"""Reverse lookup: which cards name a given wiki page in their wiki_links.

This is what lets a wiki page show the card it belongs to. The card→wiki
direction has always worked; without this the link only ever pointed one way.
"""
import main


def _rows(conn, rows):
    conn.execute.return_value.mappings.return_value.all.return_value = rows


def test_empty_path_short_circuits(mock_engine):
    """No path, no query — don't open a connection to look for nothing."""
    engine, conn = mock_engine
    assert main.cards_referencing_wiki_page(path="", tenant_slug="acme") == {"cards": []}
    assert main.cards_referencing_wiki_page(path="   ", tenant_slug="acme") == {"cards": []}
    engine.connect.assert_not_called()


def test_matches_are_case_insensitive_and_slash_stripped(mock_engine):
    """Links are stored without a leading slash (see _clean_links); a caller
    that passes one, or a different case, must still match."""
    engine, conn = mock_engine
    _rows(conn, [])
    main.cards_referencing_wiki_page(path="/AI/Projects/Netdisco/index.md", tenant_slug="acme")
    sql, params = conn.execute.call_args[0]
    assert params == {"t": "acme", "p": "AI/Projects/Netdisco/index.md"}
    text = str(sql).lower()
    assert "unnest(c.wiki_links)" in text
    assert "lower(l) = lower(:p)" in text
    assert "c.tenant_id = :t" in text          # tenant-scoped, on top of RLS


def test_returns_chip_shape_including_closed_flag(mock_engine):
    """Same fields as /status, so the UI renders one kind of chip either way.
    A closed card still genuinely references the page — it's returned and
    flagged, not hidden. `archived` rides along as the flag's old name, for a
    page loaded before the rename that is still reading it."""
    engine, conn = mock_engine
    _rows(conn, [
        {"slug": "netdisco", "title": "Netdisco rollout", "archived_at": None,
         "column_name": "Building", "stage": "building", "color": "#6366F1"},
        {"slug": "old-idea", "title": "Old idea", "archived_at": "2026-01-01",
         "column_name": "Backlog", "stage": "spark", "color": None},
    ])
    out = main.cards_referencing_wiki_page(path="AI/projects/netdisco/index.md",
                                           tenant_slug="acme")["cards"]
    assert out == [
        {"slug": "netdisco", "title": "Netdisco rollout", "column": "Building",
         "stage": "building", "color": "#6366F1", "closed": False, "archived": False},
        {"slug": "old-idea", "title": "Old idea", "column": "Backlog",
         "stage": "spark", "color": None, "closed": True, "archived": True},
    ]


def test_sets_tenant_before_querying(mock_engine):
    """RLS is only in force once app.tenant_id is set for the connection."""
    engine, conn = mock_engine
    _rows(conn, [])
    calls = []
    orig = main._set_tenant
    main._set_tenant = lambda c, s: calls.append(s)
    try:
        main.cards_referencing_wiki_page(path="a/b.md", tenant_slug="acme")
    finally:
        main._set_tenant = orig
    assert calls == ["acme"]


def test_route_is_not_under_the_card_slug_prefix():
    """/board/cards/{card_slug} would swallow a literal sibling if it were ever
    registered first; this route deliberately lives outside that prefix."""
    paths = {getattr(r, "path", None) for r in main.app.routes}
    assert "/board/wiki-page-cards" in paths
    assert not any(p and p.startswith("/board/cards/") and "wiki-page" in p for p in paths)
