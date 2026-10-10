"""/api/internal/wiki/edit/preview and /edit's `if_match` (card #420).

wiki_edit validates any Mermaid diagram an edit adds or changes before the
edit lands. The MCP layer cannot do that from the edit arguments alone (a
one-line `replace` inside a fence never mentions the fence), so it asks for
the page as the edit would leave it, checks the diagrams, then sends the real
edit pinned to the bytes it checked. These pin that contract: a preview saves
nothing and is exactly what /edit saves, and a pinned edit lands only on the
page it was checked against.
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
    slug = f"ep-{oid[:6]}"
    db.add(Tenant(id=oid, name=slug, slug=slug, display_name=slug,
                  domain=f"{slug}.test", lifecycle_status="active", is_active=True))
    db.add(TenantSettings(tenant_slug=slug, ai_wiki_folder="AI"))
    rw = hashlib.sha256(secrets.token_urlsafe(24).encode()).hexdigest()
    ro = hashlib.sha256(secrets.token_urlsafe(24).encode()).hexdigest()
    db.add(ApiToken(name="agent", token_hash=rw, tenant_id=oid,
                    wiki_read=True, wiki_write=True))
    db.add(ApiToken(name="reader", token_hash=ro, tenant_id=oid,
                    wiki_read=True, wiki_write=False))
    db.commit(); db.close()
    return {"slug": slug, "hash": rw, "ro_hash": ro}


@pytest.fixture
def bucket(monkeypatch):
    store = {}
    monkeypatch.setattr(main, "_s3_write", lambda org, rel, data: store.__setitem__(rel, data))
    monkeypatch.setattr(main, "_s3_exists", lambda org, rel: rel in store)
    monkeypatch.setattr(main, "_s3_read", lambda org, rel: store[rel])
    monkeypatch.setattr(main, "_trigger_embedder_reindex", lambda *a, **k: None)
    monkeypatch.setattr(main, "_record_wiki_revision", lambda *a, **k: None)
    return store


PAGE = "# Net\n\n## Diagram\n\nold ascii\n"


def _sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def _post(client, tenant, ep, **body):
    return client.post(f"/api/internal/wiki/{ep}",
                       json={"token_hash": tenant["hash"], "path": "AI/n.md", **body})


def test_preview_returns_the_edited_page_and_saves_nothing(client, tenant, bucket):
    bucket["AI/n.md"] = PAGE.encode()
    r = _post(client, tenant, "edit/preview", mode="replace_section", heading="Diagram",
              content="```mermaid\nflowchart TD\n  a --> b\n```")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["changed"] is True
    assert body["before"] == PAGE
    assert "```mermaid" in body["content"]
    assert body["sha256"] == _sha(PAGE)
    assert bucket["AI/n.md"] == PAGE.encode()


def test_preview_is_what_edit_saves(client, tenant, bucket):
    bucket["AI/n.md"] = PAGE.encode()
    args = dict(mode="append", content="tail")
    preview = _post(client, tenant, "edit/preview", **args).json()
    r = _post(client, tenant, "edit", if_match=preview["sha256"], **args)
    assert r.status_code == 200, r.text
    assert bucket["AI/n.md"].decode() == preview["content"]


def test_a_pinned_edit_is_refused_when_the_page_moved_on(client, tenant, bucket):
    bucket["AI/n.md"] = PAGE.encode()
    r = _post(client, tenant, "edit", mode="append", content="x", if_match=_sha("other"))
    assert r.status_code == 409
    assert "changed" in r.json()["detail"]
    assert bucket["AI/n.md"] == PAGE.encode()


def test_an_empty_if_match_is_a_422(client, tenant, bucket):
    bucket["AI/n.md"] = PAGE.encode()
    assert _post(client, tenant, "edit", mode="append", content="x",
                 if_match="").status_code == 422


def test_preview_holds_the_same_boundary_as_edit(client, tenant, bucket):
    bucket["Runbooks/n.md"] = PAGE.encode()
    r = client.post("/api/internal/wiki/edit/preview",
                    json={"token_hash": tenant["hash"], "path": "Runbooks/n.md",
                          "mode": "append", "content": "x"})
    assert r.status_code == 403
    r = client.post("/api/internal/wiki/edit/preview",
                    json={"token_hash": tenant["ro_hash"], "path": "AI/n.md",
                          "mode": "append", "content": "x"})
    assert r.status_code == 403
    assert _post(client, tenant, "edit/preview", mode="append",
                 content="x").status_code == 404


def test_preview_goes_through_the_shared_gate_and_lock():
    import inspect
    src = inspect.getsource(main.internal_wiki_edit_preview)
    for name in ("_internal_wiki_writer", "_guard_ai_path", "_assert_wiki_unlocked"):
        assert name in src
