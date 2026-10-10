#!/usr/bin/env bash
#
# Run the install instructions we publish, as published.
#
# The README's two quick starts are the whole install path for anybody who
# arrives from the release video. A README is also exactly the kind of file that
# drifts: nothing fails when the manifests move and the copy-paste block does
# not. So the blocks are marked in the markdown, extracted by
# `tools/doc_blocks.py`, and executed here for real — Compose in a clean project,
# Kubernetes against a throwaway k3d cluster.
#
# WHAT THIS CHANGES ABOUT THE PUBLISHED COMMANDS
#
# Three substitutions, all the same idea — point the docs at THIS commit instead
# of at a published release, because a release of this commit does not exist yet:
#
#   1. `git clone https://github.com/zennthic/elefant.git` + `cd elefant`
#      becomes a copy of this checkout.
#   2. the remote kustomize base `...//deploy/k8s/base?ref=vX.Y.Z` becomes this
#      checkout's `deploy/k8s/base`, copied in beside the kustomization (kustomize
#      refuses a resource outside its own root, so it has to be a subdirectory).
#   3. `ELEFANT_TAG=vX.Y.Z` becomes `ELEFANT_TAG=latest` — the tag that exists on
#      every commit. What is under test is that the pin is read and resolves, not
#      the string.
#
# Every substitution is printed as a diff before the block runs, so the job log
# shows exactly what executed next to what the README says.
#
# Nothing else is touched. In particular the harness does NOT edit the block to
# add a port, a project name or a pull secret:
#
#   * ports and the Compose project name are passed in the ENVIRONMENT, which
#     Compose reads ahead of `.env`, so the block is unmodified;
#   * the k3d cluster is given a ghcr.io credential in its own
#     `registries.yaml` at creation. That is a property of the throwaway cluster,
#     not of the install path — the published path needs no pull secret, and
#     `deploy/k8s/base` deliberately ships none. It exists only because the six
#     GHCR packages are not public yet. DELETE the
#     --registry-config plumbing below once they are: after that the pull is
#     anonymous, and leaving the credential in place would hide the day it
#     stopped being needed.
#
# WHY A FILE RATHER THAN A `run:` BLOCK
#
# So it can be exercised by hand, which is the only way to test a CI change
# without pushing it:
#
#   bash .github/scripts/test-install-docs.sh compose
#   bash .github/scripts/test-install-docs.sh k8s
#
# Needs on the machine: docker (with the Compose v2 plugin), curl, tar. kubectl
# and k3d are downloaded into a scratch directory rather than installed, so the
# runner keeps nothing afterwards.
set -euo pipefail

mode="${1:?usage: test-install-docs.sh <compose|k8s>}"
case "$mode" in
  compose|k8s) ;;
  *) echo "unknown mode: $mode (expected compose or k8s)" >&2; exit 2 ;;
esac

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK="${DOCS_TEST_WORKDIR:-${TMPDIR:-/tmp}/elefant-docs-test-$$}"
BIN="$WORK/bin"
PROJECT="elefant-docstest-$$"
CLUSTER="elefant-docs-$$"

# Ports well away from anything a runner might already be publishing (the Stage
# uses 8321/8322, and a runner can host more than one job).
HOST_PORT="${DOCS_TEST_PORT:-18321}"
HOST_MCP_PORT="${DOCS_TEST_MCP_PORT:-18322}"

# Pinned deliberately. An unpinned `curl | bash` installer would make this job's
# behaviour depend on the day it ran, which is the failure this whole file exists
# to prevent. Bump them on purpose.
K3D_VERSION="v5.7.4"
KUBECTL_VERSION="v1.31.0"

# The extraction runs in a container so the runner needs no Python. Same reason
# as .github/scripts/ci-in-container.sh, and the same image.
EXTRACT_IMAGE="python:3.11.16-slim-trixie"

mkdir -p "$WORK" "$BIN"
export PATH="$BIN:$PATH"

say() { printf '\n── %s\n' "$*"; }

# ── Extraction ───────────────────────────────────────────────────────────────

block() {
  docker run --rm --name "docs-extract-$mode-$$-$1" -v "$SRC:/src:ro" -w /src \
    "$EXTRACT_IMAGE" python tools/doc_blocks.py get "$1"
}

# The three substitutions, in one place so the diff below is the whole truth.
localise() {
  sed \
    -e "s#^git clone https://github.com/zennthic/elefant\.git\$#tar -C '$SRC' --exclude=./.git -cf - . | tar -xf -#" \
    -e "s#^cd elefant\$#: already here — see test-install-docs.sh#" \
    -e "s#https://github\.com/zennthic/elefant//deploy/k8s/base?ref=vX\.Y\.Z#./base#" \
    -e "s#ELEFANT_TAG=vX\.Y\.Z#ELEFANT_TAG=latest#"
}

# Writes <tag> to $1, localised, and shows what changed.
materialise() {
  local tag="$1" dest="$2"
  block "$tag" > "$WORK/$tag.published"
  localise < "$WORK/$tag.published" > "$dest"
  say "block '$tag' — published vs what runs here"
  if diff -u "$WORK/$tag.published" "$dest" --label "README ($tag)" --label "running"; then
    echo "   (identical — no substitution applied)"
  fi
}

# ── Checks ───────────────────────────────────────────────────────────────────

wait_for_http() {
  local url="$1" deadline=$(( SECONDS + ${2:-300} )) code=""
  say "waiting for $url"
  while (( SECONDS < deadline )); do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$url" || true)"
    case "$code" in
      200|302|303) echo "   $code after $SECONDS s"; return 0 ;;
    esac
    sleep 5
  done
  echo "   still $code after ${2:-300}s" >&2
  return 1
}

check_install() {
  local base="$1" body code
  say "checking a fresh install at $base"

  code="$(curl -s -o "$WORK/setup.html" -w '%{http_code}' --max-time 20 "$base/setup")"
  [ "$code" = "200" ] || { echo "GET /setup answered $code, expected 200" >&2; return 1; }
  echo "   /setup 200 ($(wc -c < "$WORK/setup.html" | tr -d ' ') bytes)"

  code="$(curl -s -o "$WORK/oauth.json" -w '%{http_code}' --max-time 20 \
    "$base/.well-known/oauth-authorization-server")"
  [ "$code" = "200" ] || {
    echo "GET /.well-known/oauth-authorization-server answered $code" >&2; return 1; }
  body="$(cat "$WORK/oauth.json")"
  case "$body" in
    *'"issuer"'*'"authorization_endpoint"'*) ;;
    *) echo "oauth metadata is not the expected JSON: ${body:0:200}" >&2; return 1 ;;
  esac
  echo "   /.well-known/oauth-authorization-server is JSON, issuer present"

  # The login page is what a first visitor actually lands on, and on a fresh
  # install it redirects to /setup. Either answer proves the auth surface is up.
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$base/login")"
  case "$code" in
    200|302|303) echo "   /login $code" ;;
    *) echo "GET /login answered $code" >&2; return 1 ;;
  esac
}

# ── Mode: Docker Compose ─────────────────────────────────────────────────────

compose_cmd() {
  docker compose -p "$PROJECT" -f "$WORK/repo/docker-compose.yml" "$@"
}

compose_teardown() {
  say "tearing down the Compose project"
  # -v as well: the volumes are this run's, and a docs test that left 63 GiB of
  # them behind on every push would fill the runner within a week.
  compose_cmd down -v --remove-orphans --timeout 30 2>&1 | tail -20 || true
  rm -rf "$WORK"
}

run_compose() {
  mkdir -p "$WORK/repo"
  materialise compose-quickstart "$WORK/quickstart.sh"

  trap compose_teardown EXIT

  say "running the Compose quick start in a clean project ($PROJECT)"
  (
    cd "$WORK/repo"
    # Compose reads the process environment ahead of .env, so these steer the
    # run without the block being edited. COMPOSE_PROJECT_NAME keeps the
    # containers, network and volumes clear of anything else on the runner.
    export COMPOSE_PROJECT_NAME="$PROJECT"
    export PORT="$HOST_PORT"
    export MCP_PORT="$HOST_MCP_PORT"
    bash -euxo pipefail "$WORK/quickstart.sh"
  )

  say "services"
  compose_cmd ps

  # The quick start says `--pull always`, and if a pull fails Compose will
  # quietly BUILD from the clone instead — which works, and would mean this job
  # proved the docs against images nobody else can get. A pulled image carries a
  # RepoDigest; a locally built one does not.
  say "every Elefant image was pulled, not built locally"
  local img digests built=0
  for img in $(compose_cmd config --images | grep '^ghcr.io/zennthic/elefant/' | sort -u); do
    digests="$(docker image inspect --format '{{len .RepoDigests}}' "$img" 2>/dev/null || echo 0)"
    if [ "$digests" = "0" ]; then
      echo "   $img has no RepoDigest — it was built here, not pulled" >&2
      built=1
    else
      echo "   $img pulled"
    fi
  done
  [ "$built" = "0" ] || {
    echo "the published images could not be pulled; check the GHCR package visibility" >&2
    return 1
  }

  if ! wait_for_http "http://localhost:$HOST_PORT/setup" 420; then
    say "it never came up — logs"
    compose_cmd ps
    compose_cmd logs --tail 80 nginx editor postgres || true
    return 1
  fi
  check_install "http://localhost:$HOST_PORT"
  say "Compose quick start: OK"
}

# ── Mode: Kubernetes ─────────────────────────────────────────────────────────

fetch_tools() {
  local os arch
  case "$(uname -s)" in
    Darwin) os=darwin ;;
    Linux)  os=linux ;;
    *) echo "unsupported OS: $(uname -s)" >&2; return 1 ;;
  esac
  case "$(uname -m)" in
    arm64|aarch64) arch=arm64 ;;
    x86_64|amd64)  arch=amd64 ;;
    *) echo "unsupported architecture: $(uname -m)" >&2; return 1 ;;
  esac

  say "fetching kubectl $KUBECTL_VERSION and k3d $K3D_VERSION ($os/$arch) into $BIN"
  curl -fsSL -o "$BIN/kubectl" \
    "https://dl.k8s.io/release/$KUBECTL_VERSION/bin/$os/$arch/kubectl"
  curl -fsSL -o "$BIN/k3d" \
    "https://github.com/k3d-io/k3d/releases/download/$K3D_VERSION/k3d-$os-$arch"
  chmod +x "$BIN/kubectl" "$BIN/k3d"
  kubectl version --client=true 2>/dev/null | head -1 || kubectl version --client
  k3d version | head -2
}

# The kustomization the README tells you to write, beside a copy of the base it
# would otherwise fetch by URL.
build_kustomization() {
  mkdir -p "$WORK/my-elefant/base"
  cp "$SRC"/deploy/k8s/base/*.yaml "$WORK/my-elefant/base/"
  materialise k8s-kustomization "$WORK/my-elefant/kustomization.yaml"
  materialise k8s-quickstart "$WORK/my-elefant/quickstart.sh"
  say "the kustomization as it will be applied"
  cat "$WORK/my-elefant/kustomization.yaml"
}

# Render before building a cluster. The mistake this catches is the quiet one: a
# `behavior: merge` against a name the base does not generate produces a SECOND
# ConfigMap, the pods keep reading the first, and the reader's domain is ignored
# with nothing failing. Two seconds here beats five minutes and a healthy-looking
# stack serving the wrong hostname.
check_render() {
  local out="$WORK/rendered.yaml" names
  say "rendering the kustomization"
  kubectl kustomize "$WORK/my-elefant" > "$out"
  echo "   $(grep -c '^kind:' "$out") resources"

  names="$(grep -o 'elefant-config-[a-z0-9]*' "$out" | sort -u)"
  if [ "$(echo "$names" | grep -c .)" != "1" ]; then
    echo "expected ONE generated elefant-config ConfigMap, got:" >&2
    echo "$names" >&2
    echo "the README's configMapGenerator is not merging into the base's" >&2
    return 1
  fi
  echo "   one ConfigMap, $names"

  grep -q "ELEFANT_PUBLIC_DOMAIN: elefant.example.com" "$out" || {
    echo "the domain from the README's kustomization is not in the render" >&2
    grep -n "ELEFANT_PUBLIC_DOMAIN" "$out" >&2 || true
    return 1
  }
  echo "   ELEFANT_PUBLIC_DOMAIN carries the reader's value"

  # Both are promises the install guide makes about the base.
  ! grep -q "imagePullSecrets" "$out" || {
    echo "the render carries an imagePullSecret; the docs say the images are public" >&2
    return 1
  }
  ! grep -q "storageClassName" "$out" || {
    echo "the render names a storageClassName; the docs say the cluster default is used" >&2
    return 1
  }
  echo "   no imagePullSecret, no storageClassName"
}

k8s_teardown() {
  if [ -n "${KUBECONFIG:-}" ] && [ -f "${KUBECONFIG:-}" ]; then
    say "final state"
    kubectl -n elefant get pods 2>&1 | tail -20 || true
  fi
  say "deleting the k3d cluster"
  k3d cluster delete "$CLUSTER" 2>&1 | tail -5 || true
  rm -rf "$WORK"
}

run_k8s() {
  fetch_tools
  trap k8s_teardown EXIT

  build_kustomization
  check_render

  # TEMPORARY, and the only thing here that the published install path does not
  # have: a ghcr.io credential in the cluster's own containerd config, because
  # the six packages are still private. Delete this block, and the
  # --registry-config flag below, the day they go public — anonymous pulls are
  # what the README promises, and a credential sitting here would mean nobody
  # ever finds out it is not true.
  if [ -n "${GHCR_USERNAME:-}" ] && [ -n "${GHCR_TOKEN:-}" ]; then
    cat > "$WORK/registries.yaml" <<YAML
configs:
  ghcr.io:
    auth:
      username: ${GHCR_USERNAME}
      password: ${GHCR_TOKEN}
YAML
    say "the cluster is given a ghcr.io credential (packages are private)"
  else
    # Always written, so the flag below is unconditional. An empty array
    # expanded under `set -u` is an error in bash 3.2, which macOS still ships.
    echo 'configs: {}' > "$WORK/registries.yaml"
    say "no GHCR credential passed — pulling anonymously, which needs the packages public"
  fi
  chmod 600 "$WORK/registries.yaml"

  export KUBECONFIG="$WORK/kubeconfig"
  say "creating the k3d cluster $CLUSTER"
  # No Traefik and no ServiceLB: the documented path reaches the stack with
  # `port-forward`, so an ingress controller in the cluster would be an
  # assumption the base explicitly does not make. Keeping them out also leaves
  # the node's memory for Elefant.
  k3d cluster create "$CLUSTER" \
    --wait --timeout 300s \
    --k3s-arg "--disable=traefik@server:0" \
    --k3s-arg "--disable=servicelb@server:0" \
    --kubeconfig-update-default=false \
    --kubeconfig-switch-context=false \
    --registry-config "$WORK/registries.yaml"
  k3d kubeconfig get "$CLUSTER" > "$KUBECONFIG"
  chmod 600 "$KUBECONFIG"

  kubectl get nodes
  say "storage classes (the base needs a default one, and names none)"
  kubectl get storageclass

  say "running the Kubernetes quick start"
  (
    cd "$WORK/my-elefant"
    bash -euxo pipefail ./quickstart.sh
  ) || {
    say "the quick start failed — cluster state"
    kubectl -n elefant get pods -o wide || true
    kubectl -n elefant get events --sort-by=.lastTimestamp | tail -30 || true
    return 1
  }

  # The editor is what answers /setup, and it is not in the block's
  # `rollout status` because the block waits for the front door. Wait for it
  # here rather than letting the HTTP check time out on a pod that is still
  # pulling.
  kubectl -n elefant rollout status deploy/editor --timeout=10m || {
    kubectl -n elefant describe deploy/editor | tail -30
    kubectl -n elefant logs deploy/editor --tail=50 || true
    return 1
  }

  # The one command the harness runs on the docs' behalf rather than from a
  # block: `kubectl port-forward` never returns, so it cannot be a line in a
  # script the harness waits on. It is the same command the README gives.
  say "port-forward svc/nginx $HOST_PORT:80"
  kubectl -n elefant port-forward svc/nginx "$HOST_PORT:80" >"$WORK/pf.log" 2>&1 &
  local pf=$!
  # shellcheck disable=SC2064
  trap "kill $pf 2>/dev/null || true; k8s_teardown" EXIT

  if ! wait_for_http "http://localhost:$HOST_PORT/setup" 420; then
    say "it never came up"
    cat "$WORK/pf.log" || true
    kubectl -n elefant get pods -o wide || true
    kubectl -n elefant get events --sort-by=.lastTimestamp | tail -30 || true
    kubectl -n elefant logs deploy/nginx --tail=40 || true
    kubectl -n elefant logs deploy/editor --tail=40 || true
    return 1
  fi
  check_install "http://localhost:$HOST_PORT"
  say "Kubernetes quick start: OK"
}

# ── ───────────────────────────────────────────────────────────────────────────

say "$(uname -s) $(uname -m) · docker $(docker --version | awk '{print $3}' | tr -d ,)"
case "$mode" in
  compose) run_compose ;;
  k8s)     run_k8s ;;
esac
