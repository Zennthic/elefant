"""Regression-prevention for a production bug where a user of a
non-default tenant saw `memory_list` / `memory_search` returning
403 "tenant_slug requires an authenticated session" while `memory_
capture` (hot tier via memory-api with the internal-service token)
worked fine.

Root cause: `memory_list`, `memory_search`, `search`, `memory_session`,
`memory_provenance`, `reindex_status`, and the `kb://*` resources all
called the bare `_get()` helper, which does NOT forward the caller's
X-Auth-* headers.  When the caller's tenant_slug was anything other
than "default" the embedder's `_resolve_tenant_slug` correctly refused
(no identity proven).

Fix: route every tenant-scoped read through `_get_authed` so the
embedder receives the same X-Auth-Org-Slug the editor-routed UI sends.
This test pins down the forwarding behavior so a future helper rename
or signature-change doesn't silently regress it again.
"""
import pytest
import respx
import httpx


@pytest.fixture
def helpers():
    import main
    return main


# ── _get_authed forwards X-Auth-Org-Slug ─────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_get_authed_forwards_tenant_slug_header(helpers):
    """The whole reason this helper exists — without the header the
    embedder's _resolve_tenant_slug 403s any non-default slug."""
    route = respx.get("http://test-embedder:8001/api/memories").mock(
        return_value=httpx.Response(200, json={"memories": []}),
    )
    await helpers._get_authed(
        "/api/memories",
        {"user_id": "u1", "org_id": "o1", "tenant_slug": "acme-corp"},
        tenant_slug="acme-corp",
    )
    assert route.called
    req = route.calls.last.request
    assert req.headers.get("x-auth-org-slug") == "acme-corp"
    assert req.headers.get("x-auth-user-id")  == "u1"
    assert req.headers.get("x-auth-org-id")   == "o1"


@respx.mock
@pytest.mark.asyncio
async def test_get_authed_omits_headers_when_token_empty(helpers):
    """Anonymous/unauthenticated callers (kb:// resources without a
    ctx-bound token) get no X-Auth-* headers — the embedder's
    fail-closed behavior then applies, which is the correct posture."""
    route = respx.get("http://test-embedder:8001/api/status").mock(
        return_value=httpx.Response(200, json={"state": "idle"}),
    )
    await helpers._get_authed("/api/status", {}, tenant_slug="default")
    assert route.called
    req = route.calls.last.request
    assert "x-auth-org-slug" not in req.headers
    assert "x-auth-user-id"  not in req.headers


# ── Static check: no tenant-scoped MCP tool calls the bare _get ──────────

def test_no_bare_get_calls_in_mcp_source():
    """The bare `_get` helper drops auth headers — every tool that
    targets the embedder with a tenant_slug must use `_get_authed`.

    Scanning the source is heavy-handed but it's the cheapest way to
    catch a future maintainer who adds a new tenant-scoped read and
    reaches for the unauthenticated helper out of habit."""
    from pathlib import Path
    src = Path(__file__).resolve().parent.parent / "main.py"
    text = src.read_text()
    # Find any `await _get(...` that's NOT _get_authed / _get_memory_api /
    # _editor_get etc.  Use a simple regex on "await _get(" boundary.
    import re
    matches = re.findall(r"await\s+_get\(", text)
    assert matches == [], (
        f"Found {len(matches)} bare `await _get(...)` call(s) in mcp/main.py "
        "— use _get_authed instead so the embedder receives the caller's "
        "X-Auth-Org-Slug header (otherwise non-default tenants 403 on the "
        "embedder's _resolve_tenant_slug). See 2026-06-15 incident."
    )


# ── End-to-end: memory_search forwards through _get_authed ───────────────

@respx.mock
@pytest.mark.asyncio
async def test_memory_search_forwards_tenant_slug_header(helpers, fake_ctx):
    """The reproducer for the non-default-tenant 403.  Pre-fix this
    would have called bare _get(), the embedder would have seen no
    X-Auth-Org-Slug, and the request would have 403'd before reaching
    Qdrant.  Post-fix the slug rides through and the search succeeds."""
    route = respx.get("http://test-embedder:8001/api/memories/search").mock(
        return_value=httpx.Response(200, json={"query": "x", "results": []}),
    )
    ctx = fake_ctx(token_info={
        "user_id":     "u-acme",
        "org_id":      "org-acme",
        "tenant_slug": "acme-corp",
        "memory_read": True,
    })
    out = await helpers.memory_search(query="anything", ctx=ctx)
    assert "No memories found" in out  # empty mocked result
    assert route.called
    req = route.calls.last.request
    assert req.headers.get("x-auth-org-slug") == "acme-corp"


@respx.mock
@pytest.mark.asyncio
async def test_memory_list_forwards_tenant_slug_header(helpers, fake_ctx):
    """Sibling tool that hit the same bug — pin it down too."""
    route = respx.get("http://test-embedder:8001/api/memories").mock(
        return_value=httpx.Response(200, json={"memories": []}),
    )
    ctx = fake_ctx(token_info={
        "user_id":     "u-acme",
        "org_id":      "org-acme",
        "tenant_slug": "acme-corp",
        "memory_read": True,
    })
    out = await helpers.memory_list(ctx=ctx)
    assert "No memories found" in out
    assert route.called
    req = route.calls.last.request
    assert req.headers.get("x-auth-org-slug") == "acme-corp"
