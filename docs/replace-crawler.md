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
rebuilt on a weekly cadence. Open data is a curated, sparse site list, not
global recent imagery, so the newest acquisition in the index lags the
calendar and a search for the last few days is often empty. `GET /healthz`
reports `built_at` and `items`. The collection's temporal extent is the same
window. On 2026-11-23 the API was serving the snapshot built 2026-11-16:
17,230 items, acquisitions from 2023-01-16 through 2026-11-12, 14,400 of
them with a baked thumbnail.

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
on the release. The 2026-11-16 report kept 17,230 rows, quarantined 8, dropped
747 duplicate ids, and recorded no remaining violations.

**Coverage.**
[`coverage.json`](https://github.com/reesehammer/umbra-py/releases/download/catalog-index/coverage.json)
measures the bucket against the index, by prefix (`sar-data/tasks/`,
`sar-data/task-data/`, `open-data/`), sidecar layout, and product. The
2026-11-16 report indexes 17,230 of 17,994 bucket acquisitions (`pct`
95.75). `pct_indexable` is 100.0: 17,985 have a sidecar, 755 of those are
omitted as quarantined rows or duplicate ids, and the other 17,230 are
indexed. The missing list is empty.

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
2026-11-16 snapshot contains. `limit` is a page of 5. The index
orders by task, then acquisition date, then datetime, then id — a page is the
first matches in that order, which inside one bbox is the earliest passes.

The parameters are the ones `/search` implements:

- `bbox` — `minlon,minlat,maxlon,maxlat` (GET) or a four-number array (POST).
- `datetime` — `2026-09-01/..` is an open interval from that date. A single
  date matches that day. The index filters on the acquisition date.
- `collections` — `umbra-open-data`. Any other id is a 400 that names
  `umbra-open-data`. `umbra-gec` is not a collection: GEC is `product_types`.
- `product_types` — `GEC` (comma-separated on GET). Case-insensitive. This is
  the product type, uppercase, which is a different string from the asset
  key `gec`.
- `intersects` — URL-encoded GeoJSON `Point`, `Polygon`, or `MultiPolygon`.
  A point selects footprints that contain it. Malformed JSON or any other
  geometry type is a 400. Mutually exclusive with `bbox`.
- `limit` — page size. Default 10. This host returns at most 500 items per
  page (`next` still carries `token`, the offset). A self-hosted server
  without `--public` accepts up to 10000.
- `area` — a task-name substring (case-insensitive). A hit is the same
  search as before. If it matches no Umbra task and `bbox` / `intersects`
  are omitted, `/search` and `/sites` geocode it as a place and search that
  footprint. The document then includes `matched_by` (`geocode`),
  `resolved_place`, and `resolved_bbox`. A name Nominatim does not know is
  HTTP 200 with an empty result and `hint` `no task named X; try place= or bbox=`.
  When geocoding is busy or Nominatim is unreachable, the same empty HTTP 200
  carries `hint` `no task named X; geocoding was unavailable`. The next link
  still carries `area`; the resolved box is reused from the in-process cache.
  `GET /collections/.../items` does not do this fallback.

A point over the same Nepal coverage, URL-encoded the way a GET query has to
send GeoJSON (`urllib.parse.urlencode` turns the JSON into the `intersects`
parameter). `curl --get --data-urlencode` does the same encoding. A polygon
or multipolygon replaces the `Point` object; the rest of the URL stays.

```python
import json
from urllib.parse import urlencode
import urllib.request

point = {"type": "Point", "coordinates": [85.52, 28.29]}
url = "https://api.umbra-py.space/search?" + urlencode(
    {"intersects": json.dumps(point), "product_types": "GEC", "limit": 1}
)
print(json.load(urllib.request.urlopen(url))["features"][0]["id"])
```

```bash
curl -sG "https://api.umbra-py.space/search" \
  --data-urlencode 'intersects={"type":"Point","coordinates":[85.52,28.29]}' \
  --data-urlencode "product_types=GEC" \
  --data-urlencode "limit=1"
```

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

## From a browser

Open data is published in batches. As of the snapshot built 2026-11-16, the
newest acquisition is 2026-11-12. The archive is a curated, sparse site list,
not global recent imagery, so a search for the last few days is often empty
even when `/healthz` is fresh. Read the collection `extent.temporal` (on
[`umbra-open-data`](https://api.umbra-py.space/collections/umbra-open-data))
for the acquisition window, or [`/healthz`](https://api.umbra-py.space/healthz)
`built_at` for when that snapshot was built, instead of assuming the archive
runs up to today.

The same Nepal search is a `GET` of
`https://api.umbra-py.space/search`. A `GET` with no custom headers does not
preflight. Send `collections=umbra-open-data` and either `bbox`
(`minlon,minlat,maxlon,maxlat`) or `intersects` (a URL-encoded GeoJSON
`Point`, `Polygon`, or `MultiPolygon`). The two are mutually exclusive: sending both is a 400. `datetime` is an interval (`2026-09-01/..` is open at
the end; a single date matches that day). The index filters on the
acquisition date. `product_types=GEC` is the product type, case-insensitive.
The feature's asset key is `gec`. `canonical=true` keeps the canonical copy,
including items that are not a republished pair. `limit`
is the page size (default 10; this host returns at most 500). Results stay
in the index order above, so the first page of one window is the earliest
passes in it.

`URLSearchParams` encodes `intersects` the way the `urlencode` example above
does. For the box instead of the point, drop `intersects` and set
`bbox` to `85.49,28.26,85.56,28.32`.

`features[].assets.thumbnail.href` is the baked PNG,
`https://api.umbra-py.space/artifacts/thumbnail/{id}.png`, when this snapshot
has one. The 2026-11-16 index has a bake for 14,400 of 17,230 items. A
missing `thumbnail` asset, or HTTP 404 from that URL, means skip the preview.
The PNG response sends `Cache-Control: public, max-age=86400`. A
cross-origin fetch also receives `Access-Control-Allow-Origin: *` (the same
CORS policy as `/search`; see [CORS](hosted-limits.md#cors)). Each thumbnail
GET counts toward the 120 requests per minute cap. It does not take a
catalog-read slot, so several images can load together; page modestly and
let the one-day cache do the rest. Build the element with `createElement`,
and put any label in `textContent`.

```js
const params = new URLSearchParams({
  collections: "umbra-open-data",
  intersects: JSON.stringify({ type: "Point", coordinates: [85.52, 28.29] }),
  datetime: "2026-09-01/..",
  product_types: "GEC",
  canonical: "true",
  limit: "5",
});
const page = await fetch(`https://api.umbra-py.space/search?${params}`).then(
  (response) => response.json(),
);
for (const item of page.features) {
  // Footprint positions are often [lon, lat, height]. Use the first two.
  const [lon, lat] = item.geometry.coordinates[0][0];
  console.log(item.properties.datetime, lon, lat, item.assets.gec?.href);
  const thumb = item.assets.thumbnail?.href;
  if (thumb) {
    const figure = document.createElement("figure");
    const img = document.createElement("img");
    img.alt = "";
    img.src = thumb;
    const caption = document.createElement("figcaption");
    caption.textContent = item.id;
    img.addEventListener("error", () => figure.remove());
    figure.append(img, caption);
    document.body.append(figure);
  }
}
const next = page.links.find((link) => link.rel === "next");
if (next) {
  // next.method is "GET". next.href already includes token= (the offset).
  const more = await fetch(next.href).then((response) => response.json());
  console.log(more.features.length);
}
```

A `links` entry with `rel` `next` and `method` `GET` is the next page. Follow
its `href`; that URL already carries `token`. Footprint positions are often
`[lon, lat, height]`; use the first two. `features[].assets.gec.href` is a
public COG on Umbra's bucket. Attribute Umbra, CC BY 4.0
(`X-Umbra-Attribution`, and `Link: rel="license"`). The rate limit is 120
requests per minute per client, with a short burst so a handful of parallel
calls can land together. Call `/search` one at a time, or on HTTP 429 wait
out `Retry-After` before retrying. Do not page the whole catalog this way. For
a bulk copy, `umbra index fetch` downloads the weekly
[`catalog.db`](https://github.com/reesehammer/umbra-py/releases/download/catalog-index/catalog.db)
([Bulk use and outages](hosted-limits.md#bulk-use-and-outages)).

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
CC BY 4.0. The API rate limit is 120 requests per minute per client, with a
short burst. On HTTP 429, back off and honor `Retry-After`, or run
`umbra index fetch` and search locally.

## Use umbra-py as a provider backend

A tool that already searches several sensors through one adapter,
`search(bbox, start, end)` returning scenes, can call this API with plain
`requests`. `bbox` is west, south, east, north. `start` and `end` are
timezone-aware datetimes; the query sends them as UTC in `datetime=start/end`.
The collection is `umbra-open-data`. `product_types=GEC` is the geocoded
GeoTIFF Umbra publishes. `canonical=true` keeps one copy of a republished
collect. `limit` is the page size; this host clamps it at 500.

The same collect is often published twice, under `sar-data/tasks/...` and
under `sar-data/task-data/...`, as two STAC ids. `canonical=true` keeps one
of them. An item that is not a republished pair is canonical too. How the
index picks that copy is
[One copy of a republished collect](#one-copy-of-a-republished-collect).

Checked 2026-11-23: `bbox=159.1,-31.9,159.4,-31.6` and
`datetime=2025-06-22T00:00:00Z/2025-06-23T00:00:00Z` with `product_types=GEC`
returns 2 features, one under each prefix. `canonical=true` keeps the
`sar-data/task-data/` copy.

`assets.gec.href` on this API is already a public HTTPS URL on
`umbra-open-data-catalog`. Umbra's static sidecar can leave that href empty,
or point it at a private bucket. The index stores the public URL, and
`/search` returns that href. Pass the whole feature through. A UI that reads
`assets.thumbnail.href` then finds the baked PNG when the item has one.

`/search` accepts `polarizations` (comma-separated, for example `HH`). An
item is kept when it exposes at least one of those values. The same list is
`properties["sar:polarizations"]` if you filter a page you already hold.
Instrument mode is not a query parameter. It is
`properties["sar:instrument_mode"]` on the feature (`SPOTLIGHT` on the pair
above). Filter that in Python. Sending `instrument_mode` on the query string
does not filter: the parameter is not read.

Follow the `links` entry whose `rel` is `next`. On a GET search, `method` is
`GET` and `href` already includes the query, with `token` as the offset.
Request that URL with no extra parameters. On HTTP 429, wait the number of
seconds in `Retry-After` before the next request. The adapter below raises
with that header so the caller can wait and try again. The host is
best effort and has no SLA: 120 requests per minute sustained, per client IP,
with a burst of 16. Do not page the whole catalog this way. Attribute Umbra,
CC BY 4.0 (`X-Umbra-Attribution`). The contract is
[Hosted API limits](hosted-limits.md).

```python
from datetime import timezone

import requests

SEARCH = "https://api.umbra-py.space/search"


def search_umbra_gec(bbox, start, end, max_results=10, session=None):
    session = session or requests.Session()
    window = (
        f"{start.astimezone(timezone.utc):%Y-%m-%dT%H:%M:%SZ}"
        f"/{end.astimezone(timezone.utc):%Y-%m-%dT%H:%M:%SZ}"
    )
    params = {
        "bbox": ",".join(map(str, bbox)),
        "datetime": window,
        "collections": "umbra-open-data",
        "product_types": "GEC",
        "canonical": "true",
        "limit": min(max_results, 500),
    }
    url, scenes = SEARCH, []
    while url and len(scenes) < max_results:
        response = session.get(url, params=params, timeout=30)
        params = None  # the next href already carries the query
        if response.status_code == 429:
            wait = response.headers.get("Retry-After")
            raise RuntimeError(f"rate limited, retry after {wait}")
        response.raise_for_status()
        page = response.json()
        for item in page.get("features", []):
            href = (item.get("assets") or {}).get("gec", {}).get("href")
            if href:
                scenes.append(item)
                if len(scenes) >= max_results:
                    break
        url = next(
            (link["href"] for link in page.get("links", []) if link.get("rel") == "next"),
            None,
        )
    return scenes
```

For a bulk or offline search, skip the API. `umbra index fetch` downloads
the weekly snapshot. `CatalogIndex.from_release()` is that same download and
opens the file; the returned index is a context manager. `search` takes
`bbox` as four numbers, `start` and `end` as inclusive acquisition dates,
`product_types=["GEC"]`, and `canonical=True`.
`download_item(item, dest_dir=..., assets=["GEC"])` writes the primary GEC.
That search does not call the host, so it does not use the rate cap. The
CLI, the output directory, and a stdlib GET of the same hrefs are
[Download the GEC GeoTIFFs](#download-the-gec-geotiffs).

```python
from umbra_py import CatalogIndex, download_item

with CatalogIndex.from_release() as index:
    for item in index.search(
        bbox=(159.1, -31.9, 159.4, -31.6),
        start="2025-06-22",
        end="2025-06-23",
        product_types=["GEC"],
        canonical=True,
    ):
        download_item(item, dest_dir="gec", assets=["GEC"])
```

## Download the GEC GeoTIFFs

The searches above name scenes. The file is the `gec` asset: the geocoded
cloud-optimized GeoTIFF Umbra publishes. Fetch `assets.gec.href` from the
public bucket (`umbra-open-data-catalog`). `/search` returns that href. The
bytes are the bucket object.

The product filter is `GEC`. The asset key is `gec`. A second GEC on the
same item is `gec_2`; the commands below take the primary file.

The box in the programs above (longitude 85.49–85.56, latitude 28.26–28.32,
acquisition dates 2026-09-01 through 2026-09-30) is the worked example.

### CLI and Python

`umbra index fetch` downloads the weekly
[`catalog.db`](https://github.com/reesehammer/umbra-py/releases/download/catalog-index/catalog.db)
to `$UMBRA_INDEX_DB` or `~/.cache/umbra-py/catalog.db`. `umbra search --local`
reads that file, so listing an area does not call the hosted API and does
not walk the bucket. `umbra download` then takes one sidecar URL (the `url`
line of the search output), a GEC filename, or an acquisition stem.
`--asset GEC` is the product type, any case, and selects the primary GEC.
`--dest` is the output directory, which has to exist already.

```bash
mkdir -p gec
umbra index fetch
umbra search --local \
  --bbox 85.49,28.26,85.56,28.32 \
  --start 2026-09-01 --end 2026-09-30 \
  --product GEC --limit 1
umbra download "<url>" --asset GEC --dest gec
```

`--bbox` is `min_lon,min_lat,max_lon,max_lat`. `--start` and `--end` are
inclusive acquisition dates (`YYYY-MM-DD`, or the relative forms
`umbra search --help` lists). `--product` is repeatable. `--limit` defaults
to 20. Pass each `url` line to `umbra download`.

The same filters are keyword arguments of `CatalogIndex.search` (the same
parameters as `UmbraCatalog.search`). `bbox` is four numbers. `start` and
`end` are dates. `product_types` is a list, `["GEC"]`. `canonical=True`
keeps one copy of a republished collect, the same filter as
`canonical=true` on `/search`. `download_item` writes the file;
`assets=["GEC"]` is that product type.

```python
from pathlib import Path

from umbra_py import CatalogIndex, download_item

Path("gec").mkdir(exist_ok=True)
with CatalogIndex.from_release() as index:
    for item in index.search(
        bbox=(85.49, 28.26, 85.56, 28.32),
        start="2026-09-01",
        end="2026-09-30",
        product_types=["GEC"],
        canonical=True,
        limit=1,
    ):
        path = download_item(item, dest_dir="gec", assets=["GEC"])[0]
        print(item.id, path)
```

`limit=1` stops after one scene. Omit `limit` to take every match in the
index. `CatalogIndex.from_release()` downloads the same `catalog.db` as
`umbra index fetch`. `UmbraCatalog().search` accepts those same arguments
and lists the live bucket; the snapshot is the bulk path.

`umbra search` prints every indexed copy. The `url` line is the sidecar to
pass to `umbra download`.

### Plain HTTP

`GET /search` is STAC API Item Search. Follow the `links` entry whose `rel`
is `next`: `method` is `GET`, and `href` already includes `token`. Then GET
`features[].assets.gec.href`.

The hosted API allows **120 requests per minute**, sustained, per client,
with a short burst (a token bucket: `PUBLIC_RATE_LIMIT` refilled across the
60-second window, `PUBLIC_RATE_BURST` tokens at once, on
`umbra serve --public`). Send `/search` one at a time, or on HTTP 429 back
off and honor `Retry-After`. The contract is
[Rate limit](hosted-limits.md#rate-limit). Each `/search` page counts. A GET
of the GeoTIFF is a request to the bucket. This host returns at most 500
items per page, so a long list stays a small number of `/search` calls. Stay
under 120 requests per minute. A pull the size of the catalog belongs on
`umbra index fetch` and `umbra search --local`.

```python
import json
from pathlib import Path
from urllib.request import urlopen

url = (
    "https://api.umbra-py.space/search"
    "?bbox=85.49,28.26,85.56,28.32"
    "&datetime=2026-09-01/2026-09-30"
    "&collections=umbra-open-data"
    "&product_types=GEC"
    "&canonical=true"
    "&limit=2"
)
hrefs = []
while url:
    with urlopen(url) as resp:
        page = json.load(resp)
    for feature in page["features"]:
        hrefs.append(feature["assets"]["gec"]["href"])
    url = next(
        (link["href"] for link in page["links"] if link.get("rel") == "next"),
        None,
    )

# Writes the first COG. Iterate hrefs to fetch every scene in the window.
href = hrefs[0]
dest = Path("gec") / href.rsplit("/", 1)[-1]
dest.parent.mkdir(exist_ok=True)
with urlopen(href) as remote, dest.open("wb") as out:
    while True:
        chunk = remote.read(1024 * 1024)
        if not chunk:
            break
        out.write(chunk)
print(f"{len(hrefs)} GEC scene(s); wrote {dest.name} ({dest.stat().st_size} bytes)")
```

`datetime=2026-09-01/2026-09-30` is a closed interval. The index filters on
the acquisition date, which is what `--start` and `--end` filter on.
`product_types=GEC` is the product type. `canonical=true` keeps one copy of
a republished collect. `limit=2` is the page size, so this box's `next` link
is followed until the window is listed. The snippet then writes the first
COG.

Imagery is Umbra open data, licensed CC BY 4.0. Keep the line the library
stamps on its outputs: *Contains Umbra open data, licensed under CC BY 4.0.*
The copyright notice for adapted materials is on [Citing](citing.md). Every
API response also sends that line as `X-Umbra-Attribution`, with
`Link: rel="license"`.
