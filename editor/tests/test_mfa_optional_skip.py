"""MFA setup can be skipped where the org leaves MFA optional (board card #425).

Onboarding a new org with MFA set to optional still forced its first admin
through "Set up two-factor authentication", because every admin role was held
to MFA whatever the org's policy. Now the policy decides for everyone but a
platform admin, an org or team admin is offered setup on their first sign-in
with a Skip option, and an enrolled user is always asked for a code.
"""
import secrets
from datetime import datetime

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    import main
    return TestClient(main.app)


PASSWORD = "correct-horse-battery-staple"


def _mk(policy: str, role: str, *, logged_in_before: bool = False) -> tuple[str, str]:
    """An active org with the given MFA policy and one local user in it."""
    from config_db import get_db_session, UserAccount, Tenant
    from auth import hash_password

    db = next(get_db_session())
    oid = secrets.token_hex(8)
    uid = secrets.token_hex(8)
    slug = f"skip-{oid[:6]}"
    db.add(Tenant(id=oid, name=slug, slug=slug, display_name=slug,
                  domain=f"{slug}.test", lifecycle_status="active", is_active=True,
                  mfa_policy=policy, setup_complete=True))
    db.commit()
    login = f"ann@{slug}.test"
    db.add(UserAccount(id=uid, org_id=oid, username="ann", full_username=login,
                       role=role, is_active=True, auth_method="local",
                       password_hash=hash_password(PASSWORD),
                       last_login_at=datetime.utcnow() if logged_in_before else None))
    db.commit()
    db.close()
    return uid, login


def _login(client, login):
    return client.post("/login", data={"username": login, "password": PASSWORD, "next": "/"},
                       follow_redirects=False)


def _enrol(uid):
    from config_db import get_db_session
    from auth import get_or_create_mfa_secret
    db = next(get_db_session())
    cred = get_or_create_mfa_secret(db, uid)
    cred.is_enabled = True
    db.commit()
    db.close()


# ── The policy rule ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("role,policy,required", [
    ("user", "optional", False),
    ("user", "enforced", True),
    ("team_admin", "optional", False),
    ("team_admin", "enforced", True),
    ("org_admin", "optional", False),
    ("org_admin", "enforced", True),
])
def test_org_policy_decides_whether_mfa_is_required(role, policy, required):
    from auth import mfa_is_required
    from config_db import UserAccount, Tenant
    assert mfa_is_required(UserAccount(role=role), Tenant(mfa_policy=policy)) is required


def test_platform_admin_and_orgless_admins_always_need_mfa():
    from auth import mfa_is_required
    from config_db import UserAccount, Tenant
    assert mfa_is_required(UserAccount(role="platform_admin"), Tenant(mfa_policy="optional"))
    assert mfa_is_required(UserAccount(role="org_admin"), None)
    assert not mfa_is_required(UserAccount(role="user"), None)


# ── Sign-in ────────────────────────────────────────────────────────────────

def test_admin_first_login_in_optional_org_is_offered_setup_with_skip(client):
    _uid, login = _mk("optional", "org_admin")
    r = _login(client, login)
    assert r.status_code == 302
    assert r.headers["location"].startswith("/setup-mfa")
    page = client.get(r.headers["location"], follow_redirects=False)
    assert page.status_code == 200
    assert 'action="/setup-mfa/skip"' in page.text
    assert "<!-- SKIP_HIDDEN -->" not in page.text
    assert 'action="/setup-mfa/skip" hidden' not in page.text


def test_skip_finishes_sign_in_and_is_not_offered_again(client):
    _uid, login = _mk("optional", "org_admin")
    _login(client, login)
    r = client.post("/setup-mfa/skip", data={"next": "/"}, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/"
    assert client.cookies.get("elefant_session")

    # The next sign-in goes straight through: the offer is for the first one.
    client.cookies.clear()
    r = _login(client, login)
    assert r.status_code == 302
    assert not r.headers["location"].startswith("/setup-mfa")
    assert not r.headers["location"].startswith("/mfa")


def test_enforced_org_shows_no_skip_and_refuses_one(client):
    _uid, login = _mk("enforced", "org_admin")
    r = _login(client, login)
    assert r.headers["location"].startswith("/setup-mfa")
    page = client.get(r.headers["location"], follow_redirects=False)
    assert 'action="/setup-mfa/skip" hidden' in page.text

    r = client.post("/setup-mfa/skip", data={"next": "/"}, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith("/setup-mfa")
    assert not client.cookies.get("elefant_session")


def test_skip_without_a_pending_sign_in_goes_to_login(client):
    r = client.post("/setup-mfa/skip", data={"next": "/"}, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


def test_plain_user_in_optional_org_is_not_prompted(client):
    _uid, login = _mk("optional", "user")
    r = _login(client, login)
    assert r.status_code == 302
    assert r.headers["location"] == "/"


def test_enrolled_user_is_asked_for_a_code_and_cannot_skip_it(client):
    uid, login = _mk("optional", "user", logged_in_before=True)
    _enrol(uid)
    r = _login(client, login)
    assert r.headers["location"].startswith("/mfa")
    r = client.post("/setup-mfa/skip", data={"next": "/"}, follow_redirects=False)
    assert r.headers["location"].startswith("/mfa")
    assert not client.cookies.get("elefant_session")
