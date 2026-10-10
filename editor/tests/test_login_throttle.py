"""SEC-08: password and TOTP guessing on /login and /mfa is throttled.

Guessing past the limit is refused with a 429, before the password or code is
checked, and allowed again once the lock's time is up. The lock is time-boxed
and scoped so that somebody guessing at an admin's account from their own
address cannot keep the admin, on a different address, out of it.
"""
import secrets

import pyotp
import pytest
from fastapi.testclient import TestClient

import login_throttle


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    t = login_throttle.LoginThrottle(window=900, lockout=900, limit_ip=30,
                                     limit_account_ip=5, limit_account=50, clock=c)
    monkeypatch.setattr(login_throttle, "throttle", t)
    return c


@pytest.fixture
def client(monkeypatch):
    import main
    from routers import local_auth
    # TestClient's peer is always "testclient"; let a header stand in for the
    # client address so a test can speak from more than one.
    monkeypatch.setattr(local_auth, "_request_ip",
                        lambda request: request.headers.get("x-test-ip", "198.51.100.1"))
    return TestClient(main.app, headers={"Origin": "http://testserver"})


PASSWORD = "correct horse battery"


@pytest.fixture
def user():
    from config_db import get_db_session, UserAccount, Tenant
    from auth import hash_password
    db = next(get_db_session())
    oid, uid = secrets.token_hex(8), secrets.token_hex(8)
    slug = f"thr-{oid[:6]}"
    db.add(Tenant(id=oid, name=slug, slug=slug, display_name=slug,
                  lifecycle_status="active", is_active=True))
    db.commit()
    login = f"ann@{slug}.test"
    db.add(UserAccount(id=uid, org_id=oid, username="ann", full_username=login,
                       role="user", is_active=True, auth_method="local",
                       password_hash=hash_password(PASSWORD)))
    db.commit()
    db.close()
    return {"uid": uid, "login": login}


def _login(client, login, password, ip="198.51.100.1"):
    return client.post("/login", data={"username": login, "password": password, "next": "/"},
                       headers={"x-test-ip": ip}, follow_redirects=False)


# ── /login ───────────────────────────────────────────────────────────────────

def test_guessing_past_the_limit_is_refused_then_allowed_after_the_window(client, clock, user):
    for _ in range(5):
        assert _login(client, user["login"], "wrong").status_code == 401
    # Locked: even the right password is refused, so the lock is no oracle.
    r = _login(client, user["login"], PASSWORD)
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) > 0
    assert "Too many attempts" in r.text

    clock.t += 901
    r = _login(client, user["login"], PASSWORD)
    assert r.status_code == 302, "the lock is time-boxed"


def test_the_real_user_on_another_address_is_not_locked_out(client, clock, user):
    for _ in range(5):
        _login(client, user["login"], "wrong", ip="203.0.113.9")
    assert _login(client, user["login"], PASSWORD, ip="203.0.113.9").status_code == 429
    assert _login(client, user["login"], PASSWORD, ip="198.51.100.7").status_code == 302


def test_refused_attempts_do_not_extend_the_lock(client, clock, user):
    for _ in range(5):
        _login(client, user["login"], "wrong")
    for _ in range(20):
        clock.t += 40
        assert _login(client, user["login"], "wrong").status_code == 429
    clock.t += 901 - 800
    assert _login(client, user["login"], PASSWORD).status_code == 302


def test_a_distributed_guess_locks_the_account_for_the_window_only(client, clock, user):
    for i in range(50):
        _login(client, user["login"], "wrong", ip=f"192.0.2.{i % 10 + 1}")
    assert _login(client, user["login"], PASSWORD, ip="198.51.100.7").status_code == 429
    clock.t += 901
    assert _login(client, user["login"], PASSWORD, ip="198.51.100.7").status_code == 302


def test_one_address_spraying_many_accounts_is_locked(client, clock):
    for i in range(30):
        _login(client, f"nobody{i}@example.test", "wrong", ip="192.0.2.200")
    r = _login(client, "someone-else@example.test", "wrong", ip="192.0.2.200")
    assert r.status_code == 429


def test_an_unknown_name_locks_exactly_like_a_real_one(client, clock, user):
    for _ in range(5):
        _login(client, "ghost@example.test", "wrong")
    assert _login(client, "ghost@example.test", "wrong").status_code == 429


def test_a_success_clears_that_address_budget(client, clock, user):
    for _ in range(4):
        _login(client, user["login"], "wrong")
    assert _login(client, user["login"], PASSWORD).status_code == 302
    for _ in range(4):
        assert _login(client, user["login"], "wrong").status_code == 401


# ── /mfa ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def mfa_user(user):
    from config_db import get_db_session, crypto
    from auth import get_or_create_mfa_secret, make_mfa_pending_token
    db = next(get_db_session())
    cred = get_or_create_mfa_secret(db, user["uid"])
    cred.is_enabled = True
    db.commit()
    secret = crypto.decrypt(cred.totp_secret_enc or "")
    db.close()
    return {**user, "secret": secret, "pending": make_mfa_pending_token(user["uid"])}


def _mfa(client, u, code, ip="198.51.100.1"):
    return client.post("/mfa", data={"code": code, "next": "/"},
                       cookies={"elefant_mfa_pending": u["pending"]},
                       headers={"x-test-ip": ip}, follow_redirects=False)


def _wrong_code(secret):
    totp = pyotp.TOTP(secret)
    return next(c for c in ("000000", "111111", "222222", "333333")
                if not totp.verify(c, valid_window=1))


def test_totp_guessing_is_refused_then_allowed_after_the_window(client, clock, mfa_user):
    bad = _wrong_code(mfa_user["secret"])
    for _ in range(5):
        assert _mfa(client, mfa_user, bad).status_code == 401
    r = _mfa(client, mfa_user, pyotp.TOTP(mfa_user["secret"]).now())
    assert r.status_code == 429
    assert "Too many attempts" in r.text
    clock.t += 901
    assert _mfa(client, mfa_user, pyotp.TOTP(mfa_user["secret"]).now()).status_code == 302


# ── client address ──────────────────────────────────────────────────────────

def test_x_real_ip_is_believed_only_from_a_private_peer():
    assert login_throttle.client_ip("10.42.0.7", {"x-real-ip": "203.0.113.5"}) == "203.0.113.5"
    assert login_throttle.client_ip("127.0.0.1", {"x-real-ip": "203.0.113.5"}) == "203.0.113.5"
    # A client reaching the editor directly cannot pick its own key.
    assert login_throttle.client_ip("198.51.100.3", {"x-real-ip": "203.0.113.5"}) == "198.51.100.3"
    assert login_throttle.client_ip("10.42.0.7", {}) == "10.42.0.7"


def test_the_nginx_config_throttles_login_and_mfa_too():
    from pathlib import Path
    conf = (Path(__file__).resolve().parents[2] / "nginx" / "nginx.conf").read_text()
    assert "zone=login" in conf
    assert "location ~ ^/(login|mfa)" in conf
