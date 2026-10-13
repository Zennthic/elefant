# Elefant Architecture — One Codebase, One Org or Many

> Architecture reference for how Elefant serves one organisation or many, and
> enforces tenant isolation, from a single codebase. Indexed by the embedder, so it can be
> retrieved via MCP memory/wiki search.

---

## One codebase, every feature on

Every install has every feature: unlimited users, MCP OAuth, OIDC and SAML,
git sources, and groups and teams. There are no tiers, no licence and no
feature gates. One image set serves two deployment shapes, picked at runtime by
`ELEFANT_MODE` (`editor/deployment_mode.py`):

- `single-org` (default) — one organisation; the first account is its org admin.
- `multi-org` — a platform admin hosts many organisations, each its own tenant
  with its own domains and wiki spaces; the `admin_platform` routes are on.

The legacy `ELEFANT_TIER=saas` still selects `multi-org`, with a deprecation
warning. Other `ELEFANT_TIER` values are ignored.

## Tenant isolation

1. **PostgreSQL `events` table** — carries a `tenant_id` column (idempotent
   ALTER migration) with a Row-Level Security policy (`FORCE ROW LEVEL
   SECURITY`); the app sets `SET LOCAL app.tenant_id` before every transaction.

2. **`SoftDeletedPage.tenant_id`** — the recycle bin is scoped per tenant; every
   recycle-bin route filters by the authenticated session's org.

3. **Per-tenant credentials** — local password auth, OIDC, and SAML are all
   available in every install; each user and IdP configuration is scoped to its org.

4. **Embedder file watcher** — path-based tenant derivation is gated behind
   `MULTI_TENANT_DOCS` (default off for single-tenant installs, so topic
   subdirectories aren't mis-routed as tenant slugs).

5. **Single memory surface** — the legacy `memory-api` `/memory/store` and
   `/memory/search` endpoints return `410 Gone`; all memory traffic routes
   through the embedder's tenant-aware `/api/memories/*` surface.

## Deployment model

`docker-compose.yml` + `.env` on one machine, or the kustomize base in
`deploy/k8s/` on a cluster. Both are documented in
[`docs/install/`](../install/). There is one optional compose overlay,
`docker-compose.traefik.yml`, which adds Traefik routing and nothing else —
the base file is self-contained and assumes no reverse proxy, so that a fresh
`docker compose up -d` works on a host that runs nothing of ours.

The deployment shape is steered entirely by `.env`:
- `ELEFANT_MODE=single-org` (default) — one organisation
- `ELEFANT_MODE=multi-org` — many organisations, run by a platform admin

## Key files

| File | Role |
|---|---|
| `editor/deployment_mode.py` | `ELEFANT_MODE`: single-org or multi-org |
| `editor/main.py` | FastAPI routes |
| `editor/config_db.py` | SQLAlchemy models + migrations |
| `memory-api/main.py` | Events store, RLS |
| `embedder/main.py` | File watcher, memory API, tenant routing |
| `security/tenant-isolation.md` | Isolation layers and threat model |
