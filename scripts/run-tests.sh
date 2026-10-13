#!/usr/bin/env bash
# Local convenience wrapper for the test suite.  CI runs the same per-
# service pytest commands but in parallel jobs with a dedicated venv per
# service.  Locally we share one venv because each service's deps are
# compatible (the same fastapi, sqlalchemy, etc.) — but we still
# need per-service pytest invocations because each service's main.py
# collides on the Python module name `main`.
#
# Usage:
#   ./scripts/run-tests.sh                 # all services
#   ./scripts/run-tests.sh editor          # one service
#   ./scripts/run-tests.sh smoke           # post-deploy smoke (needs BASE_URL)
set -euo pipefail

cd "$(dirname "$0")/.."

VENV=${VENV:-/tmp/elefant-venv}
if [ ! -d "$VENV" ]; then
    echo "==> creating venv at $VENV (one-time, takes ~2 min)"
    python3 -m venv "$VENV"
    # shellcheck source=/dev/null
    source "$VENV/bin/activate"
    pip install -q --upgrade pip
    pip install -q -r requirements-dev.txt
    for svc in editor embedder memory-api mcp; do
        pip install -q -r "$svc/requirements.txt"
    done
else
    # shellcheck source=/dev/null
    source "$VENV/bin/activate"
fi

SERVICES=("$@")
if [ ${#SERVICES[@]} -eq 0 ]; then
    SERVICES=(editor embedder memory-api mcp)
fi

FAIL=0
for svc in "${SERVICES[@]}"; do
    if [ "$svc" = "smoke" ]; then
        if [ -z "${BASE_URL:-}" ]; then
            echo "error: smoke needs BASE_URL set to the deployment under test, e.g. BASE_URL=https://elefant.example.com" >&2
            FAIL=1
            continue
        fi
        echo "==> running smoke (BASE_URL=$BASE_URL)"
        pytest tests/smoke.py -m smoke -v || FAIL=1
        continue
    fi
    echo "==> $svc"
    pytest "$svc/tests/" -q || FAIL=1
done

exit "$FAIL"
