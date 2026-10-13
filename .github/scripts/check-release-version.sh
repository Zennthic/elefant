#!/usr/bin/env bash
#
# Does the git tag agree with the version the manifests ship?
#
# `deploy/k8s/base/kustomization.yaml` carries an `images:` block with a
# `newTag` per service, and that is what a stranger applies. If the repo is
# tagged `v1.2.3` while the manifests still say `v1.2.2`, then `kubectl apply
# -k` at the tag installs the WRONG version — silently, and only for the people
# who are not us.
#
# So the release process is: commit "release: vX.Y.Z" bumping newTag, then tag
# THAT commit. This is what refuses to release when the two disagree.
#
# A FILE rather than a `run:` block, for the same reason as
# ci-in-container.sh: it can be exercised without pushing a tag. That is what
# editor/tests/test_release_version.py does, and how "the version check fails a
# deliberately mismatched tag" was demonstrated rather than assumed.
#
#   ./check-release-version.sh v1.2.3 [path/to/kustomization.yaml]
#
# Invoked as `bash check-release-version.sh …`, so it needs no exec bit.
set -euo pipefail

TAG="${1:?usage: check-release-version.sh <vX.Y.Z> [kustomization.yaml]}"
KUSTOMIZATION="${2:-deploy/k8s/base/kustomization.yaml}"

if [[ ! "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$ ]]; then
  echo "::error::'${TAG}' is not a release tag. Expected vX.Y.Z, optionally" \
       "with a prerelease suffix (v1.2.3-rc1)." >&2
  exit 1
fi

[ -f "$KUSTOMIZATION" ] || { echo "::error::no $KUSTOMIZATION" >&2; exit 1; }

# Every Elefant image in the block has to carry the tag, not just the first
# one. A partial bump is the failure this is most likely to meet in practice:
# somebody edits five of six by hand.
MISMATCHED=0
FOUND=0
SERVICE=""
while IFS= read -r line; do
  case "$line" in
    *"- name: ghcr.io/zennthic/elefant/"*)
      SERVICE="${line##*/}"
      ;;
    *newTag:*)
      [ -n "$SERVICE" ] || continue
      GOT="$(echo "$line" | sed 's/.*newTag:[[:space:]]*//' | tr -d '"'"'"' \r')"
      FOUND=$((FOUND + 1))
      if [ "$GOT" != "$TAG" ]; then
        echo "::error::${SERVICE} is pinned to '${GOT}' but the tag is '${TAG}'" >&2
        MISMATCHED=$((MISMATCHED + 1))
      else
        echo "  ${SERVICE}: ${GOT}"
      fi
      SERVICE=""
      ;;
  esac
done < "$KUSTOMIZATION"

if [ "$FOUND" -eq 0 ]; then
  echo "::error::no ghcr.io/zennthic/elefant images found in ${KUSTOMIZATION}." \
       "Has the images: block been renamed?" >&2
  exit 1
fi

if [ "$FOUND" -ne 8 ]; then
  echo "::error::found ${FOUND} pinned images, expected 8 (editor, embedder," \
       "mcp, memory-api, memory-ui, nginx, diagram-renderer, diagram-validator)." >&2
  exit 1
fi

if [ "$MISMATCHED" -gt 0 ]; then
  echo "::error::${MISMATCHED} image(s) disagree with the tag. Bump newTag in" \
       "${KUSTOMIZATION}, commit it, and tag THAT commit." >&2
  exit 1
fi

echo "All ${FOUND} images agree with ${TAG}."
