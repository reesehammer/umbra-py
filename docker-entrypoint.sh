#!/usr/bin/env sh
# Entrypoint for the umbra-py serve image.
#
# Default behaviour: fetch the published catalog index on first boot (unless one
# is already present on the /data volume, or fetching is disabled), then run the
# read-only STAC API bound to all interfaces so it is reachable from the host.
#
# Environment variables:
#   UMBRA_HOST         Interface to bind      (default 0.0.0.0)
#   UMBRA_PORT         Port to listen on      (default 8000)
#   UMBRA_FETCH_INDEX  Fetch the published index on first boot (default 1; "0" skips)
#   UMBRA_SERVE_LIVE   Serve from a live S3 walk per request instead of an index
#                      ("1" enables; correct but slow, needs no index)
#   UMBRA_INDEX_URL    Override the published-index asset URL (e.g. a fork/mirror)
#   UMBRA_SERVE_ARGS   Extra flags passed through to `umbra serve`
#                      (e.g. "--no-artifacts")
#   UMBRA_INDEX_DB     Explicit index path (default: $XDG_CACHE_HOME/umbra-py/catalog.db)
#
# Any other command is run verbatim, so the image doubles as the full CLI:
#   docker run --rm umbra-py search --area "Beet Piler" --limit 5
set -eu

# Passthrough: `docker run IMAGE <umbra subcommand>` runs the CLI directly.
# Only a bare run (no args) or an explicit `serve` goes through the serve flow.
if [ "$#" -gt 0 ] && [ "$1" != "serve" ]; then
    exec umbra "$@"
fi
# Drop a leading literal "serve" so we can layer in our own defaults.
if [ "$#" -gt 0 ] && [ "$1" = "serve" ]; then
    shift
fi

HOST="${UMBRA_HOST:-0.0.0.0}"
PORT="${UMBRA_PORT:-8000}"

# `UMBRA_SERVE_ARGS` and any positional args are forwarded to `umbra serve`.
# shellcheck disable=SC2086
set -- ${UMBRA_SERVE_ARGS:-} "$@"

if [ "${UMBRA_SERVE_LIVE:-0}" = "1" ]; then
    echo "umbra serve: live S3 walk per request (no index)."
    exec umbra serve --host "$HOST" --port "$PORT" --live "$@"
fi

# Fetch the published snapshot on first boot unless disabled or already present.
# This leaves the serve args in "$@" untouched for the final exec below.
INDEX_DB="${UMBRA_INDEX_DB:-${XDG_CACHE_HOME:-$HOME/.cache}/umbra-py/catalog.db}"
if [ "${UMBRA_FETCH_INDEX:-1}" != "0" ] && [ ! -f "$INDEX_DB" ]; then
    echo "No catalog index at $INDEX_DB; fetching the published snapshot..."
    if [ -n "${UMBRA_INDEX_URL:-}" ]; then
        umbra index fetch --url "$UMBRA_INDEX_URL" || FETCH_FAILED=1
    else
        umbra index fetch || FETCH_FAILED=1
    fi
    if [ "${FETCH_FAILED:-0}" = "1" ]; then
        echo "Index fetch failed; falling back to a live S3 walk (slow)." >&2
        exec umbra serve --host "$HOST" --port "$PORT" --live
    fi
fi

exec umbra serve --host "$HOST" --port "$PORT" "$@"
