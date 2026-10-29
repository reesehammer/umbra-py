# Hosted API limits and uptime

The community API at [https://api.umbra-py.space/](https://api.umbra-py.space/)
is `umbra serve --public` on Railway. The catalog index is rebuilt by the
Publish catalog index workflow (Mondays 06:17 UTC) and published on the
[`catalog-index`](https://github.com/reesehammer/umbra-py/releases/tag/catalog-index)
GitHub release. This page is the operational contract for that host.

There is **no service-level agreement** and no credits. The uptime target
below is best effort.

## Uptime

Target: **99% monthly uptime**, best effort.

Uptime is measured by an external probe, not by the process's own view of
itself. GitHub Actions requests `GET /healthz` on a `*/15` cron. GitHub often
starts that cron hours late; the history records when the probe actually ran.
A probe counts as up when the response is HTTP 200 and `degraded` is false.
Latency is stored with each result. The client waits 60 seconds. A timeout is
a failed probe and the opened issue says so. A catalog read that misses the
server's own deadline is HTTP 503, which is also a failed probe.

`/healthz` is readiness: the same JSON document as before, including
`ready`, `degraded`, and `stale`. `/livez` (and `/healthz/live`) is liveness:
`{"status":"ok"}`, no catalog read. Railway's deploy check uses `/livez`.
`/readyz` and `/healthz/ready` are the same document as `/healthz`.

`/healthz`, `/livez`, and `/readyz` do not count against the rate limit.

Results are appended to `healthz.jsonl` on the
[`monitoring`](https://github.com/reesehammer/umbra-py/tree/monitoring) branch.
That branch is not `main`. One bad probe fails the Actions run. Three consecutive
failures (about 45 minutes) open or update a GitHub issue, and the issue is
commented on at most every 12 hours while the failure holds.
A recovery closes it.

An uptime percentage is not published until that file covers **7 days**.

`degraded: true` means the thumbnail sidecar did not merge. Search still
answers; the probe counts it as down so a preview gap is visible. Only an
index that exists but cannot be opened is HTTP 503.

## Freshness

`/healthz` includes `built_at` (the index build date, `YYYY-MM-DD`) and
`snapshot` (the published `catalog.db` ETag). It also sets `stale: true` when
`built_at` is missing or more than **9 days** before today (UTC). A build
exactly 9 days old is still fresh. The weekly job runs every 7 days, so 9
days means a Monday publish was missed.

`stale: true` is still HTTP 200. It is not downtime. The same probe opens a
separate issue for it. `ok` in `healthz.jsonl` stays true when the API is up
and the index is stale.

The same two values are on the STAC collection
`GET /collections/umbra-open-data`:

- `umbra-py:built_at` — the build date
- `umbra-py:snapshot` — the ETag
- `updated` — that date at midnight UTC (`2026-10-08T00:00:00Z`). The midnight
  is the date. The workflow does not store a clock time.

They are omitted when the process has no index stamp (live mode, or a server
that is still fetching).

## Rate limit

**120 requests per 60-second sliding window**, per client. That is
`PUBLIC_RATE_LIMIT` and `PUBLIC_RATE_WINDOW_S` on `umbra serve --public`.

A client is the address Railway's edge accepted. `--public` reads
`X-Real-IP` (`UMBRA_CLIENT_IP_HEADER`) and ignores `Authorization`. A bearer
token is not a separate bucket. `X-Forwarded-For` is only the fallback: the
rightmost `UMBRA_TRUSTED_PROXY_HOPS` entries (default 1), never the leftmost.
Railway overwrites `X-Real-IP` and does not promise a fixed hop count, so
leave the header at the default. A loopback peer (a laptop `--public` with
no proxy) ignores those headers and is keyed on the socket address.

These paths do not count: `/healthz`, `/healthz/live`, `/healthz/ready`,
`/livez`, `/readyz`, `/docs`, `/openapi.json`, `/redoc`.

Over the cap the response is HTTP 429, body `RateLimited`, with `Retry-After`
(seconds), `X-RateLimit-Limit` and `X-RateLimit-Remaining`. The window is
per process. A request body over 1 MiB counts as well (HTTP 413, or 429 when
that hit crosses the cap).

## Concurrent catalog reads

One client may hold **2** catalog reads at once (`UMBRA_CLIENT_CONCURRENCY`,
minimum 1). A third is HTTP 429 with `Retry-After: 1`, before it takes a
database slot. The process still allows `UMBRA_DB_CONCURRENCY` reads in
total (default 4); past that the response is HTTP 503. A read that passes
`UMBRA_REQUEST_TIMEOUT_S` (default 15s) is HTTP 503 and the SQLite statement
is interrupted so the slot is released.

## Page size

`limit` defaults to 10. On this host, `/search` and
`/collections/{id}/items` return at most **500** items
(`PUBLIC_SEARCH_LIMIT`). A larger `limit` is clamped, not rejected, and the
`next` link advances by that page. A self-hosted server without `--public`
still accepts up to **10,000** (`MAX_LIMIT`). `GET /sites` ranks sites, and
its `top` parameter caps at 100. Its `limit` sizes a live-backend pool and
is ignored on the index, so it is not part of the page cap.

MCP `search_catalog` keeps its argument and its default of 20. On this host
a `limit` above **100** is clamped to 100 and the result includes `note`
telling the caller to take the next page. A default call has no `note`.

## Request body

`POST` bodies larger than **1 MiB** (`PUBLIC_MAX_BODY_BYTES`) are HTTP 413
and are not parsed. A `POST /search` body with the wrong JSON types (a
string `token`, a non-array `ids`, a scalar where an object is required,
or nesting the parser cannot walk) is HTTP 400.

## Geocoding

Public `geocode_place`, and a `place` filter on the public MCP tools that
geocode, share one Nominatim throttle of about **1 request per second** and
an LRU cache of 256 names. A repeated name does not call out. Local MCP and
the CLI call Nominatim directly.

## Link origin

STAC `links[].href` uses `UMBRA_PUBLIC_BASE_URL` when that variable is set
(no trailing slash). On `--public`, an unset value uses
`https://$RAILWAY_PUBLIC_DOMAIN` when Railway injected it. Otherwise the
request URL is used, which is the right origin for a laptop. Set
`UMBRA_PUBLIC_BASE_URL=https://api.umbra-py.space` on the community host so
links use that name rather than the `*.up.railway.app` domain.

## CORS

The server is read-only. Browser clients on another origin, including a
static `umbra demo` page, can call it:

| Setting | Value |
| --- | --- |
| `allow_origins` | `*` |
| `allow_methods` | `GET`, `POST`, `OPTIONS` |
| `allow_headers` | `*` |
| `expose_headers` | `Link`, `Retry-After`, `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-Umbra-Attribution`, `X-Umbra-License` |
| credentials | not allowed (`*` origins cannot be credentialed) |

Every response also sends `X-Umbra-License`, `X-Umbra-Attribution`, and
`Link: rel="license"`.

## Bulk use and outages

For a full catalog scan, or when this API is down, use the weekly release
instead of paging `/search`. GitHub redirects
`/releases/download/catalog-index/<file>` at the current asset:

| File | Use |
| --- | --- |
| [`catalog.db`](https://github.com/reesehammer/umbra-py/releases/download/catalog-index/catalog.db) | `umbra index fetch`, then `umbra search --local` |
| [`umbra-open-data.parquet`](https://github.com/reesehammer/umbra-py/releases/download/catalog-index/umbra-open-data.parquet) | DuckDB, geopandas, pyarrow, or rustac. File metadata carries `umbra-py:built_at` and `umbra-py:snapshot`. Those are not columns. |
| [`coverage.md`](https://github.com/reesehammer/umbra-py/releases/download/catalog-index/coverage.md) / [`coverage.json`](https://github.com/reesehammer/umbra-py/releases/download/catalog-index/coverage.json) | Indexed vs bucket acquisitions by prefix and product, the 99% publication bar, quarantined rows with reasons, the duplicate-id count, and republished-capture groups (linked, not dropped). See [Local index](reference/index.md). |
| [`index-validation.md`](https://github.com/reesehammer/umbra-py/releases/download/catalog-index/index-validation.md) / [`index-validation.json`](https://github.com/reesehammer/umbra-py/releases/download/catalog-index/index-validation.json) | The same quarantine, duplicate-id, and republished-capture rows from the build |

Data on that release is Umbra open data, CC BY 4.0. This host is not an Umbra
product.

If an application probes `/mcp` as a canary, the core tool names stay stable
through every 0.x minor. See [Stable tools](mcp.md#stable-tools).
