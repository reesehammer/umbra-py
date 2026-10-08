#!/usr/bin/env sh
# Entrypoint for the umbra-py image.
#
# Default behaviour: make sure the catalog index on the /data volume is the
# latest published snapshot (download it on first boot; on later boots one HEAD
# request decides whether the weekly rebuild changed it, and only then is the new
# file downloaded, validated and swapped in), then run the read-only STAC API.
# Pass `serve --public` for the hosted community instance (STAC + MCP on one
# process). Pass `mcp` as the first argument to serve
# Streamable HTTP MCP alone (`umbra mcp --http`).
#
# Environment variables:
#   UMBRA_HOST         Interface to bind      (default 0.0.0.0)
#   UMBRA_PORT         Port to listen on      (default 8000)
#   UMBRA_FETCH_INDEX  Fetch / refresh the published index on boot (default 1; "0" skips)
#   UMBRA_SERVE_LIVE   Serve from a live S3 walk per request instead of an index
#                      ("1" enables; correct but slow, needs no index)
#   UMBRA_INDEX_URL    Override the published-index asset URL (e.g. a fork/mirror)
#   UMBRA_SERVE_ARGS   Extra flags passed through to `umbra serve`
#                      (e.g. "--no-artifacts", or "--stack-lazy" to measure a
#                      long series a slice at a time -- needs the `dask` extra)
#   UMBRA_INDEX_DB     Explicit index path (default: $XDG_CACHE_HOME/umbra-py/catalog.db)
#
# Any other command is run verbatim, so the image doubles as the full CLI:
#   docker run --rm umbra-py search --area "Beet Piler" --limit 5
set -eu

# Host mounts of /data (Railway Volumes, docker -v) are root-owned and hide
# the image's chown, so `umbra` cannot mkdir /data/umbra-py. Take ownership
# then drop to umbra before any umbra command. No gosu: python:slim has Python.
if [ "$(id -u)" -eq 0 ]; then
    data_root="${XDG_CACHE_HOME:-/data}"
    mkdir -p "$data_root"
    chown -R umbra:umbra "$data_root"
    exec python -c 'import os, pwd, sys
u = pwd.getpwnam("umbra")
os.environ["HOME"] = u.pw_dir
os.initgroups(u.pw_name, u.pw_gid)
os.setgid(u.pw_gid)
os.setuid(u.pw_uid)
os.execv(sys.argv[1], sys.argv[1:])' /usr/local/bin/docker-entrypoint.sh "$@"
fi

# Passthrough: `docker run IMAGE <umbra subcommand>` runs the CLI directly.
# Bare run or explicit `serve` → STAC API. Explicit `mcp` → Streamable HTTP MCP.
if [ "$#" -gt 0 ] && [ "$1" != "serve" ] && [ "$1" != "mcp" ]; then
    exec umbra "$@"
fi

MODE=serve
if [ "$#" -gt 0 ] && [ "$1" = "mcp" ]; then
    MODE=mcp
    shift
elif [ "$#" -gt 0 ] && [ "$1" = "serve" ]; then
    shift
fi

HOST="${UMBRA_HOST:-0.0.0.0}"
# Railway injects PORT; fall back to UMBRA_PORT then 8000.
PORT="${PORT:-${UMBRA_PORT:-8000}}"

if [ "$MODE" = "serve" ]; then
    # `UMBRA_SERVE_ARGS` and any leftover args are forwarded to `umbra serve`.
    # shellcheck disable=SC2086
    set -- ${UMBRA_SERVE_ARGS:-} "$@"
fi

if [ "${UMBRA_SERVE_LIVE:-0}" = "1" ]; then
    echo "umbra ${MODE}: live S3 walk per request (no index)."
    if [ "$MODE" = "mcp" ]; then
        exec umbra mcp --http --host "$HOST" --port "$PORT" "$@"
    fi
    exec umbra serve --host "$HOST" --port "$PORT" --live "$@"
fi

# Fetch the published snapshot on first boot, and on every later boot refresh it
# when the weekly rebuild changed it. A persistent volume keeps catalog.db across
# deploys, so "fetch only when missing" pinned the hosted API to its first
# snapshot forever; `--if-changed` compares the release asset's ETag with the one
# recorded beside the index and downloads, validates and atomically swaps only
# on a change. It runs here, before the server starts, because the swap is only
# safe while nothing holds the index open.
# This leaves the serve/mcp args in "$@" untouched for the final exec below.
INDEX_DB="${UMBRA_INDEX_DB:-${XDG_CACHE_HOME:-$HOME/.cache}/umbra-py/catalog.db}"
if [ "${UMBRA_FETCH_INDEX:-1}" != "0" ]; then
    if [ -f "$INDEX_DB" ]; then
        echo "Checking for a newer published snapshot than $INDEX_DB..."
    else
        echo "No catalog index at $INDEX_DB; fetching the published snapshot..."
    fi
    if [ -n "${UMBRA_INDEX_URL:-}" ]; then
        umbra index fetch --if-changed --url "$UMBRA_INDEX_URL" || FETCH_FAILED=1
    else
        umbra index fetch --if-changed || FETCH_FAILED=1
    fi
    if [ "${FETCH_FAILED:-0}" = "1" ] && [ -f "$INDEX_DB" ]; then
        # A failed refresh never touches the existing index: keep serving it.
        echo "Snapshot refresh failed; serving the existing index at $INDEX_DB." >&2
    elif [ "${FETCH_FAILED:-0}" = "1" ]; then
        echo "Index fetch failed; falling back to a live S3 walk (slow)." >&2
        if [ "$MODE" = "mcp" ]; then
            exec umbra mcp --http --host "$HOST" --port "$PORT"
        fi
        exec umbra serve --host "$HOST" --port "$PORT" --live
    fi
fi

# Baked SAR previews so MCP quicklook / describe_scene work without proxying
# Umbra COGs. catalog.db -> catalog.thumbs.db sits beside the index.
THUMBS_DB="${INDEX_DB%.db}.thumbs.db"
if [ "${UMBRA_FETCH_INDEX:-1}" != "0" ] && [ -f "$INDEX_DB" ] && [ ! -f "$THUMBS_DB" ]; then
    echo "No thumbnail sidecar at $THUMBS_DB; fetching published baked previews..."
    umbra index fetch-thumbnails || echo "Thumbnail fetch failed; baked quicklooks unavailable." >&2
fi

if [ "$MODE" = "mcp" ]; then
    echo "umbra mcp: Streamable HTTP on ${HOST}:${PORT}/mcp"
    exec umbra mcp --http --host "$HOST" --port "$PORT" "$@"
fi
exec umbra serve --host "$HOST" --port "$PORT" "$@"
