# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- **Live search now fetches acquisition sidecars concurrently
  (`docs/CODEBASE_ANALYSIS.md` §4.2 / P1 #9).** Discovery is the library's core
  value — searching a catalog that has no search API — and the walk's one
  remaining per-acquisition round trip was the `*.stac.v2.json` sidecar GET,
  issued serially, so a 50-item search paid ~50 latencies back to back.
  `UmbraCatalog._walk_task` now resolves those sidecars through a small thread
  pool (`_SIDECAR_WORKERS = 8`, mirroring the gallery's proven pattern) and
  yields them strictly in acquisition-date order, so a task's wall time collapses
  from N serial fetches toward N/workers with the output order unchanged.
  Fetching in windows keeps the pool bounded and caps wasted work at one window
  when an early `limit` / `max_per_task` stops the search. The shared
  `_http.default_session()` connection pool was sized up (`pool_maxsize=16`) so
  the fan-out reuses connections instead of churning them. No behavior change
  beyond speed — same items, same order, still fully offline-testable.
- **The shared HTTP session now retries transient failures, and downloads verify
  their integrity (`docs/CODEBASE_ANALYSIS.md` P1 #5/#6, §3.2/§4.3).** The
  library's core job is fetching data from a public bucket; these harden that
  path from alpha-fragile to dependable, and every caller inherits them because
  everything routes through `_http.default_session()`.
  - **Retry/backoff on the shared session.** `default_session()` now mounts an
    `HTTPAdapter` with `urllib3` `Retry(total=3, backoff_factor=0.5,
    status_forcelist=(429, 500, 502, 503, 504))` on idempotent `GET`/`HEAD`
    requests. A single transient S3 hiccup no longer fails an entire
    multi-minute index build, catalog walk, or download.
  - **Download integrity is verified before finalizing.** `download_url` now
    compares the received byte count against `Content-Length` and raises
    `DownloadError` on a short read (a cleanly-closed truncated body that
    previously renamed a silently-incomplete `.part` into place), and converts a
    mid-stream connection break into `DownloadError` — in both cases leaving the
    `.part` on disk so a later call resumes rather than discarding progress.
  - **Resume is validated with `If-Range`.** A resumed download stores the
    object's `ETag` next to its `.part` and sends it as `If-Range` on the next
    `Range` request, so if the remote object changed the server returns the whole
    new object (a clean restart) instead of splicing bytes from two different
    objects into a corrupt file.

### Added
- **On-demand render artifacts on `umbra serve` — quicklook / change / timescan
  over any site (`docs/DEMO_APP_GAPS.md` R4 / Path B step 2).** The STAC API
  façade shipped *discovery* (search/collections/items); the demo-gap analysis's
  last self-serve requirement (R4) was *triggering the visual products from the
  UI over any site*, not just a curated set baked at build time. The server now
  renders them on demand, wrapping the existing `umbra_py.viz` functions
  unchanged:
  - **Three endpoints.** `GET /artifacts/quicklook/{item_id}.png` renders one
    acquisition's SAR quicklook; `POST /artifacts/change` renders a 2–3 date
    change composite over a query (`ids`, or `bbox` + `datetime`); `POST
    /artifacts/timescan` renders a temporal-statistics composite over a series.
    A query resolving to more frames than a composite takes is subsampled
    deterministically (change → first/middle/last three dates; timescan → an
    evenly-spaced cap of 60), and too few is a `400`.
  - **Disk-cached by inputs.** Every artifact is cached to disk keyed by a
    content hash of its kind, ordered frame ids and render options, so a repeat
    request is a file read (`X-Umbra-Cache: hit`) — closing the "no artifact
    caching" gap for these endpoints. Frame order is part of the key (a change
    composite is not the same picture with its passes reversed).
  - **Injectable renderers, offline-testable.** `build_app(..., renderers=...)`
    overrides the render functions, so the routes are unit-tested in the core
    install with no network and no `viz` extra; the default renderers lazily
    import `viz` at request time and a missing extra surfaces as HTTP `501`.
  - **Opt-out for public instances.** `umbra serve --no-artifacts` mounts only
    the read-only STAC surface (bounding COG-streaming egress); `--cache-dir`
    overrides where PNGs are cached. Rendering is synchronous for now — an async
    job queue for long renders is the ledgered follow-on (`TODO.md`).
- **`umbra demo` — a self-serve interactive catalog explorer in one HTML page
  (`docs/DEMO_APP_GAPS.md` G3/G4, Path A front end).** Every other visual command
  emits a *one-shot* artifact — change a filter and you re-run the CLI and open a
  new file. `umbra_py.demo` (`umbra demo`, `[viz]` extra) produces the missing
  *application*: a single self-contained page (no extra required — the page is
  pure HTML and the map runs browser-side) over a whole slice of the catalog
  with the interactive controls the gap analysis names as absent today —
  client-side **faceted filters** (free-text site/id search, a date-range slider
  bounded to the data, product-type chips), **marker clustering** so it scales
  past a Folium map's few-hundred-polygon ceiling, and a click-to-quicklook SAR
  overlay streamed on demand.
  - **Static, single file, no server.** Leaflet + Leaflet.markercluster from
    pinned CDNs, the catalog embedded as JSON, all filtering in the browser — it
    opens from `file://` or any static host (GitHub Pages), exactly like
    `umbra swipe` / `umbra gallery` output. This is Path A's front end delivered
    as an artifact; the productized FastAPI server app remains Path B.
  - **Reads the fast index.** Like the other visual commands it routes through
    `_gather_items`, so `--local` builds the page from a prebuilt index
    (`umbra index fetch` / `umbra index build`) in milliseconds instead of
    re-walking S3 — the "no multi-minute walk in the user's critical path"
    requirement a demo needs. `--max-per-task 1` gives a one-marker-per-site
    whole-archive overview.
  - **Reuses the proven COG driver.** The per-item "Get SAR image" button drives
    the same browser-side geotiff.js fetcher as `umbra map --lazy-imagery`; the
    only addition is a `window.umbraLazyMap` fallback in `_lazy_imagery` so the
    shared driver resolves a plain Leaflet map on this non-Folium page (the
    Folium DOM-walk path is untouched). Pass `--no-lazy-imagery` for a
    metadata-only explorer with no CDN dependency at click time.
  - **Safe by construction.** The catalog arrives as a JSON global
    (`window.UMBRA_DEMO`, with `</` neutralised against a `</script>` break-out)
    and the application JavaScript is a *static* string that reads it — remote
    metadata is placed into the DOM with `textContent` / `setAttribute`, never
    parsed as HTML. The generator is stdlib-only, so it runs in a core install
    and is fully offline-testable.
- **Archive scene embeddings — visual similarity search (`docs/AI_INTEGRATION_IDEAS.md`
  C5, the last open AI item).** Every other search matches *metadata* (a date, a
  bbox, a task name); this matches *appearance*. `umbra_py.embed`
  (`umbra embed`, `[ai]` + `[viz]` extras) embeds each acquisition's rendered
  quicklook into a vector once and then ranks scenes by cosine similarity, so
  "find scenes that look like this one" becomes plain offline arithmetic over the
  stored vectors — a capability nothing in the Umbra ecosystem offers.
  - **`umbra embed build`** renders each item's quicklook once (only downsampled
    overviews stream over HTTP — no full download) and embeds it, keyed by item
    id so a rebuild only embeds what is new. It takes the same search-vs-URLs
    interface as `umbra change` (plus `--local`/`--index-db`), and skips a scene
    whose asset won't render rather than aborting the batch.
  - **`umbra embed similar <item-url>`** renders and embeds the query item, then
    returns the archived scenes that look most like it (the query is excluded from
    its own results) — image-to-image search.
  - **`umbra embed search "a flooded field"`** ranks the stored *image* vectors
    against a text query — text-to-scene search, given a joint CLIP-family model
    whose text and image encoders share a space.
  - **`umbra embed info`** reports the scene-vector count, model and dimension.

  It holds the library's determinism boundary (`docs/AI_INTEGRATION_IDEAS.md` §A4,
  §6.1): the *only* model calls are turning an image or a text query into a
  vector (injectable `ImageEmbedder` / text `Embedder`, default an
  OpenAI-compatible multimodal `/embeddings` endpoint via the already-core
  `requests`, user-supplied key, never implicit). Rendering, storage, cosine
  ranking and thresholding are stdlib-only (no `numpy`, no `sqlite-vec`), so the
  whole feature is offline-testable with a deterministic stand-in embedder and
  renderer. The vectors live in a schema-versioned sidecar `catalog.embed.db`
  beside the catalog index — never inside `catalog.db` — so the deterministic
  index and its published snapshot never carry model-derived data a core install
  can't use (the same boundary `umbra semantic` uses). A `SceneMatch` is a pointer
  back to a real acquisition (id, task, datetime, STAC href), never a
  model-authored fact.
- **Example notebook gallery (`docs/STRATEGY.md` 5.4 / `docs/AI_INTEGRATION_IDEAS.md`
  B3) — the demo notebooks DevRel links first.** Three self-contained, self-checking
  Jupyter notebooks under `examples/`, each driven by a small deterministic search
  and ending its code cells with `assert`s, so running one top-to-bottom is both a
  tutorial and a live smoke test:
  - `01_hello_umbra.ipynb` — search → summarize → quicklook, plus the zero-glue
    geopandas (`__geo_interface__`) and model-ready (`to_llm_context`) paths.
  - `02_download_and_open_gec.ipynb` — stream a GEC into an analysis-ready
    `xarray.DataArray` (no full download), analyze it, and round-trip the CRS with
    `rioxarray`.
  - `03_change_detection.ipynb` — find a repeat-imaged site, pick two passes, and
    composite the change into one color image.

  The notebooks ship with cleared outputs. `tests/test_examples.py` guards them
  **offline on every CI run** using only the stdlib (`json` + `ast`): each notebook
  must be well-formed, its code cells must parse, every `umbra_py` symbol it
  references must be public (drift protection — a renamed export turns the build
  red), and the CC-BY attribution line must be present. The same test executes the
  notebooks end-to-end under `pytest -m network` when `nbclient` and the render
  extras are available, so the weekly canary can prove the documented flows still
  run against the live bucket.
- **PyPI release readiness — the single highest-leverage adoption gap
  (`docs/CODEBASE_ANALYSIS.md` P0 #2/#3, P2 #11/#15).** The whole funnel is
  built (free-bucket search → paid Canopy archive, all in one library), but the
  README's first instruction, `pip install umbra-py`, still fails because the
  package isn't on PyPI. This lands the release plumbing so a maintainer can
  claim the name and ship:
  - **`release.yml` workflow** publishing to PyPI via **Trusted Publishing**
    (OIDC) on a published GitHub Release — no long-lived token stored in the
    repo. It builds the sdist + wheel, runs `twine check`, and refuses to
    publish if the `vX.Y.Z` release tag disagrees with the package version.
    `workflow_dispatch` runs a build-and-verify dry run without publishing.
  - **Single-sourced version.** `pyproject.toml` now derives the version from
    `umbra_py.__version__` via hatchling's dynamic version, so the two can no
    longer drift (`docs/CODEBASE_ANALYSIS.md` §2.2).
  - **PEP 561 `py.typed` marker** shipped in the wheel and sdist, so downstream
    type checkers finally consume the library's inline types.
  - **Repository-identity fix.** The `pyproject.toml` project URLs, the
    `CHANGELOG` compare/tag links, and the `CONTRIBUTING` clone command now all
    point at the canonical `reesehammer/umbra-py` instead of the stale
    `theminiverse` org (`docs/CODEBASE_ANALYSIS.md` P0 #3).
- **Canopy commercial-archive backend behind the same `search()` interface
  (`docs/STRATEGY.md` 5.1 — the single highest-value strategic move).** Umbra's
  open data is a static STAC catalog with no search API (which is why this
  library crawls S3); its *commercial* product, Canopy, exposes a real,
  authenticated STAC API over the full archive. `UmbraCatalog` now accepts a
  Canopy `token` (plus optional `archive_url` / `collections`), and when one is
  given the **same `search()` call** queries
  `api.canopy.umbra.space/archive/search` instead of walking the open bucket —
  *the same filters, the same `UmbraItem` results*, so every downstream verb
  (download, quicklook, change, chips, …) works unchanged against either
  archive. This is the funnel made literal: a user onboarded on the free data is
  already holding the exact tool they'd use as a paying customer. `bbox` and the
  date bounds are pushed down to the STAC API; `product_types` and
  `area`/`fuzzy` are applied to the returned items exactly as on the open path,
  so the interface is identical across both. The client speaks the STAC API
  standard — a POST item-search body plus `rel="next"` pagination (POST-merge or
  GET token links) — and the bearer token is only ever sent to the Canopy
  endpoint, never the open bucket; a 401/403 surfaces as a clear "token
  rejected" `CatalogError`. The CLI exposes it as `umbra search --token …`
  (falling back to `$UMBRA_CANOPY_TOKEN`), mutually exclusive with
  `--local`/`--db`. No model is involved and no credentials are needed to test
  it: the whole path is offline-testable against a mocked STAC API
  (`tests/test_canopy.py`).
- **`watch_site` MCP tool + `watch-site` prompt: the standing-analyst delta,
  now conversational (`docs/AI_INTEGRATION_IDEAS.md` C3 — the last open C3
  piece).** The `umbra watch` idempotent delta is now surfaced over the flagship
  `umbra-mcp` server, reusing `umbra_py.watch.watch()` unchanged. `watch_site`
  takes the same filters as `search_catalog` (`place`/`area`/`bbox`,
  `products`, `start`/`end`, `fuzzy`) and returns only the acquisitions **new**
  since the last check of that site — all of them on the first run, and just the
  delta on every re-check. State persists in the local catalog index's `meta`
  table (`MetaWatchStore`, created on first use), so a watch survives across MCP
  sessions with no extra setup; a stable `name` is derived from the query (pass
  an explicit `name` for several independent watches over one site), and
  `reset=True` re-establishes the baseline. The returned `new_items` are context
  cards ready to hand straight to `change_composite` / `timescan`, closing the
  standing-analyst loop (new pass → composite → describe) inside one
  conversation. The companion `watch-site` prompt packages that workflow.
  **No model is called** — this is pure set arithmetic over the deterministic
  search — so the whole surface stays offline-testable (the search source is an
  injectable live/index backend and the store an injectable index).
- **`umbra chips`: turn SAR scenes into georeferenced ML training tiles
  (`docs/AI_INTEGRATION_IDEAS.md` C4 / `docs/STRATEGY.md` 5.5 — the ML
  dataset-preparation layer).** For the model-*training* audience, the missing
  verb is *chipping*. The new `umbra_py.chips` module (`[load]` extra, mirroring
  `umbra_py.load`) walks a search result and cuts each acquisition's geocoded
  GeoTIFF into fixed-size, georeferenced tiles with a manifest that carries the
  per-chip metadata a training pipeline needs. `chip_item()` reads band 1 of the
  item's COG one window at a time through GDAL's `/vsicurl/` driver — so only the
  bytes for each tile stream over HTTP range requests (no multi-gigabyte
  download, memory bounded to one chip) — and emits full `chip_size` × `chip_size`
  tiles as GeoTIFF (georeferenced) or `.npy` (bare `float32`); partial edge tiles
  are dropped so every chip has the exact shape a loader expects, `stride`
  controls overlap for dense inference / augmentation, and `min_valid` drops the
  mostly-nodata corners of a rotated footprint. `write_chips()` chips a whole
  search into a dataset and writes a manifest — `.jsonl` (one `ChipRecord` per
  line, the standard ML format) or `.geojson` (a `FeatureCollection` of chip
  footprints for QGIS / geopandas), both stdlib-only — where every record carries
  the chip's geographic bbox, CRS, affine transform, grid position and source
  pixel window plus the acquisition's datetime, place, platform, polarization,
  incidence angle and resolution, stamped with the CC-BY attribution (the same
  license discipline the library applies to GeoTIFF tags and xarray attrs). The
  `umbra chips` command mirrors `umbra change`'s search-vs-URLs interface (pass
  STAC URLs directly, or `--area`/`--bbox` with `--start`/`--end`, plus
  `--local`/`--index-db` to gather from a prebuilt index) with `--chip-size`,
  `--stride`, `--format`, `--db`, `--min-valid`, `--manifest` and a `--json`
  dataset summary. No model is called — chipping is pure raster iteration +
  manifest logic in the deterministic core — so the whole feature is
  offline-testable with a real on-disk GeoTIFF and no network.
- **`umbra watch`: idempotent delta detection for standing site monitoring
  (`docs/AI_INTEGRATION_IDEAS.md` C3 — the first "agent as a standing analyst"
  primitive).** SAR re-images a site pass after pass, so the natural way to
  monitor one is to run the same search on a schedule and act only on what is
  *new*. The new `umbra_py.watch` module packages the delta, not the schedule:
  `watch()` searches an injected source (a live `UmbraCatalog` or a
  `CatalogIndex`), compares the results against the set of acquisition keys
  previous runs already reported, returns only the new ones, and folds them back
  into a small state store. It is idempotent — an immediate re-run with no newly
  published data reports zero — because the delta is an exact set difference over
  sidecar hrefs, not a date watermark (which would miss a late upload dated
  earlier than acquisitions already seen). State persists in a `CatalogIndex`'s
  existing `meta` table (`MetaWatchStore`, no schema change, so a fetched
  snapshot is a valid store); `InMemoryWatchStore` is the offline-testable
  stand-in. The `umbra watch` command mirrors `umbra search`'s query flags plus
  `--name` (stable watch identity, auto-derived from the query via `watch_key`
  when omitted), `--state-db`, `--reset` (re-baseline), `--json` (a machine
  readable delta whose `new_items` are `to_llm_context` cards, carrying the CC-BY
  attribution), and `--exit-code` (exit 10 when there are new acquisitions, so a
  scheduler's shell `if` can branch without parsing output). Cron, a GitHub
  Action, or an agent loop supplies the schedule; this supplies the delta — pair
  it with `umbra change --narrate` / `umbra describe` for the full standing
  analyst (new pass lands → composite against the previous pass → narration). The
  search source and state store are both injectable, so the whole feature is
  deterministic and offline-testable with no network and no model call.
- **`umbra change --narrate`: a vision model narrates *what changed* between two
  SAR passes, grounded in a deterministic per-block decibel-change grid
  (`docs/AI_INTEGRATION_IDEAS.md` C2 — the second Tier C VLM-in-the-loop
  capability, completing C2).** `umbra describe` reads one scene; this reads the
  *change* between two. The new `umbra_py.narrate` module (`[ai]` + `[viz]`
  extras) computes `compute_change_stats` — a coarse grid of the mean *signed*
  backscatter change in dB (`20·log10(later) − 20·log10(earlier)`: positive =
  brightened/appeared, the composite's green; negative = dimmed/vanished, its
  magenta) plus per-block change fractions — and hands the model both the change
  composite PNG and that grid, so the narration cites numbers rather than
  hallucinating change the pixels don't support. Add `--narrate` to `umbra change`
  (composite output only): it renders the composite once, writes it, prints a
  structured `ChangeNarration` (`{summary, changes[], confidence, caveats[]}`),
  and writes the machine-readable grid alongside as `<out>.narration.json` so
  every statement is auditable against a number a test can recompute. The model
  **only interprets**: the picture and the dB grid are produced deterministically,
  the reply passes the `parse_narration` boundary and never becomes a filter, a
  URL, or a measurement, and every narration carries the CC-BY attribution and the
  `AI_PROVENANCE` note. Like `umbra describe`, the model call is an injectable
  `Narrator` (and the render an injectable `ChangeRenderer`) reusing the same
  `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` provider plumbing (`OPENAI_BASE_URL` /
  `UMBRA_NARRATE_MODEL`, `--model`), `requests` only — no heavy SDK — so the whole
  feature is offline-testable with no network. It stays behind the `[ai]` extra
  and never runs implicitly. `narrate`, `parse_narration`, `compute_change_stats`,
  `ChangeNarration`, `ChangeStats` and `NarrateError` are exported at the top
  level.
- **`umbra describe "…"`: a vision model reads a SAR scene in plain language
  (`docs/AI_INTEGRATION_IDEAS.md` C2 — the first Tier C VLM-in-the-loop
  capability).** Searching gets you the scene; *reading* SAR is a separate skill
  (why is water dark? is that black patch shadow or an empty field?). The new
  `umbra_py.describe` module (`[ai]` + `[viz]` extras) renders an item's quicklook
  and sends that PNG plus the `UmbraItem.to_llm_context()` metadata card to a
  configured vision model, returning a structured `SceneDescription`:
  `{summary, observed_features[], confidence, caveats[]}`. `umbra describe
  <item-url>` prints the reading (`--json` for the object; `--asset` / `--no-db` /
  `--max-size` control the render; `--model` picks the model). The SAR literacy a
  general vision model lacks — backscatter ≠ brightness, speckle, layover/shadow,
  one-frame ≠ change — is encoded once in the packaged prompt. The model **only
  interprets**: the picture and metadata are produced deterministically, its
  reply passes the `parse_description` boundary and never becomes a filter, a URL,
  or a coordinate, and every description carries the CC-BY attribution plus a new
  `AI_PROVENANCE` note so a model's reading of radar is never mistaken for a
  measurement. Like `umbra ask`, the model call is an injectable `Describer` (and
  the render an injectable `Renderer`) chosen from `ANTHROPIC_API_KEY` /
  `OPENAI_API_KEY` (`OPENAI_BASE_URL` / `UMBRA_DESCRIBE_MODEL`), `requests` only —
  no heavy SDK — so the whole feature is offline-testable with no network. It
  stays behind the `[ai]` extra and never runs implicitly. `describe`,
  `SceneDescription`, `parse_description`, `DescribeError` and `AI_PROVENANCE` are
  exported at the top level.
- **Semantic task-name aliasing: the embedding layer of natural-language search
  (`docs/AI_INTEGRATION_IDEAS.md` C1 — the last open C1 piece, completing Phase
  3's natural-language-search line).** `--fuzzy` matches by the *words* in a task
  label; some queries share no word with the label they mean (Umbra's
  North-Dakota grain-storage site is named *"Beet Piler - ND"*), which only a
  model that has read about the world can bridge. The new `umbra_py.semantic`
  module (`[ai]` extra) embeds the catalog index's task names once and ranks them
  by meaning: `umbra semantic build` stores one vector per distinct task name in
  a small SQLite file beside `catalog.db` (schema-versioned with `PRAGMA
  user_version`; idempotent — a rebuild only embeds new names), and `umbra
  semantic search "grain storage north dakota"` embeds the query, ranks the
  stored vectors by cosine similarity, and prints the closest task names plus the
  exact `umbra search --area …` command for the best match (`--run` executes it,
  `--json` emits the ranking, `--top-k` / `--min-score` tune it) — the same
  "model proposes, library executes, user audits" boundary as `umbra ask`. The
  **only** model call is turning text into a vector: an injectable `Embedder`
  callable (default: an OpenAI-compatible `/embeddings` endpoint via the
  already-core `requests`, `OPENAI_API_KEY` / `OPENAI_BASE_URL` /
  `UMBRA_EMBED_MODEL`, no heavy SDK). Storage, cosine ranking and thresholding
  are stdlib-only — no `numpy`, no `sqlite-vec` binary dependency — so the whole
  feature is offline-testable with a deterministic stand-in embedder. It stays
  behind the `[ai]` extra and never runs implicitly; the deterministic `--area` /
  `--fuzzy` matchers remain the default search path. `SemanticTaskIndex`,
  `SemanticMatch`, `SemanticError`, `cosine_similarity` and `default_embedder`
  are exported at the top level.
- **`umbra ask "…"`: model-planned, deterministically executed natural-language
  search (`docs/AI_INTEGRATION_IDEAS.md` C1 — the capstone of the
  natural-language-search direction, and the first feature that calls a model).**
  A configured model reads the user's sentence plus the `llm_context()` domain
  document and returns the search *parameters* it maps to; the new
  `umbra_py.planner` module then re-validates every one of them deterministically
  (`parse_plan`) — dates through `parse_date_bound`, product types against
  `PRODUCT_ASSETS`, the bounding box range-checked, `place`/`bbox` enforced
  mutually exclusive — and prints the exact `umbra search` command it resolves
  to. **Nothing the model emits becomes a filter without passing that
  deterministic layer**, and the command is shown before it runs: the LLM plans,
  the library executes, the user audits. By default `umbra ask` only prints the
  plan; `--run` executes it (against a live walk or `--local` index), `--json`
  emits the resolved plan, and `--limit` overrides the model's cap. The feature
  lives behind a new `[ai]` extra and **never runs implicitly** — only `umbra
  ask` reaches a model, and only with a user-supplied key: `ANTHROPIC_API_KEY`,
  or `OPENAI_API_KEY` (with optional `OPENAI_BASE_URL` for any OpenAI-compatible
  endpoint), with `UMBRA_ASK_MODEL` / `--model` to pick the model. The provider
  call uses only the already-core `requests` (no heavy SDK). The planning step is
  an injectable callable (`ask(question, planner=…)`), so the whole feature —
  prompt building, plan validation, command rendering, provider selection, and
  the CLI — is fully offline-testable with no network. `ask`, `parse_plan`,
  `SearchPlan` and `AskError` are exported at the top level. Semantic task
  aliasing (`"grain storage north dakota"` → `"Beet Piler - ND"`) is the
  persistent, offline embedding-index answer to the same aliasing — see the
  `umbra semantic` entry above, which closes out C1.
- **Fuzzy task matching for `--area` search (`docs/AI_INTEGRATION_IDEAS.md` C1 —
  the second deterministic step of Phase 3).** `--area` (and the
  `UmbraCatalog.search` / `CatalogIndex.search` `area=` argument, and the MCP
  `search_catalog` tool) stays a literal case-insensitive substring by default;
  passing `--fuzzy` / `fuzzy=True` widens it to a token-wise match resolved by a
  new stdlib-only `umbra_py.fuzzy` module (`task_matches` / `matching_tasks`,
  exported at the top level). The fuzzy match is **word-order- and
  punctuation-independent and tolerant of a small typo** — so `"utah
  centerfield"`, `"centerfield utah"` and `"centrfield"` all still reach
  `"Centerfield, Utah"` — while requiring *every* query token to match, which
  keeps precision. It is a **strict superset** of the substring match (it never
  drops a result), and the live (`UmbraCatalog`) and indexed (`CatalogIndex`)
  search paths share the one matcher and are tested to agree. **No model is
  called at runtime**, so it stays inside the library's determinism boundary and
  is fully offline-testable. `--fuzzy` is available on `search` and on the
  area-taking render commands (`change`, `timescan`, `swipe`, `gallery`).
  Semantic aliasing (`"grain storage north dakota"` → `"Beet Piler - ND"`) is
  deliberately out of scope — it needs the future embedding index, not plain
  string similarity.
- **Natural-language date bounds for search (`docs/AI_INTEGRATION_IDEAS.md` C1 —
  the deterministic first step of Phase 3).** `--start` / `--end` (and the
  `UmbraCatalog.search` / `CatalogIndex.search` keyword arguments, and the MCP
  `search_catalog` tool) now accept human date expressions in addition to
  `YYYY-MM-DD`: a bare year or year-month (`2024`, `2024-03`), the keywords
  `today` / `yesterday` / `tomorrow`, a relative offset (`3 months ago`,
  `a week ago`), or a period (`this month`, `last year`). Resolution is a new
  stdlib-only `umbra_py.dates.parse_date_bound` (exported at the top level) that
  uses plain calendar arithmetic — **no model call at runtime**, so it stays
  inside the library's determinism boundary and is fully offline-testable. It is
  *bound-aware*: a span expression snaps to its first day as a `--start` and its
  last day as an `--end`, so `--start 2024 --end 2024` covers the whole year and
  `--end last month` includes the last day of that month. Because every command
  that takes a date range funnels through the single `_coerce_date` choke point,
  `search`, `index build`, `change`, `timescan`, `swipe`, `map` and `gallery`
  all gain this at once. Full ISO dates behave exactly as before.
- **`llms.txt` context bundle (`docs/AI_INTEGRATION_IDEAS.md` A2 — the last open
  Phase 2 item).** `umbra_py.llms_txt()` / `llms_full_txt()` (CLI: `umbra
  llms-txt [--full]`) render the [llms.txt-convention](https://llmstxt.org/)
  Markdown that a language model pulls in to learn how to *drive* the library —
  the *user* agent guide, complementing `AGENTS.md` (the contributor guide) and
  the machine-readable `umbra context` JSON. The concise `llms.txt` is the
  index; `llms-full.txt` is the self-contained bundle: the determinism boundary,
  the domain knowledge (reusing `llm_context()`), the full CLI command reference
  introspected from the live command tree, the AI-native interfaces, and each
  core module's explanatory docstring. It is assembled entirely from facts
  already in the package — module docstrings are read via `ast` rather than by
  importing the modules, so the generator is deterministic and stdlib-only and
  runs in the bare core install without pulling in a heavy extra. The committed
  repo-root `llms.txt` / `llms-full.txt` are that rendered output; a golden test
  keeps them from drifting (regenerate with `umbra llms-txt > llms.txt && umbra
  llms-txt --full > llms-full.txt`).
- **Local-index rendering for the visual commands (`docs/DEMO_APP_GAPS.md` G2 /
  Path A step 2).** `umbra map`, `gallery`, `swipe`, `change` and `timescan` now
  accept the same `--local` / `--index-db` options as `umbra search`, so they
  render from a prebuilt catalog index (`umbra index fetch` / `umbra index
  build`) instead of re-walking S3 on every invocation. Previously only `umbra
  search` could use the index; a fully built `catalog.db` did nothing for the
  visual commands, which each re-crawled the bucket live — the gap
  `DEMO_APP_GAPS.md` named as the next step to a fast, self-serve demo (R5). The
  search backend is chosen by the shared `_gather_items` helper (the same
  `CatalogIndex`-vs-live `UmbraCatalog` split `search` already used), so every
  filter behaves identically to the live path; only acquisitions already in the
  index are returned. The path flag is `--index-db` (not `--db`) because the
  render commands already use `--db` for the decibel stretch. Without `--local`
  the commands walk S3 live exactly as before.
- **`umbra serve`: a read-only STAC API façade over the catalog index
  (`docs/AI_INTEGRATION_IDEAS.md` B2 / `docs/DEMO_APP_GAPS.md` Path B step 1).**
  Umbra publishes a *static* STAC catalog and **no** search API, which is
  exactly what breaks the standard geospatial tooling — `pystac-client`, the
  QGIS STAC plugin, `stac-browser` and leafmap all speak the STAC API *search*
  protocol and have nothing to query. This serves that protocol over
  `CatalogIndex`, so pointing any STAC client at `http://localhost:8000` makes
  Umbra's open archive searchable like Sentinel-1 or Landsat. It is the
  browser-facing sibling of `umbra-mcp`: same index underneath, a different
  front door, and the shared foundation the demo application (`DEMO_APP_GAPS.md`
  Path B) wants. Run it with `umbra serve`; needs the new `[serve]` extra
  (`pip install "umbra-py[serve]"`).

  - **Endpoints:** the STAC API landing page (`/`), `/conformance`,
    `/collections`, `/collections/{id}`, `/collections/{id}/items`,
    `/collections/{id}/items/{item_id}`, and STAC item search over both
    `GET /search` and `POST /search` (bbox, datetime interval, ids, limit, and
    opaque-token pagination). FastAPI generates the OpenAPI document at
    `/openapi.json` and interactive docs at `/docs` for free — the schema'd REST
    surface OpenAPI-driven agents consume without custom glue.
  - **Index-first:** every query is a local SQL read against the prebuilt
    `catalog.db` (`umbra index fetch`), so the server answers in milliseconds
    rather than re-walking S3. `--live` opts into a per-request S3 walk (slow)
    for a quick try without an index; a missing index returns `503` with a hint.
  - **Deterministic, thin edge** (mirrors `umbra-mcp`): the STAC documents are
    built by plain, offline functions with no web-framework dependency (so they
    are unit-testable in the core install), and the CC-BY attribution travels in
    the landing page and collection metadata. A fresh backend is opened and
    closed per request, so the app is safe under FastAPI's thread pool.
- **`umbra-mcp`: a Model Context Protocol server over the library (the flagship
  AI-integration deliverable, `docs/AI_INTEGRATION_IDEAS.md` B1 / Phase 2).**
  Umbra publishes no STAC API, so this library *is* the query layer — and this
  server exposes it over MCP, turning any MCP client (Claude Desktop / Code and
  others) into a zero-install, natural-language front door to a 17+ TB public
  SAR archive. Run it with `umbra mcp`, `umbra-mcp`, or `uvx umbra-mcp` (stdio
  transport); needs the new `[mcp]` extra (`pip install "umbra-py[mcp]"`).

  - **Tools** (thin wrappers over the existing public API): `search_catalog`
    (returns compact `to_llm_context()` cards, not full STAC JSON, to protect
    the context window), `get_item`, `geocode_place`, `index_stats`,
    `quicklook`, `change_composite`, `timescan`, and `download_asset` (gated by
    a two-step size-confirmation handshake). The three imagery tools return the
    rendered PNG as an MCP **image content block**, so the model *sees* the
    radar scene.
  - **Resources:** `umbra://context` (the `llm_context()` document) and
    `umbra://index/stats`. **Prompts:** packaged `monitor-site` and
    `survey-region` workflows.
  - **Deterministic core, AI at the edges** (the `[ai]`/determinism policy in
    `AGENTS.md`): nothing here calls a model — the server searches, geocodes and
    renders; the client's model plans and narrates. `change_composite` refuses
    to mix polarizations (HH vs VV are not comparable), and the CC-BY
    attribution line travels with every result.
- **AI-legible surface (Tier A groundwork): context cards, an `llm_context()`
  document, and `__geo_interface__`.** The friction in using Umbra's open data
  is interpretive — knowing *what to ask for* — which is exactly what a language
  model answers well when it has the domain facts in context. This lands the
  zero-dependency, deterministic groundwork the flagship MCP server and every
  later AI phase consume (`docs/AI_INTEGRATION_IDEAS.md` Phase 1):

  - `UmbraItem.to_llm_context()` — a compact, explanation-rich context card:
    like `metadata_summary()` but every present product type carries a one-line
    explanation, the polarizations carry the change-detection caveat, and the
    CC-BY attribution line travels with the data. Surfaced on the CLI as
    `umbra info <url> --json`.
  - `umbra_py.llm_context()` / `umbra context` — the library's self-describing
    document (product-type table, search-parameter semantics, license rules) an
    agent pulls into context to drive umbra-py in one shot.
  - `UmbraItem.__geo_interface__` / `ItemCollection.__geo_interface__` — the
    Python geo-interface protocol, so geopandas / shapely / leafmap ingest a
    search with zero glue (`gpd.GeoDataFrame.from_features(results)`).

  All of it is deterministic and offline (no network, no model call); the
  determinism boundary is now written into `AGENTS.md`.
- **Fetch the prebuilt catalog index (`CatalogIndex.from_release`, `umbra index
  fetch`).** The weekly workflow already publishes a `catalog.db` snapshot on
  the rolling `catalog-index` release, but a fresh install still had to crawl
  the whole S3 bucket before `umbra search --local` returned anything. The new
  fetch step downloads that snapshot straight to the default index path via the
  existing resume-safe `download_url`, so whole-catalog local search works out
  of the box — no crawl:

  ```bash
  umbra index fetch                 # download the weekly snapshot (seconds)
  umbra search --local --area "Centerfield, Utah"   # instant, offline
  ```

  ```python
  from umbra_py import CatalogIndex

  with CatalogIndex.from_release() as index:   # download + open
      for item in index.search(area="centerfield"):
          print(item.summary())
  ```

  `umbra index build` now stamps the index with a `built_at` date, and
  `umbra index info` reports it with staleness (e.g. `built : 2026-07-14 (1
  day(s) ago)`) so a downloaded snapshot's age is visible. This is the consume
  side of the publish workflow shipped in PR #26 — the last prerequisite the
  strategy, demo, and AI-integration docs named before the demo / MCP / STAC-API
  layers.
- **stac-geoparquet catalog export (`export_geoparquet`, `umbra index
  export`).** A local `CatalogIndex` makes *your* searches fast, but everyone
  still pays for their own crawl of Umbra's bucket. The new export writes an
  index out as a single [stac-geoparquet](https://stac-geoparquet.org/) file —
  the entire catalog searchable in seconds with DuckDB, geopandas, pyarrow or
  rustac, no server, no crawl, no umbra-py needed on the consuming side. Each
  row is the full STAC item, with a `self` link injected back to its sidecar
  JSON so query results lead straight to the data files (items without a
  footprint geometry are skipped and counted):

  ```bash
  umbra index build                                  # walk S3 once
  umbra index export --out umbra-open-data.parquet   # ship the catalog
  ```

  ```python
  from umbra_py import CatalogIndex, export_geoparquet

  with CatalogIndex("umbra.db") as index:
      export_geoparquet(index.search(), "umbra-open-data.parquet")
  ```

  A new scheduled workflow (`.github/workflows/publish-index.yml`) rebuilds
  the full index weekly and publishes `umbra-open-data.parquet` + `catalog.db`
  on the rolling `catalog-index` GitHub release, so users can search the whole
  catalog without ever crawling it. New public `export_geoparquet`; new
  `export` extra (`stac-geoparquet`). Project strategy notes tracking this and
  related ideas live in `docs/STRATEGY.md`.
- **Interactive full-resolution viewer (`view`, `umbra view`).** Every other
  rendering surface collapses a scene to a fixed picture — `quicklook` writes
  one downsampled PNG — which throws away the resolution that makes Umbra
  special (a GEC scene is ~25 cm imagery). `view` starts a tiny local tile
  server and opens a Leaflet map in the browser; as you pan and zoom, only the
  tiles in view stream from the cloud-optimized GeoTIFF via HTTP range requests
  (at the COG overview matching your zoom) and are warped into the Web-Mercator
  map grid — native-resolution exploration with no full download:

  ```bash
  umbra view <item-json-url> --db        # Ctrl-C to stop
  ```

  ```python
  from umbra_py import view
  view(item, db=True)                    # opens the browser
  ```

  The contrast stretch is computed once over a whole-scene overview and shared
  by every tile, so neighbouring tiles don't seam; tiles are warped through
  GDAL into true Web Mercator, so the imagery lines up with the OpenStreetMap
  basemap (unlike the bbox-stretch quick-look approximation used by the
  browser-side lazy overlay). `make_viewer_server(item, ...)` returns the
  unstarted server for embedding. Requires the `viz` extra.
- **Local catalog index (`CatalogIndex`, `umbra index`).** Umbra has no STAC
  API, so every search re-walks the public S3 bucket — fine once, slow on
  repeat. The new `CatalogIndex` persists the items a walk discovers into a
  local SQLite database and answers searches from SQL, so a repeat (or
  overlapping) search is a near-instant local query instead of a fresh crawl:

  ```bash
  umbra index build --area "Centerfield" --start 2024-01-01 --end 2024-12-31
  umbra search --local --area "Centerfield" --product GEC
  umbra index info
  ```

  ```python
  from umbra_py import CatalogIndex

  with CatalogIndex("umbra.db") as index:
      index.build(area="centerfield")            # walk S3 once, persist
      list(index.search(area="centerfield"))     # local, no network
  ```

  Run `umbra index build` (or `CatalogIndex.build()`) with **no filters to
  index the whole catalog** — one long, one-time crawl that makes every later
  `--local` search instant — or pass the usual `--area`/`--bbox`/`--start`/
  `--end` to scope it to a slice. The CLI shows a live running tally while it
  walks (a `progress` callback on `build`).

  Each acquisition is one row keyed by its sidecar URL, carrying the columns
  the filters need (acquisition date, bounding box, task, product assets) plus
  the full STAC JSON so items rebuild without another network round trip.
  `CatalogIndex.search` mirrors `UmbraCatalog.search` (bbox / date / product /
  area / limit / max_per_task); `build` is an idempotent upsert, so an index
  refreshes and grows incrementally. It's a deliberate, reusable building block
  — the substrate for a shared, prebuilt catalog (walk once, ship the `.db`) or
  a service layered on this library. `umbra search` gains `--local` / `--db`
  to query an index instead of S3; the index path defaults to `$UMBRA_INDEX_DB`
  or `~/.cache/umbra-py/catalog.db`. New public `CatalogIndex` and
  `default_index_path`. No new dependencies (SQLite is stdlib).
- **Timescan composite (`umbra timescan`).** Collapse a site's *entire* time
  series into a single temporal-statistics image, rather than the 2–3 dates
  `umbra change` is limited to. Each pixel is summarised across all passes and
  mapped to color — **red = mean** backscatter, **green = peak**, **blue =
  temporal standard deviation (variability)**:

  ```bash
  umbra timescan --area "Centerfield" --start 2024-01-01 --end 2024-12-31 \
      --out timescan.png --db
  ```

  Stable terrain (no variability) renders gray/yellow; anything that came and
  went over the series — ships cycling through a berth, vehicles in a lot, a
  field flooding — has high variability and glows blue/cyan, turning a whole
  archive into one glanceable "where did activity happen" picture. Accepts 3+
  STAC item URLs directly or a search (`--area`/`--bbox`/`--place` +
  `--start`/`--end`, preferring a single polarization). `--place` geocodes a
  name to a bounding box like the other search commands. Reuses the
  change-detection
  co-registration; only downsampled overviews are streamed via range requests.
  New public `timescan_composite` / `save_timescan_composite` functions.
  Requires the `viz` extra.
- **Gallery groups acquisitions by task.** `umbra gallery` (and
  `gallery` / `save_gallery`) now lay the contact sheet out as labelled
  per-task sections, so repeat passes of one site sit next to each other under
  the task's name (e.g. "Centerfield, Utah") instead of being scattered through
  one flat grid. A single-task gallery stays a flat grid. The new
  `UmbraItem.task` property exposes the task label an item belongs to.
- **Search by place name (`--place`).** The `search`, `map`, and `gallery`
  commands now accept `--place` (and there's a public `geocode_place` function)
  so you can search a fuzzy geography instead of hand-typing a bounding box:

  ```bash
  umbra gallery --place California --out california.html
  umbra search --place "Tokyo" --start 2024-01-01 --end 2024-12-31
  ```

  The name is forward-geocoded to a bounding box via OpenStreetMap Nominatim
  (the inverse of the existing reverse-geocoder used for map popups), and the
  resolved place is echoed so you can confirm the match. The box is rectangular
  — searching `California` also catches footprints in the box's corners that
  fall just outside the state outline — matching the bbox-overlap semantics the
  rest of the search already uses. Mutually exclusive with `--bbox`. Raises the
  new `GeocodeError` when a name can't be resolved.
- **Interactive search gallery / contact sheet.** New `umbra gallery` CLI
  command and `gallery` / `save_gallery` functions take a search (area + dates,
  or a bbox / product filter) and render a grid of streamed SAR quicklook
  thumbnails into one self-contained HTML page — each tile linking to its STAC
  item with a footprint sketch:

  ```bash
  umbra gallery --area Centerfield --out gallery.html
  ```

  It's the missing "browse the catalog visually" primitive: only downsampled
  cloud-optimized GeoTIFF overviews are fetched (via HTTP range requests, in
  parallel) — never a full download — so you can *see* what a search returned
  before committing to multi-gigabyte SAR files. Thumbnails default to the
  radiometrically-correct decibel stretch; any item that can't be previewed
  falls back to its footprint sketch, so one bad acquisition never sinks the
  page. Each tile also carries a collapsible **URLs** panel with the asset's
  direct download URL (the GEC GeoTIFF, for `curl` / GDAL `/vsicurl`) and the
  STAC item URL (for `umbra info | download | quicklook | load`), each in a
  click-to-select box so you can copy a URL straight into another command.
  Built directly on the existing `quicklook` + lazy-overview reader. Requires
  the `viz` extra.
- **Rich notebook rendering for items and search results.** `UmbraItem` now
  has a Jupyter `_repr_html_`, so an item displayed in a notebook renders as a
  card — a metadata table next to an inline SVG sketch of its ground footprint
  (north up) — instead of a bare `repr`. The new `ItemCollection` (a drop-in
  `list` subclass, exported from the package root) renders a *list* of results
  as a wrapping gallery of those cards:

  ```python
  from umbra_py import UmbraCatalog, ItemCollection
  results = ItemCollection(UmbraCatalog().search(area="rome", limit=8))
  results  # -> gallery of metadata cards (offline, core install, no network)
  ```

  Both representations are pure-stdlib and offline by default — displaying an
  item never triggers a network read, so notebooks stay snappy and the feature
  works without any extras. Pass `ItemCollection(..., thumbnails=True)` to opt
  into streamed SAR quicklook thumbnails (decibel-stretched, only the overview
  bytes are fetched per the existing `quicklook` path; needs the `viz` extra).
  Thumbnails are fetched lazily on display, and any item that can't be
  previewed falls back to its footprint card, so a repr never raises. This is
  the lowest-friction way to *see* what a search returned without leaving the
  notebook.
- **Interactive before/after SAR swipe maps.** New `umbra swipe` CLI command
  and `swipe_map` / `save_swipe_map` functions render two passes of the same
  site into a single self-contained HTML map with a draggable divider: the
  *before* acquisition fills the left of the seam, *after* the right, and
  dragging the handle wipes one over the other across the same ground. SAR's
  backscatter is stable between passes, so anything that changed — a ship that
  docked, a field that flooded, a building that rose — snaps in and out as you
  sweep the seam. Where `change_composite` bakes the comparison into one
  colored still and `change_animation` flips between dates, this lets you
  *feel* the change interactively. Like `umbra change`, it works two ways: pass
  two STAC URLs in chronological order, or search a site by
  `--area`/`--bbox` + `--start`/`--end` and it compares the earliest and latest
  pass (preferring a single polarization). The two acquisitions are
  co-registered onto their shared footprint intersection (the same warp
  `change_composite` uses), so both sides cover identical ground at identical
  scale and line up across the seam; only the requested overview resolution of
  each cloud-optimized GeoTIFF is streamed, no full download. `--db` selects
  the radiometrically-correct decibel stretch. `image_overlay` gained a
  matching `db=` option. Requires the `viz` extra.
- **Analysis-ready loading into `xarray` (the "load" step).** New
  `to_xarray(item)` turns a geocoded Umbra GeoTIFF into a georeferenced
  `xarray.DataArray` — `y`/`x` coordinate axes in the raster's native CRS,
  CRS / affine transform / bounds / acquisition metadata in `.attrs`, and the
  CC BY 4.0 attribution carried along — so the data drops straight into the
  scientific Python stack (`xarray`/`dask`/`matplotlib`/`scikit-image`/
  `rioxarray`). This is the missing verb in the project's "discover, **load**,
  download, analyze" tagline: previously you had to hand-roll `rasterio`
  windowing and coordinate construction to get an array. `bbox=` reads only a
  geographic sub-window (reprojected to the raster's CRS first), `max_size=`
  decimates via the cloud-optimized GeoTIFF overviews, and `db=` returns the
  radiometric decibel scale. Because the source is a COG read through
  `/vsicurl/`, only the requested window/resolution is streamed over HTTP range
  requests — no multi-gigabyte download. New `load` extra
  (`pip install "umbra-py[load]"`, pulls in `xarray` + `rasterio` + `numpy`).
  A file-producing companion `to_geotiff(item, dest)` and an `umbra load
  <item-url> --out scene.tif` CLI command write the same clipped/decimated
  scene to a single-band float32 GeoTIFF (in the source CRS, nodata as `NaN`)
  for QGIS / GDAL users who want a file rather than an in-memory array; both
  honor `--bbox` / `--max-size` / `--db`.
- **Animated SAR time-lapses across a whole series.** Where a change
  composite collapses 2–3 dates into one colored image, `umbra change`
  now also produces an animated GIF over *any* number of acquisitions when
  `--out` ends in `.gif` —
  `umbra change --area "Centerfield" --start 2024-01-01 --end 2024-12-31
  --out lapse.gif --db`. Every matched acquisition becomes a frame, all
  co-registered onto the shared footprint intersection so the site stays put
  and only the scene evolves; each frame is a SAR quicklook stamped with its
  acquisition date. `--fps` sets playback speed and `--colormap` pseudo-colors
  the frames. Explicit-URL mode lifts its 2–3 cap for `.gif` output (pass as
  many as you like). New public `change_animation` / `save_change_animation`
  functions; `select_change_frames(..., frames=None)` returns the whole
  single-polarization series for this path. Requires the `viz` extra.
- **One-command change composites by site + time range.** `umbra change`
  gained a search mode: instead of passing 2–3 STAC URLs, give
  `--area "<site>"` (or `--bbox`) with `--start`/`--end` and it gathers the
  site's acquisitions and auto-selects the dates to composite —
  `umbra change --area "Centerfield" --start 2024-01-01 --end 2024-12-31
  --out change.png`. `--frames {2,3}` picks how many dates (default 2),
  spread evenly from earliest to latest across the matched range. Selection
  prefers a single polarization (the largest same-polarization group), since
  compositing HH against VV would render the polarization difference as fake
  "change"; if no same-polarization pair exists it falls back to comparing
  across polarizations and warns. The chosen acquisitions are printed before
  rendering. Exposed as a reusable `select_change_frames(items, frames=2)`
  helper in the public API. The explicit-URL form still works; the two modes
  are mutually exclusive.
- **Search by area name** via a new `area=` argument on
  `UmbraCatalog.search` and an `umbra search --area "<name>"` CLI flag.
  Umbra files every pass of a site under one named task directory (e.g.
  `sar-data/tasks/Centerfield, Utah/`), so `--area centerfield` returns
  just that site's acquisitions. The match is a case-insensitive substring
  on the task-directory name, applied *before* each directory is listed, so
  non-matching tasks are skipped entirely — making a name-scoped search much
  faster than an unfiltered walk. This is the ergonomic way to gather the
  co-located passes a change composite needs: `umbra search --area X` →
  pick 2–3 same-polarization URLs → `umbra change`.
- **Multi-temporal SAR change composites** via new `change_composite` /
  `save_change_composite` functions and an `umbra change <url> <url>
  [<url>] --out change.png` CLI command. Pass 2–3 acquisitions of the
  same site (e.g. items from one Umbra task) in chronological order; the
  bands are co-registered onto a shared lon/lat grid (each cloud-optimized
  GeoTIFF is read at a downsampled resolution via HTTP range requests and
  warped so the same output pixel is the same ground location on every
  date), percentile-stretched, and assigned to color channels. Unchanged
  ground stays gray while change is tinted by *when* it happened: for two
  dates, **green** = backscatter that appeared in the later pass, **magenta**
  = backscatter that vanished; for three dates, an earliest→latest red/green/
  blue temporal-RGB. Only the area imaged on every pass is colored (pixels
  missing from any acquisition are transparent), and `--db` switches to the
  radiometrically-correct decibel stretch. This is SAR's signature change-
  detection view with no manual co-registration. Requires the `viz` extra.
  The percentile/dB stretch shared with the quicklook path was factored into
  a `_normalize_band` helper.
- **Standalone SAR quicklooks** via new `quicklook` / `save_quicklook`
  functions and an `umbra quicklook <item-url> --out scene.png` CLI
  command. This is the lowest-friction way to *see* an Umbra
  acquisition: it streams a downsampled preview of the item's
  cloud-optimized GeoTIFF via HTTP range requests (no multi-gigabyte
  download, no Folium map, no GIS) and writes a plain image whose
  format follows the output extension. The raster is read in its
  native, already-geocoded projection — a faithful look at the pixels
  rather than a map-placeable warp. Two SAR-specific rendering options:
  `--db` switches to a decibel (log-amplitude) stretch — the
  radiometrically-correct view that reveals terrain texture and urban
  structure the default linear stretch crushes toward black — and
  `--colormap NAME` (e.g. `viridis`, `magma`) pseudo-colors the result
  through any matplotlib colormap. Tunables match the map overlays:
  `--asset` (default `GEC`), `--max-size` (default 2048), `--percentile`
  (default `2,98`). Requires the `viz` extra. The `_stretch_to_rgba`
  helper grew matching `db` / `colormap` parameters, and the rasterio
  read shared with `image_overlay` was factored into `_read_sar_band`.
- **Browser-side lazy SAR imagery** via a new `lazy_imagery=True` kwarg
  on `footprint_map` and `timeline_map`, plus a matching
  `umbra map --lazy-imagery` CLI flag. Each popup gets a "Get SAR
  image" button; on click, the page lazily loads
  [`geotiff.js`](https://geotiffjs.github.io/) (from a pinned CDN),
  streams a low-resolution overview of the GEC cloud-optimized GeoTIFF
  directly from the Umbra public bucket via HTTP range requests,
  applies the same percentile-and-transparent-invalid-pixels stretch
  Python's `_stretch_to_rgba` uses, and drops it on the map as a plain
  Leaflet `L.imageOverlay` placed at the item's footprint. Second
  click removes it. A 200-item map weighs ~30 KB regardless of how
  many items it carries — users only pay the fetch cost for items they
  actually open. Works with `--timeline` (scrub to a moment, click the
  polygon, see the actual SAR), and is mutually exclusive with the
  pre-baked `--imagery` overlay path. Tunables: `lazy_imagery_asset`
  (default `"GEC"`), `lazy_imagery_percentile` (default `(2.0, 98.0)`).

  Decoding runs on the main thread (no Web Workers), so the saved HTML
  works whether opened over http(s) **or** straight off disk
  (`file://`). Placement stretches the geocoded raster onto its
  lat/lon footprint bbox rather than reprojecting — a quick-look
  approximation; use `imagery=True` for a pixel-accurate, GDAL-
  reprojected overlay.


- `umbra_py.timeline_map` / `save_timeline_map` and a matching `umbra
  map --timeline` CLI flag: render search results as a
  TimestampedGeoJson layer so Umbra's coverage accumulates beneath a
  play button + slider. Each footprint surfaces at its acquisition
  timestamp and keeps the same metadata popup as `footprint_map`.
  Tunables: `period` (slider step, ISO 8601 — `"PT1H"`/`"P1D"`/`"P7D"`
  match a day's / month's / year's search density), `duration` (how
  long each footprint stays visible — `None` accumulates, an ISO
  duration fades it back out), `auto_play`, `loop`, `transition_time`,
  and `geocode` / `geocode_zoom` (same Nominatim reverse-geocoding
  behavior as `footprint_map` — the resolved place name is baked into
  the popup before it ships into the TimestampedGeoJson payload, since
  the plugin renders properties verbatim). The CLI's existing
  `--geocode/--no-geocode` flag now flows through to `--timeline` too.
  `--timeline` is still rejected with `--imagery` (animating base64
  SAR rasters across the slider is a separate, larger lift) or with
  non-HTML output extensions.
- `UmbraCatalog.search(max_per_task=N)` (and `--max-per-task N` on `umbra
  search` / `umbra map`): cap how many items are yielded from any one
  `sar-data/tasks/<task>/` directory. Each task is repeated imaging of
  the same area, so `--max-per-task 1` swaps the usual "every revisit of
  a few sites" output for "one acquisition per distinct site" — much
  better diversity on a map.
- `umbra map --imagery-max-size N` to control how big each SAR overlay
  is read at. Default stays 1024 (modest HTML size); bump to 2048 or
  4096 for sharper overlays at quadratically larger filesizes. Useful
  when you want to zoom in on a single acquisition; remember SAR is
  inherently speckled, so higher resolutions also reveal more noise.
- A small 3-line satellite-orbit animation runs on stderr during
  `umbra map` and `umbra search` to show the catalog walk is making
  progress. Auto-suppressed when stderr isn't a TTY (CI, piped output)
  so captured logs stay clean.

### Fixed
- **Critical: S3 listings silently truncated at 1,000 keys.** The bucket
  lister built `ListObjects` URLs without the `list-type=2` parameter, so S3
  served the **V1** API — which ignores the `continuation-token` the code
  sends and never returns the `NextContinuationToken` it looks for. Every
  listing therefore stopped after its first page: any task directory with more
  than 1,000 objects (e.g. *Centerfield, Utah*) had acquisitions **silently
  missing from every search, index build, gallery, timescan, and change
  detection**, and once Umbra publishes its 1,001st task, whole tasks would
  vanish from top-level discovery with no error. Both `_list_prefix` (delimited
  task discovery) and `_stream_keys` (per-task streaming) now send
  `list-type=2`, so `continuation-token` is honored and every page is consumed.
  Covered by offline regression tests that drive both listers across two
  truncated pages, plus a `network`-marked test asserting a >1,000-key task
  streams past its first page against the live bucket. This is the prerequisite
  the strategy/demo/AI-integration docs name for any "full catalog" work —
  search results are complete again.
- **NumPy 2.5 `DeprecationWarning` from raster reads.** `to_xarray` /
  `to_geotiff` and the viz overview readers (`quicklook`, change/swipe
  composites) read a single band via rasterio's scalar-index `read(1, …)`
  path, which squeezes the band axis with an in-place `ndarray.shape`
  assignment — deprecated in NumPy 2.5, so every read emitted a warning on
  Python 3.12+/NumPy ≥2.5. These now read with a list index into a 3-D
  `out_shape` and drop the band axis explicitly (`read([1], …)[0]`), which
  returns the identical array with no in-place reshape. Output is unchanged;
  the warnings are gone.
- `UmbraItem.asset_href` now resolves a public, fetchable HTTPS URL for
  items built directly from a published STAC sidecar (i.e. `umbra info`,
  `umbra download`, `umbra quicklook`, or `UmbraItem.from_dict(get_json(url))`).
  Umbra's `*.stac.v2.json` sidecars list asset hrefs as `s3://` URLs into a
  *private* processing bucket; the old code returned those verbatim, so
  `rasterio`/CURL failed with `Protocol "s3" not supported` and downloads
  pointed at an inaccessible bucket. The download products actually sit next
  to the sidecar in the open bucket, so any non-HTTP(S) href is now rewritten
  to the sibling public URL relative to the item's own sidecar `href` — which
  also fixes named-task layouts (`tasks/<name>/<task_id>/<acq>/…`) where
  reconstructing from `umbra:task_id` alone produced a 404. `UmbraCatalog.search`
  was unaffected (it already rebuilt public hrefs while walking the bucket).

### Changed
- **Breaking:** `UmbraCatalog.search` now walks Umbra's live data layout
  at `sar-data/tasks/<task>/[<uuid>/]<acquisition>/` (each acquisition has
  a `*.stac.v2.json` sidecar) instead of the legacy `stac/catalog.json`
  tree. The v1 tree is mostly metadata stubs that reference data Umbra
  never published — a 60-item v1 search returned exactly one downloadable
  item. The v2 walker enumerates the actual published acquisitions, so
  every item returned has resolvable asset URLs. Date pruning still works:
  acquisition directory names start with `YYYY-MM-DD-HH-MM-SS`, and the
  walker skips subtrees outside the requested `start` / `end` range.
  Provide a date range — without one the walker scans every published
  acquisition, which takes minutes.
- **Breaking:** `UmbraCatalog(root_url=...)` is gone. Configure the bucket
  via `UmbraCatalog(bucket=..., region=...)` if you ever need a non-default
  endpoint.

### Removed
- **Breaking:** `UmbraCatalog.available_task_ids()` and the
  `search(data_available_only=...)` flag, plus the matching
  `umbra search --available-only` / `umbra map --available-only` flags.
  They were stopgaps that filtered the v1 walk; the v2 walker only ever
  returns items whose data is published, so the filter is redundant.
- **Breaking:** `umbra_py.constants.DEFAULT_STAC_ROOT` (was never publicly
  re-exported).

### Added
- `umbra_py.viz` module for visualizing search results.
  - `item_to_feature`, `items_to_featurecollection`, `write_geojson`:
    convert items to GeoJSON for QGIS, leafmap, Earth Engine, geopandas,
    deck.gl, or any other tool that reads GeoJSON. The third coordinate of
    Umbra's 3D footprints is stripped so they render in 2D viewers.
  - `footprint_map`, `save_footprint_map`: build an interactive Folium map
    of one or more acquisitions, with auto-fit bounds and a metadata popup
    per item. Requires the `viz` extra.
  - `UmbraItem.to_geojson()` convenience method.
- `umbra map` CLI subcommand: search the catalog and write an interactive
  HTML map (`--out footprints.html`) or a GeoJSON FeatureCollection
  (`--out footprints.geojson`) to disk.
- `UmbraItem.asset_href` now resolves empty hrefs in recent Umbra STAC
  items. Umbra currently publishes every asset with `"href": ""` and
  expects consumers to reconstruct the URL from `umbra:task_id` and a
  rename mapping (`<base>_MM.tif` -> `<base>_GEC.tif`, etc.). Items with
  populated hrefs are returned unchanged, so older catalogs and the
  offline test fixture keep working. Unblocks live downloads and the SAR
  image overlay against 2024+ items.
- SAR image overlays on the Folium map.
  - `image_overlay(item)`: stream a downsampled preview of an item's GEC
    cloud-optimized GeoTIFF via HTTP range requests (no full download),
    apply a percentile contrast stretch to handle SAR's wide dynamic
    range, reproject to lat/lon if needed, and return a Folium
    `ImageOverlay` ready to drop onto any map.
  - `footprint_map(items, imagery=True)` / `umbra map --imagery`: one-call
    convenience that combines footprints with the SAR imagery. Each
    overlay is embedded as a base64 PNG so the resulting HTML file is
    self-contained — no tile server required.
  - The `viz` extra now also pulls in `rasterio` and `numpy` for the
    image-overlay path; folium-only users are unaffected.
  - `footprint_map(items, imagery=True)` is resilient to per-item
    failures: when one item's GEC asset is unreachable (404, network
    error, missing pixels), it emits a `UserWarning` and continues, so
    the remaining footprints and overlays still render. Umbra's public
    bucket has many STAC items whose binary data was never published,
    and the previous behavior crashed the whole map on the first one.
  - `image_overlay` now raises `AssetNotFoundError` with a clear message
    when the asset's URL can't be resolved (empty href, no
    `umbra:task_id`), instead of passing an empty URL to rasterio.
  - `footprint_map` now also draws a small always-visible circle marker
    at each footprint's centroid and a fixed-position legend in the
    top-right corner. Filled markers indicate items whose SAR imagery
    was rendered; outlined markers are footprint-only. This solves the
    "I have items, but I can't see any dots at world zoom" problem
    Umbra footprints are only a few km across.

## [0.1.0] - 2026-05-22

Initial release. Discovery + download core for Umbra's open SAR data.

### Added
- `UmbraCatalog`: search Umbra's static STAC catalog by bounding box, date
  range, and product type, with date-based pruning of the catalog tree so a
  constrained search only fetches relevant day catalogs.
- `UmbraItem`: lightweight dataclass over STAC items with metadata accessors
  (platform, product type, polarizations, resolution, incidence angle, …),
  bbox derivation from 3D geometry, and human-readable summaries.
- Anonymous HTTPS downloads (`download_url`, `download_asset`, `download_item`)
  with resume support and progress callbacks.
- `umbra` CLI with `search`, `info`, and `download` commands.
- Optional `convert` extra: `sicd_to_amplitude_geotiff` for inspection-quality
  amplitude extraction from SICD.
- Project scaffolding: Apache 2.0 license, packaging, CI, tests, and docs.

[Unreleased]: https://github.com/reesehammer/umbra-py/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/reesehammer/umbra-py/releases/tag/v0.1.0
