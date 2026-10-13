"""SEC-01: the recycle bin must scope to the authenticated session org.

Previously `_tenant_id_from_request` trusted a client-supplied `X-Tenant-Slug`
header (which nginx never sets) and otherwise fell back to "the first active
tenant" — so a caller in org A could list/restore/destroy org B's soft-deleted
pages by naming B's slug, and absent the header every org commingled onto one
tenant. The fix derives the tenant from the auth_request-injected org id only.
"""
import secrets
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    import main
    return TestClient(main.app)


def _mk_org(db, Tenant, slug):
    oid = secrets.token_hex(8)
    db.add(Tenant(id=oid, name=slug, slug=slug, display_name=slug,
                  domain=f"{slug}.test", lifecycle_status="active", is_active=True))
    db.commit()
    return oid


def _mk_deleted_page(db, SoftDeletedPage, tenant_id, path):
    db.add(SoftDeletedPage(original_path=path, name=path,
                           content="secret-content", tenant_id=tenant_id))
    db.commit()


def test_recycle_bin_ignores_tenant_slug_header(client):
    from config_db import get_db_session, Tenant, SoftDeletedPage

    db = next(get_db_session())
    slug_a = f"orga-{secrets.token_hex(3)}"
    slug_b = f"orgb-{secrets.token_hex(3)}"
    org_a = _mk_org(db, Tenant, slug_a)
    org_b = _mk_org(db, Tenant, slug_b)
    _mk_deleted_page(db, SoftDeletedPage, org_a, "a-page.md")
    _mk_deleted_page(db, SoftDeletedPage, org_b, "b-page.md")
    db.close()

    # Authenticated as org A (nginx-injected headers), attacker adds a spoofed
    # X-Tenant-Slug naming org B. The listing must contain only A's page.
    r = client.get("/api/recycle-bin", headers={
        "X-Auth-User-Id": "u-" + secrets.token_hex(4),
        "X-Auth-Org-Id":  org_a,
        "X-Tenant-Slug":  slug_b,
    })
    assert r.status_code == 200
    paths = {item["original_path"] for item in r.json()["items"]}
    assert "a-page.md" in paths
    assert "b-page.md" not in paths, (
        "X-Tenant-Slug header pivoted the recycle bin to another org — "
        "SEC-01 regression"
    )
