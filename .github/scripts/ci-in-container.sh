#!/usr/bin/env bash
#
# One CI job, run inside the runtime Elefant actually ships.
#
# WHY A CONTAINER RATHER THAN THE RUNNER'S OWN PYTHON
#
# The jobs that used to run here ran on `ubuntu-latest`. Moving them to the
# a macOS self-hosted runner would have moved the test suites from Linux to
# darwin, and the runbook's central warning is that a platform change can
# delete coverage silently — a test that skips exits 0 and reads as green.
# Running in `python:3.11-slim` on linux/arm64 keeps the tests on Linux, on
# the same Python minor the Dockerfiles ship, and on the same architecture
# the deploy host (Debian 12, aarch64) runs. That is CLOSER to production
# than `ubuntu-latest` was, not merely equivalent to it.
#
# It also means the Mac needs no interpreter, no Homebrew formula and no
# virtualenv in the workspace — so the keg-only trap, the `brew upgrade`
# breakage and the venv-in-the-lint-path problem all stop applying.
#
# WHY A FILE RATHER THAN A `run:` BLOCK
#
# So it can be exercised by hand, which is the only way to test a CI change
# without pushing it. Run it the way CI does:
#
#   docker run --rm -i -v "$PWD:/src:ro" -w /work python:3.11-slim \
#     bash -s test editor < .github/scripts/ci-in-container.sh
#
# The node suite is the same shape, in the image that job uses:
#
#   docker run --rm -i -v "$PWD:/src:ro" -w /work node:22-slim \
#     bash -s test-ui < .github/scripts/ci-in-container.sh
#
# Invoked as `bash -s <mode> [service]`, so it arrives on stdin and needs no
# exec bit — no file-mode surprise on the runner.
set -euo pipefail

mode="${1:?usage: ci-in-container.sh <lint|test|test-ui|smoke> [service]}"
service="${2:-}"

# Validate the arguments BEFORE doing any work. A typo'd mode should cost a
# line, not a workspace copy and a pip install — and the failure should name
# the modes that do exist rather than surfacing as `cp: cannot stat '/src/.'`.
case "$mode" in
  lint|smoke|test-ui) ;;
  test) : "${service:?usage: ci-in-container.sh test <service>}" ;;
  *) echo "unknown mode: $mode (expected lint, test, test-ui or smoke)" >&2; exit 2 ;;
esac

# ── Bring the workspace in ───────────────────────────────────────────────────
# /src is mounted READ-ONLY on purpose: this container runs as root, and the
# build job later re-uses the same workspace and does `git clean -ffdx` as the
# runner user. Root-owned leftovers there break a deploy minutes later, in a
# place that looks nothing to do with this job.
#
# The copy is wholesale rather than a list of directories, and that is a
# decision rather than laziness: editor and mcp tests reach across the repo
# (`Path(__file__).parent.parent.parent`) to assert against memory-ui, nginx
# and each other's sources, and several skip themselves with "… is not checked
# out beside the editor" when they cannot find them. A copy list would turn
# those into skips — green, and testing nothing. `.git` comes too: the DCO and
# git-source tests build throwaway repos and shell out to real git.
#
# This is only safe because there is no virtualenv in the workspace to drag in;
# the container approach is what removed it.
cp -a /src/. /work/
cd /work

echo "── $(uname -s) $(uname -m)"

# `test-ui` is the one mode that does not run in a Python image, so the
# interpreter banner and the pip settings below are per-runtime rather than
# unconditional — `python -VV` in the node image would fail here, several
# steps before anything interesting had been attempted.
if [ "$mode" = "test-ui" ]; then
  echo "── node $(node --version)  npm $(npm --version)"
else
  export PIP_DISABLE_PIP_VERSION_CHECK=1
  export PIP_ROOT_USER_ACTION=ignore
  # The image is thrown away at the end of the job, so a wheel cache inside it
  # is a second copy of every download for no benefit.
  PIP="pip install --quiet --no-cache-dir"
  echo "── $(python -VV)"
fi

case "$mode" in

  lint)
    # Undefined names and use-before-assignment: the bugs py_compile cannot
    # catch, and the ones that reach production as a 500 rather than a crash
    # on import. Deliberately not the full ruff rule set — this gate is about
    # correctness, not style.
    $PIP ruff
    ruff check --select F821,F822,F823 --output-format=github \
      editor embedder mcp memory-api
    # The editor and memory-ui images each ship a copy of the shared UI
    # assets (theme, palettes, CSS, logo). Fail fast if the copies drifted.
    python tools/build_ui_assets.py --check
    ;;

  test)
    # git is not in python:3.11-slim, and two suites need a real one:
    # editor/tests/test_dco_check.py builds a throwaway repo to exercise the
    # sign-off trailer parsing, and embedder/tests/test_git_source_disk_state.py
    # does `git init`. Without it they ERROR rather than skip, so this is not a
    # silent dependency — but it is a cheap one to get wrong.
    apt-get -qq update
    apt-get -qq install -y --no-install-recommends git >/dev/null
    git --version

    # Each service is a separate job because every service has its own
    # `main.py`; one pytest process that crossed two of them would collide on
    # the module name (see pytest.ini). The service's own pinned requirements
    # are the same set its Dockerfile installs.
    $PIP -r "${service}/requirements.txt"
    $PIP -r requirements-dev.txt
    pytest "${service}/tests/" -v
    ;;

  test-ui)
    # memory-ui is the one service with no Python in it at all: it ships as a
    # plain nginx copy of static/, and its behaviour lives in ES modules with a
    # node suite. Nothing above this line touches it, which is why a front-end
    # regression used to reach stage green.
    #
    # `bundle-smoke` is the suite that most needs a machine other than the
    # author's: it executes the COMMITTED static/tiptap.bundle.js in jsdom, so
    # it is what catches a vendored bundle that no longer matches the source it
    # was built from — a diff nobody reads and no reviewer would spot.
    #
    # `npm ci` rather than `npm install`, so the run is the lockfile and not
    # whatever resolved today; it is also the exact pair of commands
    # CONTRIBUTING tells a contributor to run locally, so a failure here
    # reproduces by hand with no translation.
    #
    # `notices:check` regenerates THIRD_PARTY_NOTICES.md from what esbuild
    # actually bundles and fails if the committed file differs, or if a
    # bundled package carries a license outside the allow list. A dependency
    # bump that changes the bundle cannot land without its attribution.
    #
    # The rebuild at the end closes the gap bundle-smoke leaves: that suite
    # proves the committed bundle WORKS, not that it is what src/ builds to.
    # A change to src/ committed without `npm run build:editor` ships the old
    # editor while every test passes. The build is deterministic (same
    # lockfile, same esbuild), so it must reproduce the committed file byte
    # for byte. It is compared against the read-only /src checkout rather than
    # with `git diff`, because node:22-slim has no git.
    cd memory-ui
    npm ci --no-audit --no-fund
    npm test
    npm run notices:check
    npm run build:editor
    if ! cmp -s static/tiptap.bundle.js /src/memory-ui/static/tiptap.bundle.js; then
      echo "::error file=memory-ui/static/tiptap.bundle.js::committed bundle does not match what src/ builds to — run 'npm run build:editor' in memory-ui/ and commit static/tiptap.bundle.js"
      exit 1
    fi
    echo "── static/tiptap.bundle.js matches a fresh build of src/"
    # Same idea for the vendored mermaid: it must be exactly the version the
    # lockfile pins, so the notices (which read the lockfile) are true.
    npm run vendor:mermaid -- --check

    # The optional diagram renderer is the other node package. Its unit suite
    # needs no browser, and the license scan reads the lockfile just
    # installed. The browser path itself is proven by scripts/selftest.mjs,
    # which its Dockerfile runs inside the built image.
    #
    # The vendored copy of mermaid in memory-ui and the renderer's mermaid
    # are one version: the wiki and the renderer must draw the same thing.
    cd /work/diagram-renderer
    npm ci --no-audit --no-fund
    npm test
    npm run licenses:check
    want="$(node -p "require('./package.json').dependencies.mermaid")"
    have="$(node -p "require('../memory-ui/package.json').devDependencies.mermaid")"
    if [ "$want" != "$have" ]; then
      echo "::error::diagram-renderer pins mermaid ${want} but memory-ui vendors ${have}; keep them on one version"
      exit 1
    fi

    # The diagram validator: Mermaid's real parser in plain Node, so its whole
    # suite runs here. It must parse with the same mermaid the renderer draws
    # with, and the two share mermaid-errors.mjs byte for byte.
    cd /work/diagram-validator
    npm ci --no-audit --no-fund
    npm test
    npm run licenses:check
    node scripts/selftest.mjs
    have="$(node -p "require('./package.json').dependencies.mermaid")"
    if [ "$want" != "$have" ]; then
      echo "::error::diagram-validator pins mermaid ${have} but diagram-renderer pins ${want}; keep them on one version"
      exit 1
    fi
    if ! cmp -s mermaid-errors.mjs ../diagram-renderer/mermaid-errors.mjs; then
      echo "::error::diagram-validator/mermaid-errors.mjs and diagram-renderer/mermaid-errors.mjs differ; keep them identical"
      exit 1
    fi
    # The shared rich Mermaid fixture (card #413). Each image's selftest needs
    # its own copy, because a build context cannot reach outside the service.
    if ! cmp -s scripts/fixtures/rich-network.mmd ../diagram-renderer/scripts/fixtures/rich-network.mmd; then
      echo "::error::diagram-validator/scripts/fixtures/rich-network.mmd and the diagram-renderer copy differ; keep them identical"
      exit 1
    fi
    ;;

  smoke)
    # Runs against the deployed stage over the public URL, so it needs nothing
    # from the stack except the network.
    $PIP httpx pytest
    pytest tests/smoke.py -m smoke -v
    ;;
esac
