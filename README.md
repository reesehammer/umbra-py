# umbra-py

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)

**A Python-first toolkit to make [Umbra](https://umbra.space/open-data/) SAR open data easy to discover, load, download, and analyze.**

Umbra publishes very-high-resolution (down to ~16–25 cm) synthetic aperture
radar (SAR) imagery as open data under a permissive **CC BY 4.0** license. The
data is excellent, but getting started is hard: it ships in specialized formats
(SICD, SIDD, CPHD, GEC), is indexed by a large static STAC catalog, and the
existing tooling is low-level. `umbra-py` aims to make working with it feel as
approachable as working with Sentinel-1 or Landsat.

> **Status:** v0.1 / early alpha. The discovery + download core works against
> Umbra's live catalog today; processing helpers are intentionally minimal and
> will grow (see the [roadmap](#roadmap)).

## Why this exists

- **High barrier to entry** — Umbra's formats aren't well supported by mainstream
  GIS tools; users fall back to low-level libraries and hand-rolled metadata
  parsing.
- **Discovery friction** — the open data lives in a 17+ TB S3 bucket indexed by a
  static STAC catalog with no search API. Finding "the right files for my area
  and dates" is non-trivial.
- **No batteries-included workflows** — searching, downloading the right product,
  and turning it into analysis-ready data each take custom code.

`umbra-py` provides a small, well-documented layer over all of this.

## Install

```bash
pip install umbra-py            # core: search + download + metadata
pip install "umbra-py[load]"    # + analysis-ready xarray loading (xarray, rasterio)
pip install "umbra-py[convert]" # + SICD amplitude extraction (sarpy, rasterio)
pip install "umbra-py[viz]"     # + plotting/footprint helpers
pip install "umbra-py[export]"  # + stac-geoparquet catalog export
pip install "umbra-py[serve]"   # + the umbra serve read-only STAC API
pip install "umbra-py[mcp]"     # + the umbra-mcp Model Context Protocol server
```

Requires Python 3.10+.

## Quickstart

### Python

```python
from umbra_py import UmbraCatalog, download_item

catalog = UmbraCatalog()

# Find geocoded (GEC) scenes over an area, within a date range.
results = catalog.search(
    bbox=(-68.1, 10.4, -67.9, 10.6),   # min_lon, min_lat, max_lon, max_lat
    start="2024-01-01",
    end="2024-01-31",
    product_types=["GEC"],
    limit=5,
)

for item in results:
    print(item.summary())

# Download the GEC GeoTIFF of the first match.
first = next(iter(catalog.search(start="2024-01-01", end="2024-01-01", limit=1)))
paths = download_item(first, dest_dir="downloads", assets=["GEC"])
print(paths)
```

### Search a site by name (exact or fuzzy)

Umbra files every pass of a site under one named task directory, so `area=`
searches by that label — and prunes every other task *before* listing it, so
it is also the fast way to gather the co-located passes `change` / `timescan`
need. `area=` is a case-insensitive substring by default; pass `fuzzy=True`
(CLI `--fuzzy`) to match it loosely — word-order- and punctuation-independent
and tolerant of a small typo. It resolves with plain string arithmetic, **no
model call**, and never drops a result the substring match would have found:

```python
catalog.search(area="Centerfield")                 # substring: "Centerfield, Utah"
catalog.search(area="utah centerfield", fuzzy=True)  # reordered  -> same task
catalog.search(area="centrfield", fuzzy=True)        # small typo -> same task
```

```bash
umbra search --area "utah centerfield" --fuzzy
```

### Browse results in a notebook

In Jupyter, an `UmbraItem` renders as a card — a metadata table beside an
inline sketch of its ground footprint — and `ItemCollection` lays a whole
search out as a gallery. Both are offline and need no extras, so displaying
results never downloads anything:

```python
from umbra_py import UmbraCatalog, ItemCollection

results = ItemCollection(UmbraCatalog().search(area="rome", limit=8))
results  # gallery of metadata cards

# Opt in to streamed SAR quicklook thumbnails (decibel stretch; needs `viz`):
ItemCollection(results, thumbnails=True)
```

### Browse the catalog visually (HTML gallery)

For a shareable contact sheet outside a notebook, `gallery` / `save_gallery`
(and the `umbra gallery` CLI) take a search and render a grid of streamed SAR
quicklook thumbnails into one self-contained HTML page — each tile linking to
its STAC item with a footprint sketch. Only downsampled GeoTIFF overviews are
fetched (via HTTP range requests, in parallel), so you *see* what a search
returned before downloading anything (requires the `viz` extra):

```python
from umbra_py import UmbraCatalog, save_gallery

items = list(UmbraCatalog().search(area="Centerfield", limit=24))
save_gallery(items, "gallery.html")
```

```bash
# Same thing from the shell:
umbra gallery --area Centerfield --out gallery.html --db
```

### See where your search landed

Visualize footprints before downloading multi-GB SAR scenes:

```python
from umbra_py import UmbraCatalog, footprint_map, write_geojson

items = list(UmbraCatalog().search(
    start="2024-01-01", end="2025-12-31", limit=50,
))

# Interactive Folium map for notebooks / sharing (requires the `viz` extra).
footprint_map(items).save("footprints.html")

# Same map, with the actual SAR imagery overlaid. Streams a downsampled
# preview of each GEC cloud-optimized GeoTIFF via HTTP range requests —
# no full download — and embeds the result inline so the HTML is
# self-contained.
footprint_map(items, imagery=True).save("sar_map.html")

# Lazy variant: ship a tiny HTML, fetch each SAR image only when the
# user clicks "Get SAR image" in the popup. Works with any size search.
footprint_map(items, lazy_imagery=True).save("lazy.html")

# Animated timeline: watch Umbra's coverage accumulate across your search
# window with a play button + slider underneath the map. Pairs with
# lazy_imagery=True so you can click any footprint mid-animation.
from umbra_py import timeline_map
timeline_map(items, period="P7D", lazy_imagery=True).save("coverage.html")

# Or export to GeoJSON for QGIS, leafmap, Earth Engine, geopandas, deck.gl, ...
write_geojson(items, "footprints.geojson")
```

Want to *see* a single acquisition without a map or a multi-GB download?
`quicklook` streams a downsampled preview of the GEC GeoTIFF via HTTP range
requests and hands you a `PIL.Image`:

```python
from umbra_py import save_quicklook

# Grayscale linear stretch (the default).
save_quicklook(items[0], "scene.png")

# Decibel stretch + pseudo-color: the radiometrically-correct SAR look that
# brings out terrain texture and urban structure.
save_quicklook(items[0], "scene_db.png", db=True, colormap="magma")
```

### Explore a scene at full resolution (interactive viewer)

A quicklook is one downsampled PNG — it throws away the resolution that makes
Umbra special (a GEC scene is ~25 cm imagery, tens of thousands of pixels on a
side). `view` lets you actually *roam* it: it starts a tiny local tile server
and opens a Leaflet map in the browser. As you pan and zoom, only the tiles in
view stream from the cloud-optimized GeoTIFF via HTTP range requests (at the COG
overview matching your zoom) and are warped onto the web map — native-resolution
exploration with no full download (needs the `viz` extra):

```python
from umbra_py import view

view(items[0])                  # opens the browser; Ctrl-C to stop
view(items[0], db=True)         # decibel stretch, the radiometric SAR look
```

The contrast stretch is computed once over a whole-scene overview and shared by
every tile, so neighbouring tiles don't seam. Tiles are warped through GDAL into
true Web Mercator, so the imagery lines up with the OpenStreetMap basemap. Or
run it from the shell with `umbra view` (below).

### Load a scene as analysis-ready data

When you want the *pixels*, not a picture — to run your own analysis, clip to an
area, or feed a model — load an acquisition straight into a georeferenced
`xarray.DataArray`. Only the window and resolution you ask for stream over HTTP
range requests against the cloud-optimized GeoTIFF, so you can pull a small area
out of a multi-GB scene without downloading the whole thing (requires the
`load` extra):

```python
from umbra_py import UmbraCatalog, to_xarray

item = next(iter(UmbraCatalog().search(start="2024-02-08", end="2024-02-08", limit=1)))

# Full scene, decimated to a manageable size, in decibels.
da = to_xarray(item, max_size=2048, db=True)
da.plot.imshow(cmap="gray")          # xarray's matplotlib accessor
print(da.attrs["crs"], da.attrs["bounds"])

# Or pull just an area of interest (lon/lat) at full resolution.
aoi = to_xarray(item, bbox=(-68.05, 10.45, -68.00, 10.50))
print(aoi.mean().item())             # straight into the scientific Python stack
```

The returned array has `y`/`x` axes in the raster's native CRS, with the CRS,
affine transform, bounds, acquisition metadata, and the CC BY 4.0 attribution
in `da.attrs` — so it round-trips through `rioxarray`
(`da.rio.write_crs(da.attrs["crs"])`), `rasterio`, and `pyproj`.

Want a file instead of an in-memory array (for QGIS, GDAL, ...)? `to_geotiff`
writes the same clipped/decimated scene to a single-band float32 GeoTIFF —
or use the `umbra load` CLI below:

```python
from umbra_py import to_geotiff

to_geotiff(item, "aoi.tif", bbox=(-68.05, 10.45, -68.00, 10.50), max_size=4096)
```

### Fast, repeatable search with a local index

Umbra publishes no STAC API, so every search re-walks the public S3 bucket —
fine once, slow when you search the same data again and again. `CatalogIndex`
persists what a walk discovers into a local SQLite database and answers
searches from SQL, turning repeat (and overlapping) searches into near-instant
local queries. It's a first-class building block: walk once, then query offline
— or build the `.db` on a schedule and ship it as a prebuilt catalog for a
service layered on top.

```python
from umbra_py import CatalogIndex

with CatalogIndex("umbra.db") as index:
    # Pass no filters to index the WHOLE catalog — one crawl, then everything
    # is queryable offline. It's a long, one-time walk (no STAC API, so it
    # lists every task); re-running just refreshes and extends the same db.
    index.build()

    # ...or scope the build to the slice you care about (much faster):
    # index.build(area="centerfield", start="2024-01-01", end="2024-12-31")

    # Now query locally — same filters as UmbraCatalog.search, no network.
    for item in index.search(area="centerfield", product_types=["GEC"]):
        print(item.summary())
```

`CatalogIndex.search` mirrors `UmbraCatalog.search` (bbox / date / product /
area / limit / max_per_task), so you can swap the live walk for the index
without changing anything else. With no path it uses `$UMBRA_INDEX_DB` or
`~/.cache/umbra-py/catalog.db`.

### Share the search: export to stac-geoparquet

One crawl shouldn't be everyone's crawl. `export_geoparquet` (or `umbra index
export`; requires the `export` extra) writes an index out as a single
[stac-geoparquet](https://stac-geoparquet.org/) file — the entire catalog
searchable in seconds with DuckDB, geopandas, pyarrow or rustac, no server,
no crawl, and no umbra-py install needed on the consuming side. Each row is a
full STAC item with a `self` link back to its sidecar JSON, so query results
lead straight to the data files. A [scheduled GitHub
Action](.github/workflows/publish-index.yml) rebuilds the full index weekly
and publishes `umbra-open-data.parquet` (plus the SQLite `catalog.db`) on the
rolling [`catalog-index`
release](https://github.com/reesehammer/umbra-py/releases/tag/catalog-index).

```python
from umbra_py import CatalogIndex, export_geoparquet

with CatalogIndex("umbra.db") as index:
    export_geoparquet(index.search(), "umbra-open-data.parquet")
```

### Skip the crawl entirely: fetch the prebuilt index

Because that weekly workflow already ships a `catalog.db`, you never have to
run the full-bucket crawl yourself. `CatalogIndex.from_release` (or `umbra
index fetch`) downloads the latest snapshot to your default index path, and
`--local` search works immediately:

```python
from umbra_py import CatalogIndex

with CatalogIndex.from_release() as index:   # download the weekly snapshot, then open it
    for item in index.search(area="centerfield", product_types=["GEC"]):
        print(item.summary())
```

`umbra index info` reports the snapshot's build date and age, so you know how
fresh it is; re-run the fetch any time to refresh.

### Render from the index too, not just `search`

The visual commands — `map`, `gallery`, `swipe`, `change`, `timescan` — take the
same `--local` / `--index-db` flags as `search`, so once you've fetched or built
an index they render from it instead of re-walking S3. That turns every repeat
render into a near-instant, offline operation (and is the fast path a demo or
gallery flow needs). The path flag is `--index-db` rather than `--db` because the
render commands already use `--db` for the decibel stretch.

```bash
umbra index fetch                                  # one-time (or 'index build')
umbra map --local --out catalog.geojson            # whole catalog, from SQL, no crawl
umbra gallery --local --area "Centerfield" --out gallery.html --db
umbra change --local --area "Centerfield" --start 2024-01-01 --end 2024-12-31 --out change.png
```

Only acquisitions already in the index are used, so keep it fresh with `umbra
index fetch` (or an incremental `umbra index build`). Without `--local` the
commands walk S3 live exactly as before.

### Command line

```bash
# Fastest start: download the weekly prebuilt snapshot instead of crawling,
# then search it offline. `umbra index info` shows what it holds and how old
# the snapshot is.
umbra index fetch
umbra search --local --area "Centerfield" --product GEC
umbra index info

# Or build the index yourself: index the ENTIRE catalog once (no flags = whole
# bucket), then search offline with --local for near-instant repeats. The full
# build is a long, one-time crawl; re-run any time to refresh.
umbra index build
umbra search --local --area "Centerfield" --product GEC
umbra index info

# Export the index as one stac-geoparquet file: the whole catalog searchable
# in seconds by DuckDB / geopandas / pyarrow, no server (needs [export]).
umbra index export --out umbra-open-data.parquet

# Or scope the build to just a slice (much faster than the whole bucket):
umbra index build --area "Centerfield" --start 2024-01-01 --end 2024-12-31

# Search by area, dates and product type.
umbra search --bbox -68.1,10.4,-67.9,10.6 --start 2024-01-01 --end 2024-01-31 --product GEC

# Or search by place name -- geocoded to a bounding box via OpenStreetMap.
# Works on `search`, `map`, `gallery`, and `timescan`; mutually exclusive
# with --bbox.
umbra search --place "California" --start 2024-01-01 --end 2024-12-31

# Inspect a single item by its STAC JSON URL.
umbra info <item-json-url>

# Feed an agent: `info --json` emits an explanation-rich context card (per-
# product explanations, the polarization caveat, the CC-BY line); `umbra
# context` prints the library's product-type table and search semantics as
# JSON; `umbra llms-txt` prints the same as an llms.txt-convention Markdown
# guide (add --full for the self-contained bundle: domain knowledge + the full
# CLI reference + a per-module map). The committed llms.txt / llms-full.txt at
# the repo root are that output.
umbra info <item-json-url> --json
umbra context
umbra llms-txt --full

# Download specific asset(s).
umbra download <item-json-url> --asset GEC --dest downloads/

# Render a standalone SAR quicklook image -- no map, no full download.
# Add --db for the decibel stretch and --colormap for pseudo-color.
umbra quicklook <item-json-url> --out scene.png --db --colormap magma

# Explore one scene at full resolution in the browser: a local tile server
# streams only the tiles in view from the COG and warps them onto a Leaflet
# map. Pan/zoom to native resolution, no full download. Ctrl-C to stop.
umbra view <item-json-url> --db

# Browse a search visually: one self-contained HTML contact sheet of streamed
# SAR thumbnails, each tile linking to its STAC item. No full downloads.
umbra gallery --area "Centerfield" --out gallery.html --db

# Load an analysis-ready GeoTIFF -- clip to an area and/or decimate, no full
# download. Streams only the requested window of the cloud-optimized GeoTIFF.
umbra load <item-json-url> --out aoi.tif --bbox -68.05,10.45,-68.0,10.5 --max-size 4096

# Visualize search results: interactive HTML map or GeoJSON for any GIS.
umbra map --start 2024-01-01 --end 2024-01-31 --product GEC --out footprints.html
umbra map --start 2024-01-01 --end 2024-01-31 --product GEC --out footprints.geojson

# Same, but overlay the actual SAR imagery on the basemap.
umbra map --start 2024-01-01 --end 2024-01-31 --product GEC --imagery --out sar_map.html

# Tiny HTML + "Get SAR image" button per popup that streams the COG in
# the browser on click. Combine with --timeline for click-to-see SAR on
# any footprint mid-animation.
umbra map --start 2024-01-01 --end 2024-06-30 --product GEC --max-per-task 1 \
    --timeline --timeline-period P7D --lazy-imagery --out coverage.html

# Interactive before/after swipe map: drag a divider to wipe the earliest
# pass of a site over the latest and watch what changed. Self-contained HTML.
umbra swipe --area "Centerfield" --start 2024-01-01 --end 2024-12-31 --out swipe.html --db

# Timescan: collapse a whole time series of a site into one image. Per pixel,
# red=mean, green=peak, blue=temporal variability. Stable ground reads
# gray/yellow; anything that came and went over the series glows blue/cyan.
umbra timescan --area "Centerfield" --start 2024-01-01 --end 2024-12-31 --out timescan.png --db
```

### Drive it from an AI agent (MCP)

Umbra publishes no STAC API, so this library *is* the query layer — and
`umbra-mcp` exposes that layer over the [Model Context
Protocol](https://modelcontextprotocol.io/), turning any MCP client (Claude
Desktop / Code and others) into a natural-language front door to the archive.
*"Show me what changed at Centerfield, Utah this spring"* becomes a first-run
experience instead of a tutorial chapter.

```bash
pip install "umbra-py[mcp]"
umbra mcp            # run the stdio server (also: umbra-mcp, or uvx umbra-mcp)
```

Register it with an MCP client (Claude Desktop shown):

```json
{
  "mcpServers": {
    "umbra": { "command": "umbra-mcp" }
  }
}
```

The server offers `search_catalog`, `get_item`, `geocode_place`, `index_stats`,
`quicklook`, `change_composite`, `timescan` and `download_asset` tools; a
`umbra://context` resource with the product-type table and search semantics;
and packaged `monitor-site` / `survey-region` prompts. The imagery tools return
the rendered PNG as an MCP image block, so the model *sees* the radar scene. In
keeping with the library's design, the server stays deterministic — it
searches, geocodes and renders; the client's model plans and narrates. It even
refuses to composite mixed polarizations (HH and VV aren't comparable), and the
CC-BY attribution line travels with every result.

### Serve it as a STAC API (`umbra serve`)

Umbra publishes a *static* STAC catalog and no search API — which is exactly
what breaks the standard geospatial tooling: `pystac-client`, the QGIS STAC
plugin, `stac-browser` and leafmap all expect a STAC API *search* endpoint.
`umbra serve` restores one: a read-only STAC API over your local catalog index,
so any STAC client can query Umbra's open archive like Sentinel-1 or Landsat.
It's the browser-facing sibling of the MCP server — same index underneath.

```bash
pip install "umbra-py[serve]"
umbra index fetch                 # grab the prebuilt catalog.db (one-time)
umbra serve                       # http://127.0.0.1:8000  (OpenAPI docs at /docs)
```

```python
# Point any STAC API client at it:
from pystac_client import Client

client = Client.open("http://127.0.0.1:8000")
items = client.search(bbox=[-112.1, 39.0, -111.9, 39.2], datetime="2024-01-01/..").items()
```

It serves the STAC API landing page, `/conformance`, `/collections`,
`/collections/{id}/items` and STAC item search over `GET`/`POST /search` (bbox,
datetime, ids, pagination), with a generated OpenAPI document at `/openapi.json`
and interactive docs at `/docs`. Queries hit the local index, so they answer in
milliseconds; `umbra serve --live` walks S3 per request instead if you'd rather
not build an index first.

## What the data looks like

Each Umbra acquisition is a STAC item exposing these assets, from easiest to
most raw:

| Asset | What it is | Use it for |
|-------|------------|------------|
| `GEC`  | Geocoded Ellipsoid Corrected, cloud-optimized GeoTIFF | Quick, map-ready imagery. **Start here.** |
| `SIDD` | Geocoded detected image (NITF) | Detected imagery in a standard format |
| `SICD` | Complex data in the radar slant plane (NITF) | Phase-preserving analysis, InSAR inputs |
| `CPHD` | Compensated phase history (raw signal) | Custom image formation |

## Data license & attribution

Umbra's underlying imagery is licensed **CC BY 4.0**. If you use or redistribute
the data or derived products you must attribute Umbra, e.g.:

> Contains Umbra open data, licensed under CC BY 4.0.

`umbra-py` itself is licensed under **Apache 2.0** (see [LICENSE](LICENSE)). The
code license and the data license are independent and compatible.

## Roadmap

- **v0.1 (now):** STAC search with date/bbox/product pruning, anonymous downloads
  with resume, metadata summaries, CLI.
- **v0.2:** analysis-ready loading (xarray/rioxarray), footprint visualization,
  example notebooks, SICD → geocoded COG.
- **v0.3+:** change-detection and RTC recipes, QGIS / Earth Engine integration,
  ML dataset prep, cloud-native batch workflows.

See [CONTRIBUTING.md](CONTRIBUTING.md) to get involved.

## Acknowledgements

Built on the shoulders of the SAR open-source community, including
[`sarpy`](https://github.com/ngageoint/sarpy) and Umbra's open data program.
Not affiliated with or endorsed by Umbra Lab, Inc.
