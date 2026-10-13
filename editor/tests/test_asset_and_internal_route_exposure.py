"""Three ways a signed-in user could reach past their own permissions, closed.

1. nginx proxies /editor/ to the editor's root, so /editor/api/internal/* used
   to reach the peer-only routes: a browser could read any tenant's settings,
   write them, or forge a memory-edit notification. nginx now refuses the
   prefix, and the editor refuses any /api/internal/* request that carries a
   signed-in identity, so a different proxy cannot reopen it.
2. /api/image served whatever was stored, with a guessed content type. A page
   written at evil.html through /api/file then rendered as HTML from our own
   origin. Only images render inline now; SVG renders without scripts; the
   rest downloads.
3. /api/export/page, /api/export/kb, /api/image and /api/attachment skipped
   the wiki-space ACL that /api/file applies.
"""
import io
import re
import secrets
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from routers import wiki as wiki_router
from routers import backup as backup_router

NGINX_CONF = Path(__file__).resolve().parents[2] / "nginx" / "nginx.conf"


# ── 1. peer-only routes ──────────────────────────────────────────────────────

def test_nginx_refuses_the_internal_prefix_before_the_editor_sees_it():
    text = NGINX_CONF.read_text()
    for prefix in ("/editor/api/internal/", "/api/internal/"):
        m = re.search(r"location\s+\^~\s+" + re.escape(prefix) + r"\s*\{([^}]*)\}", text)
        assert m, f"no `location ^~ {prefix}` block in nginx.conf"
        assert "return 404" in m.group(1), f"{prefix} is not refused"
    # It must sit before /editor/ only for readability; ^~ wins regardless.
    assert text.index("location ^~ /editor/api/internal/") < text.index("location /editor/ {")


@pytest.fixture
def client():
    import main
    return TestClient(main.app, headers={"Origin": "http://testserver"})


@pytest.mark.parametrize("header", ["X-Auth-User-Id", "X-Auth-User"])
@pytest.mark.parametrize("path", [
    "/api/internal/tenant-settings?tenant_slug=default",
    "/api/internal/wiki-spaces?user_id=u&org_id=o",
    "/api/internal/config",
])
def test_the_editor_refuses_internal_paths_that_carry_a_browser_identity(client, header, path):
    r = client.get(path, headers={header: "someone"})
    assert r.status_code == 404, f"{path} answered a browser: {r.status_code}"


def test_a_peer_without_a_browser_identity_is_not_affected(client):
    r = client.get("/api/internal/tenant-settings?tenant_slug=default")
    assert r.status_code == 200


# ── 2. assets never run in our origin ────────────────────────────────────────

@pytest.mark.parametrize("name,ctype,inline", [
    ("shot.png", "image/png", True),
    ("photo.jpg", "image/jpeg", True),
    ("PHOTO.JPEG", "image/jpeg", True),
    ("anim.gif", "image/gif", True),
    ("pic.webp", "image/webp", True),
    ("still.avif", "image/avif", True),
    ("fav.ico", "image/vnd.microsoft.icon", True),
])
def test_images_render_inline_with_nosniff(name, ctype, inline):
    r = wiki_router._asset_response(name, b"x")
    assert r.media_type == ctype
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "content-disposition" not in r.headers


def test_the_image_table_is_ours_not_the_platforms(monkeypatch):
    """The editor image is python:3.11-slim: no /etc/mime.types, and Python's
    own table lacks .webp before 3.13. mimetypes.guess_type("pic.webp") is
    None there, and the first CI run of this file served a webp as a
    download. The type must come from our table, whatever the host knows."""
    import inspect
    import mimetypes
    for fn in (wiki_router.asset_mime, wiki_router._asset_response):
        assert "mimetypes" not in inspect.getsource(fn), fn.__name__
    monkeypatch.setattr(mimetypes, "guess_type", lambda *a, **k: (None, None))
    assert wiki_router.asset_mime("pic.webp") == "image/webp"
    assert wiki_router.asset_mime("d.svg") == "image/svg+xml"
    assert wiki_router.asset_mime("page.html") == "application/octet-stream"
    r = wiki_router._asset_response("pic.webp", b"x")
    assert r.media_type == "image/webp" and "content-disposition" not in r.headers


def test_svg_renders_without_scripts():
    r = wiki_router._asset_response("diagram.svg", b"<svg/>")
    assert r.media_type == "image/svg+xml"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in r.headers["content-security-policy"]
    assert "default-src 'none'" in r.headers["content-security-policy"]


@pytest.mark.parametrize("name", ["evil.html", "evil.htm", "page.md", "notes.txt",
                                  "script.js", "style.css", "doc.pdf", "unknown.xyz", "x.xhtml"])
def test_everything_else_is_a_download(name):
    r = wiki_router._asset_response("cards/c1/" + name, b"<script>alert(1)</script>")
    assert r.media_type == "application/octet-stream"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["content-disposition"].startswith("attachment; filename=")


def test_the_download_name_cannot_break_the_header():
    r = wiki_router._asset_response('a/b"; x=y\r\nInjected: 1.html', b"")
    assert "\r" not in r.headers["content-disposition"]
    assert '"' not in r.headers["content-disposition"].split("filename=")[1].strip('"')


def test_both_image_routes_use_the_safe_response():
    import inspect
    for fn in (wiki_router.get_image, wiki_router.public_share_image):
        src = inspect.getsource(fn)
        assert "_asset_response(" in src, f"{fn.__name__} serves assets with a guessed type"
        assert "mimetypes.guess_type" not in src


def test_the_peer_image_route_uses_the_same_table():
    """get_wiki_page puts this route's content type into a data: URI."""
    import inspect

    import main
    src = inspect.getsource(main.internal_wiki_image)
    assert "asset_mime(" in src and "mimetypes" not in src


# ── 3. the wiki-space ACL ────────────────────────────────────────────────────

@pytest.fixture
def org_with_a_private_space(monkeypatch):
    """An org, a plain user who may not read the `private` space, an org
    admin who may, and a fake bucket with pages in both spaces and at root."""
    from config_db import get_db_session, Tenant, UserAccount, WikiSpaceACL
    db = next(get_db_session())
    oid = secrets.token_hex(8)
    slug = f"sp-{oid[:6]}"
    db.add(Tenant(id=oid, name=slug, slug=slug, display_name=slug, domain=f"{slug}.test",
                  domain_verified=True, lifecycle_status="active", is_active=True))
    user = UserAccount(id=secrets.token_hex(8), org_id=oid, username="eve",
                       full_username=f"eve@{slug}.test", role="user", is_active=True,
                       auth_method="local")
    admin = UserAccount(id=secrets.token_hex(8), org_id=oid, username="ada",
                        full_username=f"ada@{slug}.test", role="org_admin", is_active=True,
                        auth_method="local")
    db.add_all([user, admin])
    # A grant to somebody else: with a row present, the space is no longer
    # default-open, and eve is not that somebody.
    db.add(WikiSpaceACL(org_id=oid, space_path="private", grantee_type="user",
                        grantee_id="somebody-else", access="reader"))
    db.commit()

    def headers(u):
        return {"X-Auth-User": u.username, "X-Auth-User-Id": u.id,
                "X-Auth-Org-Id": oid, "X-Auth-Role": u.role}
    user_h, admin_h = headers(user), headers(admin)
    db.close()

    store = {"public/a.md": b"# a", "private/b.md": b"# b", "root.md": b"# r",
             "private/img.png": b"\x89PNG", "cards/c1/f.bin": b"bin"}

    class FakeS3:
        def walk(self, org_slug, root=""):
            return [k for k in store if k.startswith(root)]
        def read(self, org_slug, key):
            return store[key]
    monkeypatch.setattr(backup_router, "_s3", lambda: FakeS3())
    monkeypatch.setattr(wiki_router, "_s3_read", lambda org, rel: store[rel])
    monkeypatch.setattr(backup_router, "_s3_read", lambda org, rel: store[rel])
    return user_h, admin_h


def test_image_attachment_and_export_page_apply_the_space_acl(client, org_with_a_private_space):
    user, admin = org_with_a_private_space
    for path, route in (("private/img.png", "/api/image"), ("private/b.md", "/api/export/page"),
                        ("private/img.png", "/api/attachment")):
        assert client.get(route, params={"path": path}, headers=user).status_code == 403, route
        assert client.get(route, params={"path": path}, headers=admin).status_code == 200, route
    assert client.get("/api/image", params={"path": "public/a.md"}, headers=user).status_code == 200


def test_the_kb_export_contains_only_what_the_user_may_read(client, org_with_a_private_space):
    user, admin = org_with_a_private_space
    def names(h):
        r = client.get("/api/export/kb", headers=h)
        assert r.status_code == 200, r.text
        return set(zipfile.ZipFile(io.BytesIO(r.content)).namelist())
    assert names(user) == {"public/a.md", "root.md", "cards/c1/f.bin"}
    assert names(admin) == {"public/a.md", "private/b.md", "root.md", "private/img.png", "cards/c1/f.bin"}
