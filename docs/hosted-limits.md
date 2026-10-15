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
itself. GitHub Actions requests `GET /healthz` every 15 minutes (GitHub may
start the cron late; the history records when the probe actually ran). A probe
counts as up when the response is HTTP 200 and `degraded` is false. Latency
is stored with each result. The server has no latency cap, so a slow 200 is
still up; a timeout (20 seconds, the same budget the publish workflow uses)
is a failed probe.

`/healthz` does not count against the rate limit.

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

A client is the `Authorization: Bearer` token when one is sent (the server
keeps a hash, not the token), otherwise the client IP. `--public` trusts
`X-Forwarded-For` from the proxy, which is what makes the cap per visitor
rather than one bucket for all of Railway.

These paths do not count: `/healthz`, `/docs`, `/openapi.json`, `/redoc`.

Over the cap the response is HTTP 429, body `RateLimited`, with `Retry-After`
(seconds), `X-RateLimit-Limit` and `X-RateLimit-Remaining`.

## Page size

`limit` defaults to 10. The most a client can request on `/search` and on
`/collections/{id}/items` is **10,000** (`MAX_LIMIT`). `GET /sites` ranks
sites, and its `top` parameter caps at 100, which is a different knob.

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
| [`coverage.md`](https://github.com/reesehammer/umbra-py/releases/download/catalog-index/coverage.md) / [`coverage.json`](https://github.com/reesehammer/umbra-py/releases/download/catalog-index/coverage.json) | Indexed vs bucket acquisitions by prefix and product, the 99% publication bar, quarantined rows with reasons, and the duplicate count. See [Local index](reference/index.md). |
| [`index-validation.md`](https://github.com/reesehammer/umbra-py/releases/download/catalog-index/index-validation.md) / [`index-validation.json`](https://github.com/reesehammer/umbra-py/releases/download/catalog-index/index-validation.json) | The same quarantine and duplicate rows from the build |

Data on that release is Umbra open data, CC BY 4.0. This host is not an Umbra
product.
