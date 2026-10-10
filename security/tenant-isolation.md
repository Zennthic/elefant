# Tenant Isolation Architecture

**Classification:** Public Security Documentation  
**Last Updated:** 2026-10-04

Elefant can serve several organisations ("orgs", called tenants in the code) from
one install. This page describes how it keeps them apart: the layer each control
lives in, the threat it addresses, and how to check it. Every control names the
code that implements it.

Elefant is not certified against any framework, and this page is not an
attestation. See [data-management.md](data-management.md#mapping-to-your-own-compliance-programme)
for how an operator can map these controls to their own programme.

---

## Deployment Modes

### One org (the default)

A fresh self-hosted install has one org, `default`. There is no second tenant to
isolate from, so isolation is whatever your own host, VM or cluster provides.
Data stays on that host unless you turn on an external embedding provider,
a reranker or an off-site backup target.

### Several orgs on one install (`ELEFANT_MODE=multi-org`)

Multi-tenancy is enabled by `ELEFANT_MODE=multi-org` (`editor/deployment_mode.py`). All orgs
share **one set of services**: one editor, one embedder, one MCP server, one
memory-api, one PostgreSQL, one Qdrant and one object store. There is no
per-org container or per-org database server. Isolation is enforced by the
layers below, inside those shared services.

---

## Isolation Layers

| Layer | Mechanism | Where |
|---|---|---|
| **Identity** | nginx sends every protected request to the editor's `/api/auth/check` (`auth_request`), and forwards the result as `X-Auth-*` headers, including `X-Auth-Org-Slug`. The check reloads the user from the database on each request, so a role or org change applies at once rather than when the 24-hour session cookie expires. | `nginx/nginx.conf`, `auth_check` in `editor/main.py` |
| **Bearer tokens** | API and OAuth tokens are stored as SHA-256 hashes, each bound to one org. The MCP server validates a token with the editor, caches the answer for 5 seconds, and forwards the token's org as `X-Auth-Org-Slug` on every downstream call. | `editor/config_db.py`, `_validate_token()` in `mcp/main.py` |
| **Editor database** | One database for all orgs (SQLite by default, PostgreSQL when `POSTGRES_EDITOR_DSN` is set; see [data-management.md](data-management.md#where-the-editors-database-lives)). Every org-owned row carries `tenant_id` or `org_id`, and queries filter on the caller's org: for example the recycle bin (`soft_deleted_pages`), wiki history, git sources and SSO providers. | `editor/config_db.py`, `editor/routers/` |
| **PostgreSQL (memory-api)** | `tenant_id TEXT NOT NULL` on every row of `events`, `memory_hot`, `memory_revisions`, `git_files`, `board_columns`, `cards`, `card_links`, `card_comments` and `card_number_seq`. Each table has row-level security **enabled and forced**, with a policy that admits only rows where `tenant_id = current_setting('app.tenant_id', true)`. The slug is set per transaction with a bound parameter, `SELECT set_config('app.tenant_id', :slug, true)`, never interpolated into SQL. | `memory-api/main.py` |
| **Qdrant** | One collection per org for wiki and one for memories: `{slug}_wiki` and `{slug}_memories` (`knowledge_base` and `memories` for the `default` org). Memory and wiki points also carry a `tenant_id` payload, which `_assert_payload_tenant()` checks on every fetch, update and delete by id. | `_wiki_coll()`, `_mem_coll()` in `embedder/main.py` |
| **Embedder tenant resolution** | `_resolve_tenant_slug()` treats `X-Auth-Org-Slug` as the caller's org. A user request that names a different org in its body or query is refused with 403. Only internal-service callers may name an arbitrary org. | `embedder/main.py` |
| **Object storage** | One bucket per org wiki, `{prefix}{org_slug}--{wiki}`. Wiki pages and card attachments are read and written only in the bucket of the caller's org. | `editor/storage.py`, `_active_org()` in `editor/wiki_storage.py` |
| **Filesystem** | The embedder's wiki mirror is `/docs/{slug}/` (`/docs` for `default`). Path inputs are normalised and refused if they are absolute or contain `..`, and resolved paths must stay inside `DOCS_DIR`. Git clones live under `/git-sources/{org_slug}/{source_id}/`. | `_normalize_path()` in `editor/wiki_storage.py`, `embedder/main.py` |
| **Service-to-service** | memory-api is internal only. Every endpoint except `/health` requires the `X-Internal-Service` header, compared in constant time against `INTERNAL_SERVICE_TOKEN`. Without a configured token the services fail closed. | `editor/internal_service.py`, `memory-api/main.py` |
| **OAuth** | PKCE with S256 is mandatory. A code is redeemed once, atomically. `redirect_uri` must match exactly and use `https`, `http` on loopback, or a native-app custom scheme; `javascript:`, `data:`, `vbscript:`, `file:`, `blob:` and `about:` are refused. | `editor/routers/oauth.py` |
| **CSRF** | Session cookies are `SameSite=Lax`, and middleware checks `Origin` (or `Referer`) on state-changing requests that carry a cookie. | `editor/auth.py`, `editor/main.py` |

### Row-level security

The policy memory-api creates on each tenant table:

```sql
ALTER TABLE events ENABLE ROW LEVEL SECURITY;
ALTER TABLE events FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation ON events
    USING (tenant_id = current_setting('app.tenant_id', true));
```

`FORCE` makes the policy apply to the table's owner as well. It does **not**
apply to a PostgreSQL superuser, or to any role with `BYPASSRLS`. The bundled
Docker Compose and Kubernetes setups connect as `memory`, the role the
`postgres` image creates from `POSTGRES_USER`, which is a superuser. In that
setup row-level security is not a backstop: isolation rests on the application
setting the right slug. For several orgs on one install, connect memory-api as
a dedicated role that is neither superuser nor `BYPASSRLS`:

```sql
CREATE ROLE elefant_app LOGIN PASSWORD '...' NOSUPERUSER NOBYPASSRLS;
```

---

## Data Paths

### Wiki

```
PUT /api/wiki/...  →  editor resolves the caller's org from the session
                   →  write to that org's S3 bucket
                   →  embedder reindexes into the org's wiki collection
```

Search reads only the collection for the caller's org.

### Memories

```
MCP memory tool  →  MCP server: org comes from the validated token
                 →  embedder / memory-api, with X-Auth-Org-Slug or an
                    internal-service call naming that org
                 →  Qdrant collection for the org, or PostgreSQL with
                    app.tenant_id set to the org
```

The org is never taken from the tool's arguments.

### Board

The board UI calls the editor, which takes the org from the session
(`_effective_tenant_slug()` in `editor/auth_helpers.py`) and forwards to
memory-api with that slug. The MCP board tools take the org from the token
alone. memory-api sets `app.tenant_id` before touching any board table, so
columns, cards, links and comments are scoped by row-level security as well as
by the query.

Card attachments are stored in the org's own bucket under
`cards/{card-slug}/`. memory-api accepts only attachment keys of that shape,
and the editor serves them from the caller's org bucket as downloads, never
inline.

### Git sources

An admin adds a source to their org (`git_sources.org_id`). Lookups are scoped
to the caller's org, so another org's source id returns 404
(`_git_source_or_404()` in `editor/git_sources.py`). Credentials are
Fernet-encrypted. Clones are kept per org, the indexed rows in `git_files`
carry `tenant_id` under row-level security, and read access within an org can
be narrowed further with `git_source_acls`.

### Recycle bin

`soft_deleted_pages.tenant_id` is set from the session's org on delete, and
every recycle-bin query filters on it. A restore naming another org's entry
matches no row and returns 404.

---

## How to Check Each Control

| Control | Where to look | How |
|---|---|---|
| Row-level security enabled and forced | PostgreSQL `pg_class`, `pg_policies` | `SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname IN ('events','cards',...)`; `SELECT * FROM pg_policies` |
| Connecting role cannot bypass it | `pg_roles` | `SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = '<your role>'` must return `f, f` |
| `tenant_id` not nullable | `information_schema.columns` | `is_nullable` is `NO` for `tenant_id` on each table above |
| Collection per org | Qdrant `GET /collections` | One `{slug}_wiki` and `{slug}_memories` per org |
| Bucket per org | Your object store | One `{prefix}{slug}--default` bucket per org |
| Recycle-bin scoping | `editor/routers/wiki.py` | Recycle-bin queries filter on `tenant_id` |
| Token to org binding | `editor/auth_helpers.py`, `mcp/main.py` | Follow the token through validation to `X-Auth-Org-Slug` |
| Embedder refuses a foreign org | `_resolve_tenant_slug()` in `embedder/main.py` | A user request naming another org gets 403 |

---

## Threat Model

| Threat | Mitigations |
|---|---|
| One org's token reads another org's memories | The org comes from the token, never from arguments; per-org collections; payload `tenant_id` check on fetch by id; row-level security on the PostgreSQL tables |
| One org's user names another org in a request | The embedder refuses a slug that disagrees with `X-Auth-Org-Slug`; editor routes resolve the org from the session |
| An application bug routes a query to the wrong org | Row-level security in the database, provided the connecting role is not a superuser and has no `BYPASSRLS` (see above) |
| A board call reaches another org's cards | Org taken from the session or token; row-level security on every board table; attachment keys confined to the card's prefix in the caller's bucket |
| An admin probes another org's git sources | Source lookups are scoped to the caller's org and return 404 otherwise |
| An admin restores another org's deleted page | Recycle-bin queries filter on the session's org; a foreign entry returns 404 |
| A path escapes the docs tree | Absolute paths and `..` are refused, and resolved paths must stay inside `DOCS_DIR` |
| A user changes role or org but keeps an old cookie | `auth_check` reloads the user on every request |
| A revoked token keeps working | The MCP server's validation cache lives 5 seconds |

---

## Known Limitations and Future Work

1. **Row-level security and the bundled database role.** The shipped setups
   connect as a superuser, which row-level security does not restrict. Use a
   least-privilege role for multi-org installs, as above.

2. **Qdrant has no row-level security.** Isolation there rests on collection
   naming in the application. Fetch, update and delete by id also check the
   point's `tenant_id` payload. Bulk search and scroll rely on the collection
   name alone; adding a payload filter to them is planned.

3. **One shared internal-service token.** `INTERNAL_SERVICE_TOKEN` is shared by
   the editor, embedder, MCP server and memory-api, so a compromised service
   can act as any other. Per-service credentials are planned.

4. **Service log filtering.** The admin log view shows lines tagged
   `[tenant=…]` for the caller's org. Lines with no tenant tag (startup
   banners, Qdrant errors) are shown to every org admin. A default-deny filter
   is planned.

5. **Org purge leaves board rows.** The org purge does not yet delete the
   org's rows from `board_columns`, `cards`, `card_links`, `card_comments` and
   `card_number_seq`. Remove them by `tenant_id` after a purge if you need them
   gone. See [data-management.md](data-management.md#org-purge-multi-org-installs).
