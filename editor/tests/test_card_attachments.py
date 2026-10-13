"""Card attachments: what the upload routes accept, and how downloads are served.

Two different risks, and they need different tests.

Uploading is about REFUSING READABLY. "Nothing happened" on a 40MB paste is the
worst version of this, and silently storing an unknown file as a .png is the
second worst — it succeeds and then renders as a broken image with nothing to
say why.

Downloading is about not turning an upload into stored XSS. /api/attachment
takes arbitrary uploaded content, so an uploaded .html or .svg served inline
from our own origin would run in the board's origin for everybody who can see
it.
"""
import inspect
import re
from pathlib import Path

import pytest

from routers import wiki as wiki_router

_NGINX_CONF = Path(__file__).resolve().parents[2] / "nginx" / "nginx.conf"


def _location_blocks(text: str):
    for m in re.finditer(r"location\s+([^\{]+)\{", text):
        i = m.end()
        depth = 1
        while i < len(text) and depth:
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
            i += 1
        yield m.group(1).strip(), text[m.end():i]


# ── Authorisation: an attachment URL is not a bearer token for the file ──────

@pytest.mark.parametrize("route", ["/api/upload-file", "/api/attachment", "/api/upload-image"])
def test_asset_routes_are_behind_auth_request(route):
    """The design's first rule. Seeing an attachment has to require being able
    to see the board, which means the same authenticated route the wiki's
    images already use — not a URL that is itself the credential."""
    text = _NGINX_CONF.read_text()
    blocks = [body for name, body in _location_blocks(text) if name.split()[-1] == route]
    assert blocks, f"no nginx location for {route}"
    for body in blocks:
        assert "auth_request       /api/auth/check;" in body, \
            f"{route} is served without auth_request — the URL would be the credential"


# ── Downloading: never inline, never sniffed ─────────────────────────────────

def test_attachments_are_served_as_a_download_not_a_document():
    """All three headers are load-bearing and any one of them alone is not
    enough: the neutral type stops it being interpreted, `nosniff` stops the
    browser second-guessing the type, and the disposition stops it rendering
    in our origin at all."""
    src = inspect.getsource(wiki_router.get_attachment)
    assert 'media_type="application/octet-stream"' in src, \
        "an attachment served with its own content type can execute in our origin"
    assert '"X-Content-Type-Options": "nosniff"' in src, \
        "without nosniff the browser may sniff an uploaded file back into HTML"
    assert "attachment; filename=" in src, \
        "without an attachment disposition an uploaded .html renders in our origin"


def test_attachment_paths_are_normalized_before_being_read():
    """Traversal is stopped by the same helper every other asset route uses."""
    src = inspect.getsource(wiki_router.get_attachment)
    assert "_normalize_path(path)" in src


# ── Uploading an image: refuse, and say why ──────────────────────────────────

def test_svg_is_not_an_accepted_image_type():
    """SVG is a script-capable document, and /api/image serves it as
    image/svg+xml from our own origin. Accepting one is accepting stored XSS
    from anybody who can paste into a card."""
    assert ".svg" not in wiki_router._IMAGE_EXTS
    assert "image/svg+xml" not in wiki_router._IMAGE_EXT_BY_MIME


def test_the_accepted_image_types_are_the_ones_a_screenshot_can_be():
    assert {".png", ".jpg", ".gif", ".webp"} <= wiki_router._IMAGE_EXTS


def test_an_unknown_type_is_refused_rather_than_stored_as_a_png():
    """The old path defaulted an unrecognised upload to .png. That succeeds and
    then renders as a broken image, with nothing anywhere saying what went
    wrong."""
    src = inspect.getsource(wiki_router.upload_image)
    assert 'ext = mime_to_ext.get(file.content_type, ".png")' not in src
    assert "status_code=400" in src


def test_the_image_size_limit_is_enforced_and_readable():
    src = inspect.getsource(wiki_router.upload_image)
    assert "MAX_IMAGE_BYTES" in src
    assert "status_code=413" in src
    # The message has to name the limit; "too large" leaves somebody guessing
    # whether to scale down or give up.
    assert "MAX_IMAGE_BYTES // (1024 * 1024)" in src
    assert wiki_router.MAX_IMAGE_BYTES <= 25 * 1024 * 1024


def test_the_attachment_size_limit_is_enforced_and_readable():
    src = inspect.getsource(wiki_router.upload_file)
    assert "MAX_ATTACHMENT_BYTES" in src
    assert "status_code=413" in src


# ── The upload prefix is allowlisted, not caller-chosen ──────────────────────

@pytest.mark.parametrize("good", [
    "cards/board-card-attachments",
    "cards/a",
    "cards/some-card.v2",
])
def test_a_card_prefix_is_accepted(good):
    assert wiki_router._CARD_PREFIX_RE.match(good)


@pytest.mark.parametrize("bad", [
    "assets/images",            # the wiki's own folder
    "cards/../assets",          # traversal
    "cards/a/b",                # deeper than a card's own folder
    "/cards/a",                 # absolute
    "cards/",                   # empty slug
    "Cards/a",                  # case
    "backups",
    "",
])
def test_anything_but_a_card_prefix_is_refused(bad):
    """_normalize_path stops traversal, but a free-form prefix would still let
    a caller scatter objects anywhere in the org's bucket."""
    assert not wiki_router._CARD_PREFIX_RE.match(bad)


def test_upload_file_requires_a_prefix_rather_than_defaulting_to_one():
    """An attachment belongs to a card. Defaulting the prefix would let a
    caller who forgot to say which card drop files into a shared folder."""
    from pydantic_core import PydanticUndefined
    sig = inspect.signature(wiki_router.upload_file)
    # Form(...) — FastAPI records a required form field as PydanticUndefined.
    assert sig.parameters["prefix"].default.default is PydanticUndefined


# ── The stored key carries the filename ──────────────────────────────────────

def test_the_display_name_comes_back_out_of_the_key():
    """There is no second place remembering what a file was called, so there is
    no way for the two to disagree."""
    assert wiki_router._attachment_display_name(
        "cards/alpha/1757000000-ab12cd34-deploy log.txt".replace(" ", "_")
    ) == "deploy_log.txt"


def test_a_hostile_filename_cannot_shape_the_key():
    """The name is sanitised into the key, so a filename cannot introduce path
    separators, traversal, or a leading dot."""
    hostile = "../../etc/passwd"
    safe = wiki_router._SAFE_NAME_RE.sub("_", Path(hostile).name).strip("._") or "file"
    assert "/" not in safe and ".." not in safe
    assert safe == "passwd"
