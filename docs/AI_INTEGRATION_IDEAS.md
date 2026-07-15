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

---

## 2. Tier A — Make the existing surface AI-legible (low effort, immediate payoff)

### A1. Structured output everywhere in the CLI

`umbra search --json` exists; extend the guarantee to the whole CLI:

- `--json` on `info` (✅ shipped — emits the A3 context card), `index info`,
  `download` (emit `{asset, path, bytes, sha256}` records), and the render
  commands (emit `{output, items_used: [ids], parameters}` manifests).
- Machine-readable errors: on failure, print a single JSON object to stderr
  (`{"error": "CatalogError", "message": ..., "hint": ...}`) when `--json` is
  active. Agents recover from `hint` fields dramatically better than from
  prose tracebacks.
- Document the JSON schemas in one place (`docs/schemas/`) and treat them as
  public API under the same compatibility rules as `__all__`.

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
> fallback. Not-yet-done follow-ups: a hosted community instance, and richer
> query extensions (free-text `area`, geometry `intersects`).

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
- Publish two or three **agent-executable example notebooks** (the planned
  `examples/*.ipynb`) with deterministic, small-area searches. Coding agents
  learn a library from its examples; runnable, self-checking examples are
  effectively free eval + documentation.
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
- Every AI-generated artifact **automatically carries the CC-BY attribution
  and an "AI-generated interpretation" provenance note** — the same license
  discipline the library already applies to GeoTIFF tags and xarray attrs,
  extended to model outputs.

### C3. Monitoring recipes: the agent as a standing analyst

SAR's revisit cadence + change detection + an agent = site monitoring. Package
the loop, not the agent:

- `umbra watch --area "…" --since-last-run` → exits with a machine-readable
  "N new acquisitions" delta (state in the local index). Cron/GitHub Actions/
  agent frameworks supply the scheduling; the library supplies idempotent
  delta detection.
- Pair with C2 so the standing workflow is: *new pass lands → composite
  against previous pass → VLM narration → notification with image + text*.
  This is the "decrease the barrier" story in one demo: a port authority,
  journalist, or humanitarian analyst gets SAR-based monitoring without
  knowing what a sigma-naught is.

### C4. ML dataset preparation (`umbra chips`)

For the model-*training* audience: a chipping API that walks search results
and emits fixed-size, georeferenced tiles (GeoTIFF or NumPy) with a manifest
(GeoParquet/JSONL: chip path, item id, datetime, bbox, polarization,
resolution, license). `to_xarray` already does windowed reads, so this is
mostly iteration + manifest logic. It positions umbra-py as the data-loading
layer for SAR foundation-model and change-detection research — the audience
most likely to contribute back.

### C5. Embedding the archive itself (exploratory)

Once chips exist: precompute image embeddings (e.g. a SAR-tuned or CLIP-family
encoder) for one quicklook per acquisition, store vectors in the index, and
expose `search_similar(item)` / text-to-scene search. "Find scenes that look
like this flooded field" is a genuinely new capability over this archive —
nothing in the Umbra ecosystem offers it. Publish the embedding table with
the nightly index so no user recomputes it. This is research-grade and should
trail C1–C4, but it is the kind of flagship feature that earns talks, papers,
and contributors.

---

## 5. Sequencing and effort map

| Phase | Items | Effort | Rationale |
|---|---|---|---|
| 1 (next release) | ✅ **shipped** — A3 context cards · A2 `llm_context()` · A4 determinism policy · B3 `__geo_interface__` · A1 `info --json` | days | Zero-dependency groundwork every later phase consumes |
| 2 | ✅ **shipped** — B1 MCP server · nightly prebuilt index · A2 `llms.txt` + docs bundle | 1–2 weeks | The adoption unlock; MCP server is the highest leverage single artifact |
| 3 | ✅ **B2 `umbra serve` STAC API (shipped)** · ✅ **C1 relative date bounds (shipped)** · ✅ **C1 fuzzy task matching (shipped)** · ✅ **C1 `umbra ask` (shipped)** · ✅ **C1 semantic aliasing / embedding index (shipped)** · ⬜ B3 notebooks | 2–4 weeks | Ecosystem bridges, both geo and AI |
| 4 | ✅ **C2 `umbra describe` (shipped)** · ✅ **C2 `change --narrate` (shipped)** · ⬜ C3 watch loops · ⬜ C4 chips | ongoing | AI-infused capabilities; each is independently shippable |
| 5 | C5 embeddings | exploratory | Flagship differentiator once the base is solid |

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
