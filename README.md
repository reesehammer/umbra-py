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

### Command line

```bash
# Search by area, dates and product type.
umbra search --bbox -68.1,10.4,-67.9,10.6 --start 2024-01-01 --end 2024-01-31 --product GEC

# Inspect a single item by its STAC JSON URL.
umbra info <item-json-url>

# Download specific asset(s).
umbra download <item-json-url> --asset GEC --dest downloads/

# Render a standalone SAR quicklook image -- no map, no full download.
# Add --db for the decibel stretch and --colormap for pseudo-color.
umbra quicklook <item-json-url> --out scene.png --db --colormap magma

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
```

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
