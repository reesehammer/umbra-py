# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **SAR acquisition-property search filters — polarization, incidence angle and
  resolution — across every discovery surface (`STRATEGY.md` §3 "discovery is
  the moat", `AI_INTEGRATION_IDEAS.md` §B2 STAC follow-on).** Search already
  filtered by geography (`bbox` / `intersects` / `place`), date and product type
  — but not by the SAR-native properties an analyst reaches for next, so those
  had to be filtered client-side after the fact (the "same 500 lines of glue"
  the strategy names). `search(...)` now accepts `polarizations` (keep items
  exposing at least one, e.g. `["VV"]` — the filter that keeps a change
  comparison like-with-like), `min_incidence` / `max_incidence` (view
  incidence-angle bounds in degrees) and `max_resolution` (keep items at least
  this fine, in metres). They are threaded through **every discovery surface so
  the backends agree**: the live open-bucket walk, the local `CatalogIndex`, the
  read-through `search_live`, the Canopy commercial archive (applied client-side
  like `product_types`), `umbra search` (`--pol` / `--min-incidence` /
  `--max-incidence` / `--max-resolution`), and the MCP `search_catalog` tool
  (so agents filter too). The metadata is already parsed on every `UmbraItem`
  (`sar:polarizations`, `view:incidence_angle`, `sar:resolution_*`), so no
  schema change is needed — the shared predicate `UmbraItem.matches_filters`
  runs in Python on each candidate, exactly as the polygon test does. Each
  filter is a **hard predicate**: a set filter excludes an item lacking that
  property (the STAC Query-extension convention), deliberately unlike the
  geometric filters' coarser-datum fallback. No model is called and no
  dependency is added; the whole surface is offline-tested
  (`tests/test_acquisition_filters.py` across the predicate, index, live walk,
  archive, CLI and MCP). Wiring these filters into the render/analysis commands
  (`change`, `timescan`, …), the `umbra serve` STAC Query extension, and
  `umbra ask` is ledgered as an additive follow-on in `TODO.md`.

### Fixed
- **`umbra index export` (stac-geoparquet) no longer crashes on catalog
  drift in the `providers` property (issue #102).** Most Umbra acquisitions
  encode the STAC `providers` property as a list of provider objects
  (spec-correct), but a handful carry a single bare object. stac-geoparquet
  infers one Arrow type per column, so a column that is a list on some rows
  and a scalar on others aborted the whole export with
  `ArrowInvalid: cannot mix list and non-list, non-null values`. This crashed
  the weekly `publish-index` workflow so the rolling `catalog-index` release
  was never produced, which in turn made the live catalog canary fail with a
  404 fetching the missing `catalog.db`. `export_geoparquet` now normalizes
  any property that drifts between list and scalar across the exported items,
  wrapping the scalar occurrences in single-element lists — lossless, and for
  `providers` the spec-correct shape (`item.raw` is never mutated). The live
  canary also now skips, rather than errors, when the `catalog-index` release
  asset isn't published yet, since that availability gap is not the catalog
  drift the canary exists to catch. Covered by `tests/test_export.py`.
- **CC-BY data attribution now shown on the interactive maps
  (`DEMO_APP_GAPS.md` G8).** Umbra open data is CC-BY-4.0, which requires the
  data credit be displayed wherever the data is used. The Folium maps
  (`umbra map`, `umbra map --timeline`, `umbra swipe`) surfaced the notice only
  inside per-marker popups, while the default basemap credited only the
  OpenStreetMap *tiles* — the Umbra footprints and SAR overlays drawn on top
  (the licensed data) had no visible attribution. A shared `viz._add_attribution`
  helper now registers `constants.ATTRIBUTION` with Leaflet's attribution control
  on every generated map, so the credit sits beside the OSM notice — the standard
  place a web map shows its data sources, matching what `umbra demo`,
  `umbra gallery`, and `umbra tiles` already do. Emitted as a Folium
  `MacroElement` (the same runtime-script mechanism as the swipe shim), so the
  notice is baked into the saved HTML and is offline-tested in
  `tests/test_viz.py`. No new dependency, no behaviour change beyond the added
  credit line.

### Security
- **Defused XML parsing of the S3 bucket listing + a scheduled `pip-audit`
  dependency audit (`CODEBASE_ANALYSIS.md` §6 P2 #13 / P2 #14, §5.2.5).** The
  catalog's core discovery path parses S3 `ListObjectsV2` responses — remote,
  untrusted XML (the listing base is configurable) — and did so with the stdlib
  `xml.etree`, which is exposed to the entity-expansion ("billion laughs") and
  external-entity (XXE) attack classes. `UmbraCatalog._parse_listing` now routes
  both listing parse sites through **`defusedxml`** (`forbid_dtd=True`), so a DTD,
  internal entity expansion, or external reference is rejected outright and turned
  into a clean `CatalogError` instead of memory exhaustion or a filesystem read.
  `defusedxml` (pure-Python, zero transitive deps) is added to the core
  dependencies; offline-tested with billion-laughs / XXE / malformed payloads and
  an end-to-end hostile listing response. Separately, a new
  `.github/workflows/security-audit.yml` runs `pip-audit --strict` against the
  full resolved dependency tree weekly (and on demand), opening a tracking issue
  on a finding — the same non-blocking canary pattern as the live-catalog run,
  chosen over a hard PR gate because advisories land continuously on transitive
  deps the project doesn't control. Closes the two remaining security-hygiene
  items the codebase analysis named as open.
- **Subresource Integrity on the browser-side `geotiff.js` loader
  (`CODEBASE_ANALYSIS.md` §3.4 / P2 #12).** The lazy-imagery driver
  (`umbra map --lazy-imagery`, `umbra demo`) fetches `geotiff.js` from a pinned
  CDN URL on first click; it now injects that `<script>` with a pinned SHA-384
  `integrity` digest (`_lazy_imagery.GEOTIFF_SRI`) and `crossorigin="anonymous"`,
  so the browser verifies the fetched bytes before executing them. A compromised
  CDN or hijacked package release can no longer run arbitrary script in every map
  a user has generated — a digest mismatch falls through the existing `onerror`
  path to a clean "Fetch failed" instead of running unverified code. The digest
  is reproducible from the npm registry tarball (unpkg serves it verbatim), and
  the recompute recipe is documented inline so it survives version bumps without
  reaching the egress-restricted CDN host. Offline-tested in
  `tests/test_lazy_imagery.py` (digest shape; the injected `<script>` carries the
  digest and a CORS fetch). No new dependency, no behavior change on the happy
  path. This closes the last open security-review item for code the project
  controls; Folium's own vendored CDN assets remain out of scope.

### Changed
- **A `mypy` type-check gate now verifies the `py.typed` promise
  (`CODEBASE_ANALYSIS.md` P2 #11).** The package ships a `py.typed` marker, so
  downstream type checkers trust its inline annotations — but nothing in CI
  verified those annotations were actually consistent, so the library shipped an
  *unchecked* promise. A new `type-check` job in `.github/workflows/ci.yml` runs
  `mypy` on every PR, backed by a `[tool.mypy]` config (`warn_unused_ignores` +
  `warn_redundant_casts` on, so stale ignores/casts can't accumulate). It runs
  against a core `[dev]` install: the optional, un-stubbed third-party libraries
  behind the extras (`rasterio`, `fastapi`, `sarpy`, `folium`, `PIL`, `click`,
  `mcp`, …) are import-ignored, so the gate checks *umbra-py's own* types rather
  than flapping on dependencies it doesn't control. `mypy`, `types-requests` and
  `types-defusedxml` are added to the `dev` extra. Landing the gate surfaced and
  fixed **18 genuine type issues** across 7 modules, several of them latent bugs:
  a `date > None` comparison in `CatalogIndex.search_live`'s freshness-horizon
  logic (`index.py`), a `datetime.isoformat()` on a possibly-`None` value in the
  timeline map builder and a `None`-unsafe sort key in the change-frame selector
  (`viz.py`), a `.submit()` on a possibly-`None` executor in the async artifact
  path (`serve.py`), a possibly-`None` href handed to `_has` during an
  incremental index update (`index.py`), and loosely-`object`-typed search
  backends in the CLI and MCP server (now the precise `UmbraCatalog | CatalogIndex`
  union, with `close()` guarded by `isinstance` narrowing). All fixes are
  behavior-preserving; the full offline suite is unchanged and green.
- **`umbra gallery --local` renders from baked thumbnails (`DEMO_APP_GAPS.md`
  G6).** The thumbnail bake shipped the primitive (`umbra index bake-thumbnails`)
  and the `umbra serve` / `umbra demo` consumers, but the *gallery* contact sheet
  still re-streamed every tile's cloud-optimized overview from S3 at render time.
  Now a `--local` / `--index-db` gallery embeds any thumbnail already baked into
  the index straight from local bytes — instant, offline, and (when every tile is
  baked) with **no `rasterio`**, so a core install over a fetched/baked
  `catalog.db` renders the visual browse in milliseconds. Only tiles missing from
  the bake are streamed the usual way, so a partially-baked index degrades
  gracefully, and a plain live `umbra gallery` is unchanged. `viz.gallery` gained
  an optional `baked` (`{id: PNG bytes}`) argument fed by
  `CatalogIndex.get_thumbnail`; the `rasterio` requirement is now raised only when
  a stream is actually needed. Deterministic, no model call, no new dependency;
  offline-tested in `tests/test_viz.py` (baked-only needs no viz extra, baked +
  streamed mix) and `tests/test_index.py` (`umbra gallery --local` over a
  bake-thumbnailed index streams nothing).
- **Per-pixel facet-area (gamma-nought) RTC model — `umbra convert --rtc
  --rtc-model gamma` / `sicd_to_geocoded_cog(rtc_model="gamma")` (`STRATEGY.md`
  5.5).** A third radiometric-terrain-flattening model alongside the default
  `cosine` and the range-plane `area`. It scales power by
  `cos(reference) * nz / cos(local_incidence)` — normalising by the local
  illuminated *facet* area projected into the plane perpendicular to the look
  direction (the gamma-nought convention). It uses the full 3-D facet normal (like
  `cosine`, unlike the range-plane `area`) *and* adds the true tilted-facet-area
  term `nz = cos(slope)` that both other models omit: a facet whose ground-projected
  area is one pixel has true area `1/nz`, so the illuminated area per pixel scales
  as `cos(local_incidence)/nz`. On flat terrain `nz == 1` and the local incidence
  equals the scene incidence, so with the default reference flat ground is left
  unchanged and only slopes are flattened. Like the other two it is an honest
  first slice — a normalisation of *detected amplitude*, not a calibrated product,
  and *not* the full image-space illuminated-area facet integration (Small 2011,
  with layover accumulation) or MultiRTC interop, which remain deferred. New value
  in the public `RTC_MODELS` constant; `rtc_model` still defaults to `"cosine"`,
  so existing calls are unchanged. The physics is a pure-numpy core
  (`_facet_area_factor`) offline-tested against closed-form planar-slope behaviour
  (flat → unchanged, the exact `nz`-scaling relative to the cosine factor, DEM-gap
  safety, and the shadow/clamp floor), with only the DEM-on-grid resample touching
  rasterio.
- **Instant SAR thumbnail preview in `umbra demo` (`DEMO_APP_GAPS.md` G6).** The
  baked-thumbnail bake shipped the primitive (`umbra index bake-thumbnails`) and
  the server endpoint (`GET /artifacts/thumbnail/{id}.png`) but left the flagship
  self-serve explorer unwired. Now, with `umbra demo --server-url` pointing at a
  running `umbra serve`, clicking a scene *leads* its detail panel with a small
  SAR picture pulled from that endpoint — the quicklook thumbnail served straight
  from the index as an offline local-bytes read (falling back to a live quicklook
  render for a scene not yet baked), so the funnel's front door opens with a
  radar image, not metadata alone. The heavier on-click "Get SAR image" COG
  overlay stays the deeper look. A scene with no baked thumbnail 404s and the
  `<img>` is dropped via `onerror` (never a broken image); the remote item id is
  url-encoded into the path (the base is the trusted server URL); and the preview
  reuses the single `serverBase` the "Analyze this view" panel already computes.
  Without `--server-url` the detail panel is unchanged and the page stays a fully
  static single file. Offline-tested in `tests/test_demo.py` (the generator is
  stdlib-only — no `viz` extra, no network).
- **Rendered documentation site — mkdocs-material + mkdocstrings + mkdocs-click
  (`CODEBASE_ANALYSIS.md` §5.2 #6 / P3 #20).** The project graduates from a
  README doing a docs site's job to being the front door of a real one — the
  highest-leverage remaining code investment for discoverability, and the anchor
  the `llms.txt` idea pointed at. `mkdocs.yml` + `docs_src/` author the site
  (`docs_dir` is `docs_src/`, so the internal strategy/analysis Markdown under
  `docs/` stays unpublished). The **API reference** is generated by mkdocstrings
  from the docstrings the package already ships; the **CLI reference** is
  generated by `mkdocs-click` straight from the Click group, so neither can drift
  from the code or from `umbra --help`. `.github/workflows/docs.yml` builds the
  site `--strict` on every PR (a broken cross-reference fails review) and deploys
  to GitHub Pages from `main`; the deploy waits only on a maintainer enabling
  Pages. New `[docs]` extra (`mkdocs-material`, `mkdocstrings[python]`,
  `mkdocs-click`); README gains a docs badge + link.
- **Native LlamaIndex tool adapter — `umbra_py.llamaindex`
  (`AI_INTEGRATION_IDEAS.md` B1 / C1).** Completes the agent-framework reach
  trilogy — MCP → LangChain → LlamaIndex — the "same shapes, a third
  registration" step named in `TODO.md`. `umbra_tools()` returns the catalog as
  native LlamaIndex `FunctionTool`s ready for `ReActAgent.from_tools(...)` or a
  tool-calling agent. There is **no new business logic**: the nine JSON tools
  (`search_catalog`, `get_item`, `geocode_place`, `index_stats`, `download_asset`,
  `watch_site`, `find_similar` / `find_similar_text`, `describe_scene`) reuse the
  MCP server's deterministic callables verbatim, so all three front doors cannot
  drift; each tool's name/description is inferred from the function docstring and
  its argument schema from the signature. *Images are the API*: LlamaIndex has no
  `content_and_artifact` split, so the `quicklook` / `change_composite` /
  `timescan` render tools — re-implemented natively so the surface never pulls in
  the MCP SDK — return a `RenderResult` whose string form is the caption and whose
  `.png` (the `ToolOutput.raw_output`) carries the raw PNG for a downstream
  multimodal model to *see* the radar scene; pass `include_render=False` for a
  JSON-only surface. The determinism boundary is preserved — `describe_scene`
  stays the one opt-in model call. New `[llamaindex]` extra (`llama-index-core` —
  the lightweight tool package, not the full framework — plus `viz`), wired into
  the all-extras CI job, and fully offline-tested in `tests/test_llamaindex.py`
  (surface, schema inference, invocation, PNG `RenderResult`, guards) with no key
  and no network.
- **Native LangChain / LangGraph tool adapter — `umbra_py.langchain`
  (`AI_INTEGRATION_IDEAS.md` B1 / C1).** *Agents are the new first-time users*:
  the MCP server puts the 17+ TB SAR archive in front of MCP-native clients, and
  this adds the **same** tool surface to the other large population of agent
  builders — anyone assembling an agent with LangChain / LangGraph.
  `umbra_tools()` returns the catalog as native LangChain `StructuredTool`s ready
  for `model.bind_tools(...)` or LangGraph's `create_react_agent`. There is **no
  new business logic**: the nine JSON tools (`search_catalog`, `get_item`,
  `geocode_place`, `index_stats`, `download_asset`, `watch_site`, `find_similar` /
  `find_similar_text`, `describe_scene`) reuse the MCP server's deterministic
  callables verbatim, so the two front doors cannot drift; each tool's schema is
  inferred from the function signature and its description from the docstring.
  *Images are the API*: the `quicklook` / `change_composite` / `timescan` render
  tools are re-implemented natively — so the LangChain surface never pulls in the
  MCP SDK — and return the PNG via LangChain's `content_and_artifact` response
  format (a text caption on the `ToolMessage` content, the raw PNG on
  `.artifact`), so a downstream multimodal model still *sees* the radar scene;
  pass `include_render=False` for a JSON-only surface. The determinism boundary is
  preserved — `describe_scene` stays the one opt-in model call. New `[langchain]`
  extra (`langchain-core` — the lightweight tool package, not the full framework —
  plus `viz`), wired into the all-extras CI job, and fully offline-tested in
  `tests/test_langchain.py` (surface, schema inference, invocation, PNG artifact,
  guards) with no key and no network. (The parallel LlamaIndex `FunctionTool`
  wrapper has since shipped too — see the `umbra_py.llamaindex` entry above.)
- **Fetchable prebuilt scene-embedding table — `umbra embed fetch`
  (`STRATEGY.md` 5.2 / `AI_INTEGRATION_IDEAS.md` C5).** Building the visual
  similarity index (`umbra embed`, C5) embeds every quicklook in the archive — the
  one expensive, model-backed step — so a fresh install got the searchable index
  (`umbra index fetch`) and the whole-catalog basemap (`umbra tiles --fetch`) for
  free but had to render and embed thousands of scenes itself before `umbra embed
  similar` returned anything. This closes that: `umbra embed fetch` /
  `fetch_prebuilt_embeddings()` / `SceneEmbeddingIndex.from_release()` pull a
  published `catalog.embed.db` from the rolling `catalog-index` GitHub release
  straight to the sibling of the catalog index, so visual similarity search works
  with **no rebuild** — only the *query* still needs an embedding key (the archive
  vectors arrive pre-built) — the embedding sibling of `umbra index fetch` /
  `umbra tiles --fetch`. New constants `CATALOG_EMBED_ASSET` /
  `CATALOG_INDEX_EMBED_URL`; the fetch path calls **no model** and adds **no
  dependency** (it reuses the resume-safe `download_url`), and is fully
  offline-tested in `tests/test_embed.py` (mocked release download + round-tripped
  DB, model label preserved, overwrite, and the CLI). Because the vectors are
  model-derived and model-*specific* — unlike the deterministic `catalog.db` /
  `catalog.pmtiles` — the *publish* is opt-in: `.github/workflows/publish-index.yml`
  gained a gated, `continue-on-error` step that builds and uploads
  `catalog.embed.db` (recording the embedding model prominently in the release
  notes) only when a maintainer has set an `OPENAI_API_KEY` secret, so it never
  affects the deterministic index publish and costs nothing until configured. This
  is exactly the static, host-anywhere artifact `STRATEGY.md` 5.2 wants to offer
  upstream — publish it beside `catalog.json` and the ecosystem gets scene
  similarity search over Umbra data for free.
- **Download content-integrity verification against the S3 ETag MD5
  (`docs/CODEBASE_ANALYSIS.md` §3.2 / P1 #5).** `download_url` already verified
  the received byte count against `Content-Length` and used `If-Range` + a stored
  ETag so a resume can't splice two different objects; this closes the remaining
  §3.2 item — *content* verification. When the server exposes a single-part S3
  `ETag` (the object's hex MD5) and `verify=True` (the new default), the finished
  file is streamed through MD5 and compared, so on-the-wire corruption a correct
  byte count can't catch fails loudly with a `Checksum mismatch` `DownloadError`.
  A mismatch means the complete-length bytes are wrong — a resume can't repair
  them — so the `.part` and its `.etag` validator are discarded and a retry
  re-downloads cleanly rather than "resuming" a full-but-corrupt file. Multipart
  ETags (`"<hash>-<n>"`) are not a plain MD5 of the bytes and are skipped rather
  than raising a spurious mismatch; `verify=False` opts out for callers that don't
  want the extra read of a multi-GB file (it threads through `download_asset` /
  `download_item`). New `verify` keyword on `download_url`; stdlib `hashlib` only,
  **no new dependency and no model call**, fully offline-tested in
  `tests/test_download.py` (matching MD5 passes, corrupt-body mismatch discards
  the `.part`, multipart-ETag skip, `verify=False` opt-out, and a resumed append
  verifying the *whole* object's MD5). This is the reliability floor under the
  library's core job — fetching multi-GB SAR products — and closes the last
  open item under `TODO.md`'s download-hardening ledger.
- **Projected-area (foreshortening) RTC model — `umbra convert --rtc
  --rtc-model area` (`STRATEGY.md` 5.5).** Radiometric terrain flattening (`--rtc`)
  shipped as the geometric cosine correction `cos(reference)/cos(local_incidence)`,
  which uses the full 3-D local incidence angle and so folds azimuth-direction tilt
  into the correction. This adds a second, selectable model,
  `sicd_to_geocoded_cog(rtc_model="area")` / `--rtc-model area`, that scales power
  by `sin(local_range_incidence)/sin(reference)`: it measures incidence in the
  *range–vertical* plane, so it targets the range-direction foreshortening and
  layover that dominate radiometric terrain distortion — separating them from the
  azimuth tilt that does not foreshorten. On flat terrain both reduce to the scene
  incidence angle (default reference), so flat ground is left unchanged and only
  slopes are corrected; DEM gaps and layover degrade gracefully (factor forced to
  one over gaps, floored/clamped in layover). It is an honest first-order step
  toward area-based gamma-nought normalisation, **not** the full illuminated-area
  facet integration (Small 2011) or MultiRTC interop, which remain deferred. New
  public constant `RTC_MODELS` and the `rtc_model=` keyword (default `"cosine"`,
  so existing calls are unchanged); the physics is a pure-numpy core
  (`_range_local_incidence`, `_foreshortening_factor`) with closed-form
  planar-slope behaviour, **no model call and no new dependency**, offline-tested
  in `tests/test_convert.py` (flat/range-ramp/azimuth-slope geometry, the
  cosine-vs-area distinction, layover/gap handling, the end-to-end and CLI paths).
  This advances the last remaining code item on `STRATEGY.md` 5.5's radiometric-RTC
  line.
- **stac-geoparquet chip manifest — `umbra chips --manifest chips.parquet`
  (`AI_INTEGRATION_IDEAS.md` C4 / `STRATEGY.md` 5.5).** `umbra chips` wrote its
  training-tile manifest as `.jsonl` (one record per line) or `.geojson` (a
  `FeatureCollection`), both stdlib-only — fine for a small run, but a large chip
  set forces a consumer to read every line. This adds a third format: a `.parquet`
  manifest written as [stac-geoparquet](https://stac-geoparquet.org/), so a chip
  dataset is one column-oriented file DuckDB, geopandas or pyarrow can query
  without loading it whole — exactly what the SAR foundation-model / change-detection
  audience (`STRATEGY.md` 5.5's "audience most likely to contribute back") reaches
  for at scale. Each chip becomes one STAC Item row (its footprint geometry, the
  acquisition datetime, and the same fields as the `.jsonl` record as properties,
  with the chip file as the item's `data` asset), reusing the same
  `stac_geoparquet.arrow` writer as `umbra_py.export`. Format is still chosen by
  the manifest filename's extension, so the CLI is unchanged beyond accepting
  `.parquet`. It stays in the project's determinism boundary (**no model call** —
  pure manifest logic) and needs the `[export]` extra alongside `[load]`; new
  public API `write_manifest_parquet`, fully offline-tested in
  `tests/test_chips.py` (round-tripped through pyarrow, including the null-datetime
  case). This closes the "publish the chip manifest as stac-geoparquet" follow-on
  in `TODO.md`.
- **SICD-convert showcase notebook — `examples/07_sicd_amplitude.ipynb`
  (`STRATEGY.md` 5.4 / 5.5).** Completes the example gallery with a runnable front
  door for the flagship SICD → geocoded COG capability. Every other notebook uses
  the already-geocoded `GEC` asset; the complex `SICD` lives in the radar slant
  plane and won't open on a map without the sensor-model geocoding `umbra convert`
  provides — extensive code that had no tutorial. The notebook takes one open-data
  SICD, detects its amplitude in the slant plane (asserting the CRS is `None`),
  geocodes it onto a north-up EPSG:4326 COG with `sicd_to_geocoded_cog`, and
  asserts the result is EPSG:4326, carries COG overviews, and lands on the
  acquisition's catalog footprint. Like the rest of the gallery it is
  self-checking (a small deterministic search with `assert`s in every code cell,
  **no model call**) and guarded offline by `tests/test_examples.py`; it executes
  end-to-end under `pytest -m network` using a curated small scene (`Centerfield,
  Utah`, ~370 MB, converts in under a minute), and the live-execution guard now
  also `importorskip`s `sarpy` (the `convert` extra). Terrain orthorectification
  (`--dem auto`), the geoid correction, and `--rtc` are named in prose as the next
  step. This finishes workstream 5.4.
- **Standing-analyst monitoring notebook — `examples/06_site_monitoring.ipynb`
  (`AI_INTEGRATION_IDEAS.md` C3 / `STRATEGY.md` 5.4).** SAR's killer application is
  monitoring — the same site re-imaged pass after pass — and the primitives for it
  (`umbra watch`, `umbra change`, the `watch_site` MCP tool) had all shipped
  without one runnable example wiring them into the standing-analyst loop. This
  adds it: the notebook stands up a `watch()` over a repeat-imaged site, asserts
  the first run reports every pass as new *and* an immediate re-run reports **zero**
  (the idempotency a scheduler depends on), then hands the new passes to
  `select_change_frames` → `save_change_composite` for the "new pass lands →
  composite → notify" action, naming `umbra change --narrate`, `MetaWatchStore`
  persistence and the `watch_site` MCP tool as the next steps. Like the rest of the
  gallery it is self-checking (a small deterministic search with `assert`s in every
  code cell, **no model call**) and guarded offline by `tests/test_examples.py`,
  and it executes end-to-end under `pytest -m network` (`viz` extra for the
  composite). The still-planned SICD-convert showcase notebook is renumbered `06`
  → `07`.
- **Baked SAR quicklook thumbnails in the catalog index — `umbra index
  bake-thumbnails` / `CatalogIndex.bake_thumbnails()` /
  `CatalogIndex.get_thumbnail()` (`docs/DEMO_APP_GAPS.md` G6).** Closes the last
  open piece of the "No thumbnail/artifact caching layer" gap. Every gallery,
  `umbra demo` preview and `umbra serve` quicklook otherwise re-streams a scene's
  cloud-optimized GeoTIFF overview from S3 at *render* time, so the first view of
  a whole catalog is network-bound and slow. `umbra index bake-thumbnails` renders
  a small (`--size`, default 256 px) PNG preview per acquisition once at build
  time and caches the bytes in a new additive `thumbnail` column, so a later
  `GET /artifacts/thumbnail/{item_id}.png` on `umbra serve` — a new endpoint that
  wraps `get_thumbnail()` — is an instant, offline file read instead of an S3 COG
  stream (a `404` falls back to `/artifacts/quicklook`). The render-side sibling
  of `umbra index bake`, it shares that command's discipline: **idempotent** (only
  acquisitions without a baked thumbnail are rendered, so a re-run bakes just what
  was added since), `--limit` for bounded batches, and a scene that can't be
  rendered is skipped and retried next run rather than aborting the batch. The
  schema migrates additively in place (`user_version` 2 → 3 — the second exercise
  of the migration path versioning was landed to enable), so an existing or fetched
  `catalog.db` gains the column on the next open. The renderer is **injectable**
  (default `viz._thumbnail_png`, needing the `viz` extra), so the whole path — bake,
  point-lookup, the server endpoint, coverage in `umbra index info` /
  `docs/schemas/index-info.schema.json` — is offline-tested with a stand-in
  renderer, no network and no `viz` extra. No model is called; the baked bytes never
  ride on `search`/`get` (which would bloat every `UmbraItem` with a PNG).
- **Baked place labels now flow through every read surface (`docs/DEMO_APP_GAPS.md`
  G2 follow-on).** `umbra index bake` writes a reverse-geocoded label onto
  `UmbraItem.place`, but until now only `umbra demo` consumed it — every other
  surface still fell back to the task codename or re-geocoded at render time
  (behind Nominatim's 1 req/s cap). This wires the baked label through the rest:
  `UmbraItem.to_llm_context()` (the A3 agent context card) prefers `.place` over
  the task codename; `footprint_map` / `timeline_map` (`umbra map`, `--timeline`)
  use `.place` directly and skip the live geocode entirely — so a fully-baked
  `--local` render with `--geocode` never touches the network, building the
  Nominatim session lazily only for items still lacking a label; `umbra serve`
  surfaces the label as a namespaced `umbra:place` STAC property so STAC clients
  show a real place name; and the stac-geoparquet export (`umbra index export`)
  carries `umbra:place` into the published snapshot, so a DuckDB / geopandas
  consumer reads the label without re-geocoding every row. In each case the
  baked label is preferred only when present and never overrides a value the
  source document already carries. Deterministic, no new dependency, no model
  call; offline-tested across models, viz, serve, and export.
- **Baked place labels in the catalog index — `umbra index bake` /
  `CatalogIndex.bake_places()` / `UmbraItem.place` (`docs/DEMO_APP_GAPS.md`
  G2).** Turning the shared index into a *labelled* demo backend, the
  denormalization G2 named as the change that does it. Reverse geocoding
  (coordinates → a human place name) used to run only at *render* time, where
  OpenStreetMap Nominatim's 1 req/s cap makes labelling thousands of
  acquisitions impractical — so `umbra demo` and the maps fell back to the Umbra
  task *codename*, not a geographic name. `umbra index bake` resolves each
  acquisition's footprint centroid to a place label ("Reykjavík, Iceland") once
  at build time and caches it in the index, so every `--local` `search`/`get`
  yields it on the new `UmbraItem.place` attribute for free and `umbra demo
  --local` shows real place names instantly, with zero per-render geocoding (the
  free-text site search matches on them too). The bake is **idempotent** (only
  unlabelled items are geocoded, so a re-run labels just what was added since,
  and `--limit` bakes a large catalog in bounded batches) and the geocoder is
  injectable, so the whole path is offline-tested with a stand-in — no network.
  This ships as the **first real schema migration** the index versioning was
  landed to enable: `place` is an additive nullable column, so a version-1 (or
  legacy version-0) `catalog.db` — including a fetched snapshot — is migrated in
  place on open (`user_version` 1 → 2, the column added, every row preserved)
  rather than rebuilt or rejected. Re-indexing an acquisition (`umbra index
  update`) now upserts via `ON CONFLICT` so it refreshes the STAC columns but
  **preserves** a baked label (the label is keyed on the footprint, not the
  document). `umbra index info` reports label coverage (`labeled` in the `--json`
  object, `docs/schemas/index-info.schema.json`; a "places: N of M labelled" line
  in the human summary). No new dependency and no model call.
- **Semantic "describe the site" search on the `umbra-mcp` MCP server —
  `search_catalog(area=…, semantic=True)` (`docs/AI_INTEGRATION_IDEAS.md` §C1
  follow-on).** The embedding-backed task-name aliasing shipped complete on the
  CLI (`umbra semantic search`), but the agent surface — the project's
  highest-leverage front door — only reached the deterministic `fuzzy=` token
  match, so a plain-language *site description* couldn't be aliased to a task
  name. The new `semantic=True` flag resolves `area` to the closest task names
  by meaning through the shipped `SemanticTaskIndex` (so `"grain storage north
  dakota"` reaches `"Beet Piler - ND"`, an alias sharing no word with the label
  that `fuzzy` cannot and should not fake), searches the best match over the
  chosen backend, and returns `resolved_area` plus the ranked `semantic_matches`
  so the resolution is auditable and retryable. A `min_score` cosine threshold
  drops weak aliases (a low-confidence description returns an empty audit trail
  rather than an arbitrary top pick), and a `search-by-description` prompt
  packages the workflow. `semantic` and `fuzzy` are mutually exclusive; the mode
  is gated (like the CLI) on a prebuilt semantic index and the `[ai]` embedding
  key, so it never runs implicitly. The only model call is turning the query
  into a vector (an injectable embedder); the whole path is offline-tested in
  `tests/test_mcp_server.py` with a deterministic concept embedder — no key, no
  network, no new dependency.
- **Polygon `intersects` spatial search — a true footprint filter, not just a
  bounding box (`docs/AI_INTEGRATION_IDEAS.md` §B2 STAC follow-on).** Discovery
  is the project's moat (`docs/STRATEGY.md` §3), and its only spatial filter was
  a rectangle: a coast, a border, or any drawn area of interest dragged in a lot
  of empty ocean and neighbouring land. `search(intersects=…)` now keeps only
  acquisitions whose footprint intersects a caller-supplied GeoJSON polygon —
  the standard STAC `intersects` every geo tool already speaks — threaded
  through every search surface so the two backends agree: the live
  `UmbraCatalog` walk, the SQLite `CatalogIndex` (its bounding box pushed into
  SQL as a cheap prefilter, the exact polygon test then run in Python),
  `CatalogIndex.search_live`, the Canopy commercial archive (the polygon POSTed
  as the STAC `intersects` and re-checked client-side), `umbra search
  --intersects <file.geojson | inline JSON>`, the `umbra serve` STAC API
  (`GET`/`POST /search`, mutually exclusive with `bbox` per the spec), and the
  `search_catalog` MCP tool. The geometry itself is a new dependency-free core
  (`umbra_py._geometry`): a stdlib GeoJSON polygon parser (`Polygon` /
  `MultiPolygon`, or a `Feature` / `FeatureCollection` wrapping one) and
  closed-form intersection primitives (bbox reject, segment-crossing, ray-cast
  point-in-polygon) over plain `(lon, lat)` tuples — no shapely, no compiled
  geometry stack in the base install. `UmbraItem.intersects_polygon` tests the
  item's *actual* footprint (a tighter filter than the bbox
  `intersects_bbox`), falling back to the bbox when a footprint is absent. Holes
  and antimeridian-spanning polygons are handled over-inclusively (they can only
  keep an item, never wrongly drop one — the safe direction for a discovery
  filter) and documented as such. No model is called; the whole path is
  offline-tested (`tests/test_geometry.py`) across the core, item, catalog,
  index, CLI, STAC API and MCP surfaces.
- **Canopy commercial-archive backend on the `umbra-mcp` MCP server — a token
  concept for the flagship AI surface (`docs/STRATEGY.md` 5.1 follow-on /
  `docs/AI_INTEGRATION_IDEAS.md` §B1).** The paid-archive funnel already ran end
  to end on the CLI, but the MCP server — the project's highest-leverage surface —
  only reached the free open bucket. `umbra_py.mcp_server` now reads
  `$UMBRA_CANOPY_TOKEN` once from the server's environment (`_canopy_token()` — a
  secret the operator configures in the MCP client's `env` block, never a tool
  argument the client's model handles or can leak): when set, `search_catalog` and
  `watch_site` query Umbra's authenticated commercial archive (`source:
  "canopy-archive"`) and `get_item` resolves a bare acquisition id through the
  shipped `UmbraCatalog.get_item` STAC `ids` lookup (a full `://` URL is still read
  directly as an open-data sidecar). So a paying Canopy customer discovers,
  monitors and retrieves the archive they pay for through the same conversation a
  newcomer learned on the free data — the funnel made literal on the surface that
  matters most. `_search_source(local, token)` rejects `local=True` with a token
  (the live archive has no local index), and the server's `instructions` announce
  archive mode when a token is configured. No model is called and no new dependency
  is added — pure backend-selection wiring; the token is only ever handed to the
  Canopy catalog (never surfaced in a result), and the whole path is offline-tested
  (`tests/test_mcp_server.py`) against a fake archive catalog with no credentials
  and no network.
- **`describe_scene` MCP tool + `describe-scene` prompt — a SAR-literate VLM
  reading of one scene over MCP (`docs/AI_INTEGRATION_IDEAS.md` §C2 follow-on).**
  The `umbra-mcp` server surfaces the shipped `umbra describe` C2 capability:
  `describe_scene(url, asset, db, max_size, model)` renders the acquisition's
  quicklook, sends it with the item's context card behind the packaged
  SAR-literacy prompt, and returns a validated `{summary, observed_features,
  confidence, caveats}` reading — so an MCP client can get "what am I looking at?"
  answered inside the same conversation that searched and viewed the scene. It is
  the **one tool on the server that consults a model**, a deliberate, opt-in
  exception to the otherwise-deterministic tool surface: gated (like the CLI) on
  the `[ai]` key so it never runs implicitly, with the boundary intact — the
  picture and metadata are produced deterministically, the model only interprets
  (its reply passes the `parse_description` boundary and never becomes a
  coordinate, URL, or filter), and every reading carries the CC-BY attribution
  plus the `AI_PROVENANCE` note. The describer and render are injectable, so the
  whole tool is offline-tested (`tests/test_mcp_server.py`) with no `[ai]`/`[viz]`
  extra, no key, and no network — including the missing-key setup error. The
  server module's "nothing here calls a model" invariant was revised to name this
  single, honest exception. No new dependency.
- **Adoption / community scaffolding — `CITATION.cff`, `SECURITY.md`,
  `CODE_OF_CONDUCT.md` (`docs/STRATEGY.md` 5.3 / `docs/CODEBASE_ANALYSIS.md` P2
  #14, P3 #22).** The library is feature-complete; the binding constraint on the
  strategy's "widen the funnel" thesis is now discoverability and citability, not
  capability. This lands the code-side pieces of workstream 5.3 ("make adoption
  visible where Umbra looks"): a machine-readable `CITATION.cff` (Citation File
  Format 1.2.0) so GitHub renders a "Cite this repository" button and Zenodo /
  citation managers can read the metadata — academic citations are the currency
  an open-data program exists to generate; a `SECURITY.md` disclosure policy
  (private GitHub advisory reporting, plus the honest security posture: anonymous
  HTTPS, no auth surface, remote-content/generated-HTML as the trust boundary);
  and a Contributor Covenant 2.1 `CODE_OF_CONDUCT.md`. Together they complete
  GitHub's Community Standards profile. `CITATION.cff`'s `version` is kept in sync
  with `umbra_py.__version__` by an offline, stdlib-only guard
  (`tests/test_citation.py`), mirroring the golden-file discipline the
  `llms.txt` bundle already uses. README gains "Citing umbra-py" and "Community"
  sections linking all three. No code surface changes and no new dependency.
- **Keyed single-item lookup against the Canopy commercial archive —
  `UmbraCatalog.get_item(item_id)` / `umbra info <id> --token` (`docs/STRATEGY.md`
  5.1 follow-on).** `search` covers *listing* the paid archive; this adds the
  *retrieval* complement — a keyed fetch of one acquisition by STAC id. It is
  implemented with the STAC API `ids` search extension over the same
  `/archive/search` endpoint the search path already POSTs to (`POST {"ids":
  [item_id], "limit": 1}`), so it introduces no new endpoint and stays
  offline-testable against a mocked API, and it inherits `_archive_page`'s bearer
  auth plus the helpful 401/403 "token rejected" and 500 wrapping. It requires a
  Canopy token (the open bucket is a static catalog with no id→item index — resolve
  an open-data item from its sidecar URL or from a built index with
  `CatalogIndex.get`), and guards against a server that ignores the `ids` filter by
  accepting only the exact id requested. On the CLI, `umbra info` gains `--token`
  (with the `$UMBRA_CANOPY_TOKEN` fallback): with a token the argument is an
  archive item id resolved via the keyed lookup, without one it stays the
  open-data sidecar-URL read it has always been — the retrieval sibling of `umbra
  search --token`, so the commercial archive now has a keyed lookup matching the
  local index's `CatalogIndex.get`. No model call and no new dependency; the whole
  path is offline-tested (`tests/test_canopy.py`, `tests/test_cli_token.py`).
- **Detection-chips example notebook — `examples/05_detection_chips.ipynb`
  (`docs/STRATEGY.md` 5.4 / `docs/AI_INTEGRATION_IDEAS.md` B3).** The ML-dataset
  half of the notebook gallery, and the workflow the model-training audience (SAR
  foundation models, change detection — the audience most likely to contribute
  back) reaches for first. It cuts one scene into fixed-size, georeferenced
  training chips with `umbra chips` — walked a window at a time straight out of
  the geocoded COG over `/vsicurl` range reads, so there is no full download and
  memory stays bounded to one tile — and reads back the manifest that makes each
  chip trainable: its geographic bbox, CRS and affine transform, and the
  acquisition's look-angle, resolution, polarization and CC-BY license. Like the
  other notebooks it is self-checking (a deterministic one-day search with
  `assert`s in every code cell) and guarded offline by `tests/test_examples.py`
  (well-formed, cells parse, only public `umbra_py` symbols, CC-BY present),
  executable end-to-end under `pytest -m network`. No new code surface and no
  model call.
- **Amplitude time-series example notebook —
  `examples/04_amplitude_time_series.ipynb` (`docs/STRATEGY.md` 5.4 /
  `docs/AI_INTEGRATION_IDEAS.md` B3).** With every capability built, the binding
  constraint on adoption is the notebook gallery — the greatest-hits SAR
  workflows, runnable, that double as live evals. The three shipped notebooks
  cover search→quicklook, streaming a GEC into `xarray`, and a two-pass change
  composite; this adds the *monitoring* greatest-hit. It reduces a site's repeat
  passes to one scalar each (mean backscatter in dB, from `to_xarray(..., db=True)`
  over streamed decimated overviews — no full download) and plots the trend — the
  whole-scene scalar complement to `umbra timescan` (which keeps the map) and
  `umbra change` (which compares two passes in color). Like the others it is
  self-checking (a small deterministic search with `assert`s in every code cell)
  and guarded offline by `tests/test_examples.py`, executable end-to-end under
  `pytest -m network`. No new code surface and no model call.
- **Radiometric terrain flattening — `umbra convert --rtc` /
  `sicd_to_geocoded_cog(rtc=True)` (`docs/STRATEGY.md` 5.5).** Terrain
  orthorectification (`--dem`) fixes *where* each pixel lands but not *how bright*
  it is, and radar backscatter is strongly modulated by the local incidence angle
  — so on relief a slope tilted toward the radar looks bright and one tilted away
  looks dark from geometry alone. `--rtc` (which requires `--dem`) removes that
  geometric modulation: after geocoding, each pixel is scaled in the power domain
  by the cosine correction `cos(reference) / cos(local_incidence)`, where the
  local incidence angle comes from the DEM's local slope (its surface normal) and
  the scene look geometry (`SCPCOA.IncidenceAng` / `AzimAng`). The reference
  defaults to the scene incidence angle, so flat terrain is left unchanged and
  only slopes are flattened (`--rtc-ref-angle` overrides it). This is an honest
  first slice: a geometric normalisation of *detected amplitude*, not a calibrated
  gamma-nought RTC product (Umbra's open products are not radiometrically
  calibrated), documented as exactly that. It holds the module's grain — the
  physics is a pure-numpy core (terrain normals, look vector, correction factor)
  with closed-form behaviour over a planar slope, so it is fully offline-tested
  with hand-built arrays; only resampling the DEM onto the output grid touches
  rasterio, and DEM gaps / radar-shadow slopes degrade gracefully (factor clamped,
  gaps pass through unchanged). No new dependency and no model call. This closes
  the geometric half of 5.5's remaining `MultiRTC`/RTC gap; full gamma-nought area
  normalisation and MultiRTC interop remain open follow-ons.
- **The Canopy commercial-archive `--token` now works on the render/analysis
  verbs, completing the funnel to full parity (`docs/STRATEGY.md` 5.1).**
  `umbra search --token …` (or `$UMBRA_CANOPY_TOKEN`) has long pointed the same
  `search()` interface at Umbra's authenticated Canopy archive instead of the
  open bucket, but every other verb routed through `_gather_items`, which dropped
  the token — so a paying customer could *search* the paid archive on the CLI but
  not *render or analyse* it. `map`, `gallery`, `change`, `timescan`, `swipe` and
  `chips` now take the same `--token` (with the `$UMBRA_CANOPY_TOKEN` fallback and
  a guard against combining it with a local index), threaded through
  `_gather_items` → `_search_source(local, db_path, token)` to the commercial
  backend. This is the funnel made literal: the tool learned on the free data
  *is* the tool used on the paid archive, with the identical flags. No new
  dependency and no model call — the token is only ever sent to the Canopy
  endpoint, and the whole path is offline-tested against a `responses`-mocked STAC
  API (no credentials, no network), covering the dispatch, the token→archive flow,
  the per-command wiring, the `$UMBRA_CANOPY_TOKEN` fallback and the
  mutual-exclusion guard.
- **Auto-fetch a global geoid grid for vertical-datum correction —
  `umbra convert --geoid auto` / `umbra_py.geoid` (`docs/STRATEGY.md` 5.5).**
  Vertical-datum correction shipped as `--geoid PATH`, but that still made the
  user find, download, and point at the right EGM undulation grid — the same
  "same 500 lines of glue" `--dem auto` removed for DEMs, still present for the
  geoid. `--geoid auto` / `sicd_to_geocoded_cog(geoid="auto")` closes it, the
  vertical sibling of `--dem auto`: the new `umbra_py.geoid` module fetches a
  global geoid-undulation grid (the compact ~4 MB EGM96 15′ model PROJ
  distributes on [`cdn.proj.org`](https://cdn.proj.org/), `us_nga_egm96_15.tif`)
  once, caches it under the same XDG cache dir the index and DEM tiles use, and
  hands it into the shipped `--geoid PATH` correction unchanged — so
  `--dem auto --geoid auto` gives a terrain-corrected *and* vertically-referenced
  scene over relief with zero data hunting. Unlike a DEM the EGM grid is a single
  global file (nothing to tile — one file covers every scene); the fetch reuses
  the resume-safe `download_url` and is injectable (`fetch_geoid_grid`,
  `geoid_grid_url`, `default_geoid_cache_dir`), so the whole download-and-cache
  path is offline-tested with a stub downloader, with no new dependency and no
  packaged EGM data. `us_nga_egm08_25.tif` (EGM2008 2.5′) is a higher-resolution
  alternative on the same CDN, selectable via `fetch_geoid_grid(name=…)`.
- **Vertical-datum / geoid correction for terrain orthorectification —
  `umbra convert --geoid PATH` / `sicd_to_geocoded_cog(geoid=…)`
  (`docs/STRATEGY.md` 5.5).** Terrain orthorectification walks each control point
  onto the DEM surface, but global DEMs (Copernicus GLO-30, SRTM) quote height
  above the **EGM geoid** while SICD projects against the **ellipsoid**; feeding
  the orthometric height in as-is mislocated relief by roughly `N·tan(look_angle)`
  (the geoid undulation `N` reaches ~±100 m worldwide). `--geoid` takes any
  rasterio-readable undulation grid (e.g. an EGM96/EGM2008 GeoTIFF) and adds `N`
  to each sampled DEM height (`hae = orthometric + N`) before projecting, for
  survey-grade geolocation over relief. The correction is a pure composition of
  two injectable `(lons, lats) -> heights` samplers (`_geoid_corrected_sampler`) —
  the geoid grid is read with the same `_dem_height_sampler` the DEM uses — so the
  whole path is offline-tested with a hand-written grid, with no new dependency
  and no packaged EGM data. It requires `--dem` (it corrects DEM heights, a hard
  error without one), degrades gracefully to the uncorrected height off the grid,
  and without it the output is unchanged (correct to the local geoid–ellipsoid
  separation, ample for map placement).
- **Auto-fetch the covering Copernicus DEM for terrain orthorectification —
  `umbra convert --dem auto` / `umbra_py.dem` (`docs/STRATEGY.md` 5.5).** DEM
  terrain orthorectification shipped as `--dem PATH`, but that still made the
  user find, download, and mosaic the right elevation tiles for the scene — the
  last convert-side "same 500 lines of glue" named in `TODO.md`. `--dem auto` /
  `sicd_to_geocoded_cog(dem="auto")` closes it: it projects the scene's image
  corners to a geographic bbox, resolves the 1°×1°
  [Copernicus GLO-30](https://registry.opendata.aws/copernicus-dem/) tiles
  covering it, pulls them from the public AWS Open Data bucket (skipping the
  all-ocean gaps Copernicus
  omits with a 404, merging several into a mosaic), and terrain-orthorectifies
  against the result — one flag, correctly geolocated over relief. The new
  `umbra_py.dem` module keeps the tile math (`copernicus_tile_id`,
  `tiles_covering_bbox`, `tile_url`, `tile_ids_for_bbox`) pure standard library
  and offline-tested, and the fetch (`fetch_dem_for_bbox`) reuses the resume-safe
  `download_url` behind an injectable `download` callable, so the skip/merge/raise
  behaviour is covered with a stub downloader — only the multi-tile
  `rasterio.merge` mosaic touches the `[convert]` extra. Tiles are cached under
  the same XDG cache dir the index uses (`default_dem_cache_dir`,
  `$UMBRA_DEM_DIR`), so a second conversion over the same area re-downloads
  nothing. `fetch_dem_for_bbox`, `copernicus_tile_id`, `tile_ids_for_bbox`,
  `default_dem_cache_dir` and `DemUnavailableError` are exported from the package
  root; the `--dem` CLI option now accepts a path *or* `auto` and validates a
  given path exists.
- **DEM terrain orthorectification for SICD geocoding — `umbra convert --dem`
  / `sicd_to_geocoded_cog(dem=...)` (`docs/STRATEGY.md` 5.5).** The single named
  remaining strategic code gap: every path to the open data assumed a flat
  height plane, which mislocates relief (a pixel is placed where the radar ray
  meets the plane, not where it meets the ground). `--dem PATH` — any
  rasterio-readable elevation model, e.g. a Copernicus/SRTM COG — now walks each
  ground-control point onto the terrain surface via the standard ortho
  fixed-point iteration (`_refine_gcps_with_dem`: project at a height → sample
  the DEM there → reproject, until the height it lands on stops moving), so
  hilltops and valley floors land in their true ground position. `--dem`
  supersedes `--projection`; where the DEM has no coverage a point falls back to
  the scene reference height rather than snapping to zero. Both the iteration and
  the DEM lookup are injectable (`project`/`sample_height` callables), so the
  whole path is exercised offline with plain callables and a hand-written DEM
  raster — no sarpy DEM plumbing, and the sarpy-facing HAE projector batches
  points that share a (binned) height into one call. Stdlib/rasterio-only tests
  cover convergence to a closed-form terrain fixed point, the flat-DEM and
  off-DEM fallbacks, the DEM sampler (ramp read, out-of-bounds/nodata masking,
  CRS reprojection), and the end-to-end + CLI paths.
- **Published + fetchable whole-catalog PMTiles basemap — `umbra tiles --fetch`
  (`docs/STRATEGY.md` 5.2, `docs/DEMO_APP_GAPS.md` Path A step 3).** `umbra
  tiles` shipped the stdlib-only PMTiles *encoder*; this ships the built
  *artifact*. The weekly `publish-index.yml` workflow now tiles the freshly
  built index (`umbra tiles --local`, no second crawl) into a single-file
  `catalog.pmtiles` and writes a `catalog.html` MapLibre GL viewer pointed at
  the published archive, uploading both to the rolling `catalog-index` release
  beside `catalog.db` / `umbra-open-data.parquet`. The consume side mirrors
  `CatalogIndex.from_release()`: `pmtiles.fetch_prebuilt_pmtiles()` downloads the
  release asset via the resume-safe `download_url` to `default_pmtiles_path()`
  (`catalog.pmtiles` beside the cached `catalog.db`, honouring `$UMBRA_PMTILES`),
  and a new `umbra tiles --fetch` mode (`--out` optional, `--url` override,
  `--viewer` writes a local viewer) gives a fresh install a fast, zoom-anywhere
  map of the *entire* archive with no crawl and no index — the visual sibling of
  `umbra index fetch`. Stdlib-only and fully offline-tested against a mocked
  release download and a round-tripped archive; the existing build path is
  unchanged.
- **Read-through catalog search — `CatalogIndex.search_live()` and
  `umbra search --local --live` (`docs/CODEBASE_ANALYSIS.md` §4.4 / P3 #21).**
  The transparent middle between the instant-but-stale local index and the
  always-current-but-slow live walk, the "make the index the default path" gap
  the analysis doc names. `search_live()` answers the whole query from the local
  index *and* walks only acquisitions at or after the index's freshness horizon
  (its newest indexed `acq_date` minus `overlap_days`), merging the two streams
  in the usual `(task, acq_date)` order and de-duplicating by sidecar href — so a
  repeat search stays near-instant but still catches anything published since the
  index was built. With `refresh=True` (the default) each genuinely new
  acquisition the delta discovers is upserted into the index as it is yielded
  (the read-through cache warms, so the next call walks even less; a read-only
  index disables warming automatically rather than failing). `umbra search
  --local --live` exposes it on the CLI; `--live` without `--local` is rejected.
  The bound reuses the same recent-only sidecar pruning `umbra index update`
  relies on, and the whole path is offline-tested with an injected catalog.
- **Keyed single-item lookup on the catalog index — `CatalogIndex.get(item_id)`
  (`docs/CODEBASE_ANALYSIS.md` §4.5).** The retrieval complement to
  `search()`'s listing: `get()` returns the indexed `UmbraItem` with a given
  STAC id (or `None`), backed by a new `idx_items_id` index so it stays fast as
  the published `catalog.db` snapshot grows, rather than scanning an
  id-filtered `search`. `umbra serve`'s `GET /collections/{id}/items/{item_id}`
  now resolves through this keyed lookup when it is backed by an index (via a
  new `serve.get_one` helper), falling back to the id-filtered search for the
  live-catalog source that only lists. The index is additive — existing
  databases gain it on the next open with no schema-version bump — so a
  deployed or fetched snapshot needs no rebuild.
- **Structured `--json` success output on the remaining commands
  (`docs/AI_INTEGRATION_IDEAS.md` §A1).** The machine-readable *error* contract
  already shipped; this completes the *success* side, so every command that
  produces a result now has a stable, machine-readable stdout shape:
  - `umbra download --json` emits a `[{asset, path, bytes, sha256}, …]` array,
    hashing each written file with a streaming SHA-256 so a caller can verify
    what it fetched without re-reading it
    (`docs/schemas/download.schema.json`).
  - `umbra index info --json` emits the index summary — `path`, `size_bytes`,
    `items`, `start`, `end`, `tasks`, `built_at`
    (`docs/schemas/index-info.schema.json`).
  - The render commands `change`, `timescan`, `swipe`, `gallery` and `map`
    accept `--json` and emit a `{output, items_used, parameters}` manifest
    naming the artifact written, the acquisition ids it was built from, and the
    settings used; a command that also writes an auxiliary file (e.g.
    `umbra change --narrate`'s narration JSON) lists it under an optional
    `sidecars` map (`docs/schemas/render-manifest.schema.json`).

  Human progress lines, warnings, and the `--place` "Resolved …" status line go
  to stderr under `--json`, so stdout carries the JSON alone. The three new
  schemas are published as public API alongside the error contract
  (`docs/schemas/README.md`), under the same backwards-compatibility rules as
  `umbra_py.__all__`.
- **Machine-readable errors (`docs/AI_INTEGRATION_IDEAS.md` §A1).** Every
  `UmbraError` now carries an optional `hint` — a single actionable recovery
  step — and serializes to a stable `{"error", "message", "hint"}` dict via
  `UmbraError.to_dict()`. When a command fails and JSON output is active (the
  invocation passed `--json`, or `UMBRA_JSON` is set to a truthy value) the CLI
  prints that object to stderr instead of a prose line, so an agent can branch
  on `error` and act on `hint` without parsing a traceback; otherwise it prints
  the usual `error: …` line plus a `hint: …` line when one applies. The wire
  shape is published as public API in `docs/schemas/error.schema.json`
  (`docs/schemas/README.md`). Every optional-dependency and API-key error now
  populates `hint` with the exact `pip install` command or the environment
  variable to set (e.g. `pip install "umbra-py[viz]"`,
  `Set ANTHROPIC_API_KEY (or OPENAI_API_KEY)`), and geocoding's no-match error
  points at `--bbox`.

### Fixed
- **Catalog index is now safe for concurrent, multi-process access
  (`docs/CODEBASE_ANALYSIS.md` §4.5).** The published `catalog.db` snapshot
  (`umbra index fetch`) is a *shared* artifact — read by `umbra serve`, `umbra
  demo` and the MCP server while a CLI writer (`umbra index update` / `build` /
  `bake-*`) may be refreshing it in another process — but `CatalogIndex` opened
  its connection with SQLite's single-process defaults (rollback journal, no busy
  timeout), so a reader that arrived while a writer held a transaction could fail
  with `database is locked`. `CatalogIndex._configure_connection` now sets a
  `busy_timeout` (5 s — a contended access waits rather than erroring at once) and
  switches the file to WAL journal mode (best-effort, swallowed on a read-only
  medium), under which a reader never blocks on the writer and a single writer
  never blocks readers. WAL needs only the writable file and directory the index
  already required (it ensures the schema on every open), so it tightens nothing;
  `check_same_thread` is left at its default because `umbra serve` already opens a
  fresh backend per request. No model call, no new dependency (two stdlib
  `PRAGMA`s); offline-tested in `tests/test_index.py` (the PRAGMAs, WAL
  persistence across reopen, and a second connection reading during an open write
  transaction).
- **Asset classifier now recognises a plain `image/tiff` GeoTIFF
  (`docs/CODEBASE_ANALYSIS.md` P1 #8).** `_classify_asset` tested `"tif" in
  name`, but `name` is upper-cased (`f"{key} {href}".upper()`), so the lowercase
  substring could never match — dead code. Umbra's own COGs were still caught by
  the parallel `"geotiff" in media` check, but an asset that declares a plain
  `image/tiff` media type (no `geotiff` profile substring) with a `.tif` key
  slipped through and was dropped from `asset_map` / `available_assets` — i.e.
  its GEC product became invisible to `info`, `download`, and every consumer of
  the item. The check now matches `"TIF"` against the upper-cased `name`; added a
  regression test (`tests/test_models.py`) covering the plain-`image/tiff` case.

### Security
- **Generated HTML now escapes all remote metadata and validates link schemes
  (`docs/CODEBASE_ANALYSIS.md` §3.1).** The map/gallery/swipe/change artifacts
  and the `umbra view` / `umbra demo` pages interpolate strings that come from
  remote STAC JSON, and the CLI accepts arbitrary item URLs — so a hostile STAC
  document could previously inject markup (a `<script>` in an `id`/`platform`
  field) or a `javascript:` link into an HTML file a user then opens locally.
  `viz._popup_html` now `html.escape()`s every remote-derived value (`id`,
  `datetime`, `platform`, `instrument_mode`, `product_type`, `polarizations`,
  `available_assets`) and routes the STAC link through a new shared
  `_html.safe_href()` gate — a scheme allowlist (`http(s)` only) plus
  attribute-escaping — which drops the link rather than emitting an unsafe
  scheme. The same `safe_href` gate now covers `_html.py`'s card/gallery links,
  `viewer._viewer_html`'s panel/title/link, and `demo.py`'s client-side STAC
  link (scheme-guarded at build time). `_lazy_imagery.popup_button_html` already
  escaped its inputs and was unchanged.

### Added
- **The local catalog index is now schema-versioned (`docs/CODEBASE_ANALYSIS.md`
  §4.5 / P1 #10).** `CatalogIndex` records its on-disk layout with
  `PRAGMA user_version` (`_SCHEMA_VERSION = 1`) and checks it on open. This
  matters because the index is no longer a private cache — the weekly `catalog.db`
  snapshot users pull with `umbra index fetch` is a *distributed* artifact that
  `--local` search, the MCP server, `umbra serve`, `umbra demo` and `umbra tiles`
  all consume — so the next schema change (the demo denormalizations in
  `docs/DEMO_APP_GAPS.md` G2, an R\*Tree upgrade) needs to be a migration, not a
  confusing break. A fresh or pre-versioning database (`user_version 0`, which
  every current snapshot reads) is adopted in place and stamped; a database
  written by a *newer* umbra-py — or a lower versioned schema with no migration
  path — now raises the new `IndexSchemaError` (surfaced by the CLI as a clean
  `error: …`) instead of being silently misread. No new dependency, no behaviour
  change for a matching index; mirrors the `PRAGMA user_version` discipline the
  `catalog.embed.db` sidecar already used. `IndexSchemaError` is exported from the
  top-level package.
- **STAC Query extension on `umbra serve` — filter `/search` by product type and
  place, not just bbox/date (`docs/AI_INTEGRATION_IDEAS.md` §B2 / `docs/DEMO_APP_GAPS.md`
  Path B).** The read-only STAC API answered only the STAC *core* filters (bbox,
  datetime, ids), even though the `CatalogIndex` it wraps already filters by
  product type and free-text task/site `area`. `/search` and
  `/collections/{id}/items` now accept `product_types` (comma-separated, e.g.
  `GEC,SICD`), `area` (a task/site substring) and a `fuzzy` toggle — as GET
  query params, plain top-level `POST` body fields, or a standards-compliant
  STAC **Query extension** object (`{"query": {"product_types": {"in": ["GEC"]},
  "area": {"like": "Beet Piler"}}}`, with bare-value shorthands). The filters
  are pushed straight down to the backend `search` both `CatalogIndex` and the
  live `UmbraCatalog` already implement, so the same query works against either,
  and GET pagination carries them into the `next` link. Two new pure parsers
  keep it honest: `parse_product_types` rejects an unknown product type with a
  `400` (never a silent empty result), and `parse_query` rejects an unsupported
  query property or operator with a `400` so a client's filter is never quietly
  dropped. The `item-search#query` conformance class is now advertised. Wired
  entirely behind the deterministic document/parse boundary, so it is
  offline-tested through the in-process `TestClient` with no network and no
  `viz` extra.
- **Visual similarity search over MCP — `find_similar` / `find_similar_text`
  tools on `umbra-mcp` (`docs/AI_INTEGRATION_IDEAS.md` §C5).** The flagship
  scene-embedding capability (`umbra embed`) is now conversational: the
  `umbra-mcp` server exposes two tools (plus a `find-similar-scenes` prompt) that
  wrap the shipped `SceneEmbeddingIndex` unchanged. `find_similar(url)` renders and
  embeds one acquisition's quicklook and ranks the pre-embedded archive by cosine
  similarity — "find scenes that *look like* this flooded field", the search that
  lives in the pixels rather than the metadata, with the query item excluded from
  its own results; `find_similar_text(query)` ranks the stored image vectors against
  a plain-language query ("ships at a berth") given a joint CLIP-family model. Both
  require a scene index built ahead of time with `umbra embed build` (a sidecar
  `catalog.embed.db`; a missing one raises a self-describing error pointing at that
  command) and the `[ai]` embedding key, and return `SceneMatch` records as compact
  cards — each carrying the acquisition's STAC `href`, so a match hands straight to
  `get_item` / `quicklook` / `change_composite`, closing the discover-then-view loop
  in one conversation. Like the rest of the server they hold the determinism
  boundary: the only model call is turning the query image/text into a vector (the
  injectable `default_image_embedder` / `default_text_embedder`), while rendering,
  storage and cosine ranking stay deterministic — so the whole path is
  offline-tested with a stand-in embedder and renderer.
- **Incremental index refresh — `umbra index update` / `CatalogIndex.update`
  (`docs/CODEBASE_ANALYSIS.md` §4.4, `docs/STRATEGY.md` §6).** A full `umbra
  index build` fetches a `*.stac.v2.json` sidecar for *every* acquisition in
  scope — the N+1 round trips that dominate a crawl — so on an index only days
  old almost all of that work re-reads unchanged data. `update` instead reads
  the newest acquisition date already indexed and passes it (minus
  `--overlap-days`, default 1) as the `start` bound to the live walk, which
  prunes older acquisitions' sidecar fetches, so a weekly refresh reads only the
  new passes and upserts them exactly as `build` does. It is the incremental
  companion to the shipped `umbra index fetch` / `CatalogIndex.from_release`:
  bootstrap from the weekly snapshot once, then `update` to catch acquisitions
  published since — the "walk only prefixes newer than the index" improvement the
  analysis doc named and the "keep the crawl incremental" guardrail in the
  strategy doc. The bound is on *acquisition* date, not publish date, so
  `--overlap-days` re-scans a little past the newest indexed date to catch
  near-real-time lag, and the docstring is explicit that completeness over
  back-dated late arrivals still wants a widened window or a full `build`. An
  empty index falls back to a full build; `--since` forces a specific lower
  bound; `--bbox`/`--place`/`--area`/`--limit` scope the refresh exactly as
  `build` does. `CatalogIndex.update()` returns an `UpdateResult`
  (`scanned`/`added`/`refreshed`/`start`, exported from the package root), and
  the whole path is offline-tested with a recording fake catalog (derived-bound,
  overlap widening, new-vs-refreshed tally, empty-index fallback, `since`
  override, scope pass-through, and the `start=`-rejection guard), plus a CLI
  test. The published weekly snapshot is deliberately left as a full rebuild so
  it stays authoritative.
- **Whole-catalog PMTiles tiling — `umbra tiles` / `build_pmtiles`
  (`docs/DEMO_APP_GAPS.md` Path A step 3).** Every other map surface embeds its
  features in the page (Folium polygons in `umbra map`, an inline JSON blob in
  `umbra demo`) — great up to a few thousand acquisitions, but the *whole*
  acquisition set was the last open demo-app gap. `umbra tiles` pre-cuts the
  catalog's acquisition centroids into a vector-tile pyramid and packages it as a
  single [PMTiles v3](https://github.com/protomaps/PMTiles) file, so a map fetches
  only the tiles in view and stays fast at whole-archive scale. The output drops
  straight onto GitHub Pages or into a bucket — no tile server, and **no
  tippecanoe**: because the geometry is points, the entire encoder (the Mapbox
  Vector Tile protobuf and the PMTiles container) is pure standard library, so it
  runs in a core install and is fully offline-tested by decoding its own output
  (verified against the reference `pmtiles` / `mapbox-vector-tile` readers).
  `--viewer` also writes a self-contained MapLibre GL page that renders the
  archive as a scalable circle layer with a click popup, the same OpenStreetMap
  basemap the Leaflet demo uses, and the mandatory CC-BY attribution. Reads a
  prebuilt index with `--local` for a near-instant build. `build_pmtiles` /
  `write_pmtiles` / `build_viewer` / `save_viewer` are exported from the package
  root.
- **SICD → geocoded COG — `umbra convert` / `sicd_to_geocoded_cog`
  (`docs/STRATEGY.md` 5.5).** Umbra's `GEC` asset is already a geocoded COG, but
  the complex `SICD` product lives in the radar slant plane and does not open on
  a map, in QGIS, or in the xarray/rioxarray stack without hand-rolled
  geocoding. The new `convert` extra function detects amplitude from the complex
  product and warps it onto a north-up EPSG:4326 cloud-optimized GeoTIFF using
  SICD's own image-projection model — a lattice of ground control points from
  `project_image_to_ground_geo`, so the sensor geometry (not a naive
  corner-stretch) places the pixels. `umbra convert SRC DST` geocodes by default
  (with `--gcp-grid`, `--resolution`, `--resampling`, `--projection`, and
  `--linear` for magnitude instead of dB); `--slant-plane` keeps the prior
  ungeoreferenced amplitude image for quick inspection. The geocoding is an
  honest flat-earth first slice (pixels on the scene's height-above-ellipsoid
  plane): exact over flat terrain, adequate for map placement elsewhere; full
  terrain orthorectification (a DEM, MultiRTC interop) is the follow-on. The
  geocoding core (`_warp_gcps_to_cog`) is free of any sarpy dependency, so it is
  offline-tested with a plain array and hand-built GCPs against real `rasterio`,
  and the read → amplitude → GCP → warp path is exercised end to end with a
  faked reader — `convert.py` gains its first test suite (the `[convert]` extra
  CI job). `sicd_to_amplitude_geotiff` / `sicd_to_geocoded_cog` are exported
  from the package root.
- **Async job semantics for long `umbra serve` renders — `202 Accepted` + poll
  (`docs/DEMO_APP_GAPS.md` Path B step 2).** The composite render endpoints
  (`POST /artifacts/change` / `timescan` / `swipe`) accept an opt-in
  `"async": true` in the request body. Instead of holding the request for the
  whole render, the server queues the work on a small background pool and returns
  `202 Accepted` with a job document (and a `Location` header). Two new endpoints
  drive the poll loop: `GET /jobs/{id}` reports status
  (`queued` → `running` → `succeeded` | `failed`, with a `result` link once done)
  and `GET /jobs/{id}/result` serves the finished artifact. There is **no
  separate result store** — the render writes the same content-addressed disk
  cache the synchronous path uses, so a completed job's result *is* a cache entry,
  and an async request whose key is already cached returns an already-`succeeded`
  job with no work. Frame resolution and validation stay synchronous, so a bad
  request (too few acquisitions, malformed bbox) is still a fast `400` and never a
  doomed background job; a failed render becomes a `failed` job whose result
  endpoint mirrors the synchronous status (`501` for a missing `viz` extra, `500`
  otherwise). Default behavior is unchanged when `"async"` is absent. The queue's
  executor is injectable (`build_app(..., job_executor=...)`) and a new pure
  `job_to_dict` builder keeps the whole path offline-testable with no wall-clock
  timing.
- **`POST /artifacts/swipe` on `umbra serve`, and `umbra demo --server-url` that
  calls the render endpoints — closing the self-serve demo loop
  (`docs/DEMO_APP_GAPS.md` R4 / Path B step 3).** `umbra serve` gained a fourth
  artifact endpoint that renders `viz.swipe_map` (an interactive before/after
  HTML page) alongside the three PNG composites; because it returns HTML rather
  than a PNG it is served from its own disk-cache entry, and the render
  functions stay injectable (offline-testable) via a new `swipe` field on
  `Renderers`. The server now also sets a permissive **read-only CORS** policy
  so a browser page on another origin can call `/search` and the artifact
  endpoints. On the front end, `build_demo(..., server_url=...)` /
  `umbra demo --server-url <serve URL>` add an "Analyze this view" sidebar panel
  whose Change / Timescan / Swipe buttons POST the currently-filtered
  acquisitions (chronological, sampled to a bounded cap) to the matching
  endpoint and render the returned artifact in place (swipe opens its map in a
  new tab) — the R4 "run this analysis here" affordance over *any* site. With no
  `server_url` the page stays a fully static single file, unchanged.

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
