"""A failed embedder call names a ref, not the exception.

Raw exception text in a 500 carried Qdrant collection names and backend
detail to whoever made the call. The detail belongs in the log, under the
same ref the response carries.
"""
import logging
import re

import pytest
from fastapi.testclient import TestClient
from unittest.mock import MagicMock

SECRET = "collection acme_memories at http://qdrant:6333"
_REF = re.compile(r"\(ref ([0-9a-f]{8})\)")
_USER = {"X-Auth-Org-Slug": "acme", "X-Auth-User-Id": "u-alice",
         "X-Auth-Org-Id": "o1", "X-Auth-Role": "user"}


@pytest.fixture
def mock_q(monkeypatch):
    import main
    q = MagicMock()
    q.scroll.return_value = ([], None)
    q.retrieve.return_value = []
    q.get_collections.return_value.collections = []
    monkeypatch.setattr(main, "qdrant", lambda: q)
    monkeypatch.setattr(main, "_ensure_tenant_collections_lazy", lambda *_: None)
    monkeypatch.setattr(main, "get_embedding", lambda _: [0.0] * 32)
    return q


@pytest.fixture
def client(mock_q):
    import main
    return TestClient(main.app, raise_server_exceptions=False)


def _assert_generic(r, caplog):
    assert r.status_code == 500
    detail = r.json()["detail"]
    assert "qdrant" not in detail.lower() and "acme_memories" not in detail
    m = _REF.search(detail)
    assert m, detail
    logged = "\n".join(rec.getMessage() for rec in caplog.records)
    assert m.group(1) in logged and "acme_memories" in logged


def test_embedding_failure_is_generic(client, monkeypatch, caplog):
    import main

    def boom(_):
        raise RuntimeError(SECRET)
    monkeypatch.setattr(main, "get_embedding", boom)
    with caplog.at_level(logging.ERROR):
        r = client.post("/api/memories", json={"content": "x", "tenant_slug": "acme"},
                        headers=_USER)
    _assert_generic(r, caplog)


def test_upsert_failure_is_generic(client, mock_q, caplog):
    mock_q.upsert.side_effect = RuntimeError(SECRET)
    with caplog.at_level(logging.ERROR):
        r = client.post("/api/memories", json={"content": "x", "tenant_slug": "acme"},
                        headers=_USER)
    _assert_generic(r, caplog)


def test_existing_id_lookup_failure_is_generic(client, mock_q, caplog):
    mock_q.retrieve.side_effect = RuntimeError(SECRET)
    with caplog.at_level(logging.ERROR):
        r = client.post("/api/memories",
                        json={"id": "3f1c2a8e-0000-4000-8000-000000000001",
                              "content": "x", "tenant_slug": "acme"},
                        headers=_USER)
    _assert_generic(r, caplog)
