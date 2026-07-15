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
> document exactly as planned. The remaining Phase 2 items (A2 `llms.txt` docs
> bundle, the nightly-index publisher already exists) and Phase 3's
> `umbra serve` STAC façade are the next critical path.

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

### A2. An LLM context bundle in the package and the docs

- **`llms.txt`** at the docs-site root (and repo root): the emerging
  convention for "here is the condensed, LLM-ready description of this
  project." Generate `llms-full.txt` from the module docstrings — they are
  already written in exactly the right explanatory register (e.g. the
  `catalog.py` and `index.py` preambles).
- The repo already has a strong `AGENTS.md` — treat it as the *contributor*
  agent guide, and make `llms.txt` the *user* agent guide ("how to drive this
  library," not "how to modify it").
- ✅ **shipped:** `umbra_py.llm_context()` (CLI: `umbra context`) returns the
  product-type table, search parameter semantics, and license/attribution
  rules as one JSON document an agent can pull into context at runtime. The
  `llms.txt` docs-bundle half remains open (Phase 2).

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

### B2. `umbra serve`: a local STAC API façade (API extensibility)

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

- **Relative dates**: `--start "3 months ago"`, `--when "last winter"` via
  plain date arithmetic (no LLM needed).
- **Fuzzy task matching**: task names are human labels ("Beet Piler - ND",
  "Atmospheric-River_Nov-2025"); today `area=` is a substring match. Add
  fuzzy/alias matching, then optionally an embedding index over task names +
  descriptions (sqlite-vec inside the existing `catalog.db`, `[ai]` extra) so
  `area="grain storage north dakota"` finds the beet pilers.
- **`umbra ask "…"`** (`[ai]` extra): a single command that hands the user's
  sentence plus the A2 context document to a configured model (Anthropic/
  OpenAI-compatible, user-supplied key) and returns the *deterministic command
  it maps to* — showing the command before running it. The LLM plans; the
  library executes; the user audits. This is the honest version of NL search.

### C2. Scene description & change narration (VLM-in-the-loop)

Build on the artifacts that already exist:

- `umbra describe <item-url>`: render the quicklook, send it with the A3
  context card to a VLM, return a structured description (`{summary,
  observed_features[], confidence, caveats[]}`). The prompt must carry SAR
  literacy: layover/shadow, speckle, dB stretch semantics — encode that
  domain knowledge once, in the packaged prompt, where it benefits every user.
- `umbra change --narrate`: after writing the composite, produce a
  plain-language change report grounded in the color semantics the library
  already documents ("green = appeared after ⟨date₁⟩ …"). Attach the
  machine-readable sidecar: per-block change statistics (mean |Δ| in dB on a
  coarse grid) so the narration cites numbers, not vibes — and so the text
  output remains auditable against a deterministic artifact.
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
| 2 | ✅ **B1 MCP server (shipped)** · nightly prebuilt index (shipped) · ⬜ A2 `llms.txt` + docs bundle | 1–2 weeks | The adoption unlock; MCP server is the highest leverage single artifact |
| 3 | B2 `umbra serve` STAC API · C1 NL search (fuzzy/date parts first) · B3 notebooks | 2–4 weeks | Ecosystem bridges, both geo and AI |
| 4 | C2 describe/narrate · C3 watch loops · C4 chips | ongoing | AI-infused capabilities; each is independently shippable |
| 5 | C5 embeddings | exploratory | Flagship differentiator once the base is solid |

Dependencies to respect: the MCP server (B1) and STAC façade (B2) both lean on
correct S3 pagination and the prebuilt index from the analysis document. Both
prerequisites are now done: PR #29 added `list-type=2` (so agents no longer
amplify silently-truncated search results), and the prebuilt-index *consume*
side has shipped — `umbra index fetch` / `CatalogIndex.from_release()` pulls the
weekly `catalog.db` snapshot, so an MCP or STAC-API layer can bootstrap a
whole-catalog index in seconds instead of crawling on first run. Both
interfaces were thereby unblocked; the MCP server (B1) is now shipped, and the
STAC façade (B2) remains open.

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
