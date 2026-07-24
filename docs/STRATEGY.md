# umbra-py Strategy — Maximally Valuable to Umbra and the SAR Ecosystem

> **How this file fits with the rest of the repo.** This is the single home for
> the project's enduring *context*: why it exists, where it sits in the SAR
> ecosystem, the design principles it holds to, and the remaining critical
> path. It is deliberately **not** a status log.
>
> - **What has shipped** lives in [`CHANGELOG.md`](../CHANGELOG.md) (history,
>   newest first) — the authoritative record. Do not re-narrate shipped work
>   here.
> - **Fine-grained open follow-ons** live in [`TODO.md`](../TODO.md) (the
>   per-PR ledger of items intentionally scoped out of merged PRs).
> - **This file** carries the durable "why" and the short list of genuinely
>   open workstreams (§8).
>
> The three companion planning docs — `CODEBASE_ANALYSIS.md`,
> `DEMO_APP_GAPS.md`, and `AI_INTEGRATION_IDEAS.md` — were analysis snapshots
> whose plans are now largely executed. They have been consolidated into this
> file and reduced to short pointers. Their historical item IDs (`C1`, `G6`,
> `P2 #11`, workstream `5.x`, …) still appear in source docstrings and commit
> messages; the detail behind each lives in git history and the CHANGELOG.

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
  The QGIS STAC plugin and leafmap search hit the same wall. (This is the wall
  `umbra serve`'s read-only STAC API façade removes — see §5.2.)
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
  `CatalogIndex`, the published geoparquet snapshot, and now the `umbra serve`
  STAC API).
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
§3); (2) the name — `umbra-py` trades on their trademark, and an unrelated
[`Umbra` package](https://pypi.org/project/Umbra/) already exists on PyPI.
Raise the naming question with Umbra proactively; the existing "not
affiliated" disclaimer plus asking first makes the project easy to say yes to.

## 5. Workstreams, ranked by leverage

Status here is a one-line marker, not a log — the shipped detail is in the
CHANGELOG, the open follow-ons in `TODO.md`. Workstream numbers (`5.1`…`5.6`)
are stable identifiers cited from source docstrings; keep them.

### 5.1 Canopy backend behind the same `search()` interface — **shipped**

Pass a Canopy token (`UmbraCatalog(token=…)`, `umbra search --token …`, or
`$UMBRA_CANOPY_TOKEN`) and the *same* `search()` interface queries the
commercial archive's real STAC API instead of the open bucket — the funnel
made literal. Reachable across the whole CLI (`map`/`gallery`/`change`/…),
plus a keyed `get_item` lookup and the MCP server. **Open:** push
`product_types` down as a STAC filter extension once the exact Canopy field
names are confirmed, and verify request/response shapes against the live API
(needs a real token — see `TODO.md`).

### 5.2 Continuously rebuilt, published catalog index — **shipped**

`export_geoparquet()` / `umbra index export` write a
[stac-geoparquet](https://stac-geoparquet.org/) snapshot; the weekly
`publish-index.yml` workflow rebuilds the full index and publishes
`catalog.db`, `umbra-open-data.parquet`, a whole-catalog `catalog.pmtiles`
basemap, and (opt-in) a `catalog.embed.db` similarity sidecar on the rolling
`catalog-index` release; the consume side (`umbra index fetch`,
`umbra tiles --fetch`, `umbra embed fetch`) pulls them so a fresh install gets
instant whole-catalog `--local` search, a zoomable basemap, and visual
similarity search with no crawl. **Open (maintainer):** *offer it upstream* —
"host the parquet, the `.pmtiles` basemap, and the similarity vectors next to
`catalog.json` and the whole ecosystem gets a search API, a whole-catalog map,
and scene-similarity search for free." If Umbra adopts it, this project is
part of their data program's infrastructure.

### 5.3 Make adoption visible where Umbra looks — **partial**

`CITATION.cff`, `SECURITY.md`, and a Contributor Covenant `CODE_OF_CONDUCT.md`
ship, completing GitHub's community profile. **Open (mostly maintainer
actions):** a PR to
[awslabs/open-data-registry](https://github.com/awslabs/open-data-registry/blob/main/datasets/umbra-open-data.yaml)
adding umbra-py under the Umbra entry's "Tools & Applications"; a listing on
the [STAC Index](https://stacindex.org/) ecosystem page; registering
`umbra-mcp` in the MCP registries and Anthropic's directory; and minting the
Zenodo DOI on the first release.

### 5.4 Demo notebooks that create SAR converts — **shipped**

The full `examples/` notebook gallery (`01`–`07`: hello → download/open GEC →
change detection → amplitude time series → detection chips → site monitoring →
SICD amplitude) exists and doubles as a live eval — each notebook is
self-checking and guarded offline by `tests/test_examples.py`.

### 5.5 Close the format gaps that generate support burden — **partial**

ML dataset prep (`umbra chips`) and SICD → geocoded COG (`umbra convert`,
including DEM/`--dem auto` orthorectification, geoid handling, and three RTC
flattening models: `cosine`/`area`/`gamma`) all ship. **Open:** the *fully
calibrated* remainder of RTC — full gamma-nought illuminated-area facet
integration in image space (vs. the shipped per-pixel `gamma` approximation)
— and MultiRTC interop. This is a heavier, calibration-oriented job (Umbra's
open products are not radiometrically calibrated) and stays deferred.

### 5.6 Then actually talk to Umbra — **not started** (maintainer/relationship)

Sequenced after 5.2–5.3 so the pitch is concrete, not a favor: "unofficial
toolkit, N downloads/month, here's a hosted search index you can adopt,
here's the notebook gallery — link us from the open data page, and tell us if
the `umbra-py` name is a problem." Good outcomes, any of which locks in the
niche: a docs link, a registry listing, co-marketing, or upstreaming the index.

## 6. Guardrails

- **Don't** build a hosted service on Umbra's data or brand without talking
  to them first.
- **Keep the crawl polite:** scheduled (weekly), rate-limited, incremental.
  The fastest way to become *negatively* valuable is to be the reason their
  S3 bill spikes.
- **Don't position against Canopy.** This is the on-ramp to their
  commercial product, not a competitor to it.

## 7. Design principles to hold onto

These are the durable rules the AI-integration and demo work were built on;
they apply to every future change (consolidated from the former
`AI_INTEGRATION_IDEAS.md` §6).

1. **Deterministic core, AI at the edges.** Models plan, describe, and
   narrate; the library searches, downloads, and renders. Never let a model
   output become a coordinate, a URL, or a filter without passing through the
   deterministic layer. (This is the `§A4`/`§6.1` determinism boundary cited
   from `planner.py`, `describe.py`, `narrate.py`, and `mcp_server.py`.)
2. **Images are the API.** The library's superpower for AI is that its
   outputs are pictures with provenance. Prefer returning renderable artifacts
   (MCP image blocks, PNGs with JSON sidecars) over prose.
3. **Context is a product surface.** `llms.txt`, context cards, tool
   descriptions, and packaged prompts deserve the same review bar as code —
   they are what the agent "reads" instead of the README.
4. **License propagation is non-negotiable.** CC-BY attribution must survive
   every AI transformation, including model-generated text about the data.
5. **Agents are users; users are agents.** Every improvement for one (JSON
   errors, stable schemas, runnable examples, resumable operations) compounds
   for the other. Build once, serve both.

## 8. Current status & remaining critical path

The three original planning docs are essentially executed — the S3 pagination
fix (PR #29), the prebuilt/published index, the MCP server, the `umbra serve`
STAC API, natural-language search (`umbra ask`), the `umbra demo` self-serve
explorer, `umbra tiles` PMTiles, `umbra describe`/`watch`/`chips`/`embed`, and
the `umbra convert` SICD/DEM/RTC pipeline have all shipped (see the CHANGELOG).
What remains, grouped by the kind of work rather than by the old doc it came
from:

**Structural code debt (schedule, don't rush)**

- Extract the shared search-vs-URLs gathering + common Click option groups out
  of the CLI commands that still duplicate them (was `CODEBASE_ANALYSIS` P3 #18).
- Split `viz.py` into a `viz/` package (geojson / maps / raster / composites /
  gallery) with re-exports preserved (was P3 #19).
- Wire `pytest --cov` + a Codecov badge into CI (was P2 #16).
- SQLite R\*Tree upgrade *iff* the index grows to hundreds of thousands of
  items (the schema-version marker already makes this a migration, not a break).

**Demo / hosting polish (was `DEMO_APP_GAPS` G7 + Path A polish)**

- Packaging/hosting: ~~a Dockerfile + compose for one-command self-hosting of
  `umbra serve`~~ **shipped** (`Dockerfile` + `docker-compose.yml` +
  `docker-entrypoint.sh`, a first-boot index fetch, a `/healthz` probe, and a
  `docker.yml` CI smoke test — see the CHANGELOG). Still open: a **GitHub Pages
  deployment of the static `umbra demo` / `catalog.pmtiles` showcase** (the
  docs site already deploys to Pages; the showcase is the remaining piece).
- Bake per-item thumbnails / place labels into the *published* weekly snapshot
  (gated on egress) and precompute showcase swipe/change/timescan artifacts for
  ~6–10 curated sites (R4 for the static path).

**SAR-processing depth (was workstream 5.5)**

- Fully calibrated gamma-nought RTC (facet integration in image space) and
  MultiRTC interop — heavy, research-oriented, deferred.

**Agent-session hardening (was `STRATEGY` §7 follow-on)**

- A SessionStart hook / permission allowlist for remote coding-agent sessions.

**Maintainer / relationship actions (no code)**

- Register the PyPI Trusted Publisher and cut the `v0.1.0` GitHub Release to
  claim the name (release plumbing already ships).
- The ecosystem-visibility actions in §5.3, the "offer it upstream" move in
  §5.2, and the "talk to Umbra" conversation in §5.6.

Fine-grained follow-ons for individual shipped features are tracked in
[`TODO.md`](../TODO.md); the record of everything already delivered is in
[`CHANGELOG.md`](../CHANGELOG.md).
