# umbra-py Strategy — Maximally Valuable to Umbra and the SAR Ecosystem

*A living strategy document: why this project exists, where it sits in the
ecosystem, and the ranked workstreams that make it valuable to Umbra (the
company) and to everyone working with SAR open data. Update the status lines
as things land; add new ideas at the bottom rather than rewriting history.
Companion docs: [`CODEBASE_ANALYSIS.md`](CODEBASE_ANALYSIS.md) (code-level
priorities), [`AI_INTEGRATION_IDEAS.md`](AI_INTEGRATION_IDEAS.md) (AI/MCP
direction), [`DEMO_APP_GAPS.md`](DEMO_APP_GAPS.md) (demo-app readiness).*

*Last updated: 2026-07-16.*

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
>
> **Update:** the **archive scene-embedding capability has shipped**
> (`AI_INTEGRATION_IDEAS.md` C5 — the last open AI item, so every idea in that
> document is now built). `umbra embed` (`umbra_py.embed`, `[ai]` + `[viz]` extras)
> embeds one quicklook per acquisition and ranks scenes by cosine similarity, so
> `umbra embed similar <url>` finds acquisitions that *look like* a given one and
> `umbra embed search "a flooded field"` finds them from a text description. This
> is the project's novelty (§3) at its sharpest: a **genuinely new capability over
> the archive** — visual similarity search that nothing in the Umbra ecosystem
> offers, and that no amount of metadata search can fake. It also stays graceful
> under the "moat is leased" risk (§3): it layers on the same discovery substrate
> the whole project is built on, and the embedding table is exactly the kind of
> artifact worth *offering upstream* (5.2) — publish it beside the nightly index and
> the ecosystem gets scene-similarity search for free. It preserves the boundary
> the scientific audience needs (§3): the only model call is turning an image or a
> query into a vector (injectable, `[ai]` extra, never implicit), while rendering,
> storage, cosine ranking and thresholding are stdlib-only — and the vectors live
> in a sidecar `catalog.embed.db`, never inside the deterministic `catalog.db`, so a
> core install is never asked to carry model-derived data it can't use. With C5
> done, the remaining strategic gaps are unchanged and non-AI: the SICD → geocoded
> COG format work (5.5) and the maintainer-side adoption moves (5.3 registries, 5.6
> talking to Umbra).
>
> **Update:** the **HTTP/download path is now hardened** (`CODEBASE_ANALYSIS.md`
> P1 #5/#6, §3.2/§4.3 — supporting infrastructure, §7). Strategy is only as
> credible as the project's reliability, and the library's core job is fetching
> data from a public bucket: `_http.default_session()` now retries transient S3
> failures with backoff (so a single 503 no longer fails a multi-minute index
> build — every caller inherits it), and `download_url` verifies each download
> against `Content-Length` and validates a resume with `If-Range` (so a
> truncated body fails loudly instead of renaming a silently-incomplete file, and
> a changed remote object restarts cleanly instead of splicing). Not a new
> capability — the reliability floor under every capability already shipped. The
> strategic gaps above are unchanged.
>
> **Update:** **live search is now concurrent** (`CODEBASE_ANALYSIS.md` §4.2 /
> P1 #9 — supporting infrastructure, §7). Discovery is the moat (§3): the one
> thing with no substitute is search over a catalog that has no search API, so
> how *fast* that search feels is strategy, not polish. The catalog walk's last
> serial bottleneck was the per-acquisition `*.stac.v2.json` sidecar GET — a
> 50-item search paid ~50 latencies back to back. `UmbraCatalog._walk_task` now
> fetches those sidecars through a bounded thread pool (mirroring the gallery's
> proven pattern) while yielding in the same deterministic acquisition-date
> order, so a task's wall time collapses from N serial fetches toward N/workers
> with no change to *what* is returned. Not a new capability — the responsiveness
> floor under the core operation every user and agent reaches for first. The
> strategic gaps above are unchanged: SICD → geocoded COG (5.5) and the
> maintainer-side adoption moves (5.3 registries, 5.6 talking to Umbra).
>
> **Update:** the **first self-serve interactive demo application has shipped** —
> `umbra demo` (`DEMO_APP_GAPS.md` G3/G4, Path A's front end). Every prior visual
> command emits a *one-shot* artifact; the demo-gap analysis names the missing
> piece as an *application* — a self-serve, full-catalog, interactive UX — and
> this is that piece, delivered in the library's own grain: one self-contained
> HTML page (Leaflet + `Leaflet.markercluster`, browser-side, no Python extra) over a whole
> gathered slice of the catalog, with the client-side controls the doc flagged as
> absent (free-text site search, a date-range slider, product-type chips), marker
> **clustering** that scales past the Folium polygon ceiling, and a
> click-to-quicklook SAR overlay reusing the proven `_lazy_imagery` geotiff.js
> driver. It routes through the shared `_gather_items` helper, so `--local` builds
> it from the prebuilt index in milliseconds — the "no multi-minute walk in the
> user's critical path" a demo needs. This is the sharpest funnel-widener since
> the notebooks (§1): the "make Umbra's SAR feel as approachable as Sentinel-1"
> thesis is best sold by a page a curious analyst can *explore*, not a command
> they must run — and it is the shared front end a Pages-hosted showcase (the
> natural companion to 5.3's adoption moves) now builds on. It preserves the
> project's grain and testability (§3): the generator is stdlib-only and fully
> offline-testable, remote metadata reaches the page only as JSON placed via
> `textContent`/`setAttribute` (never parsed as HTML), and it layers on the same
> discovery substrate the whole project rests on, so it stays graceful under
> upstream obsolescence. Remaining demo-app gaps are unchanged and additive:
> PMTiles tiling for the *whole* acquisition set and R4's on-demand
> change/swipe/timescan renders from the UI (`DEMO_APP_GAPS.md` Path A step 3 /
> Path B). The higher-level strategic gaps are also unchanged: SICD → geocoded COG
> (5.5) and the maintainer-side adoption moves (5.3 registries, 5.6 talking to
> Umbra).
>
> **Update:** the **on-demand render endpoints have shipped on `umbra serve`**
> (`DEMO_APP_GAPS.md` R4 / Path B step 2 — the server side of the last self-serve
> demo requirement). `umbra serve` had restored *discovery* (a STAC search API
> over a catalog with none); the demo-gap analysis's remaining self-serve
> requirement was *triggering the visual products over any site*, not just a
> curated set baked at build time. The server now does that: `GET
> /artifacts/quicklook/{id}.png`, `POST /artifacts/change` and `POST
> /artifacts/timescan` resolve the acquisitions from the same `CatalogIndex` and
> render them by wrapping the existing `viz` functions unchanged, caching each
> PNG to disk keyed by its inputs. It is the sharpest demo-critical-path move
> since the demo page itself (§1): the "make Umbra's SAR feel as approachable as
> Sentinel-1" thesis is best sold by a page a curious analyst can *act* on —
> click a site, see it change over time — and this is the backend that closes the
> loop over the whole archive rather than a handful of pre-baked showcases. It
> preserves the project's grain and testability (§3): the renderers are
> **injectable**, so the routes are unit-tested in the core install with no
> network and no `viz` extra (the same discipline the STAC document builders
> already hold), and the endpoints are opt-out (`--no-artifacts`) for a public
> instance that wants to bound COG-streaming egress — the guardrail (§6) against
> being the reason their S3 bill spikes. It layers on the same discovery substrate
> the whole project rests on, so it stays graceful under upstream obsolescence.
> Remaining demo gaps are unchanged and additive: the front-end "run this analysis
> here" wiring that *calls* these endpoints, a `swipe` endpoint, async job
> semantics for the longest renders, and the full-acquisition-set PMTiles tiling
> (`DEMO_APP_GAPS.md` Path A step 3). The higher-level strategic gaps are also
> unchanged: SICD → geocoded COG (5.5) and the maintainer-side adoption moves
> (5.3 registries, 5.6 talking to Umbra).
>
> **Update (2026-07-16):** the self-serve demo loop is now closed. `umbra serve`
> gained `POST /artifacts/swipe` (an interactive before/after HTML page,
> served from its own cache entry alongside the three PNG composites) and a
> permissive read-only CORS policy, and `umbra demo --server-url` wires an
> "Analyze this view" panel that POSTs the currently-filtered acquisitions to
> the change/timescan/swipe endpoints and renders the result in place — the R4
> "run this analysis here" affordance over *any* site (`DEMO_APP_GAPS.md` Path B
> step 3). What remains under the demo heading is only async job semantics for
> the longest renders and the full-acquisition-set PMTiles tiling.
>
> **Update (2026-07-16):** the **async job semantics for the longest renders have
> shipped** (`DEMO_APP_GAPS.md` Path B step 2 — the productized shape the
> synchronous render endpoints deferred as an honest first slice). A composite
> request can opt in to `"async": true` and get a `202 Accepted` + a job id back
> immediately instead of holding the request for the whole render; it then polls
> `GET /jobs/{id}` (`queued` → `running` → `succeeded` | `failed`) and fetches the
> finished artifact from `GET /jobs/{id}/result`. The move that keeps it in the
> project's grain (§3): there is **no separate result store** — the render still
> writes the same content-addressed disk cache the synchronous path uses, so a
> completed job's result *is* a cache entry, and an async request whose key is
> already cached returns an already-`succeeded` job with no work. It preserves the
> determinism and testability the scientific audience needs (§3): frame resolution
> and validation stay synchronous (a bad request is still a fast `400`, never a
> doomed job; a failed render is a `failed` job whose result endpoint mirrors the
> sync status), and the queue's executor is **injectable**, so the whole path is
> offline-testable with no wall-clock timing — the same discipline the injectable
> renderers already hold. This productizes the demo's server backend for the
> renders that actually take tens of seconds (a large `max_size`, a long
> timescan). With it, the only remaining item under the demo heading is the
> full-acquisition-set PMTiles tiling (Path A step 3); the higher-level strategic
> gaps are unchanged: SICD → geocoded COG (5.5) and the maintainer-side adoption
> moves (5.3 registries, 5.6 talking to Umbra).
>
> **Update (2026-07-16):** the **SICD → geocoded COG one-liner has shipped**
> (`STRATEGY.md` 5.5 — the higher-level code gap named at the foot of nearly
> every update above). Every path to the open data assumes the ``GEC`` asset is
> already a geocoded COG; the complex ``SICD`` product is not — it lives in the
> radar slant plane, so it does not open on a map, in QGIS, or in the
> xarray/rioxarray stack without hand-rolled geocoding, which is exactly the
> "same 500 lines of glue" the thesis (§1) says drives people away. `umbra
> convert SRC DST` (and `umbra_py.convert.sicd_to_geocoded_cog`) closes that:
> it detects amplitude and warps it onto a north-up EPSG:4326 cloud-optimized
> GeoTIFF using SICD's *own* image-projection model — a lattice of ground control
> points from `project_image_to_ground_geo` — so the sensor geometry, not a naive
> corner-stretch, places the pixels. It stays in the project's grain and
> testability (§3): the geocoding core (`_warp_gcps_to_cog`) is deliberately free
> of any sarpy dependency, so it is offline-tested with a plain array and
> hand-built GCPs against real `rasterio`, and the SICD read → amplitude → GCP →
> warp path is exercised end to end with a faked reader (the same injectable
> discipline the renderers and STAC builders already hold) — `convert.py` went
> from zero tests to a full offline suite in the `[convert]` extra CI job. The
> geocoding is an honest flat-earth first slice (pixels on the scene's HAE
> plane): exact over flat terrain, adequate for map placement elsewhere, and
> `--slant-plane` still emits the prior ungeoreferenced amplitude for quick
> inspection. This directly serves the ML/analytics audience 5.5 targets (Umbra
> data becomes trivially loadable) and unblocks the `04_sicd_amplitude` notebook
> flagged in 5.4. What remains under 5.5 is full terrain orthorectification (a
> DEM, MultiRTC interop) and RTC recipes; the other higher-level gaps are
> unchanged: the demo's full-acquisition-set PMTiles tiling (Path A step 3) and
> the maintainer-side adoption moves (5.3 registries, 5.6 talking to Umbra).
>
> **Update (2026-07-16):** the **whole-catalog PMTiles tiling has shipped** —
> `umbra tiles` (`DEMO_APP_GAPS.md` Path A step 3, the last open code gap under
> the demo heading). Discovery is the moat (§3), and the demo thesis (§1 — "make
> Umbra's SAR feel as approachable as Sentinel-1") is best sold by a map a
> curious analyst can *explore*; but every prior map surface embeds its features
> in the page (Folium polygons in `umbra map`, an inline JSON blob in `umbra
> demo`), which stops being fast at the *whole* acquisition set — the one view
> that most says "this is a real archive." `umbra tiles` (`umbra_py.pmtiles`)
> closes that: it pre-cuts the catalog's acquisition centroids into a vector-tile
> pyramid and packages it as a single [PMTiles](https://github.com/protomaps/PMTiles)
> file, so a map fetches only the tiles in view and stays fast at any scale, and
> the file drops straight onto Pages or into a bucket — no tile server. The move
> that keeps it in the project's grain and testability (§3): the demo-gap doc
> sketched this as `export GeoJSON → tile with tippecanoe` (an external binary),
> but because the geometry is *points*, the whole encoder — the Mapbox Vector
> Tile protobuf and the PMTiles v3 container — is **pure standard library**, so it
> runs in a core install and is fully offline-tested by decoding its own output
> (and verified against the reference `pmtiles` / `mapbox-vector-tile` readers) —
> the same discipline `export` and the STAC builders hold. `--viewer` emits a
> self-contained MapLibre GL page over the archive (the same OpenStreetMap
> basemap and mandatory CC-BY attribution the Leaflet demo uses), complementing
> `umbra demo` rather than replacing it: `demo` for the interactive
> filter-and-click slice, `tiles` for the fast zoom-anywhere whole-archive view.
> It also stays graceful under the "moat is leased" risk (§3): the `.pmtiles`
> file is exactly the kind of artifact worth *offering upstream* (5.2) — publish
> it beside the nightly `catalog.db` and the ecosystem gets a whole-catalog
> basemap for free. With Path A step 3 landed, the remaining strategic gaps are
> non-demo and largely non-code: 5.5's full terrain orthorectification (a DEM,
> MultiRTC interop) and RTC recipes, and the maintainer-side adoption moves (5.3
> registries, 5.6 talking to Umbra).
>
> **Update (2026-07-16):** the **generated HTML artifacts are now hardened
> against injection from remote metadata** (`CODEBASE_ANALYSIS.md` §3.1 —
> supporting infrastructure, §7). Strategy is only as credible as the project's
> reliability, and the artifacts a curious analyst opens locally (the maps,
> galleries, swipe/change pages, and the `umbra view` / `umbra demo` explorers)
> are the funnel's front door — but they interpolate strings that come from
> remote STAC JSON, and every discovery verb accepts *arbitrary* item URLs, so a
> hostile document could inject a `<script>` or a `javascript:` link into a page
> a user then opens from `file://`. A shared, dependency-free
> `_html.safe_href()` (scheme allowlist + attribute-escaping) is now the single
> gate for every clickable remote link, and every remote-derived string is
> escaped before it reaches generated HTML — across `viz`, `_html`, `viewer`,
> and `demo`, with regression tests for the escaping and the `javascript:`
> rejection. Not a new capability — the trust floor under the shareable outputs
> the whole funnel depends on (§3's "trust the scientific audience needs"). The
> strategic gaps above are unchanged.

> **Update (2026-07-16):** the **catalog index now refreshes incrementally** —
> `umbra index update` / `CatalogIndex.update` (`CODEBASE_ANALYSIS.md` §4.4, the
> "keep the crawl incremental" guardrail in §6). Discovery is the moat (§3), and
> the prebuilt, published index is how that moat reaches a fresh install without a
> multi-minute crawl (`umbra index fetch`, shipped). The missing half was
> *staying* fresh cheaply: a full `umbra index build` fetches a sidecar for every
> acquisition in the catalog — the N+1 round trips that dominate a crawl — so
> refreshing a week-old snapshot re-read almost everything unchanged. `update`
> closes that: it reads the newest acquisition date already indexed and re-walks
> only from there (minus a small `--overlap-days` window for near-real-time
> publish lag), so a weekly refresh reads just the new passes and upserts them
> exactly as `build` does. It is pure funnel-widening infrastructure (§1): the
> user who bootstrapped from the weekly snapshot now catches up in seconds rather
> than re-downloading it, and the guardrail that the crawl stay polite and
> incremental (§6) is now something a scheduled job can actually honor. It holds
> the project's grain and testability (§3): no model, no new dependency, the
> injectable catalog keeps the whole path offline-tested, and the bound's honest
> limitation (acquisition date, not publish date — so back-dated late arrivals
> want a widened window or a full build) is spelled out rather than hidden. The
> published weekly snapshot is deliberately left as a full rebuild so it stays
> authoritative. The remaining strategic gaps are unchanged and largely non-code:
> 5.5's full terrain orthorectification (a DEM, MultiRTC interop) and the
> maintainer-side adoption moves (5.3 registries, 5.6 talking to Umbra).

> **Update (2026-07-16):** **visual similarity search is now conversational** —
> `umbra-mcp` gained `find_similar` / `find_similar_text` tools (plus a
> `find-similar-scenes` prompt) surfacing the shipped `umbra embed` C5 capability
> (`AI_INTEGRATION_IDEAS.md` §C5). This puts the project's *sharpest* novelty (§3 —
> "a genuinely new capability over the archive that nothing in the Umbra ecosystem
> offers, and that no amount of metadata search can fake") on the highest-leverage
> surface it has: an MCP client can now say "find scenes that look like this flooded
> field" and get back the closest archived acquisitions as cards whose STAC `href`
> feeds straight into the existing `quicklook` / `change_composite` tools — the
> search that lives in the pixels, closing the discover-then-view loop in one
> conversation. It is a direct funnel-widener on the highest-leverage surface (§1),
> reusing `SceneEmbeddingIndex` unchanged and holding the determinism boundary and
> testability the scientific audience needs (§3): the tools gate on a prebuilt
> sidecar `catalog.embed.db` and the `[ai]` key, the only model call is the
> injectable embedder, and the whole path is offline-tested with a stand-in embedder
> and renderer. It stays graceful under the "moat is leased" risk (§3): the
> embedding table it queries is exactly the artifact worth *offering upstream* (5.2)
> — publish it beside the nightly index and the ecosystem gets scene-similarity
> search over MCP for free. The remaining strategic gaps are unchanged and largely
> non-code: 5.5's full terrain orthorectification (a DEM, MultiRTC interop) and the
> maintainer-side adoption moves (5.3 registries, 5.6 talking to Umbra).

> **Update (2026-07-16):** the **catalog index is now schema-versioned**
> (`CODEBASE_ANALYSIS.md` §4.5 / P1 #10 — supporting infrastructure, §7).
> Discovery is the moat (§3), and the way that moat now reaches a fresh install
> without a multi-minute crawl is the *published* `catalog.db` snapshot users
> `umbra index fetch` — which means the index is no longer a private cache but a
> distributed artifact every `--local` path, the MCP server, `umbra serve`,
> `umbra demo` and `umbra tiles` all consume. The one thing that turns a future
> improvement of that index (the demo-oriented denormalizations
> `DEMO_APP_GAPS.md` G2 wants — a precomputed centroid, a cached place label — or
> an R\*Tree spatial upgrade) from a clean migration into a confusing break was
> the missing schema-version marker: with DBs already in the wild, the next
> schema change would fail every deployed snapshot with no explanation.
> `CatalogIndex` now stamps `PRAGMA user_version` on create and checks it on
> open — a fresh or pre-versioning database (`user_version 0`, which every
> current snapshot reads) is adopted in place and stamped, while a database
> written by a *newer* umbra-py, or a lower version with no migration path,
> raises a self-describing `IndexSchemaError` (a clean CLI `error: …`) instead of
> being silently misread. Not a new capability — the guardrail (§6 "keep the
> crawl incremental" / reliability floor, §7) that keeps the prebuilt-index
> distribution the whole discovery story now rests on evolvable. It holds the
> project's grain and testability (§3): no model, no new dependency, and the
> whole fresh/legacy/newer/older path is offline-tested against real SQLite
> databases — mirroring the same `PRAGMA user_version` discipline the
> `catalog.embed.db` sidecar already held. The remaining strategic gaps are
> unchanged and largely non-code: 5.5's full terrain orthorectification (a DEM,
> MultiRTC interop) and the maintainer-side adoption moves (5.3 registries, 5.6
> talking to Umbra).

> **Update (2026-07-16):** the **structured `--json` success output is now
> complete across the CLI, finishing Tier A of the AI plan**
> (`AI_INTEGRATION_IDEAS.md` §A1 — supporting infrastructure, §7). "Agents are
> the new first-time users" (§3's AI thesis) is only true to the degree the tools
> report back in a shape an agent can consume: the failure side shipped as the
> machine-readable error contract, but the *success* side was still partial —
> most commands ended in a human "Wrote … to …" line an agent had to parse. This
> closes that: `umbra download --json` emits a `[{asset, path, bytes, sha256}, …]`
> array (each file hashed with a streaming SHA-256, so a caller verifies what it
> fetched without re-reading it), `umbra index info --json` emits the index
> summary, and the five render commands (`change`/`timescan`/`swipe`/`gallery`/`map`)
> emit a `{output, items_used, parameters}` manifest naming the artifact, the
> acquisitions it was built from, and the settings used. It holds the project's
> grain and testability (§3): human progress, warnings and the `--place`
> "Resolved …" status line all go to stderr so stdout carries the JSON object
> alone; the three new shapes are published as public API under `docs/schemas/`
> (the same compatibility rules as `__all__`); and the whole surface is
> offline-tested with injected renderers/downloads — no model, no network, no
> `viz` extra. Pure funnel-widening (§1): the query-and-render surface newcomers
> and agents reach for now answers in a shape a script or an LLM can branch on.
> The remaining strategic gaps are unchanged and largely non-code: 5.5's full
> terrain orthorectification (a DEM, MultiRTC interop) and the maintainer-side
> adoption moves (5.3 registries, 5.6 talking to Umbra).

> **Update (2026-07-16):** the **catalog index gained a keyed single-item
> lookup** — `CatalogIndex.get(item_id)` (`CODEBASE_ANALYSIS.md` §4.5 —
> supporting infrastructure, §7). Discovery is the moat (§3), and the way that
> moat now reaches every consumer is the *published* `catalog.db` snapshot that
> `--local` search, `umbra serve`, `umbra demo` and the MCP server all read.
> Listing that catalog was covered (`search`); the one primitive still answered
> by a *scan* was fetching a single acquisition by id — `umbra serve`'s
> `/collections/{id}/items/{item_id}` filtered an id-scoped search over the
> ordered result set, fine at today's scale but a full walk of the page as the
> snapshot grows. `get()` closes that with an `idx_items_id`-backed point lookup
> (the retrieval complement to `search`'s listing), and the serve item endpoint
> resolves through it (`serve.get_one`), falling back to the id-filtered search
> only for the live source that can't do a keyed read. Not a new capability —
> the point-lookup floor under the retrieval interface every `--local` consumer
> shares (§7). It holds the project's grain and testability (§3): no model, no
> new dependency, the whole path offline-tested against real SQLite, and the
> index is *additive* — added with `CREATE INDEX IF NOT EXISTS`, so existing and
> fetched snapshots gain it on the next open with no `PRAGMA user_version` bump,
> the first exercise of the additive-schema path the schema-version marker was
> landed to enable. The remaining strategic gaps are unchanged and largely
> non-code: 5.5's full terrain orthorectification (a DEM, MultiRTC interop) and
> the maintainer-side adoption moves (5.3 registries, 5.6 talking to Umbra).

> **Update (2026-07-16):** **catalog search is now read-through** —
> `CatalogIndex.search_live` / `umbra search --local --live`
> (`CODEBASE_ANALYSIS.md` §4.4 / P3 #21 — the "make the index the default path"
> gap, and the last open item under §4.4). Discovery is the moat (§3), and the
> way that moat now reaches every consumer is the *published* `catalog.db`
> snapshot users `umbra index fetch`; the tension left was that a local search
> was instant but only as fresh as the snapshot, while a live search was current
> but re-walked the whole bucket every call. `search_live` closes it: it answers
> the whole query from the index *and* walks only acquisitions at or after the
> index's freshness horizon (its newest indexed `acq_date` minus an overlap),
> merges the two streams in the usual `(task, acq_date)` order, de-duplicates by
> sidecar href, and — with the default `refresh=True` — upserts each new
> acquisition the delta discovers as it is yielded, so the cache warms and the
> next call walks even less. It is pure funnel-widening infrastructure (§1): the
> user who bootstrapped from the weekly snapshot now gets *fast and fresh* from
> one command instead of choosing between them, and the guardrail that the crawl
> stay polite and incremental (§6) is honored — the delta reuses the same
> recent-only sidecar pruning `umbra index update` already relies on. It holds
> the project's grain and testability (§3): no model, no new dependency, the
> injectable catalog keeps the whole path offline-tested, and it is delivered as
> an explicit read-through method + `--live` flag rather than an implicit mode
> change to `search`, so a plain `search` still means exactly what it did and a
> read-only shared snapshot disables the write-back instead of failing. The
> remaining strategic gaps are unchanged and largely non-code: 5.5's full terrain
> orthorectification (a DEM, MultiRTC interop) and the maintainer-side adoption
> moves (5.3 registries, 5.6 talking to Umbra).
>
> **Update (2026-07-16):** the **whole-catalog PMTiles basemap is now published
> and fetchable** (`STRATEGY.md` 5.2, `DEMO_APP_GAPS.md` Path A step 3). `umbra
> tiles` shipped the *encoder*; what a fresh install still lacked was the built
> artifact — it had to crawl the bucket, build an index, and tile it before
> seeing a whole-catalog map. The weekly `publish-index.yml` workflow now tiles
> the freshly built index (`umbra tiles --local`, no second crawl) into a
> single-file `catalog.pmtiles` and a `catalog.html` MapLibre viewer over it, and
> uploads both to the rolling `catalog-index` release beside `catalog.db` /
> `umbra-open-data.parquet`. The consume side is the exact visual sibling of
> `umbra index fetch`: `pmtiles.fetch_prebuilt_pmtiles()` and a new `umbra tiles
> --fetch` mode pull the published archive (resume-safe `download_url`, default
> path beside the cached index, `--viewer` for a ready-to-open page), so a fresh
> install — or a Pages showcase — gets a fast, zoom-anywhere map of the *entire*
> archive with zero tiling. It stays in the project's grain and testability (§3):
> stdlib-only, no new dependency, fully offline-tested against a mocked release
> download and a round-tripped archive, and delivered as an explicit `--fetch`
> mode rather than a change to the build path. This is precisely the static,
> host-anywhere artifact 5.2 wants to *offer upstream* — publish the `.pmtiles`
> next to `catalog.json` and the ecosystem gets a whole-catalog basemap for free.
> The remaining strategic gaps are unchanged and largely non-code: 5.5's full
> terrain orthorectification (a DEM, MultiRTC interop) and the maintainer-side
> adoption moves (5.3 registries, 5.6 talking to Umbra).
>
> **Update (2026-07-16):** **DEM terrain orthorectification has shipped** —
> `umbra convert --dem` / `sicd_to_geocoded_cog(dem=…)` (`STRATEGY.md` 5.5, the
> higher-level code gap named at the foot of nearly every update above). The SICD
> geocoder shipped as an honest *flat-earth* first slice: it places every pixel on
> the scene's single height-above-ellipsoid plane, exact over flat terrain but
> mislocating relief — a hilltop is placed where the radar ray meets the plane,
> not where it meets the ground. `--dem PATH` closes that: given any
> rasterio-readable elevation model (a Copernicus/SRTM COG works), each
> ground-control point is *walked onto the terrain surface* by the standard ortho
> fixed-point iteration — project at a height, look up the DEM there, reproject,
> repeat until the height it lands on stops moving — so the scene is genuinely
> terrain-orthorectified rather than flat-projected. It directly serves the
> ML/analytics audience 5.5 targets (Umbra data becomes trivially loadable *with
> correct geolocation over relief*) and removes the largest remaining "same 500
> lines of glue" the thesis (§1) says drives people away: hand-rolled DEM
> geocoding. It stays in the project's grain and testability (§3): the refinement
> loop and the DEM lookup are both **injectable** (`project` / `sample_height`
> callables), so the whole path is offline-tested with plain callables and a
> hand-written DEM raster — no sarpy DEM plumbing — against convergence to a
> closed-form terrain fixed point, the flat-DEM and off-DEM (no-coverage) fallbacks,
> the DEM sampler (ramp read, out-of-bounds/nodata masking, CRS reprojection), and
> the end-to-end + CLI paths; the sarpy-facing HAE projector batches points sharing
> a binned height into one call so the common early iterations stay a single
> projection. The `--dem` mode supersedes `--projection`, and where the DEM has no
> coverage a point falls back to the scene reference height rather than tearing.
> What remains under 5.5 is the vertical-datum/geoid niceties and MultiRTC/RTC
> interop (radiometric terrain correction, a different job from geometric
> orthorectification); the other higher-level gaps are unchanged and non-code: the
> maintainer-side adoption moves (5.3 registries, 5.6 talking to Umbra).
>
> **Update (2026-07-16):** the **DEM auto-fetch has shipped** — `umbra convert
> --dem auto` / `sicd_to_geocoded_cog(dem="auto")` (`STRATEGY.md` 5.5, closing the
> last convert-side glue step named in `TODO.md`). Terrain orthorectification
> landed as `--dem PATH`, but that still made the user go find, download, and
> mosaic the right elevation tiles for the scene — "the same 500 lines of glue"
> the thesis (§1) says drives people away, just relocated from geocoding to DEM
> wrangling. `umbra_py.dem` removes it: `--dem auto` projects the scene's four
> image corners to a geographic bbox, resolves the 1°×1° Copernicus GLO-30 tiles
> covering it, pulls them from the public AWS Open Data bucket (skipping the
> all-ocean gaps Copernicus omits with a 404, merging several into a mosaic), and
> hands the result straight into the shipped terrain-ortho path — so a curious
> analyst types one flag and gets a correctly geolocated scene over relief with no
> DEM hunt. It holds the project's grain and testability (§3): the tile math (id
> naming, bbox coverage, URL building) is **pure standard library** and
> offline-tested with no network, the fetch reuses the resume-safe `download_url`
> and is **injectable**, so the skip/merge/raise behaviour is covered with a
> stub downloader (only the multi-tile `rasterio.merge` mosaic touches the
> `[convert]` extra), and tiles are cached under the same XDG cache dir the index
> uses so a second conversion over the same area re-downloads nothing. It stays
> graceful under the "moat is leased" risk (§3): Copernicus GLO-30 is a public,
> host-anywhere collection, so this depends on nothing Umbra-specific. This
> directly serves the ML/analytics audience 5.5 targets (Umbra SICDs become
> trivially loadable *and* correctly geolocated over terrain in one command). What
> remains under 5.5 is the vertical-datum/geoid niceties and MultiRTC/RTC interop;
> the other higher-level gaps are unchanged and non-code: the maintainer-side
> adoption moves (5.3 registries, 5.6 talking to Umbra).
>
> **Update (2026-07-16):** the **vertical-datum / geoid handling has shipped** —
> `umbra convert --geoid` / `sicd_to_geocoded_cog(geoid=…)` (`STRATEGY.md` 5.5,
> the geocoding nicety named at the foot of the DEM-orthorectification updates
> above). Terrain orthorectification walks each control point onto the DEM
> surface, but it fed the sampled height straight into SICD's projection — and
> global DEMs (Copernicus GLO-30, SRTM) quote height above the **EGM geoid**,
> while SICD projects against the **ellipsoid**. That mismatch is the geoid
> undulation `N` (up to ~±100 m worldwide), and treating an orthometric height as
> if it were ellipsoidal mislocates relief by roughly `N·tan(look_angle)` on the
> ground — the same systematic error terrain orthorectification exists to remove,
> reintroduced through the vertical datum. `--geoid PATH` closes it: given any
> rasterio-readable undulation grid (an EGM96/EGM2008 GeoTIFF), it adds `N` at each
> point (`hae = orthometric + N`) before projecting, for survey-grade geolocation.
> It directly serves the ML/analytics audience 5.5 targets (Umbra SICDs become
> not just loadable and terrain-corrected but *vertically referenced correctly*),
> and it holds the project's grain and testability (§3): the correction is a **pure
> composition** of two injectable `(lons, lats) → heights` samplers
> (`_geoid_corrected_sampler`) — the geoid grid is read with the very same
> `_dem_height_sampler` the DEM uses — so the whole path is offline-tested with a
> hand-written grid, with **no new dependency and no packaged EGM data**. It is an
> honest optional layer: it requires `--dem` (it corrects DEM heights, so it is a
> hard error without one), degrades gracefully to the uncorrected height where the
> grid has no coverage, and without it the output is unchanged (correct to the
> local geoid–ellipsoid separation, ample for map placement). What remains under
> 5.5 is an optional `--geoid auto` (fetch a matching EGM grid for the scene, the
> vertical sibling of `--dem auto`) and MultiRTC/RTC interop (radiometric terrain
> correction, a different job); the other higher-level gaps are unchanged and
> non-code: the maintainer-side adoption moves (5.3 registries, 5.6 talking to
> Umbra).

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
- ✅ **Whole-catalog basemap now published too:** the same weekly workflow
  tiles the freshly built index into a single-file `catalog.pmtiles` (plus a
  `catalog.html` MapLibre viewer over it) and uploads both to the
  `catalog-index` release. `umbra tiles --fetch` / `fetch_prebuilt_pmtiles()`
  pull it — the visual sibling of `umbra index fetch`, and exactly the kind of
  static, host-anywhere artifact worth offering upstream. A fresh install (or a
  Pages showcase) now gets a fast, zoom-anywhere map of the *entire* archive
  with no local tiling step.
- ⬜ **Then offer it upstream:** "here's the pipeline; host the parquet (and the
  `.pmtiles` basemap) next to `catalog.json` in your bucket and the whole
  ecosystem gets a search API — and a whole-catalog map — for free." If Umbra
  adopts it, this project is part of their data program's infrastructure.

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
- ✅ **SICD → geocoded COG shipped** (`umbra convert` / `umbra_py.convert`,
  `[convert]` extra): `sicd_to_geocoded_cog()` detects amplitude from the
  complex product and warps it onto a north-up EPSG:4326 cloud-optimized GeoTIFF
  using SICD's own image-projection model (a lattice of ground control points),
  so the scene opens straight onto a map, in QGIS, or as a georeferenced array
  via `to_xarray`. `umbra convert SRC DST` geocodes by default; `--slant-plane`
  keeps the prior ungeoreferenced amplitude for quick inspection. The geocoding
  is a flat-earth first slice (pixels on the scene's HAE plane): exact over flat
  terrain, adequate for map placement elsewhere.
- ✅ **DEM terrain orthorectification shipped** (`umbra convert --dem` /
  `sicd_to_geocoded_cog(dem=…)`): given any rasterio-readable elevation model, each
  ground-control point is walked onto the terrain surface by the standard ortho
  fixed-point iteration (project → sample the DEM → reproject, until it converges),
  so relief lands in its true ground position instead of on a single flat height
  plane. `--dem` supersedes `--projection`; off-DEM points fall back to the scene
  height. The refinement loop and DEM lookup are injectable, so the whole path is
  offline-tested with plain callables and a hand-written DEM raster.
- ✅ **DEM auto-fetch shipped** (`umbra convert --dem auto` / `umbra_py.dem`):
  `dem="auto"` resolves the 1°×1° Copernicus GLO-30 tiles covering the scene's
  projected footprint, pulls them from the public AWS Open Data bucket (skipping
  the all-ocean gaps that 404, merging several into a mosaic), and
  terrain-orthorectifies against the result — so terrain orthorectification no
  longer starts with hand-finding a DEM. The tile math (id naming, bbox coverage)
  is stdlib-only and offline-tested; the fetch reuses the resume-safe
  `download_url` and is injectable, so the whole path is covered without network.
- ✅ **Vertical-datum / geoid handling shipped** (`umbra convert --geoid` /
  `sicd_to_geocoded_cog(geoid=…)`): global DEMs quote height above the EGM geoid,
  but SICD projects against the ellipsoid, so an optional undulation grid adds the
  geoid–ellipsoid separation `N` to each sampled DEM height (`hae = orthometric +
  N`) before projecting — survey-grade placement over relief. It requires `--dem`,
  degrades gracefully off the grid, and is a pure composition of two injectable
  height samplers, so it is offline-tested with a hand-written grid and needs no
  packaged EGM data.
- ⬜ Remaining geocoding niceties: an optional `--geoid auto` (fetch a matching
  EGM grid like `--dem auto`) and MultiRTC interop; RTC recipes (radiometric
  terrain correction) are still open.

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
