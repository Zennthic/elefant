"""A stale-but-signed cookie must land on the login form, not in a loop.

Reported from stage: Safari reporting "too many redirects" on the app root
while the same deployment served the login page fine to a fresh browser. The
loop was two endpoints disagreeing about what "logged in" means — the nginx
auth gate re-checks the user row on every request, while `/login` looked only
at whether the cookie's signature was good:

    GET /          → auth gate says 401   → redirect to /login
    GET /login     → "you're logged in!"  → redirect to /
    GET /          → 401 …

The victim can't reach the login form to fix it, which makes this a lockout
rather than an inconvenience — and it fires on exactly the events that are
supposed to END a session safely: offboarding, "log out all sessions", a
password change, an org going inactive.
"""
import secrets

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    import main
    return TestClient(main.app, headers={"Origin": "http://testserver"})


@pytest.fixture
def session_user():
    """A live user, their org, and a freshly minted cookie for them."""
    from config_db import get_db_session, UserAccount, Tenant
    from auth import make_session_token
    db = next(get_db_session())
    oid, uid = secrets.token_hex(8), secrets.token_hex(8)
    slug = f"loop-{oid[:6]}"
    db.add(Tenant(id=oid, name=slug, slug=slug, display_name=slug,
                  lifecycle_status="active", is_active=True))
    db.commit()
    db.add(UserAccount(id=uid, org_id=oid, username="jon",
                       full_username=f"jon@{slug}.test", role="user",
                       is_active=True, auth_method="local"))
    db.commit()
    token = make_session_token(db.get(UserAccount, uid))
    db.close()
    return {"uid": uid, "org_id": oid, "cookie": {"elefant_session": token}}


def _stale(session_user, **changes):
    """Age the session the way the real events do, leaving the cookie itself
    perfectly well-formed."""
    from config_db import get_db_session, UserAccount, Tenant
    db = next(get_db_session())
    user = db.query(UserAccount).filter_by(id=session_user["uid"]).first()
    org = db.query(Tenant).filter_by(id=session_user["org_id"]).first()
    for field, value in changes.items():
        setattr(org if field == "lifecycle_status" else user, field, value)
    db.commit()
    db.close()


STALE_CASES = [
    ("the user was offboarded",       {"is_active": False}),
    ("they logged out everywhere",    {"session_epoch": 3}),
    ("their org went inactive",       {"lifecycle_status": "recycled"}),
]


# ── the loop itself ────────────────────────────────────────────────────────

@pytest.mark.parametrize("why,change", STALE_CASES, ids=[c[0] for c in STALE_CASES])
def test_a_stale_session_reaches_the_login_form(client, session_user, why, change):
    _stale(session_user, **change)
    # The gate still refuses it — that part was never in question.
    assert client.get("/api/auth/check", cookies=session_user["cookie"]).status_code == 401
    # And /login must now agree, instead of bouncing them back at the wall.
    r = client.get("/login?next=%2F", cookies=session_user["cookie"],
                   follow_redirects=False)
    assert r.status_code == 200, f"{why}: /login redirected to {r.headers.get('location')}"
    assert "password" in r.text.lower()


@pytest.mark.parametrize("why,change", STALE_CASES, ids=[c[0] for c in STALE_CASES])
def test_the_dead_cookie_is_cleared_on_the_way_past(client, session_user, why, change):
    """Leaving it set means every other tab keeps re-running the same dance."""
    _stale(session_user, **change)
    r = client.get("/login", cookies=session_user["cookie"], follow_redirects=False)
    assert "elefant_session=" in r.headers.get("set-cookie", ""), \
        f"{why}: the refused cookie was left in place"


def test_following_the_redirects_terminates(client, session_user):
    """The symptom as the browser sees it: ask for the root, end up somewhere."""
    _stale(session_user, is_active=False)
    r = client.get("/login?next=%2F", cookies=session_user["cookie"],
                   follow_redirects=True)
    assert r.status_code == 200


# ── the case that still has to work ────────────────────────────────────────

def test_a_live_session_still_skips_the_login_form(client, session_user):
    """The redirect exists for a reason — don't fix the loop by removing it."""
    r = client.get("/login?next=%2Fboard", cookies=session_user["cookie"],
                   follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/board"


def test_a_live_session_keeps_its_cookie(client, session_user):
    r = client.get("/login", cookies=session_user["cookie"], follow_redirects=False)
    assert "elefant_session=" not in r.headers.get("set-cookie", "")


def test_no_cookie_at_all_just_shows_the_form(client):
    r = client.get("/login", follow_redirects=False)
    assert r.status_code == 200
    assert "elefant_session=" not in r.headers.get("set-cookie", ""), \
        "clearing a cookie that was never sent invites a pointless Set-Cookie on every hit"


def test_a_platform_admin_outlives_their_org(client, session_user):
    """Platform admins aren't org-scoped — an inactive org must not lock the
    one person who can go and fix it out of the console."""
    from config_db import get_db_session, UserAccount
    db = next(get_db_session())
    db.query(UserAccount).filter_by(id=session_user["uid"]).first().role = "platform_admin"
    db.commit(); db.close()
    _stale(session_user, lifecycle_status="recycled")

    assert client.get("/api/auth/check", cookies=session_user["cookie"]).status_code == 200
    r = client.get("/login?next=%2F", cookies=session_user["cookie"],
                   follow_redirects=False)
    assert r.status_code == 302
