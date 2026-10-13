"""/api/internal/wiki/write's optional `if_match` / `if_none_match`.

The MCP diagram tools revise a page they read a moment ago. Without a
precondition, two agents patching the same diagram both "succeed" and the
first change is lost without anyone being told. These pin the contract the
MCP side relies on: create never clobbers, a patch lands only on the exact
bytes it was computed from, and a caller that sends neither keeps the old
last-write-wins behaviour.
"""
import hashlib

import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture
def client():
    return TestClient(main.app)


@pytest.fixture
def tenant():
    import secrets
    from config_db import get_db_session, Tenant, ApiToken, TenantSettings

    db = next(get_db_session())
    oid = secrets.token_hex(8)
    slug = f"wp-{oid[:6]}"
    db.add(Tenant(id=oid, name=slug, slug=slug, display_name=slug,
                  domain=f"{slug}.test", lifecycle_status="active", is_active=True))
    db.add(TenantSettings(tenant_slug=slug, ai_wiki_folder="AI"))
    rw = hashlib.sha256(secrets.token_urlsafe(24).encode()).hexdigest()
    db.add(ApiToken(name="agent", token_hash=rw, tenant_id=oid,
                    wiki_read=True, wiki_write=True))
    db.commit(); db.close()
    return {"slug": slug, "hash": rw}


@pytest.fixture
def bucket(monkeypatch):
    """An in-memory stand-in for the tenant's S3 prefix."""
    store = {}
    monkeypatch.setattr(main, "_s3_write", lambda org, rel, data: store.__setitem__(rel, data))
    monkeypatch.setattr(main, "_s3_exists", lambda org, rel: rel in store)
    monkeypatch.setattr(main, "_s3_read", lambda org, rel: store[rel])
    monkeypatch.setattr(main, "_trigger_embedder_reindex", lambda *a, **k: None)
    return store


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _write(client, tenant, **body):
    return client.post("/api/internal/wiki/write",
                       json={"token_hash": tenant["hash"], "path": "AI/d.md", **body})


def test_if_none_match_creates_a_new_page(client, tenant, bucket):
    r = _write(client, tenant, content="v1", if_none_match=True)
    assert r.status_code == 200, r.text
    assert r.json()["created"] is True
    assert r.json()["sha256"] == _sha("v1")
    assert bucket["AI/d.md"] == b"v1"


def test_if_none_match_never_clobbers(client, tenant, bucket):
    bucket["AI/d.md"] = b"theirs"
    r = _write(client, tenant, content="mine", if_none_match=True)
    assert r.status_code == 409
    assert "already exists" in r.json()["detail"]
    assert bucket["AI/d.md"] == b"theirs"


def test_if_match_writes_over_the_version_it_names(client, tenant, bucket):
    bucket["AI/d.md"] = b"v1"
    r = _write(client, tenant, content="v2", if_match=_sha("v1"))
    assert r.status_code == 200, r.text
    assert bucket["AI/d.md"] == b"v2"


def test_if_match_refuses_a_page_that_moved_on(client, tenant, bucket):
    bucket["AI/d.md"] = b"someone else's v2"
    r = _write(client, tenant, content="my v2", if_match=_sha("v1"))
    assert r.status_code == 409
    assert "version conflict" in r.json()["detail"]
    assert bucket["AI/d.md"] == b"someone else's v2"


def test_if_match_refuses_a_page_that_is_gone(client, tenant, bucket):
    r = _write(client, tenant, content="v2", if_match=_sha("v1"))
    assert r.status_code == 409
    assert "AI/d.md" not in bucket


def test_if_match_must_be_a_string(client, tenant, bucket):
    bucket["AI/d.md"] = b"v1"
    assert _write(client, tenant, content="v2", if_match=123).status_code == 422


def test_no_precondition_is_still_last_write_wins(client, tenant, bucket):
    bucket["AI/d.md"] = b"v1"
    assert _write(client, tenant, content="v2").status_code == 200
    assert bucket["AI/d.md"] == b"v2"


def test_read_reports_the_hash_write_compares(client, tenant, bucket):
    bucket["AI/d.md"] = b"flowchart TD\n"
    r = client.post("/api/internal/wiki/read",
                    json={"token_hash": tenant["hash"], "path": "AI/d.md"})
    assert r.status_code == 200, r.text
    assert r.json()["sha256"] == _sha("flowchart TD\n")
