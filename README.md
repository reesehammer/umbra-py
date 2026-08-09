# umbra-py

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![CI](https://github.com/reesehammer/umbra-py/actions/workflows/ci.yml/badge.svg)](https://github.com/reesehammer/umbra-py/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/reesehammer/umbra-py/branch/main/graph/badge.svg)](https://codecov.io/gh/reesehammer/umbra-py)
[![Docs](https://img.shields.io/badge/docs-reesehammer.github.io%2Fumbra--py-informational.svg)](https://reesehammer.github.io/umbra-py/)

**A Python-first toolkit to make [Umbra](https://umbra.space/open-data/) SAR open data easy to discover, load, download, and analyze.**

📖 **Documentation:** the full guide and API reference live at
**<https://reesehammer.github.io/umbra-py/>** (built from this repo with
`mkdocs build`; sources under [`docs_src/`](docs_src/)).

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
pip install "umbra-py[dask]"    # + lazy, chunked datacubes: to_stack(lazy=True) / umbra stack --lazy
pip install "umbra-py[convert]" # + SICD amplitude extraction (sarpy, rasterio)
pip install "umbra-py[viz]"     # + plotting/footprint helpers
pip install "umbra-py[export]"  # + stac-geoparquet catalog export
pip install "umbra-py[serve]"   # + the umbra serve read-only STAC API
pip install "umbra-py[mcp]"     # + the umbra-mcp Model Context Protocol server
pip install "umbra-py[langchain]" # + the catalog as native LangChain / LangGraph tools
pip install "umbra-py[llamaindex]" # + the catalog as native LlamaIndex tools
pip install "umbra-py[ai]"      # + umbra ask / semantic / describe / embed: model-backed NL search, scene reading & visual similarity
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

### Stack a time series into a datacube

Multi-date analysis needs the passes *co-registered*: the same output pixel has
to mean the same patch of ground on every date, or a `diff` measures
misalignment instead of change. That is real work against Umbra, because
successive passes over a site arrive in whatever UTM zone and extent each
acquisition used — which is why `stackstac` / `odc-stac` can't be pointed at
them. `to_stack` does it, turning a search result into a labelled
`(time, y, x)` cube (requires the `load` extra):

```python
from umbra_py import UmbraCatalog, to_stack

passes = list(UmbraCatalog().search(area="Centerfield", polarizations=["VV"], limit=12))

cube = to_stack(passes, max_size=1024, db=True)   # (time, y, x), EPSG:4326, NaN-masked
print(cube.sizes, cube["item_id"].values)

baseline = cube.isel(time=slice(0, 3)).mean("time")   # pre-event average
delta = cube.isel(time=-1) - baseline                 # dB change vs. that baseline
activity = cube.std("time")                           # where the scene keeps changing
```

Slices are ordered oldest-first, each keeps its `item_id`, and nodata is `NaN`
so cross-date statistics aren't poisoned by fill. By default the cube covers
only the ground *every* pass saw (`extent="intersection"`), so no cell has a
gap; `extent="union"` keeps all of it and pads each slice with `NaN` instead.
Stack **one polarization** — mixing VV and VH puts a polarization difference on
the time axis where you'll read it as change.

The default grid is lon/lat, whose cells stretch with latitude — fine for
comparing a cell to *itself* across dates, but degrees are not a unit of ground,
so a count of changed cells isn't an area. Pass `crs="utm"` (or any CRS) to
build the shared grid in metres instead, and counting cells becomes measuring:

```python
cube = to_stack(passes, max_size=1024, db=True, crs="utm")  # equal-area cells
changed = (cube.isel(time=-1) - cube.isel(time=0)) < -3     # 3 dB darker
xres, yres = cube.attrs["transform"][0], cube.attrs["transform"][4]
hectares = float(changed.sum()) * abs(xres * yres) / 10_000
```

`stack_to_geotiff` (and `umbra stack --crs`, below) writes the same cube as a
multi-band GeoTIFF — one band per acquisition, each described by its timestamp —
for QGIS, GDAL, or anything that isn't Python. Where `umbra change` and
`umbra timescan` render this comparison as a *picture*, this is the *numbers*.

A cube costs `max_size²` × the number of passes in memory, so a long series used
to have to be stacked coarse. `lazy=True` removes that trade-off — each pass
becomes one `dask` chunk, fetched only when something asks for its values
(requires the `dask` extra):

```python
cube = to_stack(passes, max_size=4096, db=True, crs="utm", lazy=True)
cube.chunks[0]                      # (1, 1, 1, ...) — one chunk per acquisition
cube.isel(time=-1).load()           # only that pass is fetched
stack_stats(cube)                   # walks the series a slice at a time
```

Nothing is read until you ask (the grid is still resolved up front, so a
non-overlapping series still fails immediately), and the consumers that reduce a
cube — `stack_stats` and `stack_to_geotiff` / `umbra stack --lazy` — walk it one
slice at a time, so peak memory follows `max_size` rather than the length of the
series. The values are identical either way; only what is resident differs.

One chunk per pass still makes a whole slice the smallest unit of work, so a
single pass at a big `max_size` is read and held whole (8192 px is 256 MB of
`float32`). `chunk_size` cuts each pass into windows read — and written —
independently, so how sharp a cube can be stacked stops depending on how much of
*one scene* fits in memory:

```python
cube = to_stack(passes, max_size=8192, crs="utm", lazy=True, chunk_size=1024)
cube.chunks[1]                      # (1024, 1024, ...) — windows within a pass
stack_to_geotiff(passes, "cube.tif", max_size=8192, lazy=True, chunk_size=1024)
```

It costs range requests (one read per window instead of one per pass), so keep
the window a decent fraction of the grid rather than a tile. The numbers — and
the written file — are unchanged.

And when the answer you want *is* a number, `stack_stats` reduces the cube for
you — no array handling at all:

```python
from umbra_py import stack_stats

stats = stack_stats(cube)                   # JSON-ready, no pixels
stats["net_change"]["mean_delta_db"]        # -4.7  (first pass → last)
stats["net_change"]["changed_area_km2"]     # 1.83  (cells past 3 dB, needs crs=)
stats["passes"][-1]["change_vs_previous"]   # the latest pass alone
```

Each pass gets its distribution (mean/median/spread) and the signed decibel
change against the pass before it, so a series reads as a trend rather than a
pile of scenes. Change is always in dB — a ratio of backscatter is a difference
on the log scale — and `changed_area_km2` is `None` unless the grid is projected,
because counting geographic cells measures nothing. `umbra stack --stats` prints
the same object, and the `stack_stats` agent tool returns it over MCP /
LangChain / LlamaIndex.

A summary also says **what speckle alone would have done**, because on SAR that
is the number that decides how to read every other one. Speckle is not an error
bar on a mean: it is the dominant variation in a single cell, and on single-look
imagery of ground that did *not* change it moves two cells in three past a 3 dB
threshold. So every multi-pass summary carries a `detection` block:

```python
stats["detection"]["looks"]                 # 1.02  — measured off the cube's own blocks
stats["detection"]["cell_sigma_db"]         # 7.76  — an unchanged cell's pass-to-pass spread
stats["detection"]["false_alarm_fraction"]  # 0.664 — read this against changed_fraction
stats["detection"]["target_threshold_db"]   # 15.7  — what a 5 % false-alarm rate costs
stats["passes"][0]["looks"]                 # each pass's own reading
```

The rates are exact rather than approximated (an L-look intensity is a gamma
variate, and a normal approximation on the decibel axis is badly wrong near one
look), the looks are read off the *cube* — `to_stack` decimates onto a shared
grid, and decimation averages speckle down — and scene structure biases that read
low, so the floor is an upper bound on the false alarms rather than a flattering
estimate. A caveat quotes it on every multi-pass cube, and says so outright when
the observed change does not stand clear of it. It is also what prices
`speckle_filter=` in the units of the answer: a filtered cube reads more looks,
so its floor, its spread and its 5 % threshold all come down.

A summary also says **what it measured**. Rasters `umbra convert` produced carry
their calibration, noise-floor subtraction, terrain model and amplitude scale in
`UMBRA_*` GeoTIFF tags
(above), and the loaders read them: `to_xarray` / `to_stack` surface the record
as `attrs["provenance"]`, `stack_stats` reports it and swaps its "these decibels
are relative" caveat for a calibrated one, and a GeoTIFF written from the cube
keeps the tags. The other half of reading them is a refusal — `to_stack` will
not co-register passes whose calibration, noise-floor subtraction, terrain model
or scale disagree (a
raster with no tags at all, such as a published GEC, counts as its own kind), because
the difference between two conversions would land on the time axis and read as
change on the ground. It's the same "a mixed selection is not a measurement"
rule `POST /artifacts/stats` applies to polarization, and it surfaces there as a
`400` too. `umbra change --narrate` refuses on the same rule, because its
per-block decibel grid is a measurement between two passes even though the
composite beside it is a picture — and when the passes do agree, the record they
share travels into the narration sidecar and into what the model is told, so a
quoted dB delta says whether it is a calibrated coefficient or relative
amplitude. The commands that only draw (`umbra change` without `--narrate`,
`timescan`, `swipe`) don't check: a mixed composite is confusing to look at, a
mixed number is wrong.

That refusal used to be discoverable only by hitting it, and its advice — "use
only the acquisitions that share one" — named a subset it couldn't identify.
`stack_provenance` asks first:

```python
from umbra_py import stack_provenance

report = stack_provenance(passes)
report.agrees                  # False: this selection is two conversions
report.refusal                 # verbatim the ValueError to_stack would raise
report.largest.item_ids        # ...and the biggest set that *does* agree
report.largest.hrefs           # the URLs to re-run on
```

It reads each source's `UMBRA_*` record straight from the raster header, so it
costs the opens a stack pays for anyway — one range request of kilobytes per
acquisition — and saves everything after them: the shared grid, the warp and
every decimated pixel read. The verdict is `to_stack`'s own function called on
the same records, so a cleared selection can't then be turned away by the stack
it cleared. A source that can't be *read* lands on `report.unreadable` rather
than in a group, because a failed read isn't a product saying its pixels are
something else — it makes the answer incomplete, not the series mixed.

```console
$ umbra stack --area Centerfield --start 2024-01 --provenance
3 acquisition(s) fall into 2 conversions, so this series is not one measurement.
  2x calibration=gamma0, rtc_model=facet, scale=decibels, units=dB (gamma0)
      2024-01-08T12:04:11Z_UMBRA-04
      2024-02-11T11:58:02Z_UMBRA-05
  1x calibration=sigma0, scale=decibels, units=dB (sigma0)
      2024-03-02T12:01:44Z_UMBRA-04
  Largest agreeing subset: 2 of 3.
  Re-run on those alone:
    umbra stack 'https://.../2024-01-08...json' 'https://.../2024-02-11...json'
```

`--json` emits the same report as one object. It's the move `umbra preflight`
makes for a chip run, one layer up: ask the cheap question first, and ask it
with the function that would refuse.

The same question reaches the front doors built so nobody has to install
anything. `POST /artifacts/provenance` on `umbra serve` takes the body you would
send to `POST /artifacts/stats` and answers with that report — a mixed selection
is a `200` carrying the refusal and the largest agreeing subset, not the `400`
the stats call would have spent — and `stack_provenance(urls=[...])` is an agent
tool on MCP / LangChain / LlamaIndex, so a model that hits the refusal can ask
which passes *do* agree instead of guessing a subset. All three emit the same
document.

Every number above carries one uncertainty none of those corrections touch.
Coherent illumination of a rough surface interferes with itself, so a
single-look pixel's power scatters about its surface's true backscatter with a
standard deviation *equal to its mean* — that's speckle, and averaging is the
only thing that removes it. `speckle_filter=` does the averaging on the cube's
shared grid, which is the only place it reaches the products this library is
mostly used with: `umbra convert --speckle-filter` filters complex SICDs before
geocoding, so it never sees a published GEC.

```python
cube = to_stack(passes, max_size=1024, db=True, crs="utm", speckle_filter="lee")
cube.attrs["provenance"]        # {'speckle_filter': 'lee', 'speckle_window': '5'}
stack_stats(cube)["caveats"]    # ...and the summary states what the window cost
```

`"boxcar"` averages the window unconditionally (the multilook — most variance
removed, blind to the edge it averages across); `"lee"` averages only where a
window is no more variable than speckle alone explains, so edges, points and
textured ground survive. Both work in the power domain, where a mean is the
surface's backscatter rather than the ~2.5 dB-low geometric mean a mean of
decibels would give.

It's opt-in, because what it spends is the reason to use this archive: a window
that averages N cells reports ground N cells across. So the cube records both
halves in the same `provenance` keys `umbra convert` writes — `stack_stats`
states the trade, a written GeoTIFF carries it, and stacking a filtered cube
against an unfiltered pass is refused by the rule above. Sources that already
record a filter are refused rather than filtered twice (two averagings leave a
resolution neither window names). It composes with `chunk_size`: each window is
read with a half-window halo and cropped after filtering, so a filtered window
holds the cells the whole-pass filter would have put there, and `lee`'s speckle
parameter is read once per pass rather than per window.
`umbra stack --speckle-filter lee --speckle-window 5` is the same on the CLI.

Measuring reads a whole slice per pass, so a cube `chunk_size` let you *write*
sharper than memory was still capped at what you could *measure*.
`windowed=True` walks the same windows the cube is chunked into:

```python
cube = to_stack(passes, max_size=8192, crs="utm", lazy=True, chunk_size=1024)
stats = stack_stats(cube, windowed=True)     # three windows resident, not three slices
stats["quantile_method"]                     # "histogram" — see below
```

Every count, mean, standard deviation and change number stays exact (each is a
sum, so a window folds in). The one number that cannot be: a percentile needs the
whole pass at once, so `median` / `p5` / `p95` become histogram estimates good to
about 0.05 dB — and the summary says so (`quantile_method`, `quantile_bin_db` and
a caveat) rather than letting an estimate pass for a measurement.
`umbra stack --stats-windowed` is the same switch on the CLI.

A scene-wide mean hides a change confined to one corner, so `blocks=N` cuts the
cube into an N×N grid and answers *where* and *when* together:

```python
spatial = stack_stats(cube, blocks=6)["spatial"]
spatial["peak_block"]["compass"]                    # 'northeast'
spatial["peak_block"]["center_lonlat"]              # [-115.81, 37.24]  (map it)
spatial["peak_block"]["peak_interval"]["to_datetime"]  # when it moved
print(spatial["grid_text"])                         # north-up dB heat-grid
```

```text
 +0.1  +0.2  +0.1  +0.0  +1.1 +11.8
 +0.1  +0.0  -0.1  +0.3  +2.4  +9.6
   .   +0.1  +0.0  +0.1  +0.2  +0.4
```

Every block carries its own `net_change` (same fields as the scene-wide one),
its bounds in the cube's CRS, a lon/lat centre to map or geocode it by, and the
consecutive pair of passes it moved most between — so "the site changed 1.4 dB"
becomes "the northeast brightened 12 dB, between the February and March passes".
`.` marks ground no two passes both observed. `umbra stack --blocks 6` prints it
(and implies `--stats`), and the agent tools take the same `blocks` argument.

A peak interval says how hard a block moved, not what its history looked like —
a corner that drifted 3 dB every pass and one that jumped 12 dB once and held
both report a single number. `block_series=True` keeps the whole sequence each
peak was picked from:

```python
spatial = stack_stats(cube, blocks=6, block_series=True)["spatial"]
[s["mean_delta_db"] for s in spatial["blocks"][5]["series"]]  # [0.1, 0.2, 11.8]
```

Every consecutive step, oldest first, in the same shape as `peak_interval` — so
the trend is plottable rather than inferred. It is the largest thing this
reduction emits (`blocks²` × `passes−1` records), so it is opt-in and needs a
`blocks` grid to hang on. `umbra stack --blocks 6 --block-series` prints it, the
agent tools take the same `block_series` argument, and `POST /artifacts/stats`
accepts `"block_series": true`.

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
and publishes `umbra-open-data.parquet` (plus the SQLite `catalog.db`, a
whole-catalog `catalog.pmtiles` vector basemap, and a `catalog.html` viewer for
it) on the rolling [`catalog-index`
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

The published snapshot also arrives **pre-labelled**: each acquisition carries a
reverse-geocoded place name (`item.place`), so maps, galleries and `umbra demo`
show "Reykjavík, Iceland" instead of a task codename with no geocoding at render
time. On an index you built yourself, `umbra index bake --by-site` does the same
— one lookup per site rather than per acquisition, which is what keeps labelling
a whole catalog within OpenStreetMap Nominatim's ~1 request/sec policy.

The same release also ships a **whole-catalog `catalog.pmtiles` basemap** built
from that snapshot, so you get a fast, zoom-anywhere map of the *entire* archive
without tiling it yourself. `umbra tiles --fetch` (or
`fetch_prebuilt_pmtiles()`) downloads it; add `--viewer catalog.html` for a
ready-to-open MapLibre GL page — the visual sibling of `umbra index fetch`.

```bash
umbra tiles --fetch --viewer catalog.html   # whole-archive map, no crawl, no index
```

And it ships the **baked SAR previews** as a separate `catalog.thumbs.db`
sidecar. A quicklook otherwise costs a cloud-optimized GeoTIFF overview streamed
per scene at render time, so the workflow bakes them once and
`umbra index fetch-thumbnails` merges them into your index — after which `umbra
serve`'s `/artifacts/thumbnail/{id}.png`, the `umbra demo` preview and a
`--local` gallery all read local bytes with no range read at all. The pixels are
a separate, opt-in file precisely so `umbra index fetch` stays small:

```bash
umbra index fetch                    # metadata only (small)
umbra index fetch-thumbnails         # + the baked previews (opt-in)
```

On an index you baked yourself, `umbra index export-thumbnails` writes the same
sidecar to share — the bake is the one derived artifact worth moving rather than
recomputing. Every preview carries what it is a picture of (the asset and the
size it was baked at), so a merge keeps your local bake unless the incoming one
is a *larger* preview of the same product, rather than keeping whichever
happened to arrive first.

Once you have an index, `umbra index update` freshens it *cheaply* instead of
re-fetching or re-crawling the whole bucket: it reads the newest acquisition date
already indexed and re-walks only from there (minus `--overlap-days`, default 1,
to catch near-real-time publish lag), so a weekly refresh reads just the new
passes. `CatalogIndex.update()` returns the tally (`added` / `refreshed`). The
bound is on *acquisition* date, so for guaranteed completeness over back-dated
late arrivals, widen `--overlap-days` or run a full `umbra index build`.

```bash
umbra index fetch                 # bootstrap once
umbra index update                # later: pull only acquisitions published since
umbra index update --since "2 weeks ago" --overlap-days 3   # or force the window
```

Or skip the explicit refresh step: `umbra search --local --live` reads
*through* the index to the bucket in one call. It answers from the local index
**and** walks only acquisitions newer than the index's freshest pass, merges the
two, and caches anything new it finds — so a repeat search stays near-instant
but is never staler than the moment you run it. `CatalogIndex.search_live()` is
the API; pass `refresh=False` to leave a shared read-only snapshot untouched.

```bash
umbra index fetch                 # bootstrap once
umbra search --local --live --area "Centerfield"   # fast (index) + fresh (live delta)
```

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
index fetch`, a cheap incremental `umbra index update`, or a full `umbra index
build`. Without `--local` the commands walk S3 live exactly as before.

### Search the commercial archive too (Canopy)

The open data is a slice of what Umbra images. Umbra's commercial product,
[Canopy](https://docs.canopy.umbra.space/), exposes a *real* STAC API over the
full archive — so if you have a Canopy token, the **same `search()` call** can
query it. Pass a `token` and nothing else changes: the same filters, the same
`UmbraItem` results, so every downstream verb (download, quicklook, change,
chips, …) works unchanged.

```python
from umbra_py import UmbraCatalog

# Open data (default) — no account needed.
open_hits = UmbraCatalog().search(area="Centerfield", limit=5)

# Commercial archive — same call, one extra argument.
archive = UmbraCatalog(token="your-canopy-token")
for item in archive.search(bbox=(-118.3, 33.7, -118.1, 33.8), start="2024", limit=5):
    print(item.summary())
```

On the command line, `--token` (or the `UMBRA_CANOPY_TOKEN` environment
variable) switches `umbra search` to the commercial archive — and the same flag
works on the render and analysis verbs (`map`, `gallery`, `change`, `timescan`,
`swipe`, `chips`), so a paying customer discovers *and* renders the archive they
pay for with the identical commands:

```bash
export UMBRA_CANOPY_TOKEN=your-canopy-token
umbra search --start "3 months ago" --bbox="-118.3,33.7,-118.1,33.8" --limit 5
# ...then analyse the same commercial archive with the same flags:
umbra change --token "$UMBRA_CANOPY_TOKEN" --area "Port of Long Beach" \
  --start 2024 --out change.png
```

`bbox` and the date bounds are sent to the STAC API; `--product` and
`--area`/`--fuzzy` are applied to the returned items exactly as on the open-data
path. `--token` is mutually exclusive with `--local` / `--index-db` (which read a
local open-data index), and the token is only ever sent to the Canopy endpoint,
never the open bucket. Learn what you built on the free data, then point the same
commands at the archive you pay for.

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

# Later, refresh an existing index cheaply -- re-walks only acquisitions newer
# than what's already indexed instead of re-crawling the whole bucket.
umbra index update

# Export the index as one stac-geoparquet file: the whole catalog searchable
# in seconds by DuckDB / geopandas / pyarrow, no server (needs [export]).
umbra index export --out umbra-open-data.parquet

# Or scope the build to just a slice (much faster than the whole bucket):
umbra index build --area "Centerfield" --start 2024-01-01 --end 2024-12-31

# Search by area, dates and product type.
umbra search --bbox -68.1,10.4,-67.9,10.6 --start 2024-01-01 --end 2024-01-31 --product GEC

# Or search by place name -- geocoded to a bounding box via OpenStreetMap.
# Mutually exclusive with --bbox.
umbra search --place "California" --start 2024-01-01 --end 2024-12-31

# Or by a real area of interest: a GeoJSON polygon (file or inline string),
# which keeps only footprints that actually overlap the shape rather than its
# bounding rectangle.
umbra search --intersects aoi.geojson --start 2024-01-01

# --bbox / --place / --intersects are one group, and every command that
# gathers acquisitions by search takes all three -- the renders (`map`,
# `gallery`, `change`, `timescan`, `swipe`, `demo`, `tiles`, `showcase`), the
# analysis commands (`stack`, `chips`, `embed build`), the index builds
# (`index build` / `index update`) and `watch`. So an AOI polygon narrows a
# change composite exactly as it narrows a search:
umbra change --intersects aoi.geojson --start 2024-01-01 --out change.png

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

# Co-register a site's whole series into one analysis-ready datacube: a
# multi-band GeoTIFF, one pixel-aligned band per acquisition, oldest first.
umbra stack --area "Centerfield" --pol VV --db --out centerfield.tif
# ...on a metric, equal-area grid instead of lon/lat (for area measurements).
umbra stack --area "Centerfield" --pol VV --db --crs utm --out centerfield_utm.tif
# ...with speckle averaged down first, so a cell-to-cell difference is change
# rather than interference. The cube records the window it spent to get there.
umbra stack --area "Centerfield" --pol VV --db --crs utm --stats \
    --speckle-filter lee --speckle-window 5

# Visualize search results: interactive HTML map or GeoJSON for any GIS.
umbra map --start 2024-01-01 --end 2024-01-31 --product GEC --out footprints.html
umbra map --start 2024-01-01 --end 2024-01-31 --product GEC --out footprints.geojson

# Map one site's coverage by name -- no bounding box to look up first.
# --fuzzy accepts 'utah centerfield' or 'centrfield' too.
umbra map --area "Centerfield" --out centerfield_coverage.html

# Same, but overlay the actual SAR imagery on the basemap.
umbra map --start 2024-01-01 --end 2024-01-31 --product GEC --imagery --out sar_map.html

# Tiny HTML + "Get SAR image" button per popup that streams the COG in
# the browser on click. Combine with --timeline for click-to-see SAR on
# any footprint mid-animation.
umbra map --start 2024-01-01 --end 2024-06-30 --product GEC --max-per-task 1 \
    --timeline --timeline-period P7D --lazy-imagery --out coverage.html

# Self-serve interactive explorer: ONE HTML page over a whole slice of the
# catalog with client-side filters (search box, date range, product-type and
# polarization chips), clustered markers that scale past a plain map, and click-to-quicklook
# SAR overlays. Reads a prebuilt index with --local for a near-instant build.
umbra demo --local --max-per-task 1 --out explorer.html

# Point the explorer at a running `umbra serve` to render change/timescan/swipe
# products over the currently-filtered acquisitions on demand (the "Analyze this
# view" panel). Without --server-url the page stays a static single file.
umbra demo --local --area "Centerfield" --server-url http://localhost:8000 --out explorer.html

# ...or point the same explorer at a whole-catalog PMTiles archive and it covers
# the ENTIRE catalog instead of a searched slice: the browser range-reads only
# the tiles in view, so the filters work over every acquisition from a page that
# stays a few KB, with footprint outlines appearing as you zoom in and the same
# click-to-quicklook SAR overlay on every scene the archive references a COG for.
# Nothing is searched or embedded -- the archive is the data.
umbra tiles --fetch --out catalog.pmtiles
umbra demo --pmtiles catalog.pmtiles --out explorer.html

# Whole catalog on one map, fast: tile every acquisition into a single-file
# PMTiles vector archive (no tile server, no tippecanoe -- pure standard
# library). A map fetches only the tiles in view, so it scales past the point
# where embedding every footprint in the page stops being fast. Each scene is
# tiled as a centroid at every zoom and as its clipped footprint polygon from
# zoom 6 down, so zooming in shows coverage shape, not just a marker (add
# --no-footprints for a smaller centroids-only archive). Each feature also
# references its GEC cloud-optimized GeoTIFF, so a viewer over the archive can
# stream the picture on click (--cog-asset picks the product, --no-cog omits it).
# --viewer also writes a MapLibre GL page that renders it; host the two side by
# side.
umbra tiles --local --out catalog.pmtiles --viewer catalog.html

# ...or skip the tiling entirely: the weekly workflow already publishes a
# ready-made whole-catalog basemap, so fetch it (no crawl, no index needed).
umbra tiles --fetch --out catalog.pmtiles --viewer catalog.html

# Interactive before/after swipe map: drag a divider to wipe the earliest
# pass of a site over the latest and watch what changed. Self-contained HTML.
umbra swipe --area "Centerfield" --start 2024-01-01 --end 2024-12-31 --out swipe.html --db

# Timescan: collapse a whole time series of a site into one image. Per pixel,
# red=mean, green=peak, blue=temporal variability. Stable ground reads
# gray/yellow; anything that came and went over the series glows blue/cyan.
umbra timescan --area "Centerfield" --start 2024-01-01 --end 2024-12-31 --out timescan.png --db

# Chip a site's passes into fixed-size georeferenced ML tiles + a manifest.
umbra chips --area "Centerfield" --start 2024-01-01 --end 2024-12-31 --out chips/ --chip-size 512 --db

# Before spending the downloads: ask which complex passes can support the
# measurement at all. Reads each SICD's metadata out of the NITF by range
# request -- tens of kilobytes of a multi-gigabyte product -- and applies the
# conversion's own support check, so a calibrated chip run is decided before a
# single scene is fetched.
umbra preflight --area "Centerfield" --start 2024-01-01 --end 2024-12-31 --calibrate gamma0

# Geocode a downloaded SICD (complex) product to a north-up EPSG:4326 COG that
# opens straight on a map / in QGIS / via umbra_py.to_xarray. --slant-plane
# instead writes the raw, ungeoreferenced amplitude for quick inspection.
umbra convert scene_SICD.nitf scene_geocoded.tif

# Terrain-orthorectify against a DEM (any rasterio-readable raster, e.g. a
# Copernicus/SRTM COG) so relief lands in its true ground position, not on a
# single flat height plane. --dem supersedes the flat-earth projection.
umbra convert scene_SICD.nitf scene_ortho.tif --dem copernicus_dem.tif

# Make the pixel values physical, not just relative: --calibrate applies the
# SICD's own radiometric scale factors, so the decibels are a backscatter
# coefficient (sigma0/beta0/gamma0) or an absolute RCS in m2 -- comparable
# across scenes and dates. With --rtc it's a terrain-flattened gamma-nought
# product. Needs a product that carries the scale factors; it says so if not.
umbra convert scene_SICD.nitf scene_g0.tif --dem auto --rtc --rtc-model facet --calibrate gamma0

# A pixel is the ground's echo *plus* the receiver's own thermal noise, and over
# a dark surface (calm water, radar shadow, dry sand) the second term is most of
# it -- so a calibrated value there reports the sensor's sensitivity, not the
# scene. --subtract-noise takes the noise floor off first, in the power domain
# where noise adds. By default that floor is the product's own stated one, which
# needs an ABSOLUTE NoiseLevel; it says so when the product carries none.
umbra convert scene_SICD.nitf scene_g0.tif --dem auto --rtc --rtc-model facet \
    --calibrate gamma0 --subtract-noise

# Umbra's open products generally carry no noise metadata to read, so
# --noise-model estimated infers the floor from the scene instead: a SAR image's
# darkest surfaces return essentially nothing, so the low tail of its own power
# distribution *is* the receiver. It needs no metadata; in exchange it is one
# constant rather than a polynomial across the swath, and it assumes the scene
# has dark ground to read. It records itself as an inference (UMBRA_NOISE_-
# SUBTRACTION reads "estimated", with the level in UMBRA_NOISE_FLOOR_DB), and
# `umbra stack` refuses to difference a series that mixes the two floors.
umbra convert scene_SICD.nitf scene_denoised.tif --subtract-noise --noise-model estimated

# "One constant rather than a polynomial across the swath" is not a rounding
# error: a receiver's sensitivity varies with range, so a scalar under-subtracts
# at one edge of the swath and over-subtracts at the other, leaving exactly the
# gradient the correction exists to remove. --noise-model estimated-range takes
# the same low-tail read *per range line* (SICD stores range along the image
# rows) and fits those floors against range, so an inferred floor follows the
# swath the way the measured one does -- still with no metadata. The fit is what
# makes it work on a real scene: it interpolates across the lines that had no
# dark ground to read, and drops the lines whose tail sits far above it, since
# bright ground can only push a line's low tail up. It is recorded as its own
# third thing ("estimated-range"), and it reports the swing it found in
# UMBRA_NOISE_FLOOR_SPREAD_DB -- near zero means the constant floor was missing
# nothing.
umbra convert scene_SICD.nitf scene_denoised.tif --subtract-noise \
    --noise-model estimated-range

# Every floor also says what it did to *this* scene, rather than leaving its
# limits as documentation: how much of the image it drove to the sensor's
# sensitivity limit (UMBRA_NOISE_FLOORED_FRACTION), and -- for an inferred one --
# how far the scene's median power sat above the floor it inferred
# (UMBRA_NOISE_FLOOR_MARGIN_DB). A wide margin is the evidence that the dark
# tail really was a different population from the backscatter; a narrow one says
# this scene was bright everywhere and the estimate took real signal off, so the
# command says so and points at --noise-model measured. Advisory, not a refusal:
# a uniform scene is legitimate.
#   12.4% of the image is at the sensor's limit after the subtraction
#   Estimated floor of -31.2 dB sits 17.8 dB below the scene median

# What is left after all of that is speckle, and it is not an error: a coherently
# illuminated rough surface interferes with itself, so one look's power scatters
# about the surface's true backscatter with a standard deviation equal to its mean
# -- which is why a pixel-by-pixel difference between two passes is mostly speckle
# rather than change. Nothing subtracts it; averaging is the only correction.
# --speckle-filter boxcar averages the window unconditionally (the multilook);
# --speckle-filter lee averages only where the window is no more variable than
# speckle alone explains, so edges and points survive. It is opt-in because what
# it spends is resolution: a window that averages N pixels reports ground N pixels
# across. The raster records the filter, the window, and the equivalent looks it
# actually reached -- which on a product sampled finer than it resolves is below
# the pixels averaged, and is the honest measure of what the filter bought.
#   Equivalent looks 1.0 -> 12.7, of 25 pixels averaged
umbra convert scene_SICD.nitf scene_filtered.tif --calibrate gamma0 \
    --speckle-filter lee --speckle-window 5
# This filters in the radar's own image space, so it only reaches the complex
# archive. For the published GEC rasters the same averaging is a datacube
# option -- `umbra stack --speckle-filter`, above.

# Convert only the area you care about. A scene is tens of square kilometres at
# 16-25 cm, and every step above is proportional to it. --clip-bbox turns the
# ground rectangle back into the image window that covers it, reads only that,
# and crops the output to the request -- same pixels, a fraction of the work.
umbra convert scene_SICD.nitf site.tif --clip-bbox -112.05,40.72,-111.98,40.78

# Every converted raster records how it was made -- calibration, noise-floor
# subtraction, terrain model and reference angle, DEM/geoid, projection, scale,
# licence -- in its own
# GeoTIFF tags, so a scene can say what its pixel values mean. Read it back
# (also visible to plain gdalinfo, or umbra_py.read_conversion_tags).
umbra convert scene_g0.tif --provenance

# Visual similarity: embed a site's quicklooks, then find scenes that look alike.
umbra embed build --area "Centerfield" --start 2024-01-01 --end 2024-12-31
umbra embed similar <item-json-url>
```

### Ask in plain language (`umbra ask`)

The deterministic resolvers above (relative dates, fuzzy site names) turn some
natural language into a filter with no model at all. `umbra ask` covers the
rest — *"what did Umbra image at Centerfield, Utah last spring?"* — by letting a
model **plan** the search while the library still **executes** it deterministically:

```bash
pip install "umbra-py[ai]"
export ANTHROPIC_API_KEY=...        # or OPENROUTER_API_KEY, or OPENAI_API_KEY (+ optional OPENAI_BASE_URL)
umbra ask "what did Umbra image at Centerfield, Utah last spring?"
```

```text
Plan: named site over the northern-hemisphere spring window
umbra search --area 'Centerfield, Utah' --fuzzy --start 2024-03-01 --end 2024-05-31 --product GEC

Re-run with --run to execute this search.
```

The model only ever returns the *search parameters* it thinks your sentence maps
to; the library then re-validates every one of them — dates through the same
deterministic resolver, product types against the known set, the bounding box
range-checked — and prints the exact `umbra search` command before it runs.
**Nothing the model says becomes a filter without passing that check**, so a
hallucinated date or product type is a clear error, not a silently wrong query.
The LLM plans, the library executes, and you audit the command. Add `--run` to
execute it (against a live walk, or a prebuilt index with `--local`), `--json`
to get the resolved plan as JSON, or `--model` / `UMBRA_ASK_MODEL` to choose the
model. It's the one place a model is called — opt-in behind `[ai]`, never
implicit — so seasons and other phrasing the deterministic core rejects
(`"last winter"`) get resolved to concrete dates the deterministic layer checks.

**Areas of interest are chosen, never drawn.** A question can also mean a shape
— *"scenes over this watershed"* — so pass the areas you already have and the
model may select one **by name**:

```bash
umbra ask "what changed over the delta this spring?" --aoi delta.geojson
```

```text
Plan: the supplied delta outline over the spring window
umbra search --intersects delta.geojson --start 2024-03-01 --end 2024-05-31
```

Repeat `--aoi` for several (each takes its file stem as its name, or spell it
`NAME=PATH`). Every file is parsed by the deterministic layer *before* the model
sees anything; the prompt then lists the areas by name and bounds, and the plan
may only name one of them. There is deliberately no way for a model to write
coordinates: a hallucinated date is caught by the date resolver, but a
hallucinated ring is a plausible polygon over the wrong ground that nothing
downstream could catch. A name that isn't on the list — or any name when you
supplied no areas — is an error, not a silently unfiltered search. The polygon
that reaches the search is always your file, and the audited command points back
at it.

### Find a site you can describe but can't name (`umbra semantic`)

`--fuzzy` matches by the *words* in a task label. Some queries share no word
with the label they mean — Umbra's grain-storage site in North Dakota is
literally named *"Beet Piler - ND"* — and only a model that has read about the
world can bridge that. `umbra semantic` embeds the task names once so a query can
be ranked by **meaning**, not spelling:

```bash
pip install "umbra-py[ai]"
export OPENAI_API_KEY=...            # or OPENAI_BASE_URL for any compatible endpoint
umbra index fetch                    # (or build) so there are task names to embed
umbra semantic build                 # embed the index's task names once
umbra semantic search "grain storage north dakota"
```

```text
  0.612  Beet Piler - ND
  0.088  Grand Forks Airfield

Best match: Beet Piler - ND
umbra search --area 'Beet Piler - ND'

Re-run with --run to search the best match.
```

The embedding step is the *only* part that calls a model, and it runs once at
build time; the query embeds a single sentence and everything else — storing the
vectors (a small SQLite file beside `catalog.db`), the cosine ranking, the
threshold — is deterministic and offline. As with `umbra ask`, it prints the
exact `umbra search --area …` command for the top match before running anything,
so you audit it; add `--run` to execute it, `--json` for machine output, or
`--top-k` / `--min-score` to tune the ranking. It stays behind the `[ai]` extra
and never runs implicitly — the deterministic `--area` / `--fuzzy` matchers
remain the default search path; this is the optional layer on top of them.

### Read a scene in plain language (`umbra describe`)

Searching gets you the scene; *reading* SAR is a different skill (why is water
dark? is that black patch shadow or an empty field?). `umbra describe` renders an
item's quicklook, sends that picture plus the metadata context card to a
configured vision model, and returns a structured, plain-language reading — with
the SAR literacy baked into the packaged prompt so the model reads radar
correctly, not as an optical photo.

```bash
pip install "umbra-py[ai,viz]"       # the model call + the quicklook render
export ANTHROPIC_API_KEY=...          # or OPENROUTER_API_KEY, or OPENAI_API_KEY (+ optional OPENAI_BASE_URL)
umbra describe https://.../<item>/<id>.json
```

```text
A bright industrial complex sits amid darker, smooth agricultural fields, with a
linear road network cutting across the northeast. The strong returns concentrate
in a rectangular cluster of structures near the center.

Observed features:
  - bright rectangular structures near the center
  - dark smooth fields to the south and west

Caveats:
  - the dark fields could be low-backscatter crops or bare soil, not water

Confidence: medium

AI-generated interpretation of SAR imagery. Descriptions are a model's reading of
the scene, not verified measurements, and may be wrong; verify against the source
data before relying on them.
Contains Umbra open data, licensed under CC BY 4.0.
```

The model **only interprets** — the picture and the metadata are produced
deterministically, and nothing the model says becomes a filter, a URL, or a
coordinate. Every description is stamped with the CC-BY attribution *and* an
explicit AI-provenance note, so a model's reading of radar is never mistaken for
ground truth. Add `--json` for a `{summary, observed_features[], confidence,
caveats[]}` object, `--asset` / `--no-db` / `--max-size` to control the render,
or `--model` to pick the model. Like `umbra ask`, it stays behind the `[ai]`
extra and never runs implicitly.

**Describe from the preview you already have.** By default each reading streams a
fresh cloud-optimized overview from S3. If the local index carries baked previews
(`umbra index fetch-thumbnails`, or your own `umbra index bake-thumbnails`),
`--preview` reads one of those instead — no range read, and no `viz` extra at all:

```bash
umbra index fetch && umbra index fetch-thumbnails   # metadata + the pictures
pip install "umbra-py[ai]"                          # no rasterio needed
umbra describe https://.../<item>/<id>.json --preview baked
```

`baked` fails if the scene has no cached preview (saying which command bakes or
fetches it); `auto` renders the ones that are missing. A cached preview is a
128–256 px quicklook, so it is smaller than `--max-size` asks for: the reading
records which picture it read (`"image"` in `--json`) and carries a caveat about
the detail that was not in it. And it is only substituted for a picture of the
*same* product — the index records which asset each preview was baked from, so
`umbra index bake-thumbnails --asset CSI` answers `umbra describe --asset CSI`,
while a request the cache holds a different picture for is refused (naming what
that picture is) rather than quietly answered with it.

### Monitor a site for new passes (`umbra watch`)

SAR re-images a site pass after pass, so the natural way to monitor one is to run
the same search on a schedule and act only on what is *new*. `umbra watch` is
that primitive: it searches, compares against what previous runs already
reported (state kept in the local index), prints only the new acquisitions, and
remembers them — so it's idempotent, and a run with no newly published data is a
clean no-op.

```bash
# First run establishes the baseline; later runs report only what's new.
umbra watch --area "Centerfield, Utah"

# In a cron job / GitHub Action: exit 10 when there's something new, else 0.
umbra watch --area "Centerfield, Utah" --exit-code --json
```

```text
1 new acquisition(s) since last run for watch 'centerfield-utah-3f9a1c2e':

2024-03-01-00-00-00_UMBRA-04
  acquired : 2024-03-01T00:00:00+00:00
  product  : GEC  pol=VV  res~0.50 m
  url      : https://.../2024-03-01-.../...stac.v2.json

Tracking 12 acquisition(s) total.
```

The scheduler (cron, a GitHub Action, an agent loop) supplies the *when*; the
library supplies the idempotent *what changed* — **no model is called**, it's an
exact set difference over the deterministic search. `--json` emits a machine
readable `{new_count, new_items: [...], ...}` delta (carrying the CC-BY
attribution) whose items are ready to hand to `umbra describe` or `umbra change
--narrate` for a standing analyst. `--name` sets a stable watch identity (auto
derived from the query otherwise), `--state-db` chooses where state lives,
`--reset` re-establishes the baseline, and `--local` diffs a prebuilt index
snapshot instead of walking S3 live.

### Prepare an ML training set (`umbra chips`)

Building a model on SAR? `umbra chips` walks a search and cuts each scene's
geocoded GeoTIFF into fixed-size, georeferenced tiles with a manifest that
carries the metadata a training pipeline needs — chip path, geographic bbox,
CRS, transform, datetime, place, polarization, incidence angle, resolution, and
the CC-BY license — one record per chip.

```bash
# Chip a site's passes into 512-px GeoTIFF tiles + a JSONL manifest.
umbra chips --area "Centerfield, Utah" --start 2024-01-01 --end 2024-12-31 \
    --out chips/ --chip-size 512 --db

# NumPy arrays with overlapping tiles, dropping mostly-empty footprint corners;
# emit a GeoJSON manifest you can drop straight into QGIS.
umbra chips --area "Centerfield" --out chips/ --format npy \
    --chip-size 256 --stride 128 --min-valid 0.5 --manifest chips.geojson

# For a large chip set, write a stac-geoparquet manifest instead — one
# column-oriented file DuckDB / geopandas can query without loading every line
# (needs the export extra: pip install "umbra-py[load,export]").
umbra chips --area "Centerfield" --out chips/ --manifest chips.parquet

# Or chip specific items directly, and print the dataset summary as JSON.
umbra chips <item-json-url> --out chips/ --json

# Chip the *complex* archive: each SICD is geocoded before its tiles are cut, so
# a training set can come from the full-resolution product — terrain-corrected
# and radiometrically calibrated, not relative brightness (needs the convert
# extra: pip install "umbra-py[load,convert]").
umbra chips --area "Centerfield" --out chips/ --asset SICD \
    --dem auto --rtc --rtc-model facet --calibrate gamma0 --work-dir scenes/

# Average the speckle down, so a chip teaches a model the surface rather than
# the interference pattern on it. Works on any asset: on the published GEC the
# tiles themselves are averaged, on --asset SICD the scene is, before geocoding.
# What it spends is resolution, so every record says which filter and window it
# was cut with and the equivalent looks either side of it.
umbra chips --area "Centerfield" --out chips/ --speckle-filter lee

umbra chips --area "Centerfield" --out chips/ --asset SICD \
    --calibrate gamma0 --speckle-filter lee --work-dir scenes/

# Carry on past a scene whose own metadata cannot support the request (no
# Radiometric block for --calibrate, no stated floor for --noise-model
# measured), and say which ones were left out, instead of losing the batch to
# the first one.
umbra chips --area "Centerfield" --out chips/ --asset SICD \
    --calibrate gamma0 --skip-unsupported --work-dir scenes/

# Better still: ask each product's metadata over the wire *first* (two range
# requests, tens of kilobytes) and drop the passes that cannot answer before
# downloading any of them. The dataset reports the same hole -- and what not
# downloading them saved.
umbra chips --area "Centerfield" --out chips/ --asset SICD \
    --calibrate gamma0 --preflight --work-dir scenes/

# Chip one site out of every pass rather than every scene whole. --clip-bbox
# tiles only that window -- and for --asset SICD it is the *conversion's* clip
# too, so the geocoding step costs what the site costs, not what the scene does.
umbra chips --area "Centerfield" --out chips/ --asset SICD \
    --clip-bbox -112.05,40.72,-111.98,40.78 --work-dir scenes/
```

**The complex products are chippable too.** A `SICD` is complex slant-plane data
with no map grid, so `--asset SICD` fetches each scene and geocodes it through
the same pipeline `umbra convert` uses, then cuts the identical tiles from the
result — which is how a training set reaches the half of the archive that is the
point of 16–25 cm SAR. Because the conversion is the one `umbra convert` runs,
`--dem` / `--geoid` / `--rtc` / `--rtc-model` / `--calibrate` /
`--subtract-noise` / `--noise-model` all apply: chips can
carry a terrain-flattened, calibrated
**gamma-nought** backscatter coefficient with the receiver's own noise floor
removed and its speckle averaged down, rather than relative brightness, and each
chip GeoTIFF inherits the
`UMBRA_*` provenance tags saying so (the manifest also carries `calibration` /
`noise_subtraction` / `speckle_filter` / `speckle_window` / `rtc_model`, read back
from the raster rather than from the request — the speckle pair being what says a
chip's *resolution* as opposed to its pixel size, and so what a model trained on
it can learn to see). A run that *inferred* the noise floor also says which of its scenes it
should not have been trusted on: each record carries that scene's
`noise_floor_margin_db` and `noise_floored_fraction`, and the run prints (and
`--json` reports) one roll-up — *"2 of 22 scene(s) had under 6 dB of margin"* —
because a scene that was bright everywhere had no dark ground for the fifth
percentile to read, so what came off it was real backscatter. It stays an
advisory: filter the manifest on the margin, or use `--noise-model measured`
where the products state their own floor. The cost is honest:
unlike the GEC path this downloads each product whole, so it is opt-in, one scene
is on disk at a time, and `--work-dir` keeps the geocoded scenes so a re-run
reuses them instead of fetching and warping again.

**A mixed archive no longer costs the batch.** Radiometric calibration needs the
SICD's own `Radiometric` scale factors and `--noise-model measured` needs its
stated noise floor — and Umbra's open products generally carry neither, so a run
over twenty scenes could end on the twenty-first and take the twenty with it.
The refusal is right (a scaling by an invented number is indistinguishable in
the output from a measured one); what it lacked was a name. It has one now —
`UnsupportedMeasurementError` — which is what lets `--skip-unsupported` carry on
past exactly that family, record each left-out pass on `ChipDataset.skipped`
(which acquisition, and in the product's own words why) and print it at the end,
so the dataset *states* its hole rather than having one. Nothing else is
swallowed: a download failure or a corrupt product still ends the run. And the
check now happens off the metadata *before* the pixels are read, so a scene that
could never have answered costs its header rather than a full complex read.

**`--preflight` moves that check ahead of the download.** `--skip-unsupported`
makes a refusal survivable, but it is still discovered by fetching the product:
a batch learns which of twenty passes can be calibrated by downloading twenty
passes. With `--preflight` each acquisition's SICD XML is read over the wire
first (the same two range requests `umbra preflight` uses, tens of kilobytes of
a multi-gigabyte product), the conversion's own support check is run against it,
and the passes that cannot answer never reach the download at all:

```text
Preflighting 22 product header(s) ...
  Preflight read 452.6 KB of product headers from 22 acquisition(s) and dropped 9,
    saving 31.4 GB of download.
  Skipped 9 acquisition(s) whose metadata cannot support the request:
    2024-02-08-01-02-03_UMBRA-05 [preflight]: SICD carries no Radiometric block.
```

The question it asks is the conversion's own — the settings come from the same
request, so a pass it clears cannot then be refused for a reason it could have
seen — and a dropped pass lands in the same `ChipDataset.skipped` block a
survived refusal does (with `stage="preflight"`), because a dataset with a hole
in it has to say so however cheaply the hole was found.

An acquisition whose metadata cannot be *read* divides in two, and which half it
falls in decides what the run does with it. A read that fails on the **wire** — a
timeout, a dropped connection — says nothing about the product, so the pass is
**kept** and the run finds out the expensive way rather than losing a scene to a
blip. A read that fails on the **product** — the item lists no such asset,
nothing is at the href, what is there is not a NITF or carries no SICD XML — is
as final as any refusal, and is dropped like one (`UnreadableProductError`,
recorded on `ChipDataset.skipped` with the reader's own words). Keeping those was
never the cautious half of the choice it resembled: such a pass fails inside the
chipper as a plain read error, which `--skip-unsupported` deliberately does not
catch, so a run that preflighted still ended on an acquisition its own preflight
had already ruled out. `PreflightSummary` counts the two apart (`missing` against
`unreadable`) and both `umbra preflight` and `umbra chips` name which is which,
because only one of them is worth asking about again.

**And the dataset says so, not just the run.** Both routes to a hole reported it
to whoever was watching the console — `ChipDataset.skipped`, the `--json`
payload, the lines above — and a training loader reading `out_dir` months later
sees none of that. It sees files. So a run that could not include every
acquisition it was offered writes a `skipped.jsonl` sidecar beside the manifest:
one line per left-out pass, carrying the same `item_id` / `datetime` / `reason` /
`hint` / `stage` the summary does. It is a sidecar rather than manifest rows
because the manifest's schema is one row per *chip* and a skipped acquisition has
none, and it is written **only when there is something to record**, so a dataset
with no hole in it is exactly the set of files it was before — and the file's
presence is itself the statement. Without it, a dataset that dropped nine of
twenty-two passes is indistinguishable on disk from one that was only ever
offered thirteen.

Pass both flags: the preflight asks only the three questions the metadata answers
(`--calibrate`, `--noise-model measured`, `--rtc`'s geometry), so anything else —
`--rtc`'s DEM among them — still refuses at conversion time. The headers are read several at a
time (`--preflight-workers`, default 8), so the check that runs in front of the
batch does not become a stall that grows with the number of passes.

`--clip-bbox` is what makes
that path affordable when the subject is a *site*: it tiles only the window you
name, and on the SICD path it becomes the conversion's own clip, so each pass is
geocoded over the area of interest rather than over the whole collect (the same
flag, and the same lon/lat convention, as `umbra stack --clip-bbox`).

**Speckle is filtered on either path.** `--speckle-filter {boxcar,lee}` is not
SICD-only: on the published amplitude rasters — the products most chip sets are
actually built from — the *tiles* are averaged, which is the first point at
which those pixels exist in this library at all. Each tile is read with a
half-window halo and cropped after filtering, so every chip pixel averages the
neighbours a whole-scene filter would have given it and two overlapping tiles
agree about the ground they share; and `lee`'s speckle parameter is read once
per acquisition from a fixed sample of its windows, not per tile, because it is
a property of the product's processing rather than of the 512 pixels a tile
happens to cover. Every record carries `speckle_filter` / `speckle_window` plus
`speckle_enl_before` / `speckle_enl_after` / `speckle_looks` — the equivalent
number of looks either side of the window, which is what says whether the
resolution it spent bought anything — and the run prints (and `--json` reports)
one roll-up: *"equivalent looks up by 4.2x on the median scene"*, with a count
of the scenes the window bought little on. The chips carry `umbra convert`'s own
`UMBRA_SPECKLE_*` tags, so `to_stack`'s refusal to difference a filtered pass
against an unfiltered one works on them unchanged.

For the amplitude products (`GEC`, `CSI`) only the bytes for each tile stream
over HTTP range requests — no full download,
and memory stays bounded to one chip. Fixed-size is a promise (partial edge tiles
are dropped), so every chip has the exact shape a loader expects; `--stride`
overlaps tiles for dense inference / augmentation, and `--min-valid` drops the
mostly-nodata corners of a rotated footprint. The manifest format follows the
`--manifest` extension — `.jsonl` (the ML default), `.geojson` (QGIS / geopandas),
or `.parquet` (stac-geoparquet, queryable at scale via DuckDB / geopandas; needs
the `export` extra). **No model is called** — chipping is pure raster iteration +
manifest logic. Requires the load extra (`pip install "umbra-py[load]"`).

### Ask before you download (`umbra preflight`)

Three of the conversion's corrections depend on the product describing itself:
`--calibrate` reads the SICD's `Radiometric` scale-factor polynomials,
`--noise-model measured` reads its stated noise floor, and `--rtc` reads the
collection geometry it tilts by the terrain's slope. Umbra's open products
generally carry neither of the first two, so those runs refuse — correctly,
because a scaling by an invented number is indistinguishable in the output from a
measured one. Most products *do* state their geometry, which is what makes the
ones that don't worth asking about: a `--rtc` run refuses on them only after the
download, the DEM fetch and the warp.

The problem was never the refusal; it was *finding out*. A SICD's metadata lives
inside the NITF, so learning that a pass cannot be calibrated meant downloading
it. Over a site's twenty passes that is tens of gigabytes spent to be told no.

`umbra preflight` asks over the wire instead. A NITF states its own layout in a
fixed-width header, so the SICD XML — a data extension segment near the end of
the file — is located and fetched with two HTTP range requests:

```bash
# Which of this site's complex passes could carry a gamma-nought coefficient?
umbra preflight --area "Centerfield" --start 2024-01-01 --end 2024-12-31 \
  --calibrate gamma0

# Or ask about a measured noise floor, on one acquisition or a whole selection.
umbra preflight <item-json-url> --subtract-noise --noise-model measured --json

# Or whether a pass states the geometry terrain flattening needs.
umbra preflight --area "Centerfield" --rtc
```

```text
  2024-02-08-01-02-03_UMBRA-05: calibrations none; noise level none; look geometry 32.5 deg -> no
  2024-03-11-04-05-06_UMBRA-05: calibrations none; noise level none; look geometry 28.1 deg -> no
0 of 2 acquisition(s) support --calibrate gamma0.
  Read 41.2 KB of product headers instead of 7.4 GB of product.
  hint: Convert without --calibrate; this product states no scale factors.
```

The verdict is not a second opinion: the parsed metadata is handed to the same
`_check_measurement_support` the conversion runs, calling the same coefficient
readers, so a preflight that says yes and a conversion that then refuses cannot
disagree. What differs is only where the metadata came from. `sicd_capabilities`
also reports what the product *does* declare — which calibrations, which noise
level, its scene-centre look geometry, and the scene's identity — so `umbra chips
--asset SICD --calibrate
gamma0` over the survivors is a run with no refusals in it. That last step is
wired in rather than left to you: `umbra chips --preflight` runs this check over
the selection itself and drops the passes that cannot answer before downloading
any of them (see above).

Reading it needs no extra at all (the NITF walk and the XML parse are stdlib), so
"can this archive answer my question?" is answerable from a core install. An
acquisition whose metadata cannot be read — a missing asset, an HTTP failure — is
recorded as its own verdict rather than ending the walk, because a preflight that
dies on the nineteenth scene has failed at the one thing it is for.

Once the answer costs kilobytes, the only thing left that grows with the number
of passes is the round trip — so the selection is read several products at a time
(`--workers`, default 8; `umbra chips --preflight-workers`). Nothing about the
answer changes: each read is independent, the verdicts come back in the order they
were asked in, and the progress lines print one per pass in that order. What
changes is that the check in front of a forty-pass batch stops being the batch's
slowest part.

### Find scenes that *look alike* (`umbra embed`)

Every other search matches metadata — a date, a bbox, a task name. `umbra embed`
matches *appearance*: it embeds each acquisition's rendered quicklook into a
vector once, then ranks scenes by cosine similarity. *"Find scenes that look like
this flooded field"* — a search over pixels, not metadata, and a capability
nothing in the Umbra ecosystem offers.

```bash
# Fetch the prebuilt scene-embedding table — no rebuild (when one is published).
umbra embed fetch                    # pulls catalog.embed.db from the release

# ...or embed a site's quicklooks yourself into a scene-similarity index.
umbra embed build --area "Centerfield, Utah" --start 2024-01-01 --end 2024-12-31

# Image-to-image: archived scenes that look most like a given acquisition.
umbra embed similar https://.../<item>/<id>.stac.v2.json

# Text-to-scene (needs a joint CLIP-family model): describe what you're after.
umbra embed search "a flooded agricultural field" --json

umbra embed info                     # scene-vector count, model and dimension
```

The vectors live in a sidecar `catalog.embed.db` beside the local index, keyed by
item id (a rebuild only embeds what is new). Embedding every quicklook is the one
expensive, model-backed step, so `umbra embed fetch` pulls a published
`catalog.embed.db` from the rolling `catalog-index` release straight to that
sibling path — visual similarity search with no rebuild, the embedding sibling of
`umbra index fetch` / `umbra tiles --fetch` (only the *query* still needs a key).
The fetched vectors are model-specific, so query with the model `umbra embed info`
reports. Only turning an image or a text query
into a vector calls a model — an OpenAI-compatible multimodal `/embeddings`
endpoint (set `OPENAI_API_KEY`, optionally `OPENAI_BASE_URL` /
`UMBRA_SCENE_EMBED_MODEL`); rendering, storage and cosine ranking are
deterministic and offline. Every match is a pointer back to a real acquisition
(id, task, datetime, STAC href), never a model-authored fact. Requires the ai and
viz extras (`pip install "umbra-py[ai,viz]"`).

### Drive it from an AI agent (MCP)

Umbra publishes no STAC API, so this library *is* the query layer — and
`umbra-mcp` exposes that layer over the [Model Context
Protocol](https://modelcontextprotocol.io/), turning any MCP client (Claude
Desktop / Code and others) into a natural-language front door to the archive.
*"Show me what changed at Centerfield, Utah this spring"* becomes a first-run
experience instead of a tutorial chapter.

```bash
pip install "umbra-py[mcp]"
umbra mcp            # run the stdio server (also: umbra-mcp)

# …or run it with nothing installed at all:
uvx --from 'umbra-py[mcp]' umbra-mcp
```

The `--from` is not optional decoration. The `umbra-mcp` console script lives in
the **`umbra-py`** distribution and needs its `[mcp]` extra, so handing the
script name to `uvx` on its own looks for a distribution called `umbra-mcp` —
there is none — and would not install the extra either. `--from` names the
distribution *and* the extra; the word after it is the script to run.

Register it with an MCP client (Claude Desktop shown). The first form assumes
`pip install "umbra-py[mcp]"`; the second installs nothing:

```json
{
  "mcpServers": {
    "umbra": { "command": "umbra-mcp" }
  }
}
```

```json
{
  "mcpServers": {
    "umbra": {
      "command": "uvx",
      "args": ["--from", "umbra-py[mcp]", "umbra-mcp"]
    }
  }
}
```

That same command is published to the [MCP
registry](https://registry.modelcontextprotocol.io/) as
`io.github.reesehammer/umbra-mcp` — see [`server.json`](server.json), which the
release workflow submits, and which `tests/test_mcp_registry.py` keeps in step
with the packaging facts it describes.

<!-- mcp-name: io.github.reesehammer/umbra-mcp -->

The server offers `search_catalog`, `find_repeat_sites` (rank the archive's most
repeat-imaged sites when you don't yet know *which* site to analyse — the
discovery step before the change verbs, returning each site's passes oldest-first
ready for `pick_change_interval`), `get_item`, `geocode_place`, `index_stats`,
`quicklook`, `change_composite`, `timescan`, `stack_stats` (the same change
question in numbers: per-pass decibel statistics and how much ground moved, in
km² — and with `blocks=N`, which part of the site moved and between which two
passes), `stack_provenance` (whether those passes are one measurement at all,
and which of them are when they aren't), `pick_change_interval` (scan a whole
series and name the two passes whose change stands clearest of the speckle
floor — the scan half of scan → narrate, deterministic), `download_asset`,
`watch_site`
(report only passes new since the last check), `find_similar` /
`find_similar_text` (visual similarity search over a prebuilt scene-embedding
index), `describe_scene` (a SAR-literate model reading of one scene) and
`narrate_change` (a model reading of *what changed* between passes, grounded in
a per-block decibel grid) tools; a `umbra://context` resource with the
product-type table and search semantics; and packaged `monitor-site` /
`watch-site` / `quantify-change` / `find-similar-scenes` / `describe-scene` /
`narrate-change` / `survey-region` prompts. The imagery tools return
the rendered PNG as an MCP image block, so the model *sees* the radar scene. In
keeping with the library's design, the server stays deterministic — it
searches, geocodes and renders; the client's model plans and narrates. The two
opt-in exceptions are `describe_scene` and `narrate_change`, which consult a
vision model to *read* a scene or *narrate* change (only when an `[ai]` key is
configured), and even they hold the boundary: the model only interprets, its
reply is validated, and every reading is stamped as an AI interpretation
(`narrate_change` narrates only the change its deterministic dB grid supports).
The server refuses to composite mixed polarizations (HH and VV aren't
comparable), and the CC-BY attribution line travels with every result.

### Drive it from a LangChain / LangGraph agent

MCP reaches MCP-native clients; a large population of agent builders instead
assemble tools with LangChain. `umbra_py.langchain` offers the **same** catalog
tools as native LangChain `StructuredTool`s — the identical deterministic
callables the MCP server exposes, so the two front doors can't drift.

```bash
pip install "umbra-py[langchain]"
```

```python
from umbra_py.langchain import umbra_tools

tools = umbra_tools()                       # ready to bind to any LangChain agent
llm_with_tools = my_chat_model.bind_tools(tools)

# …or hand them straight to LangGraph's prebuilt ReAct agent:
from langgraph.prebuilt import create_react_agent
agent = create_react_agent(my_chat_model, umbra_tools())
```

`umbra_tools()` returns `search_catalog`, `find_repeat_sites`, `get_item`,
`geocode_place`,
`index_stats`, `stack_stats`, `stack_provenance`, `pick_change_interval`,
`download_asset`, `watch_site`, `find_similar` /
`find_similar_text`, `describe_scene`, `narrate_change` and the `quicklook` /
`change_composite` / `timescan` render tools — the full MCP inventory, each
schema inferred from the function signature and each description from its
docstring. *Images are the API*: the render tools use LangChain's
`content_and_artifact` response format, so the `ToolMessage` carries a text
caption and the raw PNG on `.artifact` for a downstream multimodal model to *see*
the radar scene. Pass `include_render=False` for a JSON-only surface (a text-only
model, or an install without the `viz` extra). The determinism boundary is
preserved: the tools search, geocode and render; the agent's model plans and
narrates, with `describe_scene` and `narrate_change` the two opt-in exceptions
(vision readings of a scene and of two-pass change, only when an `[ai]` key is
configured).

### Drive it from a LlamaIndex agent

A third large population of agent builders assembles tools with LlamaIndex.
`umbra_py.llamaindex` offers the **same** catalog tools as native LlamaIndex
`FunctionTool`s — the identical deterministic callables the MCP and LangChain
surfaces expose, so all three front doors can't drift.

```bash
pip install "umbra-py[llamaindex]"
```

```python
from umbra_py.llamaindex import umbra_tools

tools = umbra_tools()                       # ready for any LlamaIndex agent

from llama_index.core.agent import ReActAgent
agent = ReActAgent.from_tools(tools, llm=my_llm)
```

`umbra_tools()` returns the same inventory as the LangChain adapter
(`search_catalog`, `find_repeat_sites`, `get_item`, `geocode_place`,
`index_stats`, `stack_stats`,
`stack_provenance`, `pick_change_interval`, `download_asset`,
`watch_site`, `find_similar` / `find_similar_text`, `describe_scene`,
`narrate_change` and the `quicklook` / `change_composite` / `timescan` render
tools) — each name and description inferred from the function's docstring and each
argument schema from its signature. *Images are the API*: LlamaIndex has no
`content_and_artifact` split, so the render tools return a `RenderResult` whose
string form is the caption and whose `.png` (surfaced as the
`ToolOutput.raw_output`) carries the raw PNG for a downstream multimodal model to
*see* the radar scene. Pass `include_render=False` for a JSON-only surface. The
determinism boundary is preserved exactly as on the other surfaces:
`describe_scene` and `narrate_change` are the two opt-in model calls.

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
not build an index first. That OpenAPI document describes the artifact routes
too: the published contracts they emit
([`docs/schemas/`](docs/schemas/README.md) — `stack-stats`, `stack-provenance`,
`render-job`) are merged into it as components, so a generated client reads the
same shape `umbra stack --stats --json` and the agent tools hand back rather
than a bare object. The same contracts are readable from an install with
`umbra_py.load_schema("stack-stats")`.

Beyond the STAC core filters, `/search` also exposes the index's Umbra-specific
filters via the STAC **Query extension** — `product_types` (which product a
scene carries), free-text `area` (a task/site substring, with an optional
`fuzzy` toggle), and the SAR acquisition properties `sar:polarizations`,
`view:incidence_angle` (a `gte`/`lte` range) and `sar:resolution` (an `lte`
bound on both range and azimuth resolution, in metres). Pass them as GET params,
plain `POST` body fields, or a STAC `query` object:

```bash
# GET: VV GEC scenes over a named site, 20-40° incidence, at least 0.5 m
curl "http://127.0.0.1:8000/search?product_types=GEC&area=Beet+Piler&fuzzy=true\
&polarizations=VV&min_incidence=20&max_incidence=40&max_resolution=0.5"

# POST: the same, as a STAC Query extension body
curl -X POST http://127.0.0.1:8000/search \
  -H 'content-type: application/json' \
  -d '{"query": {"product_types": {"in": ["GEC"]}, "area": {"like": "Beet Piler"},
       "sar:polarizations": {"in": ["VV"]},
       "view:incidence_angle": {"gte": 20, "lte": 40},
       "sar:resolution": {"lte": 0.5}}}'
```

Before you can analyse a site you have to know *which* site, and `GET /sites`
answers that over HTTP — the discovery step in front of the analysis routes. It
ranks the archive's most repeat-imaged sites (where change detection has
something to measure), reusing the same STAC search for the pool and the same
ranking as `umbra sites` and the `find_repeat_sites` agent tool, and returns each
site's coverage — passes, date span, revisit cadence, footprint, products, and
the pass URLs **oldest-first**, ready to hand straight to `POST /artifacts/stats`
/ `change`. It takes the same filters `/search` does (`bbox` / `intersects` /
`datetime` / `product_types` / `area` / `fuzzy` / SAR properties), with `limit`
sizing the pool, `top` capping the answer and `min_passes` the qualifying depth,
and each record follows the committed
[`site-coverage`](docs/schemas/README.md) contract:

```bash
# The most repeat-imaged sites in a bbox, then measure the top one's passes
curl "http://127.0.0.1:8000/sites?bbox=-112.1,39.0,-111.9,39.2&top=5"
```

There is a `POST /sites` twin as well, mirroring the `GET`/`POST /search` pair:
same ranking and records, but the body carries `intersects` as a GeoJSON object
(and the SAR filters as top-level fields or a STAC `query`), which is the
ergonomic form for a real area-of-interest polygon:

```bash
curl -X POST http://127.0.0.1:8000/sites \
  -H 'content-type: application/json' \
  -d '{"intersects": {"type": "Polygon", "coordinates": [[[-112.1,39.0],[-112.1,39.2],[-111.9,39.2],[-111.9,39.0],[-112.1,39.0]]]}, "top": 5}'
```

Beyond discovery, `umbra serve` also **renders the visual products on demand**,
so a front end (or an agent) can trigger them over any site straight from HTTP:

```bash
# One acquisition's SAR quicklook:
curl -o scene.png "http://127.0.0.1:8000/artifacts/quicklook/<item-id>.png?db=true"

# A change composite / timescan over a query (by ids, or bbox + datetime):
curl -o change.png -X POST http://127.0.0.1:8000/artifacts/change \
  -H 'content-type: application/json' \
  -d '{"bbox": [-112.1, 39.0, -111.9, 39.2], "datetime": "2024-01-01/2024-03-01"}'

# An interactive before/after swipe map (HTML) over the same kind of query:
curl -o swipe.html -X POST http://127.0.0.1:8000/artifacts/swipe \
  -H 'content-type: application/json' \
  -d '{"ids": ["<before-id>", "<after-id>"]}'
```

Each artifact wraps the same `umbra_py.viz` function the CLI uses and is cached
to disk by its inputs, so a repeat request is a file read (`swipe` returns HTML,
the others PNG). The server sends a permissive read-only CORS policy, so a
browser page on another origin can call it. Use `umbra serve --no-artifacts` to
expose only the read-only STAC surface (e.g. for a public instance that wants to
bound COG-streaming egress).

**And the same change question answered in numbers.** Every other artifact is a
picture, which a person reads and a program can't. `POST /artifacts/stats` takes
the same request shape and returns the `umbra stack --stats` reduction as JSON —
per-pass decibel statistics, the signed change against the previous pass, how
much ground moved past `change_threshold_db` **in km²**, and with `"blocks": N`
which part of the site moved and between which two passes:

```bash
curl -X POST http://127.0.0.1:8000/artifacts/stats \
  -H 'content-type: application/json' \
  -d '{"bbox": [-112.1, 39.0, -111.9, 39.2], "datetime": "2024-01-01/2024-06-01",
       "blocks": 6}'
# -> {"count": 5, "units": "dB", "passes": [...], "net_change": {...},
#     "spatial": {"peak_block": {...}, "blocks": [...], "grid_text": "..."}}
```

The grid defaults to the site's UTM zone (`"crs"`) and the decibel scale, so
areas are equal-area measurements rather than counts of geographic cells — pass
`"crs": null` for a lon/lat grid and the areas come back `null` rather than
wrong. `"clip_bbox"` narrows the measurement to a sub-area inside the scenes
(distinct from `"bbox"`, which chooses *which* acquisitions are measured). Unlike
the picture endpoints it refuses to mix polarizations: an HH-vs-VV difference
would land on the time axis and read as change. It needs the `load` extra on the
*server* (`pip install "umbra-py[serve,load]"`), so a client measures a site with
nothing installed locally.

It is also the only endpoint whose cost grows with the *number* of acquisitions
rather than with one render — so it gets the same ceiling-lift `umbra stack
--lazy` has, as an **instance-wide** setting rather than a request field:

```bash
# Measure a long series a slice at a time instead of holding every pass.
umbra serve --stack-lazy                       # one dask task per pass
umbra serve --stack-lazy --stack-chunk-size 1024   # …and windows within a pass
```

Operator-configured because it needs the `dask` extra on the *server*
(`pip install "umbra-py[serve,load,dask]"`; without it a stats request answers
`501` naming the extra) and a decision about the threads one request may spend —
`--stack-scheduler synchronous` (the default) runs the chunks on the request's
own worker, `threads` gives one render dask's thread pool. Because a lazy cube's
numbers are identical to an eager one's — only the peak memory differs — the
policy is deliberately *not* part of the artifact cache key: flipping it on an
instance neither invalidates a cached artifact nor moves a figure a client
already fetched.

The *measurement* half of that ceiling is the client's call, for the opposite
reason. On an instance started with `--stack-chunk-size`, a request may add
`"windowed": true` to be reduced window by window (`umbra stack
--stats-windowed`) instead of a slice per pass, so nothing ever holds a whole
pass:

```bash
curl -X POST http://127.0.0.1:8000/artifacts/stats \
  -H 'content-type: application/json' \
  -d '{"ids": [...], "blocks": 6, "windowed": true}'
# -> {..., "quantile_method": "histogram", "quantile_bin_db": 0.05, ...}
```

It is a request field rather than a policy because it is the one stacking choice
that **moves a number**: every count, mean, standard deviation and change figure
stays exact, while each pass's `median` / `p5` / `p95` become histogram
estimates good to about a bin. So it rides in the cache key — asking for it is
asking for a different artifact — and the response says which numbers are which
(`quantile_method` / `quantile_bin_db` appear only when they are estimates). On
an instance without `--stack-chunk-size` there are no windows to walk, so it is
refused with a `400` naming the flag rather than answered with worse
percentiles for the same memory.

The other request field that moves a number is the one that decides whether a
hosted measurement is worth quoting at all. **Speckle** — the interference
pattern coherent illumination makes on rough ground, whose standard deviation
equals its mean on a single look — is the largest uncertainty in a per-cell
decibel delta, so an unfiltered measurement of a quiet site reports mostly
interference. `"speckle_filter"` averages it down on the shared grid before
anything is measured (`umbra stack --speckle-filter`):

```bash
curl -X POST http://127.0.0.1:8000/artifacts/stats \
  -H 'content-type: application/json' \
  -d '{"ids": [...], "blocks": 6, "speckle_filter": "lee", "speckle_window": 5}'
# -> {..., "caveats": ["Every pass was speckle-filtered (lee, 5x5 window), …"]}
```

`boxcar` is the multilook — most variance removed for a window, blind to the
edges it averages across — and `lee` averages only where a window is no more
variable than speckle alone explains, so edges and bright points survive. What
it spends is resolution: a cell reports ground `speckle_window` cells across,
which the response's `caveats` state rather than the client having to remember.
That is why it is a request field in the cache key rather than a server policy,
and why it is off by default. It used to be the exact complement of `windowed` —
filtering needed each pass whole, so it was a `400` on exactly the
`--stack-chunk-size` instance `windowed` requires, and the pair was
unsatisfiable everywhere. The cube reads a half-window halo per window now, so
**a chunked instance answers both**: the largest cube the server can build,
measured with the interference averaged out of it. The same options reach the
agent tools (`stack_stats(urls=[...], speckle_filter="lee")` on MCP / LangChain
/ LlamaIndex).

Which options an instance takes is still worth knowing before asking, so the
landing page says, and an unsupported one carries the reason it *would* be
refused with:

```bash
curl -s http://127.0.0.1:8000/ | jq '.links[] | select(.rel=="stats")."umbra:options"'
# {
#   "stacking": "lazy (1024px windows, synchronous scheduler)",
#   "windowed": {"supported": true},
#   "speckle_filter": {"supported": true}
# }
# ...and on an eager instance, where there are no windows to measure in:
#   "windowed": {"supported": false, "reason": "windowed measurement needs a
#      chunked instance: this server stacks eager (whole series in memory), …"}
```

The reason is the same string the endpoint raises, so the advertisement cannot
drift from the refusal it predicts — and `stacking` is the policy line the
server echoes at startup, so a client can tell the operator which flag to
change.

One refusal is about the *selection* rather than the instance, and it has its
own preflight. `/artifacts/stats` will not measure passes whose rasters were made
by different `umbra convert` settings, because differencing two conversions puts
their difference on the time axis. `POST /artifacts/provenance` asks that first,
with the same request body:

```bash
curl -X POST http://127.0.0.1:8000/artifacts/provenance \
  -H 'content-type: application/json' -d '{"ids": [...]}'
# {"asset": "GEC", "agrees": false,
#  "groups": [{"record": {"calibration": "gamma0", …}, "count": 2,
#              "item_ids": [...], "hrefs": ["https://….json", …]}, …],
#  "unreadable": [],
#  "refusal": "Refusing to stack rasters whose calibration disagrees (…)"}
```

A mix is a `200`, not a `400`: reporting the mix is what was asked for, and
`groups[0].hrefs` is the subset to send to `/artifacts/stats` instead. Only a
selection that couldn't be measured at all — fewer than two passes, or mixed
polarizations — is a `400`, because there is no stack to preflight. It renders
nothing and caches nothing: the cost is one COG header per pass, and a
re-converted source is exactly the case a content-addressed answer would get
wrong. The document is `umbra stack --provenance --json`'s, verbatim.

A long render (a large `max_size`, a many-frame timescan) needn't hold the
request: add `"async": true` to any composite (or `stats`) request body to get a `202 Accepted`
and a job id back immediately, then poll `GET /jobs/{id}` and fetch the finished
artifact from `GET /jobs/{id}/result` (the disk cache is the result store, so an
already-cached render comes straight back `succeeded`):

```bash
# Queue a change composite; the response is a job document, not the PNG.
curl -X POST http://127.0.0.1:8000/artifacts/change \
  -H 'content-type: application/json' \
  -d '{"bbox": [-112.1, 39.0, -111.9, 39.2], "datetime": "2024-01-01/2024-03-01", "async": true}'
# -> {"id": "…", "status": "queued", "links": [...]}

curl http://127.0.0.1:8000/jobs/<job-id>            # {"status": "succeeded", ...}
curl -o change.png http://127.0.0.1:8000/jobs/<job-id>/result
```

These endpoints are what `umbra demo --server-url <serve URL>` calls: the
generated explorer gains an "Analyze this view" panel whose Change / Timescan /
Swipe buttons render each product over the currently-filtered acquisitions on
demand, and whose **Quantify** button measures them instead — it POSTs the same
view to `/artifacts/stats` and reads out the answer: how many decibels the site
moved between its first and last pass, how much ground crossed the change
threshold (in km², since the endpoint stacks on the site's UTM grid), which
block moved most and between which two passes, and the north-up heat-grid of
signed change — plus two sparklines of the pass-to-pass sequence behind those
headlines, one for the site and one for the block that moved most, which is what
separates a steady drift from a single step. Without `--server-url` the page
stays a fully static single file.

### Self-host it with Docker

To stand the STAC API up without a local Python install, the repo ships a
`Dockerfile` and a `docker-compose.yml`. One command builds the image, fetches
the published catalog index on first boot (no S3 crawl), and serves it:

```bash
docker compose up            # http://localhost:8000  (OpenAPI docs at /docs)
```

or with plain Docker:

```bash
docker build -t umbra-py .
docker run -p 8000:8000 -v umbra-data:/data umbra-py
```

The container exposes a `GET /healthz` liveness/readiness probe (wired to a
Docker `HEALTHCHECK`), persists the fetched index and render cache to the
`/data` volume so restarts are instant, and runs as an unprivileged user. Tune
it with environment variables — `UMBRA_SERVE_LIVE=1` walks S3 per request with
no index, `UMBRA_FETCH_INDEX=0` skips the first-boot fetch, `UMBRA_INDEX_URL`
points at a fork/mirror snapshot, and `UMBRA_SERVE_ARGS="--no-artifacts"` bounds
COG-streaming egress for a public instance (or
`UMBRA_SERVE_ARGS="--stack-lazy"` to keep `/artifacts/stats` off the whole
series at once). Build with
`--build-arg UMBRA_EXTRAS=serve,viz` to also enable the on-demand
`/artifacts/...` render endpoints (add `load,dask` for the numeric one and its
lazy path). The image doubles as the CLI:
`docker run --rm umbra-py search --area "Beet Piler" --limit 5`. See
[`docs_src/deploy.md`](docs_src/deploy.md) for the full reference.

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

## Citing umbra-py

If `umbra-py` helps your research, please cite it. Machine-readable metadata
lives in [CITATION.cff](CITATION.cff), and GitHub renders it as a **"Cite this
repository"** button in the sidebar (with ready-to-copy APA and BibTeX). Please
also honor the CC BY 4.0 attribution for any Umbra data you use (see above).

## Community

- [Contributing guide](CONTRIBUTING.md) — setup, checks, and conventions.
- [Code of Conduct](CODE_OF_CONDUCT.md) — Contributor Covenant 2.1.
- [Security policy](SECURITY.md) — how to report a vulnerability privately.

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
