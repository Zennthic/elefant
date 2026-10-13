"""Regression tests for the OAuth login → dead-end fixes (issue #87):

- 2B: GET /login must substitute the hidden `next` field, not leave the literal
  "<!-- NEXT -->" placeholder (which _safe_next rejected → every login → /).
- 2A: the /login-redirect handler must percent-encode the original request URI
  into ?next=, so a multi-param OAuth authorize URL isn't truncated at the first
  `&` (nginx can't urlencode a variable, which is why it proxies here now).
"""
from urllib.parse import urlparse, parse_qs

from fastapi.testclient import TestClient


def test_login_html_substitutes_next_field():
    from routers import local_auth
    dest = "/editor/oauth/authorize?client_id=x&state=y"
    html = local_auth._login_html(next_url=dest).body.decode()
    assert "<!-- NEXT -->" not in html                        # placeholder consumed
    assert 'name="next"' in html
    assert "/editor/oauth/authorize?client_id=x&amp;state=y" in html  # & escaped by _render


def test_safe_next_still_blocks_open_redirect_in_form():
    from routers import local_auth
    html = local_auth._login_html(next_url="//evil.com/x").body.decode()
    assert "//evil.com" not in html                           # coerced to "/"


def test_login_redirect_percent_encodes_full_uri():
    import main
    client = TestClient(main.app)
    original = ("/editor/oauth/authorize?client_id=abc"
                "&redirect_uri=https://claude.ai/cb&state=xyz&code_challenge=zzz")
    r = client.get("/login-redirect", headers={"X-Original-URI": original},
                   follow_redirects=False)
    assert r.status_code == 302
    loc = r.headers["location"]
    assert loc.startswith("/login?next=")
    # The whole multi-param URL survives — nothing lost at the first '&'.
    nxt = parse_qs(urlparse(loc).query)["next"][0]
    assert nxt == original


def test_login_redirect_rejects_absolute_original():
    import main
    client = TestClient(main.app)
    r = client.get("/login-redirect", headers={"X-Original-URI": "https://evil.com/x"},
                   follow_redirects=False)
    assert r.status_code == 302
    nxt = parse_qs(urlparse(r.headers["location"]).query)["next"][0]
    assert nxt == "/"    # _safe_next coerces a non-relative original
