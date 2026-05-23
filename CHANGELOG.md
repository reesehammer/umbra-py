# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
