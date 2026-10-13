"""Every scope an API token carries is the owner's to see and to set.

An automated dispatcher was refused `board_write` by the MCP server while
its token showed every box ticked in My Settings → API tokens: the
board scopes existed on ApiToken and were enforced, but neither the
self-service endpoints nor the page offered them. Only the admin console
could set board_write.

So the invariant pinned here is not "board scopes work"; it is that the set
of scope flags ApiToken carries, the set the owner may set through
/api/me/tokens, and the set the settings page shows are the same set. A
scope added to one and not the others fails a test rather than a dispatch.

Admin-managed fields (rate limits, label and classification scope, source,
owner) stay off the self-service path in both directions: create and edit.
"""
import re
import secrets
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PROFILE = Path(__file__).resolve().parent.parent / "static" / "profile.html"

# The page abbreviates some scope names in its element ids.
_UI_ID = {
    "memory_read": "mem-read", "memory_write": "mem-write",
    "wiki_read": "wiki-read", "wiki_write": "wiki-write",
    "graph_read": "graph-read",
    "board_read": "board-read", "board_write": "board-write",
}


# ── fixtures (inline, as in test_me_tokens_patch.py) ──────────────────────

@pytest.fixture
def client():
    import main
    # The CSRF middleware refuses cookie-bearing writes without a same-host
    # Origin.
    return TestClient(main.app, headers={"Origin": "http://testserver"})


@pytest.fixture
def seeded_user(client):
    from config_db import get_db_session, UserAccount, Tenant
    from auth import make_session_token

    db = next(get_db_session())
    org_id = secrets.token_hex(8)
    user_id = secrets.token_hex(8)
    slug = f"acme-{org_id[:6]}"
    db.add(Tenant(id=org_id, name="Acme", slug=slug,
                  lifecycle_status="active", is_active=True))
    db.commit()
    user = UserAccount(id=user_id, org_id=org_id, username="bob",
                       full_username=f"bob@{slug}.test", role="user",
                       is_active=True, auth_method="local")
    db.add(user); db.commit()
    cookie = make_session_token(user)
    db.close()

    yield {"id": user_id, "org_id": org_id}, cookie

    from config_db import ApiToken, OAuthAccessToken
    db2 = next(get_db_session())
    try:
        db2.query(ApiToken).filter_by(user_id=user_id).delete()
        db2.query(OAuthAccessToken).filter_by(user_id=user_id).delete()
        u = db2.get(UserAccount, user_id); o = db2.get(Tenant, org_id)
        if u: db2.delete(u)
        if o: db2.delete(o)
        db2.commit()
    finally:
        db2.close()


def _create(client, cookie, **body):
    body.setdefault("name", "agent-" + secrets.token_hex(4))
    r = client.post("/api/me/tokens", json=body,
                    cookies={"elefant_session": cookie})
    assert r.status_code == 201, r.text
    return r.json()


def _patch(client, cookie, token_id, **body):
    r = client.patch(f"/api/me/tokens/{token_id}", json=body,
                     cookies={"elefant_session": cookie})
    assert r.status_code == 200, r.text
    return r.json()


def _enforced(token_id):
    """What the MCP server is told this token may do: the same dict the
    internal validate endpoint returns for it."""
    import main
    from config_db import get_db_session, ApiToken
    db = next(get_db_session())
    try:
        tok = db.get(ApiToken, token_id)
        info, _ = main._resolve_bearer_token(db, tok.token_hash)
        return info
    finally:
        db.close()


def _row(token_id):
    from config_db import get_db_session, ApiToken
    db = next(get_db_session())
    try:
        return db.get(ApiToken, token_id).to_dict()
    finally:
        db.close()


# ── the three sets agree ───────────────────────────────────────────────────

def _apitoken_scope_flags():
    from sqlalchemy import Boolean
    from config_db import ApiToken
    return {c.name for c in ApiToken.__table__.columns
            if isinstance(c.type, Boolean)
            and re.fullmatch(r"[a-z]+_(read|write)", c.name)}


def test_every_apitoken_scope_is_self_service_settable():
    from routers import tokens
    flags = _apitoken_scope_flags()
    # Guard the introspection itself: if it found nothing, the equality
    # below would be vacuous.
    assert {"board_read", "board_write", "graph_read"} <= flags
    assert set(tokens.SELF_SERVICE_TOKEN_SCOPES) == flags


def _js_function(src, name):
    m = re.search(r"(?:async )?function " + name + r"\(\)\s*\{(.*?)\n\}\n", src, re.S)
    assert m, f"{name} not found in profile.html"
    return m.group(1)


@pytest.mark.parametrize("scope", sorted(_UI_ID))
def test_settings_page_offers_every_scope(scope):
    from routers import tokens
    assert scope in tokens.SELF_SERVICE_TOKEN_SCOPES
    src = PROFILE.read_text()
    ui = _UI_ID[scope]
    # A checkbox on the create form and one in the Edit scopes dialog...
    assert f'id="perm-{ui}"' in src
    assert f'id="m-{ui}"' in src
    # ...both actually sent to the server...
    assert re.search(rf"\b{scope}:\s*\$\('#perm-{ui}'\)\.checked",
                     _js_function(src, "createToken"))
    assert re.search(rf"\b{scope}\s*[:=]\s*\$\('#m-{ui}'\)\.checked",
                     _js_function(src, "saveScopeModal"))
    # ...and a badge on the token row.
    badges = re.search(r"const SCOPE_BADGES = \[(.*?)\];", src, re.S).group(1)
    assert f"'{scope}'" in badges


def test_ui_fixture_covers_every_scope():
    """The id map above is the test's own list; keep it honest."""
    from routers import tokens
    assert set(_UI_ID) == set(tokens.SELF_SERVICE_TOKEN_SCOPES)


def test_create_form_board_defaults_match_the_column():
    src = PROFILE.read_text()
    assert re.search(r'id="perm-board-read" checked', src)
    assert not re.search(r'id="perm-board-write" checked', src)


# ── create ─────────────────────────────────────────────────────────────────

def test_create_defaults_board_read_on_write_off(client, seeded_user):
    _, cookie = seeded_user
    tok = _create(client, cookie)
    assert tok["board_read"] is True
    assert tok["board_write"] is False
    assert _enforced(tok["id"])["board_write"] is False


def test_create_with_board_write_reaches_the_mcp_server(client, seeded_user):
    """The dispatcher case: a token minted with board write in My Settings is
    one the MCP server lets move cards."""
    _, cookie = seeded_user
    tok = _create(client, cookie, board_write=True)
    assert tok["board_write"] is True
    info = _enforced(tok["id"])
    assert info["valid"] is True
    assert info["board_write"] is True
    assert info["board_read"] is True


def test_create_can_turn_board_read_off(client, seeded_user):
    _, cookie = seeded_user
    tok = _create(client, cookie, board_read=False)
    assert tok["board_read"] is False
    assert _enforced(tok["id"])["board_read"] is False


def test_create_ignores_admin_managed_fields(client, seeded_user):
    user, cookie = seeded_user
    tok = _create(client, cookie,
                  rate_limit_rpm=999999, rate_limit_rpd=999999,
                  allowed_labels=["secret"], classification_access=["PHI"],
                  source="claude_ai", user_id="someone-else",
                  tenant_id="another-org")
    row = _row(tok["id"])
    assert row["rate_limit_rpm"] == 60
    assert row["rate_limit_rpd"] == 1000
    assert row["allowed_labels"] == []
    assert row["classification_access"] == []
    assert row["source"] == "user_self_service"
    assert row["user_id"] == user["id"]
    assert row["tenant_id"] == user["org_id"]


# ── edit ───────────────────────────────────────────────────────────────────

def test_patch_widens_board_write_and_the_mcp_server_sees_it(client, seeded_user):
    """The fix for an existing token: tick Board write in Edit scopes."""
    _, cookie = seeded_user
    tok = _create(client, cookie)
    assert _enforced(tok["id"])["board_write"] is False

    body = _patch(client, cookie, tok["id"], board_write=True)
    assert body["board_write"] is True
    assert _enforced(tok["id"])["board_write"] is True


def test_patch_narrows_board_scopes(client, seeded_user):
    _, cookie = seeded_user
    tok = _create(client, cookie, board_write=True)
    body = _patch(client, cookie, tok["id"], board_read=False, board_write=False)
    assert body["board_read"] is False
    assert body["board_write"] is False
    info = _enforced(tok["id"])
    assert info["board_read"] is False and info["board_write"] is False


def test_patch_leaves_unmentioned_scopes_alone(client, seeded_user):
    _, cookie = seeded_user
    tok = _create(client, cookie, board_write=True, graph_read=True)
    body = _patch(client, cookie, tok["id"], memory_write=True)
    assert body["board_write"] is True
    assert body["graph_read"] is True


def test_patch_ignores_admin_managed_fields(client, seeded_user):
    user, cookie = seeded_user
    tok = _create(client, cookie)
    _patch(client, cookie, tok["id"],
           rate_limit_rpm=999999, rate_limit_rpd=999999,
           allowed_labels=["secret"], classification_access=["PHI"],
           source="claude_ai", user_id="someone-else", tenant_id="another-org",
           name="renamed", board_write=True)
    row = _row(tok["id"])
    assert row["board_write"] is True          # the scope edit applied
    assert row["rate_limit_rpm"] == 60
    assert row["rate_limit_rpd"] == 1000
    assert row["allowed_labels"] == []
    assert row["classification_access"] == []
    assert row["source"] == "user_self_service"
    assert row["user_id"] == user["id"]
    assert row["tenant_id"] == user["org_id"]
    assert row["name"] == tok["name"]


# ── OAuth sessions show the board access they enforce ──────────────────────

@pytest.fixture
def oauth_session(seeded_user):
    from datetime import datetime, timedelta
    from config_db import get_db_session, OAuthAccessToken

    user, cookie = seeded_user
    db = next(get_db_session())
    _raw, token_hash = OAuthAccessToken.generate()
    db.add(OAuthAccessToken(
        token_hash=token_hash, client_id="c", client_name="Claude",
        scope="mcp", tenant_id=user["org_id"], user_id=user["id"],
        memory_read=True, memory_write=False,
        wiki_read=True, wiki_write=False,
        expires_at=datetime.utcnow() + timedelta(days=30),
    ))
    db.commit(); db.close()
    return token_hash, cookie


def _session_row(client, cookie, prefix):
    r = client.get("/api/me/oauth-sessions", cookies={"elefant_session": cookie})
    return next(s for s in r.json()["sessions"] if s["token_id"] == prefix)


def test_oauth_session_reports_the_board_access_it_enforces(client, oauth_session):
    import main
    from config_db import get_db_session
    token_hash, cookie = oauth_session
    s = _session_row(client, cookie, token_hash[:8])

    db = next(get_db_session())
    try:
        info, _ = main._resolve_bearer_token(db, token_hash)
    finally:
        db.close()
    assert s["board_read"] is info["board_read"] is True
    assert s["board_write"] is info["board_write"] is False
    # An OAuth session has no graph scope, so it reports none (and the page
    # draws no G:R badge for it).
    assert "graph_read" not in s


def test_oauth_board_write_follows_wiki_write(client, oauth_session):
    token_hash, cookie = oauth_session
    prefix = token_hash[:8]
    r = client.patch(f"/api/me/oauth-sessions/{prefix}",
                     json={"wiki_write": True},
                     cookies={"elefant_session": cookie})
    assert r.status_code == 200, r.text
    assert r.json()["board_write"] is True
    assert _session_row(client, cookie, prefix)["board_write"] is True


def test_oauth_board_scopes_are_not_set_directly(client, oauth_session):
    """Board access on a session is derived, so asking for it directly
    changes nothing, and so is not an edit that stamps the audit field."""
    token_hash, cookie = oauth_session
    prefix = token_hash[:8]
    r = client.patch(f"/api/me/oauth-sessions/{prefix}",
                     json={"board_write": True},
                     cookies={"elefant_session": cookie})
    assert r.status_code == 200, r.text
    assert r.json()["board_write"] is False
    assert r.json()["scope_modified_at"] is None
