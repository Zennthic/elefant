"""SEC-08: a flood of invalid bearers reaches the editor once per token per TTL.

Before this, `_validate_token` cached only good tokens, so every random bearer
sent to /mcp was an editor DB lookup. The negative cache must hold only a
definite "not valid" from the editor: caching a timeout or a 5xx would turn an
editor blip into every real token being refused for the whole TTL.
"""
import httpx
import pytest
import respx

import main

VALIDATE = "http://test-editor:8080/api/internal/tokens/validate"


@pytest.fixture(autouse=True)
def clean_caches():
    main._token_cache.clear()
    main._invalid_token_cache.clear()
    yield
    main._token_cache.clear()
    main._invalid_token_cache.clear()


@respx.mock
async def test_an_invalid_token_is_checked_once_per_ttl():
    route = respx.post(VALIDATE).mock(return_value=httpx.Response(200, json={"valid": False}))
    for _ in range(50):
        assert await main._validate_token("junk") is None
    assert route.call_count == 1

    # Age the entry past its TTL (patching the clock would stop the event loop's).
    main._invalid_token_cache["junk"] = main.time.monotonic() - 1
    assert await main._validate_token("junk") is None
    assert route.call_count == 2


@respx.mock
async def test_each_distinct_invalid_token_is_checked_once():
    route = respx.post(VALIDATE).mock(return_value=httpx.Response(200, json={"valid": False}))
    for i in range(10):
        for _ in range(5):
            await main._validate_token(f"junk-{i}")
    assert route.call_count == 10


@respx.mock
async def test_an_editor_error_is_not_cached():
    route = respx.post(VALIDATE).mock(side_effect=[
        httpx.Response(503),
        httpx.ConnectError("editor down"),
        httpx.Response(200, json={"valid": True, "user": "ann"}),
    ])
    assert await main._validate_token("real") is None
    assert await main._validate_token("real") is None
    assert (await main._validate_token("real"))["user"] == "ann"
    assert route.call_count == 3


@respx.mock
async def test_a_valid_token_is_unaffected():
    respx.post(VALIDATE).mock(return_value=httpx.Response(200, json={"valid": True}))
    assert await main._validate_token("good") == {"valid": True}
    assert "good" not in main._invalid_token_cache


def test_the_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(main, "INVALID_CACHE_MAX", 100)
    for i in range(1000):
        main._remember_invalid(f"h{i}", 1.0)
    assert len(main._invalid_token_cache) <= 100
    assert "h999" in main._invalid_token_cache


def test_nginx_guards_mcp_before_auth():
    from pathlib import Path
    conf = (Path(__file__).resolve().parents[2] / "nginx" / "nginx.conf").read_text()
    for loc in ("location = /mcp {", "location /mcp/ {"):
        block = conf[conf.index(loc):conf.index("}", conf.index(loc))]
        assert "limit_req          zone=mcp" in block, loc
        assert "client_max_body_size 10m;" in block, loc
