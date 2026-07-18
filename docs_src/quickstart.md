# Quickstart

## Python

```python
from umbra_py import UmbraCatalog, download_item

catalog = UmbraCatalog()

# Find geocoded (GEC) scenes over an area, within a date range.
results = catalog.search(
    bbox=(-68.1, 10.4, -67.9, 10.6),   # min_lon, min_lat, max_lon, max_lat
    start="2023-01-01",
    end="2023-12-31",
    product_types=["GEC"],
    limit=10,
)

for item in results:
    print(item.id, item.datetime, item.product_types)

# Download the GEC GeoTIFF of the first match.
download_item(results[0], product_type="GEC", dest="./downloads")
```

### Search a site by name

```python
# Geocoded to a bounding box via OpenStreetMap; fuzzy matching tolerates typos.
results = catalog.search(place="Port of Rotterdam", limit=5)
```

### Load a scene as analysis-ready data

```python
from umbra_py import to_xarray

# Requires the [load] extra. Decimate a full scene to a manageable size, in dB.
da = to_xarray(results[0], product_type="GEC", decimation=8, decibels=True)
da.plot()
```

### Geocode a SICD into a map-ready GeoTIFF

```python
from umbra_py import sicd_to_geocoded_cog

# Requires the [convert] extra. `dem="auto"` fetches the covering Copernicus
# GLO-30 tiles and terrain-orthorectifies against them.
sicd_to_geocoded_cog("scene.nitf", "scene_geocoded.tif", dem="auto")
```

## Command line

```bash
# Fastest start: download the weekly prebuilt snapshot, then search it offline.
umbra index fetch
umbra index info

# Search by area, dates and product type.
umbra search --bbox -68.1 10.4 -67.9 10.6 --start 2023-01-01 --product-type GEC

# Or search by place name (geocoded to a bounding box via OpenStreetMap).
umbra search --place "Port of Rotterdam" --limit 5

# Inspect a single item by its STAC JSON URL.
umbra info https://<...>/stac.json

# Download an asset.
umbra download https://<...>/stac.json --product-type GEC --dest ./downloads

# Build an interactive, self-serve catalog explorer as a single HTML file.
umbra demo --out explorer.html
```

See the [CLI reference](cli.md) for every command and flag, and the
[example notebooks](guides/notebooks.md) for end-to-end walkthroughs.
