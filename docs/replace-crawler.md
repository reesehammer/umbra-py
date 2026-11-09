# Replace your crawler

Umbra's open data is a public bucket
(`umbra-open-data-catalog`, prefixes `sar-data/tasks/`,
`sar-data/task-data/`, and `open-data/`) full of STAC sidecars. Listing that
tree yourself is the crawl this project already runs once a week. The result
is a STAC API and one Parquet file. Both answer "which GEC scenes cover this
box?" without a bucket walk.

The community API is [https://api.umbra-py.space/](https://api.umbra-py.space/).
`GET` and `POST /search` are STAC API Item Search. The only collection is
[`umbra-open-data`](https://api.umbra-py.space/collections/umbra-open-data).

## Why the index

**Freshness.** The snapshot is what Umbra has published in the open bucket,
rebuilt on a weekly cadence. Open data is a curated batch, so the newest
acquisition in the index lags the calendar. `GET /healthz` reports `built_at`
and `items`. The collection's temporal extent is the same window. On
2026-10-15 the API was serving a snapshot built that day: 17,205 items,
acquisitions from 2023-01-16 through 2026-09-13.

**Weekly rebuild.** The
[Publish catalog index](https://github.com/reesehammer/umbra-py/actions/workflows/publish-index.yml)
workflow runs Mondays at 06:17 UTC and replaces the rolling GitHub release
[`catalog-index`](https://github.com/reesehammer/umbra-py/releases/tag/catalog-index).
That release holds `catalog.db`, `umbra-open-data.parquet`, the validation
report, and `coverage.json`.

**Validation.** `umbra index build` drops rows that cannot be published:
duplicate ids (latest `updated`, otherwise datetime, then the most assets),
a datetime before 2018-01-01 or more than a day ahead, a missing or invalid
footprint, no public HTTPS asset href, or any href into the private
`prod-prod-processed` bucket. The dropped rows are
[`index-validation.json`](https://github.com/reesehammer/umbra-py/releases/download/catalog-index/index-validation.json)
on the release. The 2026-10-15 report kept 17,205 rows, quarantined 8, dropped
747 duplicate ids, and recorded no remaining violations.

**Coverage.**
[`coverage.json`](https://github.com/reesehammer/umbra-py/releases/download/catalog-index/coverage.json)
measures the bucket against the index, by prefix (`sar-data/tasks/`,
`sar-data/task-data/`, `open-data/`), sidecar layout, and product. The
2026-10-15 report indexed 17,205 of 17,960 indexable sidecars (95.8%).
Sidecars the walk did not take are listed there; quarantined rows are not
counted as missing.

`/healthz` `built_at` and the release notes are the live copy of these
numbers. The dates above are the snapshot this page was checked against.

## Asset keys

Asset schema v2 keys each product by a stable name. The original filename is
`umbra-py:filename` on the asset (and `title`, when the asset had none). A
second file of the same product is `gec_2`, `gec_3`, … in filename order.
The canonical `*_GEC.tif` / `*_CPHD.cphd` / … name keeps the unsuffixed key.
`umbra-py:asset_schema` is `2` on the collection, on each item, and (once the
parquet is rebuilt) in the Parquet file metadata under `stac-geoparquet` →
`collections` → `umbra-open-data`.

| Key | File |
| --- | --- |
| `gec` | `*_GEC.tif`, or a legacy bare `<stem>.tif` |
| `csi` | `*_CSI.tif` |
| `sidd` | `*_SIDD.nitf` |
| `csi_sidd` | `*_CSI-SIDD.nitf` |
| `sicd` | `*_SICD.nitf` |
| `cphd` | `*_CPHD.cphd`, or a legacy bare `<stem>.cphd` |
| `metadata` | `*_METADATA.json` |
| `stac_metadata` | `*_STAC-METADATA.json` |
| `di_gif` | `*_DI-GIF.gif` |

The API already serves these keys: `/search` rewrites an older index row on
the way out. The `umbra-open-data.parquet` asset currently on `catalog-index`
is still the previous export. Its `assets` column is a struct with one field
per filename, and its `stac-geoparquet` metadata is `{"version": "1.0.0"}`
with no `umbra-py:asset_schema`. The DuckDB program below is the v2 shape.
It starts working when the next weekly rebuild publishes the file. Until
that rebuild, `/search` is the interface that already returns v2 keys.

A file that matches none of the keys is `file` or `file_<ext>`. The longer
note, including how `umbra-py:products` lines up with the keys, is
[Local index](reference/index.md).

## The same search, three ways

Each program lists GEC acquisitions whose footprint intersects longitude
85.49–85.56 and latitude 28.26–28.32, on or after 2026-09-01, and prints the
GEC href. That box is the September 2026 Nepal disaster coverage, which the
2026-10-15 snapshot actually contains. `limit` is a page of 5. The index
orders by task, then acquisition date, then datetime, then id — a page is the
first matches in that order, which inside one bbox is the earliest passes.

The parameters are the ones `/search` implements:

- `bbox` — `minlon,minlat,maxlon,maxlat` (GET) or a four-number array (POST).
- `datetime` — `2026-09-01/..` is an open interval from that date. A single
  date matches that day. The index filters on the acquisition date.
- `collections` — `umbra-open-data`. Any other id is a 400.
- `product_types` — `GEC` (comma-separated on GET). Case-insensitive. This is
  the product type, uppercase, which is a different string from the asset
  key `gec`.
- `limit` — page size. Default 10. This host returns at most 500 items per
  page (`next` still carries `token`, the offset). A self-hosted server
  without `--public` accepts up to 10000.

The programs are the files the daily canary runs. They live in
[`docs/examples/replace_crawler/`](https://github.com/reesehammer/umbra-py/tree/main/docs/examples/replace_crawler).

### Plain HTTP

`GET /search` returns a GeoJSON FeatureCollection. The GEC href is
`features[].assets.gec.href`.

```python
--8<-- "docs/examples/replace_crawler/get_search.py"
```

### pystac-client

`Client.search` speaks POST `/search`. The product filter is the STAC Query
extension body this API accepts, `{"product_types": {"in": ["GEC"]}}`,
passed as `query=`. `max_items` stops the client after five items; `limit`
is the page size it requests.

```python
--8<-- "docs/examples/replace_crawler/pystac_search.py"
```

```bash
pip install pystac-client
```

### DuckDB

The Parquet file is stac-geoparquet on the `catalog-index` release:

[https://github.com/reesehammer/umbra-py/releases/download/catalog-index/umbra-open-data.parquet](https://github.com/reesehammer/umbra-py/releases/download/catalog-index/umbra-open-data.parquet)

`bbox` is a struct (`xmin`, `ymin`, `xmax`, `ymax`). Overlap with a query
box is `bbox.xmax >= minlon AND bbox.xmin <= maxlon` (and the same for y).
`datetime` is `TIMESTAMPTZ`. Comparing it with `CAST(datetime AS DATE)`
keeps the filter in SQL. `assets` is one struct whose fields are the v2
keys. Each field is itself a struct: `href`, `type`, `title`, `roles`, and
`umbra-py:filename`. The href is `assets.gec.href`. The original filename is
`assets.gec."umbra-py:filename"` (quote the field; the name contains a
colon). `httpfs` reads the release URL. GitHub redirects that stable
`/releases/download/catalog-index/` path to the current asset.

```python
--8<-- "docs/examples/replace_crawler/duckdb_gec.py"
```

```bash
pip install duckdb
```

On the schema-v1 file the `assets` fields are filenames, so `assets.gec` is
absent and this query errors. The daily canary reads the file metadata
first and skips this program until `umbra-py:asset_schema` is 2, or a field
named `gec` exists. The HTTP and `pystac-client` programs run either way.

## One copy of a republished collect

The same collect is often published twice: once under `sar-data/task-data/`
and once under `sar-data/tasks/<campaign>/`. Those are different STAC ids.
The index keeps both. After the next rebuild, each item has
`umbra-py:canonical`. `true` is the copy to keep (newest `created`, then
processor version, datetime, product count, a `task-data` href, then the
lowest id — see [Local index](reference/index.md#republished-captures)).
Items that are not a republished pair are canonical too, so the filter is
one boolean.

These three programs are the same Nepal search limited to that copy. They
are not the daily canary: the canary hits the API and parquet that are
already published, and this filter is on the snapshot this change builds.
`GET /search?canonical=true` is the HTTP form. `pystac-client` sends it as
a query-extension predicate. DuckDB quotes `"umbra-py:canonical"` because
the column name contains a colon. The column is absent until the weekly
parquet is rebuilt from an index that has been linked.

```python
--8<-- "docs/examples/replace_crawler/canonical_get.py"
```

```python
--8<-- "docs/examples/replace_crawler/canonical_pystac.py"
```

```python
--8<-- "docs/examples/replace_crawler/canonical_duckdb.py"
```

Imagery `href`s point at Umbra's public bucket. Attribute Umbra under
CC BY 4.0. The API rate limit is 120 requests per minute per client.
