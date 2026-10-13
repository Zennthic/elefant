"""The MFA setup page shows its QR code as an image (board card #426).

_render HTML-escapes every value, and the QR SVG went through it unmarked, so
the page showed the raw <svg> markup as text instead of a scannable code.
"""
import secrets

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    import main
    return TestClient(main.app)


PASSWORD = "correct-horse-battery-staple"


def _mk() -> str:
    """An active org with optional MFA and one local org admin in it."""
    from config_db import get_db_session, UserAccount, Tenant
    from auth import hash_password

    db = next(get_db_session())
    oid = secrets.token_hex(8)
    slug = f"qr-{oid[:6]}"
    db.add(Tenant(id=oid, name=slug, slug=slug, display_name=slug,
                  domain=f"{slug}.test", lifecycle_status="active", is_active=True,
                  mfa_policy="optional", setup_complete=True))
    db.commit()
    login = f"ann@{slug}.test"
    db.add(UserAccount(id=secrets.token_hex(8), org_id=oid, username="ann",
                       full_username=login, role="org_admin", is_active=True,
                       auth_method="local", password_hash=hash_password(PASSWORD)))
    db.commit()
    db.close()
    return login


def test_setup_mfa_page_renders_qr_as_svg_markup(client):
    login = _mk()
    r = client.post("/login", data={"username": login, "password": PASSWORD, "next": "/"},
                    follow_redirects=False)
    assert r.headers["location"].startswith("/setup-mfa")
    page = client.get(r.headers["location"], follow_redirects=False)
    assert page.status_code == 200

    qr = page.text.split('<div class="qr">', 1)[1].split("</div>", 1)[0]
    assert qr.lstrip().startswith("<svg")
    assert "viewBox=" in qr
    assert "&lt;" not in qr
    assert "<?xml" not in qr
    assert "<!-- QR_SVG -->" not in page.text
