"""The agent-facing edit / move / delete endpoints, and the boundary they hold.

These are new write paths reachable by any token with the wiki_write scope, so
the thing worth testing hardest is not that they work but what they refuse:
a token without the scope, and — the constraint the whole AI-folder design
rests on — any path outside it. A page a human promoted out of the AI folder is
permanently beyond an agent's reach, and a move has TWO paths to check, because
moving a page out would be the agent promoting its own work.
"""
import pytest
from fastapi.testclient import TestClient

import main
from served_routes import served_paths


@pytest.fixture
def client():
    return TestClient(main.app)


@pytest.fixture
def tenant():
    """A real tenant with a real token, so the endpoints authenticate for
    real rather than against a patched resolver."""
    import hashlib
    import secrets
    from config_db import get_db_session, Tenant, ApiToken, TenantSettings

    def sha(raw):
        return hashlib.sha256(raw.encode()).hexdigest()

    db = next(get_db_session())
    oid = secrets.token_hex(8)
    slug = f"wm-{oid[:6]}"
    db.add(Tenant(id=oid, name=slug, slug=slug, display_name=slug,
                  domain=f"{slug}.test", lifecycle_status="active", is_active=True))
    db.add(TenantSettings(tenant_slug=slug, ai_wiki_folder="AI"))
    rw, ro = sha(secrets.token_urlsafe(24)), sha(secrets.token_urlsafe(24))
    db.add(ApiToken(name="agent", token_hash=rw, tenant_id=oid,
                    wiki_read=True, wiki_write=True))
    db.add(ApiToken(name="reader", token_hash=ro, tenant_id=oid,
                    wiki_read=True, wiki_write=False))
    db.commit(); db.close()
    return {"slug": slug, "id": oid, "hash": rw, "ro_hash": ro}


def _post(client, ep, **body):
    return client.post(f"/api/internal/wiki/{ep}", json=body)


# ── the boundary ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("ep,body", [
    ("edit", {"path": "AI/a.md", "mode": "append", "content": "x"}),
    ("move", {"from_path": "AI/a.md", "to_path": "AI/b.md"}),
    ("delete", {"path": "AI/a.md"}),
])
def test_a_bad_token_is_rejected(client, ep, body):
    assert _post(client, ep, token_hash="deadbeef", **body).status_code == 401


@pytest.mark.parametrize("ep,body", [
    ("edit", {"path": "AI/a.md", "mode": "append", "content": "x"}),
    ("move", {"from_path": "AI/a.md", "to_path": "AI/b.md"}),
    ("delete", {"path": "AI/a.md"}),
])
def test_read_only_token_cannot_mutate(client, tenant, ep, body):
    r = _post(client, ep, token_hash=tenant["ro_hash"], **body)
    assert r.status_code == 403
    assert "wiki_write" in r.json()["detail"]


@pytest.mark.parametrize("path", [
    "Runbooks/promoted.md",          # promoted out of the AI folder
    "notes.md",                      # top level
    "AInotes/x.md",                  # prefix lookalike, not the folder
])
def test_edit_refuses_paths_outside_the_ai_folder(client, tenant, path):
    r = _post(client, "edit", token_hash=tenant["hash"], path=path,
              mode="append", content="x")
    assert r.status_code == 403
    assert "promoted" in r.json()["detail"]


def test_delete_refuses_a_promoted_page(client, tenant):
    r = _post(client, "delete", token_hash=tenant["hash"], path="Runbooks/promoted.md")
    assert r.status_code == 403


def test_move_checks_the_destination_too(client, tenant):
    """Moving a page out of the AI folder would be the agent promoting its own
    work as human-reviewed. Checking only the source would allow exactly that."""
    r = _post(client, "move", token_hash=tenant["hash"],
              from_path="AI/draft.md", to_path="Runbooks/draft.md")
    assert r.status_code == 403
    assert "destination" in r.json()["detail"]


def test_move_checks_the_source_too(client, tenant):
    r = _post(client, "move", token_hash=tenant["hash"],
              from_path="Runbooks/promoted.md", to_path="AI/stolen.md")
    assert r.status_code == 403
    assert "source" in r.json()["detail"]


def test_move_rejects_traversal(client, tenant):
    r = _post(client, "move", token_hash=tenant["hash"],
              from_path="AI/a.md", to_path="AI/../../etc/passwd")
    assert r.status_code in (400, 403)


# ── argument validation that doesn't need a bucket ─────────────────────────

def test_move_onto_itself_is_refused(client, tenant):
    r = _post(client, "move", token_hash=tenant["hash"],
              from_path="AI/a.md", to_path="AI/a.md")
    assert r.status_code == 422
    assert "same" in r.json()["detail"]


def test_a_folder_cannot_be_moved_inside_itself(client, tenant):
    """The copy loop would walk into its own output."""
    r = _post(client, "move", token_hash=tenant["hash"],
              from_path="AI/projects", to_path="AI/projects/archive")
    assert r.status_code == 422
    assert "into itself" in r.json()["detail"]


def test_edit_rejects_an_unknown_mode_before_touching_storage(client, tenant):
    r = _post(client, "edit", token_hash=tenant["hash"], path="AI/a.md",
              mode="patch", content="x")
    # 404 (no such page) or 422 (bad mode) — either way, never a 500.
    assert r.status_code in (404, 422)


def test_editing_a_missing_page_points_at_wiki_write(client, tenant):
    r = _post(client, "edit", token_hash=tenant["hash"],
              path="AI/nope.md", mode="append", content="x")
    assert r.status_code == 404
    assert "wiki_write" in r.json()["detail"]


def test_bad_json_is_a_400(client, tenant):
    r = client.post("/api/internal/wiki/edit", content=b"not json",
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 400


# ── wiring ─────────────────────────────────────────────────────────────────

def test_routes_are_registered():
    paths = served_paths(main.app)
    for p in ("/api/internal/wiki/edit", "/api/internal/wiki/move",
              "/api/internal/wiki/delete"):
        assert p in paths


def test_every_mutation_endpoint_goes_through_the_shared_gate():
    """Three endpoints, one boundary. A new one that forgets the guard is the
    way this protection quietly stops applying."""
    import inspect
    for fn in (main.internal_wiki_edit, main.internal_wiki_move,
               main.internal_wiki_delete):
        src = inspect.getsource(fn)
        assert "_internal_wiki_writer" in src, fn.__name__
        assert "_guard_ai_path" in src, fn.__name__
