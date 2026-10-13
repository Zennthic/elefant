"""Tests for wiki page version history (the WikiRevision feature).

Three layers, none of which need a live S3:
  1. `_parse_frontmatter_meta` — pure frontmatter tag/title extraction.
  2. `_record_wiki_revision`   — the capture logic (changed_fields, no-op
     skip, non-md skip), exercised directly against a real DB session.
  3. The internal read endpoints (`/api/internal/wiki/history` and
     `/wiki/revision`) and the auth/validation guards on `/wiki/revert`,
     via TestClient with a seeded token + seeded revision rows.

The S3-touching half of revert (the actual restore write) is covered by the
revert guards failing BEFORE S3 is reached; the full restore path is left to
integration coverage where MinIO is available.
"""
import json
import secrets

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    import main
    return TestClient(main.app, headers={"Origin": "http://testserver"})


@pytest.fixture
def seeded_token():
    """Seed a Tenant + an ApiToken (wiki_read + wiki_write) and return the
    handles tests need. Cleaned up — including any revisions — afterwards."""
    import main  # noqa: F401 — import-time init_db() creates the tables
    from config_db import get_db_session, Tenant, ApiToken, WikiRevision

    db = next(get_db_session())
    org_id = secrets.token_hex(8)
    slug = f"wikihist-{org_id[:6]}"
    token_hash = "wikihist-" + secrets.token_hex(16)
    db.add(Tenant(id=org_id, name="WikiHist", slug=slug,
                  lifecycle_status="active", is_active=True))
    db.commit()
    db.add(ApiToken(name="hist-token", token_hash=token_hash, tenant_id=org_id,
                    user_id="user-1", source="claude_code",
                    wiki_read=True, wiki_write=True))
    db.commit()
    db.close()

    yield {"org_id": org_id, "slug": slug, "token_hash": token_hash}

    db2 = next(get_db_session())
    try:
        db2.query(WikiRevision).filter_by(tenant_id=org_id).delete()
        t = db2.query(ApiToken).filter_by(token_hash=token_hash).first()
        if t:
            db2.delete(t)
        o = db2.get(Tenant, org_id)
        if o:
            db2.delete(o)
        db2.commit()
    finally:
        db2.close()


def _seed_revision(org_id, path, content, **kw):
    """Insert a WikiRevision row directly; return its id."""
    from config_db import get_db_session, WikiRevision
    db = next(get_db_session())
    row = WikiRevision(
        tenant_id=org_id, path=path, content=content,
        tags=json.dumps(kw.get("tags", [])),
        title=kw.get("title"),
        edited_by_id=kw.get("edited_by_id"),
        edited_by_name=kw.get("edited_by_name"),
        source=kw.get("source", "editor"),
        message=kw.get("message"),
        changed_fields=json.dumps(kw.get("changed_fields", ["content"])),
        created=kw.get("created", False),
    )
    db.add(row)
    db.commit()
    rid = row.id
    db.close()
    return rid


# ── 1. _parse_frontmatter_meta ────────────────────────────────────────────

def test_frontmatter_flow_list():
    import wiki_pages
    tags, title = wiki_pages._parse_frontmatter_meta(
        "---\ntitle: My Page\ntags: [infra, ci, docker]\n---\n# Body\n")
    assert tags == ["infra", "ci", "docker"]
    assert title == "My Page"


def test_frontmatter_block_list():
    import wiki_pages
    tags, title = wiki_pages._parse_frontmatter_meta(
        "---\ntags:\n  - alpha\n  - beta\n---\nbody")
    assert tags == ["alpha", "beta"]
    assert title is None


def test_frontmatter_comma_string():
    import wiki_pages
    tags, _ = wiki_pages._parse_frontmatter_meta("---\ntags: one, two\n---\nx")
    assert tags == ["one", "two"]


def test_frontmatter_absent():
    import wiki_pages
    tags, title = wiki_pages._parse_frontmatter_meta("# Just a heading\n\nno frontmatter")
    assert tags == []
    assert title is None


# ── 2. _record_wiki_revision ──────────────────────────────────────────────

def _org(db, org_id):
    from config_db import Tenant
    return db.get(Tenant, org_id)


def test_record_create_then_noop(seeded_token):
    import wiki_pages
    from config_db import get_db_session, WikiRevision
    db = next(get_db_session())
    org = _org(db, seeded_token["org_id"])

    wiki_pages._record_wiki_revision(db, org, "AI/n.md", b"same body",
                                     source="editor", created=True)
    # Identical save → no second row.
    wiki_pages._record_wiki_revision(db, org, "AI/n.md", b"same body", source="editor")

    rows = db.query(WikiRevision).filter_by(tenant_id=org.id, path="AI/n.md").all()
    assert len(rows) == 1
    assert rows[0].content == "same body"
    assert json.loads(rows[0].changed_fields) == ["created"]
    db.close()


def test_record_content_change_tracks_fields(seeded_token):
    import wiki_pages
    from config_db import get_db_session, WikiRevision
    db = next(get_db_session())
    org = _org(db, seeded_token["org_id"])

    wiki_pages._record_wiki_revision(db, org, "AI/n.md", b"v1", created=True)
    wiki_pages._record_wiki_revision(db, org, "AI/n.md", b"v2")

    rows = (db.query(WikiRevision)
            .filter_by(tenant_id=org.id, path="AI/n.md")
            .order_by(WikiRevision.id.desc()).all())
    assert len(rows) == 2
    assert rows[0].content == "v2"
    assert json.loads(rows[0].changed_fields) == ["content"]
    db.close()


def test_record_tag_change_detected(seeded_token):
    import wiki_pages
    from config_db import get_db_session, WikiRevision
    db = next(get_db_session())
    org = _org(db, seeded_token["org_id"])

    wiki_pages._record_wiki_revision(db, org, "AI/n.md",
                                     b"---\ntags: [a]\n---\nbody", created=True)
    wiki_pages._record_wiki_revision(db, org, "AI/n.md",
                                     b"---\ntags: [a, b]\n---\nbody")

    latest = (db.query(WikiRevision)
              .filter_by(tenant_id=org.id, path="AI/n.md")
              .order_by(WikiRevision.id.desc()).first())
    assert "tags" in json.loads(latest.changed_fields)
    assert json.loads(latest.tags) == ["a", "b"]
    db.close()


def test_record_skips_non_markdown(seeded_token):
    import wiki_pages
    from config_db import get_db_session, WikiRevision
    db = next(get_db_session())
    org = _org(db, seeded_token["org_id"])

    wiki_pages._record_wiki_revision(db, org, "AI/data.txt", b"x", created=True)
    rows = db.query(WikiRevision).filter_by(tenant_id=org.id, path="AI/data.txt").all()
    assert rows == []
    db.close()


# ── 3. Internal endpoints (history / revision / revert guards) ────────────

def test_history_lists_newest_first(client, seeded_token):
    org_id, th = seeded_token["org_id"], seeded_token["token_hash"]
    _seed_revision(org_id, "AI/p.md", "first",  changed_fields=["created"])
    _seed_revision(org_id, "AI/p.md", "second", changed_fields=["content"],
                   edited_by_name="Bob")

    r = client.post("/api/internal/wiki/history",
                    json={"token_hash": th, "path": "AI/p.md"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 2
    # content is omitted from the list payload (use /wiki/revision to fetch it)
    assert "content" not in body["revisions"][0]
    # newest-first ordering by edited_at/id
    assert body["revisions"][0]["id"] > body["revisions"][1]["id"]


def test_history_is_tenant_scoped(client, seeded_token):
    org_id, th = seeded_token["org_id"], seeded_token["token_hash"]
    _seed_revision(org_id, "AI/p.md", "mine")
    _seed_revision("some-other-tenant", "AI/p.md", "theirs")  # must NOT leak

    r = client.post("/api/internal/wiki/history",
                    json={"token_hash": th, "path": "AI/p.md"})
    assert r.status_code == 200
    assert r.json()["count"] == 1


def test_history_requires_valid_token(client, seeded_token):
    r = client.post("/api/internal/wiki/history",
                    json={"token_hash": "not-a-real-token", "path": "AI/p.md"})
    assert r.status_code == 401


def test_revision_returns_content(client, seeded_token):
    org_id, th = seeded_token["org_id"], seeded_token["token_hash"]
    rid = _seed_revision(org_id, "AI/p.md", "the full content")

    r = client.post("/api/internal/wiki/revision",
                    json={"token_hash": th, "path": "AI/p.md", "revision_id": rid})
    assert r.status_code == 200, r.text
    assert r.json()["revision"]["content"] == "the full content"


def test_revision_unknown_id_404(client, seeded_token):
    th = seeded_token["token_hash"]
    r = client.post("/api/internal/wiki/revision",
                    json={"token_hash": th, "path": "AI/p.md", "revision_id": 9999999})
    assert r.status_code == 404


def test_revert_rejects_non_ai_path(client, seeded_token):
    """Revert must honour the same AI-folder confinement as wiki_write —
    refused before any storage write happens."""
    th = seeded_token["token_hash"]
    r = client.post("/api/internal/wiki/revert",
                    json={"token_hash": th, "path": "Notes/secret.md", "revision_id": 1})
    assert r.status_code == 403


def test_revert_unknown_revision_404(client, seeded_token):
    """An AI-folder path with a bogus revision id 404s before reaching S3."""
    th = seeded_token["token_hash"]
    r = client.post("/api/internal/wiki/revert",
                    json={"token_hash": th, "path": "AI/p.md", "revision_id": 9999999})
    assert r.status_code == 404


def test_revert_requires_wiki_write_scope(client, seeded_token):
    """A read-only token cannot revert."""
    from config_db import get_db_session, ApiToken
    db = next(get_db_session())
    ro_hash = "wikihist-ro-" + secrets.token_hex(8)
    db.add(ApiToken(name="ro", token_hash=ro_hash, tenant_id=seeded_token["org_id"],
                    wiki_read=True, wiki_write=False))
    db.commit()
    db.close()
    try:
        r = client.post("/api/internal/wiki/revert",
                        json={"token_hash": ro_hash, "path": "AI/p.md", "revision_id": 1})
        assert r.status_code == 403
    finally:
        db2 = next(get_db_session())
        t = db2.query(ApiToken).filter_by(token_hash=ro_hash).first()
        if t:
            db2.delete(t); db2.commit()
        db2.close()


# ── 4. Session-authed web endpoints (memory-ui History panel) ─────────────
# Identity comes from the X-Auth-* headers nginx injects (here supplied
# directly). org_admin role bypasses the wiki-space ACL in _check_space_access.

def _auth(org_id):
    return {"x-auth-user-id": "web-user-1", "x-auth-org-id": org_id,
            "x-auth-role": "org_admin"}


def test_web_history_lists_revisions(client, seeded_token):
    org_id = seeded_token["org_id"]
    _seed_revision(org_id, "AI/p.md", "v1", changed_fields=["created"])
    _seed_revision(org_id, "AI/p.md", "v2", changed_fields=["content"],
                   edited_by_name="Web Bob")

    r = client.get("/api/wiki/history?path=AI/p.md", headers=_auth(org_id))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 2
    assert "content" not in body["revisions"][0]          # omitted from the list
    assert body["revisions"][0]["id"] > body["revisions"][1]["id"]  # newest first


def test_web_history_is_org_scoped(client, seeded_token):
    org_id = seeded_token["org_id"]
    _seed_revision(org_id, "AI/p.md", "mine")
    _seed_revision("some-other-org", "AI/p.md", "theirs")  # must not leak
    r = client.get("/api/wiki/history?path=AI/p.md", headers=_auth(org_id))
    assert r.status_code == 200
    assert r.json()["count"] == 1


def test_web_revision_returns_content(client, seeded_token):
    org_id = seeded_token["org_id"]
    rid = _seed_revision(org_id, "AI/p.md", "the full body")
    r = client.get(f"/api/wiki/revision?path=AI/p.md&id={rid}", headers=_auth(org_id))
    assert r.status_code == 200, r.text
    assert r.json()["revision"]["content"] == "the full body"


def test_web_revision_unknown_404(client, seeded_token):
    org_id = seeded_token["org_id"]
    r = client.get("/api/wiki/revision?path=AI/p.md&id=9999999", headers=_auth(org_id))
    assert r.status_code == 404


def test_web_revert_unknown_revision_404(client, seeded_token):
    """Revert of a bogus revision 404s before any storage write."""
    org_id = seeded_token["org_id"]
    r = client.post("/api/wiki/revert",
                    json={"path": "AI/p.md", "revision_id": 9999999},
                    headers=_auth(org_id))
    assert r.status_code == 404
