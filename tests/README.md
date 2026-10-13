# Elefant test suite

Three layers, all run automatically in CI (`.github/workflows/build-deploy.yml`):

## 1. Per-service unit + integration tests

Each Python service has its own `tests/` directory:

```
editor/tests/         # OAuth helpers, path normalisation, AI/-folder check
embedder/tests/       # _resolve_tenant_slug matrix, payload-tenant verify, /api/memories TestClient
memory-api/tests/     # internal-service auth middleware
mcp/tests/            # _auth_headers, _override_key, helpers
```

Run a single service:

```bash
python -m venv /tmp/venv && source /tmp/venv/bin/activate
pip install -r <service>/requirements.txt -r requirements-dev.txt
pytest <service>/tests/ -v
```

The per-service `conftest.py` sets the env vars `<service>/main.py` reads
at import time (DOCS_DIR, INTERNAL_SERVICE_TOKEN, etc.) so the module
imports cleanly without a deployed stack.

**Cross-service collision warning**: each service's `main.py` collides
on the Python module name `main`.  Always run pytest per-service, never
across multiple services in one `pytest` invocation.

## 2. Cross-service property tests

`embedder/tests/test_property_isolation.py` uses Hypothesis to fuzz the
tenant-isolation contract on `_resolve_tenant_slug`.  Catches the
variants we didn't think of in the hand-written matrix.

## 3. Smoke tests (post-deploy)

`tests/smoke.py` runs against a live deploy (set via `BASE_URL`). CI runs it
after every successful `deploy` job; locally you can hit any deployment with
`BASE_URL=...`.

```bash
BASE_URL=https://elefant.example.com \
SMOKE_SESSION_COOKIE=<paste-elefant_session-value> \
    pytest tests/smoke.py -m smoke -v
```

The anonymous endpoints (`/health`, `/.well-known/...`) run with no
cookie.  The cookie-bearing tests verify the cross-service auth chain
(editor → memory-api with `INTERNAL_SERVICE_TOKEN`).  Use a dedicated
service-account cookie that
gets rotated regularly — don't paste a real admin session into CI
secrets.

Without a cookie the authenticated tests skip, which exits 0 and reads as
green. Setting `SMOKE_REQUIRE_AUTH=1` makes `test_authenticated_smoke_is_armed`
fail the run instead. CI does **not** set it at the moment: a session cookie
expires after 24 hours, so requiring one halted every deploy. How CI's
authenticated smoke should sign in is still open. Whenever a cookie is set,
the same test checks the deployment still accepts it, so an expired cookie
fails with one clear message instead of a spread of 401s. Leave the secret
unset rather than let an old cookie sit in it.

## Adding a test

- **Endpoint changes auth/tenant resolution**: add to
  `embedder/tests/test_api_memories_auth.py` (the matrix file).
- **New helper in a service main.py**: add a `test_<helper>.py` in that
  service's tests/ dir.
- **New cross-service invariant**: add to
  `embedder/tests/test_property_isolation.py`.
- **New endpoint that the UI hits post-deploy**: add to `tests/smoke.py`
  so a missing env var or broken proxy is caught at deploy time.
