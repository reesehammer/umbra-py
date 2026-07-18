# ML chips & export

Cut scenes into fixed-size, georeferenced training tiles with per-chip metadata
(look angle, resolution, polarization, license) in a `.jsonl`, `.geojson`, or
stac-geoparquet manifest. Chipping needs the `[load]` extra; the GeoParquet
manifest and catalog export need `[export]`.

## Chips

::: umbra_py.chip_item

::: umbra_py.write_chips

::: umbra_py.write_manifest

::: umbra_py.write_manifest_parquet

::: umbra_py.ChipRecord

::: umbra_py.ChipDataset

::: umbra_py.CHIPPABLE_ASSETS

## Catalog export

::: umbra_py.export_geoparquet
