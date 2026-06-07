# SAR change composites

SAR's signature trick is change detection. Radar backscatter from a fixed
scene is remarkably stable between passes, so anything that *did* change
between two acquisitions — a ship that arrived, a field that flooded, a
building that went up — jumps out against the static background. `umbra
change` turns 2–3 acquisitions of the same site into a single color image
where unchanged ground stays gray and change is tinted by *when* it happened.

The [`quicklook`](quicklook.md) guide answers "*what does this one scene look
like?*"; a change composite answers "*what moved between these passes?*" — and
it does the co-registration for you, so you never touch a GIS.

Change composites need the `viz` extra (rasterio + numpy + Pillow):

```bash
pip install "umbra-py[viz]"
```

---

## 1. The one-liner

Name a site and a time range, and `umbra change` searches, picks the dates, and
renders — no URL wrangling:

```bash
umbra change --area "Centerfield" --start 2024-01-01 --end 2024-12-31 --out change.png --db
```

It gathers that site's GEC acquisitions, selects two dates spanning the range
(earliest and latest), and composites them. Add `--frames 3` for an
earliest/middle/latest temporal trail. The selection prefers a single
polarization (comparing HH against VV would show the polarization difference as
fake change); the chosen acquisitions are printed before rendering. `--bbox
min_lon,min_lat,max_lon,max_lat` works in place of `--area` if you prefer
coordinates.

## 2. The explicit form

To choose exactly which acquisitions to compare, pass 2–3 STAC URLs in
**chronological order**:

```bash
umbra change "<earlier-url>" "<later-url>" --out change.png
```

```python
from umbra_py import UmbraCatalog, save_change_composite, select_change_frames

# Auto-select frames from a site's passes...
passes = list(UmbraCatalog().search(
    area="Centerfield", product_types=["GEC"], start="2024-01-01", end="2024-12-31",
))
save_change_composite(select_change_frames(passes, frames=2), "change.png", db=True)

# ...or hand-pick and pass them straight through (oldest first).
save_change_composite([earlier, later], "change.png")
```

Both co-register the inputs onto a shared lon/lat grid (only a downsampled
overview of each cloud-optimized GeoTIFF is streamed via HTTP range requests —
no multi-gigabyte download), stretch each date for SAR's dynamic range, and
write the composite. `change_composite([...], ...)` returns a `PIL.Image` if
you'd rather display or post-process it in a notebook.

---

## 3. Reading the colors

Each date is mapped to a color channel, so an **unchanged** pixel — equal
brightness on every pass — lands on the gray diagonal (gray/white/black). Only
the area imaged on *every* pass is colored; ground missing from any
acquisition is transparent.

**Two dates** (`R = t1, G = t2, B = t1`):

| Color | Meaning |
| ----- | ------- |
| **Green** | Backscatter *appeared* in the later pass (new bright target — a ship, a vehicle, a new structure) |
| **Magenta** | Backscatter *vanished* (a bright target that left, or a surface that smoothed/flooded) |
| Gray / white / black | Unchanged between the two passes |

**Three dates** (`R = t1, G = t2, B = t3`): an earliest→latest temporal-RGB.
A bright target that moves across the scene leaves a red → green → blue trail,
one frame per pass; stationary scene stays gray.

---

## 4. Getting the item URLs (explicit form)

Each URL is an item's `.stac.v2.json` sidecar. Because change detection needs
acquisitions of the *same area*, the easy path is `umbra search --area`: Umbra
files every pass of a site under one named directory, so naming it returns just
that site's acquisitions (and runs faster, since the rest of the catalog is
skipped):

```bash
umbra search --area "Centerfield" --product GEC --start 2024-01-01 --end 2024-12-31
```

Copy two or three `url:` values in date order — ideally **matching
polarization and resolution** (the `product` line shows `pol=`), so the
composite reflects real change and not a polarization or look-geometry
difference. **Quote the URLs** — named-task directories contain spaces (`%20`)
and commas. `umbra info <url>` confirms a URL parses (and prints its `acquired`
time) before you render.

Don't know the site name? List the areas with repeat coverage first:

```bash
umbra search --start 2024-01-01 --end 2024-12-31 --product GEC --limit 500 \
  | grep -o 'tasks/[^/]*/' | sort | uniq -c | sort -rn | head
```

Anything with a count ≥ 2 is a change-detection candidate; feed its name back
to `--area`. Or, if you know coordinates, `umbra search --bbox
min_lon,min_lat,max_lon,max_lat` constrains to a box — every result overlaps
it.

If the footprints don't overlap, `change` raises a clear error — there's
nothing to compare. Pick acquisitions of the same place.

---

## 5. Every option

| Flag | Domain | Default | Notes |
| ---- | ------ | ------- | ----- |
| `ITEM_URLS` (positional) | 2 or 3 STAC `.stac.v2.json` URLs | — | explicit form; chronological order. Mutually exclusive with the search flags |
| `--area` | task/site name substring | — | search form; gather a site's passes (see [`maps`](maps.md) / §4) |
| `--bbox` | `min_lon,min_lat,max_lon,max_lat` | — | search form; alternative to `--area` |
| `--start` / `--end` | `YYYY-MM-DD` | — | search form; bound the time range |
| `--frames` | `2` or `3` | `2` | search form; how many dates to composite, spread across the range |
| `--max-search` | positive integer | `50` | search form; cap acquisitions pulled before selecting frames |
| `--out` | any path | — | required; **extension picks the format** (`.png`, `.jpg`, …) |
| `--asset` | `GEC` \| `CSI` \| `SIDD` \| `SICD` \| `CPHD` | `GEC` | `GEC`/`CSI` are the sensible targets |
| `--max-size` | positive integer (pixels) | `2048` | longer side of the shared grid; larger = sharper but more bytes (~quadratic) |
| `--db` | flag | off | decibel (log-amplitude) stretch — the radiometrically-correct view |
| `--percentile` | `"low,high"` | `"2,98"` | per-date contrast cut percentiles |

Pass **either** item URLs **or** the search flags, not both. In search form,
`--asset` doubles as the product filter (only acquisitions exposing it are
considered).

The Python `change_composite` / `save_change_composite` functions take the same
options as keyword arguments (`asset=`, `max_size=`, `db=`,
`percentile=(2.0, 98.0)`).

---

## 6. Recipe gallery

### One command: a site's change over a year

```bash
umbra change --area "Centerfield" --start 2024-01-01 --end 2024-12-31 \
    --out change.png --db
```

### Three-frame temporal trail over a range

```bash
umbra change --area "Centerfield" --start 2024-01-01 --end 2024-12-31 \
    --frames 3 --out trail.png --db
```

### Search a named site → composite its two earliest passes

```python
from umbra_py import UmbraCatalog, save_change_composite

passes = sorted(
    UmbraCatalog().search(
        area="Centerfield", product_types=["GEC"],
        start="2024-01-01", end="2024-12-31",
    ),
    key=lambda i: i.datetime,  # oldest → newest
)
save_change_composite(passes[:2], "change.png", db=True)
```

### Three-date temporal-RGB

```bash
umbra change "<t1-url>" "<t2-url>" "<t3-url>" --out trail.png --db
```

### Post-process in a notebook

```python
from umbra_py import change_composite

img = change_composite([earlier, later], db=True, max_size=1024)  # a PIL.Image
img.save("change.png")
```

---

## 7. Troubleshooting

- **`change_composite needs 2 or 3 acquisitions`** — pass exactly two or three
  item URLs.
- **`Footprints do not overlap`** — the acquisitions image different places.
  Change detection needs the *same* area; pick items from one Umbra task.
- **`Image has no valid pixels to stretch`** — a fetched overview was all
  nodata (e.g. a non-amplitude `--asset` like SICD/CPHD). Use `--asset GEC`.
- **`MissingDependencyError: 'rasterio' is required`** — install the extra:
  `pip install "umbra-py[viz]"`.
- **The whole image is gray** — that's the honest answer: little changed
  between the passes. Try acquisitions further apart in time, or add `--db` to
  bring out subtler differences.
- **File is bigger/slower than expected** — `--max-size` is the lever; it's
  quadratic in filesize and fetch cost.
```

