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

::: umbra_py.CatalogIndex

::: umbra_py.UpdateResult

::: umbra_py.default_index_path

::: umbra_py.default_thumbs_path

::: umbra_py.fetch_prebuilt_thumbnails
