"""wiki_upload_image, and the get_wiki_page path resolution it relies on.

The card: an agent could read images embedded in a page but had no way to put
one there, so mockups and screenshots got linked out instead of shown. The
editor owns the boundary and the type checks; this covers what the MCP layer
owns — the scope check, path and extension handling, the reference it hands
back, and that the reference resolves to the stored image when the page is
read back.
"""
import base64
import json

import httpx
import pytest
import respx

EDITOR = "http://test-editor:8080"
_RW = {"user_id": "u1", "tenant_slug": "acme", "wiki_read": True, "wiki_write": True}
_RO = {"user_id": "u1", "tenant_slug": "acme", "wiki_read": True, "wiki_write": False}
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
PNG_B64 = base64.b64encode(PNG).decode()


@pytest.fixture
def m():
    import main
    return main


@pytest.fixture
def editor():
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{EDITOR}/api/internal/tenant-settings").mock(
            return_value=httpx.Response(200, json={"settings": {"ai_wiki_folder": "AI"}}))
        yield mock


def _saved(path, created=True):
    return httpx.Response(200, json={"status": "saved", "path": path, "created": created,
                                     "bytes": len(PNG), "content_type": "image/png"})


def _sent(route):
    return json.loads(route.calls.last.request.content)


# ── resolving a src the way the wiki UI does ───────────────────────────────

@pytest.mark.parametrize("src,page,want", [
    ("assets/a.png", "AI/projects/t/page.md", ["AI/projects/t/assets/a.png", "assets/a.png"]),
    ("./assets/a.png", "AI/projects/t/page.md", ["AI/projects/t/assets/a.png", "assets/a.png"]),
    ("a.png", "AI/projects/t/page.md", ["AI/projects/t/a.png"]),
    # what the UI writes for a pasted image on a nested page
    ("../../../assets/images/p.png", "AI/projects/t/page.md", ["assets/images/p.png"]),
    ("../shared/a.png", "AI/projects/t/page.md", ["AI/projects/shared/a.png"]),
    ("/AI/x/a.png", "AI/projects/t/page.md", ["AI/x/a.png"]),
    ("assets/a.png", "top.md", ["assets/a.png"]),
    ("assets/a.png", "AI/projects/t/page.md#section", ["AI/projects/t/assets/a.png", "assets/a.png"]),
    ('assets/a.png "a title"', "AI/p.md", ["AI/assets/a.png", "assets/a.png"]),
])
def test_srcs_resolve_relative_to_the_page(m, src, page, want):
    assert m._resolve_img_paths(src, page) == want


@pytest.mark.parametrize("src", [
    "https://example.com/a.png", "http://x/a.png", "//cdn/a.png",
    "data:image/png;base64,AAAA", "", "../../../../a.png",
])
def test_nothing_to_fetch_for_external_or_escaping_srcs(m, src):
    assert m._resolve_img_paths(src, "AI/projects/t/page.md") == []


@pytest.mark.parametrize("asset,page,ref", [
    ("AI/projects/t/assets/a.png", "AI/projects/t/page.md", "assets/a.png"),
    ("AI/projects/t/a.png", "AI/projects/t/page.md", "a.png"),
    ("AI/projects/t/assets/a.png", "AI/projects/t/sub/page.md", "../assets/a.png"),
    ("AI/shared/a.png", "AI/projects/t/page.md", "../../shared/a.png"),
    ("AI/a.png", "top.md", "AI/a.png"),
])
def test_the_reference_handed_back_resolves_to_the_asset(m, asset, page, ref):
    assert m._image_ref_for(asset, page) == ref
    assert m._resolve_img_paths(ref, page)[0] == asset


# ── the tool ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_requires_wiki_write(m, fake_ctx):
    out = await m.wiki_upload_image("AI/a.png", PNG_B64, ctx=fake_ctx(token_info=_RO))
    assert "wiki_write" in out.lower()


@pytest.mark.asyncio
async def test_sends_the_image_and_returns_a_page_relative_reference(m, fake_ctx, editor):
    route = editor.post(f"{EDITOR}/api/internal/wiki/upload-image").mock(
        return_value=_saved("AI/projects/tailor/assets/teaser.png"))
    out = await m.wiki_upload_image(
        "AI/projects/tailor/assets/teaser.png", PNG_B64, "image/png",
        page="AI/projects/tailor/branding.md", ctx=fake_ctx(token_info=_RW))
    sent = _sent(route)
    assert sent["path"] == "AI/projects/tailor/assets/teaser.png"
    assert sent["content_type"] == "image/png"
    assert sent["image_base64"] == PNG_B64
    assert sent["token_hash"] == "testhash"
    assert "Uploaded" in out
    assert "![teaser](assets/teaser.png)" in out


@pytest.mark.asyncio
async def test_without_a_page_the_reference_is_for_the_assets_parent(m, fake_ctx, editor):
    editor.post(f"{EDITOR}/api/internal/wiki/upload-image").mock(
        return_value=_saved("AI/projects/tailor/assets/teaser.png", created=False))
    out = await m.wiki_upload_image("AI/projects/tailor/assets/teaser.png", PNG_B64,
                                    ctx=fake_ctx(token_info=_RW))
    assert "Replaced" in out
    assert "AI/projects/tailor/" in out
    assert "![teaser](assets/teaser.png)" in out


@pytest.mark.asyncio
async def test_type_comes_from_the_extension_when_not_given(m, fake_ctx, editor):
    route = editor.post(f"{EDITOR}/api/internal/wiki/upload-image").mock(
        return_value=_saved("AI/a.jpeg"))
    await m.wiki_upload_image("AI/a.jpeg", PNG_B64, ctx=fake_ctx(token_info=_RW))
    assert _sent(route)["content_type"] == "image/jpeg"


@pytest.mark.asyncio
async def test_extension_comes_from_the_type_when_missing(m, fake_ctx, editor):
    route = editor.post(f"{EDITOR}/api/internal/wiki/upload-image").mock(
        return_value=_saved("AI/assets/diagram.webp"))
    await m.wiki_upload_image("assets/diagram", PNG_B64, "image/webp",
                              ctx=fake_ctx(token_info=_RW))
    assert _sent(route)["path"] == "AI/assets/diagram.webp"


@pytest.mark.asyncio
async def test_no_type_and_no_extension_is_refused_before_upload(m, fake_ctx, editor):
    route = editor.post(f"{EDITOR}/api/internal/wiki/upload-image").mock(
        return_value=_saved("AI/x"))
    out = await m.wiki_upload_image("AI/x", PNG_B64, ctx=fake_ctx(token_info=_RW))
    assert "content_type" in out
    assert not route.called


@pytest.mark.asyncio
async def test_traversal_is_refused_without_calling_the_editor(m, fake_ctx, editor):
    route = editor.post(f"{EDITOR}/api/internal/wiki/upload-image").mock(
        return_value=_saved("x"))
    out = await m.wiki_upload_image("AI/../etc/a.png", PNG_B64, ctx=fake_ctx(token_info=_RW))
    assert "traversal" in out.lower()
    assert not route.called


@pytest.mark.asyncio
async def test_an_editor_refusal_is_passed_on(m, fake_ctx, editor):
    editor.post(f"{EDITOR}/api/internal/wiki/upload-image").mock(
        return_value=httpx.Response(415, json={"detail": "SVG is refused because it can carry script."}))
    out = await m.wiki_upload_image("AI/a.svg", "PHN2Zz4=", "image/svg+xml",
                                    ctx=fake_ctx(token_info=_RW))
    assert "415" in out and "SVG" in out


# ── the acceptance loop: upload, reference, read back inline ───────────────

@pytest.mark.asyncio
async def test_an_uploaded_image_reads_back_inline(m, fake_ctx, editor):
    store = {}

    def _upload(request):
        body = json.loads(request.content)
        store[body["path"]] = base64.b64decode(body["image_base64"])
        return _saved(body["path"])

    def _image(request):
        path = json.loads(request.content)["path"]
        if path not in store:
            return httpx.Response(404)
        return httpx.Response(200, content=store[path], headers={"content-type": "image/png"})

    editor.post(f"{EDITOR}/api/internal/wiki/upload-image").mock(side_effect=_upload)
    editor.post(f"{EDITOR}/api/internal/wiki/image").mock(side_effect=_image)
    editor.post(f"{EDITOR}/api/internal/board/page-cards").mock(
        return_value=httpx.Response(200, json={"cards": []}))

    page = "AI/projects/tailor/branding.md"
    out = await m.wiki_upload_image("AI/projects/tailor/assets/teaser.png", PNG_B64,
                                    page=page, ctx=fake_ctx(token_info=_RW))
    ref = out.split("![teaser](", 1)[1].split(")", 1)[0]

    editor.post(f"{EDITOR}/api/internal/wiki/read").mock(return_value=httpx.Response(
        200, json={"path": page, "content": f"# Branding\n\n![teaser]({ref})\n"}))
    read = await m.get_wiki_page(page, ctx=fake_ctx(token_info=_RW))
    assert f"![teaser](data:image/png;base64,{PNG_B64})" in read


@pytest.mark.asyncio
async def test_a_legacy_root_assets_reference_still_reads_back(m, fake_ctx, editor):
    """Before this change `assets/x.png` on a nested page meant the space root.
    A page written that way must keep rendering."""
    def _image(request):
        if json.loads(request.content)["path"] == "assets/images/old.png":
            return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
        return httpx.Response(404)

    editor.post(f"{EDITOR}/api/internal/wiki/image").mock(side_effect=_image)
    editor.post(f"{EDITOR}/api/internal/board/page-cards").mock(
        return_value=httpx.Response(200, json={"cards": []}))
    editor.post(f"{EDITOR}/api/internal/wiki/read").mock(return_value=httpx.Response(
        200, json={"path": "AI/p/x.md", "content": "![old](assets/images/old.png)"}))
    read = await m.get_wiki_page("AI/p/x.md", ctx=fake_ctx(token_info=_RO))
    assert "data:image/png;base64," in read
