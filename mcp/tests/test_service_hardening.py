"""Service hardening: client-facing errors and bounded in-process state.

A tool failure must not hand the client raw exception text — that is where
internal hostnames, service URLs and collection names leaked from. The client
gets a generic message and a short ref; the server log gets the detail under
the same ref. Errors the caller can act on (not found, bad argument, no
permission) stay specific.

Every in-process map on the MCP server is keyed by something a client
controls, so each one has to be bounded.
"""
import logging
import re

import httpx
import pytest
import respx

import main

EMBEDDER = "http://test-embedder:8001"
MEMORY_API = "http://test-memory-api:8002"
_RW = {"user_id": "u1", "tenant_slug": "acme", "name": "agent",
       "wiki_read": True, "wiki_write": True,
       "memory_read": True, "memory_write": True,
       "board_read": True, "board_write": True}
_LEAKS = ("http://", "qdrant", "6333", "kb_acme", "Traceback", "embedder:", "memory-api")
_REF = re.compile(r"ref ([0-9a-f]{8})")


def _assert_generic(out: str) -> str:
    for leak in _LEAKS:
        assert leak not in out, f"{leak!r} leaked in {out!r}"
    m = _REF.search(out)
    assert m, f"no ref in {out!r}"
    return m.group(1)


# ── generic client errors ───────────────────────────────────────────────────

def test_internal_error_logs_detail_under_the_ref_it_returns(caplog):
    exc = RuntimeError("collection kb_acme missing at http://qdrant:6333")
    with caplog.at_level(logging.WARNING, logger="mcp"):
        out = main._internal_error("Wiki tree failed", exc)
    ref = _assert_generic(out)
    assert out.startswith("Wiki tree failed")
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert ref in logged and "kb_acme" in logged


def test_a_4xx_stays_specific():
    out = main._internal_error("Wiki edit failed",
                               main.UpstreamError(404, "no page AI/x.md"))
    assert out == "Wiki edit failed (404): no page AI/x.md"
    assert "ref" not in out


def test_an_upstream_5xx_detail_is_masked():
    exc = main.UpstreamError(500, "QdrantException: kb_acme at http://qdrant:6333")
    out = main._internal_error("Search failed", exc)
    _assert_generic(out)
    assert "(500)" in out


def test_http_err_detail_masks_a_5xx_json_detail():
    resp = httpx.Response(500, json={"detail": "psycopg: kb_acme at http://qdrant:6333"},
                          request=httpx.Request("POST", f"{EMBEDDER}/x"))
    exc = httpx.HTTPStatusError("boom", request=resp.request, response=resp)
    _assert_generic(main._http_err_detail(exc))


def test_http_err_detail_keeps_a_4xx_detail():
    resp = httpx.Response(422, json={"detail": "heading not found"},
                          request=httpx.Request("POST", f"{EMBEDDER}/x"))
    exc = httpx.HTTPStatusError("bad", request=resp.request, response=resp)
    assert main._http_err_detail(exc) == "heading not found"


@respx.mock
async def test_a_tool_connect_error_is_generic(fake_ctx):
    respx.delete(url__regex=rf"{EMBEDDER}/api/memories/.*").mock(
        side_effect=httpx.ConnectError("cannot reach http://embedder:8001 (kb_acme)"))
    out = await main.memory_delete("abc", ctx=fake_ctx(token_info=_RW))
    _assert_generic(out)


@respx.mock
async def test_a_board_5xx_body_is_not_relayed(fake_ctx):
    respx.get(url__regex=rf"{MEMORY_API}/board/cards/.*").mock(
        return_value=httpx.Response(500, text="Traceback ... http://qdrant:6333 kb_acme"))
    out = await main.card_get("x", ctx=fake_ctx(token_info=_RW))
    _assert_generic(out)


@respx.mock
async def test_a_board_4xx_detail_is_relayed(fake_ctx):
    respx.post(url__regex=rf"{MEMORY_API}/board/cards").mock(
        return_value=httpx.Response(409, json={"detail": "slug `x` is held by #12"}))
    out = await main.card_add("x", ctx=fake_ctx(token_info=_RW))
    assert "slug `x` is held by #12" in out


@respx.mock
async def test_an_uncaught_tool_exception_is_caught_by_the_backstop(fake_ctx):
    """card_get has no handler of its own for a dead memory-api."""
    respx.get(url__regex=rf"{MEMORY_API}/board/cards/.*").mock(
        side_effect=httpx.ConnectError("http://memory-api:8002 refused"))
    out = await main.card_get("x", ctx=fake_ctx(token_info=_RW))
    assert out.startswith("card_get failed")
    _assert_generic(out)


# ── bounded caches ──────────────────────────────────────────────────────────

@pytest.fixture
def clean_state():
    maps = (main._token_cache, main._invalid_token_cache, main._rate_counters,
            main._session_source_overrides, main._session_override_touched)
    for m in maps:
        m.clear()
    yield
    for m in maps:
        m.clear()


def test_make_room_drops_expired_before_live():
    cache = {"old": 1, "live1": 10, "live2": 10}
    main._make_room(cache, 3, lambda v: v < 5)
    assert cache == {"live1": 10, "live2": 10}


def test_make_room_drops_oldest_when_all_live():
    cache = {f"k{i}": 10 for i in range(5)}
    main._make_room(cache, 3, lambda v: False)
    assert list(cache) == ["k3", "k4"]


@respx.mock
async def test_token_cache_is_bounded(monkeypatch, clean_state):
    monkeypatch.setattr(main, "TOKEN_CACHE_MAX", 20)
    respx.post("http://test-editor:8080/api/internal/tokens/validate").mock(
        return_value=httpx.Response(200, json={"valid": True}))
    for i in range(200):
        await main._validate_token(f"t{i}")
    assert len(main._token_cache) <= 20
    assert "t199" in main._token_cache


def test_rate_counters_are_bounded(monkeypatch, clean_state):
    monkeypatch.setattr(main, "RATE_LIMITING_ON", True)
    monkeypatch.setattr(main, "RATE_COUNTERS_MAX", 50)
    monkeypatch.setattr(main, "_RATE_WINDOWS", (
        ("rpm", 60, "rate_limit_rpm", 60, "per-minute"),
    ))
    for i in range(500):
        assert main._check_rate_limit(f"tok{i}", {}) is None
    assert len(main._rate_counters) <= 50
    assert "tok499" in main._rate_counters


def test_rate_counters_evict_stale_entries_first(monkeypatch, clean_state):
    monkeypatch.setattr(main, "RATE_LIMITING_ON", True)
    monkeypatch.setattr(main, "RATE_COUNTERS_MAX", 3)
    monkeypatch.setattr(main, "_RATE_WINDOWS", (
        ("rpm", 60, "rate_limit_rpm", 60, "per-minute"),
    ))
    now = main.time.time()
    main._rate_counters["stale"]["rpm"] = [now - 120]
    main._rate_counters["live1"]["rpm"] = [now]
    main._rate_counters["live2"]["rpm"] = [now]
    main._check_rate_limit("new", {})
    assert set(main._rate_counters) == {"live1", "live2", "new"}


def test_session_overrides_are_bounded(monkeypatch, clean_state):
    monkeypatch.setattr(main, "SESSION_OVERRIDE_MAX", 25)
    for i in range(300):
        main._session_overrides(f"tok:s{i}", create=True)["src"] = False
    assert len(main._session_source_overrides) <= 25
    assert len(main._session_override_touched) <= 25
    assert main._session_overrides("tok:s299") == {"src": False}


def test_session_overrides_expire_when_idle(clean_state):
    main._session_overrides("tok:s", create=True)["src"] = False
    main._session_override_touched["tok:s"] -= main.SESSION_OVERRIDE_TTL + 1
    assert main._session_overrides("tok:s") == {}
    assert "tok:s" not in main._session_source_overrides


def test_reading_overrides_does_not_create_an_entry(clean_state):
    assert main._session_overrides("tok:nobody") == {}
    assert not main._session_source_overrides
