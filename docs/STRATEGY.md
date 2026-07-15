# umbra-py Strategy — Maximally Valuable to Umbra and the SAR Ecosystem

*A living strategy document: why this project exists, where it sits in the
ecosystem, and the ranked workstreams that make it valuable to Umbra (the
company) and to everyone working with SAR open data. Update the status lines
as things land; add new ideas at the bottom rather than rewriting history.
Companion docs: [`CODEBASE_ANALYSIS.md`](CODEBASE_ANALYSIS.md) (code-level
priorities), [`AI_INTEGRATION_IDEAS.md`](AI_INTEGRATION_IDEAS.md) (AI/MCP
direction), [`DEMO_APP_GAPS.md`](DEMO_APP_GAPS.md) (demo-app readiness).*

*Last updated: 2026-07-15.*

---

## 1. The thesis

Umbra's [Open Data Program](https://umbra.space/open-data/) is a marketing
funnel: its job is to turn curious analysts into
[Canopy](https://docs.canopy.umbra.space/) (commercial tasking/archive API)
customers. umbra-py becomes valuable to Umbra to the exact degree it:

1. **widens that funnel** — more people successfully using the open data;
2. **shortens the path from free data to paid tasking**; and
3. **does work Umbra would otherwise have to do themselves.**

For the wider ecosystem, the goal is simpler: make Umbra's 16–25 cm SAR feel
as approachable as Sentinel-1 or Landsat — searchable, previewable, and
analysis-ready from the scientific Python stack in a few lines.

These goals reinforce each other. The honest pitch to Umbra is not "no one
can do this without us"; it's *"everyone who does this without us writes the
same 500 lines of glue first, and many give up."*

> **Critical-path note (2026-07-15):** the S3 pagination bug that silently
> truncated every listing at 1,000 keys — the prerequisite the analysis, demo,
> and AI-integration docs all named for any "full catalog" work — is fixed
> (PR #29). Whole-catalog search, index builds, and renders are complete again.
> Building on that, the **prebuilt-index consume side is now shipped**
> (workstream 5.2): `umbra index fetch` / `CatalogIndex.from_release()` pulls
> the weekly `catalog.db` snapshot, so a fresh install gets instant
> whole-catalog `--local` search with no crawl. That was the last shared
> prerequisite the demo / MCP / STAC-API layers were waiting on — those are now
> unblocked.
>
> **Update:** the **`umbra-mcp` MCP server has shipped** (`AI_INTEGRATION_IDEAS.md`
> B1 — the flagship AI deliverable). Every MCP-enabled client is now a
> zero-install natural-language front door to the archive; the imagery tools
> return radar pictures, not just JSON. Getting it *listed* in the MCP
> registries and Anthropic's directory is now part of workstream 5.3 ("make
> adoption visible where Umbra looks").

## 2. The landscape: life without umbra-py

Every existing path to the open data is workable but not easy, for one
structural reason: **Umbra publishes a static STAC catalog with no search
API**, which breaks the standard tooling that makes other missions feel easy.

- **Official surfaces.** A public 40+ TB S3 bucket
  ([AWS Open Data registry](https://registry.opendata.aws/umbra-open-data/))
  listable with `aws s3 ls --no-sign-request`, and a hosted
  [STAC Browser](https://open-data.umbra.space/browse/) for clicking around.
  That's *browsing*, not *searching* — there is no "GEC scenes in this bbox
  for these dates" primitive. Canopy runs a real authenticated STAC API, but
  it serves the commercial archive, not the open data.
- **Generic STAC tooling falls flat.** The elegant answer elsewhere is
  `pystac-client` + `stackstac`/`odc-stac`, but that stack assumes a STAC
  *API*. Against a static catalog you're reduced to crawling thousands of
  nested `catalog.json` files with plain `pystac` and filtering client-side.
  The QGIS STAC plugin and leafmap search hit the same wall.
- **Google Earth Engine.** The
  [community catalog](https://gee-community-catalog.org/projects/umbra_opendata/)
  mirrors GEC products as an ImageCollection — genuinely elegant if you live
  in GEE, but GEC-only, community-maintained, and platform-locked away from
  xarray / rasterio / PyTorch.
- **The DIY route.** The best-documented workflow is
  [Mark Litwintschik's blog series](https://tech.marksblogg.com/umbra-open-data-free-satellite-imagery.html)
  (`aws s3 sync` + jq + DuckDB + GDAL + sarpy) — strong evidence the gap is
  real: the state of the art is a multi-page tutorial, not a `pip install`.
- **Scattered pieces.** [sarpy](https://github.com/ngageoint/sarpy)
  (SICD/CPHD, low-level), [MultiRTC](https://github.com/MultiSAR/MultiRTC)
  (RTC processing), one-off downloader scripts. No cohesive toolkit; EODAG
  has no Umbra provider.

## 3. Novelty, honestly assessed

The individual techniques here are standard — STAC crawling, COG range
reads, SQLite indexing, xarray loading. The *packaging* is the novelty:
nothing else goes search → footprint map → quicklook → analysis-ready array
in a few lines against Umbra's catalog.

Two consequences to keep in mind:

- **Discovery is the moat; loading is convenience.** GEC products are
  cloud-optimized GeoTIFFs, so once someone has a URL, plain
  rasterio/rioxarray/QGIS can stream them. The part with no substitute is
  search over a catalog that has no search API (`UmbraCatalog`,
  `CatalogIndex`, and the published geoparquet snapshot).
- **The moat is leased, not owned.** Umbra could publish a stac-geoparquet
  index or a public STAC API tomorrow, obsoleting the crawler layer. That's
  fine — it would be a *win* for the mission, and the viz / quicklook /
  xarray / workflow layers survive and get better. Design so that outcome is
  graceful (see workstream 5.2's "offer it upstream").

## 4. Why Umbra should care (and the risks)

- The Open Data Program exists for adoption ("experiment with SAR's
  capabilities", CC BY 4.0, no sign-up), and its best-documented complaint
  is exactly the friction this library removes.
- Umbra's own engineering targets authenticated commercial customers
  (Canopy), so an open-data toolkit doesn't compete with anything they sell
  — it widens the funnel toward it. Precedent: Capella ships an official
  `capella-console-client`; Umbra has no equivalent.
- The AWS registry entry has a "Tools & Applications" section with very
  little in it; community tooling is the kind of thing companies link from
  their docs.

**Risks:** (1) upstream obsolescence of the crawler layer (acceptable, see
above); (2) the name — `umbra-py` trades on their trademark, and an
unrelated [`Umbra` package](https://pypi.org/project/Umbra/) already exists
on PyPI. Raise the naming question with Umbra proactively; the existing
"not affiliated" disclaimer plus asking first makes the project easy to say
yes to.

## 5. Workstreams, ranked by leverage

### 5.1 Canopy backend behind the same `search()` interface — **not started**

The single highest-value move. Same three lines of code against the open
bucket by default, `UmbraCatalog(token=...)` against
`api.canopy.umbra.space/archive/search` (a real STAC API) for the
commercial archive. Every user onboarded on open data is then already
holding the tool they'd use as a paying customer — the funnel, made
literal.

### 5.2 Continuously rebuilt, published catalog index — **shipped** (PR #26)

One crawl shouldn't be everyone's crawl.

- ✅ `export_geoparquet()` / `umbra index export` write a
  [stac-geoparquet](https://stac-geoparquet.org/) snapshot of an index —
  queryable by DuckDB / geopandas / pyarrow / rustac, no umbra-py needed on
  the consuming side. Every row carries a `self` link back to its sidecar.
- ✅ `.github/workflows/publish-index.yml` rebuilds the full index weekly
  and publishes `umbra-open-data.parquet` + `catalog.db` on the rolling
  `catalog-index` GitHub release.
- ✅ **Consume side shipped:** `umbra index fetch` /
  `CatalogIndex.from_release()` downloads the published `catalog.db` snapshot
  to the default index path (via the resume-safe `download_url`), so a fresh
  install runs whole-catalog `--local` search out of the box — no crawl.
  `umbra index build` now stamps a `built_at` date and `umbra index info`
  reports snapshot staleness.
- ⬜ **Then offer it upstream:** "here's the pipeline; host the parquet next
  to `catalog.json` in your bucket and the whole ecosystem gets a search API
  for free." If Umbra adopts it, this project is part of their data
  program's infrastructure.

### 5.3 Make adoption visible where Umbra looks — **not started**

- PR to [awslabs/open-data-registry](https://github.com/awslabs/open-data-registry/blob/main/datasets/umbra-open-data.yaml)
  adding umbra-py under the Umbra entry's "Tools & Applications".
- Get listed on the [STAC Index](https://stacindex.org/) ecosystem page.
- `CITATION.cff` + Zenodo DOI so academic users cite the package —
  publications using Umbra data are what an open data program exists to
  generate, and companies count them.

### 5.4 Demo notebooks that create SAR converts — **not started**

An `examples/` gallery for the greatest hits: change detection over one of
Umbra's time-series sites, an amplitude time series, detection chips
(ship/aircraft). Each notebook is marketing Umbra doesn't have to write and
the thing DevRel links first. The markdown walkthroughs in `examples/` are a
start; notebooks with rendered output travel further.

### 5.5 Close the format gaps that generate support burden — **partial**

SICD → geocoded COG one-liner, RTC recipes (interop with MultiRTC), and ML
dataset prep: chipping scenes into training tiles with look-angle /
resolution / polarization metadata attached. Umbra sells into ML-heavy
analytics; tooling that makes Umbra data trivially trainable increases
demand for Umbra pixels. (`convert.py` has amplitude extraction; the rest is
open.)

### 5.6 Then actually talk to Umbra — **not started**

Sequenced after 5.2–5.3 so the pitch is concrete, not a favor: "unofficial
toolkit, N downloads/month, here's a hosted search index you can adopt,
here's the notebook gallery — link us from the open data page, and tell us
if the `umbra-py` name is a problem." Good outcomes, any of which locks in
the niche: a docs link, a registry listing, co-marketing, or them
upstreaming the index.

## 6. Guardrails

- **Don't** build a hosted service on Umbra's data or brand without talking
  to them first.
- **Keep the crawl polite:** scheduled (weekly), rate-limited, incremental.
  The fastest way to become *negatively* valuable is to be the reason their
  S3 bill spikes.
- **Don't position against Canopy.** This is the on-ramp to their
  commercial product, not a competitor to it.

## 7. Supporting infrastructure — **shipped** (PR #26)

Strategy is only as credible as the project's reliability. In place:

- **All-extras CI job** — the optional-extra test suites (viz / load /
  convert / export) run on every PR instead of silently skipping.
- **Weekly live-catalog canary** — `pytest -m network` against the real
  bucket; catalog drift (Umbra changing layout or naming, which has
  happened before) opens a tracking issue instead of surfacing as a user
  bug report.
- **Weekly index publish** — doubles as a second canary: a red publish run
  means the crawl itself broke.
- **CI hygiene** — dependency caching, superseded-run cancellation,
  grouped Dependabot updates for Actions and pip.

Still open (from the same review): a PyPI release workflow with trusted
publishing + single-sourced version, SessionStart hook / permission
allowlist for remote agent sessions, and resolving the
`theminiverse`/`reesehammer` repository-identity mismatch in
`pyproject.toml`.
