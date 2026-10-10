# Elefant

Elefant is a self-hosted second brain that your AI assistants share. People
write in a wiki. Agents read it, remember what they learn, and keep a board of
work, all through one MCP endpoint. Claude, ChatGPT, Cursor, Codex and Open WebUI
connect to it, so what one assistant learns is there for the next.

## What it does

- **Wiki.** Markdown pages on disk with a WYSIWYG editor, folders and git-backed
  history. Agents can write too, but only inside the org's AI folder (`AI/` by
  default), so human pages stay human.
- **Two-tier AI memory.** Agents write cheap captures with `memory_capture`. A
  background compactor clusters them, scores them for recurrence, source
  diversity and recency, and promotes the survivors into durable, embedded
  memory. `memory_add` writes durable memory directly when a fact must be kept
  exactly.
- **Board.** A Kanban board for work and ideas: cards with comments,
  attachments, labels, links between cards (including "blocks" and "blocked
  by") and links to wiki pages. It updates live in the browser. Agents use it
  through the MCP `card_*` tools.
- **Search.** One semantic search over the wiki, memories and git sources.
- **Git knowledge sources.** Add a git repository, and its Markdown is indexed
  into search next to your wiki. Agents can read the files.
- **MCP server.** A Streamable HTTP endpoint at `https://<host>/mcp`. OAuth for
  claude.ai and mobile, and personal API tokens for desktop and CLI clients.
- **Identity and access.** Local accounts with required MFA, OIDC and SAML,
  orgs, teams, groups and per-token scopes.
- **Backup and restore.** Encrypted backups of the wiki and memories, with a
  restore screen in the admin UI.

**Every feature is included in every install.** There are no paid tiers, no
user limits, no trial periods and nothing to unlock: a default install has
unlimited users, MCP OAuth, OIDC and SAML, git sources, and groups and teams.
The one choice is the deployment's shape, set by `ELEFANT_MODE`: one
organisation (`single-org`, the default) or a platform admin hosting many
(`multi-org`). The install guide's
[One organisation or many](docs/install/docker-compose.md#one-organisation-or-many)
section has the details.

The full documentation index is [`docs/index.md`](docs/index.md).

## Install: pick one

| Path | Pick it when | Full guide |
|---|---|---|
| **Docker Compose** | You have one machine, and you want Elefant running on it in a few minutes. | [`docs/install/docker-compose.md`](docs/install/docker-compose.md) |
| **Kubernetes** | You already run a cluster — k3s, kubeadm, EKS, GKE, AKS. | [`docs/install/kubernetes.md`](docs/install/kubernetes.md) |

Both paths run the same published images, `ghcr.io/zennthic/elefant/*`, built for
**linux/amd64** and **linux/arm64**. Both quick starts below are extracted from
this file and run end to end in CI, so they cannot drift from the manifests they
install.

**What it needs**, either way: **2 CPU cores and 4 GiB RAM** to start (8 GiB is
comfortable — the embedder alone is capped at 1.5 GiB), plus disk: about
**20 GiB** for a Compose install, or **63 GiB across seven volumes** for the
Kubernetes one.

Pick your version from the [Releases](https://github.com/zennthic/elefant/releases)
page and use it in place of `vX.Y.Z` below. The `latest` tag is not a release: it
is the most recent build of `main`, so it moves under you.

### Docker Compose

<!-- test: compose-quickstart -->
```bash
git clone https://github.com/zennthic/elefant.git
cd elefant
cp .env.example .env

# The version to run, and the five values the stack will not start without.
# Appending wins: these override whatever .env.example left blank.
{
  echo "ELEFANT_TAG=vX.Y.Z"
  echo "ELEFANT_SESSION_SECRET=$(openssl rand -hex 32)"
  echo "ELEFANT_INTERNAL_SERVICE_TOKEN=$(openssl rand -hex 32)"
  echo "POSTGRES_PASSWORD=$(openssl rand -hex 24)"
  echo "ELEFANT_S3_ACCESS_KEY=$(openssl rand -hex 12)"
  echo "ELEFANT_S3_SECRET_KEY=$(openssl rand -hex 24)"
} >> .env

docker compose up -d --pull always
```

Open **http://localhost:8321/setup** and create the first account. The embedder
downloads its model on first boot, so semantic search takes a few minutes to
come alive; everything else is usable immediately.

Before you put it in front of anyone, set `ELEFANT_PUBLIC_DOMAIN` in `.env` to
your own hostname and put TLS in front of nginx —
[the guide](docs/install/docker-compose.md) covers both.

#### Promoting a release: preflight → pull → up → verify

Moving an existing install to a new release is where a Compose stack breaks:
new images meet an old compose file, and `docker compose ps` can say
"healthy" while the site is down. `scripts/prod-preflight.sh` checks the host
before and after. Use the copy from the release you are promoting. It needs
`python3` on the host.

```bash
cd elefant
sed -i 's/^ELEFANT_TAG=.*/ELEFANT_TAG=vX.Y.Z/' .env

# 1. Preflight, then pull. The script pulls the release's images and changes
#    nothing that is running. It fails if the compose file is not the
#    release's, if .env is missing a required key, if an image changes user
#    or listens on another port, or if a volume is not owned by the user that
#    mounts it (it prints the chown).
scripts/prod-preflight.sh --source .     # or GITHUB_TOKEN=... without a checkout

# 2. Up. Pass every compose file the stack uses (the Traefik override too).
docker compose up -d --no-build
docker compose restart nginx

# 3. Verify: health, the published port on the LAN IP, the auth gate, a
#    token check and a semantic search through /mcp, and the editor's logs.
ELEFANT_VERIFY_TOKEN=... scripts/prod-preflight.sh --verify
```

Do not run step 2 while step 1 has a `FAIL` line. The
[upgrade guide](docs/install/docker-compose.md#upgrading) has the details.

### Kubernetes

You need a cluster, `kubectl`, and a default StorageClass
(`kubectl get storageclass` shows one marked `(default)`).

<!-- test: k8s-kustomization file=kustomization.yaml -->
```yaml
# kustomization.yaml — the only file you write. The remote base carries the
# manifests and the image tags for that release; this adds your domain.
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
namespace: elefant

resources:
  - https://github.com/zennthic/elefant//deploy/k8s/base?ref=vX.Y.Z

configMapGenerator:
  - name: elefant-config
    behavior: merge
    literals:
      - ELEFANT_PUBLIC_DOMAIN=elefant.example.com
```

<!-- test: k8s-quickstart -->
```bash
kubectl create namespace elefant

# Once, by hand. `apply -k` deliberately does not manage this: it would rewrite
# the Secret on every deploy, and POSTGRES_PASSWORD has to stay matched to the
# volume that Postgres already initialised with it.
kubectl -n elefant create secret generic elefant-secrets \
  --from-literal=ELEFANT_SESSION_SECRET="$(openssl rand -hex 32)" \
  --from-literal=ELEFANT_INTERNAL_SERVICE_TOKEN="$(openssl rand -hex 32)" \
  --from-literal=POSTGRES_PASSWORD="$(openssl rand -hex 24)" \
  --from-literal=ELEFANT_S3_ACCESS_KEY="$(openssl rand -hex 12)" \
  --from-literal=ELEFANT_S3_SECRET_KEY="$(openssl rand -hex 24)"

kubectl apply -k .
kubectl -n elefant rollout status deploy/nginx --timeout=10m
```

Then reach it. With no ingress controller in the way:

```bash
kubectl -n elefant port-forward svc/nginx 8321:80
# http://localhost:8321/setup
```

A plain-http trial over `port-forward` works completely, login included: the
session cookie is only marked `Secure` when a proxy in front sends
`X-Forwarded-Proto: https`, and the OAuth issuer is derived from the address you
actually used. For a real deployment, add an Ingress —
`deploy/k8s/examples/` has one for ingress-nginx with cert-manager and one for
Traefik, and [the guide](docs/install/kubernetes.md) covers TLS, upgrades,
backup and uninstall.

## Connect your AI

Every client talks to the same endpoint:

```
https://<host>/mcp
```

There are two ways to sign in:

- **OAuth**, for claude.ai on the web and in the Claude mobile apps. Add
  Elefant as a custom connector with the URL above, then sign in to Elefant
  when the client asks. You do not copy a token.
- **A personal API token**, for Claude Desktop, Claude Code, Cursor, Codex,
  Open WebUI and other clients that take a header. In Elefant, open
  **My Settings** (your name, top right), go to **API tokens** and select
  **+ New token**. Copy the token when it is shown, because Elefant shows it
  only once. The client sends it as `Authorization: Bearer <token>`.

For Claude Code and Codex, one command does the whole setup, including the
Elefant skill:

```bash
make install-claude ELEFANT_URL=https://elefant.example.com
make install-codex  ELEFANT_URL=https://elefant.example.com
```

[`docs/connect/`](docs/connect/README.md) has a section for each client: the
config to paste, where the token comes from, and the base prompt that makes the
assistant search and capture without being asked.

## Services

| Service     | Port  | Role |
|-------------|-------|------|
| `editor`    | 8080  | FastAPI: wiki CRUD, auth, OAuth AS, admin API |
| `embedder`  | 8001  | Embedding pipeline, Qdrant indexer, semantic search |
| `mcp`       | 8003  | Streamable HTTP MCP endpoint |
| `memory-api`| 8002  | Memory store/retrieval API |
| `memory-ui` | 80    | Wiki viewer + memory browser SPA |
| `nginx`     | —     | Internal reverse proxy, `auth_request` gate |
| `postgres`  | 5432  | memory-api store: events, hot tier, revisions, board, git index (row-level security per tenant) |
| `qdrant`    | 6333  | Vector store |
| `minio`     | 9000  | Object storage for wiki pages and attachments |
| `diagram-validator` | 8005 | Parses Mermaid diagrams (no browser) so the MCP diagram tools refuse a broken one ([docs](docs/diagrams.md)) |
| `diagram-renderer` | 8004 | Optional. Renders Mermaid diagrams in headless Chromium for the MCP diagram tools ([docs](docs/diagrams.md)) |

The editor keeps its own database (accounts, orgs, tokens, settings) in
**SQLite at `/data/config.db`** on the `editor_data` volume. Both quick starts
give you this. Set `POSTGRES_EDITOR_DSN` to move it to PostgreSQL instead; see
[`docs/configuration.md`](docs/configuration.md).

Nothing in the stack terminates TLS. `docker-compose.yml` publishes nginx on
`${PORT:-8321}` over plain http and assumes no proxy in front of it, so the
choice of reverse proxy is yours — `docs/install/docker-compose.md` shows
Caddy and nginx, and `docker-compose.traefik.yml` is the Traefik override this
project's own hosts run.

To host many organisations, set `ELEFANT_MODE=multi-org` before the first
start, so the bootstrap creates a `platform_admin` rather than an `org_admin`.

## Documentation

Start at [`docs/index.md`](docs/index.md). The main entries:

- [`docs/install/`](docs/install/) has the two install guides in full
- [`docs/configuration.md`](docs/configuration.md) lists every environment
  variable, with its default and what a change needs
- [`docs/connect/`](docs/connect/README.md) connects your AI client
- [`docs/getting-started/overview.md`](docs/getting-started/overview.md) covers
  your first ten minutes
- [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) lists common failure modes
- [`security/`](security/) covers tenant isolation and data management
- [`docs/onboarding/`](docs/onboarding/) has printable admin and user guides

Questions? Ask in [GitHub Discussions](https://github.com/Zennthic/elefant/discussions);
[`SUPPORT.md`](SUPPORT.md) says where everything else goes.

## Contributing

Contributions are welcome. See [`CONTRIBUTING.md`](CONTRIBUTING.md) to set up a
dev environment and run the tests, and [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md)
for community expectations. To report a security vulnerability, follow
[`SECURITY.md`](SECURITY.md) — please do **not** open a public issue for security
reports.

Sign your commits off with `git commit -s`. That line certifies the
[Developer Certificate of Origin](https://developercertificate.org/) — that you
wrote the code, or otherwise have the right to contribute it under this
project's license. CI checks for it. There is no CLA.

## License

Elefant is open source, licensed under the
[Apache License 2.0](LICENSE) (`Apache-2.0`). Copyright © Zennthic LLC.

Run it, modify it, and deploy it — including inside a company and including
commercially. Keep the license and [`NOTICE`](NOTICE) with any copies you
distribute, and say what you changed. The license also grants you the patent
rights the contributors hold in their own contributions.

### Trademarks

The license covers the code. It does not grant rights to the **Elefant** name,
the Elefant logo, or the Zennthic name and marks — Apache-2.0 §6 excludes them
deliberately, and they remain the property of Zennthic LLC.

In practice:

- **Yes**, without asking: say your project uses, integrates with, is built on,
  or is compatible with Elefant. Keep the name in a fork you publish so people
  can see where it came from. Write about it, teach it, compare it.
- **Please ask first**: naming a product, service, company, or domain after
  Elefant, or using the logo as your own — anything that would leave a
  reasonable person unsure whether Zennthic publishes it or stands behind it.

A modified build distributed under the Elefant name should say plainly that it
is modified and not an official release. Questions: <trademarks@zennthic.com>.
