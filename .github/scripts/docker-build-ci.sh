#!/usr/bin/env bash
# Build the self-host image on GitHub Actions without a new registry secret.
#
# public.ecr.aws anonymous pulls share a per-IP data quota. When it is spent,
# BuildKit fails in under a second on "load metadata" with
# "429 Too Many Requests - toomanyrequests: Data limit exceeded". A warm
# layer cache does not skip that metadata request, so this retries the build
# (which is the pull) and, if the quota is still spent, retries it against
# mirror.gcr.io via the existing PYTHON_IMAGE build-arg. The Dockerfile
# default stays public.ecr.aws/docker/library/python:3.12-slim, which is
# what Railway builds.
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")/../.."

MIRROR_IMAGE="${UMBRA_MIRROR_IMAGE:-mirror.gcr.io/library/python:3.12-slim}"
ECR_ATTEMPTS="${UMBRA_ECR_ATTEMPTS:-5}"
MIRROR_ATTEMPTS="${UMBRA_MIRROR_ATTEMPTS:-3}"
BACKOFF="${UMBRA_BACKOFF_SECONDS:-15}"

cache_args=(
  --file deploy/Dockerfile
  --build-arg UMBRA_EXTRAS=serve
  --tag umbra-py:ci
  --load
  --provenance=false
  --cache-from type=gha
  --cache-to "type=gha,mode=max,ignore-error=true"
)

rate_limited() {
  grep -Eq '429|toomanyrequests|Too Many Requests|Data limit exceeded' "$1"
}

# Run a docker command. Return 0 on success, 10 when the registry rate-limits
# the base image, or the command's own status for any other failure.
# `||` keeps a non-zero docker status from aborting under `set -e`.
run_logged() {
  local log status=0
  log="$(mktemp)"
  "$@" >"$log" 2>&1 || status=$?
  cat "$log"
  if [ "$status" -eq 0 ]; then
    rm -f "$log"
    return 0
  fi
  if rate_limited "$log"; then
    rm -f "$log"
    return 10
  fi
  rm -f "$log"
  return "$status"
}

build_once() {
  run_logged docker buildx build "${cache_args[@]}" "$@" .
}

attempt=1
while [ "$attempt" -le "$ECR_ATTEMPTS" ]; do
  status=0
  build_once || status=$?
  if [ "$status" -eq 0 ]; then
    exit 0
  fi
  if [ "$status" -ne 10 ]; then
    exit "$status"
  fi
  echo "ECR Public rate-limited the python base image (attempt ${attempt}/${ECR_ATTEMPTS})" >&2
  if [ "$attempt" -lt "$ECR_ATTEMPTS" ]; then
    sleep $((attempt * BACKOFF))
  fi
  attempt=$((attempt + 1))
done

echo "ECR Public still rate-limiting; building from ${MIRROR_IMAGE}" >&2
attempt=1
while [ "$attempt" -le "$MIRROR_ATTEMPTS" ]; do
  status=0
  # PYTHON_IMAGE is the Dockerfile's existing override. Railway does not pass
  # it, so production keeps the ECR Public default.
  build_once --build-arg "PYTHON_IMAGE=${MIRROR_IMAGE}" || status=$?
  if [ "$status" -eq 0 ]; then
    exit 0
  fi
  if [ "$status" -ne 10 ]; then
    exit "$status"
  fi
  echo "mirror.gcr.io rate-limited the python base image (attempt ${attempt}/${MIRROR_ATTEMPTS})" >&2
  if [ "$attempt" -lt "$MIRROR_ATTEMPTS" ]; then
    sleep $((attempt * BACKOFF))
  fi
  attempt=$((attempt + 1))
done

echo "Both public.ecr.aws and mirror.gcr.io rate-limited the python base image" >&2
exit 1
