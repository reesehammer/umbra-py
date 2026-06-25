# Before/after SAR swipe maps

A [change composite](change.md) bakes two passes into one colored still and a
time-lapse flips between them. A **swipe map** is the interactive cousin: it
puts a draggable divider over a basemap, with the *before* acquisition on the
left of the seam and *after* on the right, and you wipe one over the other
across the same ground. Because radar backscatter is so stable between passes,
anything that changed — a ship that docked, a field that flooded, a building
that rose — snaps in and out as you sweep the handle. It's the most direct way
to *feel* change in the archive, and the output is a single self-contained HTML
file you can open in any browser or drop into a notebook.

Swipe maps need the `viz` extra (rasterio + numpy + Pillow + folium):

```bash
pip install "umbra-py[viz]"
```

---

## 1. The one-liner

Name a site and a time range, and `umbra swipe` searches, picks the earliest and
latest pass, and renders — no URL wrangling:

```bash
umbra swipe --area "Centerfield" --start 2024-01-01 --end 2024-12-31 --out swipe.html --db
```

It gathers that site's GEC acquisitions and compares the two ends of the range.
The selection prefers a single polarization (comparing HH against VV would show
the polarization difference as fake change); the chosen acquisitions are printed
before rendering. `--bbox min_lon,min_lat,max_lon,max_lat` works in place of
`--area` if you prefer coordinates.

`--db` uses the decibel (log-amplitude) stretch — the radiometrically-correct
SAR look that reveals texture the default linear stretch crushes toward black.

## 2. The explicit form

Already have two STAC item URLs? Pass them directly, **in chronological order**
(before then after):

```bash
umbra swipe \
  https://.../2024-01-05-.../<id>.stac.v2.json \
  https://.../2024-09-18-.../<id>.stac.v2.json \
  --out swipe.html
```

Only downsampled overviews are streamed via HTTP range requests — no full
download. `--max-size` controls each overlay's resolution (default 1024; larger
is sharper but fetches more bytes, roughly quadratically).

## 3. From Python

```python
from umbra_py import UmbraCatalog, save_swipe_map, select_change_frames

catalog = UmbraCatalog()
found = list(catalog.search(area="Centerfield", start="2024-01-01", end="2024-12-31"))
before, after = select_change_frames(found, frames=2)   # earliest, latest

save_swipe_map(before, after, "swipe.html", db=True)
```

`swipe_map(before, after, ...)` returns the `folium.Map` if you'd rather render
it inline in a notebook or add your own layers before saving.

---

## How it lines up

The two passes are **co-registered** onto one shared lon/lat grid — their
footprint intersection — exactly like a [change composite](change.md). Both
overlays then cover the *identical* ground at the *identical* pixel scale, so
the same field or road is continuous across the seam and only genuine change
moves as you drag. This matters because each SAR pass geocodes to a
differently-rotated rectangle: placing each by its own bounds would leave the
two sides misaligned at the divider. Only the requested overview resolution of
each cloud-optimized GeoTIFF is streamed, so there's no full download.

If the two footprints don't overlap, there's nothing to compare and the command
raises an error.

Pick `GEC` (the default, a detected GeoTIFF) or `CSI` with `--asset`; the
complex `SICD`/`CPHD` products aren't amplitude rasters and won't render.
