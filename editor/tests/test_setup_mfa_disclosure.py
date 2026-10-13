"""SEC-02: /setup-mfa must not re-expose an enrolled user's TOTP secret.

The mid-login "pending" cookie is granted on the password alone. Before the fix,
GET /setup-mfa echoed the live decrypted secret for an already-enrolled user, so
anyone holding the password could read it and derive valid codes — collapsing
MFA (mandatory for admins) to a single factor. An enrolled user mid-login must
be sent to /mfa to enter a code, never shown the secret again.
"""
import secrets
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    import main
    return TestClient(main.app)


def _mk_user(db, UserAccount, Tenant):
    oid = secrets.token_hex(8)
    uid = secrets.token_hex(8)
    slug = f"mfa-{oid[:6]}"
    db.add(Tenant(id=oid, name=slug, slug=slug, display_name=slug,
                  domain=f"{slug}.test", lifecycle_status="active", is_active=True))
    db.commit()
    db.add(UserAccount(id=uid, org_id=oid, username="bob",
                       full_username=f"bob@{slug}.test", role="user",
                       is_active=True, auth_method="local"))
    db.commit()
    return uid


def test_enrolled_user_with_pending_cookie_is_redirected_not_shown_secret(client):
    from config_db import get_db_session, UserAccount, Tenant, crypto
    from auth import make_mfa_pending_token, get_or_create_mfa_secret

    db = next(get_db_session())
    uid = _mk_user(db, UserAccount, Tenant)
    cred = get_or_create_mfa_secret(db, uid)
    cred.is_enabled = True
    db.commit()
    secret = crypto.decrypt(cred.totp_secret_enc or "")
    db.close()

    token = make_mfa_pending_token(uid)
    r = client.get("/setup-mfa", params={"next": "/"},
                   cookies={"elefant_mfa_pending": token},
                   follow_redirects=False)
    assert r.status_code == 302
    assert "/mfa" in r.headers["location"]
    # The secret must never appear in the (empty) redirect body.
    assert secret and secret not in r.text


def test_unenrolled_user_can_still_enrol(client):
    """First-time enrolment (no enabled credential yet) still shows the page."""
    from config_db import get_db_session, UserAccount, Tenant
    from auth import make_mfa_pending_token

    db = next(get_db_session())
    uid = _mk_user(db, UserAccount, Tenant)
    db.close()

    token = make_mfa_pending_token(uid)
    r = client.get("/setup-mfa",
                   cookies={"elefant_mfa_pending": token},
                   follow_redirects=False)
    assert r.status_code == 200
