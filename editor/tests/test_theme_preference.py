"""The per-user UI theme preference (dark · light · auto).

The SPA applies the theme before first paint from localStorage, but the value
is stored on the account so it follows the user to another browser or device.
That makes two things worth pinning: the account default is dark (the Zennthic
theme), and the column only ever holds one of the three known values — a bad
value would reach `data-theme` and silently break the page's colours.
"""
import secrets

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    """Origin header set for every request: the CSRF middleware refuses a
    cookie-bearing state-changing request whose Origin doesn't match its host."""
    import main
    return TestClient(main.app, headers={"Origin": "http://testserver"})


@pytest.fixture
def user_cookie():
    from config_db import get_db_session, UserAccount, Tenant
    from auth import make_session_token

    db = next(get_db_session())
    org_id, user_id = secrets.token_hex(8), secrets.token_hex(8)
    slug = f"theme-{org_id[:6]}"
    db.add(Tenant(id=org_id, name=slug, slug=slug, display_name=slug,
                  domain=f"{slug}.test", lifecycle_status="active", is_active=True))
    db.commit()
    user = UserAccount(id=user_id, org_id=org_id, username="dana",
                       full_username=f"dana@{slug}.test", role="user",
                       is_active=True, auth_method="local")
    db.add(user)
    db.commit()
    cookie = make_session_token(user)
    db.close()
    return cookie


def test_defaults_to_dark(client, user_cookie):
    r = client.get("/api/me", cookies={"elefant_session": user_cookie})
    assert r.status_code == 200
    assert r.json()["theme"] == "dark"


@pytest.mark.parametrize("theme", ["light", "auto", "dark"])
def test_accepts_each_known_theme(client, user_cookie, theme):
    r = client.patch("/api/me", json={"theme": theme},
                     cookies={"elefant_session": user_cookie})
    assert r.status_code == 200
    assert r.json()["theme"] == theme
    # Round-trips, so it follows the user to another browser.
    again = client.get("/api/me", cookies={"elefant_session": user_cookie})
    assert again.json()["theme"] == theme


def test_rejects_unknown_theme(client, user_cookie):
    r = client.patch("/api/me", json={"theme": "solarized"},
                     cookies={"elefant_session": user_cookie})
    assert r.status_code == 400


def test_theme_is_untouched_by_unrelated_patch(client, user_cookie):
    client.patch("/api/me", json={"theme": "light"},
                 cookies={"elefant_session": user_cookie})
    client.patch("/api/me", json={"display_name": "Dana"},
                 cookies={"elefant_session": user_cookie})
    r = client.get("/api/me", cookies={"elefant_session": user_cookie})
    assert r.json()["theme"] == "light"


def test_whoami_reports_theme(client, user_cookie):
    """The SPA reads it from whoami so it can adopt the account preference
    without a second round trip on load."""
    client.patch("/api/me", json={"theme": "auto"},
                 cookies={"elefant_session": user_cookie})
    r = client.get("/api/admin/whoami", cookies={"elefant_session": user_cookie})
    assert r.status_code == 200
    assert r.json()["theme"] == "auto"


# ── The palette: the second, independent axis ──────────────────────────────
# Stored beside the theme for the same reason — so the choice follows the user
# to another browser — and validated against static/palettes.json, the file the
# CSS is generated from, so the API can never accept a palette the CSS lacks.

def test_palette_defaults_to_indigo(client, user_cookie):
    r = client.get("/api/me", cookies={"elefant_session": user_cookie})
    assert r.json()["palette"] == "indigo"


def test_accepts_every_palette_the_css_defines(client, user_cookie):
    from appearance import PALETTE_IDS
    assert len(PALETTE_IDS) >= 2
    for pid in sorted(PALETTE_IDS):
        r = client.patch("/api/me", json={"palette": pid},
                         cookies={"elefant_session": user_cookie})
        assert r.status_code == 200, (pid, r.text)
        assert r.json()["palette"] == pid


def test_rejects_a_palette_the_css_does_not_define(client, user_cookie):
    """The value reaches a data-palette attribute. One with no block in
    appearance.css would silently fall back, so it is refused at the door."""
    for bad in ("solarized", "INDIGO<script>", ""):
        r = client.patch("/api/me", json={"palette": bad},
                         cookies={"elefant_session": user_cookie})
        assert r.status_code == 400, bad


def test_palette_and_theme_are_independent(client, user_cookie):
    client.patch("/api/me", json={"theme": "light"}, cookies={"elefant_session": user_cookie})
    client.patch("/api/me", json={"palette": "forest"}, cookies={"elefant_session": user_cookie})
    r = client.get("/api/me", cookies={"elefant_session": user_cookie}).json()
    assert (r["theme"], r["palette"]) == ("light", "forest")


def test_whoami_reports_palette(client, user_cookie):
    client.patch("/api/me", json={"palette": "ocean"}, cookies={"elefant_session": user_cookie})
    r = client.get("/api/admin/whoami", cookies={"elefant_session": user_cookie})
    assert r.json()["palette"] == "ocean"
