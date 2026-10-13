"""Unit tests for the OAuth pure helpers introduced/touched by PR #68:

  * _validate_redirect_uri   — scheme allowlist (the C2 audit fix)
  * _pkce_verify             — constant-time PKCE S256 (the L2 fix)
  * _safe_oauth_redirect     — urlencoded redirect builder (the L5 fix)
  * _safe_next               — login open-redirect fix (the H1 fix)

These were all part of the security audit follow-up; without coverage,
a future refactor could trivially regress any of them.
"""
import pytest


# ── _validate_redirect_uri ───────────────────────────────────────────────

@pytest.fixture
def validate_redirect():
    from routers import oauth
    return oauth._validate_redirect_uri


@pytest.mark.parametrize("uri", [
    "https://claude.ai/oauth/callback",
    "https://example.com/cb",
    "https://sub.example.com/path/cb",
])
def test_https_accepted(validate_redirect, uri):
    assert validate_redirect(uri) is True


@pytest.mark.parametrize("uri", [
    "http://localhost/cb",
    "http://localhost:1234/cb",
    "http://127.0.0.1/cb",
    "http://127.0.0.1:8888/cb",
    "http://app.localhost/cb",
])
def test_http_loopback_accepted(validate_redirect, uri):
    """RFC 8252 — http allowed on loopback for native-app installs."""
    assert validate_redirect(uri) is True


def test_http_non_loopback_rejected(validate_redirect):
    """A non-localhost http URL is a TLS-stripping vector — refuse."""
    assert validate_redirect("http://evil.example.com/cb") is False


@pytest.mark.parametrize("uri", [
    "javascript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "vbscript:msgbox(1)",
    "file:///etc/passwd",
    "blob:https://x/aabb",
    "about:blank",
])
def test_dangerous_schemes_rejected(validate_redirect, uri):
    """C2 audit fix — these schemes would execute in the user's browser
    if accepted as a redirect target."""
    assert validate_redirect(uri) is False


def test_custom_native_app_scheme_accepted(validate_redirect):
    """RFC 8252 — native-app callbacks use custom schemes like
    com.example.app://oauth or claude://cb.  Accepted as long as not
    in the dangerous list and not empty."""
    assert validate_redirect("com.example.app://oauth/cb") is True
    assert validate_redirect("claude://callback") is True


def test_fragment_rejected(validate_redirect):
    """RFC 6749 §3.1.2 — redirect_uri MUST NOT include a fragment."""
    assert validate_redirect("https://example.com/cb#foo") is False


@pytest.mark.parametrize("bad", [None, "", "not-a-url", 123, []])
def test_garbage_rejected(validate_redirect, bad):
    assert validate_redirect(bad) is False


# ── _pkce_verify ──────────────────────────────────────────────────────────

@pytest.fixture
def pkce():
    from routers import oauth
    return oauth._pkce_verify


def test_pkce_matching_verifier_passes(pkce):
    """The RFC 7636 example test vector."""
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    challenge = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    assert pkce(verifier, challenge) is True


def test_pkce_wrong_verifier_fails(pkce):
    assert pkce("wrong-verifier", "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM") is False


@pytest.mark.parametrize("verifier,challenge", [
    ("", "anything"),
    ("verifier", ""),
    ("", ""),
])
def test_pkce_empty_inputs_fail(pkce, verifier, challenge):
    """Defensive — never accept an empty input.  An attacker who could
    post an empty challenge at consent would otherwise match an empty
    verifier at token exchange."""
    assert pkce(verifier, challenge) is False


# ── _safe_next (login open-redirect fix) ─────────────────────────────────

@pytest.fixture
def safe_next():
    from routers import local_auth
    return local_auth._safe_next


@pytest.mark.parametrize("n", ["/", "/wiki", "/oauth/authorize?x=1"])
def test_relative_path_accepted(safe_next, n):
    assert safe_next(n) == n


@pytest.mark.parametrize("n", [
    "//evil.com/path",                # protocol-relative → resolves to evil.com
    "//evil.com",
    "/\\evil.com",
    "/%2fevil.com",
    "/%2Fevil.com",
    "https://evil.com",
    "http://evil.com",
    "javascript:alert(1)",
    "ftp://evil.com",
    "",
    None,
])
def test_open_redirect_attempts_rejected(safe_next, n):
    """H1 audit fix — any of these would have post-login-redirected
    the user to an attacker domain."""
    assert safe_next(n) == "/"


# ── _safe_oauth_redirect (urlencode fix) ─────────────────────────────────

@pytest.fixture
def safe_oauth():
    from routers import oauth
    return oauth._safe_oauth_redirect


def test_safe_oauth_redirect_basic(safe_oauth):
    url = safe_oauth("https://client.example/cb", code="abc123", state="xyz")
    # Both params present, urlencoded.
    assert "code=abc123" in url
    assert "state=xyz" in url


def test_safe_oauth_redirect_preserves_existing_query(safe_oauth):
    """If the registered URI has its own query, we must APPEND rather
    than replace it."""
    url = safe_oauth("https://client.example/cb?app=x", code="abc")
    assert "app=x" in url
    assert "code=abc" in url


def test_safe_oauth_redirect_dangerous_state_encoded(safe_oauth):
    """A weird state value that contained & or # would have injected
    extra params or fragments without urlencoding.  Confirm the
    builder defends against it."""
    url = safe_oauth("https://client.example/cb", code="abc",
                     state="x&y=z#frag")
    # Important assertions: the literal "&y=z" must NOT appear as
    # its own param, and "#frag" must NOT appear as a literal fragment.
    # urlencode emits '%26y%3Dz%23frag' or similar.
    assert "&y=z" not in url.split("?", 1)[1]
    # Find the state= value and confirm it's percent-encoded
    state_segment = [p for p in url.split("?", 1)[1].split("&")
                     if p.startswith("state=")]
    assert state_segment, "state param missing"
    assert "%" in state_segment[0]  # encoded


def test_safe_oauth_redirect_drops_none_values(safe_oauth):
    """None values shouldn't appear as 'key=None' in the URL."""
    url = safe_oauth("https://client.example/cb", code="abc", state=None)
    assert "state=" not in url
    assert "code=abc" in url
