"""Property-based tests for tenant-isolation invariants.

Hypothesis generates arbitrary (auth_header_slug, body_slug, has_internal)
inputs against the embedder's _resolve_tenant_slug and asserts the
invariants the audit + recent regressions taught us:

  1. ISOLATION INVARIANT: a non-internal-service caller can never get
     back a slug that isn't either their authenticated org or "default".
  2. INTERNAL TRUST INVARIANT: an internal-service caller's body slug
     is always honoured (never silently mapped to default).
  3. NEVER-SILENT-403 INVARIANT: if the body slug equals the auth slug
     OR equals "default" OR the caller is internal, the function never
     raises 403.

The matrix of hand-written tests covers ~12 cases; hypothesis explores
the much larger space and catches the variants we didn't think of
(unicode slugs, very long slugs, leading-whitespace tricks, etc.).
"""
import pytest
from fastapi import HTTPException

from hypothesis import given, strategies as st, assume, settings, HealthCheck


# Reuse the same stub-request from the unit tests.  Hypothesis lets us
# stuff anything in here including ugly Unicode.
class _StubRequest:
    def __init__(self, headers=None):
        self._h = {k.lower(): v for k, v in (headers or {}).items()}
    @property
    def headers(self):
        class _H:
            def __init__(self, store): self._s = store
            def get(self, k, default=None): return self._s.get(k.lower(), default)
        return _H(self._h)


# Slug strategies: realistic Elefant slugs are lowercase ascii + hyphens,
# but the FUNCTION needs to handle anything an attacker could send.
_slug = st.text(min_size=0, max_size=80)
_internal_token = "test-internal-service-token-cafef00d"


@pytest.fixture(scope="module")
def resolve():
    import main
    return main._resolve_tenant_slug


# ── Invariant 1: non-internal caller never gets a foreign slug back ─────

@given(auth_slug=_slug, body_slug=_slug)
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture],
          max_examples=200)
def test_isolation_non_internal_never_returns_foreign(resolve, auth_slug, body_slug):
    """For ANY combination of auth_slug + body_slug, a non-internal caller
    either:
      (a) gets back the auth_slug (when set), or
      (b) gets back "default" (when no auth), or
      (c) is rejected with 403.

    Crucially, NEVER returns body_slug when body_slug != auth_slug and
    body_slug != "default"."""
    # Skip the values that aren't meaningful as headers (Hypothesis
    # generates NULs and other control chars that real HTTP would
    # reject upstream).
    assume("\x00" not in auth_slug and "\x00" not in body_slug)

    headers = {}
    if auth_slug.strip():
        headers["X-Auth-Org-Slug"] = auth_slug
    req = _StubRequest(headers)

    try:
        result = resolve(req, body_slug)
    except HTTPException as exc:
        assert exc.status_code == 403
        return  # caller refused — invariant holds

    auth_normalised = auth_slug.strip()
    body_normalised = body_slug.strip()
    # The result MUST be one of these three — and importantly NOT a
    # foreign body_slug snuck through.
    valid_outputs = {auth_normalised, "default"}
    if not auth_normalised:
        valid_outputs = {"default"}
    assert result in valid_outputs, (
        f"isolation breach: auth={auth_slug!r} body={body_slug!r} "
        f"returned {result!r} (expected one of {valid_outputs})"
    )


# ── Invariant 2: internal-service caller is always trusted ──────────────

@given(body_slug=_slug)
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture],
          max_examples=100)
def test_internal_caller_body_slug_is_honoured(resolve, body_slug):
    """Compactor / editor-admin path: a request with a valid X-Internal-
    Service header always gets either body_slug back (when non-empty)
    or 'default' (when empty).  Never 403 for an internal caller."""
    assume("\x00" not in body_slug)

    req = _StubRequest({"X-Internal-Service": _internal_token})
    result = resolve(req, body_slug)
    expected = body_slug.strip() or "default"
    assert result == expected


# ── Invariant 3: matching slug never raises ─────────────────────────────

@given(slug=st.text(min_size=1, max_size=80).filter(lambda s: s.strip() and s.strip() != "default"))
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture],
          max_examples=100)
def test_matching_auth_and_body_never_raises(resolve, slug):
    """No matter what (non-default) slug is supplied, if header and
    body agree, the function returns that slug without raising."""
    assume("\x00" not in slug)
    req = _StubRequest({"X-Auth-Org-Slug": slug})
    result = resolve(req, slug)
    assert result == slug.strip()
