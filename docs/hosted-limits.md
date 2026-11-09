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

A client is the `Authorization: Bearer` token when one is sent (the server
keeps a hash, not the token), otherwise the client IP. `--public` trusts
`X-Forwarded-For` from the proxy, which is what makes the cap per visitor
rather than one bucket for all of Railway.

These paths do not count: `/healthz`, `/healthz/live`, `/healthz/ready`,
`/livez`, `/readyz`, `/docs`, `/openapi.json`, `/redoc`.

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
`POST /mcp`) and `stac_search` (each `/search`). Those two lines still
record `client_ip`, the peer the server already resolved. `client_net` is
only on `mcp_tool`.

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
| `client_net` | First 16 hex characters of SHA-256 over `24:<IPv4 /24>` or `64:<IPv6 /64>`. An IPv4-mapped IPv6 address uses the IPv4 `/24`. The address is not logged. The same prefix hashes the same on every replica |

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
