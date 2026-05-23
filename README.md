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

items = list(UmbraCatalog().search(start="2024-01-01", end="2024-01-31", limit=50))

# Interactive Folium map for notebooks / sharing (requires the `viz` extra).
footprint_map(items).save("footprints.html")

# Or export to GeoJSON for QGIS, leafmap, Earth Engine, geopandas, deck.gl, ...
write_geojson(items, "footprints.geojson")
```

### Command line

```bash
# Search by area, dates and product type.
umbra search --bbox -68.1,10.4,-67.9,10.6 --start 2024-01-01 --end 2024-01-31 --product GEC

# Inspect a single item by its STAC JSON URL.
umbra info <item-json-url>

# Download specific asset(s).
umbra download <item-json-url> --asset GEC --dest downloads/

# Visualize search results: interactive HTML map or GeoJSON for any GIS.
umbra map --start 2024-01-01 --end 2024-01-31 --product GEC --out footprints.html
umbra map --start 2024-01-01 --end 2024-01-31 --product GEC --out footprints.geojson
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
