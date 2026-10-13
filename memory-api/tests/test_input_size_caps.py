"""SEC-08: over-long capture, card and column fields are refused with a 422.

The request never reaches the handler, so nothing is stored; the tests use a
mocked engine to prove that no database call was made.
"""
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import main

AUTH = {"X-Internal-Service": "test-internal-service-token-cafef00d"}


@pytest.fixture
def client(monkeypatch):
    engine = MagicMock()
    monkeypatch.setattr(main, "_engine", engine)
    monkeypatch.setattr(main, "_qdrant", MagicMock())
    return TestClient(main.app), engine


CASES = [
    ("post", "/memory/capture", {"content": "x" * (main.MAX_CONTENT_CHARS + 1)}),
    ("post", "/memory/capture", {"content": "ok", "tags": ["t" * (main.MAX_NAME_CHARS + 1)]}),
    ("post", "/memory/capture", {"content": "ok", "tags": ["t"] * (main.MAX_LIST_ITEMS + 1)}),
    ("post", "/board/cards", {"title": "x" * (main.MAX_TITLE_CHARS + 1)}),
    ("post", "/board/cards", {"title": "ok", "body": "x" * (main.MAX_CARD_BODY_CHARS + 1)}),
    ("post", "/board/cards", {"title": "ok", "labels": ["l" * (main.MAX_NAME_CHARS + 1)]}),
    ("patch", "/board/cards/some-card", {"body": "x" * (main.MAX_CARD_BODY_CHARS + 1)}),
    ("post", "/board/columns", {"name": "x" * (main.MAX_NAME_CHARS + 1), "stage": "backlog"}),
]


@pytest.mark.parametrize("method,path,body", CASES,
                         ids=[f"{m} {p} {sorted(b)}" for m, p, b in CASES])
def test_over_long_field_is_refused(client, method, path, body):
    c, engine = client
    r = getattr(c, method)(path, json=body, headers=AUTH)
    assert r.status_code == 422, (r.status_code, r.text[:200])
    assert not engine.connect.called and not engine.begin.called


def test_card_body_under_the_cap_passes_validation():
    req = main.CardAddRequest(title="ok", body="x" * main.MAX_CARD_BODY_CHARS)
    assert len(req.body) == main.MAX_CARD_BODY_CHARS
