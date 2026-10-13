"""POST /api/memories attribution and caller-supplied ids.

A create that names an existing id replaces that point, so it runs the same
author/admin check as PUT /api/memories/{id}. A normal caller always writes
as themselves: author_id is their own and any owner: tag must be theirs.
Admins and internal services (compactor promotion, org restore) keep the
ability to set attribution explicitly.
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock


VALID_INTERNAL = "test-internal-service-token-cafef00d"
VICTIM_ID = str(uuid.uuid4())


@pytest.fixture
def mock_q(monkeypatch):
    import main
    q = MagicMock()
    q.scroll.return_value = ([], None)
    q.search.return_value = []
    q.retrieve.return_value = []
    q.get_collections.return_value.collections = []
    monkeypatch.setattr(main, "qdrant", lambda: q)
    monkeypatch.setattr(main, "_ensure_tenant_collections_lazy", lambda *_: None)
    monkeypatch.setattr(main, "get_embedding", lambda _: [0.0] * 32)
    return q


@pytest.fixture
def client(mock_q):
    import main
    return TestClient(main.app)


def _user(user_id="u-alice", role="user", owner_name=""):
    h = {"X-Auth-Org-Slug": "acme", "X-Auth-User-Id": user_id,
         "X-Auth-Org-Id": "o1", "X-Auth-Role": role}
    if owner_name:
        h["X-Auth-Owner-Name"] = owner_name
    return h


def _existing(mock_q, payload):
    point = MagicMock()
    point.payload = payload
    mock_q.retrieve.return_value = [point]


def _written(mock_q):
    assert mock_q.upsert.call_count == 1
    return mock_q.upsert.call_args.kwargs["points"][0].payload


# ── Caller-supplied id ───────────────────────────────────────────────────

def test_id_of_other_users_memory_is_refused(client, mock_q):
    _existing(mock_q, {"author_id": "u-bob", "tags": ["owner:u-bob"],
                       "tenant_id": "acme"})
    r = client.post("/api/memories",
                    json={"id": VICTIM_ID, "content": "x", "tenant_slug": "acme"},
                    headers=_user())
    assert r.status_code == 403
    assert mock_q.upsert.call_count == 0


def test_id_of_own_memory_is_allowed(client, mock_q):
    _existing(mock_q, {"author_id": "u-alice", "tenant_id": "acme"})
    r = client.post("/api/memories",
                    json={"id": VICTIM_ID, "content": "x", "tenant_slug": "acme"},
                    headers=_user())
    assert r.status_code == 201, r.text
    assert _written(mock_q)["id"] == VICTIM_ID


def test_id_of_memory_owned_by_callers_token_name_is_allowed(client, mock_q):
    _existing(mock_q, {"tags": ["owner:Claude"], "tenant_id": "acme"})
    r = client.post("/api/memories",
                    json={"id": VICTIM_ID, "content": "x", "tenant_slug": "acme"},
                    headers=_user(owner_name="Claude"))
    assert r.status_code == 201, r.text


def test_unused_id_is_allowed(client, mock_q):
    r = client.post("/api/memories",
                    json={"id": VICTIM_ID, "content": "x", "tenant_slug": "acme"},
                    headers=_user())
    assert r.status_code == 201, r.text
    assert _written(mock_q)["id"] == VICTIM_ID


def test_admin_may_replace_other_users_memory(client, mock_q):
    _existing(mock_q, {"author_id": "u-bob", "tenant_id": "acme"})
    r = client.post("/api/memories",
                    json={"id": VICTIM_ID, "content": "x", "tenant_slug": "acme"},
                    headers=_user(role="org_admin"))
    assert r.status_code == 201, r.text


def test_no_identity_may_not_replace(client, mock_q):
    _existing(mock_q, {"author_id": "u-bob", "tenant_id": None})
    r = client.post("/api/memories",
                    json={"id": VICTIM_ID, "content": "x"})
    assert r.status_code == 403
    assert mock_q.upsert.call_count == 0


def test_internal_restore_keeps_id_and_attribution(client, mock_q):
    _existing(mock_q, {"author_id": "u-bob", "tenant_id": "acme"})
    r = client.post("/api/memories",
                    json={"id": VICTIM_ID, "content": "x", "tenant_slug": "acme",
                          "author_id": "u-bob", "tags": ["owner:u-bob"]},
                    headers={"X-Internal-Service": VALID_INTERNAL})
    assert r.status_code == 201, r.text
    p = _written(mock_q)
    assert p["id"] == VICTIM_ID
    assert p["author_id"] == "u-bob"
    assert p["tags"] == ["owner:u-bob"]
    assert mock_q.retrieve.call_count == 0


# ── author_id / owner: tags ──────────────────────────────────────────────

def test_supplied_author_and_owner_tag_are_replaced(client, mock_q):
    r = client.post("/api/memories",
                    json={"content": "x", "tenant_slug": "acme",
                          "author_id": "u-bob", "tags": ["owner:u-bob", "keep"]},
                    headers=_user())
    assert r.status_code == 201, r.text
    p = _written(mock_q)
    assert p["author_id"] == "u-alice"
    assert p["tags"] == ["keep", "owner:u-alice"]


def test_callers_own_token_name_tag_is_kept(client, mock_q):
    # The MCP's memory_add attributes by owner:<token-name>.
    r = client.post("/api/memories",
                    json={"content": "x", "tenant_slug": "acme",
                          "tags": ["owner:Claude", "owner:Mallory"]},
                    headers=_user(owner_name="Claude"))
    assert r.status_code == 201, r.text
    p = _written(mock_q)
    assert p["tags"] == ["owner:Claude"]
    assert p["author_id"] == "u-alice"


def test_admin_may_set_attribution(client, mock_q):
    r = client.post("/api/memories",
                    json={"content": "x", "tenant_slug": "acme",
                          "author_id": "u-bob", "tags": ["owner:u-bob"]},
                    headers=_user(role="org_admin"))
    assert r.status_code == 201, r.text
    p = _written(mock_q)
    assert p["author_id"] == "u-bob"
    assert p["tags"] == ["owner:u-bob"]


def test_compactor_promotion_keeps_owner_name_tag(client, mock_q):
    r = client.post("/api/memories",
                    json={"content": "x", "tenant_slug": "acme",
                          "tags": ["owner:Claude", "hot-tier-promoted"],
                          "originated_from_hot_ids": ["h1"]},
                    headers={"X-Internal-Service": VALID_INTERNAL})
    assert r.status_code == 201, r.text
    p = _written(mock_q)
    assert p["tags"] == ["owner:Claude", "hot-tier-promoted"]
    assert p["author_id"] is None
