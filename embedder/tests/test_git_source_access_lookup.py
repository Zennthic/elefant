"""The git-source access check must deny when the editor lookup fails.

`_fetch_git_source_access` asks the editor which git sources a user may
read. If that call errors or times out, the embedder cannot know the
answer, so it must return the empty set, the same way
`_fetch_memory_scope` falls back to the caller's own scope.
"""
import urllib.request
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient


def _editor_down(*_args, **_kwargs):
    raise OSError("editor unreachable")


def test_lookup_failure_returns_empty_set(monkeypatch):
    import main
    monkeypatch.setattr(urllib.request, "urlopen", _editor_down)
    assert main._fetch_git_source_access("u1", "o1", ["src-a", "src-b"]) == set()


@pytest.fixture
def client(monkeypatch):
    import main
    mock_q = MagicMock()
    mock_q.search.return_value = [
        SimpleNamespace(score=0.9, payload={
            "source_type": "git_source", "source_id": "src-private",
            "path": "repo/secret.py", "title": "secret.py",
            "tenant_id": "acme",
        }),
    ]
    monkeypatch.setattr(main, "qdrant", lambda: mock_q)
    monkeypatch.setattr(main, "get_embedding", lambda _q: [0.0])
    monkeypatch.setattr(main, "_resolve_allowed_spaces", lambda ctx: set())
    monkeypatch.setattr(urllib.request, "urlopen", _editor_down)
    return TestClient(main.app)


AUTH = {"X-Auth-Org-Slug": "acme", "X-Auth-User-Id": "u1", "X-Auth-Org-Id": "o1"}


def test_search_hides_git_source_hits_when_lookup_fails(client):
    r = client.get("/api/search", params={"q": "secret", "tenant_slug": "acme"},
                   headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["results"] == []


def test_source_tree_denied_when_lookup_fails(client):
    r = client.get("/api/git-sources/src-private/tree", headers=AUTH)
    assert r.status_code == 403
