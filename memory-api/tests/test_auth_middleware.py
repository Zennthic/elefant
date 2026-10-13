"""Tests for the require_internal_service ASGI middleware.

memory-api had ZERO authentication before PR #68.  The middleware
introduced there is the entire perimeter — every endpoint except
/health must present a valid X-Internal-Service header or get 401.
A 503 fires only when the operator hasn't configured the token (a
deploy that comes up with no env var: every call returns 503
'memory-api is mis-configured' until it's set).

A regression here re-opens the whole internal-network bypass the
audit found.
"""
import os
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    """A bare TestClient over memory-api with mocked DB/Qdrant so the
    middleware tests don't need a real Postgres."""
    from unittest.mock import MagicMock
    import main
    main._engine = MagicMock()
    main._qdrant = MagicMock()
    return TestClient(main.app)


# ── happy path ────────────────────────────────────────────────────────────

def test_health_open_no_token(client):
    """Docker healthcheck must work without the token — otherwise no
    container is ever 'healthy' and depends_on blocks."""
    r = client.get("/health")
    assert r.status_code in (200, 503)  # 503 if Qdrant/PG mocked-out poorly
    # The middleware itself shouldn't have gated this.


def test_valid_token_accepted(client):
    """Send the token the conftest set INTERNAL_SERVICE_TOKEN to —
    middleware lets the request through (handler may still 4xx/5xx
    on body validation, but NOT on auth)."""
    r = client.get(
        "/memory/compact/config",
        headers={"X-Internal-Service": "test-internal-service-token-cafef00d"},
    )
    # 200, 500, anything — just NOT a 401/503 from the middleware
    assert r.status_code not in (401, 503), \
        f"middleware rejected a valid token; status={r.status_code}, body={r.text[:200]}"


# ── 401 rejections ────────────────────────────────────────────────────────

def test_missing_header_rejected(client):
    r = client.get("/memory/compact/config")
    assert r.status_code == 401
    assert "authentication" in r.json()["detail"].lower()


def test_empty_header_rejected(client):
    r = client.get(
        "/memory/compact/config",
        headers={"X-Internal-Service": ""},
    )
    assert r.status_code == 401


def test_wrong_token_rejected(client):
    r = client.get(
        "/memory/compact/config",
        headers={"X-Internal-Service": "not-the-right-token"},
    )
    assert r.status_code == 401


def test_partial_match_rejected(client):
    """Constant-time comparison should reject a prefix of the real
    token — partial matches must not work."""
    real = "test-internal-service-token-cafef00d"
    r = client.get(
        "/memory/compact/config",
        headers={"X-Internal-Service": real[:-2]},
    )
    assert r.status_code == 401


# ── 503 (mis-configuration) — a deploy with the token unset ─────────────

def test_missing_token_returns_503(client, monkeypatch):
    """An operator deploys without setting ELEFANT_INTERNAL_SERVICE_TOKEN; the middleware
    returns 503 instead of silently accepting traffic."""
    import main
    monkeypatch.setattr(main, "INTERNAL_SERVICE_TOKEN", "")
    r = client.get(
        "/memory/compact/config",
        headers={"X-Internal-Service": "anything"},
    )
    assert r.status_code == 503
    assert "mis-configured" in r.json()["detail"].lower()


def test_missing_token_health_still_open(client, monkeypatch):
    """Even with no INTERNAL_SERVICE_TOKEN, /health stays open so the
    container can be marked 'healthy' / depends_on can resolve."""
    import main
    monkeypatch.setattr(main, "INTERNAL_SERVICE_TOKEN", "")
    r = client.get("/health")
    assert r.status_code != 401  # /health is not gated


# ── exemption coverage ──────────────────────────────────────────────────

def test_exempt_paths_are_only_health(client):
    """Document the full exempt set so a future addition to _AUTH_
    EXEMPT_PATHS is a deliberate decision visible in a test diff."""
    import main
    assert main._AUTH_EXEMPT_PATHS == ("/health",)
