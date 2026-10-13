# Data Management and Custody

**Classification:** Public Security Documentation  
**Last Updated:** 2026-10-04

This page says where Elefant keeps each kind of data, how it is protected, how
it leaves, and how it is backed up and deleted. Every control names the code
that implements it, so you can check the claim against the source.

> **Elefant is not certified.** The project holds no SOC 2, ISO 27001 or similar
> attestation, and nothing on this page claims one. A self-hosted install is
> yours to operate and to certify. The [control mapping](#mapping-to-your-own-compliance-programme)
> at the end lists controls you can cite in your own programme. It does not say
> that Elefant, or any deployment of it, meets a standard.

---

## Where the Editor's Database Lives

The editor keeps accounts, orgs, tokens, ACLs, settings, wiki history and the
recycle bin in one SQLAlchemy database (`editor/config_db.py`). It runs in one
of two modes:

| Mode | When | Where |
|---|---|---|
| **SQLite** (default) | `POSTGRES_EDITOR_DSN` is empty or unset | The file at `DB_PATH`, default `/data/config.db`, on the editor's data volume (`editor_data` in Docker Compose, the `editor-data` PVC on Kubernetes) |
| **PostgreSQL** | `POSTGRES_EDITOR_DSN` is set to a SQLAlchemy URL | That database |

Both quick starts give you **SQLite at `/data/config.db`**. `docker-compose.yml` passes
`POSTGRES_EDITOR_DSN` through with an empty default, and the Kubernetes base in
`deploy/k8s/base/` does not set it at all. The bundled `postgres` service is
the memory-api's store (events, hot tier, revisions, board, git index), not the
editor's, unless you point `POSTGRES_EDITOR_DSN` at it yourself. Switching modes
does not move existing data. See [`docs/configuration.md`](../docs/configuration.md).

---

## Data Inventory

| Data | Where it is stored | Protection at rest | Retention |
|---|---|---|---|
| Wiki pages (Markdown) and card attachments | Object storage: one S3 bucket per org wiki (`editor/storage.py`), the bundled MinIO by default. The embedder also keeps a working mirror under `DOCS_DIR` (`/docs`, `/docs/{slug}/` for orgs other than `default`) | Bucket versioning is turned on at provision time. Disk encryption is the operator's | Until deleted. Deleted pages go to the recycle bin for 30 days |
| Wiki embeddings | Qdrant: `knowledge_base` for the `default` org, `{slug}_wiki` for others (`tenant_wiki_collection()` in `editor/config_db.py`, `_wiki_coll()` in `embedder/main.py`) | Operator-managed disk encryption | Removed with the page |
| Durable memories | Qdrant: `memories` for the `default` org, `{slug}_memories` for others (`_mem_coll()` in `embedder/main.py`) | Operator-managed disk encryption | Until deleted |
| Hot-tier captures, memory revisions, event log | PostgreSQL tables `memory_hot`, `memory_revisions`, `events` (`memory-api/main.py`) | Operator-managed disk encryption; row-level security per tenant | Until the tenant is purged |
| Board (columns, cards, links, comments) | PostgreSQL tables `board_columns`, `cards`, `card_links`, `card_comments`, `card_number_seq` (`memory-api/main.py`) | Operator-managed disk encryption; row-level security per tenant | Indefinite. Cards are closed rather than deleted |
| Git source index | Clones on the `git_sources` volume; indexed file rows in PostgreSQL `git_files` | Operator-managed disk encryption; row-level security per tenant | Until the source is removed |
| Git source credentials (PAT, password, SSH key) | Editor database, `git_sources` table | Fernet per field (`token_enc`, `password_enc`, `private_key_enc`) | Until the source is removed |
| Settings and integration secrets | Editor database, `config_entries` table | Fernet per row (`ConfigEntry` in `editor/config_db.py`) | Until overwritten |
| Other secrets: OIDC client secrets, SAML private keys, TOTP secrets, backup passcodes, S3 and SSH backup target credentials | Editor database, the matching `*_enc` columns | Fernet per field | Until removed |
| API tokens and OAuth access tokens | Editor database, `api_tokens` and `oauth_access_tokens` | **Only a SHA-256 hash** of each token is stored. The raw token is shown once and cannot be recovered | API tokens: until revoked or expired. OAuth: `OAUTH_ACCESS_TOKEN_TTL_DAYS`, default 365 days (`editor/routers/oauth.py`) |
| Local passwords | Editor database, `user_accounts.password_hash` | bcrypt hash (`hash_password()` in `editor/auth.py`); no plaintext stored. NULL for accounts that sign in only through an IdP | Until the account is deleted |
| Org snapshots | `/data/org-backups/{slug}/`, and optionally an S3 or SSH target | AES-256 password ZIP (`editor/backup_crypto.py`). An unencrypted snapshot is refused | Per the schedule's `retention_days`, default 30 |
| Service logs | Container stdout, to your log aggregator | Operator-managed | Operator-managed |

---

## Data Classification

| Class | Definition | Examples |
|---|---|---|
| **Confidential** | Secrets, credentials, personal data | Tokens, passwords, IdP secrets, user email addresses |
| **Internal** | Knowledge the org writes | Wiki pages, memories, board cards, indexed git content |
| **Operational** | Telemetry and logs | Reindex timing, error logs, event metadata |

---

## Encryption Key Management

Field encryption uses Fernet, through `CryptoManager` in `editor/config_db.py`.
On first use it reads the key from `CRYPTO_KEY_PATH` (default
`/data/crypto.key`). If the file does not exist, it generates a new key and
writes the file with mode `0600`.

As the operator:

- Back up `/data/crypto.key`, and keep that copy apart from the database it
  protects. Without the key, every Fernet-encrypted field is unrecoverable.
- Never delete or regenerate the key on a running install. A new key cannot
  read what the old one wrote.
- Keep the file readable only by the editor's user (UID `10001` in the
  shipped images).

There is no key-rotation endpoint. To rotate, you would have to re-encrypt
every `*_enc` column and `config_entries` row yourself; plan for the key to
live as long as the database.

Org snapshots are encrypted with a passcode, not with `crypto.key`. A snapshot
therefore restores on a different install, and
[`tools/decrypt-elefant-backup.py`](../tools/decrypt-elefant-backup.py) can
open one outside the app.

---

## Data Flows

### Inbound

| Source | Transport | Data |
|---|---|---|
| Browser (wiki, board, admin) | HTTP to nginx; TLS is whatever proxy you put in front | Pages, cards, settings |
| MCP client (Claude, Codex and others) | HTTP to nginx with an OAuth or API bearer token; TLS as above | Memory, wiki and board calls |
| Git sources added by an admin | HTTPS or SSH, outbound clone and fetch | Repository content, indexed for search. Nothing is pushed back |

**Nothing in the stack terminates TLS.** nginx listens on plain HTTP, and
`docker-compose.yml` publishes it on `${PORT:-8321}`. Encryption in transit
between users and Elefant is the job of the reverse proxy or ingress you put in
front: see [`docs/install/docker-compose.md`](../docs/install/docker-compose.md)
and [`docs/install/kubernetes.md`](../docs/install/kubernetes.md#ingress-and-tls).
Traffic between the containers is plain HTTP on the internal network,
authenticated by `INTERNAL_SERVICE_TOKEN`.

The session cookie is set with `secure` only when the request arrived over TLS
(`editor/auth.py`), so run the public endpoint over HTTPS.

### Outbound

| Destination | When | Data | Control |
|---|---|---|---|
| Embedding API | Indexing and search, only when the provider is not `local` | Text chunks and queries | `EMBEDDING_PROVIDER` (default `local`, where nothing leaves the host) |
| Reranker endpoint | Memory search, only when set | Query text and candidate texts | `RERANKER_ENDPOINT` (default empty) |
| Backup targets | Scheduled or manual org snapshot, only when configured | The encrypted snapshot | Admin → Organisation backup settings |
| Git remotes | Source sync | Clone and fetch requests only | Admin-added sources |

**No telemetry, crash reporting or usage analytics is sent to the Elefant
maintainers or anyone else.** You can confirm this by watching the containers'
outbound connections.

---

## Backup and Recovery

Use the application's **org snapshot** (Admin → Organisation → Snapshot). It is
built by `_create_org_snapshot()` in `editor/org_backup.py` and is consistent
across all three stores, which separate volume copies taken while the stack runs
are not. One snapshot is a single AES-256 password ZIP holding:

| Member | Contents |
|---|---|
| `manifest.json` | Org, counts, format version |
| `db.json` | The org's rows from the editor database: users, teams, groups, tokens (hashes), ACLs, settings, wiki history, recycle bin |
| `memories.json` | Durable memories from Qdrant |
| `memapi.json` | Board tables, hot tier and memory revisions from PostgreSQL |
| `wiki.tar.gz` | The org's wiki pages |

The `events` log and the git index are not in a snapshot. The git index can
be rebuilt by syncing the source again.

Restore runs from the same admin screen as a supervised job
(`editor/restore_jobs.py`). Keep the snapshot passcode with the snapshot: it is
the only way to open it.

Beyond snapshots, back up these yourself:

| What | Why |
|---|---|
| `/data/crypto.key` | Required to read every encrypted field, in either database mode |
| `/data/config.db` (SQLite mode) or your editor database (PostgreSQL mode) | The whole install's accounts and settings, all orgs |
| The `postgres` volume | Events log, which snapshots leave out |

The install guides cover volume-level copies:
[Docker Compose](../docs/install/docker-compose.md#backup),
[Kubernetes](../docs/install/kubernetes.md#backup).

---

## Deletion

### By users

- **Wiki page.** Moves to the recycle bin (`soft_deleted_pages`, status
  `recycled`). It is removed for good when a user empties it, or by the
  startup purge of entries older than 30 days (`editor/routers/wiki.py`). Its
  embeddings leave Qdrant when it goes to the bin, and come back if it is
  restored. Old object versions stay in the versioned bucket until your
  lifecycle rules expire them.
- **Memory.** The point is deleted from Qdrant at once, after the tenant and
  author checks in `embedder/main.py`. Its rows in `memory_revisions` and
  `events` are history and are kept until the tenant is purged.
- **Card.** Cards are closed, not deleted, and a closed card stays readable.
  Comments can be deleted.
- **Account.** An admin removes the user from the org. Check that the user's
  API tokens are revoked as well.

### Org purge (multi-org installs)

A platform admin purges an org with
`DELETE /api/platform/orgs/{org_id}/purge` (`_purge_org_data()` in
`editor/routers/admin_platform.py`). In one call it:

1. Deletes the org's rows from the editor database, including tokens, users,
   teams, groups, ACLs, settings and the org itself.
2. Removes the org's `/docs/{slug}/` mirror and `/data/org-backups/{slug}/`.
3. Drops the org's Qdrant collections, through the embedder.
4. Deletes the org's rows from `events`, `memory_hot`, `memory_revisions` and
   `git_files`, through `DELETE /tenant/{slug}` on memory-api.
5. Empties and drops the org's S3 bucket, every object version included.

Board rows (`board_columns`, `cards`, `card_links`, `card_comments`,
`card_number_seq`) are **not** removed by the purge today. Delete them by
`tenant_id` in PostgreSQL if you need them gone. See
[known limitations](tenant-isolation.md#known-limitations-and-future-work).

A purge cannot be undone. Copies inside backups and snapshots stay until your
own retention removes them.

---

## Mapping to Your Own Compliance Programme

Elefant is not certified, and this is not an attestation. These are the
controls the code provides, for an operator to map to their own SOC 2, ISO 27001
or GDPR work. Whether they satisfy a given criterion depends on how you deploy
and run them.

| Concern | Control in Elefant | Where | Yours to provide |
|---|---|---|---|
| Logical access | Session and bearer authentication on every protected route; tenant bound to the session or token | `editor/auth.py`, `editor/auth_helpers.py`, `nginx/nginx.conf` | IdP, account lifecycle, access reviews |
| Confidentiality of secrets | Fernet field encryption; tokens and passwords stored only as hashes | `editor/config_db.py`, `editor/auth.py` | Key custody, disk encryption |
| Tenant separation | Row-level security, per-tenant collections and buckets | [tenant-isolation.md](tenant-isolation.md) | A least-privilege database role |
| Encryption in transit | None inside the stack | | TLS at your proxy or ingress |
| Availability | `restart: unless-stopped`; `/health` on each service; encrypted org snapshots | `docker-compose.yml`, `editor/org_backup.py` | Monitoring, backup testing, capacity |
| Third-party processors | External embedding and reranking are off by default | `embedder/main.py`, `memory-api/main.py` | Your own vendor review if you enable them |
| Erasure | Page, memory and org deletion as above | This page, [Deletion](#deletion) | Board rows on purge; backup retention |
| Portability | Org snapshot; wiki pages as plain Markdown | `editor/org_backup.py` | |
| Logging | Service logs to stdout | | Collection, retention, review |

---

## Data Processing

A self-hosted install is run entirely by you. For the data it holds you are the
controller, and the processor too. The Elefant project and its maintainers have
no access to it.
