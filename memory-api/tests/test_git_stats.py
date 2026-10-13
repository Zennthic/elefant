"""Issue #136: /git/stats, the question that tells an empty index from a current clone.

The embedder's incremental sync derives its work from `git diff old_head HEAD`.
That diff is empty both when a source is fully indexed and when its index holds
nothing at all, so a source in the second state reported a successful sync
forever while serving no search results. This endpoint is how the sync tells
the two apart.

The distinction that matters most here is that a source with no rows answers
`files: 0` rather than going missing, because the caller is asking about one source,
and "no row" is precisely the answer it needs to act on.
"""
import pytest


@pytest.fixture
def stats(mock_engine):
    import main
    engine, conn = mock_engine

    def _call(rows, source_id="", tenant_slug="acme"):
        conn.execute.return_value.fetchall.return_value = rows
        return main.git_stats(source_id=source_id, tenant_slug=tenant_slug)
    return _call


def test_one_source_reports_its_file_count(stats):
    out = stats([("s1", 106)], source_id="s1")
    assert out["files"] == 106
    assert out["source_id"] == "s1"


def test_a_source_with_nothing_indexed_reports_zero_not_absent(stats):
    """GROUP BY returns no row for an unindexed source. Zero is the answer the
    sync acts on, so it has to be stated, not inferred from a missing key."""
    out = stats([], source_id="s1")
    assert out["files"] == 0


def test_another_source_s_rows_are_not_counted_as_this_one_s(stats):
    out = stats([("s2", 40)], source_id="s1")
    assert out["files"] == 0


def test_the_whole_tenant_breaks_down_per_source(stats):
    out = stats([("s1", 106), ("s2", 40)])
    assert out["files"] == 146
    assert out["by_source"] == {"s1": 106, "s2": 40}


def test_a_tenant_with_nothing_indexed_totals_zero(stats):
    out = stats([])
    assert out["files"] == 0
    assert out["by_source"] == {}


def test_the_tenant_slug_is_echoed_back(stats):
    assert stats([], tenant_slug="globex")["tenant_slug"] == "globex"


def test_a_blank_tenant_slug_falls_back_to_default(stats):
    assert stats([], tenant_slug="")["tenant_slug"] == "default"


def test_the_query_is_scoped_to_the_tenant(mock_engine):
    """Every other git_files reader scopes by tenant_id; a stats call that
    counted across tenants would leak one org's repo sizes to another."""
    import main
    engine, conn = mock_engine
    conn.execute.return_value.fetchall.return_value = []
    main.git_stats(source_id="s1", tenant_slug="acme")
    sql = " ".join(str(c.args[0]) for c in conn.execute.call_args_list)
    assert "tenant_id = :tenant_id" in sql
    params = [c.args[1] for c in conn.execute.call_args_list if len(c.args) > 1]
    assert any(p.get("tenant_id") == "acme" for p in params)


def test_the_row_level_tenant_guard_is_set(mock_engine):
    """Same app.tenant_id set_config the other git endpoints use."""
    import main
    engine, conn = mock_engine
    conn.execute.return_value.fetchall.return_value = []
    main.git_stats(source_id="s1", tenant_slug="acme")
    sql = " ".join(str(c.args[0]) for c in conn.execute.call_args_list)
    assert "app.tenant_id" in sql


def test_the_endpoint_is_behind_the_internal_service_gate():
    """/git/stats answers how many files a tenant has indexed, which is the
    shape of their private repos. It must not be reachable without the peer
    token. The app-wide middleware exempts only /health, and this pins that
    this route was not added to the exemption list."""
    import main
    from fastapi.testclient import TestClient
    r = TestClient(main.app).get("/git/stats", params={"source_id": "s1"})
    assert r.status_code == 401
    assert "/git/stats" not in main._AUTH_EXEMPT_PATHS
