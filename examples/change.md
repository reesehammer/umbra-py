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

## 1. The bare minimum

Pass the item URLs in **chronological order**:

```bash
umbra change "<earlier-url>" "<later-url>" --out change.png
```

```python
from umbra_py import UmbraCatalog, save_change_composite

# Two acquisitions of the same Umbra task (same site, different days).
items = list(UmbraCatalog().search(
    start="2024-01-01", end="2024-12-31", product_types=["GEC"], limit=12,
))
# ... pick two items from the same umbra:task_id, oldest first ...
save_change_composite([earlier, later], "change.png")
```

Both co-register the inputs onto a shared lon/lat grid (only a downsampled
overview of each cloud-optimized GeoTIFF is streamed via HTTP range requests —
no multi-gigabyte download), stretch each date for SAR's dynamic range, and
write the composite. `change_composite([...], ...)` returns a `PIL.Image` if
you'd rather display or post-process it in a notebook.

---

## 2. Reading the colors

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

## 3. Getting the item URLs

Each URL is an item's `.stac.v2.json` sidecar. The easiest source is `umbra
search`, which prints a `url:` line per result. Because change detection needs
acquisitions of the *same area*, search within one task's repeat coverage:

```bash
umbra search --start 2024-01-01 --end 2024-12-31 --product GEC --limit 10
```

Look for several results sharing a location (an Umbra **task** is repeat
imaging of one site), and copy two or three `url:` values in date order.
**Quote them** — Umbra's named-task directories contain spaces (`%20`) and
commas. `umbra info <url>` confirms a URL parses (and prints its `acquired`
time) before you render.

If the footprints don't overlap, `change` raises a clear error — there's
nothing to compare. Pick acquisitions of the same place.

---

## 4. Every option

| Flag | Domain | Default | Notes |
| ---- | ------ | ------- | ----- |
| `ITEM_URLS` (positional) | 2 or 3 STAC `.stac.v2.json` URLs | — | required; chronological order |
| `--out` | any path | — | required; **extension picks the format** (`.png`, `.jpg`, …) |
| `--asset` | `GEC` \| `CSI` \| `SIDD` \| `SICD` \| `CPHD` | `GEC` | `GEC`/`CSI` are the sensible targets |
| `--max-size` | positive integer (pixels) | `2048` | longer side of the shared grid; larger = sharper but more bytes (~quadratic) |
| `--db` | flag | off | decibel (log-amplitude) stretch — the radiometrically-correct view |
| `--percentile` | `"low,high"` | `"2,98"` | per-date contrast cut percentiles |

The Python `change_composite` / `save_change_composite` functions take the same
options as keyword arguments (`asset=`, `max_size=`, `db=`,
`percentile=(2.0, 98.0)`).

---

## 5. Recipe gallery

### Search → composite the two latest passes of a task

```python
from collections import defaultdict
from umbra_py import UmbraCatalog, save_change_composite

by_task = defaultdict(list)
for item in UmbraCatalog().search(
    start="2024-01-01", end="2024-12-31", product_types=["GEC"], limit=40,
):
    task = item.properties.get("umbra:task_id")
    if task and "GEC" in item.available_assets:
        by_task[task].append(item)

# First task with at least two passes; oldest → newest.
passes = next(v for v in by_task.values() if len(v) >= 2)
passes.sort(key=lambda i: i.datetime)
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

## 6. Troubleshooting

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

