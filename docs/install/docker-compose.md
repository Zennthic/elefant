# Installing Elefant with Docker Compose

One machine, one `.env`, nine containers. This is the quickest way to a running
Elefant, and the one to pick unless you already run a cluster (for that, see
[`kubernetes.md`](kubernetes.md)).

The quick start in the repository [`README.md`](../../README.md) gets you to a
login page. This page is everything after that: what the machine needs, what
each setting does, how to put TLS in front of it, and how to upgrade, back up
and remove it.

## What you need

| | |
|---|---|
| Docker | 24 or newer, with the Compose v2 plugin (`docker compose version`). |
| Architecture | `linux/amd64` or `linux/arm64`. The images are published for both. |
| CPU | 2 cores to start. The embedder is the only greedy one, and only while it indexes. |
| RAM | **4 GiB minimum, 8 GiB comfortable.** The embedder is capped at 1.5 GiB by `mem_limit`, which is a tuned number rather than a guess: below it the model load fails. |
| Disk | About **20 GiB** free. Eight named volumes grow with your content; the images themselves are roughly 4 GiB, most of that the embedder's ML runtime. |
| Ports | One published port for the front door (`8321` by default) and one for direct MCP access (`8322`). Both are configurable. |

macOS and Windows work through Docker Desktop or OrbStack, but give the VM at
least 6 GiB of memory or the embedder will be killed mid-load.

## Install

```bash
git clone https://github.com/zennthic/elefant.git
cd elefant
cp .env.example .env
```

Then edit `.env`. Every value is documented in the file itself; these are the
ones you cannot skip.

| Key | What to put in it |
|---|---|
| `ELEFANT_PUBLIC_DOMAIN` | The hostname people will use, e.g. `wiki.example.com`. The editor and the MCP server hand this out as their own external URLs, so a wrong value produces links that do not resolve. |
| `ELEFANT_SESSION_SECRET` | `openssl rand -hex 32`. Signs session cookies; rotating it logs everybody out. |
| `ELEFANT_INTERNAL_SERVICE_TOKEN` | `openssl rand -hex 32`. Authenticates the containers to each other. Without it the internal flows (compaction, restore, purge) fail closed. |
| `POSTGRES_PASSWORD` | Any high-entropy string. The bundled Postgres holds every tenant's captures, events and audit revisions. |
| `ELEFANT_S3_ACCESS_KEY` | Credentials for object storage. With the bundled MinIO these are also its root credentials, which is why the defaults are not usable. |
| `ELEFANT_S3_SECRET_KEY` | At least 8 characters. |

Everything else has a working default. The
[configuration reference](../configuration.md) lists every variable the services
read, if you want to tune one.

And one you should not skip:

```bash
echo "ELEFANT_TAG=vX.Y.Z" >> .env      # the tag from the Releases page
```

`ELEFANT_TAG` is what every service's image reference resolves to. Left unset it
is **`latest`, which is not a release** — it is the most recent build of `main`,
so an unpinned install changes whenever we push. Pin it, and move the pin
deliberately.

Then:

```bash
docker compose up -d --pull always
```

`docker compose ps` should show nine services. Open
`http://localhost:8321/setup` and create the first account — the account created
there is the org admin.

The embedder is the slow one. It downloads a sentence-transformers model on
first boot, which takes a few minutes and is cached in the `hf_cache` volume
afterwards. Until it finishes, semantic search returns nothing and everything
else works.

### One organisation or many

Every feature is on in every install, with no key and no limits: unlimited
users, MCP OAuth, OIDC and SAML, git sources, groups and teams. The default
install serves one organisation, and its first account is that organisation's
`org_admin`.

To host many organisations instead, each its own tenant with its own domains,
set this before the first start:

```
ELEFANT_MODE=multi-org
```

`multi-org` makes the first account a `platform_admin` rather than an
`org_admin`, and turns on the platform admin console. Switching the mode later
is supported; switching *after* bootstrap does not go back and change what the
first account is.

An older `.env` may set `ELEFANT_TIER`. `ELEFANT_TIER=saas` still means
`multi-org`, with a deprecation warning in the log. Any other value is ignored.

## TLS

**Nothing in the stack terminates TLS.** `docker-compose.yml` publishes nginx on
`${PORT:-8321}` over plain http and assumes no proxy in front of it. That is
deliberate — it is what makes a fresh `docker compose up -d` work on a machine
that runs nothing else — and it means the certificate is your reverse proxy's
job.

A plain-http install on `localhost` is fully usable for a trial, login included:
the session cookie is only marked `Secure` when the proxy in front sends
`X-Forwarded-Proto: https`, and the editor derives its own base URL from the
address you actually used. Do not run it that way on a network.

Whatever proxy you choose, it must pass three headers, because nginx forwards
them to the editor and the editor builds its public URLs from them:

```
Host              $host
X-Real-IP         $remote_addr
X-Forwarded-Proto https
```

`X-Forwarded-Proto` is the load-bearing one. Without it the editor decides it is
being reached over http, and the session cookie loses its `Secure` flag.

### Caddy

Two lines, and it gets the certificate itself:

```
elefant.example.com {
    reverse_proxy localhost:8321
}
```

Caddy sets `Host`, `X-Forwarded-Proto` and `X-Real-IP` by default, so there is
nothing else to configure.

### nginx

```nginx
server {
    listen 443 ssl;
    server_name elefant.example.com;

    ssl_certificate     /etc/letsencrypt/live/elefant.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/elefant.example.com/privkey.pem;

    client_max_body_size 64m;          # attachments and org snapshots

    location / {
        proxy_pass http://127.0.0.1:8321;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;

        # The MCP endpoint streams. Without these, a long tool call is cut off
        # at the proxy's default read timeout with no error the client can see.
        proxy_buffering off;
        proxy_read_timeout 300s;
    }
}
```

### Traefik

If you already run Traefik, this repository ships the override it uses itself:

```bash
docker compose -f docker-compose.yml -f docker-compose.traefik.yml up -d
```

Read [`docker-compose.traefik.yml`](../../docker-compose.traefik.yml) before
using it. It expects a Traefik already attached to a Docker network named
`traefik_traefik-public`, with a `websecured` entrypoint and a `route53`
certificate resolver. Those three names are this project's; change them to match
yours. It is a separate file precisely because it declares an **external**
network, and an external network in the base file makes `docker compose up -d`
fail on any host that does not already have it.

Whichever proxy you use, once TLS is in front you probably want the published
port off the LAN. `.env` takes a bind address:

```
PORT=127.0.0.1:8321
MCP_PORT=127.0.0.1:8322
```

## Upgrading

```bash
cd elefant
git pull                                  # if you want the new compose file too
sed -i 's/^ELEFANT_TAG=.*/ELEFANT_TAG=vX.Y.Z/' .env
docker compose up -d --pull always
docker compose restart nginx
```

That last line is not optional and is easy to lose. Recreated services get new
container IP addresses, nginx resolves its upstreams once at startup, and it is
not itself recreated by an image change — so without the restart it keeps
proxying an address that no longer answers, and every page returns `502`.

### Check the host before and after: `scripts/prod-preflight.sh`

Run the release's copy of `scripts/prod-preflight.sh` on the host before
`up -d`, and again with `--verify` after it. It reads the release from
`ELEFANT_TAG` in `.env` and fetches that release's `docker-compose.yml` and
`.env.example` from a git checkout (`--source DIR`) or from GitHub
(`GITHUB_TOKEN` is optional; it raises the API rate limit).

Preflight pulls the release's images and changes nothing that is running. It
fails when:

- the local compose file (and the Traefik override, if you use it) is not the
  release's, and prints the diff;
- `.env` has no value for a key the release requires (it prints key names,
  never values);
- an image runs as a different user than the container it replaces
  (`--allow-user-change` accepts this after you have checked the volumes);
- a published port, or a Traefik `loadbalancer.server.port`, is a port the
  image does not listen on;
- a non-root image mounts a volume that it does not own. It prints the exact
  `chown` command. If the compose file has a `volume-perms` step that covers the
  volume, this is only a warning, because `up` fixes it.

`--verify` waits up to three minutes for every service to be running and
healthy. It fetches `/login` on the published port at the host's LAN IP and
checks that `/editor/` redirects to login. It greps the last two minutes of
editor logs for `Errno 13` and `Failed to initialize encryption`. With
`ELEFANT_VERIFY_TOKEN` set in the environment (an API token with board read
and wiki/memory read), it also asks the editor's `/api/auth/check` to accept
the token and runs one semantic search through `/mcp`. Without the token those
two checks are skipped, and the run exits non-zero unless you pass
`--allow-skips`.

nginx, mcp and memory-api have healthchecks. The nginx healthcheck fetches
`/login` through the proxy to the editor. Thus an nginx that is up but cannot
reach its upstream shows as `unhealthy` in `docker compose ps`, not as `Up`.
Behind Traefik, an nginx that is not healthy is removed from routing.

Read the release notes before a minor version bump. Database migrations run
automatically at editor startup; they are forward-only, so take a snapshot first
(below) if you might want to go back.

### Upgrading to the non-root images

Every Elefant image now runs as a non-root user: UID/GID `10001` for the Python
services, and `101` for the two nginx images (`nginx`, `memory-ui`), which
now listen on **8080** inside the container instead of 80. Take the new
`docker-compose.yml` with this upgrade (`git pull` above is not optional this
once). An older compose file will not work with these images: it publishes
nginx's port 80, and it has no `volume-perms` step.

`volume-perms` is a one-shot service in the compose file. On every `up` it
runs as root, gives the existing volumes (created by the old images, so owned
by root) to `10001:10001`, and exits. The editor and the embedder wait for it
to finish. The first run walks every file. Later runs change nothing and finish
in about a second. `docker compose ps -a` shows it as `Exited (0)`, which is
the correct state.

If you cannot take the new compose file yet, chown the volumes by hand once,
with the stack stopped, before starting the new images. Do not touch
`postgres_data`, `minio_data` or `qdrant_data`: those images run as their own
users.

```bash
docker run --rm -v elefant_editor_data:/v/a -v elefant_docs:/v/b \
  -v elefant_backend_sync:/v/c -v elefant_git_sources:/v/d \
  -v elefant_hf_cache:/v/e alpine chown -R 10001:10001 /v
```

If the editor logs `Failed to initialize encryption: [Errno 13] Permission
denied`, this is the cause. Fix the ownership. Never delete or regenerate
`/data/crypto.key`, because every encrypted backup and stored secret depends on
it.

If you put your own proxy override in front of nginx on the Docker network
rather than on the published port, point it at port 8080 now.

## Backup

The data lives in named volumes, and the supported way to get it out is the
application's own **org snapshot**, not a `tar` of the volumes: a snapshot is
consistent across Postgres, Qdrant and object storage, which three separate
copies taken while the stack is running are not.

Take one from **Admin → Organisation → Snapshot**, or over the API. Snapshots
are encrypted; keep the key with them, and note that
[`tools/decrypt-elefant-backup.py`](../../tools/decrypt-elefant-backup.py) is
what reads one back outside the app. See
[`security/data-management.md`](../../security/data-management.md) for what a
snapshot contains and for the restore path.

Know where each store lives. The editor's own database (accounts, orgs, tokens,
settings) is **SQLite at `/data/config.db`** on the `editor_data` volume, next
to `/data/crypto.key`. It moves to Postgres only if you set
`POSTGRES_EDITOR_DSN` in `.env`. The `postgres_data` volume holds memory-api's
data: events, the hot tier, revisions, the board and the git index. Wiki pages
are in `minio_data`.

If you do also want a volume-level copy, stop the stack first
(`docker compose stop`) so Postgres is not mid-write:

```bash
docker run --rm --name elefant-backup \
  -v elefant_postgres_data:/data:ro -v "$PWD":/out \
  alpine tar czf /out/postgres_data.tgz -C /data .
```

Repeat per volume (`docker volume ls | grep elefant`). Treat it as a disaster
copy, not as the backup you plan to restore from.

## Uninstall

```bash
docker compose down            # stops and removes the containers, keeps the data
```

`down` on its own leaves every named volume in place, so the same `up -d` brings
your content back. To remove the data as well:

```bash
docker compose down -v         # THIS DELETES YOUR WIKI, MEMORIES AND EVENT LOG
```

There is no undo, and no prompt. Take a snapshot first if there is any chance
you want it back.

## When it does not come up

[`TROUBLESHOOTING.md`](../../TROUBLESHOOTING.md) covers the failure modes worth
recognising. The three that catch people on a fresh install:

- **`network traefik_traefik-public declared as external`** — you are running
  the Traefik override without a Traefik. Drop `-f docker-compose.traefik.yml`.
- **Everything `502`** — nginx is pointing at stale upstream addresses. `docker
  compose restart nginx`.
- **The embedder restarting in a loop** — it is being killed at its 1.5 GiB
  limit, which on macOS and Windows usually means the Docker VM itself is too
  small. `docker compose logs embedder | grep "Embedding backend ready"` tells
  you whether it ever finished loading.
