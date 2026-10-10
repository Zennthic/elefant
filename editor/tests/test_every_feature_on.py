"""Every feature works on a default install (card #319, decision C).

The open-source build used to gate features by tier: one user on the default
`free` tier, a 30-day MCP OAuth trial, and no git sources, OIDC or SAML without
a licence. All of that is gone. These tests run the editor in its default
deployment mode, `single-org`, and prove each formerly gated feature works.

They also hold the back-compat promise for older `.env` files: `ELEFANT_TIER`
still parses, `saas` meaning multi-org and anything else being ignored.
"""
import secrets
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import deployment_mode
from deployment_mode import MULTI_ORG, SINGLE_ORG, resolve_mode


# ── ELEFANT_MODE and the legacy ELEFANT_TIER ──────────────────────────────

def test_the_default_is_single_org_with_nothing_to_say():
    assert resolve_mode({}) == (SINGLE_ORG, [])


@pytest.mark.parametrize("mode", [SINGLE_ORG, MULTI_ORG])
def test_elefant_mode_is_read_as_given(mode):
    assert resolve_mode({"ELEFANT_MODE": mode}) == (mode, [])


def test_tier_saas_still_means_multi_org_with_one_deprecation_warning():
    mode, warnings = resolve_mode({"ELEFANT_TIER": "saas"})
    assert mode == MULTI_ORG
    assert len(warnings) == 1 and "deprecated" in warnings[0]
    assert "ELEFANT_MODE=multi-org" in warnings[0]


@pytest.mark.parametrize("tier", ["free", "premium", "PREMIUM"])
def test_any_other_tier_is_single_org_with_one_line_saying_tiers_are_gone(tier):
    mode, warnings = resolve_mode({"ELEFANT_TIER": tier})
    assert mode == SINGLE_ORG
    assert len(warnings) == 1 and "no tiers" in warnings[0]


def test_elefant_mode_wins_over_a_legacy_tier():
    assert resolve_mode({"ELEFANT_MODE": "single-org", "ELEFANT_TIER": "saas"})[0] == SINGLE_ORG


def test_an_unknown_mode_runs_single_org_and_says_so():
    mode, warnings = resolve_mode({"ELEFANT_MODE": "enterprise"})
    assert mode == SINGLE_ORG
    assert len(warnings) == 1 and "enterprise" in warnings[0]


def test_the_platform_admin_routes_follow_the_mode(monkeypatch):
    from fastapi import HTTPException
    from auth_helpers import _require_multi_org
    monkeypatch.setattr(deployment_mode, "ELEFANT_MODE", SINGLE_ORG)
    with pytest.raises(HTTPException) as e:
        _require_multi_org()
    assert e.value.status_code == 404
    monkeypatch.setattr(deployment_mode, "ELEFANT_MODE", MULTI_ORG)
    assert _require_multi_org() is None


@pytest.mark.parametrize("tier,multi", [("saas", True), ("premium", False), ("free", False)])
def test_a_legacy_tier_env_drives_the_editor_mode(monkeypatch, tier, multi):
    from auth_helpers import _is_multi_org_mode
    monkeypatch.setattr(deployment_mode, "ELEFANT_MODE",
                        resolve_mode({"ELEFANT_TIER": tier})[0])
    assert _is_multi_org_mode() is multi


# ── A default single-org install has every feature ────────────────────────

@pytest.fixture
def single_org(monkeypatch):
    monkeypatch.setattr(deployment_mode, "ELEFANT_MODE", SINGLE_ORG)


@pytest.fixture
def client(single_org):
    """Origin is set on every request: the CSRF middleware refuses a
    state-changing call that carries a session cookie but no matching Origin."""
    import main
    return TestClient(main.app, headers={"Origin": "http://testserver"})


@pytest.fixture
def org_admin(client):
    """A fresh org with one org admin, signed in on `client`. Returns (org_id, domain)."""
    from auth import make_session_token
    from config_db import get_db_session, OrgDomain, Tenant, UserAccount
    db = next(get_db_session())
    oid = secrets.token_hex(8)
    slug = f"on-{oid[:6]}"
    domain = f"{slug}.test"
    db.add(Tenant(id=oid, name=slug, slug=slug, display_name=slug, domain=domain,
                  domain_verified=True, lifecycle_status="active", is_active=True))
    db.commit()
    db.add(OrgDomain(org_id=oid, domain=domain, is_primary=True, verified=True,
                     verify_token=secrets.token_hex(8)))
    admin = UserAccount(id=secrets.token_hex(8), org_id=oid, username="ada",
                        full_username=f"ada@{domain}", role="org_admin",
                        is_active=True, auth_method="local")
    db.add(admin)
    db.commit()
    client.cookies.set("elefant_session", make_session_token(admin))
    db.close()
    return oid, domain


def test_an_admin_can_add_more_users_than_any_old_tier_allowed(client, org_admin):
    """Free allowed 1 user and Premium 20. The 21st local user still goes in."""
    _, domain = org_admin
    for n in range(21):
        r = client.post("/api/admin/users", json={
            "local_part": f"user{n}", "domain": domain, "creation_mode": "invite",
        })
        assert r.status_code == 201, (n, r.text)
    users = client.get("/api/admin/users").json()["users"]
    assert len(users) == 22


def test_mcp_oauth_works_after_the_old_30_day_trial(client, org_admin):
    """An install whose OAuth trial started 31 days ago keeps working: the
    leftover ConfigEntry from the old trial is simply never read."""
    from config_db import get_db_session, ConfigEntry
    db = next(get_db_session())
    entry = db.query(ConfigEntry).filter_by(key="oauth_trial_started_at").first()
    if entry is None:
        entry = ConfigEntry(key="oauth_trial_started_at")
        db.add(entry)
    entry.set_value({"started_at": (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()})
    db.commit()
    db.close()

    redirect = "https://claude.ai/api/mcp/auth_callback"
    r = client.post("/oauth/register", json={"client_name": "after-trial",
                                             "redirect_uris": [redirect]})
    assert r.status_code == 201, r.text
    client_id = r.json()["client_id"]

    r = client.get("/oauth/authorize", params={
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code",
        "code_challenge": "x" * 43, "code_challenge_method": "S256", "state": "s",
    })
    assert r.status_code == 200, r.text


def test_an_admin_can_add_a_git_source(client, org_admin):
    r = client.post("/api/admin/git-sources",
                    json={"repo_url": "https://github.com/example/notes", "branch": "main"})
    assert r.status_code == 201, r.text
    assert any(s["id"] == r.json()["id"]
               for s in client.get("/api/admin/git-sources").json()["sources"])


def test_an_admin_can_configure_oidc(client, org_admin):
    r = client.post("/api/admin/identity-providers/oidc", json={
        "display_name": "Example IdP", "issuer": "https://idp.example.com",
        "client_id": "elefant",
    })
    assert r.status_code == 201, r.text


def test_an_admin_can_configure_saml(client, org_admin):
    r = client.post("/api/admin/identity-providers/saml", json={
        "display_name": "Example SAML", "idp_entity_id": "https://idp.example.com/saml",
        "idp_sso_url": "https://idp.example.com/saml/sso",
        "idp_x509_cert": "MIIC-example", "sp_entity_id": "https://test.example.com/editor",
    })
    assert r.status_code == 201, r.text
    providers = client.get("/api/admin/identity-providers").json()
    assert len(providers["oidc"]) == 0 and len(providers["saml"]) == 1


def test_the_licence_endpoint_is_gone(client):
    assert client.get("/api/license").status_code == 404
