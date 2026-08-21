# Limitations

Honest bounds on what v0.1.0 will and will not do. Silent errors are easy in
SAR; this page is the list of things the library refuses, approximates, or
has not yet checked on real products.

## Not an InSAR toolbox

`SICD` and `CPHD` are classified and downloadable. `umbra convert` detects
**amplitude** and writes a geocoded GeoTIFF — the phase is discarded. There
is no interferogram, no coherence, no perpendicular-baseline filter, and no
CPHD image formation. If you need phase-preserving work, download the SICD
and use [sarpy](https://github.com/ngageoint/sarpy) (or similar) directly.

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
amplitude, not a calibrated backscatter coefficient.

## Search is a crawl unless you fetch the index

There is no STAC API on the open bucket. `UmbraCatalog.search` paginates S3
listings and is slow on an unconstrained query — that is why
`umbra index fetch` / `CatalogIndex.from_release()` exist. Prefer `--local`
for anything you will run more than once.

`area=` is a task-directory name, not a geocoded place. `--place` (CLI only)
geocodes via Nominatim to a **rectangle**, so it can include nearby ground
outside the named place.

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

## No public hosted API

`umbra serve` and `docker compose up` stand up a read-only STAC API on
your machine. There is no community instance. A public one is a policy
decision (COG-streaming egress) and waits on talking to Umbra.

## AI is opt-in and never implicit

`umbra ask`, `describe`, `embed`, and `change --narrate` call a model only
when you invoke them and have configured a key. Model output is re-validated
or provenance-stamped; it never becomes a coordinate, URL, or filter on its
own. The core search / download / render path never calls a model.

## What to read next

- [Quickstart](../quickstart.md) — the five-minute path.
- [Install](../install.md) — which extra you need.
- [`docs/TODO.md`](https://github.com/reesehammer/umbra-py/blob/main/docs/TODO.md)
  — follow-ons that were scoped out of merged PRs on purpose.
