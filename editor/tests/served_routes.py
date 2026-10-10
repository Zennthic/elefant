"""The routes an app actually serves, with included routers flattened.

Up to FastAPI 0.111, `include_router` copied every route onto the app, so
`app.routes` was the whole route table. Since the 0.14x releases it adds one
lazy entry per included router instead, and a test that walks `app.routes`
sees the app's own handlers and nothing a router serves. Such a test fails
loudly, which is how the upgrade found them, but the next one written against
`app.routes` would read a route as missing.

`iter_route_contexts` is FastAPI's own walk of that tree. Each item carries the
effective `path` (prefix applied), `methods` and `endpoint`.
"""
from fastapi.routing import iter_route_contexts


def served_routes(app):
    return list(iter_route_contexts(app.routes))


def served_paths(app):
    return {r.path for r in served_routes(app) if r.path}
