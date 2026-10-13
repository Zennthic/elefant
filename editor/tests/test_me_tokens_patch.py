"""Tests for the self-service token-edit endpoints.

Two endpoints, both owner-editable but with different audit semantics:
  PATCH /api/me/tokens/{id}                — full edit (user owns the
                                              token, free-form scope flips)
  PATCH /api/me/oauth-sessions/{prefix}    — full edit (narrow OR widen),
                                              but any change stamps
                                              scope_modified_at so an
                                              out-of-band edit can be told
                                              apart from a session that
                                              still matches its original
                                              consent grant.

The audit stamp is the invariant most likely to regress in a future
refactor — without it a widened OAuth session would silently masquerade
as one that still matches the consent the client was shown.  The tests
below pin both the widen-is-allowed behaviour and the audit stamp.
"""
import pytest
import secrets
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    """A TestClient that sets Origin: http://testserver on every request.

    The editor's CSRF middleware refuses any cookie-bearing state-
    changing request whose Origin (or Referer) doesn't match the
    request's own host.  TestClient defaults to host=testserver, so
    every POST/PATCH/DELETE in this file needs Origin=http://testserver
    or the middleware will 403 it.  Encoding that in the client fixture
    keeps individual tests focused on the actual behaviour."""
    import main
    return TestClient(main.app, headers={"Origin": "http://testserver"})


@pytest.fixture
def seeded_user(client):
    """Same shape as test_auth_check, but inline so this file is
    independent.  Returns dict + the session-cookie string for that
    user, ready to attach to test requests."""
    from config_db import get_db_session, UserAccount, Tenant
    from auth import make_session_token

    db = next(get_db_session())
    org_id = secrets.token_hex(8)
    user_id = secrets.token_hex(8)
    slug = f"acme-{org_id[:6]}"
    org = Tenant(id=org_id, name="Acme", slug=slug,
                 lifecycle_status="active", is_active=True)
    db.add(org); db.commit()
    user = UserAccount(
        id=user_id, org_id=org_id, username="bob",
        full_username=f"bob@{slug}.test", role="user",
        is_active=True, auth_method="local",
    )
    db.add(user); db.commit()
    cookie = make_session_token(user)
    user_dict = {"id": user_id, "org_id": org_id}
    db.close()

    yield user_dict, cookie

    db2 = next(get_db_session())
    try:
        u = db2.get(UserAccount, user_id); o = db2.get(Tenant, org_id)
        if u: db2.delete(u)
        if o: db2.delete(o)
        db2.commit()
    finally:
        db2.close()


def _create_token(client, cookie, **overrides):
    """Mint a fresh API token for the seeded user via the live POST
    endpoint, so the test exercises the production code path end-to-end."""
    body = {
        "name": "test-token-" + secrets.token_hex(4),
        "memory_read": True, "memory_write": False,
        "wiki_read": True, "wiki_write": True,
        **overrides,
    }
    r = client.post("/api/me/tokens",
                    json=body,
                    cookies={"elefant_session": cookie})
    assert r.status_code == 201, r.text
    return r.json()


# ── PATCH /api/me/tokens/{id} (API token, full edit) ─────────────────────

def test_patch_api_token_narrows_scope(client, seeded_user):
    _, cookie = seeded_user
    tok = _create_token(client, cookie, wiki_write=True)
    assert tok["wiki_write"] is True

    r = client.patch(
        f"/api/me/tokens/{tok['id']}",
        json={"wiki_write": False},
        cookies={"elefant_session": cookie},
    )
    assert r.status_code == 200, r.text
    assert r.json()["wiki_write"] is False


def test_patch_api_token_widens_scope_freely(client, seeded_user):
    """API tokens (vs OAuth sessions) are PATs — the user IS the user.
    Widening their own scope is fine."""
    _, cookie = seeded_user
    tok = _create_token(client, cookie, wiki_write=False, memory_write=False)

    r = client.patch(
        f"/api/me/tokens/{tok['id']}",
        json={"wiki_write": True, "memory_write": True},
        cookies={"elefant_session": cookie},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["wiki_write"] is True
    assert body["memory_write"] is True


def test_patch_api_token_ignores_non_scope_fields(client, seeded_user):
    """Only the four scope flags are editable here — name, source,
    rate limits, classifications stay admin-managed.  A patch that
    tries to set them shouldn't change them and shouldn't 4xx."""
    _, cookie = seeded_user
    tok = _create_token(client, cookie)
    original_name = tok["name"]

    r = client.patch(
        f"/api/me/tokens/{tok['id']}",
        json={"name": "hacked", "source": "evil",
              "rate_limit_rpm": 999999, "memory_write": True},
        cookies={"elefant_session": cookie},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == original_name           # untouched
    assert body["memory_write"] is True            # edited fields applied


def test_patch_another_users_token_returns_404(client, seeded_user):
    """An ID guess that lands on a token the caller doesn't own returns
    404 — never 200, never 403 with a leaky message."""
    _, cookie = seeded_user
    r = client.patch(
        "/api/me/tokens/not-my-token-id",
        json={"wiki_write": False},
        cookies={"elefant_session": cookie},
    )
    assert r.status_code == 404


def test_patch_no_session_returns_401(client):
    r = client.patch("/api/me/tokens/some-id", json={"wiki_write": False})
    assert r.status_code == 401


# ── PATCH /api/me/oauth-sessions/{prefix} (owner edit + audit) ───────────
# OAuth sessions need an actual issued OAuthAccessToken row to PATCH;
# minting one through the full code flow is heavy.  Seed one directly so
# the edit + audit contract can be exercised in isolation.

@pytest.fixture
def seeded_oauth_session(seeded_user):
    """Insert an OAuthAccessToken for the seeded user that grants wiki_
    read + memory_read but NOT wiki_write/memory_write.  Tests can then
    narrow further or widen — both allowed, both audited."""
    from config_db import get_db_session, OAuthAccessToken
    from datetime import datetime, timedelta

    user, cookie = seeded_user
    db = next(get_db_session())
    raw, token_hash = OAuthAccessToken.generate()
    row = OAuthAccessToken(
        token_hash=token_hash,
        client_id="test-client",
        client_name="Test Client",
        scope="mcp",
        tenant_id=user["org_id"],
        user_id=user["id"],
        memory_read=True, memory_write=False,
        wiki_read=True,   wiki_write=False,
        expires_at=datetime.utcnow() + timedelta(days=30),
    )
    db.add(row); db.commit()
    prefix = token_hash[:8]
    db.close()
    yield prefix, cookie

    db2 = next(get_db_session())
    try:
        r = db2.get(OAuthAccessToken, token_hash)
        if r: db2.delete(r); db2.commit()
    finally:
        db2.close()


def test_oauth_session_narrow_allowed_and_audited(client, seeded_oauth_session):
    """memory_read was True; flipping to False is a narrowing — allowed,
    and (like any scope change) it stamps scope_modified_at."""
    prefix, cookie = seeded_oauth_session
    r = client.patch(
        f"/api/me/oauth-sessions/{prefix}",
        json={"memory_read": False},
        cookies={"elefant_session": cookie},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["memory_read"] is False
    assert body["scope_modified_at"] is not None


def test_oauth_session_widen_allowed_and_audited(client, seeded_oauth_session):
    """wiki_write was False; the owner flips it to True post-issuance.

    THIS IS THE CONTRACT (owner edit + audit): widening is allowed —
    the consent page always offered all four scopes and the owner could
    otherwise just mint a full-scope PAT — but the change MUST stamp
    scope_modified_at so the session is flagged as diverging from the
    consent the client originally saw."""
    prefix, cookie = seeded_oauth_session
    r = client.patch(
        f"/api/me/oauth-sessions/{prefix}",
        json={"wiki_write": True},
        cookies={"elefant_session": cookie},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["wiki_write"] is True
    assert body["scope_modified_at"] is not None

    # The list view surfaces the same audit stamp for the UI marker.
    r2 = client.get("/api/me/oauth-sessions",
                    cookies={"elefant_session": cookie})
    sess = next(s for s in r2.json()["sessions"]
                if s["token_id"] == prefix)
    assert sess["wiki_write"] is True
    assert sess["scope_modified_at"] is not None


def test_oauth_session_noop_patch_leaves_no_audit_stamp(client, seeded_oauth_session):
    """A PATCH that re-asserts the current scope (no actual flip) is a
    no-op — it must NOT stamp scope_modified_at, so an unedited session
    never wrongly shows the "edited" marker."""
    prefix, cookie = seeded_oauth_session
    r = client.patch(
        f"/api/me/oauth-sessions/{prefix}",
        json={"memory_read": True, "wiki_write": False},  # both already so
        cookies={"elefant_session": cookie},
    )
    assert r.status_code == 200, r.text
    assert r.json()["scope_modified_at"] is None


def test_oauth_session_short_prefix_refused(client, seeded_oauth_session):
    """The M6 audit fix: token_id_prefix must be >=8 chars or someone
    could edit an arbitrary session via a 1-char prefix collision."""
    _, cookie = seeded_oauth_session
    r = client.patch(
        "/api/me/oauth-sessions/abc",
        json={"memory_read": False},
        cookies={"elefant_session": cookie},
    )
    assert r.status_code == 400


def test_oauth_session_mixed_narrow_and_widen_both_apply(client, seeded_oauth_session):
    """A PATCH that BOTH narrows one scope AND widens another now applies
    both in one shot (owner edit) and stamps the audit field once."""
    prefix, cookie = seeded_oauth_session
    r = client.patch(
        f"/api/me/oauth-sessions/{prefix}",
        json={"memory_read": False, "wiki_write": True},
        cookies={"elefant_session": cookie},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["memory_read"] is False   # narrow half applied
    assert body["wiki_write"] is True      # widen half applied
    assert body["scope_modified_at"] is not None

    # Confirm both landed durably.
    r2 = client.get("/api/me/oauth-sessions",
                    cookies={"elefant_session": cookie})
    sess = next(s for s in r2.json()["sessions"]
                if s["token_id"] == prefix)
    assert sess["memory_read"] is False
    assert sess["wiki_write"] is True
