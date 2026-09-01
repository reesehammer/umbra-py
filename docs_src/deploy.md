# Deploy / self-host

Umbra publishes a static STAC catalog and *no* search API, so the standard STAC
tooling (`pystac-client`, the QGIS STAC plugin, `stac-browser`, leafmap) has
nothing to query. [`umbra serve`](cli.md) restores that missing endpoint — a
read-only STAC API over a local catalog index. This page covers standing it up
as a container so no local Python install is needed.

## One command

The repository ships a `Dockerfile` and a `docker-compose.yml`. `docker compose
up` builds the image, fetches the published catalog index snapshot on first boot
(no multi-minute S3 crawl), and serves the STAC API:

```bash
docker compose up            # http://localhost:8000  (OpenAPI docs at /docs)
```

Point any STAC API client at it:

```bash
curl http://localhost:8000/search?limit=2
```

Or with plain Docker:

```bash
docker build -t umbra-py .
docker run -p 8000:8000 -v umbra-data:/data umbra-py
```

## What the image does

- **Fetches the published index on first boot.** The entrypoint runs
  [`umbra index fetch`](cli.md) into the `/data` volume before starting the
  server, so a fresh container is queryable in seconds rather than after a full
  bucket walk. Subsequent starts reuse the cached index.
- **Persists to a volume.** The catalog index, any fetched snapshot and the
  render-artifact cache all live under `/data` (the image sets
  `XDG_CACHE_HOME=/data`), so restarts are instant and the archive is never
  re-crawled.
- **Exposes a health probe.** `GET /healthz` returns `200` once the HTTP server
  is up (liveness); its body's `ready` flag reports whether the search backend
  can answer queries yet (readiness — the first-boot fetch may still be in
  flight). It is wired to a Docker `HEALTHCHECK` and is exactly what a Kubernetes
  liveness/readiness probe wants.
- **Runs unprivileged.** The process runs as a non-root user that owns only the
  `/data` volume.
- **Doubles as the CLI.** Any other command runs the full CLI:
  `docker run --rm umbra-py search --area "Beet Piler" --limit 5`.

## Configuration

All behaviour is driven by environment variables (set them in the compose file's
`environment:` block or with `docker run -e`):

| Variable            | Default   | Effect                                                                 |
| ------------------- | --------- | ---------------------------------------------------------------------- |
| `UMBRA_HOST`        | `0.0.0.0` | Interface the server binds to inside the container.                    |
| `UMBRA_PORT`        | `8000`    | Port the server listens on.                                            |
| `UMBRA_FETCH_INDEX` | `1`       | Fetch the published index on first boot; set to `0` to skip.           |
| `UMBRA_SERVE_LIVE`  | unset     | `1` serves from a live S3 walk per request — no index (correct, slow). |
| `UMBRA_INDEX_URL`   | unset     | Override the published-index asset URL (e.g. a fork or mirror).        |
| `UMBRA_INDEX_DB`    | `/data/umbra-py/catalog.db` | Explicit index path.                                   |
| `UMBRA_SERVE_ARGS`  | unset     | Extra flags forwarded to `umbra serve` (e.g. `--no-artifacts`).        |

If the first-boot fetch fails (e.g. no outbound network), the entrypoint falls
back to a live S3 walk so the server still answers.

## Render endpoints

The default image installs only the lean `serve` extra, so it exposes the STAC
API alone; the on-demand `/artifacts/...` render endpoints return a clear "viz
extra not installed" error. To enable them, build with the `viz` stack:

```bash
docker build --build-arg UMBRA_EXTRAS=serve,viz -t umbra-py:full .
```

or set `UMBRA_EXTRAS: serve,viz` under the compose `build.args`. For a public
instance that wants to bound COG-streaming egress, keep the lean image or set
`UMBRA_SERVE_ARGS="--no-artifacts"`.

`POST /artifacts/stats` — the one artifact that answers in numbers rather than a
picture — needs the `load` extra instead of `viz` (it co-registers the passes
into a datacube and reduces it), so build with `serve,viz,load` for the full
artifact surface.

### Bounding the stats endpoint's memory

`/artifacts/stats` is also the only endpoint whose cost grows with the *number*
of acquisitions rather than with one render: it stacks the whole series. To
measure a long series a slice at a time instead, add the `dask` extra and turn
the lazy path on for the instance:

```bash
docker build --build-arg UMBRA_EXTRAS=serve,viz,load,dask -t umbra-py:full .
docker run --rm -p 8000:8000 -v umbra-data:/data \
  -e UMBRA_SERVE_ARGS="--stack-lazy --stack-chunk-size 1024" umbra-py:full
```

`--stack-lazy` makes each pass one deferred chunk; `--stack-chunk-size N` also
cuts each pass into `N`-square windows read independently, so one *scene* need
not fit either (at one range read per window instead of one per pass).
`--stack-scheduler` picks who evaluates the chunks: `synchronous` (the default)
uses the request's own worker, so the container's thread count stays whatever
its ASGI server was configured with; `threads` gives a single render dask's
thread pool, which is faster alone and multiplies under concurrent requests.

This is an operator setting, never a request field — it needs the extra
installed here and a decision about threads. A lazy cube's numbers are identical
to an eager one's (only the peak memory differs), so it is not part of the
artifact cache key: turn it on or off without invalidating anything. Without the
`dask` extra a stats request answers `501` naming it, exactly like a missing
`load`.

Building in windows only lowers what the *build* holds; the reduction still
reads a slice per pass unless a request asks otherwise. On an instance started
with `--stack-chunk-size`, a client may send `"windowed": true` to be measured
in those windows too:

```bash
curl -X POST http://127.0.0.1:8000/artifacts/stats \
  -H 'content-type: application/json' \
  -d '{"ids": ["…", "…"], "windowed": true}'
```

That one *is* a request field, because unlike the policy above it moves a
number: counts, means, spreads and every change figure stay exact, but each
pass's `median` / `p5` / `p95` become histogram estimates (`quantile_method` /
`quantile_bin_db` say so in the response), so it belongs in the cache key rather
than in an invisible server flag a cached artifact would depend on. Sent to an
instance with no `--stack-chunk-size`, it answers `400` naming the flag — there
would be no windows to walk, so it could only estimate percentiles for the same
memory. The complementary field is `"speckle_filter"`, which needs each pass
whole and so is the `400` on a chunked instance.

Which of that pair a deployment honours is therefore a consequence of the flags
above, and clients read it off the landing page rather than by probing:

```bash
curl -s http://127.0.0.1:8000/ | jq '.links[] | select(.rel=="stats")."umbra:options"'
# {"stacking": "lazy (1024px windows, synchronous scheduler)",
#  "windowed": {"supported": true},
#  "speckle_filter": {"supported": false, "reason": "speckle filtering needs an …"}}
```

The `reason` on an unsupported option is the same string the endpoint's `400`
carries, so an operator changing `--stack-chunk-size` changes both together, and
`stacking` repeats the policy line the container logs at startup.

## Behind a reverse proxy

The server sends a permissive read-only CORS policy, so a browser front end on
another origin (including a static [`umbra demo`](cli.md) page) can call
`/search` and the render endpoints cross-origin. Terminate TLS at your proxy and
forward to the container's port; `GET /healthz` is a cheap upstream health check.

## Static showcase (no server)

For a zero-install *front door* — no API, no container — [`umbra
showcase`](cli.md) assembles a static site you drop on any static host:

```bash
umbra index fetch            # pull the published catalog snapshot (no crawl)
umbra showcase \
    --unified \
    --fetch-pmtiles \
    --featured 6 \
    --out ./showcase
```

That writes a self-contained directory:

- `index.html` — a landing page linking the pieces below plus install/docs/source;
- `explore.html` — the interactive [`umbra demo`](cli.md) explorer, reading the
  whole-catalog `catalog.pmtiles` archive (copied in beside it, so the folder is
  relocatable): **every** acquisition in the catalog, with live filters;
- `featured/*` — one precomputed artifact per featured site (a `.png` composite,
  or a `.html` swipe map with `--featured-view swipe`).

Every page is self-contained HTML, so it needs no extra and no backend. This is
what the repository's own **[hosted showcase](https://umbra-py.space/showcase/)**
is: the `.github/workflows/docs.yml` Pages job runs `umbra showcase` after the
mkdocs build and publishes `site/showcase/` beside the docs. Point `--pmtiles`
at a locally built basemap instead of `--fetch-pmtiles` for an offline build, or
pass `--no-explore` for a map-only page.

### One page or two

`--unified` is the recommended shape and the one the hosted showcase uses: the
explorer reads the tiled archive itself, so a visitor gets the whole catalog
*and* the filters on a single page.

Drop `--unified` and you get the original pair instead — `map.html`, a MapLibre
viewer over the archive you can only pan and click, plus `explore.html` over a
*gathered slice* (`--local --max-per-task 1` for a one-pin-per-site overview).
Both modes now draw footprint outlines — the unified one from the archive's
footprint polygons (tiled from zoom 6 up), the embedded one from the slice it
carries — and both offer the on-click "Get SAR image" COG overlay, the unified
one from the COG reference each tiled feature carries. What is left to the
embedded pair is only the two fields vector tiles do not encode: an
acquisition's polarizations and its per-product asset list. The two modes are
exclusive — in unified mode nothing is gathered, because the archive is the data
source.

!!! note "The published basemap"

    `--fetch-pmtiles` pulls the weekly-published `catalog.pmtiles`, which gains
    the COG references on its next `publish-index.yml` run. Until then the
    unified explorer built from it shows no "Get SAR image" button (a page over
    an archive without the references degrades to metadata, it does not error);
    `umbra tiles --local --out catalog.pmtiles` builds one with them now.

### Featured artifacts

`--featured N` precomputes an artifact for the `N` most repeat-imaged sites in
the catalog and puts them on the landing page, so a first-time visitor sees *what
SAR change looks like* immediately — no render round-trip, no server, and it
still works on a plain static host. Name the sites yourself with a repeatable
`--featured-area` instead:

```bash
umbra showcase --local --featured-area "Centerfield, Utah" \
    --featured-area "Beet Piler" --featured-frames 3 --out ./showcase
```

`--featured-view` picks *what* is precomputed from the same marquee selection:

| `--featured-view` | Artifact | Site needs | Reads |
| --- | --- | --- | --- |
| `change` (default) | `featured/<site>.png` | 2 passes (3 with `--featured-frames 3`) | green = new/brighter, magenta = gone/dimmer (or temporal RGB) |
| `timescan` | `featured/<site>.png` | 3+ passes | the **whole** series as one image — red = mean, green = peak, blue = variability, so anything that came and went glows blue/cyan |
| `swipe` | `featured/<site>.html` | 2 passes | an interactive before/after map with a draggable divider, linked from the gallery as a card |

`--featured-frames` applies to the `change` view only: a timescan summarises
every pass it has, and a swipe is always two. Sites that can't clear a view's
minimum are dropped before any render is attempted, so `--featured-view
timescan` quietly skips the two-pass sites that `change` would have used.

```bash
umbra showcase --local --featured 6 --featured-view timescan --out ./showcase
umbra showcase --local --featured 6 --featured-view swipe --out ./showcase
```

This is the one step that needs the `viz` extra
(`pip install "umbra-py[viz]"`); it streams only a downsampled overview of each
scene, and a site whose asset won't render is skipped with a warning rather than
failing the build. Without `--featured` the showcase is unchanged and stays
stdlib-only.

## Remote MCP (Railway)

`umbra mcp` is stdio by default (Claude Desktop / `uvx --from 'umbra-py[mcp]' umbra-mcp`).
`--http` serves the **same tools** over Streamable HTTP so a client can connect
to a URL instead of spawning a process:

```bash
umbra mcp --http --host 0.0.0.0 --port 8000
# POST http://127.0.0.1:8000/mcp
# GET  http://127.0.0.1:8000/healthz
```

`$PORT` (Railway) or `$UMBRA_PORT` is used when `--port` is omitted.

### Docker

The image entrypoint treats a first argument of `mcp` like `serve`: fetch the
published catalog index on first boot, then listen. `Dockerfile.mcp` bakes the
MCP extra (and `viz` for quicklook / change / timescan pictures):

```bash
docker build -f Dockerfile.mcp -t umbra-py:mcp .
docker run --rm -p 8000:8000 -v umbra-data:/data umbra-py:mcp
```

### Railway

The repo ships `railway.toml` and `Dockerfile.mcp`. Create a service from this
GitHub repo and deploy; extras and the start command are already in those files.

A volume is **not** required for the first boot. `/data` is writable in the
image, and the published `catalog.db` is ~17 MB (seconds, not a crawl). A
Railway Volume mounted at `/data` keeps that index across deploys. Do **not**
put `VOLUME ["/data"]` in the Dockerfile — Railway's Metal builder rejects it
even when a Railway Volume is attached.

`*.railway.internal` is Railway's **private** mesh DNS — only other services in
the same project can reach it. Claude and the Anthropic directory cannot.
After a green deploy: **Settings → Networking → Generate Domain**, then point
clients at `https://<generated>.up.railway.app/mcp` (or a custom domain).

If a previous deploy crashed with `exec: mcp: not found`, Railway replaced the
image entrypoint with a bare `mcp`. `railway.toml` now wraps the entrypoint;
you do not need a start command in the dashboard.

Do not set `UMBRA_CANOPY_TOKEN` or model API keys on a public instance.

Claude Desktop / Code remote MCP:

```json
{
  "mcpServers": {
    "umbra": {
      "url": "https://<your-service>.up.railway.app/mcp"
    }
  }
}
```

This is an unofficial community host of Umbra's *open* data, not an Umbra
product. Quicklooks stream their COGs; keep an eye on egress.
