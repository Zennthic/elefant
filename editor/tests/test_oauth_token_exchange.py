"""Functional regression test for POST /oauth/token — the endpoint the audit on
issue #87 flagged as having ZERO coverage.

The exchange 500'd for everyone re-consenting since June 12: it claimed the
auth code with `UPDATE oauth_codes … RETURNING id` and then
`filter_by(id=claimed)`, but OAuthCode's primary key is `code` and there is no
`id` column. Postgres rejects `RETURNING id`, so every token exchange failed —
surfaced by claude.ai as the generic "Authorization with Elefant failed".

This drives the real exchange end-to-end and asserts it mints a token, and that
a replay of the (now-used) code is rejected.
"""
import base64
import hashlib
from datetime import datetime, timedelta

from fastapi.testclient import TestClient


def _pkce_pair():
    verifier = "test-verifier-" + "a1b2c3" * 10          # ≥43 chars
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def _seed(code_val, client_id, redirect_uri, challenge):
    from config_db import SessionLocal, OAuthClient, OAuthCode, UserAccount, Tenant
    s = SessionLocal()
    try:
        if not s.query(Tenant).filter_by(id="t-oauth-test").first():
            s.add(Tenant(id="t-oauth-test", name="OAuth Test Org", slug="oauth-test",
                         is_active=True, lifecycle_status="active", domain_verified=False,
                         max_teams=10, setup_complete=True))
        if not s.query(UserAccount).filter_by(id="u-oauth-test").first():
            s.add(UserAccount(id="u-oauth-test", org_id="t-oauth-test", username="oauthuser",
                              full_username="oauthuser", is_active=True, can_oauth=True,
                              auth_method="local", role="user"))
        if not s.query(OAuthClient).filter_by(client_id=client_id).first():
            s.add(OAuthClient(client_id=client_id, client_name="Test Client",
                              redirect_uris=[redirect_uri]))
        s.query(OAuthCode).filter_by(code=code_val).delete()
        s.add(OAuthCode(code=code_val, client_id=client_id, redirect_uri=redirect_uri,
                        code_challenge=challenge, code_challenge_method="S256",
                        user_id="u-oauth-test",
                        expires_at=datetime.utcnow() + timedelta(minutes=5), used=False,
                        memory_read=True, memory_write=False, wiki_read=True, wiki_write=True))
        s.commit()
    finally:
        s.close()


def test_oauth_token_exchange_mints_token_then_rejects_replay():
    import main

    verifier, challenge = _pkce_pair()
    redirect_uri = "https://claude.ai/api/mcp/auth_callback"
    client_id = "test-client-oauth-exchange"
    code_val = "testcode-" + "z9y8x7" * 8

    _seed(code_val, client_id, redirect_uri, challenge)
    client = TestClient(main.app)

    form = {
        "grant_type": "authorization_code",
        "code": code_val,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": verifier,
    }
    resp = client.post("/oauth/token", data=form)
    assert resp.status_code == 200, f"exchange failed {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body.get("access_token"), body
    assert body.get("token_type", "").lower() == "bearer"

    # The code is single-use: a second exchange (now used=TRUE) must be rejected,
    # not 500 and not a second token.
    resp2 = client.post("/oauth/token", data=form)
    assert resp2.status_code == 400, f"replay not rejected: {resp2.status_code} {resp2.text}"
    assert resp2.json().get("error") == "invalid_grant"
