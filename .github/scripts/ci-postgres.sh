#!/usr/bin/env bash
#
# A throwaway PostgreSQL for the memory-api integration tests.
#
# Eight memory-api modules (`*_integration.py`) test the board against a real
# database: card links, the eval lock, comments, attachments, search, label
# merges. For a long time nothing in CI gave them one, so they skipped and the
# job stayed green. This script is what gives them one.
#
# WHY HERE AND NOT IN ci-in-container.sh
#
# The tests run inside a container with no Docker socket, so they cannot start
# a sibling. This runs on the RUNNER, beside the `docker run` that starts the
# test container, and the two meet on a per-job Docker network.
#
# WHY NOT A `services:` BLOCK
#
# `services:` is Linux-only, and on the macOS self-hosted runner it does not
# fail, it simply never starts (CI runbook §3.3). A Docker container started
# by hand works the same on that runner and on a GitHub-hosted one.
#
# USAGE (from the repo root, the way the workflows call it):
#
#   bash .github/scripts/ci-postgres.sh up   <suffix>
#   bash .github/scripts/ci-postgres.sh down <suffix>
#
# `up` starts `elefant-ci-pg-<suffix>` on network `elefant-ci-net-<suffix>`,
# waits until it accepts connections, and appends PG_DOCKER_ARGS to
# $GITHUB_ENV (or prints it, run by hand). The test step passes those args to
# its own `docker run`, which puts the test container on the network and sets
# ELEFANT_TEST_PG_DSN. `down` removes both, and is safe to run twice.
#
# The `elefant-ci-` prefix matters: it is the name every CI container on the
# self-hosted runner carries, so the existing cleanup covers this one too.
set -euo pipefail

# The version compose ships (`postgres:16-alpine`), pinned by digest so a tag
# moving under us cannot change what CI tests against. This is the multi-arch
# index, so it resolves natively on both the arm64 Mac and amd64 hosted runners.
PG_IMAGE="docker.io/library/postgres:16-alpine@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea"

action="${1:?usage: ci-postgres.sh <up|down> <suffix>}"
suffix="${2:?usage: ci-postgres.sh <up|down> <suffix>}"
name="elefant-ci-pg-${suffix}"
net="elefant-ci-net-${suffix}"

down() {
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker network rm "$net" >/dev/null 2>&1 || true
}

case "$action" in
  down)
    down
    ;;

  up)
    # A runner that is never wiped can still hold a container or network from
    # a cancelled run. Clear them first rather than failing on the name.
    down
    docker network create "$net" >/dev/null

    # Trust auth: the database lives on a private network for one job and
    # holds nothing. tmpfs: there is nothing to keep, and it is faster.
    docker run -d --name "$name" --network "$net" \
      -e POSTGRES_HOST_AUTH_METHOD=trust \
      --tmpfs /var/lib/postgresql/data \
      "$PG_IMAGE" >/dev/null

    # Probe over TCP, not the socket. The image's entrypoint runs a temporary
    # socket-only server while it initialises, and a socket probe reports that
    # one as ready a moment before it is shut down again.
    for _ in $(seq 1 60); do
      if docker exec "$name" pg_isready -q -h 127.0.0.1 -U postgres; then
        break
      fi
      sleep 1
    done
    if ! docker exec "$name" pg_isready -q -h 127.0.0.1 -U postgres; then
      echo "::error::PostgreSQL ($name) did not become ready in 60s"
      docker logs "$name" 2>&1 | tail -n 40 || true
      exit 1
    fi
    docker exec "$name" psql -U postgres -Atc 'SELECT version()'

    args="--network ${net} -e ELEFANT_TEST_PG_DSN=postgresql://postgres@${name}:5432/postgres"
    if [ -n "${GITHUB_ENV:-}" ]; then
      echo "PG_DOCKER_ARGS=${args}" >> "$GITHUB_ENV"
    else
      echo "PG_DOCKER_ARGS=${args}"
    fi
    ;;

  *)
    echo "unknown action: $action (expected up or down)" >&2
    exit 2
    ;;
esac
