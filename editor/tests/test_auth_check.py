"""Tests for /api/auth/check — the nginx auth_request endpoint that
gates every protected upstream route.  A regression here re-opens
the 24-hour stale-claim privilege-escalation window the security
audit (C1) found.

The contract:
  - No session cookie → 401
  - Invalid/expired JWT → 401
  - Valid JWT but user no longer in DB → 401
  - Valid JWT but org is inactive AND user isn't platform_admin → 401
  - Valid JWT, user present, org active → 200 with X-Auth-* headers
    sourced from the LIVE DB row, not from the cookie's claims.
"""
import pytest
import uuid
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    """A TestClient over the real editor app.  No mocking — uses the
    on-disk SQLite the conftest seeded into a tmpdir."""
    import main
    return TestClient(main.app)


@pytest.fixture
def seed_user_and_org(client):
    """Insert a UserAccount + Tenant via the live ORM so the tests
    can build real session JWTs against them.  Returns (user, org)
    rows so tests can mutate them mid-flow (the C1 invariant)."""
    from config_db import get_db_session, UserAccount, Tenant
    import secrets

    db = next(get_db_session())
    org_id = secrets.token_hex(8)
    user_id = secrets.token_hex(8)
    slug = f"acme-{org_id[:6]}"
    try:
        org = Tenant(
            id=org_id, name=f"Acme {org_id[:6]}", slug=slug,
            display_name="Acme Test", domain=f"{slug}.test",
            lifecycle_status="active", is_active=True,
        )
        db.add(org)
        db.commit()

        user = UserAccount(
            id=user_id,
            org_id=org_id,
            username="alice",
            full_username=f"alice@{slug}.test",
            role="user",
            is_active=True,
            auth_method="local",
        )
        db.add(user)
        db.commit()
        # Detach so tests can rebuild a fresh session and mutate
        # independently — this matches the request-scoped session
        # pattern in the live app.
        user_dict = {"id": user.id, "org_id": user.org_id,
                     "role": user.role, "full_username": user.full_username,
                     "username": user.username}
        org_dict = {"id": org.id, "slug": org.slug}
        db.close()

        # Yield bare ids; tests fetch fresh rows per-step.
        yield user_dict, org_dict
    finally:
        # Best-effort cleanup with a fresh session.
        db2 = next(get_db_session())
        try:
            u = db2.get(UserAccount, user_id)
            o = db2.get(Tenant, org_id)
            if u: db2.delete(u)
            if o: db2.delete(o)
            db2.commit()
        except Exception:
            db2.rollback()
        finally:
            db2.close()


def _session_cookie_for(user):
    """Mint a session JWT shaped like a real login would produce.
    `user` is a dict or a UserAccount-shaped object; the helper just
    needs the attributes auth.make_session_token reads."""
    from auth import make_session_token
    if isinstance(user, dict):
        class _U: pass
        u = _U()
        for k, v in user.items():
            setattr(u, k, v)
        return make_session_token(u)
    return make_session_token(user)


# ── unauthenticated paths ────────────────────────────────────────────────

def test_no_cookie_returns_401(client):
    r = client.get("/api/auth/check")
    assert r.status_code == 401


def test_garbage_cookie_returns_401(client):
    r = client.get("/api/auth/check",
                   cookies={"elefant_session": "not-a-jwt"})
    assert r.status_code == 401


# ── happy path ──────────────────────────────────────────────────────────

def test_valid_session_returns_200_with_db_sourced_headers(client, seed_user_and_org):
    user, org = seed_user_and_org
    cookie = _session_cookie_for(user)
    r = client.get("/api/auth/check",
                   cookies={"elefant_session": cookie})
    assert r.status_code == 200
    # Headers come from the DB row, not from claims.
    assert r.headers["X-Auth-User-Id"]  == user["id"]
    assert r.headers["X-Auth-Org-Id"]   == org["id"]
    assert r.headers["X-Auth-Org-Slug"] == org["slug"]
    assert r.headers["X-Auth-Role"]     == "user"


# ── the C1 stale-claim regression-prevention case ──────────────────────

def test_role_downgrade_takes_effect_immediately(client, seed_user_and_org):
    """The audit's C1 fix.  A user demoted from platform_admin should
    NOT keep their elevated headers flowing for the 24h JWT lifetime.
    auth_check must re-read role from the DB on every request."""
    from config_db import get_db_session, UserAccount

    user, org = seed_user_and_org

    # Promote the user to platform_admin and mint a JWT that claims so.
    db = next(get_db_session())
    user_row = db.get(UserAccount, user["id"])
    user_row.role = "platform_admin"
    db.commit()
    cookie = _session_cookie_for(user_row)
    db.close()

    # Sanity check — admin headers present.
    r = client.get("/api/auth/check", cookies={"elefant_session": cookie})
    assert r.status_code == 200
    assert r.headers["X-Auth-Role"] == "platform_admin"

    # Demote them in the DB but keep the cookie (a real attacker
    # scenario: stolen cookie OR a user who got demoted but hasn't
    # logged out).
    db = next(get_db_session())
    user_row = db.get(UserAccount, user["id"])
    user_row.role = "user"
    db.commit()
    db.close()

    # Re-checking with the same cookie — the LIVE role must come back.
    r = client.get("/api/auth/check", cookies={"elefant_session": cookie})
    assert r.status_code == 200
    assert r.headers["X-Auth-Role"] == "user", (
        "auth_check is leaking stale role from JWT claims — "
        "C1 regression"
    )


def test_deleted_user_returns_401_even_with_valid_jwt(client, seed_user_and_org):
    """User row gone → 401.  Pre-C1-fix, the cookie still validated
    against SESSION_SECRET so they'd get 200 with a vanished uid."""
    from config_db import get_db_session, UserAccount
    user, org = seed_user_and_org
    cookie = _session_cookie_for(user)

    # Sanity: works while user exists.
    assert client.get("/api/auth/check",
                      cookies={"elefant_session": cookie}).status_code == 200

    # Now delete the user.
    db = next(get_db_session())
    user_row = db.get(UserAccount, user["id"])
    db.delete(user_row); db.commit(); db.close()

    r = client.get("/api/auth/check", cookies={"elefant_session": cookie})
    assert r.status_code == 401, (
        "auth_check accepted a JWT whose user row is gone — "
        "C1 regression"
    )


def test_inactive_org_blocks_non_platform_admin(client, seed_user_and_org):
    """An org in lifecycle_status='recycled' = users can't log in.
    Platform admins bypass this gate."""
    from config_db import get_db_session, Tenant
    user, org = seed_user_and_org
    cookie = _session_cookie_for(user)

    db = next(get_db_session())
    org_row = db.get(Tenant, org["id"])
    org_row.lifecycle_status = "recycled"
    db.commit(); db.close()

    r = client.get("/api/auth/check", cookies={"elefant_session": cookie})
    assert r.status_code == 401
