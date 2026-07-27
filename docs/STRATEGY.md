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
  `umbra serve`'s read-only STAC API façade removes — see §5.2.) The *load*
  half of that stack fails for a second, independent reason: `stackstac` /
  `odc-stac` want a common projected grid, and successive Umbra passes over one
  site arrive in whatever UTM zone and extent each acquisition used. That is
  what `to_stack` / `umbra stack` do themselves — see §5.5.
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

The full `examples/` notebook gallery (`01`–`08`: hello → download/open GEC →
change detection → amplitude time series → detection chips → site monitoring →
SICD amplitude → time-series datacube) exists and doubles as a live eval — each
notebook is self-checking and guarded offline by `tests/test_examples.py`. `08`
is the on-ramp for the stacking chain of workstream 5.5: `to_stack` onto a
shared equal-area grid, then `stack_stats` (net change, `blocks`, `block_series`)
reducing the cube to a measured answer.

### 5.5 Close the format gaps that generate support burden — **partial**

ML dataset prep (`umbra chips`), time-series datacubes (`umbra_py.to_stack` /
`stack_to_geotiff` / `umbra stack` — the co-registered `(time, y, x)` xarray
cube or multi-band GeoTIFF that `stackstac`/`odc-stac` can't produce here, see
§2 — reduced to a JSON answer by `stack_stats` / `umbra stack --stats` / the
`stack_stats` agent tool / `POST /artifacts/stats` on `umbra serve` / the
`umbra demo` explorer's **Quantify** button, and to a
*located* answer by its `blocks=N` spatial breakdown, which says which part of a
site moved and between which two passes — and, with `block_series=True`, each
block's whole pass-to-pass sequence rather than only the interval it moved most
in, which is what tells a steady drift apart from a single step, now *drawn* as
sparklines of the site's and its peak block's history in the explorer's Quantify
readout — and, since the cube used to cost `max_size²` × the number of passes in
memory, `to_stack(lazy=True)` / `umbra stack --lazy` now defer each pass into one
`dask` chunk and have both consumers, `stack_to_geotiff` and `stack_stats`, walk
the series a slice at a time, and `chunk_size=N` / `--chunk-size N` takes the
same step *within* a pass — windows read and written independently — so neither
the length of the series nor the size of one scene sets how much archive can be
stacked sharp)
and SICD → geocoded COG (`umbra convert`, including DEM/`--dem auto`
orthorectification, geoid handling, and four RTC flattening models:
`cosine`/`area`/`gamma`/`facet`) all ship. The fourth is the image-space
illuminated-area facet integration (Small 2011): it projects every terrain facet
into the scene's own `(slant_range, azimuth)` geometry, accumulates the
illuminated area landing in each radar cell, and normalises by that total — so
it is the only one of the four that measures **layover**, where several ground
facets image into one cell and their returns sum. ~~**Open:** calibration
itself~~ **shipped** — `umbra convert --calibrate {sigma0,beta0,gamma0,rcs}` /
`sicd_to_geocoded_cog(calibration=…)` scales pixel power by the SICD's own
`Radiometric` scale-factor polynomial, in image space where those polynomials
are defined, so the output is a physical backscatter coefficient rather than
relative amplitude. It composes with the flattening (both are power-domain
factors), which makes `--rtc-model facet --calibrate gamma0` a terrain-flattened
**gamma-nought** product. The caveat did not disappear so much as become
*detected*: Umbra's open products generally carry no `Radiometric` block, and
asking for a calibration one cannot support is a self-describing error naming
what it does carry (`sicd_calibration_types` answers the same question ahead of
time) rather than a calibrated-looking number. And a calibrated product now
*says so*: every raster `umbra convert` writes carries `UMBRA_*` GeoTIFF
metadata naming the calibration, the RTC model and its resolved reference angle,
the DEM/geoid, the projection, the scale and the CC-BY licence
(`read_conversion_tags` / `umbra convert --provenance` / `gdalinfo`), because a
physical measurement nobody can attribute to a calibration is not one.
**Open:** MultiRTC interop, which stays deferred.

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
  **Partly shipped:** the gathering half (`_gather_items` / `_search_source`) and
  now the *geography* option group — `--bbox` / `--place` / `--intersects` are one
  shared pair of decorators plus one shared resolver across all fourteen gather
  commands. That extraction was worth doing because of what the duplication had
  cost: the options had drifted, so the polygon filter the library, the index and
  the STAC API all support reached exactly one command (`umbra search`) and
  `--place` was missing from three more. Every front door now takes an area of
  interest as a shape rather than a bounding rectangle — including the
  natural-language one: `umbra ask --aoi` lets the planner *select* one of the
  polygons the user supplied, by name, rather than authoring coordinates a
  hallucination could silently move (the §7.1 boundary applied to geometry).
  ~~**Open:** the date / task-name / limit options are still per-command (their
  help text is genuinely command-specific), and `umbra map` still lacks
  `--area`.~~ The **task-name group is shipped too**, and with it the gap it had
  left: `umbra map` now takes `--area` / `--fuzzy` like every sibling, so the one
  verb whose job is showing where the archive has imagery no longer makes you
  find a site's bounding box first (`umbra index build` / `update` gained the
  matching `--fuzzy`). `--area` is one shared `_area_option` beside
  `_fuzzy_option`, and — the part that outlasts this PR — **both** shared groups
  are now checked against one roster of gather commands
  (`conftest.GATHER_COMMANDS`): `tests/test_cli_option_groups.py` asserts every
  command on it exposes the group *and* forwards it to the search backend, so the
  drift that cost the polygon filter thirteen commands, `--place` three and
  `--area` one now fails a test rather than shipping. See the CHANGELOG.
  **Open:** the date / limit options are still per-command, and deliberately so —
  unlike geography and task name, `--limit`'s *default* is command-specific
  (20 / 24 / 100 / 500 / 2000) as well as its wording, so a shared decorator would
  have to override both and would buy nothing but indirection. Extract them only
  if that stops being true — see `TODO.md`.
- ~~Split `viz.py` into a `viz/` package (geojson / maps / raster / composites /
  gallery) with re-exports preserved (was P3 #19).~~ **shipped** — the
  2 023-line module is now `geojson.py` / `raster.py` / `composites.py` /
  `contact_sheet.py` (the gallery, renamed so the submodule is not shadowed by
  the `gallery` function beside it) / `maps.py`, plus `_deps.py` for the single
  `viz`-extra gate. `viz/__init__.py` re-exports every name the module had —
  public functions *and* the private helpers other package modules import from
  `umbra_py.viz` — and every definition is AST-identical to its pre-split form
  bar six relative-import levels, so no caller changed. The one behavioural
  difference is the test seam: an internal helper is now patched on the module
  that *calls* it. See the CHANGELOG.
- ~~Split `cli.py` — the outlier the `viz` split left behind — the same way.~~
  **shipped** — the 5 522-line module is now a `cli/` package of nine modules
  grouped by what the verb does: `_root.py` (the Click group, the JSON error
  envelope, `main()`), `_shared.py` (the option groups and how a command obtains
  its items), then `discover` / `scenes` / `process` / `composites` / `atlas` /
  `explore` / `indexes`. The claim is checked rather than asserted: the whole
  `--help` surface (group, 28 commands, 3 sub-groups, every option and help
  string) is byte-identical to the pre-split output, and all 74 definitions are
  AST-identical bar three mechanical rewrites — import depth, the shared helpers
  qualified as `_shared.<name>` (one patch target, which is what lets the
  option-group parity suite keep iterating over all fourteen gather commands),
  and thirteen copies of one item-fetch expression collapsed into
  `_shared._item_from_url`. See the CHANGELOG. **With this the structural-debt
  group is closed bar the conditional R\*Tree upgrade below**; the date/limit
  option groups stay deliberately per-command (see above).
- ~~Wire `pytest --cov` + a Codecov badge into CI (was P2 #16).~~ **shipped** —
  the `test-all-extras` job (the one job where every module actually runs)
  measures branch coverage behind a `--cov-fail-under` floor and uploads to
  Codecov (non-blocking); README carries the CI + coverage badges. See the
  CHANGELOG.
- SQLite R\*Tree upgrade *iff* the index grows to hundreds of thousands of
  items (the schema-version marker already makes this a migration, not a break).

**Demo / hosting polish (was `DEMO_APP_GAPS` G7 + Path A polish)**

- Packaging/hosting: ~~a Dockerfile + compose for one-command self-hosting of
  `umbra serve`~~ **shipped** (`Dockerfile` + `docker-compose.yml` +
  `docker-entrypoint.sh`, a first-boot index fetch, a `/healthz` probe, and a
  `docker.yml` CI smoke test — see the CHANGELOG). ~~A **GitHub Pages
  deployment of the static `umbra demo` / `catalog.pmtiles` showcase**~~
  **shipped** (`umbra showcase` composes the whole-catalog map + interactive
  explorer + a landing page into a static `site/showcase/`, and the `docs.yml`
  Pages job publishes it beside the docs — non-blocking and main-only; see the
  CHANGELOG).
- ~~Precompute showcase change artifacts for ~6–10 curated sites (R4 for the
  static path).~~ **shipped** — `umbra showcase --featured N` renders a change
  composite for the most repeat-imaged sites (or the ones `--featured-area`
  names) into `featured/` and puts them on the landing page as a captioned
  gallery, and the `docs.yml` Pages job passes `--featured 6`; see the CHANGELOG.
  ~~Still open under R4: the *swipe* and *timescan* variants of the same idea.~~
  **shipped too** — `umbra showcase --featured-view {change,timescan,swipe}`
  renders the same marquee selection as a whole-series *timescan* composite
  (mean/peak/variability as RGB, 3+ passes) or as a self-contained before/after
  *swipe* page, which the gallery shows as a link card since an interactive page
  has no still to preview. **R4 is closed.**
- ~~Let the interactive explorer scale to the whole catalog (Path A's last
  structural cap) and collapse the showcase's map/explorer pair into one page.~~
  **shipped** — `umbra demo --pmtiles` draws every acquisition from a
  whole-catalog `.pmtiles` archive read by range request, with the sidebar filters
  compiled to MapLibre expressions evaluated inside the tiles, and `umbra showcase
  --unified` (what `docs.yml` now deploys) builds the showcase as a single
  explorer over that archive instead of a click-only map plus a sliced explorer.
  See the CHANGELOG. ~~Still open: tiling footprint *polygons* rather than
  centroids, which is what the embedded-slice mode still has over it.~~
  **shipped** — the archive now carries a `footprints` polygon layer (clipped per
  tile, from zoom 6 up) beside the centroids, and both the explorer and the
  minimal viewer draw coverage shape as you zoom in. ~~The embedded-slice mode's
  one remaining extra is the on-click "Get SAR image" COG overlay.~~ **shipped
  too** — every tiled feature now references its GEC cloud-optimized GeoTIFF (a
  bare filename resolved against the `stac_href` the tiles already carry) plus the
  bounds to place it, and the shared geotiff.js driver gained a MapLibre
  `image`-source placement beside its Leaflet `imageOverlay` one, so the
  whole-archive explorer streams the radar picture on click like the slice one.
  ~~The two modes now differ only in the two fields vector tiles do not encode
  (polarizations and the per-product asset list).~~ **Shipped, and Path A is
  closed** — those two fields (`pol`, `assets`) are tiled comma-joined, which
  both finished the detail panel and bought a facet *neither* explorer had: a
  **polarization filter**, the one that decides whether a change measurement is
  valid rather than what it shows (`POST /artifacts/stats` refuses a
  mixed-polarization selection; the page had no control to narrow one). Chips in
  both modes, compiled to a MapLibre `index-of` test inside the tiles for the
  whole-archive one. The whole-archive front end is now a strict superset of the
  embedded-slice one. See the CHANGELOG.
- ~~Bake place labels into the *published* weekly snapshot.~~ **shipped** —
  `umbra index bake --by-site` geocodes once per site rather than once per
  acquisition (a task's passes share their ground), which brings a whole-catalog
  bake inside Nominatim's ~1 req/s policy, and `publish-index.yml` now runs it
  (bounded, non-blocking) before the derived artifacts, so the fetched
  `catalog.db`, the parquet export and `catalog.pmtiles` all arrive pre-labelled.
  `umbra tiles` was also taught to tile the baked label rather than the task
  codename. See the CHANGELOG. ~~Still open: baking per-item **thumbnails** into
  the published snapshot, which is gated on egress (a COG overview streamed per
  acquisition) rather than a rate limit.~~ **shipped** — the pictures are
  published as a separate `catalog.thumbs.db` sidecar (`umbra index
  fetch-thumbnails` / `export-thumbnails`), not as a column of `catalog.db`, so
  the metadata download that promises "instant local search, no crawl" stays
  small and the pixels are opt-in. The egress gate is answered by making the
  bake *incremental* — the weekly run re-imports the previous sidecar and then
  streams only the acquisitions added since, bounded by `--limit` and spent
  newest-first — so the archive is re-listed weekly but never re-streamed. See
  the CHANGELOG. **This closes the demo / hosting polish group.**

**SAR-processing depth (was workstream 5.5)**

- ~~Gamma-nought RTC by facet integration in image space.~~ **shipped** —
  `umbra convert --rtc --rtc-model facet` accumulates each terrain facet's
  illuminated area into the radar cell it images into and normalises by that
  total, so folded ground (layover) is suppressed together rather than corrected
  pixel-by-pixel; flat terrain is unchanged and a planar slope reduces to the
  `area` × `gamma` closed form. See the CHANGELOG.
- ~~Radiometric *calibration* (every RTC model normalises detected amplitude, so
  the output was a relative image).~~ **shipped** — `umbra convert --calibrate
  {sigma0,beta0,gamma0,rcs}` applies the SICD's own `Radiometric` scale-factor
  polynomial to pixel power, in image space where the polynomial is defined
  (metres from the SCP, chip origin included), so the result is a physical
  backscatter coefficient — and because it is a power-domain factor like the
  flattening, the two compose into a terrain-flattened gamma-nought product.
  Where the metadata cannot support it — Umbra's open products generally carry no
  `Radiometric` block — the refusal is explicit and names what the product does
  carry, and `sicd_calibration_types` reports it without trying. See the
  CHANGELOG.
- ~~Record what a converted raster *is* (every setting above left no trace in the
  file, so two scenes converted differently were indistinguishable after the
  fact).~~ **shipped** — `conversion_tags` writes namespaced `UMBRA_*` GeoTIFF
  metadata into every raster the module emits: the calibration, the RTC model and
  the reference incidence angle it resolved to, the DEM/geoid used, the
  projection, the resampling kernel, the amplitude scale, what a pixel value is,
  the umbra-py version, and the CC-BY licence + attribution (design principle 4
  applied to a derivative). Steps that did not run report `"none"` rather than
  vanishing, and only the source *file name* is recorded. Read it with
  `read_conversion_tags`, `umbra convert --provenance`, or `gdalinfo`. See the
  CHANGELOG. **Still open:** MultiRTC interop — heavy, research-oriented,
  deferred. **This closes the SAR-processing-depth group bar that interop.**
- ~~The datacube's memory ceiling (every pass read eagerly, so a long series had
  to be traded against resolution).~~ **shipped** — `to_stack(lazy=True)` /
  `umbra stack --lazy` (the new `[dask]` extra) make each acquisition one chunk
  fetched on demand, while the shared grid stays eagerly resolved so an
  impossible stack still fails at the call. `stack_to_geotiff` writes band by
  band and `stack_stats` streams the series (first / previous / current pass
  resident), so the whole chain's memory follows the grid rather than the number
  of passes — the numbers are identical either way. See the CHANGELOG.
  ~~**Open:** chunking *within* a slice~~ **shipped** — `to_stack(chunk_size=N)`
  / `umbra stack --lazy --chunk-size N` cuts each pass into `N`-square windows
  read (and written) independently, so the unit of work stops being a whole
  `max_size²` slab and the achievable sharpness stops depending on how much of
  *one scene* fits in memory. A window is the shared grid restricted to its own
  rows and columns, so it is pixel-identical to that region of the whole-slab
  read; the cost is one range-read per window rather than per pass, which is why
  it is opt-in. `_write_stack_geotiff` follows the cube's chunks, so the written
  file is byte-identical however it was read. See the CHANGELOG.
  ~~**Open:** letting `umbra serve`'s `POST /artifacts/stats` opt into the lazy
  path.~~ **shipped** — `umbra serve --stack-lazy [--stack-chunk-size N]
  [--stack-scheduler {synchronous,threads}]` gives the one endpoint whose cost
  grows with the *number* of acquisitions the same ceiling-lift the CLI has.
  It is an instance-wide policy (`serve.StackExecution`), not a request field,
  because it needs the `dask` extra on the server and a decision about the
  threads one request may spend — `synchronous` by default, since a request
  handler that quietly starts a thread pool per render is a worse surprise than
  a slower one. The scheduler is set as a context manager around the single
  render (so it cannot leak), the eager default never imports `dask`, and
  because the numbers are identical either way the policy is deliberately *not*
  in the artifact cache key: an operator flips it without invalidating a cached
  artifact. See the CHANGELOG.
  ~~**Open (not a blocker, in `TODO.md`):** `stack_stats` still materialises one
  slice per pass — its medians and percentiles need the pass whole, so streaming
  it would mean approximate quantiles, i.e. changing the numbers.~~ **shipped** —
  `stack_stats(windowed=True)` / `umbra stack --stats-windowed` walks the cube's
  own windows instead of whole passes, so three windows are resident rather than
  three slices and a cube stacked sharper than a slice you can hold is
  *measurable* and not only writable. The decision this entry deferred was made
  rather than dodged: every count, mean, standard deviation and change number is
  still exact (each is a sum, so a window folds into an accumulator), and the
  percentiles — the one statistic that needs the pass whole — become estimates
  from a mergeable 0.05 dB histogram, good to about a bin. What makes that
  acceptable is that the summary *says which numbers are which*
  (`quantile_method` / `quantile_bin_db` + a caveat), and that a default summary
  is byte-identical to before, so nobody gets an estimate they didn't ask for.
  Blocks are cut from the shared grid, not from a window, so a misaligned window
  edge leaves the spatial breakdown identical. **This closes the datacube's
  memory ceiling end to end — build, write and measure.** See the CHANGELOG.
  ~~**Open (not a blocker, in `TODO.md`):** the mode is library + CLI only; `umbra
  serve`'s `POST /artifacts/stats` and the agent tools do not expose it, because
  there it would have to enter the artifact cache key (unlike `--stack-lazy`,
  this one *does* move numbers).~~ **shipped for the server** — `"windowed":
  true` on `POST /artifacts/stats`, and the cache-key question was answered by
  making it a **request option** rather than an instance policy: because it
  moves the percentiles it belongs in the key, and putting it in the request
  body puts it there for free, so a cached artifact can never depend on an
  invisible server flag. It is refused (`400`) on an instance without
  `--stack-chunk-size`, where there are no windows to walk and it could only
  estimate percentiles for the same memory. The exact numbers are pinned
  identical through the endpoint's own renderer. The agent tools are left out on
  purpose: they build an eager 512-pixel cube, so there is no ceiling there to
  lift and `windowed` would only make a model's percentiles approximate. See the
  CHANGELOG.

**Agent-session hardening (was `STRATEGY` §7 follow-on)**

- ~~A SessionStart hook / permission allowlist for remote coding-agent sessions.~~
  **shipped** — `.claude/hooks/session-start.sh` (registered in
  `.claude/settings.json`) installs the package editable with every extra on a
  Claude-Code-on-the-web container so `ruff` / `mypy` / `pytest` and the `umbra`
  CLI work from the first turn (mirroring CI's `test-all-extras` job), and the
  same `settings.json` pre-approves the documented dev-loop + read-only commands.
  Gated on `$CLAUDE_CODE_REMOTE`, idempotent, synchronous, dev-tooling only (no
  runtime code, nothing on the published package). See the CHANGELOG.

**Getting the published artifacts to actually exist**

- ~~The rolling `catalog-index` release had never been produced, so every
  artifact this project publishes was a 404.~~ **Fixed** — the weekly
  `publish-index` workflow had died in the same step on both of the only two
  runs it has ever had: `umbra tiles --local --db catalog.db`, where the gather
  commands spell that option `--index-db` (`--db` is the decibel stretch on the
  render commands). Because the tiling step ran *before* the release step, a
  completed crawl, 2 725 baked place labels and a good parquet export went with
  it and the release was never created — so `umbra index fetch`, the parquet,
  `catalog.pmtiles`, the thumbnail sidecar and the Pages showcase built from
  them were all dark. The invocation is corrected, each artifact is now uploaded
  by the step that builds it (the crawl publishes before anything is derived
  from it, so a failure deriving the basemap costs the basemap and not the
  snapshot), and `tests/test_workflows.py` parses every `umbra …` invocation in
  `.github/workflows/*.yml` against the real Click command tree so the same
  drift fails a pull request rather than a Monday morning. See the CHANGELOG.
  **Open, and operational rather than code:** the workflow is weekly +
  `workflow_dispatch`, so the first good snapshot needs a maintainer to dispatch
  a run (or to wait for the Monday cron); until one lands, the artifacts stay
  absent and the docs job keeps emitting its "showcase not built" warning.

**Maintainer / relationship actions (no code)**

- Dispatch `Publish catalog index` once so the rolling `catalog-index` release
  exists (see the group above; the code side is fixed).
- Register the PyPI Trusted Publisher and cut the `v0.1.0` GitHub Release to
  claim the name (release plumbing already ships).
- The ecosystem-visibility actions in §5.3, the "offer it upstream" move in
  §5.2, and the "talk to Umbra" conversation in §5.6.

Fine-grained follow-ons for individual shipped features are tracked in
[`TODO.md`](../TODO.md); the record of everything already delivered is in
[`CHANGELOG.md`](../CHANGELOG.md).
