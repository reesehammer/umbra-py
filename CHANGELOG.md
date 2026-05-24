# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `UmbraCatalog.available_task_ids()` and `search(data_available_only=True)`:
  list the public bucket's top-level `sar-data/tasks/<UUID>/` directories
  and prune the v1 STAC walk to items whose `umbra:task_id` actually has
  data published. Umbra's v1 STAC catalog is mostly metadata stubs (a
  60-item search returned only 1 with reachable data); this filter lets
  users get a map / list of only the items they can actually download.
  Costs one extra paginated S3 listing on the first call (cached).
- `umbra search --available-only` and `umbra map --available-only` CLI
  flags wired through to the same filter.
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
