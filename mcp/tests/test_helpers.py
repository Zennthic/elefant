"""Unit tests for the MCP server's request-shaped helpers.

Most of these are tiny but they back EVERY tool's auth-forwarding behavior
— a regression in `_auth_headers` (forgetting one of the X-Auth-* keys)
would silently break tenant-resolution on the embedder side for one
class of caller, which is exactly the kind of bug we want pinned down."""
import pytest


@pytest.fixture
def helpers():
    import main
    return main


def test_token_extracts_from_scope(helpers, fake_ctx):
    info = {"tenant_slug": "acme", "user_id": "u1"}
    ctx = fake_ctx(token_info=info)
    assert helpers._token(ctx) == info


def test_token_returns_empty_dict_when_missing(helpers, fake_ctx):
    """Defensive: the helper never raises on a missing scope key.
    Callers downstream use .get(...)."""
    ctx = fake_ctx(token_info=None)
    assert helpers._token(ctx) == {}


def test_token_hash_extracts_from_scope(helpers, fake_ctx):
    ctx = fake_ctx(token_hash="hash-abc")
    assert helpers._token_hash(ctx) == "hash-abc"


def test_tenant_slug_from_token(helpers, fake_ctx):
    ctx = fake_ctx(token_info={"tenant_slug": "victim"})
    assert helpers._tenant_slug(ctx) == "victim"


def test_tenant_slug_default_when_missing(helpers, fake_ctx):
    """Token-less context (shouldn't happen in prod — middleware
    rejects unauth — but be defensive)."""
    ctx = fake_ctx(token_info={})
    assert helpers._tenant_slug(ctx) == "default"


# ── _auth_headers ────────────────────────────────────────────────────────

def test_auth_headers_full_token(helpers):
    info = {
        "user_id":     "u1",
        "org_id":      "o1",
        "tenant_slug": "acme",
    }
    hdrs = helpers._auth_headers(info)
    assert hdrs["X-Auth-User-Id"]  == "u1"
    assert hdrs["X-Auth-Org-Id"]   == "o1"
    assert hdrs["X-Auth-Org-Slug"] == "acme"


def test_auth_headers_empty_dict_on_empty_token(helpers):
    assert helpers._auth_headers({}) == {}


def test_auth_headers_tenant_id_fallback_for_org(helpers):
    """Legacy tokens carry tenant_id instead of org_id — the helper
    falls back so both shapes resolve to X-Auth-Org-Id."""
    info = {"user_id": "u1", "tenant_id": "tnt-1", "tenant_slug": "acme"}
    hdrs = helpers._auth_headers(info)
    assert hdrs["X-Auth-Org-Id"] == "tnt-1"


def test_auth_headers_no_slug_omits_slug_header(helpers):
    info = {"user_id": "u1", "org_id": "o1"}
    hdrs = helpers._auth_headers(info)
    assert "X-Auth-Org-Slug" not in hdrs


# ── _internal_service_headers ────────────────────────────────────────────

def test_internal_service_headers_when_configured(helpers):
    hdrs = helpers._internal_service_headers()
    assert hdrs == {"X-Internal-Service": "test-internal-service-token-cafef00d"}


def test_internal_service_headers_empty_when_unconfigured(helpers, monkeypatch):
    monkeypatch.setattr(helpers, "INTERNAL_SERVICE_TOKEN", "")
    assert helpers._internal_service_headers() == {}


# ── _override_key (session source-toggles namespacing) ───────────────────

def test_override_key_namespaces_by_token_hash(helpers, fake_ctx):
    """Two callers with the same mcp-session-id but different tokens
    must not see each other's per-session source toggles.  Confirm
    the key includes both."""
    a = fake_ctx(token_hash="hash-a", session_id="session-x")
    b = fake_ctx(token_hash="hash-b", session_id="session-x")
    assert helpers._override_key(a) != helpers._override_key(b)


def test_override_key_distinguishes_session_within_same_token(helpers, fake_ctx):
    a = fake_ctx(token_hash="hash-x", session_id="session-a")
    b = fake_ctx(token_hash="hash-x", session_id="session-b")
    assert helpers._override_key(a) != helpers._override_key(b)


def test_override_key_stable_for_same_caller(helpers, fake_ctx):
    a = fake_ctx(token_hash="hash-x", session_id="session-y")
    b = fake_ctx(token_hash="hash-x", session_id="session-y")
    assert helpers._override_key(a) == helpers._override_key(b)
