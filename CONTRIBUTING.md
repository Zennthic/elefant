# Contributing to Elefant

Thanks for your interest in improving Elefant.

## Development setup

Elefant runs as a set of containers orchestrated by Docker Compose. Start from
the README's [Docker Compose quick start](README.md#docker-compose): it writes
`.env` with the secrets the stack needs. For development, leave `ELEFANT_TAG`
unset (it defaults to `latest`, the most recent build of `main`):

```bash
cp .env.example .env
{
  echo "ELEFANT_SESSION_SECRET=$(openssl rand -hex 32)"
  echo "ELEFANT_INTERNAL_SERVICE_TOKEN=$(openssl rand -hex 32)"
  echo "POSTGRES_PASSWORD=$(openssl rand -hex 24)"
  echo "ELEFANT_S3_ACCESS_KEY=$(openssl rand -hex 12)"
  echo "ELEFANT_S3_SECRET_KEY=$(openssl rand -hex 24)"
} >> .env
docker compose up -d
```

Open **http://localhost:8321/setup** and complete the first-run flow. See the
[README](README.md) for the service map and
[`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) for common failure modes.

### Running your changes

`docker compose up -d` runs the published images. It does not build your
working tree. To run a change, build the service you changed and recreate it:

```bash
docker compose build editor && docker compose up -d editor
```

The services that build from this repository are `nginx`, `editor`,
`embedder`, `memory-api`, `memory-ui` and `mcp`. A local build is tagged with
the published image's name, so `docker compose up -d --pull always` replaces
it with the published image again.

## Running the tests

The Python services each have their own suite; one script runs them all:

```bash
./scripts/run-tests.sh            # all services
./scripts/run-tests.sh editor     # a single service
```

The web editor (`memory-ui`) has a JavaScript suite:

```bash
cd memory-ui && npm ci && npm test
```

### The PostgreSQL integration tests

Eight `memory-api` modules (`memory-api/tests/*_integration.py`) run the board
against a real PostgreSQL: card links, the eval lock, comments, attachments,
search and label merges. They need a database named by `ELEFANT_TEST_PG_DSN`.
Without one they skip on your machine, so the rest of the suite still runs.

To run them, start a throwaway PostgreSQL 16 with either Docker:

```bash
docker run -d --rm --name elefant-test-pg -p 5599:5432 \
  -e POSTGRES_HOST_AUTH_METHOD=trust postgres:16-alpine
export ELEFANT_TEST_PG_DSN="postgresql://postgres@127.0.0.1:5599/postgres"
pytest memory-api/tests/
```

or a local PostgreSQL install:

```bash
initdb -D /tmp/pg -U postgres --auth=trust
pg_ctl -D /tmp/pg -o "-p 5599 -k /tmp" start
export ELEFANT_TEST_PG_DSN="postgresql://postgres@/postgres?host=/tmp&port=5599"
pytest memory-api/tests/
```

Each test works in a freshly named tenant, so the database can be reused between runs.

CI always runs them. When `CI=true`, an unset or unreachable
`ELEFANT_TEST_PG_DSN` fails these modules instead of skipping them, so a green
run cannot hide that they never ran. CI starts the database with
`.github/scripts/ci-postgres.sh`.

### What CI runs

CI runs the same suites — the per-service Python ones and the `memory-ui` one —
plus a lint pass (`ruff`, Pyflakes rules). On `main` it gates build + deploy
on them; on a pull request they run as checks (see below).

## Pull requests

- Branch from `main` and open your PR against `main`.
- A PR is checked by two workflows, both on GitHub-hosted runners, so a PR from
  a fork is checked too:
  - **PR checks** (`.github/workflows/pr-checks.yml`): the `ruff` lint, the
    `editor`, `embedder`, `memory-api` and `mcp` suites, and the `memory-ui`
    suite with its third-party-notices and bundle-drift checks. It runs the
    same script and images as `main`, so a failure reproduces locally with the
    `docker run` line at the top of `.github/scripts/ci-in-container.sh`.
  - **DCO**: every commit carries a `Signed-off-by` trailer (see below).
- Keep a change focused. A change that spans several services should stay in one
  commit so CI rebuilds every affected service together.
- Add or update tests for the behaviour you change — especially around
  authentication, tenant isolation, and the MCP surface.
- Make sure `./scripts/run-tests.sh` and the lint pass are green locally, and
  `npm test` in `memory-ui` if you touched the web editor. If you changed
  anything under `memory-ui/src/`, also run `npm run build:editor` and commit
  the rebuilt `memory-ui/static/tiptap.bundle.js`: CI rebuilds it and fails if
  the committed bundle differs.
- Sign every commit off with `git commit -s`. That trailer certifies the
  [Developer Certificate of Origin](https://developercertificate.org/): that you
  wrote the code, or otherwise have the right to submit it under the project's
  Apache-2.0 license. CI checks every commit in the PR and fails with the fix if
  one is missing — `git rebase --signoff origin/main` repairs a whole branch,
  and `git config --global format.signOff true` makes it the default. Apache-2.0
  §5 already licenses what you submit under the project license, so there is no
  separate CLA to sign; the sign-off is about your right to submit it at all.

## Third-party licenses

Elefant ships some third-party JavaScript inside its own tree: the vendored
`memory-ui/static/marked.min.js` and the Tiptap/ProseMirror bundle in
`memory-ui/static/tiptap.bundle.js`. Their attribution lives in
`THIRD_PARTY_NOTICES.md`, which is generated, not written by hand:

```bash
cd memory-ui && npm run notices          # regenerate after a dependency change
cd memory-ui && npm run notices:check    # what CI runs
```

The list comes from esbuild's metafile, so it names exactly what the bundle
contains. CI fails when the committed file is out of date, and when a bundled
package carries a license outside the allow list in
`memory-ui/scripts/third-party-notices.mjs` (MIT, ISC, BSD-2-Clause,
BSD-3-Clause, Apache-2.0, 0BSD). A new license needs a human decision, so do
not just add it to the list. If a package ships no license file, commit its
upstream text under `memory-ui/licenses/`. Keep the upstream header at the top
of a vendored file when you update it.

Python dependencies are installed, not vendored, so they need no entry in
`NOTICE`, but they must stay compatible with Apache-2.0. To check a service:

```bash
python -m venv /tmp/lic && /tmp/lic/bin/pip install -r <service>/requirements.txt pip-licenses
/tmp/lic/bin/pip-licenses --from=mixed
```

The last check (2026-09-29, all services, direct and transitive runtime
dependencies) found nothing incompatible with Apache-2.0. Notes on the
non-permissive or unusual ones:

- `psycopg2-binary` (editor, memory-api) is LGPL-3.0 with an OpenSSL exception.
  Elefant imports it unmodified as a separate library, which the LGPL allows.
- `certifi` and `tqdm` are MPL-2.0 (file-level copyleft). Used unmodified, so
  there is no obligation beyond keeping their notices.
- `qrcode` (editor) is BSD. Its "Other/Proprietary" classifier refers to the
  QR Code trademark notice, not to a code license.
- The embedder's local backend (`requirements-local.txt`) is
  sentence-transformers and transformers (Apache-2.0) and the CPU-only torch
  build (BSD-3-Clause and Apache-2.0). The Dockerfile refuses the CUDA build,
  so no NVIDIA binaries ship in the image.

## Reporting security issues

Do not open a public issue. Follow [`SECURITY.md`](SECURITY.md).
