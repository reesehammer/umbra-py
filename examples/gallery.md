# Browsing the catalog with a SAR gallery

`umbra search` tells you *what* a query returns and [`umbra map`](maps.md) shows
*where* — but neither lets you actually *look* at the imagery before downloading
multi-gigabyte SAR files. A **gallery** is the missing piece: a contact sheet of
streamed SAR quicklook thumbnails laid out as a grid in one self-contained HTML
page. Each tile shows the acquisition's SAR look, a footprint sketch for spatial
orientation, and a collapsible panel of copy-paste URLs to feed into other
commands. It's the "browse the catalog visually" primitive.

Only downsampled cloud-optimized GeoTIFF *overviews* are fetched (via HTTP range
requests, in parallel) — never a full download — so a 24-tile gallery of a whole
state costs a handful of megabytes, not gigabytes.

Galleries need the `viz` extra (rasterio + numpy + Pillow):

```bash
pip install "umbra-py[viz]"
```

---

## 1. The one-liner

Name a site (or a date window) and `umbra gallery` searches, streams a thumbnail
per match, and writes the page:

```bash
umbra gallery --area "Centerfield" --out gallery.html --db
```

Open `gallery.html` in any browser. `--db` uses the decibel (log-amplitude)
stretch — the radiometrically-correct SAR look that reveals texture the default
linear stretch crushes toward black.

---

## 2. Search by place name

Don't know the coordinates? Hand `--place` a free-text geography and it's
geocoded to a bounding box via OpenStreetMap Nominatim:

```bash
umbra gallery --place "California" --start 2024-01-01 --end 2024-12-31 --out california.html
```

The resolved place is printed so you can confirm the match:

```
Resolved 'California' to California, United States.
```

The box is rectangular, so a state-sized query also catches footprints in the
box's corners just outside the true outline. `--place` is mutually exclusive with
`--bbox`, and works the same way on `umbra search` and `umbra map`.

---

## 3. What's on each tile

Every tile carries:

- **The SAR quicklook** — a streamed thumbnail of the acquisition. Click it to
  open the STAC item JSON. When a preview can't be fetched (no GEC asset, a
  decode error), the tile falls back to a footprint sketch so it's never blank.
- **A footprint badge** — a small north-up sketch of the ground footprint, since
  the square-cropped thumbnail loses the true shape.
- **A `URLs` panel** — expand it for two click-to-select boxes (one click grabs
  the whole string — no JavaScript): the asset's direct download URL (the GEC
  GeoTIFF) and the STAC item URL. Paste the **asset URL** into `curl`, GDAL
  `/vsicurl/`, or rasterio; paste the **STAC item URL** into
  `umbra info | download | quicklook | load`:

  ```bash
  # The asset URL is the file itself:
  curl -O "<asset-url>"

  # The STAC item URL is what the other subcommands consume:
  umbra quicklook "<stac-item-url>" --out scene.png --db
  umbra download  "<stac-item-url>" --asset GEC --dest downloads/
  ```

---

## 4. Narrowing what shows up

`umbra gallery` shares the
[search-side options](maps.md#3-search-side-options-what-ends-up-on-the-map) of
`map`:

```bash
# One tile per distinct site (not every revisit), GEC only, capped at 24.
umbra gallery \
    --place "San Francisco Bay" \
    --start 2024-01-01 --end 2024-06-30 \
    --max-per-task 1 \
    --limit 24 \
    --out sf_bay.html
```

`--max-per-task 1` trades revisit density for geographic diversity — handy for a
"where does the archive have imagery?" overview. `--asset` selects which product
each thumbnail renders **and** which URL the panel exposes (default `GEC`; `CSI`
also works — the complex `SICD`/`CPHD` products aren't amplitude rasters).

---

## 5. From Python

```python
from umbra_py import UmbraCatalog, save_gallery

catalog = UmbraCatalog()
items = list(catalog.search(area="Centerfield", limit=24))

save_gallery(items, "gallery.html", db=True)
```

`gallery(items, ...)` returns the HTML as a string if you'd rather post-process
it or serve it directly. Tuning knobs:

- **`max_size`** — thumbnail resolution (default 512). Larger is sharper but
  fetches more bytes per tile (~quadratic).
- **`max_workers`** — how many thumbnails stream in parallel (default 8).
- **`colormap`** — a matplotlib colormap name for pseudo-colored thumbnails.
- **`subtitle`** — text for the page header (the CLI fills this with your search
  terms).

Need just a bounding box from a place name in your own code? `geocode_place`
exposes the same forward geocoder the CLI uses:

```python
from umbra_py import geocode_place

bbox, label = geocode_place("California")   # ((-124.4, 32.5, -114.1, 42.0), "California, United States")
items = list(catalog.search(bbox=bbox, start="2024-01-01", end="2024-12-31"))
```

---

## How it works

Each tile's thumbnail is a [quicklook](quicklook.md) read at a small `max_size`:
overview selection means only a few range requests hit the cloud-optimized
GeoTIFF, never the full-res image. The reads run on a thread pool (GDAL releases
the GIL during `/vsicurl` I/O), so an N-tile sheet streams in roughly
N / `max_workers` of the serial time.

Resilience is deliberate: any item whose thumbnail can't be fetched falls back to
its offline footprint sketch instead of failing the page, so one bad acquisition
never sinks a 50-tile gallery. The whole output is a single HTML file with the
thumbnails embedded as base64 PNGs — no sidecar images, no tile server, nothing
to host.
