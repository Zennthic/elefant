"""Tests for the wiki version-history MCP tools: wiki_history, wiki_revision,
wiki_revert, wiki_diff.

The editor's internal endpoints are mocked with respx, so these pin down the
tool contract: permission gating, that the caller's token_hash is forwarded,
and the formatting/diff behaviour — without needing a live editor or S3.
"""
import json

import httpx
import pytest
import respx


@pytest.fixture
def helpers():
    import main
    return main


_RW = {"user_id": "u1", "org_id": "o1", "tenant_slug": "acme",
       "wiki_read": True, "wiki_write": True}
_RO = {"user_id": "u1", "tenant_slug": "acme", "wiki_read": True, "wiki_write": False}


# ── wiki_history ──────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_wiki_history_formats_and_forwards_token(helpers, fake_ctx):
    route = respx.post("http://test-editor:8080/api/internal/wiki/history").mock(
        return_value=httpx.Response(200, json={
            "path": "AI/p.md", "count": 2,
            "revisions": [
                {"id": 7, "edited_at": "2026-06-16T10:00:00", "edited_by_name": "Bob",
                 "source": "editor", "changed_fields": ["content"], "tags": ["ci"],
                 "size": 42, "message": None},
                {"id": 3, "edited_at": "2026-06-15T09:00:00", "edited_by_name": "claude",
                 "source": "mcp", "changed_fields": ["created"], "tags": [],
                 "size": 10, "message": None},
            ],
        }),
    )
    out = await helpers.wiki_history(path="AI/p.md", ctx=fake_ctx(token_info=_RW))
    assert "History of `AI/p.md`" in out
    assert "Version 2" in out and "Version 1" in out
    assert "Bob" in out
    assert route.called
    sent = json.loads(route.calls.last.request.content)
    assert sent["token_hash"] == "testhash"
    assert sent["path"] == "AI/p.md"


@respx.mock
@pytest.mark.asyncio
async def test_wiki_history_empty(helpers, fake_ctx):
    respx.post("http://test-editor:8080/api/internal/wiki/history").mock(
        return_value=httpx.Response(200, json={"path": "AI/p.md", "count": 0, "revisions": []}),
    )
    out = await helpers.wiki_history(path="AI/p.md", ctx=fake_ctx(token_info=_RW))
    assert "No revisions recorded" in out


@pytest.mark.asyncio
async def test_wiki_history_requires_wiki_read(helpers, fake_ctx):
    out = await helpers.wiki_history(path="AI/p.md",
                                     ctx=fake_ctx(token_info={"wiki_read": False}))
    assert "wiki_read" in out.lower()


# ── wiki_revision ─────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_wiki_revision_returns_content(helpers, fake_ctx):
    respx.post("http://test-editor:8080/api/internal/wiki/revision").mock(
        return_value=httpx.Response(200, json={
            "path": "AI/p.md",
            "revision": {"id": 5, "edited_at": "2026-06-16T10:00:00",
                         "edited_by_name": "Bob", "changed_fields": ["content"],
                         "content": "# The old content"}}),
    )
    out = await helpers.wiki_revision(path="AI/p.md", revision_id=5,
                                      ctx=fake_ctx(token_info=_RW))
    assert "revision `5`" in out
    assert "# The old content" in out


# ── wiki_revert ───────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_wiki_revert_forwards_and_reports(helpers, fake_ctx):
    route = respx.post("http://test-editor:8080/api/internal/wiki/revert").mock(
        return_value=httpx.Response(200, json={
            "status": "reverted", "path": "AI/p.md",
            "revision_id": 3, "restored_from": "2026-06-15T09:00:00"}),
    )
    out = await helpers.wiki_revert(path="AI/p.md", revision_id=3,
                                    ctx=fake_ctx(token_info=_RW))
    assert "Reverted" in out and "AI/p.md" in out and "3" in out
    assert route.called
    sent = json.loads(route.calls.last.request.content)
    assert sent["revision_id"] == 3


@pytest.mark.asyncio
async def test_wiki_revert_requires_wiki_write(helpers, fake_ctx):
    out = await helpers.wiki_revert(path="AI/p.md", revision_id=3,
                                    ctx=fake_ctx(token_info=_RO))
    assert "wiki_write" in out.lower()


# ── wiki_diff ─────────────────────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_wiki_diff_between_two_revisions(helpers, fake_ctx):
    def _rev(request):
        rid = json.loads(request.content)["revision_id"]
        content = {1: "line A\nline B", 2: "line A\nline C"}[rid]
        return httpx.Response(200, json={"path": "AI/p.md",
                                         "revision": {"id": rid, "content": content}})
    respx.post("http://test-editor:8080/api/internal/wiki/revision").mock(side_effect=_rev)

    out = await helpers.wiki_diff(path="AI/p.md", revision_id=1, against_id=2,
                                  ctx=fake_ctx(token_info=_RW))
    assert "```diff" in out
    assert "-line B" in out and "+line C" in out


@respx.mock
@pytest.mark.asyncio
async def test_wiki_diff_identical_is_noop(helpers, fake_ctx):
    def _rev(request):
        return httpx.Response(200, json={"path": "AI/p.md",
                                         "revision": {"id": 1, "content": "same"}})
    respx.post("http://test-editor:8080/api/internal/wiki/revision").mock(side_effect=_rev)
    out = await helpers.wiki_diff(path="AI/p.md", revision_id=1, against_id=1,
                                  ctx=fake_ctx(token_info=_RW))
    assert "No differences" in out
