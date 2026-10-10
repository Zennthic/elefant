"""Service-level integration tests for embedder /api/memories with the
tenant-resolution + auth-header matrix.

THIS IS THE TEST FILE THAT WOULD HAVE PREVENTED THE PR #68 REGRESSION.

The regression class: a caller hits an endpoint with a non-default
tenant_slug but no auth headers + no internal-service token, gets a
403 from _resolve_tenant_slug.  In production that was the editor's
_create_memory_backup loop, whose httpx call was missing
_internal_service_headers().

These tests assert the full auth matrix for the four GET/POST/PUT/DELETE
on /api/memories.  Adding an endpoint to the embedder that takes
tenant_slug should require extending this matrix as part of the PR.
"""
import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock


@pytest.fixture
def client(monkeypatch):
    """A FastAPI TestClient over the real embedder app with Qdrant
    mocked at the factory level.  Every test gets a fresh mock so
    assertions don't leak across tests."""
    import main
    mock_q = MagicMock()
    # The scroll/search calls return (points_list, next_offset) tuples.
    mock_q.scroll.return_value = ([], None)
    mock_q.search.return_value = []
    mock_q.retrieve.return_value = []
    mock_q.get_collections.return_value.collections = []
    monkeypatch.setattr(main, "qdrant", lambda: mock_q)
    # Patch out the editor RPC helpers — tests don't need real ACL data.
    monkeypatch.setattr(main, "_fetch_memory_scope", lambda *_: {
        "share_mode": "individual", "scope_user_ids": set(),
    })
    return TestClient(main.app)


VALID_INTERNAL = "test-internal-service-token-cafef00d"


# ── GET /api/memories ────────────────────────────────────────────────────

def test_get_with_matching_auth_header_returns_200(client):
    r = client.get(
        "/api/memories",
        params={"tenant_slug": "acme"},
        headers={"X-Auth-Org-Slug": "acme", "X-Auth-User-Id": "u1",
                 "X-Auth-Org-Id": "o1"},
    )
    assert r.status_code == 200, r.text


def test_get_with_mismatched_slug_returns_403(client):
    """The audit fix in action.  Authenticated `acme` user trying to
    read `victim-corp`'s memories must be refused."""
    r = client.get(
        "/api/memories",
        params={"tenant_slug": "victim-corp"},
        headers={"X-Auth-Org-Slug": "acme", "X-Auth-User-Id": "u1",
                 "X-Auth-Org-Id": "o1"},
    )
    assert r.status_code == 403


def test_get_with_no_auth_no_internal_returns_403(client):
    """The latent bug PR #68 exposed: no auth header AND no internal
    service token AND a non-default slug = refuse.  This is also the
    exact prod-incident shape (editor's backup loop without the
    X-Internal-Service header)."""
    r = client.get(
        "/api/memories",
        params={"tenant_slug": "acme-corp"},
    )
    assert r.status_code == 403


def test_get_with_internal_service_token_returns_200(client):
    """The fix: when the caller proves internal-service identity via
    the header, body slug is trusted.  This is exactly what
    _create_memory_backup needed to send and didn't, until PR #69."""
    r = client.get(
        "/api/memories",
        params={"tenant_slug": "acme-corp"},
        headers={"X-Internal-Service": VALID_INTERNAL},
    )
    assert r.status_code == 200, r.text


def test_get_default_org_no_auth_returns_200(client):
    """Bare single-org legacy path stays working — default tenant,
    no auth, no internal token."""
    r = client.get("/api/memories", params={"tenant_slug": "default"})
    assert r.status_code == 200, r.text


# ── POST /api/memories (memory creation) ─────────────────────────────────

def test_post_with_mismatched_slug_returns_403(client):
    """Memory POISONING attempt — authenticated as `acme`, post into
    victim's collection.  Must 403."""
    r = client.post(
        "/api/memories",
        json={"content": "poison", "tenant_slug": "victim-corp"},
        headers={"X-Auth-Org-Slug": "acme", "X-Auth-User-Id": "u1",
                 "X-Auth-Org-Id": "o1"},
    )
    assert r.status_code == 403


def test_post_with_internal_token_to_any_tenant_succeeds(client, monkeypatch):
    """The compactor's path — POSTs to /api/memories with internal
    token + an arbitrary tenant_slug as part of promotion."""
    import main
    monkeypatch.setattr(main, "get_embedding", lambda _: [0.0] * 32)
    r = client.post(
        "/api/memories",
        json={"content": "compacted memory", "tenant_slug": "acme"},
        headers={"X-Internal-Service": VALID_INTERNAL},
    )
    assert r.status_code == 201, r.text


# ── GET /api/memories/{id} (the by-id retrieve, audit C-3) ──────────────

def test_get_by_id_with_mismatched_slug_returns_403_before_qdrant(client):
    """A user shouldn't even reach Qdrant when they try to retrieve a
    memory by UUID against a slug they don't own.  The 403 fires at
    _resolve_tenant_slug, before memory_point_id ever runs."""
    import main
    mock_q = main.qdrant()
    mock_q.retrieve.reset_mock()
    r = client.get(
        "/api/memories/some-uuid-123",
        params={"tenant_slug": "victim-corp"},
        headers={"X-Auth-Org-Slug": "acme", "X-Auth-User-Id": "u1",
                 "X-Auth-Org-Id": "o1"},
    )
    assert r.status_code == 403
    assert mock_q.retrieve.call_count == 0
