"""The org admin console, end to end over HTTP (CQ-04 phase 5).

Phase 5 moved users, teams, groups and the org's domains out of main.py into
routers/org_admin.py and routers/admin_platform.py. The route table tests
say those routes are still served; this says they still work: an org admin
creates a team, a group in it and a user on the team, and the admin
console's lists show all three, the way the console loads them.
"""
import secrets

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    """Origin is set on every request: the CSRF middleware refuses a
    state-changing call that carries a session cookie but no matching Origin."""
    import main
    return TestClient(main.app, headers={"Origin": "http://testserver"})


@pytest.fixture
def org_admin():
    """A fresh org with a verified primary domain and one org admin in it.
    Returns (org_id, domain, session cookie)."""
    from auth import make_session_token
    from config_db import get_db_session, OrgDomain, Tenant, UserAccount
    db = next(get_db_session())
    oid = secrets.token_hex(8)
    slug = f"oa-{oid[:6]}"
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
    cookie = make_session_token(admin)
    db.close()
    return oid, domain, cookie


def test_org_admin_console_loads_users_teams_and_groups(client, org_admin):
    oid, domain, cookie = org_admin
    client.cookies.set("elefant_session", cookie)

    r = client.post("/api/admin/teams", json={"name": "Field Ops"})
    assert r.status_code == 201, r.text
    team = r.json()
    assert team["slug"] == "field-ops"

    r = client.post("/api/admin/groups", json={"name": "Night Shift", "team_ids": [team["id"]]})
    assert r.status_code == 201, r.text
    group = r.json()

    r = client.post("/api/admin/users", json={
        "local_part": "bob", "domain": domain, "creation_mode": "set_password",
        "password": secrets.token_urlsafe(12), "team_id": team["id"],
    })
    assert r.status_code == 201, r.text
    bob = r.json()
    assert bob["full_username"] == f"bob@{domain}"
    assert bob["role"] == "user"

    r = client.put(f"/api/admin/users/{bob['id']}/groups", json={"group_ids": [group["id"]]})
    assert r.status_code == 200, r.text

    users = client.get("/api/admin/users")
    assert users.status_code == 200, users.text
    assert {u["full_username"] for u in users.json()["users"]} == {f"ada@{domain}", f"bob@{domain}"}

    teams = client.get("/api/admin/teams")
    assert teams.status_code == 200, teams.text
    assert [t["id"] for t in teams.json()["teams"]] == [team["id"]]

    groups = client.get("/api/admin/groups")
    assert groups.status_code == 200, groups.text
    assert [g["id"] for g in groups.json()["groups"]] == [group["id"]]

    members = client.get(f"/api/admin/groups/{group['id']}/members")
    assert members.status_code == 200, members.text
    assert [m["id"] for m in members.json()["members"]] == [bob["id"]]


def test_org_admin_sees_its_domains_and_org(client, org_admin):
    oid, domain, cookie = org_admin
    client.cookies.set("elefant_session", cookie)

    r = client.get("/api/admin/org/domains")
    assert r.status_code == 200, r.text
    [row] = r.json()["domains"]
    assert row["domain"] == domain and row["is_primary"]
    assert row["verify_host"] == f"elefant-verify.{domain}"

    r = client.get("/api/org")
    assert r.status_code == 200, r.text
    assert r.json()["id"] == oid


def test_org_admin_routes_refuse_an_ordinary_user(client, org_admin):
    from auth import make_session_token
    from config_db import get_db_session, UserAccount
    oid, domain, _ = org_admin
    db = next(get_db_session())
    user = UserAccount(id=secrets.token_hex(8), org_id=oid, username="eve",
                       full_username=f"eve@{domain}", role="user",
                       is_active=True, auth_method="local")
    db.add(user)
    db.commit()
    client.cookies.set("elefant_session", make_session_token(user))
    db.close()
    for path in ("/api/admin/users", "/api/admin/teams", "/api/admin/groups", "/api/admin/org/domains"):
        assert client.get(path).status_code == 403, path


def test_team_admin_console_is_scoped_to_its_own_teams(client, org_admin):
    """A team admin sees and manages only the teams it administers.

    org_admin.py once defined _team_admin_team_ids twice. The second (claims,
    session) definition shadowed the first (user_id, session) one, so the
    user list, user create and team list called .get on a string and answered
    500 to every team admin (card #294)."""
    from auth import make_session_token
    from config_db import get_db_session, TeamMembership, UserAccount
    oid, domain, cookie = org_admin
    client.cookies.set("elefant_session", cookie)
    mine = client.post("/api/admin/teams", json={"name": "Day Shift"}).json()
    theirs = client.post("/api/admin/teams", json={"name": "Back Office"}).json()

    db = next(get_db_session())
    carl = UserAccount(id=secrets.token_hex(8), org_id=oid, username="carl",
                       full_username=f"carl@{domain}", role="team_admin",
                       is_active=True, auth_method="local")
    db.add(carl)
    db.add(TeamMembership(user_id=carl.id, team_id=mine["id"], role="team_admin"))
    db.commit()
    client.cookies.set("elefant_session", make_session_token(carl))
    db.close()

    teams = client.get("/api/admin/teams")
    assert teams.status_code == 200, teams.text
    assert [t["id"] for t in teams.json()["teams"]] == [mine["id"]]

    r = client.post("/api/admin/users", json={
        "local_part": "dora", "domain": domain, "creation_mode": "set_password",
        "password": secrets.token_urlsafe(12), "team_id": mine["id"],
    })
    assert r.status_code == 201, r.text
    r = client.post("/api/admin/users", json={
        "local_part": "erin", "domain": domain, "creation_mode": "set_password",
        "password": secrets.token_urlsafe(12), "team_id": theirs["id"],
    })
    assert r.status_code == 403, r.text

    users = client.get("/api/admin/users")
    assert users.status_code == 200, users.text
    assert {u["full_username"] for u in users.json()["users"]} == {f"carl@{domain}", f"dora@{domain}"}

    r = client.post("/api/admin/groups", json={"name": "Mine", "team_ids": [mine["id"]]})
    assert r.status_code == 201, r.text
    r = client.post("/api/admin/groups", json={"name": "Theirs", "team_ids": [theirs["id"]]})
    assert r.status_code == 403, r.text
    groups = client.get("/api/admin/groups")
    assert groups.status_code == 200, groups.text
    assert [g["name"] for g in groups.json()["groups"]] == ["Mine"]


def test_legacy_tenant_crud_is_still_gone(client):
    assert client.get("/api/admin/tenants").status_code == 410
    assert client.post("/api/admin/tenants/x").status_code == 410
