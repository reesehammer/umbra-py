# Limitations

Honest bounds on what v0.1.0 will and will not do. Silent errors are easy in
SAR; this page is the list of things the library refuses, approximates, or
has not yet checked on real products.

## Not an InSAR toolbox

`SICD` and `CPHD` are classified and downloadable. That is the whole
phase-preserving path in umbra-py: **search, size-check, download, stop.**

`CPHD` is compensated phase history for **formation elsewhere** (a GPU
backprojector, a custom former, sarpy). umbra-py does not form an image
from it and does not run backprojection. `umbra convert` does not read
CPHD.

`umbra convert` detects **amplitude** from SICD and writes a geocoded
GeoTIFF — the phase is discarded. There is no interferogram, no
coherence, no perpendicular-baseline filter, and no PFA → range-Doppler
rewrite.

Open Umbra SICDs are **spotlight / RGAZIM / Polar Format (PFA)**, not
Capella-style RGZERO stripmap. A processor that only ingests RGZERO should
reject them. If you need the complex pixels, download the SICD (or CPHD)
and hand it to [sarpy](https://github.com/ngageoint/sarpy) or another
downstream tool — see [Complex products (SICD/CPHD)](complex-downstream.md).

## Radiometry on the open archive

Umbra's **open** SICDs generally ship without a `Radiometric` block.

- `--calibrate sigma0|beta0|gamma0|rcs` **refuses** when the product cannot
  support it. It does not invent a scale factor.
- `--noise-model measured` **refuses** when there is no `ABSOLUTE` `NoisePoly`.
- `--noise-model estimated` / `estimated-range` infer a floor from the
  scene's dark tail. The arithmetic is tested on synthetic data. They have
  **not** been compared to a real product that carries a measured floor
  (that needs a Canopy scene or equivalent).

A published `GEC` is already a geocoded GeoTIFF. Its pixels are relative
amplitude, not a calibrated backscatter coefficient. Open GEC is 8-bit
display-scaled data with nodata 0 and a floor at DN 1. Mask nodata 0 before
a threshold or a statistic, and use SICD or CSI when dynamic range matters.

## Search is a crawl unless you fetch the index

There is no STAC API on the open bucket. `UmbraCatalog.search` paginates S3
listings and is slow on an unconstrained query — that is why
`umbra index fetch` / `CatalogIndex.from_release()` exist, and why the
community `umbra serve --public` host exists (see [Deploy](../deploy.md)).
Prefer `--local` for anything you will run more than once.

`area=` is a task-directory name, not a geocoded place. `--place` (CLI only)
geocodes via Nominatim to a **rectangle**, so it can include nearby ground
outside the named place.

The index is what Umbra has published, rebuilt weekly. `umbra index info`
(and `index_stats`) report `built_at` and `end`, the newest plausible
acquisition. As of 2026-10-14 the published snapshot was built 2026-10-08
and its newest plausible acquisition is 2026-09-13 (Nepal disaster coverage).
Umbra open data arrives in irregular batches. A gap of weeks between that
acquisition and today is normal, and it is not an umbra-py lag.

A few upstream items carry impossible future dates (a `task-data` collect
dated 2036-07-24, and others in 2035). `umbra index build` quarantines a
datetime before 2018-01-01 or more than a day after the build and reports
how many in `quarantined_future_count` — those rows are not in the index.
A datetime that is after the item's own `properties.created`, or after
`built_at` when `created` is missing, but still inside that window is kept,
flagged `datetime_suspect`, and left out of `end`, `rank_by=recency`, and
other latest-pass selections. The datetime is not rewritten.

## Open-data timestamps are not delivery latency

The catalog index (schema version 6) stores two times beside `datetime`,
the acquisition time:

- `created` is STAC `properties.created` when the sidecar has it. Legacy
  `_METADATA.json` collects do not. `created` minus `datetime` is the lag
  until Umbra generated the STAC record, not tasking delivery latency.
- `published` is the S3 `LastModified` of the sidecar object. The crawl
  already sees it in `ListObjectsV2`, so filling it costs no extra request.
  It is when that object appeared in the public bucket.

Open data is a curated, batch-published subset. A task's collects can land
in the bucket together, weeks after they were acquired. Neither field is a
commercial delivery-latency measurement. `umbra serve` passes `created`
through as `properties.created` and `published` as
`properties["umbra-py:published"]`.

## Canopy is the same interface, not a live-verified client

`UmbraCatalog(token=...)` / `umbra search --token` posts to Canopy's STAC
API. The client is built to the STAC API standard and tested against a
mock. Request/response shapes have not been confirmed against the live
API; `product_types` and `area` are still applied client-side.

## Convert is a toolkit, not MultiRTC

Terrain orthorectification, four RTC models (including a plane-wave
image-space "facet" approximation of Small 2011), speckle filters, and
clipping all ship. They are exercised offline with fakes and synthetic
arrays. They have not been cross-checked against
[MultiRTC](https://github.com/MultiSAR/MultiRTC). Over extreme relief, or
when you need a survey-grade RTC product, compare before you publish numbers.

## Community STAC API, not an Umbra product

Umbra's open catalog is still a static tree with no official search API.
`umbra serve --public` ([https://api.umbra-py.space/](https://api.umbra-py.space/))
is an unofficial community instance: STAC search + MCP on one URL,
artifacts off so this host does not proxy rasters, a per-client rate
limit, and CC-BY license headers.
Asset `href`s point at Umbra's public bucket — stream them yourself.
Do not set a Canopy token or a model key on that instance. See
[Deploy](../deploy.md).

## AI is opt-in and never implicit

`umbra ask`, `describe`, `embed`, and `change --narrate` call a model only
when you invoke them and have configured a key. Model output is re-validated
or provenance-stamped; it never becomes a coordinate, URL, or filter on its
own. The core search / download / render path never calls a model.

## What to read next

- [Complex products (SICD/CPHD)](complex-downstream.md) — phase-preserving
  handoff: index → search → HEAD → download → stop.
- [Quickstart](../quickstart.md) — the five-minute path (GEC / convert).
- [Install](../install.md) — which extra you need.
- [`.github/TODO.md`](https://github.com/reesehammer/umbra-py/blob/main/.github/TODO.md)
  — follow-ons that were scoped out of merged PRs on purpose.
