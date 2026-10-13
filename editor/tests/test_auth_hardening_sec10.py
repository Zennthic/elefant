"""SEC-10a: auth hardening batch.

One test (or small group) per item:
  1. reset-password CLI writes user_accounts and logs old sessions out
  2. OIDC id_token: issuer and nonce are verified; no unverified fallback
  3. /oauth/register is throttled per address; access-token TTL configurable
  4. login spends a bcrypt round on an unknown username
  5. /setup: a racing second first-admin is refused
"""
import secrets
import time

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    import main
    return TestClient(main.app)


def _mk_org_user(auth_method="local", password_hash=None):
    from config_db import SessionLocal, Tenant, UserAccount
    oid = secrets.token_hex(8)
    slug = f"sec10-{oid[:6]}"
    s = SessionLocal()
    s.add(Tenant(id=oid, name=slug, slug=slug, display_name=slug, domain=f"{slug}.test",
                 lifecycle_status="active", is_active=True))
    s.commit()
    uid = secrets.token_hex(8)
    full = f"carol@{slug}.test"
    s.add(UserAccount(id=uid, org_id=oid, username="carol", full_username=full,
                      role="org_admin", is_active=True, auth_method=auth_method,
                      password_hash=password_hash))
    s.commit()
    s.close()
    return uid, full


# ── 1. reset-password ─────────────────────────────────────────────────────

def test_reset_password_sets_hash_and_bumps_session_epoch():
    import main  # noqa: F401  (initialises the database)
    import reset_password
    from auth import verify_password
    from config_db import SessionLocal, UserAccount

    uid, full = _mk_org_user(password_hash=None)
    s = SessionLocal()
    before = s.query(UserAccount).filter_by(id=uid).one().session_epoch or 0
    assert reset_password.reset_password(s, full, "n3w-passw0rd") is None
    s.close()

    s = SessionLocal()
    u = s.query(UserAccount).filter_by(id=uid).one()
    assert verify_password("n3w-passw0rd", u.password_hash)
    assert u.session_epoch == before + 1
    assert u.last_password_reset_at is not None
    s.close()


def test_reset_password_cli_refuses_unknown_idp_and_short(capsys):
    import main  # noqa: F401
    import reset_password

    _uid, full = _mk_org_user(auth_method="oidc")
    assert reset_password.main([full, "long-enough-pw"]) == 1
    assert "identity provider" in capsys.readouterr().err
    assert reset_password.main(["nobody@nowhere.test", "long-enough-pw"]) == 1
    assert "no user" in capsys.readouterr().err
    _uid2, full2 = _mk_org_user()
    assert reset_password.main([full2, "short"]) == 1
    assert reset_password.main([full2, "long-enough-pw"]) == 0


# ── 2. OIDC id_token validation ────────────────────────────────────────────

@pytest.fixture
def rsa_signer(monkeypatch):
    import jwt as pyjwt
    from routers import sso
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    class _Key:
        def __init__(self):
            self.key = key.public_key()

    class _FakeJWKClient:
        def __init__(self, uri):
            pass

        def get_signing_key_from_jwt(self, token):
            return _Key()

    monkeypatch.setattr(sso, "PyJWKClient", _FakeJWKClient)

    def sign(drop=(), **claims):
        base = {"iss": "https://idp.test", "aud": "client-1", "sub": "s1",
                "exp": int(time.time()) + 300, "nonce": "n-123"}
        base.update(claims)
        for name in drop:
            base.pop(name)
        return pyjwt.encode(base, key, algorithm="RS256")
    return sign


def _validate(token, issuer="https://idp.test", nonce="n-123"):
    from routers import sso
    return sso._oidc_validate_id_token(token, jwks_uri="https://idp.test/jwks",
                                       client_id="client-1", issuer=issuer,
                                       expected_nonce=nonce)


def test_oidc_id_token_checks_issuer_and_nonce(rsa_signer):
    assert _validate(rsa_signer())["sub"] == "s1"
    # A trailing slash on the issuer is not significant.
    assert _validate(rsa_signer(iss="https://idp.test/"))["sub"] == "s1"
    with pytest.raises(Exception, match="issuer"):
        _validate(rsa_signer(iss="https://evil.test"))
    with pytest.raises(Exception, match="nonce"):
        _validate(rsa_signer(nonce="other"))
    with pytest.raises(Exception, match="nonce"):
        _validate(rsa_signer(drop=("nonce",)))
    with pytest.raises(Exception, match="iss"):
        _validate(rsa_signer(drop=("iss",)))
    with pytest.raises(Exception):
        _validate(rsa_signer(aud="someone-else"))


def test_oidc_login_sends_nonce_and_has_no_unverified_fallback(monkeypatch):
    import inspect
    from routers import sso

    src = inspect.getsource(sso.oidc_callback)
    assert "urlsafe_b64decode" not in src
    assert not hasattr(sso, "JWT_AVAILABLE")

    class _P:
        client_id, scopes, use_userinfo_only = "c", "openid", False
    url = sso._oidc_build_auth_url({"authorization_endpoint": "https://idp/a"}, _P(),
                                   "st", "cc", "https://rp/cb", "the-nonce")
    assert "nonce=the-nonce" in url


# ── 3. OAuth registration throttle + token TTL ─────────────────────────────

def test_oauth_register_is_throttled_per_address(client, monkeypatch):
    import login_throttle
    from routers import oauth

    limiter = login_throttle.RateLimiter(limit=3, window=3600)
    monkeypatch.setattr(login_throttle, "oauth_register_limiter", limiter)
    body = {"client_name": "t", "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"]}
    for _ in range(3):
        assert client.post("/oauth/register", json=body).status_code == 201
    r = client.post("/oauth/register", json=body)
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) > 0
    # A different address is unaffected.
    assert limiter.hit("203.0.113.9") == 0


def test_rate_limiter_window_expires():
    import login_throttle
    now = [0.0]
    lim = login_throttle.RateLimiter(limit=2, window=60, clock=lambda: now[0])
    assert lim.hit("a") == 0 and lim.hit("a") == 0
    assert lim.hit("a") == 60
    now[0] = 61
    assert lim.hit("a") == 0


def test_oauth_access_token_ttl_is_configurable(monkeypatch):
    from routers import oauth
    monkeypatch.delenv("OAUTH_ACCESS_TOKEN_TTL_DAYS", raising=False)
    assert oauth._oauth_access_token_ttl() == 365 * 86400
    monkeypatch.setenv("OAUTH_ACCESS_TOKEN_TTL_DAYS", "30")
    assert oauth._oauth_access_token_ttl() == 30 * 86400
    for bad in ("0", "-1", "soon"):
        monkeypatch.setenv("OAUTH_ACCESS_TOKEN_TTL_DAYS", bad)
        assert oauth._oauth_access_token_ttl() == 365 * 86400


# ── 4. login timing ────────────────────────────────────────────────────────

def test_login_unknown_user_spends_a_bcrypt_round(client, monkeypatch):
    import bcrypt
    import login_throttle
    from routers import local_auth

    login_throttle.throttle.reset()
    calls = []
    real = bcrypt.checkpw
    monkeypatch.setattr(bcrypt, "checkpw", lambda p, h: calls.append(h) or real(p, h))

    r = client.post("/login", data={"username": "ghost@nowhere.test", "password": "pw-guess"},
                    follow_redirects=False)
    assert r.status_code == 401
    assert len(calls) == 1

    # An IdP-only account (no local hash) costs the same.
    _uid, full = _mk_org_user(auth_method="oidc")
    calls.clear()
    r = client.post("/login", data={"username": full, "password": "pw-guess"},
                    follow_redirects=False)
    assert r.status_code == 401
    assert len(calls) == 1
    login_throttle.throttle.reset()


# ── 5. first-run /setup race ──────────────────────────────────────────────

def test_first_run_claim_admits_one_setup(tmp_path, monkeypatch):
    """Two setups that both passed needs_first_run(): only one gets the slot."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import config_db
    from routers import local_auth

    eng = create_engine(f"sqlite:///{tmp_path}/race.db")
    config_db.Base.metadata.create_all(eng)
    Sess = sessionmaker(bind=eng)

    a, b = Sess(), Sess()
    assert local_auth.needs_first_run(a) and local_auth.needs_first_run(b)
    assert local_auth._claim_first_run(a) is True
    a.add(config_db.UserAccount(id="first", username="admin", full_username="admin",
                                role="platform_admin", auth_method="local"))
    a.commit()
    # b checked before a committed; its claim now collides and it backs off.
    assert local_auth._claim_first_run(b) is False
    assert b.query(config_db.UserAccount).count() == 1
    a.close(); b.close()


def test_first_run_claim_recovers_from_stale_claim(tmp_path):
    """A claim with no users behind it (accounts all removed) does not wedge /setup."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import config_db
    from routers import local_auth

    eng = create_engine(f"sqlite:///{tmp_path}/stale.db")
    config_db.Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    s.add(config_db.ConfigEntry(key=local_auth._FIRST_RUN_CLAIM_KEY, encrypted_value="x"))
    s.commit()
    assert local_auth._claim_first_run(s) is True
    s.close()


def test_setup_post_second_racer_creates_no_admin(client, monkeypatch):
    """Drive /setup twice as if both requests saw an empty install."""
    from routers import local_auth
    from config_db import SessionLocal, UserAccount, ConfigEntry

    monkeypatch.setattr(local_auth, "_bootstrap_env_set", lambda: False)
    s = SessionLocal()
    s.query(ConfigEntry).filter_by(key=local_auth._FIRST_RUN_CLAIM_KEY).delete()
    s.commit()
    s.close()
    # Both requests pass the outer "is this a fresh install?" check, as two
    # concurrent POSTs would; only the claim can stop the second. Checks made
    # under the claim see the real database.
    real = local_auth.needs_first_run
    outer = {"pending": True}

    def fake(session):
        if outer["pending"]:
            outer["pending"] = False
            return True
        return real(session) or s_count_only_racers(session)

    def s_count_only_racers(session):
        # The shared test DB already holds other tests' users; judge "fresh"
        # by the racers alone.
        return session.query(UserAccount).filter(
            UserAccount.username.in_(names)).count() == 0

    monkeypatch.setattr(local_auth, "needs_first_run", fake)
    names = [f"racer{secrets.token_hex(3)}" for _ in range(2)]
    try:
        for n in names:
            outer["pending"] = True
            r = client.post("/setup", data={"username": n, "password": "long-enough-pw"},
                            follow_redirects=False)
            assert r.status_code == 302
        s = SessionLocal()
        created = s.query(UserAccount).filter(UserAccount.username.in_(names)).all()
        assert [u.username for u in created] == [names[0]]
    finally:
        s = SessionLocal()
        s.query(UserAccount).filter(UserAccount.username.in_(names)).delete(
            synchronize_session=False)
        s.query(ConfigEntry).filter_by(key=local_auth._FIRST_RUN_CLAIM_KEY).delete()
        s.commit()
        s.close()
