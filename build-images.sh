#!/usr/bin/env bash
# Build (and optionally push) both containerproxy images.
#
#   ./build-images.sh            build only
#   ./build-images.sh --push     build and push
#
# Tags: :<git short sha> and :latest (plus :<tag> when HEAD is a git tag).
# REGISTRY overrides the image prefix.
set -euo pipefail

REGISTRY=${REGISTRY:-containerproxy}
PUSH=false
[[ ${1:-} == --push ]] && PUSH=true

cd "$(dirname "$0")"

SHA=$(git rev-parse --short HEAD)
TAGS=("$SHA" latest)
if GIT_TAG=$(git describe --tags --exact-match 2>/dev/null); then
  TAGS+=("$GIT_TAG")
fi
[[ -n $(git status --porcelain) ]] && echo "warning: uncommitted changes are included in this build" >&2

# image name | Dockerfile | build context
# The UI MUST be built from webapp/: from the repo root it would package
# the proxy instead and fail with "No module named containerproxy_ui".
BUILDS=(
  "$REGISTRY|docker/Dockerfile|."
  "$REGISTRY/ui|docker/Dockerfile.ui|webapp"
)

for build in "${BUILDS[@]}"; do
  IFS='|' read -r image dockerfile context <<<"$build"
  tag_args=()
  for t in "${TAGS[@]}"; do tag_args+=(--tag "$image:$t"); done

  echo "==> $image (${TAGS[*]})"
  docker build \
    --provenance=false \
    --pull \
    --label "org.opencontainers.image.revision=$(git rev-parse HEAD)" \
    --file "$dockerfile" \
    "${tag_args[@]}" \
    "$context"

  if $PUSH; then
    for t in "${TAGS[@]}"; do docker push "$image:$t"; done
  fi
done

$PUSH || echo "Built only. Run with --push to publish."
