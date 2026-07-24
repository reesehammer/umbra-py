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
    --local \
    --fetch-pmtiles \
    --out ./showcase
```

That writes a self-contained directory:

- `index.html` — a landing page linking the pieces below plus install/docs/source;
- `map.html` — a MapLibre viewer over the whole-catalog `catalog.pmtiles` basemap
  (copied in beside it), so the folder is relocatable;
- `explore.html` — the interactive [`umbra demo`](cli.md) explorer over a
  one-pin-per-site overview of the catalog.

Every page is self-contained HTML, so it needs no extra and no backend. This is
what the repository's own **[hosted showcase](https://reesehammer.github.io/umbra-py/showcase/)**
is: the `.github/workflows/docs.yml` Pages job runs `umbra showcase` after the
mkdocs build and publishes `site/showcase/` beside the docs. Point `--pmtiles`
at a locally built basemap instead of `--fetch-pmtiles` for an offline build, or
pass `--no-explore` for a map-only page.
