"""Issue #93 — the MCP-side bugs Codex found while exercising every tool.

Three of the five live here: the internal URL leaking through an unhandled
upstream error, search going silently empty under a label scope, and
memory_capture withholding the session id that memory_session needs.
"""
import httpx
import pytest
import respx

EMBEDDER = "http://test-embedder:8001"
MEMORY_API = "http://test-memory-api:8002"
_RW = {"user_id": "u1", "tenant_slug": "acme", "name": "agent",
       "wiki_read": True, "wiki_write": True,
       "memory_read": True, "memory_write": True}


@pytest.fixture
def helpers():
    import main
    return main


# ── the internal URL must never reach a client ─────────────────────────────

def test_upstream_error_message_carries_no_url(helpers):
    """httpx's own HTTPStatusError embeds the request URL. Ours must not:
    that string is what told a user Elefant talks to http://embedder:8001."""
    exc = helpers.UpstreamError(404, "no such memory")
    text = str(exc)
    assert "404" in text and "no such memory" in text
    for leak in ("http://", "embedder", "8001", "memory-api", "editor"):
        assert leak not in text


def test_raise_clean_prefers_the_upstream_detail(helpers):
    resp = httpx.Response(403, json={"detail": "token lacks memory_write"},
                          request=httpx.Request("GET", f"{EMBEDDER}/api/x"))
    with pytest.raises(helpers.UpstreamError) as e:
        helpers._raise_clean(resp)
    assert e.value.status == 403
    assert e.value.detail == "token lacks memory_write"


def test_raise_clean_never_relays_a_raw_body(helpers):
    """A non-JSON body can carry a traceback, a hostname or a path — it gets
    logged, not forwarded."""
    resp = httpx.Response(
        500, text="Traceback: connection to http://embedder:8001 refused",
        request=httpx.Request("GET", f"{EMBEDDER}/api/x"))
    with pytest.raises(helpers.UpstreamError) as e:
        helpers._raise_clean(resp)
    assert "embedder" not in str(e.value)
    assert "8001" not in str(e.value)


def test_raise_clean_passes_success_through(helpers):
    resp = httpx.Response(200, json={"ok": True},
                          request=httpx.Request("GET", f"{EMBEDDER}/api/x"))
    assert helpers._raise_clean(resp) is None


@respx.mock
@pytest.mark.asyncio
async def test_memory_delete_404_is_a_plain_answer(helpers, fake_ctx):
    """The reported case. Deleting something already gone is an ordinary
    outcome, not an exception with a URL in it."""
    respx.delete(url__regex=rf"{EMBEDDER}/api/memories/.*").mock(
        return_value=httpx.Response(404, json={"detail": "Memory not found"}))
    out = await helpers.memory_delete("does-not-exist", ctx=fake_ctx(token_info=_RW))
    assert "nothing to delete" in out
    for leak in ("http://", "embedder", "8001", "Traceback"):
        assert leak not in out


@respx.mock
@pytest.mark.asyncio
async def test_other_delete_failures_are_reported_without_the_url(helpers, fake_ctx):
    respx.delete(url__regex=rf"{EMBEDDER}/api/memories/.*").mock(
        return_value=httpx.Response(500, text="boom at http://embedder:8001"))
    out = await helpers.memory_delete("abc", ctx=fake_ctx(token_info=_RW))
    assert "500" in out
    assert "embedder" not in out and "8001" not in out


# ── search going silently empty ────────────────────────────────────────────

RESULT = {"id": "1", "path": "AI/guide.md", "title": "Guide",
          "tags": [], "score": 0.9, "source": "wiki"}


def test_label_scope_still_hides_what_it_should(helpers):
    """The ACL is unchanged — this is about explaining it, not loosening it."""
    scoped = {"allowed_labels": ["infra"]}
    assert helpers._filter_by_label_scope([dict(RESULT)], scoped) == []
    assert helpers._filter_by_label_scope(
        [{**RESULT, "tags": ["infra"]}], scoped) != []


def test_empty_reason_explains_a_label_scope_drop(helpers):
    """An untagged page matches no label, so it is always filtered — including
    the page the agent just wrote. Silence made that look like a broken index."""
    msg = helpers._empty_reason([dict(RESULT)], [], {"allowed_labels": ["infra"]})
    assert "label scope" in msg
    assert "infra" in msg
    assert "no matching tag" in msg


def test_empty_reason_is_silent_when_there_was_simply_nothing(helpers):
    """A genuinely empty index must not get a scary ACL warning."""
    assert helpers._empty_reason([], [], {"allowed_labels": ["infra"]}) == ""


def test_empty_reason_is_silent_when_results_survived(helpers):
    assert helpers._empty_reason([dict(RESULT)], [dict(RESULT)], {}) == ""


def test_empty_reason_covers_classification_too(helpers):
    msg = helpers._empty_reason([dict(RESULT)], [], {})
    assert "classification" in msg


@respx.mock
@pytest.mark.asyncio
async def test_wiki_search_says_why_it_found_nothing(helpers, fake_ctx):
    respx.get(f"{EMBEDDER}/api/search").mock(
        return_value=httpx.Response(200, json={"results": [dict(RESULT)]}))
    out = await helpers.wiki_search_only(
        "the new guide", ctx=fake_ctx(token_info={**_RW, "allowed_labels": ["infra"]}))
    assert "No wiki results" in out
    assert "label scope" in out


@respx.mock
@pytest.mark.asyncio
async def test_wiki_search_stays_quiet_on_a_real_miss(helpers, fake_ctx):
    respx.get(f"{EMBEDDER}/api/search").mock(
        return_value=httpx.Response(200, json={"results": []}))
    out = await helpers.wiki_search_only("nothing", ctx=fake_ctx(token_info=_RW))
    assert "No wiki results" in out
    assert "⚠️" not in out


# ── memory_capture must hand back the session id ───────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_capture_returns_the_session_id(helpers, fake_ctx):
    """memory_session takes a session id and nothing else produced one, so the
    tool could only ever be called with a guess."""
    respx.post(f"{MEMORY_API}/memory/capture").mock(
        return_value=httpx.Response(200, json={"id": "m1", "session": "sess-abc"}))
    out = await helpers.memory_capture("a fact", ctx=fake_ctx(token_info=_RW))
    assert "sess-abc" in out
    assert "memory_session" in out


@respx.mock
@pytest.mark.asyncio
async def test_capture_says_when_the_client_sent_no_session(helpers, fake_ctx):
    """Some clients don't send mcp-session-id. Printing nothing made that look
    like memory_session was broken rather than the attribution being absent."""
    respx.post(f"{MEMORY_API}/memory/capture").mock(
        return_value=httpx.Response(200, json={"id": "m1", "session": None}))
    out = await helpers.memory_capture("a fact", ctx=fake_ctx(token_info=_RW))
    assert "mcp-session-id" in out
    assert "cannot find it" in out


@respx.mock
@pytest.mark.asyncio
async def test_deduped_capture_also_reports_the_session(helpers, fake_ctx):
    respx.post(f"{MEMORY_API}/memory/capture").mock(
        return_value=httpx.Response(200, json={"id": "m1", "deduped": True,
                                               "session": "sess-abc"}))
    out = await helpers.memory_capture("a fact", ctx=fake_ctx(token_info=_RW))
    assert "deduped" in out and "sess-abc" in out
