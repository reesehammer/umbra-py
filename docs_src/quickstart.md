# Quickstart

The fastest start is the weekly catalog snapshot, then a local search. A live
walk of the public bucket (`UmbraCatalog().search` / `umbra search` without
`--local`) works but is slow — Umbra publishes no STAC API. A community
instance of `umbra serve` restores one; see [Deploy](deploy.md) to point
`pystac-client` at it with no local install.

## Python

```python
from umbra_py import CatalogIndex, download_item, to_xarray

with CatalogIndex.from_release() as index:
    items = list(index.search(area="Centerfield", product_types=["GEC"], limit=3))

for item in items:
    print(item.id, item.datetime, item.available_assets)

first = items[0]
download_item(first, dest_dir="./downloads", assets=["GEC"])

# Stream a downsampled window over HTTP. Requires the [load] extra.
da = to_xarray(first, max_size=1024, db=True)
```

Without the snapshot, the same filters go to the live bucket:

```python
from umbra_py import UmbraCatalog

catalog = UmbraCatalog()
results = catalog.search(
    bbox=(-68.1, 10.4, -67.9, 10.6),
    start="2024-01-01",
    end="2024-12-31",
    product_types=["GEC"],
    limit=10,
)
```

`area=` matches an Umbra task-directory name (the fast path for a named site).
Place-name geocoding is a CLI flag (`umbra search --place "Rotterdam"`), not
an argument to `search()`.

### Geocode a SICD into a map-ready GeoTIFF

```python
from umbra_py import sicd_to_geocoded_cog

# Requires the [convert] extra. dem="auto" fetches Copernicus GLO-30 tiles.
sicd_to_geocoded_cog("scene.nitf", "scene_geocoded.tif", dem="auto")
```

Open products generally carry no `Radiometric` block, so `calibration=` and a
measured noise floor will refuse rather than invent numbers. See
[limitations](guides/limitations.md).

## Command line

```bash
# Fastest start: weekly snapshot, then search it offline.
umbra index fetch
umbra index info
umbra search --local --area Centerfield --product GEC --limit 3

# Preview a site as a contact sheet (needs [viz]).
umbra gallery --local --area Centerfield --limit 6 --out gallery.html --db

# Live walk (no snapshot) — slower.
umbra search --bbox -68.1,10.4,-67.9,10.6 --start 2024-01-01 --product GEC

# Place name (geocoded via OpenStreetMap). Mutually exclusive with --bbox.
umbra search --place "Port of Rotterdam" --limit 5

# Inspect / download / render one acquisition by its STAC JSON URL.
umbra info https://example.com/item.stac.v2.json
umbra download https://example.com/item.stac.v2.json --asset GEC --dest ./downloads
umbra quicklook https://example.com/item.stac.v2.json --out scene.png --db
```

See the [CLI reference](cli.md) for every command and flag, the
[example notebooks](guides/notebooks.md) for end-to-end walkthroughs, and
[limitations](guides/limitations.md) for what this library does not do.
