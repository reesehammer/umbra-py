# ML chips & export

Cut scenes into fixed-size, georeferenced training tiles with per-chip metadata
(look angle, resolution, polarization, license) in a `.jsonl`, `.geojson`, or
stac-geoparquet manifest. Chipping needs the `[load]` extra; the GeoParquet
manifest and catalog export need `[export]`.

The complex products are chippable too: `asset="SICD"` geocodes each acquisition
through the [conversion pipeline](convert.md) — optionally terrain-orthorectified,
terrain-flattened and radiometrically calibrated (see `SicdConversion`) — and cuts
the identical tiles from the result, so a training set can carry a physical
backscatter coefficient from the full-resolution archive. That path needs the
`[convert]` extra alongside `[load]`.

`bbox=` (`umbra chips --clip-bbox`) chips one area of interest out of each
acquisition rather than the whole raster, numbering each chip's `row`/`col` from
that window's corner. On the complex path it is also the
[conversion's](convert.md) own clip, so each scene is geocoded over the site
rather than whole — which is where the cost of chipping the complex archive
actually lives. It is lon/lat whatever the raster's CRS is, matching
`to_stack(bbox=…)`.

## Chips

::: umbra_py.chip_item

::: umbra_py.write_chips

::: umbra_py.write_manifest

::: umbra_py.write_manifest_parquet

::: umbra_py.ChipRecord

::: umbra_py.ChipDataset

::: umbra_py.SicdConversion

::: umbra_py.CHIPPABLE_ASSETS

::: umbra_py.RASTER_ASSETS

::: umbra_py.COMPLEX_ASSETS

## Catalog export

::: umbra_py.export_geoparquet
