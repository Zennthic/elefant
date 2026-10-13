"""The board's live-update stream: the hub, the SSE framing, and the rule that
every board mutation announces itself.

The last of those is the one worth having. The hub and the framing are small
enough to read; what is easy to get wrong is adding a ninth way to change the
board and forgetting to tell anyone, which fails as a board that silently stops
being live for that one operation — exactly the bug this whole feature exists to
fix, reintroduced one endpoint at a time.
"""
import asyncio
import inspect
import json

import pytest

import main


# ── The hub ───────────────────────────────────────────────────────────────────

def test_subscribe_and_unsubscribe_track_the_count():
    hub = main._BoardHub()
    assert hub.subscriber_count("acme") == 0
    q1 = hub.subscribe("acme")
    q2 = hub.subscribe("acme")
    assert hub.subscriber_count("acme") == 2
    assert hub.subscriber_count() == 2
    hub.unsubscribe("acme", q1)
    assert hub.subscriber_count("acme") == 1
    hub.unsubscribe("acme", q2)
    assert hub.subscriber_count("acme") == 0
    # The tenant's entry goes with its last subscriber; a board nobody is
    # watching should not cost a dictionary key for the life of the process.
    assert "acme" not in hub._subs


def test_unsubscribing_twice_is_harmless():
    """A stream can unwind through its `finally` after the hub has already
    forgotten it (shutdown, a cancelled request). That must not raise into
    the response cycle."""
    hub = main._BoardHub()
    q = hub.subscribe("acme")
    hub.unsubscribe("acme", q)
    hub.unsubscribe("acme", q)
    hub.unsubscribe("nobody", q)


def test_publish_without_a_loop_is_a_no_op():
    """Publishing before startup binds the loop (or after shutdown) must not
    raise — a board write is not allowed to fail because nobody is listening."""
    hub = main._BoardHub()
    hub.subscribe("acme")
    hub.publish("acme", "card.added", slug="x")     # no loop bound


async def test_publish_reaches_only_that_tenants_subscribers():
    """Tenant isolation on the stream. The events carry no board content, but
    they do carry card slugs, and a slug is somebody else's data."""
    hub = main._BoardHub()
    hub.bind_loop(asyncio.get_running_loop())
    mine = hub.subscribe("acme")
    theirs = hub.subscribe("other-org")

    hub.publish("acme", "card.added", slug="secret-project")
    await asyncio.sleep(0)          # let the call_soon_threadsafe callback run

    got = mine.get_nowait()
    assert got["type"] == "card.added"
    assert got["slug"] == "secret-project"
    assert got["tenant"] == "acme"
    assert got["at"]
    assert theirs.empty(), "a board event reached a different tenant's stream"


async def test_publish_to_nobody_is_a_no_op():
    hub = main._BoardHub()
    hub.bind_loop(asyncio.get_running_loop())
    hub.publish("acme", "card.added", slug="x")
    await asyncio.sleep(0)


async def test_a_full_queue_drops_the_hint_instead_of_raising():
    """A client too far behind to keep up is about to re-read the whole board
    anyway, so the newest hint is no more valuable than the 64 already queued.
    Dropping it is correct; raising would fail the write that provoked it."""
    hub = main._BoardHub()
    hub.bind_loop(asyncio.get_running_loop())
    q = hub.subscribe("acme")
    for _ in range(main.BOARD_STREAM_QUEUE_MAX):
        q.put_nowait({"type": "filler"})
    assert q.full()

    hub.publish("acme", "card.added", slug="x")
    await asyncio.sleep(0)
    assert q.qsize() == main.BOARD_STREAM_QUEUE_MAX


def test_board_changed_never_raises_into_the_caller(monkeypatch):
    """It is called after the commit. A broken notification must not turn a
    write that already succeeded into a 500 the caller will retry."""
    def boom(*_a, **_kw):
        raise RuntimeError("hub exploded")
    monkeypatch.setattr(main._board_hub, "publish", boom)
    main._board_changed("acme", "card.added", slug="x")     # must not raise


# ── SSE framing ───────────────────────────────────────────────────────────────

def test_sse_pack_frames_an_event_and_terminates_it():
    out = main._sse_pack({"type": "card.moved", "slug": "abc"})
    assert out.startswith("event: card.moved\n")
    assert out.endswith("\n\n"), "an SSE frame that does not end in a blank line never dispatches"
    data = [ln for ln in out.splitlines() if ln.startswith("data: ")]
    assert len(data) == 1
    assert json.loads(data[0][len("data: "):])["slug"] == "abc"


def test_sse_pack_data_is_a_single_line():
    """A newline inside `data:` would split one event into two frames, and the
    second would be a frame with no event name. json.dumps escapes them, which
    this pins down rather than leaves to chance."""
    out = main._sse_pack({"type": "card.updated", "slug": "a\nb"})
    assert out.count("\n\n") == 1
    assert len([ln for ln in out.splitlines() if ln.startswith("data: ")]) == 1


def test_stream_response_is_unbuffered_and_uncached():
    """These headers are the difference between a live stream and a batched
    one. nginx honours X-Accel-Buffering per response, so the stream stays
    unbuffered even if a location block is ever written without
    `proxy_buffering off`."""
    src = inspect.getsource(main.board_stream)
    assert "text/event-stream" in src
    assert "no-cache, no-transform" in src
    assert "X-Accel-Buffering" in src and '"no"' in src


def test_the_stream_sends_a_keepalive_rather_than_sitting_silent():
    src = inspect.getsource(main.board_stream)
    assert ": keepalive" in src, (
        "a silent SSE connection is closed by any idle proxy hop, and the "
        "client cannot tell that from a board where nothing is happening")
    assert main.BOARD_STREAM_KEEPALIVE_SECS < 60, (
        "the keepalive has to be comfortably under a typical proxy idle timeout")


def test_the_stream_always_unsubscribes():
    src = inspect.getsource(main.board_stream)
    assert "finally:" in src and "unsubscribe" in src, (
        "a stream that does not unsubscribe leaks a queue per disconnect, and "
        "the hub then fans out to subscribers that will never read again")


# ── The endpoint, driven for real ─────────────────────────────────────────────
# The checks above read source. These run the actual handler and pull frames out
# of its response, because the parts most likely to break are the ones source
# cannot show: that the generator yields hello BEFORE it blocks waiting for an
# event, that a published change comes out in the right frame, and that closing
# the response releases the subscription.
#
# Driven through the response's own body iterator rather than an HTTP client:
# httpx's ASGITransport runs the app to completion and buffers the body, so it
# can never read a stream that by design does not end.

_STREAM_READ_TIMEOUT = 5.0          # a wrong answer here hangs; fail instead


async def _next_frame(body_iter) -> str:
    chunk = await asyncio.wait_for(body_iter.__anext__(), timeout=_STREAM_READ_TIMEOUT)
    return chunk.decode() if isinstance(chunk, bytes) else chunk


async def test_the_stream_says_hello_and_then_relays_a_change():
    main._board_hub.bind_loop(asyncio.get_running_loop())
    before = main._board_hub.subscriber_count("acme")

    resp = await main.board_stream(tenant_slug="acme")
    assert resp.media_type == "text/event-stream"
    assert resp.headers["x-accel-buffering"] == "no"
    assert "no-cache" in resp.headers["cache-control"]

    body = resp.body_iterator
    try:
        # Hello has to arrive without anything having happened on the board.
        # If the generator blocked on the queue first, the client would sit
        # there with no evidence the stream works and keep polling fast.
        hello = await _next_frame(body)
        assert hello.startswith("event: hello\n")
        assert hello.endswith("\n\n")

        # Subscribed only once the generator has started — which is exactly why
        # the hello frame is what tells the client it is safe to slow the poll.
        assert main._board_hub.subscriber_count("acme") == before + 1

        main._board_changed("acme", "card.moved", slug="a-card")
        frame = await _next_frame(body)
        assert frame.startswith("event: card.moved\n")
        payload = json.loads(frame.split("data: ", 1)[1].strip())
        assert payload["slug"] == "a-card"
        assert payload["tenant"] == "acme"
    finally:
        await body.aclose()

    # Closing the response has to release the subscription, or every disconnect
    # leaks a queue and the hub fans out to readers that will never read again.
    assert main._board_hub.subscriber_count("acme") == before


async def test_a_change_to_another_tenant_does_not_appear_on_this_stream():
    main._board_hub.bind_loop(asyncio.get_running_loop())
    resp = await main.board_stream(tenant_slug="acme")
    body = resp.body_iterator
    try:
        assert (await _next_frame(body)).startswith("event: hello")
        main._board_changed("other-org", "card.added", slug="not-yours")
        with pytest.raises(asyncio.TimeoutError):
            # Nothing for this tenant, so the only thing due is the keepalive,
            # which is far enough out that a timeout here means silence.
            await asyncio.wait_for(body.__anext__(), timeout=0.2)
    finally:
        await body.aclose()


async def test_an_unauthenticated_stream_is_refused():
    """memory-api is internal-only, and the stream is a new route into it. The
    middleware rejects before the handler runs, so this one CAN go through the
    ASGI transport — there is no endless body to buffer."""
    import httpx

    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
        resp = await asyncio.wait_for(
            client.get("/board/stream", params={"tenant_slug": "acme"}),
            timeout=_STREAM_READ_TIMEOUT)
        assert resp.status_code == 401


# ── The rule: every board mutation announces itself ───────────────────────────

# Handler → the event type it must announce. Adding a board mutation means
# adding it here, which is the point: the failure this catches is a new endpoint
# that changes the board and tells nobody, leaving an open board stale for that
# one operation with nothing anywhere reporting a problem.
MUTATIONS = {
    "card_add":        "card.added",
    "card_move":       "card.moved",
    "card_update":     "card.updated",
    "card_archive":    "card.archived",
    "card_unarchive":  "card.unarchived",
    "card_link":       "card.linked",
    # All three announce the same event. A client's useful response to any of
    # them is to re-read the thread, so splitting them into added/edited/
    # deleted would be three names for one instruction.
    "card_comment_add":    "card.commented",
    "card_comment_update": "card.commented",
    "card_comment_delete": "card.commented",
    # The evaluation lock. Acquire can also publish `card.eval_lock_broken`,
    # which is a second event on top of this one rather than an alternative to
    # it -- a break is still an acquire.
    "card_eval_lock":   "card.eval_locked",
    "card_eval_unlock": "card.eval_unlocked",
    "column_add":      "column.added",
    "column_update":   "column.updated",
    "column_archive":  "column.archived",
}


@pytest.mark.parametrize("handler,event", sorted(MUTATIONS.items()))
def test_every_mutation_announces_its_change(handler, event):
    src = inspect.getsource(getattr(main, handler))
    assert "_board_changed(" in src, f"{handler} changes the board without announcing it"
    assert f'"{event}"' in src, f"{handler} should announce {event}"


@pytest.mark.parametrize("handler", sorted(MUTATIONS))
def test_the_announcement_follows_the_commit(handler):
    """Announced before the commit, a client can re-read the board and see the
    state it is being told has changed — a race that reads as an event that did
    nothing, and is unreproducible by hand."""
    src = inspect.getsource(getattr(main, handler))
    assert src.index("conn.commit()") < src.index("_board_changed("), (
        f"{handler} announces its change before committing it")


def test_the_mutation_list_is_the_whole_board_api():
    """The map above is only a guard if it is complete. Every route that
    changes the board — anything not a GET under /board — has to be in it, so
    the next mutation added cannot pass by being left off the list."""
    changing = set()
    for route in main.app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", set()) or set()
        if not path.startswith("/board"):
            continue
        if methods <= {"GET", "HEAD", "OPTIONS"}:
            continue
        changing.add(route.name)
    assert changing == set(MUTATIONS), (
        f"board mutations not covered by the stream test: "
        f"{sorted(changing - set(MUTATIONS))}; stale entries: "
        f"{sorted(set(MUTATIONS) - changing)}")
