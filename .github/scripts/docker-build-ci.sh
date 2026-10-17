#!/usr/bin/env bash
# Retry the CI image build on "429 Too Many Requests" or "toomanyrequests".
# Dockerfiles unchanged; Railway builds deploy/Dockerfile.mcp, so production
# is unaffected.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")/../.."
MIRROR="${UMBRA_MIRROR_IMAGE:-mirror.gcr.io/library/python:3.12-slim}"
ECR_ATTEMPTS="${UMBRA_ECR_ATTEMPTS:-5}"
MIRROR_ATTEMPTS="${UMBRA_MIRROR_ATTEMPTS:-3}"
BACKOFF="${UMBRA_BACKOFF_SECONDS:-15}"

retry_build() { # $1 attempts, $2 optional PYTHON_IMAGE
  local attempts="$1" image="${2:-}" attempt=1 status log
  local -a extra=()
  [ -n "$image" ] && extra=(--build-arg "PYTHON_IMAGE=${image}")
  while [ "$attempt" -le "$attempts" ]; do
    log="$(mktemp)"
    set +e
    docker build -f deploy/Dockerfile --build-arg UMBRA_EXTRAS=serve \
      "${extra[@]}" -t umbra-py:ci . 2>&1 | tee "$log"
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
