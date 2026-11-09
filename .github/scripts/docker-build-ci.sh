#!/usr/bin/env bash
# Retry the CI image build on "429 Too Many Requests" or "toomanyrequests".
# The Dockerfile ARG is a digest pin on public.ecr.aws. After those attempts
# are spent, rebuild from mirror.gcr.io at the same digest.
# Railway builds deploy/Dockerfile.mcp and does not use this script.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/../.."

DOCKERFILE="${UMBRA_DOCKERFILE:-deploy/Dockerfile}"
IMAGE_TAG="${UMBRA_IMAGE_TAG:-umbra-py:ci}"
# One digest pin, the ARG PYTHON_IMAGE line. Both Dockerfiles carry the same
# pin; this reads the file actually being built.
python_image="$(sed -n 's/^ARG PYTHON_IMAGE=//p' "$DOCKERFILE" | head -n 1)"
digest="${python_image##*@}"
case "$digest" in
  sha256:*) ;;
  *)
    echo "PYTHON_IMAGE in ${DOCKERFILE} is not pinned by digest" >&2
    exit 1
    ;;
esac
MIRROR="${UMBRA_MIRROR_IMAGE:-mirror.gcr.io/library/python:3.12-slim@${digest}}"
ECR_ATTEMPTS="${UMBRA_ECR_ATTEMPTS:-5}"
MIRROR_ATTEMPTS="${UMBRA_MIRROR_ATTEMPTS:-3}"
BACKOFF="${UMBRA_BACKOFF_SECONDS:-15}"

# The self-host image takes UMBRA_EXTRAS. The hosted Dockerfile hardcodes its
# extras, so that job sets UMBRA_SKIP_EXTRAS_ARG=1.
extras_args=()
if [ "${UMBRA_SKIP_EXTRAS_ARG:-0}" != "1" ]; then
  extras_args=(--build-arg UMBRA_EXTRAS=serve)
fi

retry_build() { # $1 attempts, $2 optional PYTHON_IMAGE
  local attempts="$1" image="${2:-}" attempt=1 status log
  local -a extra=()
  [ -n "$image" ] && extra=(--build-arg "PYTHON_IMAGE=${image}")
  while [ "$attempt" -le "$attempts" ]; do
    log="$(mktemp)"
    set +e
    docker build -f "$DOCKERFILE" "${extras_args[@]}" \
      "${extra[@]}" -t "$IMAGE_TAG" . 2>&1 | tee "$log"
    status=${PIPESTATUS[0]}
    set -e
    if [ "$status" -eq 0 ]; then rm -f "$log"; return 0; fi
    if grep -Eq 'toomanyrequests|429 Too Many Requests' "$log"; then
      rm -f "$log"
      echo "base image rate-limited (attempt ${attempt}/${attempts})" >&2
      [ "$attempt" -lt "$attempts" ] && sleep $((attempt * BACKOFF))
      attempt=$((attempt + 1))
      continue
    fi
    rm -f "$log"
    return "$status"
  done
  return 10
}

retry_build "$ECR_ATTEMPTS" && exit 0
status=$?; [ "$status" -eq 10 ] || exit "$status"
echo "ECR Public still rate-limiting; building from ${MIRROR}" >&2
retry_build "$MIRROR_ATTEMPTS" "$MIRROR" && exit 0
status=$?; [ "$status" -eq 10 ] || exit "$status"
echo "Both public.ecr.aws and mirror.gcr.io rate-limited the python base image" >&2
exit 1
