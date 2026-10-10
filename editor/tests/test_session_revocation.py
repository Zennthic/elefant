"""SEC-04: session-epoch revocation + deactivation gate at /api/auth/check.

Sessions are stateless 24h JWTs. Before this fix a demoted admin, a
deactivated user, or a password reset left existing cookies fully valid until
expiry. Now every session cookie carries a `sepoch` claim; /api/auth/check
(the nginx auth_request gate every protected route passes through) rejects any
cookie whose epoch no longer matches the user row, and rejects inactive users.
"""
import secrets
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    """Origin is set on every request: the CSRF middleware refuses a
    state-changing call that carries a session cookie but no matching Origin,
    which is exactly what the POSTs below are."""
    import main
    return TestClient(main.app, headers={"Origin": "http://testserver"})


@pytest.fixture
def user_id():
    from config_db import get_db_session, UserAccount, Tenant
    db = next(get_db_session())
    oid = secrets.token_hex(8)
    uid = secrets.token_hex(8)
    slug = f"rev-{oid[:6]}"
    db.add(Tenant(id=oid, name=slug, slug=slug, display_name=slug,
                  domain=f"{slug}.test", lifecycle_status="active", is_active=True))
    db.commit()
    db.add(UserAccount(id=uid, org_id=oid, username="carol",
                       full_username=f"carol@{slug}.test", role="user",
                       is_active=True, auth_method="local"))
    db.commit()
    db.close()
    return uid


def _cookie(uid):
    from config_db import get_db_session, UserAccount
    from auth import make_session_token
    db = next(get_db_session())
    tok = make_session_token(db.get(UserAccount, uid))
    db.close()
    return tok


def test_valid_session_ok(client, user_id):
    r = client.get("/api/auth/check", cookies={"elefant_session": _cookie(user_id)})
    assert r.status_code == 200


def test_deactivated_user_session_rejected(client, user_id):
    from config_db import get_db_session, UserAccount
    cookie = _cookie(user_id)
    assert client.get("/api/auth/check",
                      cookies={"elefant_session": cookie}).status_code == 200
    db = next(get_db_session())
    u = db.get(UserAccount, user_id); u.is_active = False; db.commit(); db.close()
    r = client.get("/api/auth/check", cookies={"elefant_session": cookie})
    assert r.status_code == 401, "deactivated user's live session must be rejected (SEC-04)"


def test_epoch_bump_revokes_old_cookie_but_fresh_works(client, user_id):
    from config_db import get_db_session, UserAccount
    from auth import bump_session_epoch, make_session_token
    old_cookie = _cookie(user_id)
    assert client.get("/api/auth/check",
                      cookies={"elefant_session": old_cookie}).status_code == 200
    db = next(get_db_session())
    u = db.get(UserAccount, user_id)
    bump_session_epoch(u); db.commit()
    fresh_cookie = make_session_token(u); db.close()
    assert client.get("/api/auth/check",
                      cookies={"elefant_session": old_cookie}).status_code == 401, \
        "old-epoch cookie must be rejected after a bump (SEC-04)"
    assert client.get("/api/auth/check",
                      cookies={"elefant_session": fresh_cookie}).status_code == 200, \
        "a freshly minted cookie carrying the new epoch must work"


def test_logout_is_scoped_to_this_session(client, user_id):
    """An ordinary logout signs out THIS device and leaves the others alone.

    Logging out on the office laptop must not sign you out on your phone —
    that's what users mean by the button, and killing every session is the
    separate, deliberate action below.
    """
    this_device = _cookie(user_id)
    other_device = _cookie(user_id)
    client.get("/logout", cookies={"elefant_session": this_device},
               follow_redirects=False)
    assert client.get("/api/auth/check",
                      cookies={"elefant_session": other_device}).status_code == 200, \
        "logging out on one device must not end the user's other sessions"


def test_logout_clears_the_cookie(client, user_id):
    """The cookie in front of it does get cleared — that's the whole job."""
    r = client.get("/logout", cookies={"elefant_session": _cookie(user_id)},
                   follow_redirects=False)
    assert r.status_code == 302
    assert 'elefant_session=""' in r.headers.get("set-cookie", "") \
        or "elefant_session=;" in r.headers.get("set-cookie", "")


def test_logout_all_ends_every_session(client, user_id):
    """The lost-laptop button: every session for the account, everywhere."""
    here = _cookie(user_id)
    elsewhere = _cookie(user_id)
    assert client.get("/api/auth/check",
                      cookies={"elefant_session": elsewhere}).status_code == 200
    r = client.post("/api/me/logout-all", cookies={"elefant_session": here})
    assert r.status_code == 200 and r.json()["ok"] is True
    for name, cookie in (("the calling session", here), ("another device", elsewhere)):
        assert client.get("/api/auth/check",
                          cookies={"elefant_session": cookie}).status_code == 401, \
            f"logout-all must end {name}"


def test_logout_all_requires_a_session(client):
    assert client.post("/api/me/logout-all").status_code == 401


def test_password_change_still_ends_other_sessions(client, user_id):
    """Changing a password is a lock-out event — it must keep bumping the epoch
    now that plain logout no longer does."""
    from config_db import get_db_session, UserAccount
    from auth import hash_password
    db = next(get_db_session())
    u = db.get(UserAccount, user_id)
    u.password_hash = hash_password("original-password")
    db.commit(); db.close()
    stale = _cookie(user_id)
    r = client.post("/api/me/password",
                    cookies={"elefant_session": _cookie(user_id)},
                    json={"current_password": "original-password",
                          "new_password": "a-brand-new-password"})
    assert r.status_code == 200, r.text
    assert client.get("/api/auth/check",
                      cookies={"elefant_session": stale}).status_code == 401
