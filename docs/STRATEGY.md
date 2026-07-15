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
>
> **Update:** the **`umbra serve` STAC API façade has shipped** (`AI_INTEGRATION_IDEAS.md`
> B2 / `DEMO_APP_GAPS.md` Path B). Umbra publishes a static STAC catalog and no
> search API — the structural reason the standard geo tooling falls flat (§2) —
> and this restores it: a read-only STAC API over the local index (`umbra serve`,
> `[serve]` extra) that `pystac-client`, the QGIS STAC plugin, `stac-browser`,
> leafmap, and OpenAPI-driven agents all speak. It is the browser-facing sibling
> of the MCP server and the shared backend a self-serve demo app (`DEMO_APP_GAPS.md`)
> needs. Materially, this widens the "discovery is the moat" surface (§3): the
> search-over-a-catalog-with-no-search-API primitive is now reachable from every
> STAC client, not just this library's own API — and it is exactly the kind of
> component that would be graceful to *offer upstream* to Umbra (5.2).
>
> **Update:** the **visual commands now render from the prebuilt index**
> (`DEMO_APP_GAPS.md` G2 / Path A step 2). `umbra map`, `gallery`, `swipe`,
> `change` and `timescan` take the same `--local` / `--index-db` flags as
> `search`, so a fetched `catalog.db` turns whole-catalog maps and galleries into
> instant, offline renders instead of a live S3 re-walk. Small but on the
> critical path: it was the last "the index does nothing for the visual output"
> gap, and it is the fast-render substrate the static-first demo (Path A) builds
> on next.
>
> **Update:** the **`llms.txt` context bundle has shipped**
> (`AI_INTEGRATION_IDEAS.md` A2 — the last open Phase 2 item). `umbra llms-txt
> [--full]` renders the [llms.txt-convention](https://llmstxt.org/) Markdown —
> the *user* agent guide ("how to drive the library"), complementing `AGENTS.md`
> (the contributor guide) and the machine-readable `umbra context` JSON — and
> the committed repo-root `llms.txt` / `llms-full.txt` are that output. It is
> pure adoption plumbing (widen the funnel, §1): any agent or newcomer can now
> fetch one file and know which product to ask for, how to search, and that
> attribution is mandatory — without reading the source. `llms-full.txt` is
> assembled from facts already in the package (the domain document, the live CLI
> tree, the module docstrings), so it can never drift from the code it
> describes.
>
> **Update:** the **deterministic first step of natural-language search has
> shipped** (`AI_INTEGRATION_IDEAS.md` C1). `--start` / `--end` now accept human
> date expressions (`2024`, `today`, `3 months ago`, `last month`) alongside
> `YYYY-MM-DD`, resolved by a stdlib-only bound-aware calendar parser with no
> model call. It lands in the single date choke point every command shares, so
> `search`, `index build`, and all the visual commands gain it at once — pure
> funnel-widening (§1): the query surface newcomers *and* agents reach for
> reads the way people actually describe time, while the core stays fully
> deterministic and offline-testable.
>
> **Update:** the **deterministic fuzzy task matching step of natural-language
> search has shipped** (`AI_INTEGRATION_IDEAS.md` C1). `area=` stays a literal
> substring by default; `fuzzy=True` / `--fuzzy` widens it to a stdlib-only
> token-wise match (`umbra_py.fuzzy`) — word-order- and punctuation-independent
> and typo-tolerant, so `"utah centerfield"` or `"centrfield"` still reach
> `"Centerfield, Utah"` with no model call. It is a strict superset of the
> substring match and the live and index backends share the one matcher, so the
> query surface newcomers and agents reach for tolerates how people actually
> type a site name while the core stays deterministic — pure funnel-widening
> (§1). The remaining C1 pieces (semantic/embedding aliasing and the LLM-planned
> `umbra ask`) are the model-backed layer that builds on this deterministic base.
>
> **Update:** the **LLM-planned `umbra ask` has shipped** (`AI_INTEGRATION_IDEAS.md`
> C1 — the capstone of natural-language search and the first feature in the
> package that calls a model). `umbra ask "…"` (`umbra_py.planner`, `[ai]` extra)
> sends the user's sentence plus the `llm_context()` document to a configured
> model (Anthropic or any OpenAI-compatible endpoint, user-supplied key) and gets
> back the search *parameters* it maps to — but the model **only plans**: every
> field is re-validated deterministically (`parse_plan`) and the resolved `umbra
> search` command is shown before it runs. This is the honest funnel-widener the
> whole C1 line was building toward (§1): a newcomer who can't yet name the
> product type or phrase a bbox describes what they want in a sentence and gets a
> real, auditable search — while the deterministic core, its testability, and the
> trust of the scientific audience (the "model plans, library executes" boundary,
> §3 novelty) are all preserved. The one open C1 piece is now the semantic
> embedding index — the offline answer to task aliasing.
>
> **Update:** the **semantic embedding index has shipped** (`AI_INTEGRATION_IDEAS.md`
> C1 — the last open C1 piece, so **natural-language search is now complete**).
> `umbra semantic build` embeds the catalog index's task names once, and `umbra
> semantic search "grain storage north dakota"` ranks them by meaning to reach
> `"Beet Piler - ND"` — the alias a query shares no word with, which the
> deterministic `--fuzzy` matcher can't and shouldn't fake. It is the persistent,
> offline, no-round-trip answer `umbra ask` only approximated, and it holds the
> same funnel-widening line (§1): a newcomer who can *describe* a site but can't
> *name* it now gets there. It also preserves the project's boundary and novelty
> (§3): the only model call is turning text into a vector (an injectable embedder,
> `[ai]` extra, never implicit), while storage, cosine ranking and the
> audit-then-run command are all deterministic — and it stays graceful under
> upstream obsolescence, since it layers on the same task list Umbra could publish
> an index for tomorrow. With C1 done, the AI critical path moves to Tier C's
> VLM-in-the-loop capabilities (scene description / change narration) and the
> example notebooks; the single highest-value strategic move overall remains the
> unstarted Canopy backend (5.1).
>
> **Update:** the **first Tier C VLM-in-the-loop capability has shipped** —
> `umbra describe` (`AI_INTEGRATION_IDEAS.md` C2). This is where the project's
> AI thesis (§3 novelty) becomes a product: the library's outputs are *images
> with precise metadata*, so `umbra describe <item-url>` renders an item's
> quicklook, sends it plus the metadata context card to a configured vision model
> (Anthropic or any OpenAI-compatible endpoint, user key), and returns a
> structured, plain-language reading — `{summary, observed_features[], confidence,
> caveats[]}`. It widens the funnel the honest way (§1): a newcomer who can search
> but can't *read* SAR (why is water dark? is that shadow or an empty field?) now
> gets the scene explained, with the SAR literacy encoded once in the packaged
> prompt. It preserves the boundary and trust the scientific audience needs (§3):
> the model **only interprets** — the picture and metadata are deterministic, the
> reply is re-validated, and every description carries the CC-BY attribution plus
> an explicit "AI-generated interpretation" provenance note, so a model's reading
> of radar is never mistaken for a measurement. The remaining C2 piece is change
> narration (`umbra change --narrate`); the example notebooks and the unstarted
> Canopy backend (5.1) remain the higher-level critical path.
>
> **Update:** the **second Tier C VLM capability has shipped, completing C2** —
> `umbra change --narrate` (`AI_INTEGRATION_IDEAS.md` C2). Where `umbra describe`
> reads one scene, this narrates the *change* between two passes — and it is the
> honest version §3's novelty demands, because the narration is grounded in a
> **deterministic per-block dB sidecar**, not just the picture. `umbra_py.narrate`
> computes a coarse north-up grid of the mean *signed* backscatter change in
> decibels (positive = brightened/appeared — the composite's green; negative =
> dimmed/vanished — its magenta) and hands the model *both* the composite PNG and
> that grid, so it narrates only change the numbers support; the grid ships next to
> the image as `<out>.narration.json`, making every statement auditable against a
> value a test can recompute. It widens the funnel the honest way (§1): a newcomer
> who can render a change composite but can't *read* it now gets "what changed,
> where, and how much (in dB)" in plain language. It preserves the boundary and
> trust the scientific audience needs (§3): the picture and the numbers are
> deterministic, the model **only interprets** (its reply passes the
> `parse_narration` boundary and never becomes a filter or a measurement), and every
> narration carries the CC-BY attribution plus the `AI_PROVENANCE` note. With C2
> complete, the AI critical path moves to Tier C's C3 watch loops / C4 `umbra chips`
> and the B3 example notebooks; the single highest-value strategic move overall
> remains the unstarted Canopy backend (5.1).
>
> **Update:** the **first C3 capability has shipped** — `umbra watch`
> (`AI_INTEGRATION_IDEAS.md` C3), the "agent as a standing analyst" primitive.
> SAR's value for monitoring is its cadence, so the funnel-widening move (§1) is
> to make *standing* monitoring trivial: run the same search on a schedule and act
> only on what is **new**. `umbra watch` (`umbra_py.watch`) packages the
> idempotent delta — it searches (live or from the index), diffs against the set
> of acquisitions previous runs already reported, returns only the new ones, and
> remembers them in the index's `meta` table — while the scheduler (cron, a GitHub
> Action, an agent loop) supplies the "when". It is machine-readable first
> (`--json` delta, `--exit-code` for a shell `if`), so a monitoring pipeline needs
> no glue. It preserves the boundary and novelty the scientific audience needs
> (§3): **no model is called** — this is pure set arithmetic over the deterministic
> search — and it layers on the same discovery moat the whole project is built on,
> so it stays graceful under upstream obsolescence. Paired with the shipped
> `umbra change --narrate`, the standing-analyst loop (new pass → composite →
> narration → notify) is now assemblable end-to-end. The AI critical path moves to
> C4 `umbra chips` and the B3 example notebooks; the single highest-value strategic
> move overall remains the unstarted Canopy backend (5.1).
>
> **Update:** **`umbra chips` has shipped** (`AI_INTEGRATION_IDEAS.md` C4 / the
> ML-dataset-prep half of workstream 5.5). Umbra sells into ML-heavy analytics, so
> the funnel-widening move (§1) is to make Umbra data trivially *trainable*:
> `umbra chips` (`umbra_py.chips`, `[load]` extra) walks a search result and cuts
> each scene's geocoded GeoTIFF into fixed-size, georeferenced training tiles
> (GeoTIFF or `.npy`) with a manifest (`.jsonl` or `.geojson`) that attaches the
> look-angle / resolution / polarization / license metadata a training pipeline
> needs to every chip. It streams only each tile's bytes via `/vsicurl/` range
> reads (no full download, memory bounded to one chip), drops partial edges and
> mostly-nodata corners, and supports overlapping tiles via `stride`. It preserves
> the boundary and novelty the scientific audience needs (§3): **no model is
> called** — chipping is pure raster iteration + manifest logic in the
> deterministic core — and it layers on the same discovery moat the project is
> built on, so it stays graceful under upstream obsolescence. It makes umbra-py the
> data-loading layer for SAR foundation-model and change-detection research (the
> audience most likely to contribute back) and is the prerequisite the exploratory
> archive-embedding work (C5) builds on. The remaining AI item is the B3 example
> notebooks; the single highest-value strategic move overall remains the unstarted
> Canopy backend (5.1).
>
> **Update:** the **standing-analyst loop is now conversational** — the `umbra
> watch` delta ships as a `watch_site` tool and a `watch-site` prompt on the
> flagship MCP server (`AI_INTEGRATION_IDEAS.md` C3's optional follow-up, which
> **completes C3**), reusing the `watch()` function unchanged. This is a direct
> funnel-widening move (§1) on the highest-leverage surface the project has: an
> MCP client can now ask "what's new at this site since I last checked?" and get
> back only the delta — all of it on the first check, just the new passes after —
> as context cards that feed straight into the existing `change_composite` /
> `timescan` tools, so SAR-based monitoring (new pass → composite → describe) is a
> zero-install conversation instead of a scripting project. It preserves the
> boundary and novelty the scientific audience needs (§3): **no model is called**
> — pure set arithmetic over the deterministic search — watch state persists in
> the local index's `meta` table so it survives across sessions with no schema
> change, and the source and store stay injectable, so it is fully offline-testable
> without the SDK. The remaining AI item is the B3 example notebooks; the single
> highest-value strategic move overall remains the unstarted Canopy backend (5.1).
>
> **Update:** the **single highest-value strategic move has shipped — the Canopy
> commercial-archive backend behind the same `search()` interface** (workstream
> 5.1). `UmbraCatalog(token=…)` (and `umbra search --token …` /
> `$UMBRA_CANOPY_TOKEN`) now searches Umbra's authenticated commercial archive
> over its real STAC API (`api.canopy.umbra.space/archive/search`) instead of
> crawling the open bucket — *the same call, one extra argument*, yielding the
> same `UmbraItem`s so every downstream verb (download, quicklook, change,
> chips, …) works unchanged against either archive. This is the funnel made
> literal (§1): a user who learned the library on the free data is already
> holding the exact tool they'd use as a paying Canopy customer, with no new
> API to learn. It is the honest, standards-based version of the pitch — the
> client speaks the STAC API standard (POST search body, `rel="next"`
> pagination), the token is only ever sent to the Canopy endpoint (never the
> open bucket), and the whole path is offline-testable against a mocked API with
> no credentials, so it holds the library's testability and trust (§3). With
> 5.1 landed, the two remaining strategic gaps are the B3 example notebooks
> (5.4) and taking adoption visible where Umbra looks (5.3) — including opening
> the "talk to Umbra" conversation (5.6), which is now concrete: the funnel runs
> end to end from free bucket to paid archive in one library.
>
> **Update:** the **release plumbing for the funnel's front door has shipped**
> (`CODEBASE_ANALYSIS.md` P0 #2/#3, P2 #11/#15). With the whole funnel now built
> — free-bucket search through the paid Canopy archive, all in one library — the
> binding constraint on §1's thesis ("widen the funnel — more people successfully
> using the open data") is no longer a missing *capability*; it is that the
> README's first instruction, `pip install umbra-py`, still fails because the
> package isn't on PyPI. The analysis doc names that the single highest-leverage
> adoption gap, and it gates the two remaining strategic workstreams (5.3 "make
> adoption visible" — every registry listing assumes an installable package — and,
> after it, 5.6 "talk to Umbra"). This lands the code half of that gap: a
> Trusted-Publishing (OIDC, no stored token) `release.yml` that builds, `twine
> check`s, and guards the tag/version before publishing on a GitHub Release; a
> single-sourced version (hatchling dynamic version, so `pyproject.toml` and
> `__version__` can't drift); the `py.typed` marker so downstream type checkers
> consume the inline types; and the fix to the stale `theminiverse`→`reesehammer`
> repository-identity mismatch across `pyproject.toml`, `CHANGELOG`, and
> `CONTRIBUTING`. What remains is a maintainer action, not code: register the PyPI
> Trusted Publisher and cut the `v0.1.0` release, which fires the workflow. With
> that, `pip install umbra-py` works and 5.3's registry/ecosystem listings become
> unblocked.
>
> **Update:** the **example notebook gallery has shipped** (workstream 5.4 /
> `AI_INTEGRATION_IDEAS.md` B3 — the last standing item on the AI critical path
> and "the thing DevRel links first"). Every AI-infused capability (C1–C4) and
> the whole funnel (free bucket → paid Canopy archive) are built; what was still
> missing was the *runnable, rendered* front door that turns a curious analyst
> into a user. Three self-contained notebooks under `examples/` now supply it —
> `01_hello_umbra` (search → summarize → quicklook, plus the geopandas /
> `to_llm_context` paths), `02_download_and_open_gec` (stream a GEC into
> analysis-ready `xarray`), and `03_change_detection` (composite two passes of a
> repeat-imaged site). They hold the project's culture exactly (§3): each is a
> *small deterministic search with `assert`s in its cells*, so it is a live eval
> as much as a tutorial, and `tests/test_examples.py` keeps them from rotting
> with a stdlib-only offline CI guard (well-formed, cells parse, only public
> `umbra_py` symbols, CC-BY attribution present) plus an opt-in
> `pytest -m network` execution against the live bucket. This is pure
> funnel-widening (§1): the greatest-hits SAR workflows are now marketing Umbra
> doesn't have to write, and the first thing a newcomer or a coding agent runs.
> With the notebooks landed, the remaining strategic gaps are the SICD → geocoded
> COG format work (5.5) and the maintainer-side adoption moves (5.3 registries,
> 5.6 talking to Umbra) — the code funnel now runs end to end.

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

### 5.1 Canopy backend behind the same `search()` interface — **shipped** (PR #45)

The single highest-value move, now landed. Same three lines of code against
the open bucket by default; pass a Canopy token —
`UmbraCatalog(token=...)`, or `umbra search --token …` / `$UMBRA_CANOPY_TOKEN`
— and the *same* `search()` interface queries
`api.canopy.umbra.space/archive/search` (a real STAC API) over the
commercial archive instead. Every user onboarded on open data is then already
holding the tool they'd use as a paying customer — the funnel, made literal.

Design notes that keep it honest and testable:

- **One interface, two archives.** `bbox` and the date bounds are pushed down
  to the STAC API; `product_types` and `area`/`fuzzy` are applied to the
  returned items exactly as on the open-bucket path, so the filters mean the
  same thing on both. Both paths yield `UmbraItem`s, so every downstream verb
  (download, quicklook, change, chips, …) works unchanged against either.
- **STAC-API standard, not a bespoke wrapper.** The client POSTs a standard
  STAC item-search body and follows `rel="next"` pagination (POST-merge or GET
  token links), so it is offline-testable against a mocked API with no
  credentials — and it degrades to a clear "token rejected" error on 401/403.
- **The token never touches the open bucket.** Bearer auth is only ever sent
  to the configured Canopy endpoint.

Open follow-ons (not blockers): pushing `product_types` down as a STAC
query/filter extension once the exact Canopy field names are confirmed, and a
`get_item(id)` archive lookup. See `TODO.md`.

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

### 5.4 Demo notebooks that create SAR converts — **partial**

An `examples/` gallery for the greatest hits: change detection over one of
Umbra's time-series sites, an amplitude time series, detection chips
(ship/aircraft). Each notebook is marketing Umbra doesn't have to write and
the thing DevRel links first. The markdown walkthroughs in `examples/` are a
start; notebooks with rendered output travel further.

- ✅ **The first three notebooks have shipped** — `01_hello_umbra.ipynb`
  (search → summarize → quicklook, plus the geopandas / `to_llm_context` paths),
  `02_download_and_open_gec.ipynb` (stream a GEC into analysis-ready `xarray`),
  and `03_change_detection.ipynb` (composite two passes of a repeat-imaged
  site). Each is *self-checking* — small deterministic searches, `assert`s in
  the code cells — so it doubles as a live eval, and `tests/test_examples.py`
  keeps them from drifting: an offline, stdlib-only CI guard (well-formed, cells
  parse, only public `umbra_py` symbols, CC-BY present) plus an opt-in
  `pytest -m network` end-to-end execution.
- ⬜ Remaining: an amplitude time-series notebook, a detection-chips notebook
  (`umbra chips`), and `04_sicd_amplitude.ipynb` (paired with the SICD →
  geocoded COG work in 5.5). Rendering pre-baked output into the committed
  notebooks (they currently ship with cleared cells) is a later polish step.

### 5.5 Close the format gaps that generate support burden — **partial**

SICD → geocoded COG one-liner, RTC recipes (interop with MultiRTC), and ML
dataset prep. Umbra sells into ML-heavy analytics; tooling that makes Umbra
data trivially trainable increases demand for Umbra pixels.

- ✅ **ML dataset prep shipped** (`umbra chips` / `umbra_py.chips`, `[load]`
  extra): chips scenes into fixed-size, georeferenced training tiles with the
  look-angle / resolution / polarization / license metadata attached per chip in
  a `.jsonl` (or `.geojson`) manifest. See `AI_INTEGRATION_IDEAS.md` C4.
- ⬜ SICD → geocoded COG one-liner and RTC recipes (interop with MultiRTC) are
  still open. (`convert.py` has slant-plane amplitude extraction; full geocoding
  of the complex products is the remaining gap.)

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

Now shipped from the same review: the **PyPI release workflow with trusted
publishing** (`.github/workflows/release.yml` — OIDC, no stored token, tag/version
guard) plus a **single-sourced version** (hatchling dynamic version from
`__version__`), the **`py.typed` marker**, and the fix to the
`theminiverse`/`reesehammer` **repository-identity mismatch** across
`pyproject.toml`, `CHANGELOG.md`, and `CONTRIBUTING.md`. The one remaining
step is a maintainer action, not code: register the PyPI Trusted Publisher for
`reesehammer/umbra-py` and cut the `v0.1.0` GitHub Release (which fires the
workflow and claims the name). Still open otherwise: a SessionStart hook /
permission allowlist for remote agent sessions.
