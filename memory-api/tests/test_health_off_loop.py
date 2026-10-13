"""/health must not stall the event loop.

Its Qdrant and Postgres probes are synchronous clients. Called directly from
the `async def` handler they held the loop for as long as either backend took
to answer, and /health is polled constantly — so a slow Qdrant froze every
other request memory-api was serving, the hot-tier promote included (CQ-05).

The probe here blocks for a while; if the handler runs it on the loop, a
trivial coroutine scheduled beside it cannot finish until the probe does.
"""
import asyncio
import time
from unittest.mock import MagicMock

import httpx
import pytest

PROBE_SECONDS = 0.6


@pytest.fixture
def slow_backends(monkeypatch):
    import main

    def slow_collections():
        time.sleep(PROBE_SECONDS)

    qdrant = MagicMock()
    qdrant.get_collections.side_effect = slow_collections
    monkeypatch.setattr(main, "get_qdrant", lambda: qdrant)

    engine = MagicMock()
    conn = MagicMock()
    conn.execute.side_effect = lambda *a, **k: time.sleep(PROBE_SECONDS)
    engine.connect.return_value.__enter__.return_value = conn
    engine.connect.return_value.__exit__.return_value = False
    monkeypatch.setattr(main, "get_engine", lambda: engine)
    return qdrant, conn


async def test_health_probes_do_not_block_the_loop(slow_backends):
    import main

    stalls = []
    done = asyncio.Event()

    async def ticker():
        # The longest gap between 10ms ticks is how long the loop was held.
        last = time.monotonic()
        while not done.is_set():
            await asyncio.sleep(0.01)
            now = time.monotonic()
            stalls.append(now - last)
            last = now

    transport = httpx.ASGITransport(app=main.app)
    tick = asyncio.create_task(ticker())
    await asyncio.sleep(0)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.get("/health")
    done.set()
    await tick

    assert r.status_code == 200
    body = r.json()
    assert body["components"]["qdrant"] == "ok"
    assert body["components"]["postgres"] == "ok"
    worst = max(stalls)
    assert worst < PROBE_SECONDS / 2, (
        f"the event loop stalled for {worst:.2f}s while /health probed — "
        "a probe ran on the loop instead of in the threadpool"
    )


def test_startup_waits_for_postgres_without_blocking(monkeypatch):
    """The Postgres-wait loop sleeps with asyncio, not time.sleep."""
    import inspect
    import main

    src = inspect.getsource(main.startup)
    assert "time.sleep" not in src
    assert "await asyncio.sleep" in src
    assert "asyncio.to_thread(get_engine)" in src
