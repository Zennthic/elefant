# Configuration reference

Every environment variable an Elefant service reads, grouped by the service
that reads it. A test (`editor/tests/test_configuration_reference.py`) scans the
code for environment reads and fails when one is missing from this page, so the
list is complete for the code it ships with.

To install, start with an install guide:

- [Docker Compose](install/docker-compose.md#install) has the table of the values
  the stack will not start without.
- [Kubernetes](install/kubernetes.md#2-the-secret) has the same values as one
  Secret.

This page does not repeat those tables. It covers everything else.

## How to read this page

**Two names for one setting.** The deployment files do not always pass your
value to the container under the same name. You write `ELEFANT_SESSION_SECRET`
in `.env` (or in the `elefant-secrets` Secret), and Compose or the manifest gives
it to the editor as `SESSION_SECRET`. The tables list the name the **code**
reads. The "Set it with" column gives the `.env` key, or the ConfigMap or Secret
key on Kubernetes, when it is different.

- **Compose:** set the key in `.env`. A variable that `docker-compose.yml` does
  not pass to the container cannot be set from `.env`. To set one of those, add
  it to the service's `environment:` block in a
  [`docker-compose.override.yml`](https://docs.docker.com/compose/how-tos/multiple-compose-files/merge/).
- **Kubernetes:** override a key of the `elefant-config` ConfigMap with
  `behavior: merge`, as the install guide shows. Set a variable that the base does
  not pass with a strategic-merge patch on the Deployment's `env`.

**Secrets** are marked **(secret)**. Keep them out of version control, out of a
ConfigMap and out of logs.

**Required** means the service refuses to start without the variable, or one
of its main features fails closed. **Optional** variables have a working
default.

**Change needs** says what a new value takes:

- **restart:** every service reads its environment once, at start. A changed
  value has no effect until the container restarts (`docker compose up -d`
  recreates a container whose environment changed; on Kubernetes the pods roll
  when the ConfigMap changes).
- **reindex:** the search index was built under the old value. Restart, then
  run a full wiki reindex (the `wiki_reindex` MCP tool, or `POST /api/reindex`
  on the embedder).
- **care:** a change can strand data. Read the row before you change it.

## Shared by several services

| Variable | Read by | Set it with | Default | Required | Change needs | What it does |
|---|---|---|---|---|---|---|
| `INTERNAL_SERVICE_TOKEN` (secret) | editor, embedder, memory-api, mcp | `ELEFANT_INTERNAL_SERVICE_TOKEN` | none | required | restart, all four services together | Shared secret the containers send to each other in the `X-Internal-Service` header. If it is not set, or if the services have different values, cross-service writes (compactor promotions, org backup and restore, hard purge) fail closed. |
| `EMBEDDER_URL` | editor, memory-api, mcp | | `http://embedder:8001` | optional | restart | Internal URL of the embedder. |
| `MEMORY_API_URL` | editor, embedder, mcp | | `http://memory-api:8002` | optional | restart | Internal URL of memory-api. |
| `EDITOR_URL` | embedder, memory-api, mcp | | `http://editor:8080` | optional | restart | Internal URL of the editor. |
| `QDRANT_HOST` | embedder, memory-api | | `qdrant` | optional | restart | Host name of the Qdrant vector store. |
| `QDRANT_PORT` | embedder, memory-api | | `6333` | optional | restart | Qdrant HTTP port. |
| `COLLECTION_NAME` | embedder, memory-api | | `knowledge_base` | optional | reindex | Qdrant collection that holds the wiki index. Both services must agree. A new name starts an empty collection. |
| `DOCS_DIR` | editor, embedder | | `/docs` | optional | reindex | Directory of the wiki mirror on disk. The editor writes pages there for the embedder to index; S3 holds the real copy. Both services must mount the same volume here. |
| `BACKEND_READINESS_FILE` | editor, embedder | | `/tmp/backend_ready` | optional | restart | The editor touches this file when it has started. The embedder waits for it before it indexes, and keeps its `embedding_state.json` in the same directory. Compose and the manifests point it at a volume both containers share (`/backend_sync/ready`). |
| `ELEFANT_S3_ENDPOINT_URL` | editor, embedder | | empty (AWS S3). Compose and the k8s base set `http://minio:9000` | optional | restart | Object-storage endpoint for wiki content. Leave empty for AWS S3. Set it for MinIO, R2, B2 or Wasabi. |
| `ELEFANT_S3_REGION` | editor, embedder | | `us-east-1` | optional | restart | Object-storage region. |
| `ELEFANT_S3_ACCESS_KEY` (secret) | editor, embedder | | none | required | care | Object-storage access key. With the bundled MinIO it is also MinIO's root user, so a new value must reach MinIO and both services at the same time. See the install guides. |
| `ELEFANT_S3_SECRET_KEY` (secret) | editor, embedder | | none | required | care | Object-storage secret key, at least 8 characters. Same rules as the access key. |
| `ELEFANT_S3_BUCKET_PREFIX` | editor, embedder | | empty | optional | care | Prefix for every bucket name. Each org's wiki lives in `<prefix><org_slug>--default`. On AWS S3 set a deployment-scoped value, because bucket names are global. Changing it on a running install points Elefant at new, empty buckets; move the old buckets first. |

## editor

| Variable | Set it with | Default | Required | Change needs | What it does |
|---|---|---|---|---|---|
| `SESSION_SECRET` (secret) | `ELEFANT_SESSION_SECRET` | the file `/run/secrets/elefant_session_secret`, else none | required | restart | Signs session cookies. Login is refused without it. A new value logs everybody out. |
| `EDITOR_EXTERNAL_URL` | built from `ELEFANT_PUBLIC_DOMAIN` as `https://<domain>/editor` | worked out per request from `X-Forwarded-Host`, `X-Forwarded-Proto` and `EDITOR_PATH_PREFIX` | optional | restart | Public base URL of the editor. Used for OIDC and SAML callback URLs and for the links the editor hands out. |
| `EDITOR_PATH_PREFIX` | not passed by Compose or the k8s base | `/editor` | optional | restart | Path the editor is mounted under. Used only when `EDITOR_EXTERNAL_URL` is empty. |
| `APP_NAME` | `ELEFANT_APP_NAME` | `Elefant` | optional | restart | Product name shown in the UI. |
| `DB_PATH` | | `/data/config.db` | optional | care | SQLite file for the editor's own database (accounts, orgs, tokens, settings). Not used when `POSTGRES_EDITOR_DSN` is set. A new path starts an empty database. |
| `POSTGRES_EDITOR_DSN` (secret) | `.env` key of the same name. The k8s base does not pass it. | empty (use SQLite at `DB_PATH`) | optional | care | SQLAlchemy URL of a Postgres database for the editor, in place of SQLite. It carries a password. Switching does not move existing data. |
| `CRYPTO_KEY_PATH` | | `/data/crypto.key` | optional | care | File holding the key that encrypts the secrets the editor stores: admin settings, MFA secrets, OAuth and IdP client secrets, SAML keys and git-source tokens. Created on first start. If you lose it or point at another file, those secrets can no longer be decrypted. Back it up with the database. |
| `RESTORE_STAGING_DIR` | not passed | `/data/restore-staging` | optional | restart | Where an uploaded org backup waits for the admin to confirm a restore. Swept on a timer. |
| `ELEFANT_MODE` | | `single-org` | optional | restart | Deployment shape: `single-org` or `multi-org`. See [Deployment mode](#deployment-mode). |
| `ELEFANT_TIER` | | empty | optional | restart | Deprecated spelling of `ELEFANT_MODE`, read only when `ELEFANT_MODE` is empty. `saas` means `multi-org`, with a warning in the log. Any other value is ignored. |
| `PLATFORM_ADMIN_USERNAME` | not passed by the k8s base | empty | required for `multi-org` | restart | Break-glass platform administrator for `multi-org` mode. Both this and the password must be set, or the platform admin login is off. |
| `PLATFORM_ADMIN_PASSWORD` (secret) | not passed by the k8s base | empty | required for `multi-org` | restart | Password for `PLATFORM_ADMIN_USERNAME`. |
| `ELEFANT_DOMAIN_VERIFY_HOST` | not passed | `verify.elefant.app` | optional | restart | Bring-your-own-domain verification. An org proves it owns a domain with a CNAME at `elefant-verify.<domain>` that points to `<token>.<this host>`. Elefant compares the string and does not resolve it. |
| `OAUTH_ACCESS_TOKEN_TTL_DAYS` | not passed | `365` | optional | restart | Lifetime of an MCP OAuth access token, in days. There is no refresh grant, so the user must reconnect a client whose token expires. Tokens that already exist keep their expiry. Zero, a negative value or a value that is not a number means 365. |
| `OAUTH_REGISTER_LIMIT` | not passed | `20` | optional | restart | Most OAuth dynamic client registrations (`POST /oauth/register`) one client address may make in `OAUTH_REGISTER_WINDOW`. |
| `OAUTH_REGISTER_WINDOW` | not passed | `3600` | optional | restart | Window for `OAUTH_REGISTER_LIMIT`, in seconds. |
| `LOGIN_THROTTLE_WINDOW` | not passed | `900` | optional | restart | Seconds over which failed logins are counted. |
| `LOGIN_THROTTLE_LOCKOUT` | not passed | `900` | optional | restart | Seconds a throttled key stays locked. |
| `LOGIN_THROTTLE_IP_LIMIT` | not passed | `30` | optional | restart | Failed logins from one IP address, for any account, before that address is locked. |
| `LOGIN_THROTTLE_ACCOUNT_IP_LIMIT` | not passed | `5` | optional | restart | Failed logins for one account from one IP address before that pair is locked. |
| `LOGIN_THROTTLE_ACCOUNT_LIMIT` | not passed | `50` | optional | restart | Failed logins for one account, from any address, before the account is locked. |

The login throttle and the registration limit hold their counters in memory,
per editor process. A value that is not an integer falls back to the default.

## embedder

| Variable | Set it with | Default | Required | Change needs | What it does |
|---|---|---|---|---|---|
| `EMBEDDING_PROVIDER` | Compose pins `local`. On k8s, the `EMBEDDING_PROVIDER` ConfigMap key. | `local` | optional | reindex | Embedding backend: `local`, `openai` or `custom`. **Used only when the editor cannot be reached.** The provider an org admin saves in the admin settings comes first. |
| `EMBEDDING_MODEL` | | `multi-qa-distilbert-cos-v1` | optional | reindex | Embedding model, used under the same rule as `EMBEDDING_PROVIDER`. The `local` provider always runs `multi-qa-distilbert-cos-v1` and logs a warning if you ask for another model. To run another model, use an external provider. |
| `OPENAI_API_KEY` (secret) | not passed by Compose or the k8s base | empty | required for `openai` | restart | API key for the `openai` provider, when the admin settings do not hold one. |
| `EMBED_CPU_THREADS` | not passed | `2` | optional | restart | Threads for the local model. Also the default for `OMP_NUM_THREADS`, `MKL_NUM_THREADS`, `OPENBLAS_NUM_THREADS`, `NUMEXPR_NUM_THREADS` and `VECLIB_MAXIMUM_THREADS`. A value you set for one of those yourself wins. |
| `EMBED_MAX_ATTEMPTS` | not passed | `3` | optional | restart | Attempts at one embedding call when the error looks transient. |
| `EMBED_BACKOFF_BASE_S` | not passed | `0.5` | optional | restart | Base delay between those attempts, in seconds. It doubles each attempt. |
| `EMBEDDER_FORCE_COLLECTION_RECREATE` | not passed | empty | optional | care | When the vector size changes (a new model), the wiki collection is always rebuilt, but the memories collection is kept, and memory search stays broken until it is rebuilt. Set `memories` or `all` and restart to delete the memories collection and start it again empty. **This deletes every stored memory vector.** Unset it again afterwards. |
| `MULTI_TENANT_DOCS` | | empty (off). Compose and the k8s base set `true` | optional | reindex | `true` when `DOCS_DIR` holds one folder per org (`/docs/<org_slug>/...`), which is what the editor writes. Off treats every file as the default org's. |
| `GIT_SOURCES_DIR` | | `/git-sources` | optional | care | Where git knowledge sources are cloned. A new path clones every source again. |
| `GIT_SOURCE_EMBED` | | `false` | optional | reindex | Git sources are always indexed lexically (in Postgres). `true` also embeds their files as vectors. That costs time and space, and is rebuilt on every model change. |
| `BACKEND_INIT_TIMEOUT` | | `300` | optional | restart | Seconds the embedder waits for `BACKEND_READINESS_FILE` before it starts anyway. |

The embedder also uses `HF_HOME` (set by Compose and the k8s base to the
`hf_cache` volume), which tells the Hugging Face libraries where to cache the
downloaded model. Elefant does not read it itself.

The local model is pinned to one Hugging Face commit. It downloads once, on
the first start with an empty `hf_cache`, and the embedder logs one line when
it does. Later starts load it from `hf_cache` and make no network calls, so
they work with no internet access. You do not need `HF_TOKEN`: the model is
public. A new Elefant release that changes the pinned commit re-embeds the
wiki once on its first start, as any model change does.

## memory-api

| Variable | Set it with | Default | Required | Change needs | What it does |
|---|---|---|---|---|---|
| `POSTGRES_PASSWORD` (secret) | | none | required | care | Password of the bundled Postgres. memory-api refuses to start without it. Postgres keeps the password it was first initialised with, so a new value locks memory-api out of the existing volume. See the install guides. |
| `POSTGRES_HOST` | | `postgres` | optional | restart | Postgres host. |
| `POSTGRES_PORT` | not passed | `5432` | optional | restart | Postgres port. |
| `POSTGRES_DB` | | `memory` | optional | care | Database name. A new name is an empty database. |
| `POSTGRES_USER` | | `memory` | optional | care | Database user. |
| `RERANKER_ENDPOINT` | not passed | empty (off) | optional | restart | URL of a cross-encoder reranking service. memory-api posts `{"pairs": [[query, text], ...]}` and expects `{"scores": [...]}` back. If it is empty or the call fails, results are ranked by the hybrid score. |
| `CARD_EVAL_LOCK_STALE_SECONDS` | `ELEFANT_CARD_EVAL_LOCK_STALE_SECONDS` | `1800` | optional | restart | Age at which a board card's evaluation lock counts as stale, so that the next acquire may break it. Taking the lock again does not reset its clock, so this must be longer than one complete evaluation. |
| `COMPACTOR_ENABLED` | not passed | `1` (on) | optional | restart | `0`, `false` or `no` turns off the background compactor that promotes hot-tier captures into durable memories. |
| `COMPACTOR_INTERVAL_SECONDS` | not passed | `900` | optional | restart | Seconds between compactor runs. |
| `COMPACTOR_BATCH_SIZE` | not passed | `100` | optional | restart | Captures handled per run. |
| `COMPACTOR_CLUSTER_THRESHOLD` | not passed | `0.85` | optional | restart | Cosine similarity at which two captures are put in one cluster. Per-org override in the org's settings. |
| `COMPACTOR_PROMOTE_SCORE` | not passed | `1.0` | optional | restart | Cluster score at which it is promoted to a durable memory. Per-org override in the org's settings. |
| `COMPACTOR_DROP_SCORE` | not passed | `0.3` | optional | restart | Score below which an old enough capture is dropped. Per-org override in the org's settings. |
| `COMPACTOR_DROP_AGE_DAYS` | not passed | `7` | optional | restart | Age in days before a low-scoring capture can be dropped. Per-org override in the org's settings. |
| `COMPACTOR_EXISTING_DEDUP_SIM` | not passed | `0.9` | optional | restart | Similarity to an existing durable memory at which a promotion is a duplicate and is skipped. |

The four compactor values marked "per-org override" are defaults. An org's own
hot-tier settings, when set, win over them, and they apply again when the
editor cannot be reached.

## mcp

| Variable | Set it with | Default | Required | Change needs | What it does |
|---|---|---|---|---|---|
| `EXTERNAL_MCP_URL` | built from `ELEFANT_PUBLIC_DOMAIN` as `https://<domain>/mcp` | empty | required | restart | Public URL of the MCP server. Used in the OAuth metadata that clients discover. |
| `EXTERNAL_EDITOR_URL` | built from `ELEFANT_PUBLIC_DOMAIN` as `https://<domain>/editor` | empty | required | restart | Public URL of the editor. Its scheme and host are the OAuth issuer. |
| `ELEFANT_MCP_RATE_LIMIT_RPM` | | `0` (no limit) | optional | restart | Requests per minute per token. This is the master switch: limits stored on a token apply only when this or the next value is above zero. |
| `ELEFANT_MCP_RATE_LIMIT_RPD` | | `0` (no limit) | optional | restart | Requests per day per token. Same rule. |
| `DIAGRAM_VALIDATOR_URL` | Compose, k8s | `http://diagram-validator:8005` | optional | restart | Internal URL of the diagram validator, which checks every diagram create and patch with Mermaid's parser. Empty turns the lookup off. |
| `DIAGRAM_VALIDATE_TIMEOUT` | | `10` | optional | restart | Seconds the MCP server waits for one validation. Keep it above the validator's own `VALIDATE_TIMEOUT_MS`. |
| `DIAGRAM_ALLOW_UNVALIDATED` | Compose | `false` | optional | restart | When neither the validator nor the renderer answers, diagram writes are refused. Set this to `true` to save them instead, with `validated: false` and a warning. A source that a validator rejects is refused in all cases. |
| `DIAGRAM_RENDERER_URL` | Compose, k8s | `http://diagram-renderer:8004` | optional | restart | Internal URL of the optional diagram renderer. It draws the images the diagram tools return, and validates too. When nothing answers there, the tools return no image. Empty turns the lookup off. |
| `DIAGRAM_RENDER_TIMEOUT` | | `30` | optional | restart | Seconds the MCP server waits for one render. Keep it above the renderer's own `RENDER_TIMEOUT_MS`, so the renderer's clean "took too long" arrives first. |

## diagram-validator

Parses Mermaid diagrams for the MCP diagram tools, without a browser. Compose
and the Kubernetes base both run it. It reads the shared
`INTERNAL_SERVICE_TOKEN`, and refuses every request without it.

| Variable | Set it with | Default | Required | Change needs | What it does |
|---|---|---|---|---|---|
| `VALIDATE_TIMEOUT_MS` | | `5000` | optional | restart | Hard limit on one parse. A parse that runs over is stopped and its worker thread replaced. |
| `VALIDATE_HEAP_MB` | | `128` | optional | restart | Heap limit of the worker thread that parses. |
| `PORT` | | `8005` | optional | restart | Listening port. The MCP server's `DIAGRAM_VALIDATOR_URL` has to agree. |

## diagram-renderer

Optional. Renders Mermaid diagrams to images in headless Chromium for the
MCP diagram tools. Compose runs it only with the `diagrams` profile
(`COMPOSE_PROFILES=diagrams`); the Kubernetes base includes it. It also reads
the shared `INTERNAL_SERVICE_TOKEN`, and refuses every render without it.

| Variable | Set it with | Default | Required | Change needs | What it does |
|---|---|---|---|---|---|
| `RENDER_TIMEOUT_MS` | | `15000` | optional | restart | Hard limit on one render. A render that runs over is stopped and its browser killed. |
| `RENDER_CONCURRENCY` | | `2` | optional | restart | Renders run at once; the rest queue. |
| `RENDER_CACHE_ENTRIES` | | `256` | optional | restart | Rendered images kept in memory, keyed by a hash of the source. |
| `CHROMIUM_PATH` | image | `/usr/bin/chromium` | optional | restart | The browser to drive. |
| `PORT` | | `8004` | optional | restart | Listening port. The MCP server's `DIAGRAM_RENDERER_URL` has to agree. |

## Deployment mode

Every feature is on in every install: unlimited users, MCP OAuth, OIDC and
SAML sign-in, git sources, and groups and teams. There are no tiers and no
licence. `ELEFANT_MODE`, read by the editor, picks only the deployment's shape.

- `single-org` (the default): one organisation. The first account is its
  `org_admin`.
- `multi-org`: a platform admin hosts many organisations, each its own tenant
  with its own domains and wiki spaces. The first account is a
  `platform_admin`, and the platform admin console is on. Also set
  `PLATFORM_ADMIN_USERNAME` and `PLATFORM_ADMIN_PASSWORD`.
- An unknown value runs as `single-org`, with a warning in the log.
- Set the mode before the first start. Changing it later does not change the
  first account.
- An older `.env` may set `ELEFANT_TIER` instead. When `ELEFANT_MODE` is empty,
  `ELEFANT_TIER=saas` selects `multi-org` and logs a deprecation warning. Any
  other `ELEFANT_TIER` value is ignored, with one line in the log.

## Deployment-only keys

The services do not read these keys. Compose or kustomize use them to build
the stack.

| Key | Used by | Default | What it does |
|---|---|---|---|
| `ELEFANT_PUBLIC_DOMAIN` | Compose, k8s | none (required) | Builds `EDITOR_EXTERNAL_URL`, `EXTERNAL_MCP_URL` and `EXTERNAL_EDITOR_URL`, and is the Ingress host. |
| `ELEFANT_TAG` | Compose | `latest` | Image tag for Elefant's images. On Kubernetes the `?ref=` of the remote base pins the tag. |
| `EMBEDDER_IMAGE` | Compose | the full embedder image at `ELEFANT_TAG` | Full embedder image reference. Use the `slim` image only with an external embedding provider. |
| `EMBEDDER_INCLUDE_LOCAL` | Compose (build) | `true` | `false` builds the slim, torch-free embedder from source. |
| `PORT` | Compose | `8321` | Host port for nginx. |
| `MCP_PORT` | Compose | `8322` | Host port for direct access to the MCP server. |
| `COMPOSE_PROFILES` | Compose | empty | `diagrams` also starts the optional diagram renderer (a Chromium image). |

## Client install scripts

`make install-claude` and `make install-codex` (`scripts/install-*.sh`) run on
your workstation, not in the stack.

| Variable | Default | What it does |
|---|---|---|
| `ELEFANT_URL` | none (required) | Your deployment, e.g. `https://elefant.example.com`. |
| `ELEFANT_MCP_URL` | `$ELEFANT_URL/mcp` | MCP endpoint to register. |
| `ELEFANT_SKILL_FILE` | `skill/SKILL.md` in this repo | Skill file to install. |
| `ELEFANT_MCP_NAME` | `elefant` | Name the MCP server is registered under. |
| `ELEFANT_INSTALL_TOKEN` (secret) | none | Read by `scripts/configure-codex-mcp.py`. The install script sets it from the token you paste; you do not set it yourself. |
