"""SEC-08: over-long memory content and tags are refused with a 422.

Without a cap one token could store gigabytes of text a day. The caps are
generous; what they must do is refuse before anything reaches Qdrant.
"""
import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock

import main

HEADERS = {"X-Auth-Org-Slug": "acme", "X-Auth-User-Id": "u-alice",
           "X-Auth-Org-Id": "o1", "X-Auth-Role": "user"}


@pytest.fixture
def mock_q(monkeypatch):
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
    return TestClient(main.app)


@pytest.mark.parametrize("body", [
    {"content": "x" * (main.MAX_CONTENT_CHARS + 1)},
    {"content": "ok", "tags": ["t" * (main.MAX_TAG_CHARS + 1)]},
    {"content": "ok", "tags": [f"t{i}" for i in range(main.MAX_TAGS + 1)]},
], ids=["content", "tag-length", "tag-count"])
def test_over_long_create_is_refused(client, mock_q, body):
    r = client.post("/api/memories", json={**body, "tenant_slug": "acme"}, headers=HEADERS)
    assert r.status_code == 422
    assert mock_q.upsert.call_count == 0


def test_over_long_update_is_refused(client, mock_q):
    r = client.put("/api/memories/00000000-0000-0000-0000-000000000001",
                   json={"content": "x" * (main.MAX_CONTENT_CHARS + 1), "tenant_slug": "acme"},
                   headers=HEADERS)
    assert r.status_code == 422
    assert mock_q.upsert.call_count == 0


def test_content_at_the_cap_is_accepted(client, mock_q):
    r = client.post("/api/memories",
                    json={"content": "x" * main.MAX_CONTENT_CHARS, "tenant_slug": "acme"},
                    headers=HEADERS)
    assert r.status_code == 201, r.text
