"""Heading anchors on the agent side.

An agent can read one section of a page as `path#anchor`, sees every anchor a
page has when it reads the whole thing, and gets search hits on a section
back as `path#anchor` it can cite. The slug rule is a copy of the editor's;
both are held to tests/fixtures/heading-anchors.json.
"""
import json
from pathlib import Path

import httpx
import pytest
import respx

_RO = {"user_id": "u1", "tenant_slug": "acme", "wiki_read": True}
_READ = "http://test-editor:8080/api/internal/wiki/read"
_CARDS = "http://test-editor:8080/api/internal/board/page-cards"

FIXTURE = json.loads(
    (Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "heading-anchors.json")
    .read_text(encoding="utf-8"))


@pytest.fixture
def m():
    import main
    return main


@pytest.mark.parametrize("raw,anchor", FIXTURE["headings"])
def test_anchor_matches_the_shared_fixture(m, raw, anchor):
    assert m._heading_anchor(raw) == anchor


def test_embedder_style_titles_with_closing_hashes(m):
    assert m._heading_anchor("Rollback Plan ##") == "rollback-plan"


def test_search_hit_on_a_section_carries_its_anchor(m):
    hit = {"path": "runbooks/db.md", "title": "db", "section_title": "Rollback Plan",
           "score": 0.9, "tags": [], "excerpt": "x"}
    assert m._wiki_hit_path(hit) == "runbooks/db.md#rollback-plan"
    assert "**Path:** runbooks/db.md#rollback-plan" in m._fmt_wiki_result(hit, 1)


def test_search_hit_on_a_whole_page_has_no_anchor(m):
    assert m._wiki_hit_path({"path": "a.md", "section_title": ""}) == "a.md"
    assert m._wiki_hit_path({"path": "a.md"}) == "a.md"


def _mock_read(payload):
    route = respx.post(_READ).mock(return_value=httpx.Response(200, json=payload))
    respx.post(_CARDS).mock(return_value=httpx.Response(200, json={"cards": []}))
    return route


_PAGE = "# DB\n\n## Steps\n\none\n\n## Rollback\n\nundo\n"
_HEADS = [{"level": 1, "text": "DB", "anchor": "db"},
          {"level": 2, "text": "Steps", "anchor": "steps"},
          {"level": 2, "text": "Rollback", "anchor": "rollback"}]


@respx.mock
@pytest.mark.asyncio
async def test_section_read_returns_only_that_section(m, fake_ctx):
    route = _mock_read({"path": "runbooks/db.md", "content": _PAGE, "headings": _HEADS,
                        "anchor": "rollback",
                        "section": {"anchor": "rollback", "heading": "Rollback",
                                    "level": 2, "content": "## Rollback\n\nundo\n"}})
    out = await m.get_wiki_page("runbooks/db.md#rollback", ctx=fake_ctx(token_info=_RO))
    assert json.loads(route.calls.last.request.content)["path"] == "runbooks/db.md#rollback"
    assert out.startswith("# runbooks/db.md#rollback\n")
    assert "undo" in out and "one" not in out
    assert "`runbooks/db.md`" in out, "the agent is told how to get the whole page"


@respx.mock
@pytest.mark.asyncio
async def test_unknown_anchor_fails_soft_to_the_whole_page(m, fake_ctx):
    _mock_read({"path": "runbooks/db.md", "content": _PAGE, "headings": _HEADS,
                "anchor": "renamed", "section": None})
    out = await m.get_wiki_page("runbooks/db.md#renamed", ctx=fake_ctx(token_info=_RO))
    assert out.startswith("# runbooks/db.md\n")
    assert "`#renamed`" in out and "whole page" in out
    assert "one" in out and "undo" in out


@respx.mock
@pytest.mark.asyncio
async def test_full_read_lists_the_section_anchors(m, fake_ctx):
    _mock_read({"path": "runbooks/db.md", "content": _PAGE, "headings": _HEADS})
    out = await m.get_wiki_page("runbooks/db.md", ctx=fake_ctx(token_info=_RO))
    tail = out[out.index("Section anchors"):]
    assert "`#db`, `#steps`, `#rollback`" in tail
    assert "runbooks/db.md#anchor" in tail


@respx.mock
@pytest.mark.asyncio
async def test_wiki_edit_takes_the_section_from_the_path(m, fake_ctx):
    respx.get(url__regex=r".*/api/internal/tenant-settings.*").mock(
        return_value=httpx.Response(200, json={"ai_wiki_folder": "AI"}))
    respx.post("http://test-editor:8080/api/internal/wiki/edit/preview").mock(
        return_value=httpx.Response(404, json={"detail": "Not Found"}))
    route = respx.post("http://test-editor:8080/api/internal/wiki/edit").mock(
        return_value=httpx.Response(200, json={"changed": True, "path": "AI/x.md",
                                               "bytes_before": 1, "bytes_after": 2}))
    ctx = fake_ctx(token_info={**_RO, "wiki_write": True})
    await m.wiki_edit("AI/x.md#steps-1", "replace_section", content="new", ctx=ctx)
    sent = json.loads(route.calls.last.request.content)
    assert sent["path"] == "AI/x.md" and sent["heading"] == "steps-1"
