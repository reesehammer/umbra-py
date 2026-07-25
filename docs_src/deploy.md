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
- `featured/*.png` — one precomputed change composite per featured site.

Every page is self-contained HTML, so it needs no extra and no backend. This is
what the repository's own **[hosted showcase](https://reesehammer.github.io/umbra-py/showcase/)**
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

### Featured change composites

`--featured N` renders a change composite for the `N` most repeat-imaged sites in
the catalog and puts them on the landing page, so a first-time visitor sees *what
SAR change looks like* immediately — no render round-trip, no server, and it
still works on a plain static host. Name the sites yourself with a repeatable
`--featured-area` instead, and pick two-colour (green = new, magenta = gone) or
three-date temporal RGB with `--featured-frames`:

```bash
umbra showcase --local --featured-area "Centerfield, Utah" \
    --featured-area "Beet Piler" --featured-frames 3 --out ./showcase
```

This is the one step that needs the `viz` extra
(`pip install "umbra-py[viz]"`); it streams only a downsampled overview of each
scene, and a site whose asset won't render is skipped with a warning rather than
failing the build. Without `--featured` the showcase is unchanged and stays
stdlib-only.
