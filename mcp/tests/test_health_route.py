"""/health answers without a token, and nothing else gets past auth (card #300).

The Compose healthcheck probes /health, so it has to answer from the router in
front of BearerAuthMiddleware. The same router must not become a way round
auth for any other path.
"""
import json

import anyio

import main


def _call(app, path):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "path": path, "method": "GET", "headers": []}
    anyio.run(app, scope, receive, send)
    status = sent[0]["status"]
    body = b"".join(m.get("body", b"") for m in sent[1:])
    return status, body


def _app():
    async def inner(scope, receive, send):
        raise AssertionError(f"{scope['path']} reached the tool handlers without auth")
    return main._OAuthDiscoveryRouter(main.BearerAuthMiddleware(inner))


def test_health_answers_without_a_token():
    status, body = _call(_app(), "/health")
    assert status == 200
    assert json.loads(body) == {"status": "ok"}


def test_other_paths_still_need_a_bearer():
    for path in ("/mcp", "/mcp/", "/health/x", "/healthz"):
        status, _ = _call(_app(), path)
        assert status == 401, path
