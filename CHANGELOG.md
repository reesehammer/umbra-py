# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- Lazy SAR imagery now surfaces a clear **"Open via http://"**
  message (with the exact `python3 -m http.server` command) when the
  page is opened from a `file://` origin, instead of silently failing
  -- Chromium-family browsers refuse to spawn the georaster bundle's
  worker chunks from `file://` under unique-origin rules.
- Degenerate-percentile fallback in the JS stretch no longer adds
  an absolute `+1` (which rendered any normalized-amplitude raster
  with values in `[0, 0.05]` as solid black); uses a relative epsilon
  centered on the value so uniform-pixel scenes render as mid-gray.
- The driver now resolves the running map by DOM-walking from the
  clicked button to its enclosing `.folium-map` div, instead of
  closing over a single `map_var` string. Fixes silent misrouting in
  multi-map pages and after Jupyter cell reruns.
- CDN URLs are emitted as JSON-encoded JS strings (was Python `repr`),
  so a future bump to a URL with quotes or non-ASCII characters can't
  silently break the driver template.
- `noDataValue` is coerced to `Number` before equality comparison;
  COGs that publish string-typed nodata no longer leak into the
  percentile sample.
- Percentile picks share one in-place sort instead of two `slice().sort()`
  passes.

### Added
- **Browser-side lazy SAR imagery** via a new `lazy_imagery=True` kwarg
  on `footprint_map` and `timeline_map`, plus a matching
  `umbra map --lazy-imagery` CLI flag. Each popup gets a "Get SAR
  image" button; on click, the page streams the GEC cloud-optimized
  GeoTIFF directly from the Umbra public bucket via HTTP range
  requests using `georaster-layer-for-leaflet` + `geotiff.js` (pulled
  in once from a pinned CDN). The contrast stretch runs in JS with the
  same percentile-and-transparent-invalid-pixels logic Python's
  `_stretch_to_rgba` uses; second click on the same button removes the
  layer. A 200-item map weighs ~30 KB regardless of how many items it
  carries, and users only pay the fetch cost for items they actually
  open. Works with `--timeline` (scrub to a moment, click the
  polygon, see the actual SAR), and is mutually exclusive with the
  pre-baked `--imagery` overlay path. Tunables:
  `lazy_imagery_asset` (default `"GEC"`), `lazy_imagery_percentile`
  (default `(2.0, 98.0)`). Items whose asset href can't be resolved
  silently drop the button so we never ship a 400 KB CDN bundle for a
  map with zero clickable items.


- `umbra_py.timeline_map` / `save_timeline_map` and a matching `umbra
  map --timeline` CLI flag: render search results as a
  TimestampedGeoJson layer so Umbra's coverage accumulates beneath a
  play button + slider. Each footprint surfaces at its acquisition
  timestamp and keeps the same metadata popup as `footprint_map`.
  Tunables: `period` (slider step, ISO 8601 — `"PT1H"`/`"P1D"`/`"P7D"`
  match a day's / month's / year's search density), `duration` (how
  long each footprint stays visible — `None` accumulates, an ISO
  duration fades it back out), `auto_play`, `loop`, `transition_time`,
  and `geocode` / `geocode_zoom` (same Nominatim reverse-geocoding
  behavior as `footprint_map` — the resolved place name is baked into
  the popup before it ships into the TimestampedGeoJson payload, since
  the plugin renders properties verbatim). The CLI's existing
  `--geocode/--no-geocode` flag now flows through to `--timeline` too.
  `--timeline` is still rejected with `--imagery` (animating base64
  SAR rasters across the slider is a separate, larger lift) or with
  non-HTML output extensions.
- `UmbraCatalog.search(max_per_task=N)` (and `--max-per-task N` on `umbra
  search` / `umbra map`): cap how many items are yielded from any one
  `sar-data/tasks/<task>/` directory. Each task is repeated imaging of
  the same area, so `--max-per-task 1` swaps the usual "every revisit of
  a few sites" output for "one acquisition per distinct site" — much
  better diversity on a map.
- `umbra map --imagery-max-size N` to control how big each SAR overlay
  is read at. Default stays 1024 (modest HTML size); bump to 2048 or
  4096 for sharper overlays at quadratically larger filesizes. Useful
  when you want to zoom in on a single acquisition; remember SAR is
  inherently speckled, so higher resolutions also reveal more noise.
- A small 3-line satellite-orbit animation runs on stderr during
  `umbra map` and `umbra search` to show the catalog walk is making
  progress. Auto-suppressed when stderr isn't a TTY (CI, piped output)
  so captured logs stay clean.

### Changed
- **Breaking:** `UmbraCatalog.search` now walks Umbra's live data layout
  at `sar-data/tasks/<task>/[<uuid>/]<acquisition>/` (each acquisition has
  a `*.stac.v2.json` sidecar) instead of the legacy `stac/catalog.json`
  tree. The v1 tree is mostly metadata stubs that reference data Umbra
  never published — a 60-item v1 search returned exactly one downloadable
  item. The v2 walker enumerates the actual published acquisitions, so
  every item returned has resolvable asset URLs. Date pruning still works:
  acquisition directory names start with `YYYY-MM-DD-HH-MM-SS`, and the
  walker skips subtrees outside the requested `start` / `end` range.
  Provide a date range — without one the walker scans every published
  acquisition, which takes minutes.
- **Breaking:** `UmbraCatalog(root_url=...)` is gone. Configure the bucket
  via `UmbraCatalog(bucket=..., region=...)` if you ever need a non-default
  endpoint.

### Removed
- **Breaking:** `UmbraCatalog.available_task_ids()` and the
  `search(data_available_only=...)` flag, plus the matching
  `umbra search --available-only` / `umbra map --available-only` flags.
  They were stopgaps that filtered the v1 walk; the v2 walker only ever
  returns items whose data is published, so the filter is redundant.
- **Breaking:** `umbra_py.constants.DEFAULT_STAC_ROOT` (was never publicly
  re-exported).

### Added
- `umbra_py.viz` module for visualizing search results.
  - `item_to_feature`, `items_to_featurecollection`, `write_geojson`:
    convert items to GeoJSON for QGIS, leafmap, Earth Engine, geopandas,
    deck.gl, or any other tool that reads GeoJSON. The third coordinate of
    Umbra's 3D footprints is stripped so they render in 2D viewers.
  - `footprint_map`, `save_footprint_map`: build an interactive Folium map
    of one or more acquisitions, with auto-fit bounds and a metadata popup
    per item. Requires the `viz` extra.
  - `UmbraItem.to_geojson()` convenience method.
- `umbra map` CLI subcommand: search the catalog and write an interactive
  HTML map (`--out footprints.html`) or a GeoJSON FeatureCollection
  (`--out footprints.geojson`) to disk.
- `UmbraItem.asset_href` now resolves empty hrefs in recent Umbra STAC
  items. Umbra currently publishes every asset with `"href": ""` and
  expects consumers to reconstruct the URL from `umbra:task_id` and a
  rename mapping (`<base>_MM.tif` -> `<base>_GEC.tif`, etc.). Items with
  populated hrefs are returned unchanged, so older catalogs and the
  offline test fixture keep working. Unblocks live downloads and the SAR
  image overlay against 2024+ items.
- SAR image overlays on the Folium map.
  - `image_overlay(item)`: stream a downsampled preview of an item's GEC
    cloud-optimized GeoTIFF via HTTP range requests (no full download),
    apply a percentile contrast stretch to handle SAR's wide dynamic
    range, reproject to lat/lon if needed, and return a Folium
    `ImageOverlay` ready to drop onto any map.
  - `footprint_map(items, imagery=True)` / `umbra map --imagery`: one-call
    convenience that combines footprints with the SAR imagery. Each
    overlay is embedded as a base64 PNG so the resulting HTML file is
    self-contained — no tile server required.
  - The `viz` extra now also pulls in `rasterio` and `numpy` for the
    image-overlay path; folium-only users are unaffected.
  - `footprint_map(items, imagery=True)` is resilient to per-item
    failures: when one item's GEC asset is unreachable (404, network
    error, missing pixels), it emits a `UserWarning` and continues, so
    the remaining footprints and overlays still render. Umbra's public
    bucket has many STAC items whose binary data was never published,
    and the previous behavior crashed the whole map on the first one.
  - `image_overlay` now raises `AssetNotFoundError` with a clear message
    when the asset's URL can't be resolved (empty href, no
    `umbra:task_id`), instead of passing an empty URL to rasterio.
  - `footprint_map` now also draws a small always-visible circle marker
    at each footprint's centroid and a fixed-position legend in the
    top-right corner. Filled markers indicate items whose SAR imagery
    was rendered; outlined markers are footprint-only. This solves the
    "I have items, but I can't see any dots at world zoom" problem
    Umbra footprints are only a few km across.

## [0.1.0] - 2026-05-22

Initial release. Discovery + download core for Umbra's open SAR data.

### Added
- `UmbraCatalog`: search Umbra's static STAC catalog by bounding box, date
  range, and product type, with date-based pruning of the catalog tree so a
  constrained search only fetches relevant day catalogs.
- `UmbraItem`: lightweight dataclass over STAC items with metadata accessors
  (platform, product type, polarizations, resolution, incidence angle, …),
  bbox derivation from 3D geometry, and human-readable summaries.
- Anonymous HTTPS downloads (`download_url`, `download_asset`, `download_item`)
  with resume support and progress callbacks.
- `umbra` CLI with `search`, `info`, and `download` commands.
- Optional `convert` extra: `sicd_to_amplitude_geotiff` for inspection-quality
  amplitude extraction from SICD.
- Project scaffolding: Apache 2.0 license, packaging, CI, tests, and docs.

[Unreleased]: https://github.com/theminiverse/umbra-py/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/theminiverse/umbra-py/releases/tag/v0.1.0
