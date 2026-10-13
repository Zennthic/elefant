"""The peer-service card wiki-thread endpoint (MCP `card_get`).

MCP reads cards straight from memory-api, which stores only the explicit
`wiki_links` — resolving the pages associated by convention needs the tenant's
AI folder and S3, which only the editor has. This endpoint exposes the same
enrichment the browser gets, so it must be gated to peer services (it reveals
which wiki pages exist for a tenant) and must never fail a card read.
"""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    import main
    return TestClient(main.app)


def _tok():
    import internal_service
    return internal_service.INTERNAL_SERVICE_TOKEN


_BODY = {"tenant_slug": "default", "slug": "netdisco",
         "aliases": [], "wiki_links": []}


def test_requires_internal_service_token(client):
    r = client.post("/api/internal/board/wiki-pages", json=_BODY)
    assert r.status_code == 403


def test_rejects_wrong_token(client):
    r = client.post("/api/internal/board/wiki-pages", json=_BODY,
                    headers={"X-Internal-Service": "not-the-token"})
    assert r.status_code == 403


def test_requires_tenant_and_slug(client):
    r = client.post("/api/internal/board/wiki-pages",
                    json={"tenant_slug": "default"},
                    headers={"X-Internal-Service": _tok()})
    assert r.status_code == 400


def test_returns_thread_shape_for_valid_call(client):
    """An unknown tenant/card yields an empty thread rather than an error —
    card_get must not break because the thread couldn't be resolved."""
    r = client.post("/api/internal/board/wiki-pages", json=_BODY,
                    headers={"X-Internal-Service": _tok()})
    assert r.status_code == 200
    assert isinstance(r.json()["wiki_pages"], list)
