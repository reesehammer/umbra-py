#!/usr/bin/env sh
# Entrypoint for the umbra-py image.
#
# Default behaviour: serve the read-only STAC API. When the volume already
# holds a usable catalog, the server listens immediately and refreshes the
# published snapshot in the background. A cold volume fetches the index
# before listening. `mcp` still refreshes before it listens.
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

# A persistent volume keeps catalog.db across deploys, so "fetch only when
# missing" pinned the hosted API to its first snapshot. The serve path hands
# that refresh to `umbra serve --refresh-index`: a catalog file already on
# disk is served immediately and the snapshot, thumbnail sidecar, and preview
# merge run after the socket is open, swapping in under the server's lease.
# The shell only checks that the file exists. Opening it here as well as in
# the server was two integrity scans of the live catalog before listen.
# The server does one cheap open (schema version, and that items has a row)
# and leaves /healthz failing until that passes. `PRAGMA quick_check` stays
# on the staged download. Railway volumes cannot overlap two deployments, so
# blocking on that work before listen is the whole outage. A cold volume (no
# catalog file) still fetches the index here, before the process listens, and
# a total failure still falls back to a live S3 walk. `mcp` mode has no
# readiness gate of its own and keeps the blocking refresh.
INDEX_DB="${UMBRA_INDEX_DB:-${XDG_CACHE_HOME:-$HOME/.cache}/umbra-py/catalog.db}"
if [ "${UMBRA_FETCH_INDEX:-1}" != "0" ] && [ -d "$(dirname "$INDEX_DB")" ]; then
    # What the refreshes below have to work with, so a full volume is
    # diagnosable from the boot log rather than guessed at.
    data_root="${XDG_CACHE_HOME:-$(dirname "$(dirname "$INDEX_DB")")}"
    df -Ph "$data_root" | awk 'NR == 2 {print "Volume " $6 ": " $4 " free of " $2 "; largest entries:"}'
    du -xah -d 2 "$data_root" 2>/dev/null | sort -rh | head -n 10 || true
fi
if [ "${UMBRA_FETCH_INDEX:-1}" != "0" ] && [ "$MODE" = "mcp" ]; then
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
        exec umbra mcp --http --host "$HOST" --port "$PORT"
    fi
elif [ "${UMBRA_FETCH_INDEX:-1}" != "0" ]; then
    if [ -f "$INDEX_DB" ]; then
        # Present, readable or not. The server opens it once and keeps
        # /healthz failing until that open passes. Do not integrity-scan it
        # here: that scan is the warm-boot delay.
        echo "Catalog at $INDEX_DB is present; the server will refresh it after it is listening."
    else
        echo "No catalog at $INDEX_DB; fetching the published snapshot before listening..."
        if [ -n "${UMBRA_INDEX_URL:-}" ]; then
            umbra index fetch --if-changed --url "$UMBRA_INDEX_URL" || FETCH_FAILED=1
        else
            umbra index fetch --if-changed || FETCH_FAILED=1
        fi
        if [ "${FETCH_FAILED:-0}" = "1" ] && [ -f "$INDEX_DB" ]; then
            echo "Snapshot refresh failed; serving the existing index at $INDEX_DB." >&2
        elif [ "${FETCH_FAILED:-0}" = "1" ]; then
            echo "Index fetch failed; falling back to a live S3 walk (slow)." >&2
            # Drop --public: it refuses --live. Same fallback the mcp branch uses.
            exec umbra serve --host "$HOST" --port "$PORT" --live
        fi
    fi
    set -- --refresh-index "$@"
fi

# Baked SAR previews so MCP quicklook / describe_scene work without proxying
# Umbra COGs. The local file is catalog.db -> catalog.thumbs.db beside the
# index. The catalog-index release publishes that sqlite as
# catalog.thumbs.manifest.json plus catalog.thumbs.db.partNNN.<sha> parts,
# each under GitHub's 2 GiB asset limit. `umbra index fetch-thumbnails
# --if-changed` (no --url) reads catalog.thumbs.manifest.json, downloads
# those parts, and writes catalog.thumbs.db. A release with no manifest
# still serves a single catalog.thumbs.db. Refreshed like the index:
# `--if-changed` downloads only when that manifest (or the legacy asset)
# changed, and re-merges whenever the sidecar changed or the index above
# was swapped (the published catalog.db carries no thumbnails). A published
# sidecar with fewer previews than the local file is refused: the download
# is discarded and the local catalog.thumbs.db stays (no --force on this
# command). A failed check keeps and merges the existing sidecar; no sidecar
# at all never blocks startup.
# Both refreshes are disk-safe: each first deletes the scratch files an
# interrupted boot left (`.next`, `.next.part`, `.merge`, ...), checks free
# space before downloading, and the merge writes a copy of catalog.db that is
# swapped in only after it passes quick_check. A full volume skips the step
# with a log line and keeps the current files; /healthz reports `degraded`.
# `serve` does this inside --refresh-index, after the socket is open. Blocking
# here is what made a volume redeploy 502 until the merge finished.
THUMBS_DB="${INDEX_DB%.db}.thumbs.db"
if [ "$MODE" = "mcp" ] && [ "${UMBRA_FETCH_INDEX:-1}" != "0" ] && [ -f "$INDEX_DB" ]; then
    if [ -f "$THUMBS_DB" ]; then
        echo "Checking for newer published baked previews than $THUMBS_DB..."
    else
        echo "No thumbnail sidecar at $THUMBS_DB; fetching published baked previews..."
    fi
    umbra index fetch-thumbnails --if-changed || echo "Thumbnail refresh incomplete; serving the index with the thumbnails it has." >&2
fi

if [ "$MODE" = "mcp" ]; then
    echo "umbra mcp: Streamable HTTP on ${HOST}:${PORT}/mcp"
    exec umbra mcp --http --host "$HOST" --port "$PORT" "$@"
fi
exec umbra serve --host "$HOST" --port "$PORT" "$@"
