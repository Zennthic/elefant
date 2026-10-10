"""Public wiki sharing (opaque-link) tests.

Two layers, neither needing a live S3 for the security-critical parts:
  1. The scoping helpers (`_share_allows_file`, `_share_allows_asset`,
     `_share_pages`, `_share_page_title`) — pure functions that decide exactly
     which paths a share may expose. This is the part that must not leak.
  2. The public + authed endpoints via TestClient with seeded rows: token
     validation (bad / revoked / expired), page-share metadata, out-of-scope
     and traversal rejection (which happen BEFORE any S3 read), and a full
     create → public-read → revoke round-trip (page existence S3-mocked).
"""
import secrets
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


def _share(rtype, path, subf=True):
    return SimpleNamespace(resource_type=rtype, path=path, include_subfolders=subf)


@pytest.fixture
def m():
    from routers import wiki
    return wiki


@pytest.fixture
def client():
    import main
    return TestClient(main.app, headers={"Origin": "http://testserver"})


# ── Scoping helpers (the part that must never leak) ─────────────────────────

def test_allows_file_page(m):
    s = _share("page", "Guide/intro.md")
    assert m._share_allows_file(s, "Guide/intro.md") is True
    assert m._share_allows_file(s, "Guide/other.md") is False
    assert m._share_allows_file(s, "Secret/x.md") is False


def test_allows_file_folder_recursive(m):
    s = _share("folder", "Guide", subf=True)
    assert m._share_allows_file(s, "Guide/intro.md") is True
    assert m._share_allows_file(s, "Guide/sub/deep.md") is True
    assert m._share_allows_file(s, "GuideBook/x.md") is False   # prefix false-match guard
    assert m._share_allows_file(s, "Other/x.md") is False


def test_allows_file_folder_nonrecursive(m):
    s = _share("folder", "Guide", subf=False)
    assert m._share_allows_file(s, "Guide/intro.md") is True
    assert m._share_allows_file(s, "Guide/sub/deep.md") is False   # subfolder excluded


def test_allows_asset_scoping(m):
    s = _share("page", "Guide/intro.md")
    assert m._share_allows_asset(s, "Guide/assets/x.png") is True   # under the page's dir
    assert m._share_allows_asset(s, "Other/x.png") is False
    f = _share("folder", "Guide")
    assert m._share_allows_asset(f, "Guide/sub/pic.png") is True
    assert m._share_allows_asset(f, "Elsewhere/pic.png") is False


def test_root_level_page_asset_scope(m):
    s = _share("page", "readme.md")
    assert m._share_allows_asset(s, "logo.png") is True      # top-level sibling
    assert m._share_allows_asset(s, "sub/logo.png") is False  # deeper is blocked


def test_page_title(m):
    assert m._share_page_title("Guide/intro.md") == "intro"
    assert m._share_page_title("x.md") == "x"


# ── Public + authed endpoints ───────────────────────────────────────────────

def _seed_tenant_and_share(**share_kw):
    from config_db import get_db_session, Tenant, WikiShare
    db = next(get_db_session())
    org_id = secrets.token_hex(8)
    slug = f"share-{org_id[:6]}"
    db.add(Tenant(id=org_id, name="ShareCo", slug=slug,
                  lifecycle_status="active", is_active=True))
    db.commit()
    kw = dict(resource_type="page", path="Guide/intro.md",
              include_subfolders=True, audience="public")
    kw.update(share_kw)
    token = kw.pop("token", "tok-" + secrets.token_hex(12))
    db.add(WikiShare(token=token, org_id=org_id, **kw))
    db.commit()
    db.close()
    return org_id, slug, token


def _cleanup(org_id):
    from config_db import get_db_session, Tenant, WikiShare
    db = next(get_db_session())
    try:
        db.query(WikiShare).filter_by(org_id=org_id).delete()
        o = db.get(Tenant, org_id)
        if o:
            db.delete(o)
        db.commit()
    finally:
        db.close()


def test_public_meta_page_share(client):
    org_id, slug, token = _seed_tenant_and_share()
    try:
        r = client.get(f"/api/public/share/{token}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["type"] == "page"
        assert body["pages"][0]["path"] == "Guide/intro.md"
        assert r.headers.get("x-robots-tag", "").startswith("noindex")
    finally:
        _cleanup(org_id)


def test_public_meta_bad_token(client):
    assert client.get("/api/public/share/nope-not-a-real-token").status_code == 404


def test_public_meta_revoked(client):
    org_id, slug, token = _seed_tenant_and_share(revoked=True)
    try:
        assert client.get(f"/api/public/share/{token}").status_code == 404
    finally:
        _cleanup(org_id)


def test_public_meta_expired(client):
    org_id, slug, token = _seed_tenant_and_share(
        expires_at=datetime.utcnow() - timedelta(days=1))
    try:
        assert client.get(f"/api/public/share/{token}").status_code == 404
    finally:
        _cleanup(org_id)


def test_public_file_out_of_scope_404(client):
    # Scoping rejects before any S3 read, so no mock needed.
    org_id, slug, token = _seed_tenant_and_share()
    try:
        r = client.get(f"/api/public/share/{token}/file", params={"path": "Secret/x.md"})
        assert r.status_code == 404
    finally:
        _cleanup(org_id)


def test_public_file_traversal_rejected(client):
    org_id, slug, token = _seed_tenant_and_share()
    try:
        r = client.get(f"/api/public/share/{token}/file",
                       params={"path": "../../etc/passwd"})
        assert r.status_code in (400, 404)   # normalize 400 or scope 404
    finally:
        _cleanup(org_id)


def test_public_image_of_markdown_404(client):
    org_id, slug, token = _seed_tenant_and_share()
    try:
        r = client.get(f"/api/public/share/{token}/image",
                       params={"path": "Guide/intro.md"})
        assert r.status_code == 404
    finally:
        _cleanup(org_id)


def test_create_public_read_revoke_roundtrip(client, monkeypatch):
    from config_db import get_db_session, Tenant, WikiShare
    from routers import wiki
    org_id = secrets.token_hex(8)
    slug = f"cr-{org_id[:6]}"
    db = next(get_db_session())
    db.add(Tenant(id=org_id, name="CR", slug=slug,
                  lifecycle_status="active", is_active=True))
    db.commit()
    db.close()
    # S3 page-existence check on create → pretend the page exists.
    monkeypatch.setattr(wiki, "_s3_read", lambda org, path: b"# Hi\n")
    hdrs = {"X-Auth-User-Id": "u1", "X-Auth-Org-Id": org_id,
            "X-Auth-Org-Slug": slug, "X-Auth-Role": "org_admin"}
    try:
        r = client.post("/api/wiki/share",
                        json={"path": "Guide/intro.md", "resource_type": "page"},
                        headers=hdrs)
        assert r.status_code == 200, r.text
        tok = r.json()["token"]
        sid = r.json()["id"]

        assert client.get(f"/api/public/share/{tok}").status_code == 200
        # and the content read works through the (mocked) S3
        rf = client.get(f"/api/public/share/{tok}/file", params={"path": "Guide/intro.md"})
        assert rf.status_code == 200 and "Hi" in rf.json()["content"]

        assert client.delete(f"/api/wiki/share/{sid}", headers=hdrs).status_code == 200
        assert client.get(f"/api/public/share/{tok}").status_code == 404
    finally:
        _cleanup(org_id)
