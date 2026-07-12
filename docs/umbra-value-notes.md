# Making umbra-py valuable to Umbra — working notes

Living notes on project strategy: how this unofficial toolkit becomes useful
enough to Umbra (the company) that they link to it, adopt pieces of it, or
build on it. Update the status lines as things land; add new ideas at the
bottom rather than rewriting history.

_Last updated: 2026-07-12._

## The framing

Umbra's [Open Data Program](https://umbra.space/open-data/) is a marketing
funnel: its job is to turn curious analysts into
[Canopy](https://docs.canopy.umbra.space/) (commercial tasking/archive API)
customers. This project becomes valuable to Umbra to the exact degree it:

1. **widens that funnel** — more people successfully using the open data;
2. **shortens the path from free data to paid tasking**; and
3. **does work Umbra would otherwise have to do themselves.**

## The landscape (why there's room)

Without umbra-py, the options today are:

- **Official surfaces:** a public 40+ TB S3 bucket
  ([AWS Open Data registry](https://registry.opendata.aws/umbra-open-data/))
  and a hosted [STAC Browser](https://open-data.umbra.space/browse/) — browsing,
  not searching. The catalog is *static* STAC: no search API, so
  `pystac-client`, the QGIS STAC plugin, and leafmap don't work against it.
  Canopy has a real authenticated STAC API, but it serves the commercial
  archive, not the open data.
- **Google Earth Engine:** the
  [community catalog](https://gee-community-catalog.org/projects/umbra_opendata/)
  mirrors GEC products as an ImageCollection — elegant if you live in GEE,
  platform-locked otherwise.
- **DIY:** `aws s3 sync` + jq + DuckDB + GDAL + sarpy, as documented in
  [Mark Litwintschik's blog series](https://tech.marksblogg.com/umbra-open-data-free-satellite-imagery.html).
  The best-documented workflow is a blog series, not a `pip install`.
- **Scattered pieces:** [sarpy](https://github.com/ngageoint/sarpy) (SICD/CPHD,
  low-level), [MultiRTC](https://github.com/MultiSAR/MultiRTC) (RTC processing),
  one-off downloader scripts. No cohesive toolkit; EODAG has no Umbra provider.

The techniques here aren't novel — the *packaging* is. The honest pitch:
"everyone who does this without us writes the same 500 lines of glue first."

## The ideas, in leverage order

### 1. Canopy backend behind the same `search()` interface — **not started**

The single highest-value move. Same three lines of code against the open
bucket by default, `UmbraCatalog(token=...)` against
`api.canopy.umbra.space/archive/search` for the commercial archive. Every
user onboarded on open data is then already holding the tool they'd use as a
paying customer. Precedent: Capella ships an official
`capella-console-client`; Umbra has no equivalent.

### 2. Continuously-rebuilt, published catalog index — **shipped (this repo)**

`CatalogIndex` solves the no-search-API problem locally, but every user pays
for their own crawl. Instead: crawl once on a schedule, publish the result,
everyone searches instantly.

- `export_geoparquet()` / `umbra index export` write a
  [stac-geoparquet](https://stac-geoparquet.org/) snapshot of an index —
  queryable by DuckDB / geopandas / pyarrow / rustac, even without umbra-py.
- `.github/workflows/publish-index.yml` rebuilds the full index weekly and
  publishes `umbra-open-data.parquet` + `catalog.db` on the rolling
  `catalog-index` GitHub release.
- **Next:** teach `umbra search --local` / `CatalogIndex` to bootstrap from
  the published snapshot (download instead of crawl), so a fresh install gets
  instant whole-catalog search.
- **Then offer it upstream:** "here's the pipeline; host the parquet next to
  `catalog.json` in your bucket and the whole ecosystem gets a search API for
  free." If Umbra adopts it, this project is part of their data program's
  infrastructure.

### 3. Make adoption visible where Umbra looks — **not started**

- PR to [awslabs/open-data-registry](https://github.com/awslabs/open-data-registry/blob/main/datasets/umbra-open-data.yaml)
  adding umbra-py under the Umbra entry's "Tools & Applications" (AWS sponsors
  their hosting; usage metrics matter to that program).
- Get listed on the [STAC Index](https://stacindex.org/) ecosystem page.
- Add `CITATION.cff` + a Zenodo DOI so academic users cite the package —
  publications using Umbra data are what an open data program exists to
  generate.

### 4. Demo notebooks that create SAR converts — **not started**

An `examples/` gallery for the greatest hits: change detection over one of
Umbra's time-series sites, an amplitude time series, detection chips
(ship/aircraft). Each notebook is marketing Umbra doesn't have to write, and
the thing DevRel links first. (The markdown walkthroughs in `examples/` are a
start; notebooks with rendered output travel further.)

### 5. Close the format gaps that generate support burden — **partial**

SICD → geocoded COG one-liner, RTC recipes (interop with MultiRTC), and ML
dataset prep: chipping scenes into training tiles with look-angle /
resolution / polarization metadata attached. Umbra sells into ML-heavy
analytics; tooling that makes Umbra data trivially trainable increases demand
for Umbra pixels. (`convert.py` has amplitude extraction; the rest is open.)

### 6. Then actually talk to Umbra — **not started**

With 2–3 in hand the pitch is concrete, not a favor: "unofficial toolkit,
N downloads/month, here's a hosted search index you can adopt, here's the
notebook gallery — link us from the open data page, and tell us if the
`umbra-py` name is a problem." Raise the trademark question proactively;
the existing "not affiliated" disclaimer plus asking first makes the project
easy to say yes to. Good outcomes, any of which locks in the niche: a docs
link, a registry listing, co-marketing, or them upstreaming the index.

## Guardrails

- **Don't** build a hosted service on Umbra's data or brand without talking
  to them first.
- **Keep the crawl polite:** scheduled (weekly), rate-limited, incremental.
  The fastest way to become *negatively* valuable is to be the reason their
  S3 bill spikes.
- **Don't position against Canopy.** This is the on-ramp to their commercial
  product, not a competitor to it.
