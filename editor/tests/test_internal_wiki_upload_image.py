"""/api/internal/wiki/upload-image — the store half of embedding an image.

Like every agent write path, what matters most is what it refuses: a token
without wiki_write, a path outside the AI folder, a type the wiki will not
serve safely (SVG above all), and bytes that are not the image they claim to
be. The happy path is checked against a stubbed bucket.
"""
import base64

import pytest
from fastapi.testclient import TestClient

import main

# A 1x1 PNG.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
PNG_B64 = base64.b64encode(PNG).decode()


@pytest.fixture
def client():
    return TestClient(main.app)


@pytest.fixture
def tenant():
    import hashlib
    import secrets
    from config_db import get_db_session, Tenant, ApiToken, TenantSettings

    def sha(raw):
        return hashlib.sha256(raw.encode()).hexdigest()

    db = next(get_db_session())
    oid = secrets.token_hex(8)
    slug = f"wi-{oid[:6]}"
    db.add(Tenant(id=oid, name=slug, slug=slug, display_name=slug,
                  domain=f"{slug}.test", lifecycle_status="active", is_active=True))
    db.add(TenantSettings(tenant_slug=slug, ai_wiki_folder="AI"))
    rw, ro = sha(secrets.token_urlsafe(24)), sha(secrets.token_urlsafe(24))
    db.add(ApiToken(name="agent", token_hash=rw, tenant_id=oid,
                    wiki_read=True, wiki_write=True))
    db.add(ApiToken(name="reader", token_hash=ro, tenant_id=oid,
                    wiki_read=True, wiki_write=False))
    db.commit(); db.close()
    return {"slug": slug, "id": oid, "hash": rw, "ro_hash": ro}


@pytest.fixture
def bucket(monkeypatch):
    """An in-memory stand-in for the tenant's S3 prefix."""
    store = {}
    monkeypatch.setattr(main, "_s3_write", lambda org, rel, data: store.__setitem__(rel, data))
    monkeypatch.setattr(main, "_s3_exists", lambda org, rel: rel in store)
    return store


def _up(client, token, **body):
    body.setdefault("path", "AI/projects/x/assets/a.png")
    body.setdefault("content_type", "image/png")
    body.setdefault("image_base64", PNG_B64)
    return client.post("/api/internal/wiki/upload-image",
                       json={"token_hash": token, **body})


# ── the boundary ───────────────────────────────────────────────────────────

def test_a_bad_token_is_rejected(client):
    assert _up(client, "deadbeef").status_code == 401


def test_read_only_token_cannot_upload(client, tenant):
    r = _up(client, tenant["ro_hash"])
    assert r.status_code == 403
    assert "wiki_write" in r.json()["detail"]


@pytest.mark.parametrize("path", [
    "Runbooks/assets/a.png",        # promoted out of the AI folder
    "assets/images/a.png",          # the UI's shared asset folder
    "AInotes/a.png",                # prefix lookalike
])
def test_paths_outside_the_ai_folder_are_refused(client, tenant, bucket, path):
    r = _up(client, tenant["hash"], path=path)
    assert r.status_code == 403
    assert not bucket


def test_traversal_is_refused(client, tenant, bucket):
    r = _up(client, tenant["hash"], path="AI/../Runbooks/a.png")
    assert r.status_code in (400, 403)
    assert not bucket


# ── what it will store ─────────────────────────────────────────────────────

def test_svg_is_refused_by_name(client, tenant, bucket):
    svg = base64.b64encode(b"<svg xmlns='http://www.w3.org/2000/svg'><script/></svg>").decode()
    r = _up(client, tenant["hash"], path="AI/a.svg", content_type="image/svg+xml",
            image_base64=svg)
    assert r.status_code == 415
    assert "SVG" in r.json()["detail"]
    assert not bucket


def test_extension_must_agree_with_the_type(client, tenant, bucket):
    r = _up(client, tenant["hash"], path="AI/a.jpg", content_type="image/png")
    assert r.status_code == 422
    assert ".png" in r.json()["detail"]


def test_bytes_must_be_the_image_they_claim_to_be(client, tenant, bucket):
    r = _up(client, tenant["hash"], path="AI/a.gif", content_type="image/gif")
    assert r.status_code == 422
    assert "not a image/gif" in r.json()["detail"]
    assert not bucket


def test_invalid_base64_is_refused(client, tenant, bucket):
    r = _up(client, tenant["hash"], image_base64="not base64 at all!!")
    assert r.status_code == 422


def test_an_oversized_image_is_refused_readably(client, tenant, bucket, monkeypatch):
    from routers import wiki as wiki_router
    monkeypatch.setattr(wiki_router, "MAX_IMAGE_BYTES", 10)
    r = _up(client, tenant["hash"])
    assert r.status_code == 413
    assert not bucket


# ── storing it ─────────────────────────────────────────────────────────────

def test_a_png_is_stored_byte_for_byte(client, tenant, bucket):
    r = _up(client, tenant["hash"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["path"] == "AI/projects/x/assets/a.png"
    assert body["created"] is True
    assert body["bytes"] == len(PNG)
    assert bucket["AI/projects/x/assets/a.png"] == PNG


def test_a_data_uri_is_accepted_and_jpg_alias_normalised(client, tenant, bucket):
    jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 16
    uri = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()
    r = _up(client, tenant["hash"], path="AI/a.jpeg", content_type="image/jpg",
            image_base64=uri)
    assert r.status_code == 200, r.text
    assert r.json()["content_type"] == "image/jpeg"
    assert bucket["AI/a.jpeg"] == jpeg


def test_uploading_again_replaces(client, tenant, bucket):
    assert _up(client, tenant["hash"]).json()["created"] is True
    assert _up(client, tenant["hash"]).json()["created"] is False


def test_the_stored_image_reads_back_through_the_image_endpoint(client, tenant, bucket, monkeypatch):
    """The round trip get_wiki_page makes: what was stored is what is served,
    under the type the extension implies."""
    _up(client, tenant["hash"])
    monkeypatch.setattr(main, "_s3_read", lambda org, rel: bucket[rel])
    r = client.post("/api/internal/wiki/image",
                    json={"token_hash": tenant["hash"], "path": "AI/projects/x/assets/a.png"})
    assert r.status_code == 200
    assert r.content == PNG
    assert r.headers["content-type"].startswith("image/png")


def test_a_webp_reads_back_as_image_webp(client, tenant, bucket, monkeypatch):
    """The slim image has no /etc/mime.types and Python 3.11 does not know
    .webp, so a guessed type was application/octet-stream in production and
    get_wiki_page built a data: URI of that type."""
    webp = b"RIFF" + b"\x00" * 4 + b"WEBPVP8 " + b"\x00" * 16
    bucket["AI/projects/x/assets/a.webp"] = webp
    monkeypatch.setattr(main, "_s3_read", lambda org, rel: bucket[rel])
    r = client.post("/api/internal/wiki/image",
                    json={"token_hash": tenant["hash"], "path": "AI/projects/x/assets/a.webp"})
    assert r.status_code == 200
    assert r.content == webp
    assert r.headers["content-type"].startswith("image/webp")
