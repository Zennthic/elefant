"""The board's live stream, from the editor out: the relay route, the nginx hop
that carries it, and the client that listens to it.

Three services have to agree for a board to update live, and each of them looks
correct on its own when it disagrees with the others. nginx buffering a stream
is a working proxy. A client listening for event names the server never sends is
a working EventSource. The board just stops being live, with nothing logged
anywhere. So the checks that matter here are the cross-service ones.
"""
import inspect
import re
from pathlib import Path

import pytest

import main
from served_routes import served_paths
from routers import boards

_ROOT = Path(main.__file__).resolve().parents[1]
_NGINX_CONF = _ROOT / "nginx" / "nginx.conf"
_BOARDS_JS = _ROOT / "memory-ui" / "static" / "boards.js"
_MEMORY_API = _ROOT / "memory-api" / "main.py"


def _location_blocks(text: str):
    """Yield (matcher, body) for each `location ... { ... }`, brace-aware.

    Anchored to the start of a line so the word "location" inside a comment
    cannot start a match and swallow the real directive below it.
    """
    for m in re.finditer(r"^[ \t]*location\s+([^\{\n]+)\{", text, re.MULTILINE):
        i, depth = m.end(), 1
        while i < len(text) and depth:
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
            i += 1
        yield m.group(1).strip(), text[m.end():i - 1]


# ── The editor's relay route ──────────────────────────────────────────────────

def test_the_stream_route_is_registered():
    paths = served_paths(main.app)
    assert "/api/board/stream" in paths


def test_the_stream_route_is_async():
    """A sync handler would park a threadpool worker for the whole life of each
    connection. These are long-lived and almost entirely idle, so a handful of
    open tabs would starve the pool every other editor endpoint shares."""
    assert inspect.iscoroutinefunction(boards.board_stream), (
        "/api/board/stream must be async or open boards will exhaust the "
        "threadpool the rest of the editor runs on")


def test_the_stream_does_not_hold_a_request_scoped_db_session():
    """A dependency-injected Session lives until the RESPONSE completes, and an
    SSE response completes when the user closes the tab. That would pin one
    database connection per open board, for hours."""
    params = inspect.signature(boards.board_stream).parameters
    for name, p in params.items():
        assert "Session" not in str(p.annotation), (
            f"board_stream takes {name}: a request-scoped session would stay "
            f"open for the life of the stream")
    src = inspect.getsource(boards._board_stream_tenant)
    assert "gen.close()" in src, "the tenant lookup must close its own session"


def test_the_stream_resolves_its_tenant_from_the_session():
    """The whole reason the stream comes through the editor rather than going
    straight to memory-api. A stream that took the tenant from the caller would
    let any logged-in user watch any org's board."""
    src = inspect.getsource(boards.board_stream)
    assert "_board_stream_tenant" in src
    assert "_board_stream_caller" in src
    # A caller with a session cookie, or with no credentials at all, still
    # goes through the member gate every other board route uses.
    assert "require_authenticated" in inspect.getsource(boards._board_stream_caller)
    assert inspect.getsource(boards._board_stream_tenant).count("_effective_tenant_slug") == 1


def test_the_relay_reports_upstream_failure_on_the_stream():
    """Once the response has begun there is no status code left to send. An
    EventSource that sees a clean close simply reconnects, forever, with no
    idea why — so the failure has to be said on the stream itself."""
    src = inspect.getsource(boards.board_stream)
    assert "stream.error" in src


def test_the_relay_response_is_unbuffered():
    src = inspect.getsource(boards.board_stream)
    assert "text/event-stream" in src
    assert "no-cache, no-transform" in src
    assert "X-Accel-Buffering" in src


# ── The nginx hop ─────────────────────────────────────────────────────────────

def _stream_location():
    conf = _NGINX_CONF.read_text()
    for matcher, body in _location_blocks(conf):
        if matcher.replace(" ", "") in ("=/api/board/stream", "/api/board/stream"):
            return matcher, body
    pytest.fail("nginx has no location for /api/board/stream")


def test_nginx_does_not_buffer_the_stream():
    """The default proxy buffering holds each event until a buffer fills. The
    stream still works, the board still updates — in batches, minutes apart.
    That failure looks like nothing at all in a log."""
    _, body = _stream_location()
    assert re.search(r"proxy_buffering\s+off", body), (
        "a buffered SSE hop turns a live board into a board that updates in "
        "batches, with nothing anywhere reporting a problem")


def test_nginx_keeps_the_stream_connection_open():
    _, body = _stream_location()
    assert re.search(r"proxy_http_version\s+1\.1", body)
    m = re.search(r"proxy_read_timeout\s+(\d+)s", body)
    assert m and int(m.group(1)) >= 600, (
        "a short read timeout cuts an idle stream off mid-connection")


def test_nginx_authenticates_the_stream_like_the_rest_of_the_board_api():
    _, body = _stream_location()
    assert "auth_request" in body and "/api/auth/check" in body, (
        "the stream must sit behind the same session gate as every other "
        "board route")


def test_nginx_answers_an_unauthenticated_stream_with_401_not_a_redirect():
    """EventSource follows redirects. Pointed at the login page it would hand
    the board an HTML document as if it were a stream, and never fire the error
    the polling fallback listens for."""
    _, body = _stream_location()
    assert "@bearer_401" in body


def test_nginx_answers_a_token_without_board_read_with_json_403():
    """An API token that is valid but lacks board_read gets a 403 from the
    gate. Without a named handler nginx would answer with its HTML error page."""
    _, body = _stream_location()
    assert "@bearer_403" in body
    conf = _NGINX_CONF.read_text()
    assert any(m.strip() == "@bearer_403" for m, _ in _location_blocks(conf))


def test_the_stream_location_wins_over_the_board_prefix():
    """`location /api/board/` would otherwise swallow the stream and proxy it
    buffered. An exact match beats a prefix match regardless of file order, so
    the two do not have to be kept in a particular sequence."""
    matcher, _ = _stream_location()
    assert matcher.startswith("="), (
        "the stream location must be an exact match (`location = /api/board/"
        "stream`) or the /api/board/ prefix block can take it")


# ── Cross-service: does the client listen for what the server sends? ──────────

def _published_event_types() -> set:
    """Every event type memory-api announces, read from its own source."""
    src = _MEMORY_API.read_text()
    return set(re.findall(r'_board_changed\(\s*\w+\s*,\s*"([\w.]+)"', src))


def _client_event_types() -> set:
    """Every event type boards.js subscribes to, read from its own source."""
    src = _BOARDS_JS.read_text()
    block = src.split("].forEach(name =>", 1)
    assert len(block) == 2, "boards.js no longer registers its stream listeners as a list"
    listeners = block[0].rsplit("[", 1)[1]
    return set(re.findall(r"'([\w.]+)'", listeners))


def test_the_board_listens_for_every_event_the_server_sends():
    """The drift that this catches is silent in both directions and in both
    services. A named SSE event never fires `message`, so an event the client
    does not name is simply not heard: that one operation stops updating the
    board live, and nothing errors. The reverse — a listener for an event that
    was renamed away — is dead code that looks like coverage.
    """
    if not _BOARDS_JS.exists() or not _MEMORY_API.exists():
        pytest.skip("memory-ui / memory-api are not checked out beside the editor")
    published, heard = _published_event_types(), _client_event_types()
    assert published, "no _board_changed() calls found in memory-api"
    assert published == heard, (
        f"the board does not listen for {sorted(published - heard)} "
        f"(those changes will not appear until the next poll); "
        f"it listens for {sorted(heard - published)} which nothing sends")


def test_the_board_treats_hello_as_proof_the_stream_works():
    """An EventSource object exists the instant it is constructed, long before
    anything proves the chain delivers. Slowing the poll on that would trust a
    connection that may never carry a byte — through, say, a proxy hop that
    buffers it."""
    src = _BOARDS_JS.read_text()
    assert "addEventListener('hello'" in src


def test_the_board_keeps_polling_as_a_fallback():
    """A stream can fail in ways it cannot report: a buffering middlebox, a
    sleeping laptop, a session that expired. Every one of them looks like a
    board that stopped updating, so the poll has to survive the stream."""
    src = _BOARDS_JS.read_text()
    assert "POLL_FAST_MS" in src and "POLL_SAFETY_MS" in src
    fast = int(re.search(r"POLL_FAST_MS\s*=\s*(\d+)", src).group(1))
    safety = int(re.search(r"POLL_SAFETY_MS\s*=\s*(\d+)", src).group(1))
    assert 0 < fast < safety, (
        "the poll must beat faster when there is no stream than when there is")


def test_landing_straight_on_the_board_starts_the_live_updates():
    """The original bug. boards.js is deferred, so the SPA's tab restore runs
    before window.Boards exists and skips BOTH init() and the call that starts
    the live updates. init() was already self-served here; the live updates were
    not, so a board opened by its own URL loaded once and then never changed —
    the "needs a hard refresh" this whole feature is here to fix.
    """
    src = _BOARDS_JS.read_text()
    tail = src.split("Landing straight on the Board tab", 1)
    assert len(tail) == 2, "boards.js no longer has the self-init block"
    assert "startLive()" in tail[1], (
        "the self-init path must start the live updates, not only load once")


def test_a_deferred_refresh_is_not_dropped():
    """Refreshing is held back during a drag, behind an open modal and while the
    tab is hidden. Under polling that only delayed it to the next tick; on a
    pushed stream the push has already happened, so a dropped refresh is a board
    that stays stale until something else changes."""
    src = _BOARDS_JS.read_text()
    assert "_deferredRefresh" in src and "flushDeferredRefresh" in src
    # Every path that closes a modal has to flush, including the two that
    # bypass closeModal() (clicking the backdrop, and Escape).
    assert src.count("flushDeferredRefresh()") >= 4
