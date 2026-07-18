# umbra-py — Making the Project AI-Native

*Ideas for MCP extensibility, API extensibility, and AI-infused capabilities
that lower the barrier to Umbra open SAR data and unlock workflows that only
became possible with modern AI. Companion to
[`CODEBASE_ANALYSIS.md`](CODEBASE_ANALYSIS.md); this document proposes
direction and design, not code.*

---

## 1. Why this problem space is unusually ripe for AI

Three properties of the Umbra open-data problem make AI integration more than
a checkbox here:

1. **The friction is *interpretive*, not computational.** The hard part of
   using Umbra data is knowing *what to ask for*: which product type (GEC vs
   SICD vs CPHD), which site, which dates, what a dB stretch is, why two
   polarizations shouldn't be compared. These are exactly the questions a
   language model answers well — if the tooling exposes the right verbs.
   `umbra-py` has already collapsed the mechanical friction (search, resume
   downloads, COG streaming); an AI layer collapses the conceptual friction.
2. **The outputs are inherently multimodal.** Quicklooks, change composites,
   timescans, and swipe maps are *images with precise metadata* — the native
   input format of vision-language models. A VLM shown a two-date change
   composite ("green = appeared, magenta = vanished") can describe *what
   changed at a port between January and March* in plain language. The library
   already produces the exact artifacts VLM analysis needs; nothing new has to
   be invented, only connected.
3. **There is no upstream API to compete with.** Umbra publishes no STAC API
   and no search endpoint — this library *is* the query layer. That means an
   MCP server or API façade built here isn't a wrapper around someone else's
   service; it becomes the de-facto programmatic front door to a 17+ TB public
   SAR archive. That is a rare position for a small OSS project.

A useful framing for everything below: **agents are the new first-time
users.** Every design choice that helps Claude/GPT/Gemini operate the library
(structured outputs, stable schemas, self-describing errors) also helps human
newcomers, scripts, and CI.

The ideas are organized in three tiers by ambition. Tier A makes the existing
library legible to AI; Tier B exposes it through AI-native interfaces; Tier C
builds capabilities that assume an AI in the loop.

> **Status (2026-07-15):** the **Tier A / Phase 1 groundwork is shipped** — the
> zero-dependency, deterministic prerequisite every later phase consumes.
> `UmbraItem.to_llm_context()` (A3) returns an explanation-rich context card;
> `umbra_py.llm_context()` / `umbra context` (A2) is the runtime context
> document; `UmbraItem`/`ItemCollection` now implement `__geo_interface__` (B3);
> the determinism boundary (A4) is written into `AGENTS.md`; and `umbra info
> --json` extends the structured-output guarantee (A1).
>
> **The flagship MCP server (B1) is now shipped** — the highest-leverage single
> artifact in this document. `umbra-mcp` (`umbra mcp` / `uvx umbra-mcp`, `[mcp]`
> extra) exposes `search_catalog`/`get_item`/`geocode_place`/`index_stats`/
> `quicklook`/`change_composite`/`timescan`/`download_asset` as MCP tools (the
> imagery tools return PNG image blocks), a `umbra://context` resource, and the
> `monitor-site`/`survey-region` prompts — reusing the A3 cards and the A2
> document exactly as planned.
>
> **Update:** the **`umbra serve` STAC API façade (B2) has now shipped** — the
> browser-facing sibling of the MCP server and the shared foundation the demo
> application (`DEMO_APP_GAPS.md` Path B) was waiting on. `umbra serve` (`[serve]`
> extra) runs a read-only STAC API over the same `CatalogIndex`
> (landing/conformance/collections/items + `GET`/`POST /search`, with a free
> OpenAPI doc at `/docs`), so the standard STAC tooling — `pystac-client`, the
> QGIS STAC plugin, `stac-browser`, leafmap — and OpenAPI-driven agents can now
> query Umbra's open archive.
>
> **Update:** **`umbra serve` now renders artifacts on demand, not just
> search** (`DEMO_APP_GAPS.md` R4 / Path B step 2). The façade grew three render
> endpoints alongside discovery — `GET /artifacts/quicklook/{id}.png`, `POST
> /artifacts/change`, `POST /artifacts/timescan` — that resolve acquisitions from
> the same index and return the library's visual products (§2's "the outputs are
> inherently multimodal" made reachable over plain HTTP for *any* site, not a
> curated set). They wrap the existing `viz` compositors unchanged, cache each PNG
> to disk keyed by its inputs, and — like the STAC document builders — keep the
> render functions **injectable** so the routes are offline-testable in the core
> install. This is the OpenAPI-visible counterpart to the MCP server's
> `quicklook`/`change_composite`/`timescan` image tools (B1): a browser or an
> OpenAPI-driven agent now gets the pictures, not just the metadata, from the
> generated schema alone.
>
> **Update:** the **A2 `llms.txt` docs bundle has now shipped** — the last open
> Phase 2 item, and the *user* agent guide that completes the AI-legible
> surface. `umbra_py.llms_txt()` / `llms_full_txt()` (CLI: `umbra llms-txt
> [--full]`, `[mcp]`/`[serve]` not required — it is stdlib-only) render the
> [llms.txt-convention](https://llmstxt.org/) Markdown a model pulls in to learn
> how to *drive* the library, and the committed `llms.txt` / `llms-full.txt` at
> the repo root are that rendered output (a golden test keeps them in sync).
> `llms-full.txt` is assembled entirely from facts already in the package — the
> A2 `llm_context()` domain document, the live CLI command tree, and each core
> module's explanatory docstring (read via `ast`, so the generator never imports
> a heavy extra). With Phase 2 complete, **Phase 3's Tier C AI-infused
> capabilities (C1 NL search first) are the next critical path.**
>
> **Update:** the **deterministic first step of C1 (natural-language date
> bounds) has now shipped.** `--start` / `--end` — and the `search()` keyword
> arguments and the MCP `search_catalog` tool — accept human date expressions
> (`2024`, `2024-03`, `today`, `yesterday`, `3 months ago`, `last month`,
> `this year`) alongside `YYYY-MM-DD`, resolved by a new stdlib-only
> `umbra_py.dates.parse_date_bound` with plain calendar arithmetic and **no model
> call**. It is bound-aware — a span snaps to its first day as a start and its
> last day as an end, so `--start 2024 --end 2024` is the whole year — and lands
> in the single `_coerce_date` choke point, so `search`, `index build`,
> `change`, `timescan`, `swipe`, `map` and `gallery` all gain it at once. This is
> exactly the "NL in, deterministic filter out, no model required at runtime"
> philosophy §C1 describes; fuzzy task matching and the LLM-planned `umbra ask`
> are the remaining C1 pieces.
>
> **Update:** the **deterministic fuzzy task matching step of C1 has now
> shipped** — the second "NL in, deterministic filter out" resolver. `area=`
> stays a literal case-insensitive substring by default; `fuzzy=True` (CLI
> `--fuzzy` on `search`/`change`/`timescan`/`swipe`/`gallery`, and the MCP
> `search_catalog` tool) widens it to a token-wise match in the new
> stdlib-only `umbra_py.fuzzy` — word-order- and punctuation-independent and
> typo-tolerant, so `"utah centerfield"` or `"centrfield"` still reach
> `"Centerfield, Utah"`. It is a **strict superset** of the substring match (it
> never drops a result), the live (`UmbraCatalog`) and indexed (`CatalogIndex`)
> paths share the one matcher and are tested to agree, and no model is called.
> Semantic aliasing (`"grain storage north dakota"` → `"Beet Piler - ND"`) is
> deliberately left to the future embedding index; the LLM-planned `umbra ask`
> is now the last open C1 piece.
>
> **Update:** the **LLM-planned `umbra ask` has now shipped** — the capstone of
> C1 and the *first feature in the package that calls a model*. `umbra ask "…"`
> (`src/umbra_py/planner.py`, `[ai]` extra) hands the user's sentence plus the
> `llm_context()` document to a configured model (Anthropic or any
> OpenAI-compatible endpoint, user-supplied key, `requests` only — no SDK) and
> gets back the search *parameters* it maps to. The model **only plans**: the new
> `parse_plan` re-validates every field deterministically — dates through
> `parse_date_bound`, product types against `PRODUCT_ASSETS`, the bbox
> range-checked, `place`/`bbox` mutually exclusive — so **nothing the model emits
> becomes a filter without passing the deterministic layer**, and the resolved
> `umbra search` command is printed before it runs (`--run` executes it,
> `--json` emits the plan). This is exactly the honest "the LLM plans; the
> library executes; the user audits" design §C1 describes, and it stays inside
> the determinism boundary (§A4, §6.1): the feature is opt-in behind `[ai]`,
> never runs implicitly, and its planning step is an injectable callable so the
> whole thing is offline-testable with no network. With `umbra ask` done, the one
> remaining C1 piece is the **semantic embedding index** (`sqlite-vec`) — the
> offline, no-round-trip answer to task aliasing that plain string similarity
> can't fake.
>
> **Update:** the **semantic embedding index has shipped** — the last open C1
> piece, so **C1 (natural-language search) is now complete**. `umbra_py.semantic`
> (`[ai]` extra) embeds the catalog index's task names once (`umbra semantic
> build`) and ranks them against a query by cosine similarity (`umbra semantic
> search "grain storage north dakota"` → "Beet Piler - ND"), printing the `umbra
> search --area …` command for the best match to audit before `--run` — the same
> "model proposes, library executes, user audits" boundary as `umbra ask`. The
> **only** model call is turning text into a vector (an injectable `Embedder`,
> default an OpenAI-compatible `/embeddings` endpoint via the already-core
> `requests`); storage, cosine ranking and thresholding are stdlib-only — no
> `numpy`, and deliberately *no* `sqlite-vec` binary dependency (a brute-force
> scan over a few thousand task vectors is instant, and the schema leaves room to
> swap in a vector extension later) — so the whole feature is offline-testable
> with a deterministic stand-in embedder. It stays behind `[ai]` and never runs
> implicitly; the deterministic `--area` / `--fuzzy` matchers remain the default.
> One implementation note: the vectors live in a sidecar `catalog.semantic.db`
> rather than *inside* `catalog.db`, so the deterministic index and its published
> snapshot never carry model-derived data a core install can't use. With C1 done,
> the next critical path is **Tier C's VLM-in-the-loop capabilities** (C2 scene
> description / change narration) and the **B3 example notebooks**.
>
> **Update (2026-07-18):** the **semantic aliasing is now on the MCP surface** —
> `search_catalog(area=…, semantic=True)` (the C1 follow-on named in `TODO.md`).
> The embedding index shipped complete on the CLI, but the agent surface — the
> project's highest-leverage front door (B1) — only reached the deterministic
> `fuzzy=` token match; an agent handed a plain-language *site description* had no
> way to alias it to a task name. The new `semantic=True` flag closes that: it
> resolves `area` to the closest task names by meaning through the shipped
> `SemanticTaskIndex` (`_resolve_semantic_area`), searches the best over the
> chosen backend, and returns `resolved_area` + the ranked `semantic_matches` so
> the resolution is auditable, with a `min_score` threshold (a low-confidence
> description returns an empty audit trail, not an arbitrary pick) and a
> `search-by-description` prompt packaging the workflow. It reuses
> `SemanticTaskIndex` unchanged and holds the same boundary as `umbra semantic`
> (§A4, §6.1): the only model call is the injectable query embedder, gated on a
> prebuilt index and the `[ai]` key (`semantic` and `fuzzy` are mutually
> exclusive), so it never runs implicitly and the whole path is offline-tested in
> `tests/test_mcp_server.py` with a deterministic concept embedder — no key, no
> network, no new dependency.
>
> **Update:** the **first C2 VLM-in-the-loop capability has shipped** — `umbra
> describe` (`src/umbra_py/describe.py`, `[ai]` + `[viz]` extras), the moment the
> library's superpower for AI (its outputs are *images with precise metadata*)
> becomes a product. `umbra describe <item-url>` renders the item's quicklook,
> sends that PNG plus the A3 `to_llm_context()` card to a configured vision model
> (Anthropic or any OpenAI-compatible endpoint, user-supplied key, `requests`
> only — no SDK), and returns a structured `SceneDescription`: `{summary,
> observed_features[], confidence, caveats[]}`. It holds the same determinism
> boundary as `umbra ask` (§A4, §6.1): the picture and the metadata are produced
> deterministically, the model **only interprets** (its reply passes the
> `parse_description` boundary and never becomes a filter, a URL, or a
> coordinate), and the SAR literacy the model needs — backscatter ≠ brightness,
> speckle, layover/shadow — is encoded once in the packaged prompt. Provenance is
> non-negotiable (§6.4): every description carries the CC-BY attribution *and* a
> new `AI_PROVENANCE` note ("AI-generated interpretation … not verified
> measurements"), so a model's reading of radar is never mistaken for ground
> truth. The model call is an injectable `Describer` and the render an injectable
> `Renderer`, so the whole feature is offline-testable with no network. The
> remaining C2 piece is **`umbra change --narrate`** (change narration grounded in
> a per-block dB sidecar); **B3 example notebooks** stay on the critical path.
>
> **Update:** the **second C2 VLM-in-the-loop capability has shipped, completing
> C2** — `umbra change --narrate` (`src/umbra_py/narrate.py`, `[ai]` + `[viz]`
> extras). Where `umbra describe` reads *one* scene, this narrates the *change*
> between two — and it is the design §C2 called for: the narration is grounded in a
> **deterministic per-block dB sidecar**, not just the picture. `compute_change_stats`
> divides the co-registered scene into a coarse grid and measures each block's mean
> *signed* backscatter change in decibels (`20·log10(later) − 20·log10(earlier)`:
> positive = brightened/appeared — the composite's green; negative = dimmed/vanished
> — its magenta) plus the fraction that moved past a threshold. `umbra change
> --narrate` renders the composite once, hands the model *both* the PNG and that
> grid, prints a structured `ChangeNarration` (`{summary, changes[], confidence,
> caveats[]}`), and writes the grid alongside the image as `<out>.narration.json` —
> so the narration cites numbers, not vibes, and every statement is auditable
> against a value a test can recompute. It holds the same determinism boundary as
> `umbra describe` (§A4, §6.1): the picture and the numbers are deterministic, the
> model **only interprets** (its reply passes `parse_narration` and never becomes a
> filter, a URL, or a measurement — the measurements are the sidecar's), and every
> narration carries the CC-BY attribution and the `AI_PROVENANCE` note. The model
> call is an injectable `Narrator` (reusing `umbra describe`'s provider plumbing)
> and the render an injectable `ChangeRenderer`, so it is fully offline-testable
> with no network. With C2 complete, the AI critical path is now **C3 watch loops**,
> **C4 `umbra chips`**, and the **B3 example notebooks**.
>
> **Update:** the **first C3 capability has shipped** — `umbra watch`
> (`src/umbra_py/watch.py`), the "agent as a standing analyst" primitive. SAR's
> value for monitoring is its cadence, so the natural workflow is *standing*: run
> the same search on a schedule and act only on what is **new**. `umbra watch`
> packages the delta, not the schedule — the scheduler (cron, a GitHub Action, an
> agent loop) supplies the "when"; `watch()` supplies the idempotent "what
> changed". It searches an injected source (a live `UmbraCatalog` or a
> `CatalogIndex`), diffs the results against the set of acquisition keys previous
> runs already reported, returns only the new ones, and folds them into a small
> state store (`MetaWatchStore`, kept in the `CatalogIndex` `meta` table — no
> schema change, so a fetched snapshot is a valid store; `InMemoryWatchStore` for
> tests). The delta is an exact set difference over sidecar hrefs, not a date
> watermark, so it is truly idempotent (a re-run with no new data reports zero)
> and never misses a late upload dated earlier than acquisitions already seen. It
> stays inside the determinism boundary (§A4, §6.1): **no model is called** — this
> is pure set arithmetic over the deterministic search the library already does —
> and the source and store are injectable, so it is fully offline-testable. It is
> machine-readable first: `--json` emits `{new_count, new_items: [context cards],
> ...}` (carrying the CC-BY attribution) for a scheduler to branch on, and
> `--exit-code` turns "are there new acquisitions?" into a process exit status a
> shell `if` can test. Paired with the shipped `umbra change --narrate` /
> `umbra describe`, it completes the standing-analyst loop C3 describes: new pass
> lands → composite against the previous pass → narration. The remaining C3 piece
> is optional (surfacing the same delta as an MCP tool/prompt); **C4 `umbra
> chips`** and the **B3 example notebooks** stay on the critical path.
>
> **Update:** **C4 `umbra chips` has shipped** — the ML dataset-preparation layer
> (`src/umbra_py/chips.py`, `[load]` extra). For the model-*training* audience the
> missing verb was *chipping*, and this supplies it: `chip_item` walks an
> acquisition's geocoded GeoTIFF one window at a time via GDAL's `/vsicurl/`
> driver (only each tile's bytes stream over HTTP range requests — no full
> download, memory bounded to one chip) and emits full `chip_size` × `chip_size`
> tiles as GeoTIFF or `.npy`; `write_chips` chips a whole search into a dataset
> with a manifest — `.jsonl` (one record per line, the standard ML format) or
> `.geojson` (chip footprints for QGIS / geopandas). Every `ChipRecord` carries
> the chip's geographic bbox, CRS, affine transform, grid position and source
> pixel window plus the acquisition's datetime, place, platform, polarization,
> incidence angle and resolution, stamped with the CC-BY attribution — the
> look-angle / resolution / polarization metadata §C4 asked for, attached per
> tile. It holds the determinism boundary the scientific audience needs (§A4,
> §6.1): **no model is called** — chipping is pure raster iteration + manifest
> logic in the deterministic core, mirroring `umbra_py.load`, so the whole
> feature is offline-testable with a real on-disk GeoTIFF and no network. Fixed
> size is a promise (partial edge tiles are dropped), `stride` produces
> overlapping tiles for dense inference / augmentation, and `min_valid` drops the
> mostly-nodata corners of a rotated footprint so a dataset isn't padded with
> black squares. This positions umbra-py as the data-loading layer for SAR
> foundation-model and change-detection research — the audience most likely to
> contribute back. With C4 done, the remaining Tier C item is the exploratory C5
> archive-embedding work (which builds directly on these chips); the **B3 example
> notebooks** stay on the critical path, and the single highest-value strategic
> move overall remains the unstarted Canopy backend (`STRATEGY.md` 5.1).
>
> **Update:** the **optional C3 MCP surface has shipped, so C3 is now fully
> complete** — the `umbra watch` delta is exposed as a `watch_site` tool and a
> `watch-site` prompt on the flagship `umbra-mcp` server, reusing the `watch()`
> function unchanged (§C3's "optional next step"). This makes the standing-analyst
> loop *conversational*: an MCP client asks "what's new at Centerfield, Utah?",
> `watch_site` returns only the passes published since the last check (all of them
> on the first run, just the delta after) as context cards, and those cards feed
> straight into the already-present `change_composite` / `timescan` tools — new
> pass → composite → describe, all in one conversation and with no glue. It holds
> the same determinism boundary as the rest of the server (§A4, §6.1): **no model
> is called** (pure set arithmetic over the deterministic search), watch state
> persists in the local index's `meta` table (`MetaWatchStore`) so a watch
> survives across sessions with no schema change, and the search source and store
> are both injectable so the whole tool is offline-testable without the SDK. With
> every C1–C4 item and this C3 follow-up done, the remaining AI work is the
> exploratory **C5 archive-embedding** and the **B3 example notebooks**; the single
> highest-value strategic move overall remains the unstarted Canopy backend
> (`STRATEGY.md` 5.1).
>
> **Update:** the **B3 example notebooks have shipped** — the last open Tier B
> item and, with the Canopy backend since landed (`STRATEGY.md` 5.1), the last
> code item on the whole AI critical path. Three self-contained, self-checking
> notebooks (`examples/01_hello_umbra.ipynb`, `02_download_and_open_gec.ipynb`,
> `03_change_detection.ipynb`) each run a small deterministic search and `assert`
> their own results, so they are the "runnable, self-checking examples =
> effectively free eval + documentation" this document called for — a coding
> agent learns the library from them and a green run proves the flow still works.
> They hold the determinism boundary (§6.1): no model is called, and
> `tests/test_examples.py` guards them **offline** on every CI run with the stdlib
> alone (`json` + `ast` — well-formed, code cells parse, every referenced
> `umbra_py` symbol is public so a rename turns the build red, CC-BY attribution
> present), then executes them end-to-end under `pytest -m network` when
> `nbclient` and the render extras are present. With B3 done, the only remaining
> AI item is the exploratory **C5 archive-embedding** (research-grade, trails the
> rest by design).
>
> **Update:** the **C5 archive-embedding capability has now shipped — the last
> open AI item, so every idea in this document (Tier A–C) is now built.**
> `umbra_py.embed` (`umbra embed`, `[ai]` + `[viz]` extras) is the flagship
> differentiator §C5 named: it precomputes a vector for *one quicklook per
> acquisition* (`umbra embed build`, keyed by item id and idempotent — a rebuild
> only embeds what is new) and exposes both `search_similar(item)`
> (`umbra embed similar <url>` — image-to-image "find scenes that look like this
> flooded field") and text-to-scene search (`umbra embed search "ships at a
> berth"`, given a joint CLIP-family model). It builds directly on the two pieces
> §C5 required: the `umbra chips` raster-iteration substrate and the already-shipped
> quicklook render — `umbra embed` reuses `umbra describe`'s injectable quicklook
> renderer so a scene is embedded from exactly the picture a human sees. It holds
> the determinism boundary the whole document rests on (§A4, §6.1): the *only*
> model calls are turning an image or a text query into a vector (both injectable —
> an `ImageEmbedder` and a text `Embedder`, default an OpenAI-compatible multimodal
> `/embeddings` endpoint via the already-core `requests`, user-supplied key, never
> implicit), while rendering, storage, cosine ranking and thresholding are
> stdlib-only (no `numpy`, no `sqlite-vec` — a brute-force scan at catalog scale is
> instant). The vectors live in a schema-versioned sidecar `catalog.embed.db` beside
> the catalog index — the same reasoning `umbra semantic` uses for its task-name
> sidecar, so the deterministic index and its published snapshot never carry
> model-derived data a core install can't use — and a `SceneMatch` is always a
> pointer back to a real acquisition (id, task, datetime, STAC href), never a
> model-authored fact. It is fully offline-testable with a deterministic stand-in
> embedder and renderer. The remaining C5 follow-ons are optional and non-blocking:
> publishing the embedding table alongside the nightly index so no user recomputes
> it, and surfacing `search_similar` as an MCP tool.
>
> **Update:** the **`search_similar` MCP surface has now shipped**, so the flagship
> C5 differentiator is conversational on the highest-leverage surface the project
> has. `umbra-mcp` grew two tools — `find_similar(url)` (image-to-image: render +
> embed the query item's quicklook, rank the pre-embedded archive by cosine
> similarity, query excluded from its own results) and `find_similar_text(query)`
> (text-to-scene, given a joint CLIP-family model) — plus a `find-similar-scenes`
> prompt, all wrapping the shipped `SceneEmbeddingIndex` (§C5) unchanged. This makes
> the search that lives in the *pixels* ("find scenes that look like this flooded
> field") a first-run MCP conversation, and the returned `SceneMatch` cards carry
> each acquisition's STAC `href` so a match hands straight to the server's existing
> `get_item` / `quicklook` / `change_composite` tools — closing the
> discover-then-view loop without leaving the chat. It holds the same determinism
> boundary as the rest of the server (§A4, §6.1): the tools gate on a prebuilt
> sidecar `catalog.embed.db` (a self-describing error points at `umbra embed build`
> when it is absent) and the `[ai]` key, and the *only* model call is the injectable
> image/text embedder — rendering, storage and ranking stay deterministic, so the
> whole path is offline-tested with a stand-in embedder and renderer, no `[viz]` or
> network. With this done, the sole remaining C5 follow-on is the optional,
> non-blocking work of publishing the embedding table alongside the nightly index so
> no user recomputes it.

---

## 2. Tier A — Make the existing surface AI-legible (low effort, immediate payoff)

### A1. Structured output everywhere in the CLI

> **Update:** **machine-readable errors have now shipped** — the load-bearing
> half of A1, and the last still-open item in Tier A (every B/C surface that
> reports failure rests on it). Every `UmbraError` carries an optional `hint`
> and a stable `to_dict()` (`{"error", "message", "hint"}`); on failure the CLI
> prints that JSON object to stderr when `--json` / `UMBRA_JSON` is active and
> an `error:`/`hint:` prose pair otherwise. The contract is published as public
> API in ✅ `docs/schemas/error.schema.json`.
>
> **Update:** the **success-side `--json` guarantee is now complete, so A1 is
> fully shipped.** Every command that produces a result has a machine-readable
> stdout shape: `umbra download --json` emits a `[{asset, path, bytes, sha256}, …]`
> array (each written file hashed with a streaming SHA-256), `umbra index info
> --json` emits the index summary (`CatalogIndex.stats()` plus `path`/`size_bytes`),
> and the five render commands (`change`, `timescan`, `swipe`, `gallery`, `map`)
> emit a `{output, items_used, parameters}` manifest — with an optional `sidecars`
> map for the auxiliary files a command writes (e.g. `umbra change --narrate`'s
> narration JSON). Human progress/warnings and the `--place` "Resolved …" status
> line go to stderr, so stdout carries the JSON object alone — the guarantee an
> agent depends on. Three new schemas are published as public API alongside the
> error contract: ✅ `docs/schemas/download.schema.json`,
> `docs/schemas/index-info.schema.json`, and `docs/schemas/render-manifest.schema.json`.
> The whole surface is offline-tested (`tests/test_cli_json.py`) with injected
> renderers/downloads — no network, no `viz` extra. With this, **Tier A is
> complete end to end.**

`umbra search --json` exists; the guarantee now covers the whole CLI:

- ✅ **shipped:** `--json` on `info` (emits the A3 context card), `index info`
  (index summary), `download` (`{asset, path, bytes, sha256}` records), and the
  render commands (`{output, items_used, parameters}` manifests).
- ✅ **shipped:** Machine-readable errors: on failure, print a single JSON
  object to stderr (`{"error": "CatalogError", "message": ..., "hint": ...}`)
  when `--json` (or `UMBRA_JSON`) is active. Agents recover from `hint` fields
  dramatically better than from prose tracebacks.
- ✅ **shipped:** Document the JSON schemas in one place (`docs/schemas/`) and
  treat them as public API under the same compatibility rules as `__all__`.

The spinner already no-ops on non-TTY output — good agent hygiene by accident;
keep that guarantee explicit in a test.

### A2. An LLM context bundle in the package and the docs — **shipped**

- ✅ **`llms.txt`** at the repo root: the emerging convention for "here is the
  condensed, LLM-ready description of this project." `llms-full.txt` is
  generated from the module docstrings — already written in exactly the right
  explanatory register (e.g. the `catalog.py` and `index.py` preambles) — read
  via `ast` so the stdlib-only generator never imports a heavy extra.
- ✅ The repo's strong `AGENTS.md` is the *contributor* agent guide; `llms.txt`
  is now the *user* agent guide ("how to drive this library," not "how to
  modify it").
- ✅ **shipped:** `umbra_py.llm_context()` (CLI: `umbra context`) returns the
  product-type table, search parameter semantics, and license/attribution
  rules as one JSON document an agent can pull into context at runtime.
- ✅ **shipped:** `umbra_py.llms_txt()` / `llms_full_txt()` (CLI: `umbra
  llms-txt [--full]`) render the [llms.txt-convention](https://llmstxt.org/)
  Markdown. `llms-full.txt` bundles the domain knowledge (reusing
  `llm_context()`), the full CLI command reference (introspected from the live
  command tree, so it never drifts), the AI-native interfaces, and a per-module
  map. The committed repo-root `llms.txt` / `llms-full.txt` are that output,
  kept in sync by a golden test.

### A3. Item-level "context cards" for models — **shipped**

✅ `UmbraItem.to_llm_context()` ships (surfaced on the CLI as `umbra info
<url> --json`). It returns a compact, token-efficient dict
designed for prompting: id, ISO datetime, place (task name), bbox, product
types with one-line explanations, resolution, polarization *with the caveat
string* ("HH and VV cannot be compared for change"), the asset URLs, and the
mandatory CC-BY attribution line. The differences from `metadata_summary()`
are the embedded explanations and the license string — the things a model
needs and a human already knows.

### A4. Keep the determinism boundary explicit

A principle worth writing into `AGENTS.md`/CONTRIBUTING now, before AI
features land: **the core library stays deterministic; anything that calls a
model lives behind an `[ai]` extra and never runs implicitly.** This preserves
the library's testability and the trust of its scientific audience while
still allowing everything below.

---

## 3. Tier B — AI-native interfaces (MCP, STAC API, notebooks)

### B1. `umbra-mcp`: an MCP server over the library (the flagship idea) — **shipped**

> **Status:** ✅ Shipped as `umbra_py.mcp_server` behind the `[mcp]` extra,
> runnable as `umbra mcp`, `umbra-mcp`, or `uvx umbra-mcp` (stdio transport).
> The tools, resources and prompts below are all live; the imagery tools return
> the rendered PNG as an MCP image block. The tool *logic* lives in plain,
> deterministic functions (offline-testable without the SDK) that
> `build_server()` registers, so the server stays within the library's
> determinism boundary. Not-yet-done follow-ups: registering in the public MCP
> registries / Anthropic's directory (part of the deliverable, tracked in
> `TODO.md`), and a LangChain/LlamaIndex wrapper reusing these tool shapes.

The CLI subcommands already map 1:1 to library functions — which means the
tool inventory for an MCP server is already designed. Shipped as a submodule
(`umbra_py.mcp_server`, runnable via `uvx umbra-mcp`, stdio transport)
exposing:

**Tools** (thin wrappers over existing functions):

| Tool | Wraps | Notes |
|---|---|---|
| `search_catalog` | `UmbraCatalog.search` / `CatalogIndex.search` | args mirror the CLI; returns compact item summaries (A3 cards), not full STAC JSON, to protect context windows |
| `get_item` | `UmbraItem.from_dict(get_json(url))` | full metadata for one item |
| `geocode_place` | `geocode_place` | lets the agent turn "Port of Long Beach" into a bbox itself |
| `quicklook` | `quicklook` | **returns the PNG as an MCP image content block** — the agent *sees* the scene |
| `change_composite` | `change_composite` + `select_change_frames` | image block + the polarization-mixing warning as text |
| `timescan` | `timescan_composite` | image block; the "where did activity happen" primitive |
| `download_asset` | `download_asset` | gated by a size confirmation parameter; returns path + bytes |
| `find_similar` / `find_similar_text` | `SceneEmbeddingIndex.similar_to_item` / `similar_to_text` | **visual similarity search (C5)** — image-to-image and text-to-scene over the pre-embedded archive; returns `SceneMatch` cards (each with a STAC `href` for `quicklook`/`change_composite`); `[ai]` extra + a prebuilt `catalog.embed.db` |
| `describe_scene` | `describe.describe` | **SAR-literate VLM reading of one scene (C2)** — renders the quicklook, sends it with the context card behind the packaged SAR primer, and returns a validated `{summary, observed_features, confidence, caveats}` stamped as an AI interpretation with CC-BY; the one tool that consults a model, gated on the `[ai]` key |
| `build_index` / `index_stats` | `CatalogIndex` | lets a long-running agent make its own searches fast |

**Resources:** the local index DB stats; recently fetched STAC items;
`llms.txt` as a readable resource.

**Prompts:** packaged workflows — "monitor a site for change" (search →
select frames → composite → describe), "survey what Umbra has over ⟨region⟩"
(geocode → search → gallery). MCP prompts are the right home for the domain
guidance currently spread across CLI `--help` epilogs.

Why this matters strategically: every MCP-enabled client (Claude Desktop/Code,
increasingly others) becomes a zero-install natural-language front end to the
Umbra archive. "Show me what changed at Centerfield, Utah this spring" becomes
a first-run experience instead of a tutorial chapter. The image-returning
tools are the differentiator — most geo MCP servers return JSON; this one can
return *pictures of the Earth from radar*.

Implementation notes: the official `mcp` Python SDK plus the existing library
is nearly the whole job; the server needs the `viz` extra. Registering in the
MCP registries (and Anthropic's directory) is part of the deliverable, not an
afterthought.

### B2. `umbra serve`: a local STAC API façade (API extensibility) — **shipped**

> **Status:** ✅ Shipped as `umbra_py.serve`, behind the `[serve]` extra and
> runnable as `umbra serve`. It serves the STAC API landing page,
> `/conformance`, `/collections`, `/collections/{id}`,
> `/collections/{id}/items`, `/collections/{id}/items/{item_id}`, and STAC item
> search over both `GET /search` and `POST /search` (bbox, datetime interval,
> ids, limit, opaque-token pagination), with FastAPI's generated OpenAPI doc at
> `/docs`. Following the package's determinism boundary, the STAC documents are
> built by plain offline functions (`landing_page`/`collection`/`item_to_stac`/
> `search_result`) with no web-framework dependency — offline-testable in the
> core install — and `build_app()` only wires them onto routes. It reads the
> prebuilt `catalog.db` index first (instant), with an opt-in `--live` S3-walk
> fallback. The **STAC Query extension is now wired** (`item-search#query`
> conformance): `product_types`, free-text `area` and a `fuzzy` toggle are
> accepted as GET params, top-level POST fields, or a STAC `query` object, and
> pushed down to the same backend `search` both the index and the live catalog
> answer. Geometry **`intersects` is now wired too** (`search(intersects=…)` /
> `GET`/`POST /search`, mutually exclusive with `bbox` per the spec): a
> dependency-free polygon test (`umbra_py._geometry`) filters on each item's
> *actual* footprint, pushing the polygon's bbox into SQL as a cheap prefilter
> first — the real polygon test the earlier note said this needed. The one
> not-yet-done follow-up is a hosted community instance.

`CatalogIndex` already mirrors search semantics in SQL. Putting a small
read-only **STAC API** (FastAPI, `[serve]` extra) in front of it —
`/search`, `/collections`, `/collections/{id}/items` — buys two ecosystems at
once:

1. **The existing geo ecosystem**: QGIS STAC plugins, `pystac-client`,
   stac-browser, leafmap all speak STAC API. Umbra's archive currently speaks
   none of them; this makes `umbra-py` the bridge.
2. **The AI ecosystem**: STAC API is exactly the kind of well-documented,
   schema'd REST surface that LLM function-calling and OpenAPI-driven agents
   consume without custom glue. One OpenAPI document covers every framework
   that isn't MCP.

Combined with the prebuilt nightly index (recommendation #17 in the analysis
doc), `umbra serve` starts answering in milliseconds on first run. A hosted
community instance is a possible later step, but the local-first version has
no operational cost and no abuse surface.

### B3. Notebook & framework affordances

- `_repr_html_` cards already exist (excellent). ✅ **shipped:**
  `UmbraItem`/`ItemCollection` now implement `__geo_interface__` (derived from
  the existing `to_geojson`) so geopandas/shapely/leafmap ingest results with
  zero code — and so agent-written analysis code "just works" on the first try.
- ✅ **shipped:** six **agent-executable example notebooks**
  (`examples/01_hello_umbra.ipynb`, `02_download_and_open_gec.ipynb`,
  `03_change_detection.ipynb`, `04_amplitude_time_series.ipynb`,
  `05_detection_chips.ipynb` — the ML-dataset workflow over `umbra chips` for the
  §C4 model-training audience — and `06_site_monitoring.ipynb`, the standing-analyst
  `umbra watch` → change-composite loop for §C3) with deterministic, small-area
  searches and `assert`s in every code cell — so, exactly as this line hoped, they are
  effectively free eval + documentation: a coding agent learns the library from
  them, and running one is a live check that the flow still works.
  `tests/test_examples.py` keeps them honest offline (stdlib `json`/`ast`: well
  formed, cells parse, only public `umbra_py` symbols, CC-BY present) and
  executes them end-to-end under `pytest -m network`.
- A LangChain/LlamaIndex community tool wrapper is low-cost once the MCP tool
  schemas exist (same shapes, different registration) — worth doing for reach,
  after MCP.

---

## 4. Tier C — AI-infused capabilities (new value, not just access)

### C1. Natural-language search, resolved deterministically

`--place` already geocodes fuzzy geography. Extend the same philosophy — *NL
in, deterministic filter out, no model required at runtime*:

- ✅ **Relative dates (shipped)**: `--start "3 months ago"`, `--start 2024`,
  `--end "last month"`, `today`/`yesterday`, `this year` via plain date
  arithmetic (no LLM needed), in `umbra_py.dates.parse_date_bound`. Bound-aware
  (spans snap to their first/last day), so `--start 2024 --end 2024` is the
  whole year. Range keywords with hemisphere-dependent meaning (`"last winter"`)
  are deliberately deferred — they belong to the LLM-planned `umbra ask` below,
  not the deterministic resolver.
- ✅ **Fuzzy task matching (shipped)**: task names are human labels ("Beet
  Piler - ND", "Atmospheric-River_Nov-2025"); `area=` is a substring match by
  default, and `fuzzy=True` now widens it to a deterministic token-wise match
  (`umbra_py.fuzzy`) that is word-order- and punctuation-independent and
  typo-tolerant — a strict superset of the substring path, shared by the live
  and index backends, no model call.
- ✅ **Semantic aliasing (shipped)** (`[ai]` extra): an **embedding index** over
  the task names (`umbra_py.semantic`) so `area="grain storage north dakota"`
  finds the beet pilers — the semantic layer plain string similarity can't and
  shouldn't fake. `umbra semantic build` embeds each distinct task name once into
  a schema-versioned sidecar SQLite DB (`catalog.semantic.db`); `umbra semantic
  search` embeds the query and ranks by cosine similarity, printing the `umbra
  search --area …` command for the best match to audit before `--run`. The only
  model call is the injectable `Embedder` (default: an OpenAI-compatible
  `/embeddings` endpoint via `requests`); storage, cosine and ranking are
  stdlib-only (no `numpy`, no `sqlite-vec` — brute force is instant at catalog
  scale, and the schema leaves room to add a vector extension later), so it is
  fully offline-testable with a stand-in embedder.
- ✅ **`umbra ask "…"` (shipped)** (`[ai]` extra): a single command that hands
  the user's sentence plus the A2 context document to a configured model
  (Anthropic / any OpenAI-compatible endpoint, user-supplied key, `requests`
  only) and returns the *deterministic command it maps to* — printing that
  command before running it. The model **only plans**; `umbra_py.planner`'s
  `parse_plan` re-validates every field (dates via `parse_date_bound`, product
  types via `PRODUCT_ASSETS`, the bbox range-checked), so nothing the model
  emits becomes a filter without passing the deterministic layer. The LLM plans;
  the library executes; the user audits — the honest version of NL search. It is
  also where range keywords with hemisphere-dependent meaning (`"last winter"`)
  that `parse_date_bound` deliberately rejects belong: the model resolves the
  season to concrete dates the deterministic layer then validates.

### C2. Scene description & change narration (VLM-in-the-loop)

Build on the artifacts that already exist:

- ✅ **`umbra describe <item-url>` (shipped)** (`umbra_py.describe`, `[ai]` +
  `[viz]` extras): renders the quicklook, sends it with the A3 context card to a
  VLM, and returns a structured description (`{summary, observed_features[],
  confidence, caveats[]}`). The packaged prompt carries the SAR literacy —
  backscatter ≠ brightness, speckle, layover/shadow, one-frame ≠ change — so a
  general vision model reads the radar correctly. The model **only interprets**
  (its reply passes the deterministic `parse_description` boundary), and every
  description is stamped with the CC-BY attribution and an `AI_PROVENANCE` note.
  The model call is an injectable `Describer` and the render an injectable
  `Renderer`, so it is fully offline-testable.
- ✅ **`umbra change --narrate` (shipped)** (`umbra_py.narrate`, `[ai]` + `[viz]`
  extras): after rendering the composite, produce a plain-language change report
  grounded in the color semantics the library already documents ("green =
  appeared / brightened in the later pass; magenta = vanished / dimmed"). The
  machine-readable sidecar is `compute_change_stats` — per-block change
  statistics (mean *signed* Δ in dB on a coarse north-up grid, plus the fraction
  of each block that moved past a threshold) — handed to the model alongside the
  PNG and written next to the image as `<out>.narration.json`, so the narration
  cites numbers, not vibes, and the text output stays auditable against a
  deterministic artifact. The model **only interprets** (its reply passes the
  `parse_narration` boundary); the narration is a structured `ChangeNarration`
  (`{summary, changes[], confidence, caveats[]}`), and the model call / render are
  injectable so the whole feature is offline-testable.
- ✅ **`describe_scene` MCP tool + `describe-scene` prompt (shipped)**: the same
  scene reading is now surfaced over the `umbra-mcp` server, reusing the
  `describe()` function unchanged, so an MCP client can get a grounded SAR reading
  of a scene in one call. It is the **one tool on the server that consults a
  model** — a deliberate, opt-in exception to the otherwise-deterministic tool
  surface, gated (like the CLI) on the `[ai]` key, so it never runs implicitly.
  The boundary still holds: the picture and the metadata card are produced
  deterministically, the model **only interprets** (its reply passes the
  `parse_description` boundary), and every reading carries the CC-BY attribution
  and the `AI_PROVENANCE` note. The describer and render are injectable, so the
  whole tool is offline-testable without the SDK, a key, or the network.
- Every AI-generated artifact **automatically carries the CC-BY attribution
  and an "AI-generated interpretation" provenance note** — the same license
  discipline the library already applies to GeoTIFF tags and xarray attrs,
  extended to model outputs.

### C3. Monitoring recipes: the agent as a standing analyst

SAR's revisit cadence + change detection + an agent = site monitoring. Package
the loop, not the agent:

- ✅ **`umbra watch` (shipped)** (`umbra_py.watch`): run the same search on a
  schedule and report only the acquisitions **new** since the last run — state
  in the local index, exact set-difference delta (not a date watermark, so a
  late upload is never missed), fully idempotent. `--json` emits a machine
  readable `{new_count, new_items: [context cards], ...}` delta (with CC-BY
  attribution) for a scheduler to act on; `--exit-code` turns "any new?" into a
  process exit status a shell `if` can branch on. Cron / GitHub Actions / agent
  frameworks supply the scheduling; the library supplies the idempotent delta
  detection. No model is called — the search source and state store are both
  injectable, so the whole feature is deterministic and offline-testable (§A4,
  §6.1). State is kept in the `CatalogIndex` `meta` table (`MetaWatchStore`), so
  a fetched snapshot is a valid store with no schema change.
- Pair with C2 so the standing workflow is: *new pass lands → composite
  against previous pass → VLM narration → notification with image + text*.
  With `umbra watch` and `umbra change --narrate` both shipped, this loop is now
  assemblable end-to-end. This is the "decrease the barrier" story in one demo:
  a port authority, journalist, or humanitarian analyst gets SAR-based
  monitoring without knowing what a sigma-naught is.
- ✅ **`watch_site` MCP tool + `watch-site` prompt (shipped)**: the same delta
  is now surfaced over the `umbra-mcp` server, reusing the `watch()` function
  unchanged, so an MCP client can run the standing check conversationally. The
  tool takes the same filters as `search_catalog`, persists state in the local
  index's `meta` table (so a watch survives across sessions), and returns the
  new acquisitions as context cards ready to hand straight to `change_composite`
  / `timescan` — closing the standing-analyst loop (new pass → composite →
  describe) inside one conversation. No model is called; the source and store
  stay injectable, so it is fully offline-testable.

### C4. ML dataset preparation (`umbra chips`) — **shipped**

✅ **shipped** (`umbra_py.chips`, `[load]` extra): a chipping API that walks
search results and emits fixed-size, georeferenced tiles (GeoTIFF or NumPy)
with a manifest (JSONL — chip path, item id, datetime, place, bbox, CRS,
transform, source pixel window, polarization, incidence angle, resolution,
license — a `.geojson` `FeatureCollection` of chip footprints, or a `.parquet`
stac-geoparquet table for querying a large chip set with DuckDB / geopandas
without loading every line, reusing the `[export]` extra's writer). `chip_item`
reads band 1 of the item's COG one window at a time via GDAL's `/vsicurl/`
driver (mirroring `umbra_py.load`), so only each tile's bytes stream over HTTP
range requests and memory stays bounded to one chip. Fixed size is a promise
(partial edge tiles are dropped), `stride` produces overlapping tiles for dense
inference / augmentation, and `min_valid` drops the mostly-nodata corners of a
rotated footprint. `write_chips` chips a whole search into a dataset + manifest;
the `umbra chips` CLI mirrors `umbra change`'s search-vs-URLs interface (plus
`--local`/`--index-db`). No model is called — pure raster iteration + manifest
logic in the deterministic core — so it is fully offline-testable. It positions
umbra-py as the data-loading layer for SAR foundation-model and change-detection
research — the audience most likely to contribute back — and it is the
prerequisite the exploratory C5 archive-embedding work builds on.

### C5. Embedding the archive itself — **shipped**

✅ **shipped** (`umbra_py.embed`, `umbra embed`, `[ai]` + `[viz]` extras).
`umbra embed build` precomputes an image embedding for one quicklook per
acquisition and stores the vectors in a schema-versioned sidecar SQLite DB
(`catalog.embed.db`) beside the catalog index; `umbra embed similar <item-url>`
exposes `search_similar(item)` (image-to-image — "find scenes that look like this
flooded field") and `umbra embed search "…"` exposes text-to-scene search (given
a joint CLIP-family model whose text and image encoders share a space). This is
the genuinely new capability over the archive §C5 called for — nothing in the
Umbra ecosystem offers visual similarity search over Umbra data.

It holds the same determinism boundary as `umbra semantic` (§A4, §6.1): the only
model calls are turning an image or a text query into a vector — an injectable
`ImageEmbedder` and text `Embedder` (default an OpenAI-compatible multimodal
`/embeddings` endpoint via the already-core `requests`, user-supplied key, never
implicit) — while rendering (it reuses `umbra describe`'s injectable quicklook
renderer, so a scene is embedded from exactly the picture a human sees), storage,
cosine ranking and thresholding are stdlib-only (no `numpy`, no `sqlite-vec`, no
binary dependency — a brute-force scan at catalog scale is instant). `build` is
idempotent (keyed by item id) and skips a scene whose asset won't render rather
than aborting the batch; a `SceneMatch` is always a pointer back to a real
acquisition (id, task, datetime, STAC href), never a model-authored fact. It is
fully offline-testable with a deterministic stand-in embedder and renderer.
✅ **`search_similar` is now surfaced as an MCP tool** — `umbra-mcp`'s
`find_similar` (image-to-image) and `find_similar_text` (text-to-scene) wrap
`SceneEmbeddingIndex` unchanged and return `SceneMatch` cards that hand straight to
`quicklook` / `change_composite`. Remaining (optional, non-blocking): publish the
embedding table with the nightly index so no user recomputes it. It is the kind of
flagship feature that earns talks, papers, and contributors.

---

## 5. Sequencing and effort map

| Phase | Items | Effort | Rationale |
|---|---|---|---|
| 1 (next release) | ✅ **shipped** — A3 context cards · A2 `llm_context()` · A4 determinism policy · B3 `__geo_interface__` · A1 `info --json` | days | Zero-dependency groundwork every later phase consumes |
| 2 | ✅ **shipped** — B1 MCP server · nightly prebuilt index · A2 `llms.txt` + docs bundle | 1–2 weeks | The adoption unlock; MCP server is the highest leverage single artifact |
| 3 | ✅ **B2 `umbra serve` STAC API (shipped)** · ✅ **C1 relative date bounds (shipped)** · ✅ **C1 fuzzy task matching (shipped)** · ✅ **C1 `umbra ask` (shipped)** · ✅ **C1 semantic aliasing / embedding index (shipped)** · ✅ **B3 notebooks (shipped)** | 2–4 weeks | Ecosystem bridges, both geo and AI |
| 4 | ✅ **C2 `umbra describe` (shipped)** · ✅ **C2 `change --narrate` (shipped)** · ✅ **C3 `umbra watch` (shipped)** · ✅ **C4 `umbra chips` (shipped)** | ongoing | AI-infused capabilities; each is independently shippable |
| 5 | ✅ **C5 embeddings — visual similarity search (shipped)** | exploratory | Flagship differentiator; every idea in this document is now built |

Dependencies to respect: the MCP server (B1) and STAC façade (B2) both lean on
correct S3 pagination and the prebuilt index from the analysis document. Both
prerequisites are now done: PR #29 added `list-type=2` (so agents no longer
amplify silently-truncated search results), and the prebuilt-index *consume*
side has shipped — `umbra index fetch` / `CatalogIndex.from_release()` pulls the
weekly `catalog.db` snapshot, so an MCP or STAC-API layer can bootstrap a
whole-catalog index in seconds instead of crawling on first run. Both
interfaces were thereby unblocked, and both have now shipped: the MCP server
(B1) and the `umbra serve` STAC façade (B2).

---

## 6. Design principles to hold onto

1. **Deterministic core, AI at the edges.** Models plan, describe, and
   narrate; the library searches, downloads, and renders. Never let a model
   output become a coordinate, a URL, or a filter without passing through the
   deterministic layer.
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
