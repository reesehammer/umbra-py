# Local index

`CatalogIndex` is a SQLite index over the catalog for near-instant, offline
repeat searches. Build it yourself (`umbra index build`), refresh it
incrementally (`umbra index update`), or fetch the prebuilt weekly snapshot
(`umbra index fetch`).

The baked SAR previews travel separately, as an opt-in `catalog.thumbs.db`
sidecar (`umbra index fetch-thumbnails`), so the metadata download stays small.

`umbra index build` quarantines source items that cannot be published: a
datetime before 2018-01-01 or more than a day ahead of the build, a missing
or invalid footprint, no public HTTPS asset href, or any href into the
private `prod-prod-processed` bucket. Duplicate ids are collapsed, keeping
the latest `updated` (or datetime), then the record with the most assets.
The dropped rows are listed in `umbra index coverage` and in
`index-validation.json`. `umbra index validate` re-checks the index and,
with `--parquet`, the geoparquet export, and exits non-zero if any violation
remains.

::: umbra_py.CatalogIndex

::: umbra_py.UpdateResult

::: umbra_py.default_index_path

::: umbra_py.default_thumbs_path

::: umbra_py.fetch_prebuilt_thumbnails
