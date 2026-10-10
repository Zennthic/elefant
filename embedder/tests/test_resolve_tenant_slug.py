"""Tests for embedder/main.py::_resolve_tenant_slug — THE function whose
ambiguity caused the cross-tenant data-leak audit finding AND the PR #68
regression that 403'd the editor's per-org auto-backup loop in production.

The function's contract:

  1. Internal-service callers (X-Internal-Service header valid) may
     specify any tenant_slug in the body/query.  This is the compactor
     and editor-admin path.
  2. Authenticated user requests via nginx have X-Auth-Org-Slug set by
     the editor's auth_check from the session's DB org row.  That slug
     is authoritative; a disagreeing body/query slug returns 403.
     "default" is tolerated as the Query default.
  3. No identity + no internal-service token + non-default body slug =
     refuse (403). Single-org legacy callers without a slug fall through
     to "default".

These cases together are what kept us from shipping a working /api/me
endpoint and a working admin compactor at the same time.  Any future
refactor to _resolve_tenant_slug MUST keep all these green or it breaks
isolation.
"""
import pytest
from fastapi import HTTPException


# ── helper: build a request-shaped object with the headers we care about ──

class _StubRequest:
    """Minimal stand-in for fastapi.Request — _resolve_tenant_slug only
    reads from request.headers.get(...), so we don't need a real one."""
    def __init__(self, headers=None):
        self._h = {k.lower(): v for k, v in (headers or {}).items()}

    @property
    def headers(self):
        class _H:
            def __init__(self, store):
                self._s = store
            def get(self, k, default=None):
                return self._s.get(k.lower(), default)
        return _H(self._h)


@pytest.fixture
def resolve():
    import main
    return main._resolve_tenant_slug


@pytest.fixture
def is_internal():
    import main
    return main._is_internal_service_request


# ── End-user (nginx-routed) flow ──────────────────────────────────────────

def test_auth_header_matches_body_slug_returns_slug(resolve):
    """Same slug in header and body → returns the slug."""
    req = _StubRequest({"X-Auth-Org-Slug": "acme"})
    assert resolve(req, "acme") == "acme"


def test_auth_header_present_body_default_returns_header(resolve):
    """User session calls /api/memories without a tenant_slug; the
    Query() default of "default" should NOT override their real org."""
    req = _StubRequest({"X-Auth-Org-Slug": "acme"})
    assert resolve(req, "default") == "acme"


def test_auth_header_present_body_empty_returns_header(resolve):
    """Empty string body slug behaves like missing — return the header."""
    req = _StubRequest({"X-Auth-Org-Slug": "acme"})
    assert resolve(req, "") == "acme"


def test_auth_header_disagrees_with_body_raises_403(resolve):
    """THE cross-tenant block.  Logged-in `acme` user passes
    ?tenant_slug=victim-corp → 403.  This is the PR #68 audit fix."""
    req = _StubRequest({"X-Auth-Org-Slug": "acme"})
    with pytest.raises(HTTPException) as exc:
        resolve(req, "victim-corp")
    assert exc.value.status_code == 403
    assert "does not match" in exc.value.detail.lower()


# ── Internal-service flow ────────────────────────────────────────────────

def test_internal_service_token_allows_any_slug(resolve, monkeypatch):
    """Compactor / editor-admin path: with a valid X-Internal-Service
    token, body slug is trusted (this is what makes per-org backup
    snapshots work — different from PR #68 regression cause)."""
    req = _StubRequest({"X-Internal-Service": "test-internal-service-token-cafef00d"})
    assert resolve(req, "victim-corp") == "victim-corp"


def test_internal_service_token_with_no_body_falls_through_to_default(resolve):
    req = _StubRequest({"X-Internal-Service": "test-internal-service-token-cafef00d"})
    assert resolve(req, "") == "default"


def test_internal_service_token_with_both_prefers_body(resolve):
    """An internal caller might present both headers (e.g. forwarded
    from nginx in a future shape); body wins because it's the
    deliberately-specified target."""
    req = _StubRequest({
        "X-Internal-Service": "test-internal-service-token-cafef00d",
        "X-Auth-Org-Slug": "acme",
    })
    assert resolve(req, "victim-corp") == "victim-corp"


def test_wrong_internal_token_falls_through_to_auth_check(resolve):
    """A garbage X-Internal-Service header is NOT an internal call —
    handler should treat the request as end-user-shaped instead."""
    req = _StubRequest({
        "X-Internal-Service": "wrong-token",
        "X-Auth-Org-Slug": "acme",
    })
    # User has acme header; passing victim → 403 (same as user-only path)
    with pytest.raises(HTTPException) as exc:
        resolve(req, "victim-corp")
    assert exc.value.status_code == 403


# ── No identity at all ────────────────────────────────────────────────────

def test_no_identity_no_body_returns_default(resolve):
    """Bare default path — single-org callers with no auth still work."""
    req = _StubRequest({})
    assert resolve(req, "") == "default"


def test_no_identity_explicit_default_returns_default(resolve):
    req = _StubRequest({})
    assert resolve(req, "default") == "default"


def test_no_identity_non_default_body_raises_403(resolve):
    """No auth header AND no internal token AND a non-default slug =
    attacker poking the embedder from a foreign network OR a
    misconfigured legacy caller.  Either way: refuse."""
    req = _StubRequest({})
    with pytest.raises(HTTPException) as exc:
        resolve(req, "acme")
    assert exc.value.status_code == 403


# ── _is_internal_service_request unit ─────────────────────────────────────

def test_is_internal_service_with_valid_token_true(is_internal):
    req = _StubRequest({"X-Internal-Service": "test-internal-service-token-cafef00d"})
    assert is_internal(req) is True


def test_is_internal_service_with_wrong_token_false(is_internal):
    req = _StubRequest({"X-Internal-Service": "wrong-token"})
    assert is_internal(req) is False


def test_is_internal_service_with_missing_header_false(is_internal):
    req = _StubRequest({})
    assert is_internal(req) is False


def test_is_internal_service_unconfigured_always_false(is_internal, monkeypatch):
    """Fail-closed safety net: even a CORRECT header string returns
    False if the server has no token configured.  Otherwise we'd be
    accepting any caller as 'internal' on a mis-configured deploy."""
    import main
    monkeypatch.setattr(main, "INTERNAL_SERVICE_TOKEN", "")
    # Even sending the previously-valid token now returns False since
    # the comparator has nothing real to compare against.
    req = _StubRequest({"X-Internal-Service": "test-internal-service-token-cafef00d"})
    assert is_internal(req) is False
