# Generating Umbra maps

`umbra-py` turns Umbra SAR acquisitions into maps you can open in a browser,
drop into a notebook, or hand to another GIS tool. This document walks through
every knob the library exposes for that — from the one-liner that renders a
single footprint to the full interactive map with SAR imagery overlaid on top
of an OpenStreetMap basemap.

Everything below works against Umbra's public, anonymous-access catalog. The
core install is enough for GeoJSON; the interactive HTML maps and SAR
overlays need the `viz` extra:

```bash
pip install "umbra-py[viz]"
```

---

## 1. The two output formats

There are two fundamentally different things you can produce:

| Output                  | What you get                                     | Extra needed | Best for                                                   |
| ----------------------- | ------------------------------------------------ | ------------ | ---------------------------------------------------------- |
| **GeoJSON FeatureCollection** | A `.geojson` / `.json` file of polygons + metadata | none         | QGIS, leafmap, Earth Engine, geopandas, deck.gl, scripts   |
| **Interactive Folium map**   | A self-contained `.html` file (Leaflet + tiles)    | `viz`        | Quick visual exploration, sharing a link, embedding in a notebook |

The interactive map can additionally embed each acquisition's **SAR image** as
a base64 PNG overlay so the HTML stays a single self-contained file (no tile
server, no external dependencies at view time). That needs `rasterio`, which
the `viz` extra also pulls in.

---

## 2. The bare minimum

```python
from umbra_py import UmbraCatalog, footprint_map

catalog = UmbraCatalog()
items = list(catalog.search(start="2024-02-08", end="2024-02-08", limit=10))

m = footprint_map(items)
m.save("umbra.html")
```

This searches one day of Umbra's archive, then builds an interactive map
that auto-fits to the union of all returned footprints. Each acquisition
becomes a polygon with a popup showing the item's metadata (acquisition
time, platform, mode, resolution, polarizations, available assets) and a
link to the source STAC item.

Items without a geometry or bbox are silently skipped — they can't be
plotted.

---

## 3. Search-side options (what ends up on the map)

The map only shows items the catalog search returns, so most of your
control over what's on the map happens at search time. The relevant
`UmbraCatalog.search` parameters:

```python
from umbra_py import UmbraCatalog

catalog = UmbraCatalog()
items = list(
    catalog.search(
        bbox=(-122.6, 37.5, -122.2, 37.9),   # spatial filter: SF Bay Area
        start="2024-01-01",                  # inclusive date bounds (str or date)
        end="2024-03-31",
        product_types=["GEC"],               # keep only items with a GEC GeoTIFF
        limit=50,                            # stop after this many matches
        max_per_task=1,                      # one acquisition per tasking campaign
    )
)
```

A few notes that matter for cartographic results:

- **`bbox`** is `(min_lon, min_lat, max_lon, max_lat)` in WGS-84 degrees.
  An item is kept if its footprint overlaps the box at all (not strictly
  contained).
- **`product_types=["GEC"]`** is what you almost always want for visual
  maps: GEC is the cloud-optimized GeoTIFF, and it's the only product
  that the imagery overlay (§5) can render.
- **`max_per_task=1`** is the difference between "twenty revisits of the
  same airport" and "twenty distinct sites". Each Umbra "task" is a
  tasking campaign over one area, so capping per-task replaces revisit
  density with geographic diversity on the map.
- **`limit`** stops the search early. Without it, an unbounded date
  window iterates the entire archive — that's slow and produces a map so
  dense it's unreadable.

---

## 4. Folium map styling options

`footprint_map(items, ...)` accepts the following keyword arguments:

```python
m = footprint_map(
    items,
    tiles="OpenStreetMap",   # basemap: any Folium-recognised tile name or URL
    color="#ff5500",         # polygon outline + centroid marker colour
    weight=2,                # polygon outline width in pixels
    fill_opacity=0.15,       # polygon fill opacity (0 = outline only)
    zoom_start=None,         # initial zoom level; ignored if items have a bbox
    imagery=False,           # render the SAR image overlay (§5)
    imagery_kwargs=None,     # per-overlay kwargs, see §5
)
```

### Basemap (`tiles`)

Folium ships with several built-in basemap names. Common picks:

- `"OpenStreetMap"` — the default; readable street labels worldwide.
- `"CartoDB positron"` — pale, low-contrast map that lets bright
  footprints and SAR overlays pop visually.
- `"CartoDB dark_matter"` — dark mode; good for screenshots.
- A custom XYZ tile URL — pass the template directly and Folium will
  use it as-is.

For any custom tile server, give Folium an attribution string by
constructing a `folium.Map` yourself and passing the `tiles` /
`attr` arguments — but for almost every map, the built-in names are
fine.

### Polygon styling

- `color` accepts any CSS colour. The centroid marker uses the same
  colour so a single tiny footprint stays findable at world zoom (a
  filled dot for "imagery rendered", a hollow ring for "footprint only").
- `weight=0` makes the outline disappear; combine with
  `fill_opacity=0.4` for a softer, filled-only look.
- `fill_opacity=0` makes the polygon an outline-only sketch — useful
  when you want the basemap to remain fully visible underneath.

### Initial zoom

`zoom_start` is only used as a fallback when none of the items have a
bbox. When items have bboxes (almost always), the map calls
`fit_bounds` on the union of footprints instead, so `zoom_start` is
ignored. To force a wider view, post-process the returned
`folium.Map` (e.g. `m.fit_bounds(...)` with your own bounds).

---

## 5. SAR imagery overlay

Setting `imagery=True` streams each item's GEC GeoTIFF via HTTP range
requests, downsamples it, applies a percentile contrast stretch, and
composites it onto the basemap as a base64-encoded PNG. The output HTML
remains a single self-contained file — no tile server, no external
references.

```python
m = footprint_map(
    items,
    imagery=True,
    imagery_kwargs={
        "max_size": 2048,            # max pixel dim per overlay (default 1024)
        "percentile": (2.0, 98.0),   # contrast stretch percentiles
        "opacity": 1.0,              # overlay opacity 0..1
        "asset": "GEC",              # asset key (rarely changed)
    },
)
```

What each option does:

- **`max_size`** — the largest dimension (width or height) the overlay
  will be downsampled to. SAR data is inherently grainy (speckle), so
  cranking this up reveals more detail *and* more noise. Default
  `1024`; `2048` and `4096` are reasonable for one-or-two-item maps;
  beyond that the HTML file size (quadratic in `max_size`) becomes
  painful. Each overlay is fetched via HTTP range requests against a
  cloud-optimized GeoTIFF, so only the bytes for the requested
  resolution are downloaded.
- **`percentile`** — the low/high cut for the contrast stretch. SAR
  amplitudes span enormous dynamic range; a straight 0–255 scaling
  looks almost black. The default `(2.0, 98.0)` clips the dimmest and
  brightest 2 % of pixels for a punchy stretch. Try `(5.0, 95.0)` for
  more contrast or `(0.5, 99.5)` for more highlight detail.
- **`opacity`** — `1.0` for opaque imagery, `0.7` for a slight blend
  with the basemap, `0.0` to hide overlays entirely while keeping the
  polygons.
- **`asset`** — defaults to `"GEC"`. Other asset types (`SICD`,
  `CPHD`) aren't geocoded ground-plane rasters, so they won't overlay
  meaningfully — leave this alone unless you have a reason.

Items that don't have a GEC asset, or whose imagery can't be fetched
(404, decode error, no valid pixels), are skipped with a warning. The
footprint polygon and centroid marker still render. The legend in the
top-right shows how many items had imagery vs footprint-only.

---

## 5.5 Lazy SAR imagery (fetch on click)

`imagery=True` is great when you want everything overlaid up front,
but the resulting HTML carries a base64 PNG per item — a 100-item map
can weigh hundreds of MB. `lazy_imagery=True` is the opposite
tradeoff: each popup ships with a "Get SAR image" button, and the
COG is streamed straight from the Umbra bucket in the browser on
click. The map weighs ~30 KB regardless of how many items it carries.

```python
m = footprint_map(
    items,
    lazy_imagery=True,
    lazy_imagery_percentile=(2.0, 98.0),   # contrast stretch (same default)
    lazy_imagery_asset="GEC",              # asset key (rarely changed)
)
m.save("lazy.html")
```

How it works:

- The HTML pulls `georaster-layer-for-leaflet` + `geotiff.js` from a
  pinned CDN (`unpkg.com`). The bundles only load when the user
  actually opens a popup and clicks the button.
- The COG is fetched via HTTP range requests — only the bytes for the
  chosen overview level are downloaded. Umbra's public bucket has
  permissive CORS, which is what makes browser-direct streaming
  possible.
- A small JS percentile pass over the decoded pixels picks the
  contrast cuts (same math as the Python overlay path). Invalid /
  non-positive pixels render transparent so basemap shows through
  scene edges.
- Click the button again to remove the overlay.

When to pick which:

| Scenario                                 | Use            |
| ---------------------------------------- | -------------- |
| Single map, you want everything visible immediately | `imagery=True` |
| Many items, exploring on demand          | `lazy_imagery=True` |
| Animated timeline                        | `lazy_imagery=True` (works with `timeline_map`) |
| Sharing a self-contained file that works offline | `imagery=True` |

The two are mutually exclusive — both would try to add a SAR raster
per item, so passing both raises `ValueError`.

CLI:

```bash
umbra map \
    --start 2024-01-01 --end 2024-06-30 \
    --product GEC --max-per-task 1 \
    --limit 200 \
    --lazy-imagery \
    --out lazy.html

# Combined with the timeline view: scrub to a moment, click the
# polygon, see the actual SAR.
umbra map \
    --start 2024-01-01 --end 2024-06-30 \
    --product GEC --max-per-task 1 \
    --timeline --timeline-period P7D \
    --lazy-imagery \
    --out coverage_with_sar.html
```

---

## 6. Working with a single item

`UmbraItem.to_geojson()` returns a single GeoJSON `Feature` you can
feed into any 2D-aware GIS tool:

```python
item = items[0]
feature = item.to_geojson()           # dict
print(feature["geometry"]["type"])    # 'Polygon'
print(feature["properties"]["id"])
```

For a one-item interactive map, just pass a single-element list to
`footprint_map`:

```python
m = footprint_map([item], imagery=True)
m.save("one_item.html")
```

For a one-item, no-overlay image you can also call `image_overlay`
directly and add it to a `folium.Map` you've built yourself — useful
when composing the SAR layer alongside other Folium layers (markers,
heatmaps, drawing tools):

```python
import folium
from umbra_py import image_overlay

m = folium.Map(location=(37.7, -122.4), zoom_start=11, tiles="CartoDB positron")
overlay = image_overlay(item, max_size=2048, percentile=(2.0, 98.0), opacity=0.9)
overlay.add_to(m)
m.save("custom.html")
```

---

## 6.5 Animated timeline map

Static maps answer *where*. The timeline map answers *when*. It's the
same search results re-rendered as a TimestampedGeoJson layer with a
play button and a slider beneath the map, so you can watch Umbra's
coverage accumulate over your requested window.

```python
from umbra_py import UmbraCatalog, timeline_map

items = list(UmbraCatalog().search(
    start="2024-01-01", end="2024-06-30",
    product_types=["GEC"],
    max_per_task=1,
    limit=200,
))

m = timeline_map(items, period="P7D")   # one tick = one week
m.save("coverage.html")
```

What the knobs do:

- **`period`** — ISO 8601 duration controlling the slider's step size.
  Match it to the cadence of your search: `"PT1H"` for one day's
  acquisitions, `"P1D"` for a month, `"P7D"` for a year. Too small and
  the playhead crawls; too large and bursts collapse into a single
  tick.
- **`duration`** — how long each footprint stays visible after its
  timestamp. `None` (the default) leaves footprints on the map once
  revealed, so the animation accumulates coverage. Pass an ISO duration
  like `"P1D"` for a "show each day's collection then fade" effect —
  useful for spotting one-off events vs. sustained tasking.
- **`auto_play`** — start the animation when the page loads. Default
  `True`; flip to `False` if you'd rather the viewer press play.
- **`loop`** — restart from the beginning when the slider reaches the
  end.
- **`transition_time`** — milliseconds between ticks during playback.
  Lower = snappier animation; raise it for a more deliberate pace.
- **`tiles`, `color`, `weight`, `fill_opacity`, `zoom_start`** —
  identical to `footprint_map`.
- **`geocode`, `geocode_zoom`** — identical to `footprint_map`. Each
  footprint's centroid is reverse-geocoded via OpenStreetMap Nominatim
  and the resulting place name appears as a "Location" row in the
  popup. Off by default (so a library call doesn't make surprise
  network requests); throttled to ~1 req/s and cached, so a 100-item
  timeline takes ~100 s on first render and reruns are fast. The CLI's
  `--geocode/--no-geocode` flag flows through to `--timeline` too.

Items without a datetime *or* a geometry are silently skipped (they
can't be placed on a time axis). Click any footprint mid-animation and
you get the same metadata popup `footprint_map` renders.

Pre-baked `imagery=True` overlays aren't supported on the timeline
(animating base64 rasters across the slider is a separate problem),
but **`lazy_imagery=True` is** — every popup gets a "Get SAR image"
button that streams the COG on click. Scrub the timeline to a moment,
click the polygon, see the actual SAR. See §5.5 for the details.

CLI:

```bash
umbra map \
    --start 2024-01-01 --end 2024-06-30 \
    --product GEC \
    --max-per-task 1 \
    --limit 200 \
    --timeline --timeline-period P7D \
    --out coverage.html
```

---

## 7. Persisted outputs

### Save a Folium map to HTML

```python
from umbra_py import save_footprint_map, save_timeline_map

save_footprint_map(items, "out/umbra.html", imagery=True, color="#00aaff")
save_timeline_map(items, "out/coverage.html", period="P7D")
```

`save_footprint_map` and `save_timeline_map` are thin wrappers that
build the map with the same kwargs as `footprint_map` /
`timeline_map` respectively and write the resulting standalone HTML
to disk, creating parent directories as needed.

### Export GeoJSON

```python
from umbra_py import write_geojson, items_to_featurecollection

# Write straight to disk
write_geojson(items, "out/footprints.geojson", indent=2)

# Or build the FeatureCollection dict in memory (e.g. to POST it somewhere)
fc = items_to_featurecollection(items)
```

The exported GeoJSON has every item as a `Feature` with properties
matching `UmbraItem.metadata_summary()` plus a `stac_href` pointing
back at the source STAC item. Pass `indent=None` for a minified file.

---

## 8. CLI: maps without writing code

The `umbra map` subcommand wraps the same search and rendering logic
behind one command:

```bash
# Basic interactive map of a date window
umbra map --start 2024-02-08 --end 2024-02-08 --limit 20 --out map.html

# Spatial filter, GEC only, with SAR imagery overlay
umbra map \
    --bbox -122.6,37.5,-122.2,37.9 \
    --start 2024-01-01 --end 2024-03-31 \
    --product GEC \
    --max-per-task 1 \
    --imagery \
    --imagery-max-size 2048 \
    --limit 30 \
    --out sf_bay.html

# Same query but export GeoJSON for QGIS / geopandas instead
umbra map \
    --bbox -122.6,37.5,-122.2,37.9 \
    --start 2024-01-01 --end 2024-03-31 \
    --out sf_bay.geojson
```

The output extension picks the format: `.html` / `.htm` → Folium map;
`.geojson` / `.json` → GeoJSON FeatureCollection. `--imagery` and
`--imagery-max-size` only apply to HTML output. Every other flag
maps directly to a `UmbraCatalog.search` parameter from §3.

---

## 9. Recipe gallery

A handful of complete, copy-pasteable patterns for common needs.

### One-site time series

Plot every revisit of a tasking campaign on a single map. Useful for
spotting how a site changed across the archive.

```python
items = list(catalog.search(
    bbox=(-118.42, 33.90, -118.36, 33.96),   # LAX
    start="2024-01-01", end="2024-12-31",
    product_types=["GEC"],
    limit=200,
))
m = footprint_map(items, color="#0066cc", fill_opacity=0.05, imagery=False)
m.save("lax_revisits.html")
```

### Global diversity map

One acquisition per tasking campaign, world-wide, no imagery (just
footprints) — a quick overview of where Umbra has been collecting.

```python
items = list(catalog.search(
    start="2024-06-01", end="2024-06-30",
    product_types=["GEC"],
    max_per_task=1,
    limit=300,
))
save_footprint_map(items, "june_2024.html", tiles="CartoDB dark_matter",
                   color="#00ff88", fill_opacity=0.3)
```

### High-fidelity SAR look at one acquisition

Maximum overlay resolution, minimal map decoration.

```python
item = next(catalog.search(start="2024-02-08", end="2024-02-08",
                           product_types=["GEC"], limit=1))
m = footprint_map(
    [item],
    tiles="CartoDB positron",
    fill_opacity=0,                       # no fill — let the imagery speak
    weight=1,
    imagery=True,
    imagery_kwargs={"max_size": 4096, "percentile": (1.0, 99.0)},
)
m.save("hires.html")
```

### Pipe into geopandas

```python
import geopandas as gpd
from umbra_py import items_to_featurecollection

gdf = gpd.GeoDataFrame.from_features(items_to_featurecollection(items)["features"])
gdf.set_crs("EPSG:4326", inplace=True)
gdf.explore()        # geopandas' own folium-backed map, with full pandas tooling
```

---

## 10. Troubleshooting

- **Map is empty / `Wrote 0 footprints`** — your search returned
  nothing. Loosen `bbox`, widen the date window, or drop
  `product_types`. Run `umbra search` with the same flags to see what
  the catalog actually has.
- **"MissingDependencyError: 'folium' is required"** — install the
  extra: `pip install "umbra-py[viz]"`.
- **HTML opens but no SAR imagery shows** — check the warnings on
  stderr; common causes are items without a `GEC` asset or transient
  S3 fetch errors. The polygons render either way.
- **HTML file is huge** — `max_size` is the lever; halve it (or drop
  `imagery=True` entirely). Each overlay is embedded as a PNG, so a
  map with 50 items at `max_size=4096` will be hundreds of MB.
- **Polygon visible but no marker / no popup** — that item had no
  bbox at all. Check `item.bbox`; if it's `None`, the STAC document
  was missing both `bbox` and `geometry`.
