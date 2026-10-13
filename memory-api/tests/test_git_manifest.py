"""Issue #269: /git/manifest, what the index holds for one source and at what content.

The embedder's sync compares this with the checkout. A diff between two HEADs
only says what changed in the repo, so a file the index missed once stayed
missing, and a file indexed at old content stayed old, through every later
sync. Comparing per-file content hashes is what closes that gap.
"""
import pytest


@pytest.fixture
def manifest(mock_engine):
    import main
    engine, conn = mock_engine

    def _call(rows, source_id="s1", tenant_slug="acme"):
        conn.execute.return_value.fetchall.return_value = rows
        return main.git_manifest(source_id=source_id, tenant_slug=tenant_slug)
    return _call


def test_every_indexed_path_comes_back_with_its_hash(manifest):
    out = manifest([("README.md", "a" * 32), ("main.py", "b" * 32)])
    assert out["files"] == {"README.md": "a" * 32, "main.py": "b" * 32}
    assert out["source_id"] == "s1"


def test_an_unindexed_source_has_an_empty_manifest(manifest):
    assert manifest([])["files"] == {}


def test_a_blank_tenant_slug_falls_back_to_default(manifest):
    assert manifest([], tenant_slug="")["tenant_slug"] == "default"


def test_the_hash_is_computed_over_the_stored_content(mock_engine):
    """The embedder hashes the text it would index; the server must hash the
    same thing, the stored content, or every file looks changed."""
    import main
    engine, conn = mock_engine
    conn.execute.return_value.fetchall.return_value = []
    main.git_manifest(source_id="s1", tenant_slug="acme")
    sql = " ".join(str(c.args[0]) for c in conn.execute.call_args_list)
    assert "md5(content)" in sql


def test_the_query_is_scoped_to_the_tenant_and_source(mock_engine):
    import main
    engine, conn = mock_engine
    conn.execute.return_value.fetchall.return_value = []
    main.git_manifest(source_id="s1", tenant_slug="acme")
    sql = " ".join(str(c.args[0]) for c in conn.execute.call_args_list)
    assert "tenant_id = :tenant_id" in sql
    assert "source_id = :source_id" in sql
    assert "app.tenant_id" in sql
    params = [c.args[1] for c in conn.execute.call_args_list if len(c.args) > 1]
    assert any(p.get("tenant_id") == "acme" and p.get("source_id") == "s1" for p in params)


def test_the_endpoint_is_behind_the_internal_service_gate():
    """The manifest lists a tenant's private file paths. No peer token, no answer."""
    import main
    from fastapi.testclient import TestClient
    r = TestClient(main.app).get("/git/manifest", params={"source_id": "s1"})
    assert r.status_code == 401
    assert "/git/manifest" not in main._AUTH_EXEMPT_PATHS
