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
`X-Real-IP` and ignores `Authorization`. A bearer token is not a separate
bucket. `X-Forwarded-For` is only the fallback: the rightmost
`UMBRA_TRUSTED_PROXY_HOPS` entries (default 1), never the leftmost.
Railway overwrites `X-Real-IP` and does not promise a fixed hop count, so
leave that fallback at the default. IPv4 is the full address. IPv6 collapses
to its `/64`. A loopback peer (a laptop `--public` with no proxy) ignores
those headers and is keyed on the socket address.

These paths do not count: `/healthz`, `/healthz/live`, `/healthz/ready`,
`/livez`, `/readyz`, `/docs`, `/openapi.json`, `/redoc`.

Over the cap the response is HTTP 429, body `RateLimited`, with `Retry-After`
(seconds), `X-RateLimit-Limit` and `X-RateLimit-Remaining`. The window is
per process. A request body over 1 MiB counts as well (HTTP 413, or 429 when
that hit crosses the cap).

## Concurrent catalog reads

One client may hold **2** catalog reads at once. A third is HTTP 429 with
`Retry-After: 1`, before it takes a database slot. The process still allows
`UMBRA_DB_CONCURRENCY` reads in
total (default 4); past that the response is HTTP 503. A read that passes
`UMBRA_REQUEST_TIMEOUT_S` (default 15s) is HTTP 503 and the SQLite statement
is interrupted so the slot is released.

## Page size

`limit` defaults to 10. On this host, `/search` and
`/collections/{id}/items` return at most **500** items
(`PUBLIC_SEARCH_LIMIT`). A larger numeric `limit` is clamped, not rejected,
including a number above 10,000, and the `next` link advances by that page.
A self-hosted server without `--public` still accepts up to **10,000**
(`MAX_LIMIT`). `GET /sites` ranks sites, and
its `top` parameter caps at 100. Its `limit` sizes a live-backend pool and
is ignored on the index, so it is not part of the page cap.

MCP `search_catalog` keeps its argument and its default of 20. On this host
a `limit` above **100** is clamped to 100 and the result includes `note`
telling the caller to take the next page. A default call has no `note`.

## Request body

`POST` bodies larger than **1 MiB** (`PUBLIC_MAX_BODY_BYTES`) are HTTP 413
and are not parsed. On this host, a `POST /search` body with the wrong JSON
types (a non-numeric `token` or `limit`, a non-array `ids`, a scalar where
an object is required, `NaN`/`Infinity`, or nesting the parser cannot walk)
is HTTP 400. A numeric `limit` or `token` outside the page range is clamped.
A server without `--public` keeps the previous body handling.

## Geocoding

Public `geocode_place`, a `place` filter, and an `area` that falls back to
geocoding share one Nominatim throttle of about **1 request per second** and
one LRU cache of 256 names. A repeated name does not call out. A place that
does not resolve is remembered for about 10 minutes. A miss that would spend
more than about 5 seconds waiting for the in-flight call and the gap, or
join more than 5 waiters, fails immediately. `geocode_place` and a `place`
filter return that as an error. An `area` fallback that is busy, cannot
reach Nominatim, or finds the quick outbound pool full returns the empty
result with `hint` `no task named X; geocoding was unavailable` instead.
A live catalog's task-name check takes a long outbound permit, the same
pool as the catalog walk; an index check does not. A cached name does not
take a quick outbound permit. `GET`/`POST /search` keeps that fallback's
Nominatim wait and HTTP timeout inside half the request deadline, so a
slow geocode is the unavailable hint rather than HTTP 503. `/sites` is
not on that deadline. Local `umbra serve`
uses the same cache, without the throttle, so a next link does not geocode
again. Local MCP `place=` and the CLI call Nominatim directly.

## Link origin

STAC `links[].href` on this host uses `UMBRA_PUBLIC_BASE_URL` when that
optional variable is a bare `http(s)://host[:port]` origin (a trailing slash
is removed). Any other value is ignored. When it is unset, `--public` builds
the origin from `X-Forwarded-Proto` and `X-Forwarded-Host` (or `Host`) if the
socket peer is not loopback. Railway's edge overwrites those headers, which
is how `https://api.umbra-py.space` stays the link base without a new
variable. The same proto is what `GET /mcp/` redirects with, so the
trailing-slash redirect stays `https`. A loopback peer, or a header that is
not `http`/`https` plus a plain host, keeps the request URL. A server
without `--public` does not read `UMBRA_PUBLIC_BASE_URL`.

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

## Baked thumbnails

`GET /artifacts/thumbnail/{item_id}.png` returns a PNG already stored in the
local thumbnail sidecar. It does not read Umbra's bucket, render a quicklook,
or fall back to another picture. A missing bake is HTTP 404 (`No baked
thumbnail`). An id that is not a single token (a slash, `..`, or anything
outside letters, digits, `.`, `_`, and `-`) is HTTP 400. The response sends
`Cache-Control: public, max-age=86400`. The URL is the item id, not a content
hash, so the PNG is not marked `immutable`: a later sidecar can replace the
bytes for that id. The same CORS headers as the rest of this API apply. Each
GET counts toward the 120/min limit. It does not take one of the two
concurrent catalog-read slots (a browser fetches several images at once).

`/search`, `/collections/umbra-open-data/items`, and
`/collections/umbra-open-data/items/{item_id}` add `assets.thumbnail` (`href`
on this host, `type: image/png`, `roles: ["thumbnail"]`) only when that item
has a bake. An item without one has no thumbnail asset; show a placeholder.
On-demand `/artifacts/quicklook` and the other `/artifacts/*` routes stay off.

## Bulk use and outages

Do not page `/search` for a full catalog. Take the weekly snapshot instead,
including when this API is down. Download it with
`umbra index fetch` (flags `--db`, `--url`, `--insecure-url`, `--if-changed`,
and `--force`; `--force` only with `--if-changed`) from
[`catalog.db`](https://github.com/reesehammer/umbra-py/releases/download/catalog-index/catalog.db),
then search it with `umbra search --local`. The [local index](reference/index.md)
page says what each flag does.
[`catalog.sha256`](https://github.com/reesehammer/umbra-py/releases/download/catalog-index/catalog.sha256)
is the GNU `sha256sum` manifest beside that file. `umbra index fetch` checks
`catalog.db` against it when the manifest is present, and warns and continues
when the manifest is missing. `umbra serve --public` writes `catalog.rtree.db`
next to `catalog.db` at startup. A tight bbox search reads it, and so does a
deep page of a smaller box; a wide first page keeps the ordered index scan.
The file is local: fetch does not download it,
and `catalog.sha256` does not list it. It is rebuilt when the snapshot id in
`/healthz` (or, with no recorded snapshot, the catalog file's size and mtime)
or the item count changes. If the build fails, search uses the same results
without it.

GitHub redirects `/releases/download/catalog-index/<file>` at the current
asset:

| File | Use |
| --- | --- |
| [`catalog.db`](https://github.com/reesehammer/umbra-py/releases/download/catalog-index/catalog.db) | The snapshot `umbra index fetch` downloads. `umbra search --local` reads it. |
| [`catalog.sha256`](https://github.com/reesehammer/umbra-py/releases/download/catalog-index/catalog.sha256) | GNU `sha256sum` manifest listing `catalog.db`. `umbra index fetch` checks `catalog.db` against it when the manifest is present, and warns and continues when the manifest is missing. |
| [`umbra-open-data.parquet`](https://github.com/reesehammer/umbra-py/releases/download/catalog-index/umbra-open-data.parquet) | DuckDB, geopandas, pyarrow, or rustac. File metadata carries `umbra-py:built_at` and `umbra-py:snapshot`. Those are not columns. |
| [`coverage.md`](https://github.com/reesehammer/umbra-py/releases/download/catalog-index/coverage.md) / [`coverage.json`](https://github.com/reesehammer/umbra-py/releases/download/catalog-index/coverage.json) | Indexed vs bucket acquisitions by prefix and product, the 99% publication bar, quarantined rows with reasons, the duplicate-id count, and republished-capture groups (linked, not dropped). See [Local index](reference/index.md). |
| [`index-validation.md`](https://github.com/reesehammer/umbra-py/releases/download/catalog-index/index-validation.md) / [`index-validation.json`](https://github.com/reesehammer/umbra-py/releases/download/catalog-index/index-validation.json) | The same quarantine, duplicate-id, and republished-capture rows from the build |

Data on that release is Umbra open data, CC BY 4.0. This host is not an Umbra
product.

If an application probes `/mcp` as a canary, the core tool names stay stable
through every 0.x minor. See [Stable tools](mcp.md#stable-tools).

## MCP tool logs

`umbra serve` writes one JSON object per MCP tool call to stdout. The `msg`
field is `mcp_tool`. Railway's log explorer rewrites `msg` to `message`, so
filters use `@message`. The same logger writes `mcp_request` (each
`POST /mcp`) and `stac_search` (each `/search`). Those lines record
`client_ip`. On this host that is `X-Real-IP`, or the trusted
`X-Forwarded-For` hop (`UMBRA_TRUSTED_PROXY_HOPS`, default 1) when
`X-Real-IP` is absent. `client_net` is only on `mcp_tool`, and it is the
hash of that same address.

A tool line has:

| Field | Meaning |
| --- | --- |
| `tool` | Tool name |
| `duration_ms` | Milliseconds, including argument validation |
| `outcome` | `ok`, or the exception class (`ValueError`, `ToolError`, …) |
| `result_count` | The result's `count`, or the length of `items` / `sites` / `matches` / `passes` / `results` / `features` when that is how the reply is shaped. Otherwise null |
| `client_name`, `client_version` | `initialize` `clientInfo` when that handshake shares the connection. Otherwise the User-Agent product token (`name/version`). Comments in the User-Agent are not logged |
| `session_id` | `mcp-session-id` when the client sent a token of letters, digits, `_`, and `-`. This host is stateless, so the field is usually null |
| `argument_keys` | Argument names. Not the values |
| `limit` | Present when `limit` is an integer from 0 to 10000000 |
| `collection` | Present when `collection` or `collections` is a short token such as `umbra-open-data` |
| `bbox`, `place` | True when that argument was set. The coordinates and the place name are not logged |
| `client_net` | First 16 hex characters of SHA-256 over `24:<IPv4 /24>` or `64:<IPv6 /64>` of the same address `client_ip` records. An IPv4-mapped IPv6 address uses the IPv4 `/24`. The public host stores an IPv6 client as its `/64`, and that network hashes the same as any address inside it. The address is not logged. The same prefix hashes the same on every replica |

`level` is `error` when `outcome` is not `ok`, and `info` otherwise.
`@level:error AND @message:mcp_tool` is the failed tool calls.

Recompute a prefix hash:

```python
import hashlib
import ipaddress

ip = ipaddress.ip_address("203.0.113.7")
net = ipaddress.ip_network(f"{ip}/24", strict=False)
print(hashlib.sha256(f"24:{net.network_address}".encode()).hexdigest()[:16])
```

In the Railway log explorer (Observability) or `railway logs`:

```text
@message:mcp_tool
@message:mcp_tool AND @tool:search_catalog
@message:mcp_tool AND @outcome:ValueError
@message:mcp_tool -@outcome:ok
@message:mcp_tool AND @duration_ms:>=1000
@message:mcp_tool AND @bbox:true
@message:mcp_tool AND @client_net:<hash>
```

Railway's HTTP log stream (`@srcIp`, `@path`) is the edge's own record and
is separate from these lines. The process access log is also separate.
