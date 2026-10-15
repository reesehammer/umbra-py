# Local index

`CatalogIndex` is a SQLite index over the catalog for near-instant, offline
repeat searches. Build it yourself (`umbra index build`), refresh it
incrementally (`umbra index update`), or fetch the prebuilt weekly snapshot
(`umbra index fetch`).

The baked SAR previews travel separately, as an opt-in `catalog.thumbs.db`
sidecar (`umbra index fetch-thumbnails`), so the metadata download stays small.
The weekly release stores that file as `catalog.thumbs.manifest.json` and
hash-named parts under GitHub's 2 GiB asset limit; fetch reassembles them.
A release that still has a single `catalog.thumbs.db` and no manifest is
downloaded as that file. `umbra index fetch-thumbnails --if-changed` will
not replace a local sidecar that already has more previews. The weekly job
caches its bake between runs and will not publish fewer matching previews
than the released sidecar, `/healthz`, or
`.github/thumbnail-preview-floor.json`, except to seed a release that has
no sidecar yet.

`umbra index build` quarantines source items that cannot be published: a
datetime before 2018-01-01 or more than a day ahead of the build, a missing
or invalid footprint, no public HTTPS asset href, or any href into the
private `prod-prod-processed` bucket. Duplicate ids are collapsed, keeping
the latest `updated` (or datetime), then the record with the most assets.
The dropped rows are listed in `umbra index coverage` and in
`index-validation.json`. They are omitted from the coverage percentage and
from the missing list; a sidecar that was never indexed still counts as
missing. `--validation` points the report at a saved `index-validation.json`. The weekly
[`coverage.md`](https://github.com/reesehammer/umbra-py/releases/download/catalog-index/coverage.md)
and
[`coverage.json`](https://github.com/reesehammer/umbra-py/releases/download/catalog-index/coverage.json)
on the `catalog-index` release are that report: indexed vs bucket acquisitions
by prefix (`sar-data/tasks/`, `sar-data/task-data/`, `open-data/`) and by
product, whether `pct_indexable` meets the 99% publication bar, each
quarantined row with its reason, and the duplicate count.
[`index-validation.json`](https://github.com/reesehammer/umbra-py/releases/download/catalog-index/index-validation.json)
is the same list from the build. `umbra index validate` re-checks the index and,
with `--parquet`, the geoparquet export, and exits non-zero if any violation
remains. Duplicate ids are counted in SQL and from the parquet `id` column;
rows are checked a batch at a time. A snapshot exported before asset schema
v2 has one `assets` field per filename (tens of thousands); v2 uses a fixed
set of product keys, and validation still batches so a v1 file is not loaded
all at once.

Asset schema v2 keys each product by a stable name instead of its filename.
The filename is `title` (when the asset had none) and `umbra-py:filename`.
A second file of the same product is `gec_2`, `gec_3`, … in filename order;
the canonical `*_GEC.tif` / `*_CPHD.cphd` / … name keeps the unsuffixed key.
`umbra-py:products` stays the uppercase type list (`GEC`, `SICD`, …), one
entry per type, lined up with those keys. The collection document and the
parquet file metadata (`stac-geoparquet` → `collections` → `umbra-open-data`)
both carry `umbra-py:asset_schema: 2`, and so does each item.

| Key | File |
| --- | --- |
| `gec` | `*_GEC.tif`, or a legacy bare `<stem>.tif` |
| `csi` | `*_CSI.tif` (color sub-aperture GeoTIFF) |
| `sidd` | `*_SIDD.nitf` |
| `csi_sidd` | `*_CSI-SIDD.nitf` (color sub-aperture SIDD) |
| `sicd` | `*_SICD.nitf` |
| `cphd` | `*_CPHD.cphd`, or a legacy bare `<stem>.cphd` |
| `metadata` | `*_METADATA.json` |
| `stac_metadata` | `*_STAC-METADATA.json` |
| `di_gif` | `*_DI-GIF.gif` detected-image preview |

`di_gif` is the only thumbnail-like product file in the open archive. It is
not the baked quicklook PNG (`catalog.thumbs.db`). A file that matches none
of these is `file` or `file_<ext>`. `asset_href("GEC")`, `asset_href("gec")`,
and `asset_href` of the filename all resolve the same primary asset.

The snapshot is what Umbra has published, rebuilt weekly. `umbra index info`
reports `built_at` and `end` (the newest plausible acquisition). As of
2026-10-14 that was a build on 2026-10-08 whose newest plausible acquisition
is 2026-09-13. Gaps of weeks are Umbra's publish cadence.

A datetime after `properties.created` — or after `built_at` when `created`
is missing — that is still inside the quarantine window is kept and flagged
`datetime_suspect`. It is omitted from `end`, from `rank_by=recency`, and
from other latest-pass selections. `datetime_suspect_count` is how many
rows are flagged. `quarantined_future_count` is how many source rows the
build removed for a datetime more than a day ahead (the upstream 2035–2036
items); those are not in the index, and the count is what keeps them visible.

`created` and `published` are nullable columns (schema version 6; version 5
is `datetime_suspect`). Opening a version-5 index adds the two columns and
keeps an existing flag. `created` is STAC `properties.created` when the sidecar
has one; legacy
`_METADATA.json` collects leave it empty. `published` is the sidecar's S3
`LastModified`, read from the `ListObjectsV2` page the crawl already
fetches. `created` minus `datetime` is the lag until Umbra's STAC
generation, not delivery latency, and `published` is when the object
appeared in the public bucket. Open data is a curated batch-published
subset — see [Limitations](../guides/limitations.md#open-data-timestamps-are-not-delivery-latency).

::: umbra_py.CatalogIndex

::: umbra_py.UpdateResult

::: umbra_py.default_index_path

::: umbra_py.default_thumbs_path

::: umbra_py.fetch_prebuilt_thumbnails
