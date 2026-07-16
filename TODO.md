# Outstanding TODOs

This file tracks follow-up items that were intentionally scoped out of merged
PRs. Each entry should link to the PR that surfaced it, point at the code
involved, and describe the smallest change that closes it out.

When you finish one, delete the entry (or move it under a short "Done" log at
the bottom if the history is useful).

---

## Whole-catalog PMTiles tiling follow-ons (`umbra tiles` shipped)

- **Surfaced in:** the `umbra tiles` PR (`docs/DEMO_APP_GAPS.md` Path A step 3).
- **Code:** `src/umbra_py/pmtiles.py`, `umbra tiles` in `cli.py`.

`umbra tiles` (a stdlib-only PMTiles v3 writer over acquisition centroids + a
MapLibre GL viewer, no extra, no tippecanoe) is shipped, closing the demo's
full-acquisition-set tiling gap. Follow-ons that build on it, none a blocker:

- **Wire the PMTiles source into `umbra demo`.** The demo embeds its gathered
  slice as inline JSON; an opt-in `--pmtiles <url>` that swaps the Leaflet
  cluster layer for a MapLibre vector layer over a tiled archive would let the
  interactive explorer scale to the whole catalog too (today `tiles` ships its
  own separate MapLibre viewer to keep the proven demo page untouched).
- **Leaf directories for very large catalogs.** The writer emits a single root
  directory, which is spec-valid and ample for the current catalog (thousands of
  tiles). If the tile count ever grows past a comfortable root-directory size,
  add leaf-directory splitting (the PMTiles spec's mechanism) so readers still
  fetch a small root first.
- **Tile polygons, not just centroids.** Points are what a whole-catalog overview
  needs; tiling the actual footprints (clipping polygons to tile boundaries)
  would let the viewer show coverage shape at high zoom — more encoder work
  (clipping, MoveTo/LineTo/ClosePath commands) for a niche gain.

---

## Register `umbra-mcp` in the MCP registries and Anthropic's directory

- **Surfaced in:** the `umbra-mcp` MCP server PR (`AI_INTEGRATION_IDEAS.md` B1).
- **Code:** `src/umbra_py/mcp_server.py`, `pyproject.toml` (`[mcp]` extra,
  `umbra-mcp` console script).

The server itself is shipped and runnable (`umbra mcp` / `uvx umbra-mcp`), but
registering it in the public MCP registries and Anthropic's directory — the
discovery half of the deliverable — is still open. Follow-ons named in the B1
doc: a LangChain/LlamaIndex community tool wrapper reusing the same tool shapes,
and returning the polarization-mixing warning as structured text alongside the
`change_composite` image block.

---

## Grow the `umbra serve` STAC API (query extensions + a hosted instance)

- **Surfaced in:** the `umbra serve` STAC API PR (`AI_INTEGRATION_IDEAS.md` B2 /
  `DEMO_APP_GAPS.md` Path B).
- **Code:** `src/umbra_py/serve.py`, `pyproject.toml` (`[serve]` extra).

The read-only STAC API is shipped (landing / conformance / collections / items /
`GET`+`POST /search` with bbox, datetime, ids and token pagination), and now
renders artifacts on demand (`GET /artifacts/quicklook/{id}.png`, `POST
/artifacts/change`, `POST /artifacts/timescan`, `POST /artifacts/swipe`), each
disk-cached by its inputs and wrapping the existing `viz` functions behind
injectable renderers. The `umbra demo` front end now calls these endpoints (see
the Done log), closing the self-serve R4 loop. **Async job semantics for long
renders are now shipped** (see the Done log): a composite request can opt in to
`"async": true`, get a `202 Accepted` + a job id, poll `GET /jobs/{id}`, and
fetch the result from `GET /jobs/{id}/result` (the disk cache is the result
store). **The STAC Query extension now exposes the index's two Umbra-specific
filters** (see the Done log): `/search` and `/collections/{id}/items` take
`product_types`, `area` and `fuzzy` (as GET params, top-level POST fields, or a
STAC `query` object), advertised via the `item-search#query` conformance class.
Open follow-ons:

- **Geometry `intersects`.** The Query extension now covers `product_types` and
  `area`; geometry `intersects` still needs more than the stored footprint bbox
  (a real polygon-in-polygon test), so it remains unexposed.
- **A hosted community instance.** The local-first server has no operational
  cost; a public instance is a policy decision (COG-streaming egress) that would
  make the archive queryable with zero install — pair it with the demo front end
  in `DEMO_APP_GAPS.md` Path B.

---

## Canopy commercial-archive backend follow-ons (`UmbraCatalog(token=...)` shipped)

- **Surfaced in:** the Canopy backend PR (`docs/STRATEGY.md` 5.1).
- **Code:** `src/umbra_py/catalog.py` (`_search_archive` / `_archive_page`),
  `src/umbra_py/constants.py` (`CANOPY_ARCHIVE_URL`), `umbra search --token`.

The commercial archive is now searchable behind the same `search()` interface
(bearer token → STAC API POST search + `rel="next"` pagination, offline-tested
against a mocked API). Open follow-ons, none a blocker:

- **Push `product_types` / `area` down as STAC query/filter extensions.** They
  are applied client-side today (exact parity with the open-bucket path). Once
  the concrete Canopy field names are confirmed against the live API, sending
  them as a STAC *query*/*filter* body would let the server pre-filter and cut
  transferred pages. This needs a real token to verify, so it is deliberately
  deferred rather than guessed.
- **`get_item(id)` against the archive.** `UmbraCatalog.search` covers listing;
  a keyed single-item fetch (`GET /collections/{id}/items/{item_id}` or an `ids`
  search) would round out the interface for the MCP `get_item` tool over the
  commercial archive.
- **Verify request/response shapes against the live Canopy API.** The client is
  built to the STAC API *standard*; confirm the exact search body, collection
  ids, and pagination link shape Canopy emits, and adjust if it deviates. Add a
  `network`-marked smoke test gated on a `UMBRA_CANOPY_TOKEN` secret.
- **Wire `--token` into the visual commands.** `umbra search` takes `--token`;
  the render commands (`map`/`gallery`/`change`/…) route through `_gather_items`
  and could accept it too, so a paying user renders the commercial archive with
  the same flags.

---

## C1 natural-language search follow-ons (all four steps now shipped)

The four C1 steps — relative dates (`dates.py`), the deterministic fuzzy task
matcher (`fuzzy.py`), the model-planned `umbra ask` (`planner.py`), and the
semantic embedding index (`semantic.py`) — are all shipped (see the **Done**
log). Optional follow-ons that build on them, not blockers:

- **LangChain/LlamaIndex tool wrapper** reusing `SearchPlan` / the semantic
  matcher (same shapes, different registration) — worth doing for reach.
- **MCP `search_catalog` semantic mode.** The MCP tool exposes `fuzzy=`; a
  `semantic=` mode (resolving a query to task names via `SemanticTaskIndex`
  before searching) would give agents the same aliasing the CLI now has — gated,
  like the CLI, on the `[ai]` embedding key being configured.
- **Embed task *descriptions*, not just names.** The current index embeds the
  task label; if Umbra publishes per-task descriptions, embedding those too would
  widen recall further.

---

## C2 VLM-in-the-loop follow-ons (`umbra describe` shipped)

- **Surfaced in:** the `umbra describe` PR (`AI_INTEGRATION_IDEAS.md` C2).
- **Code:** `src/umbra_py/describe.py` (`[ai]` + `[viz]` extras),
  `constants.AI_PROVENANCE`.

`umbra describe` (scene description) is shipped — a vision model reads the
rendered quicklook plus the A3 context card and returns a provenance-stamped
`{summary, observed_features[], confidence, caveats[]}`. The rest of C2 is still
open and builds on the same boundary:

- **`umbra change --narrate`** (the second half of C2): after writing a change
  composite, send it with the color-semantics legend and a coarse per-block
  |Δ|-in-dB sidecar to a VLM and return a plain-language, number-grounded change
  report — so the narration cites the deterministic statistics, not vibes. Reuse
  `describe.py`'s `Describer`/`parse_*` boundary and the `AI_PROVENANCE` stamp.
- **MCP `describe_scene` tool.** The MCP server already returns imagery; a
  `describe_scene` tool wrapping `describe()` would let an agent get the
  structured reading directly (gated, like the CLI, on the `[ai]` key).
- **A `describe` render is a fresh S3 read every call.** When the demo/thumbnail
  bake (`DEMO_APP_GAPS.md` G6) lands, feed the cached quicklook into `describe`
  via its injectable `render=` hook instead of re-streaming the COG.

---

## C3 monitoring follow-ons (`umbra watch` shipped)

- **Surfaced in:** the `umbra watch` PR (`AI_INTEGRATION_IDEAS.md` C3).
- **Code:** `src/umbra_py/watch.py`, `umbra watch` in `cli.py`.

`umbra watch` (idempotent delta detection) is shipped — it searches, diffs the
results against the set of acquisitions previous runs already reported (state in
the `CatalogIndex` `meta` table), returns only the new ones, and remembers them,
so cron / a GitHub Action / an agent loop can supply the schedule. No model is
called. The remaining C3 pieces build on it:

- **MCP `watch_site` tool / prompt.** The `watch()` function is a plain,
  deterministic callable; wrapping it as an MCP tool (returning the same JSON
  delta) would let an MCP client run the standing check conversationally, reusing
  the state store unchanged.
- **A packaged monitoring recipe/notebook.** The base example gallery has
  shipped (`examples/01_hello_umbra.ipynb`, `02_download_and_open_gec.ipynb`,
  `03_change_detection.ipynb`; `B3` / `STRATEGY.md` 5.4, guarded offline by
  `tests/test_examples.py`). Still open: a *standing-analyst* notebook that wires
  `umbra watch --json` → `select_change_frames` → `umbra change --narrate` into
  one runnable example so the "new pass lands → composite → narration → notify"
  loop ships as a copy-pasteable standing analyst, not just a set of primitives.

---

## C4/C5 ML dataset follow-ons (`umbra chips` shipped)

- **Surfaced in:** the `umbra chips` PR (`AI_INTEGRATION_IDEAS.md` C4 /
  `STRATEGY.md` 5.5).
- **Code:** `src/umbra_py/chips.py`, `umbra chips` in `cli.py`.

`umbra chips` (fixed-size, georeferenced ML tiles + a `.jsonl`/`.geojson`
manifest, `[load]` extra, no model call) is shipped. Follow-ons that build on it,
not blockers:

- **Publish the chip manifest as stac-geoparquet.** The manifest is JSONL /
  GeoJSON today; a `.parquet` option (reusing the `[export]` extra's
  stac-geoparquet plumbing) would let DuckDB / geopandas query a large chip set
  without loading every line.
- **Chip the complex products.** The chipper reads amplitude rasters (GEC/CSI);
  chipping SICD/CPHD would need the slant-plane handling that `convert.py`
  begins — related to the still-open SICD → geocoded COG gap in `STRATEGY.md` 5.5.

---

## C5 archive-embedding follow-ons (`umbra embed` shipped)

- **Surfaced in:** the `umbra embed` PR (`AI_INTEGRATION_IDEAS.md` C5 /
  `STRATEGY.md` 5.2).
- **Code:** `src/umbra_py/embed.py`, `umbra embed` in `cli.py`.

`umbra embed` (visual similarity search — one image vector per acquisition in a
sidecar `catalog.embed.db`, `search_similar(item)` and text-to-scene, `[ai]` +
`[viz]` extras) is shipped. Follow-ons that build on it, not blockers:

- **Publish the embedding table with the nightly index.** The scene vectors are
  local-only today; publishing `catalog.embed.db` (or a stac-geoparquet embedding
  table) beside the weekly `catalog.db` snapshot would let a fresh install run
  `umbra embed similar` with no rebuild — and is exactly the kind of artifact worth
  offering upstream (`STRATEGY.md` 5.2). Note the published table would be model-
  and dimension-specific, so record the model label prominently.
- **A native vector index at scale.** Ranking is a brute-force cosine scan today
  (instant at catalog scale, no binary dependency). If the archive grows to
  hundreds of thousands of scenes, the schema leaves room to swap in `sqlite-vec`
  or an ANN index behind the same `similar()` API.
- **A SAR-tuned encoder.** The default targets a generic CLIP-family multimodal
  `/embeddings` endpoint; a SAR-specific encoder (once one is broadly available)
  would sharpen recall for radar-specific scene types. The `model` label already
  guards against silently mixing encoders in one index.

---

## Download: verify the ETag checksum, not just the byte count

- **Surfaced in:** the HTTP/download hardening PR (`docs/CODEBASE_ANALYSIS.md`
  P1 #5 / §3.2).
- **Code:** `src/umbra_py/download.py` (`download_url`).

`download_url` now verifies the received byte count against `Content-Length` and
uses `If-Range` + a stored ETag so a resume can't splice two different objects.
The remaining §3.2 item is *content* verification: S3's ETag is the MD5 of the
object for single-part uploads (no `-` suffix), so hashing the finished file and
comparing to the stored ETag would catch on-the-wire corruption that a correct
length can't. Skip the check when the ETag is multipart (`"<hash>-<n>"`), where
it isn't a plain MD5. Small, and testable offline with a known body + its MD5.

---

## Done

- **Publish + fetch the whole-catalog `catalog.pmtiles` basemap (`umbra tiles
  --fetch`).** The weekly `publish-index.yml` workflow now tiles the freshly
  built index (`umbra tiles --local`, no second crawl) into a single-file
  `catalog.pmtiles` and writes a `catalog.html` MapLibre viewer pointed at the
  published archive's stable release URL, uploading both to the rolling
  `catalog-index` release beside `catalog.db` / `umbra-open-data.parquet`. The
  consume side mirrors `CatalogIndex.from_release`: `pmtiles.fetch_prebuilt_pmtiles`
  (resume-safe `download_url` of the release asset, default
  `pmtiles.default_pmtiles_path` = `catalog.pmtiles` beside the cached
  `catalog.db`, honouring `$UMBRA_PMTILES`) and a new `umbra tiles --fetch`
  mode (`--out` optional, `--url` override, `--viewer` writes a local viewer)
  give a fresh install a fast, zoom-anywhere whole-archive map with no crawl and
  no index — the visual sibling of `umbra index fetch`, and the published
  artifact worth offering upstream (`STRATEGY.md` 5.2, `DEMO_APP_GAPS.md` Path A
  step 3). Stdlib-only and fully offline-tested (mocked release download +
  round-tripped archive). This closed the "Publish `catalog.pmtiles` with the
  nightly index" PMTiles follow-on above.
- **Read-through catalog search — `CatalogIndex.search_live` / `umbra search
  --local --live` (`docs/CODEBASE_ANALYSIS.md` §4.4 / P3 #21).** The transparent
  middle between the instant-but-stale local index and the always-current live
  walk, the "make the index the default path" gap. `search_live` answers the
  whole query from the local index *and* walks only acquisitions at or after the
  index's freshness horizon (its newest `acq_date` minus `overlap_days`), merges
  the two streams (`heapq.merge` on the `(task, acq_date, href)` key) and
  de-duplicates by sidecar href, so an acquisition the index already holds is
  never yielded twice and the result is what a single fresh search would return.
  With `refresh=True` (the default) each genuinely new acquisition the delta
  discovers is upserted as it is yielded — the read-through cache warms, so the
  next call walks even less — committing (and re-stamping `built_at`) only when a
  row was actually added; a read-only index catches the `OperationalError` and
  disables the write-back rather than failing the search. `umbra search --local
  --live` exposes it (and `--live` without `--local` is a clean error). It reuses
  the same recent-only sidecar pruning `CatalogIndex.update` relies on and is
  delivered as an explicit method + flag rather than an implicit mode change to
  `search`, so a plain `search` is unchanged. Fully offline-tested in
  `tests/test_index.py` (horizon derivation, merge/dedup, cache warming,
  `refresh=False`, start-bound interaction, empty-index seed, and the two CLI
  paths) with an injected catalog. Was `docs/CODEBASE_ANALYSIS.md` §4.4's last
  open item and P3 #21.
- **Keyed single-item lookup on the catalog index (`umbra serve` follow-on).**
  `/collections/{id}/items/{item_id}` previously resolved a single item by
  filtering an id-scoped `run_search` in the serve layer — a scan of the ordered
  result set. Added `CatalogIndex.get(item_id) -> UmbraItem | None`, an
  `idx_items_id`-backed point lookup (the retrieval complement to `search`'s
  listing), and a `serve.get_one(source, item_id)` helper that uses it when the
  backend is a `CatalogIndex` and falls back to the id-filtered search for the
  live `UmbraCatalog`, which only lists. The new index is additive — added to
  `_SCHEMA` with `CREATE INDEX IF NOT EXISTS`, so existing databases (including a
  fetched snapshot) gain it on the next open with no `PRAGMA user_version` bump,
  exactly the additive path the schema-version marker was landed to enable.
  Fully offline-tested (`tests/test_index.py`, `tests/test_serve.py`): found /
  missing / index-present, plus the keyed-vs-listing dispatch in `get_one`.
  Was `docs/CODEBASE_ANALYSIS.md` §4.5 and this file's `umbra serve` open item.
  The Canopy-archive `get_item(id)` (a keyed fetch against the commercial STAC
  API) remains the separate open follow-on under the Canopy section.
- **Structured `--json` success output on the remaining commands (A1 follow-on).**
  The A1 error contract already shipped (structured stderr errors with `hint`,
  `docs/schemas/error.schema.json`); this completes the success side so every
  command that produces a result has a machine-readable stdout shape. `umbra
  download --json` emits a `[{asset, path, bytes, sha256}, …]` array (hashing each
  written file with a streaming SHA-256), `umbra index info --json` prints the
  `CatalogIndex.stats()` summary plus `path`/`size_bytes`, and the five render
  commands (`change`, `timescan`, `swipe`, `gallery`, `map`) print a `{output,
  items_used, parameters}` manifest — with an optional `sidecars` map for the
  auxiliary files a command writes (e.g. `umbra change --narrate`'s narration
  JSON). Human progress/warnings and the `--place` "Resolved …" status line were
  moved to (or kept on) stderr so stdout carries the JSON object alone. Three
  schemas published under `docs/schemas/` (`download`, `index-info`,
  `render-manifest`) and documented in `docs/schemas/README.md`, under the same
  compatibility rules as `__all__`. Fully offline-tested in `tests/test_cli_json.py`
  with injected renderers/downloads (no network, no `viz` extra). Was
  `AI_INTEGRATION_IDEAS.md` §A1's last open item.
- **STAC Query extension on `umbra serve` — expose the index's `product_types` /
  `area` / `fuzzy` filters over `/search`.** The read-only STAC API previously
  answered only the STAC *core* filters (bbox, datetime, ids), even though the
  `CatalogIndex` it wraps also filters by product type and free-text task/site
  `area` (with an optional token-wise `fuzzy` widen). Wired those two
  Umbra-specific filters through the API: `run_search` and `_do_search` now
  thread `product_types` / `area` / `fuzzy` down to the backend's `search`
  (which both `CatalogIndex` and the live `UmbraCatalog` already accept, so the
  same query works against either), and the endpoints accept them three ways —
  GET query params on `/search` and `/collections/{id}/items`
  (`?product_types=GEC,SICD&area=Beet+Piler&fuzzy=true`), plain top-level POST
  body fields, and a proper STAC **Query extension** object
  (`{"query": {"product_types": {"in": ["GEC"]}, "area": {"like": "Beet"}}}`,
  with bare-value shorthands). Two new pure parsers do the work offline —
  `parse_product_types` (comma/list → canonical `PRODUCT_ASSETS`, an unknown
  type is a `400`, not a silent empty result) and `parse_query` (maps the Query
  object onto the two fields; an unsupported property or operator is a hard
  `400` so a client's filter is never silently dropped). The
  `item-search#query` conformance class is now advertised, and GET pagination
  carries the filters into the `next` link. Fully offline-testable through the
  existing in-process `TestClient` harness (no network, no `viz` extra). Was
  `AI_INTEGRATION_IDEAS.md` B2 / `DEMO_APP_GAPS.md` Path B's "query extensions"
  follow-on and this file's `umbra serve` open item.
- **MCP `find_similar` / `find_similar_text` tools — visual similarity search over
  the flagship server (C5 follow-on).** Surfaced the shipped `umbra embed`
  capability (`SceneEmbeddingIndex.similar_to_item` / `similar_to_text`) as two
  tools on `umbra-mcp` (`src/umbra_py/mcp_server.py`), plus a `find-similar-scenes`
  prompt. `find_similar(url)` renders + embeds the query item's quicklook and ranks
  the pre-embedded archive by cosine similarity (image-to-image, query excluded from
  its own results); `find_similar_text(query)` ranks the stored image vectors against
  a text query (text-to-scene, joint CLIP-family model). Both reuse the existing
  `SceneEmbeddingIndex` unchanged, gate on a prebuilt sidecar `catalog.embed.db`
  (a self-describing `FileNotFoundError` pointing at `umbra embed build` when
  absent) and the `[ai]` embedding key, and return `SceneMatch` records as compact
  cards carrying each acquisition's STAC `href` so a match hands straight to
  `get_item` / `quicklook` / `change_composite`. It holds the server's determinism
  boundary (`AI_INTEGRATION_IDEAS.md` §A4/§6.1): the only model call is turning the
  query image/text into a vector (the injectable `default_image_embedder` /
  `default_text_embedder`), while rendering, storage and ranking are deterministic
  — so the whole path is offline-tested with a stand-in embedder and renderer, no
  `[viz]`/network. Named in `AI_INTEGRATION_IDEAS.md` §C5 and this file's C5
  follow-ons.
- **Async job semantics for long `umbra serve` renders (`202 Accepted` + poll).**
  Added a small in-memory job queue to `src/umbra_py/serve.py` so a composite
  render need not hold a request for its whole duration. A `POST /artifacts/change`
  / `timescan` / `swipe` request that carries `"async": true` gets a `202 Accepted`
  and a job document back immediately; the render runs on a background pool
  (`ARTIFACT_JOB_WORKERS`, injectable via `build_app(..., job_executor=...)`).
  `GET /jobs/{id}` polls status (`queued` → `running` → `succeeded` | `failed`)
  and `GET /jobs/{id}/result` serves the finished artifact — from the *same*
  content-addressed disk cache the synchronous path writes, so there is no
  separate result store and an async request whose key is already cached returns
  an already-`succeeded` job with no work. Frame resolution/validation stays
  synchronous, so a bad request (too few acquisitions, malformed bbox) is still a
  fast `400`, never a doomed job; a failed render becomes a `failed` job whose
  result endpoint mirrors the sync path's status (`501` for a missing `viz`
  extra, `500` otherwise). The default synchronous behavior is unchanged when
  `"async"` is absent. New pure builder `job_to_dict` and the injectable executor
  keep it offline-testable without wall-clock timing. This was
  `DEMO_APP_GAPS.md` Path B step 2's remaining item.
- **`POST /artifacts/swipe` + the demo front end that calls the render
  endpoints (closes the self-serve R4 loop).** Added the fourth artifact
  endpoint to `src/umbra_py/serve.py`: `POST /artifacts/swipe` wraps
  `viz.swipe_map` (before/after co-registered passes) and returns a
  self-contained **HTML** page — so `_serve_artifact` grew a `media_type`/
  `suffix` so a swipe caches to its own `.html` entry, distinct from the PNG
  composites, and `Renderers` grew a `swipe` field (injectable, offline-tested
  like the rest). `swipe_frames` collapses a many-frame query to its temporal
  endpoints (first/last). `umbra serve` now also sets a permissive read-only
  CORS policy so a browser page on another origin can call it. The front end:
  `build_demo(..., server_url=...)` / `umbra demo --server-url` adds an "Analyze
  this view" sidebar panel whose Change / Timescan / Swipe buttons POST the
  currently-filtered acquisitions (chronological, sampled to a bounded cap) to
  the matching endpoint and render the returned artifact in place (swipe opens
  its interactive map in a new tab). With no `server_url` the page stays a fully
  static single file, unchanged. This was `DEMO_APP_GAPS.md` R4 / Path B step 3
  — the last self-serve-demo gap.
- **`umbra embed`: archive scene embeddings / visual similarity search (C5).**
  Added `src/umbra_py/embed.py` (`[ai]` + `[viz]` extras). `umbra embed build`
  renders each acquisition's quicklook once (reusing `umbra describe`'s injectable
  renderer — only downsampled overviews stream over HTTP) and embeds it into a
  vector stored in a schema-versioned sidecar `catalog.embed.db` beside the catalog
  index, keyed by item id and idempotent (a rebuild only embeds what is new; a
  scene whose asset won't render is skipped, not fatal). `umbra embed similar
  <url>` renders + embeds the query item and returns the archived scenes that look
  most like it (image-to-image, the query excluded from its own results); `umbra
  embed search "…"` ranks the stored image vectors against a text query
  (text-to-scene, with a joint CLIP-family model); `umbra embed info` reports the
  count, model and dimension. The only model calls are turning an image or a text
  query into a vector — both injectable (`ImageEmbedder` / text `Embedder`, default
  an OpenAI-compatible multimodal `/embeddings` endpoint via `requests`,
  user-supplied key) — while rendering, storage, `cosine_similarity` (reused from
  `umbra_py.semantic`) ranking and thresholding are stdlib-only (no `numpy`, no
  `sqlite-vec`), so the whole feature is offline-testable with a deterministic
  stand-in embedder and renderer. Chose a sidecar `catalog.embed.db` over embedding
  vectors *inside* `catalog.db` so the deterministic index and its published
  snapshot never carry model-derived data a core install can't use — the same
  boundary `umbra semantic` uses. A `SceneMatch` is a pointer back to a real
  acquisition (id, task, datetime, STAC href), never a model-authored fact.
- **`umbra chips`: ML dataset preparation (C4).** Added `src/umbra_py/chips.py`
  (`[load]` extra). `chip_item` walks an acquisition's geocoded GeoTIFF one window
  at a time via GDAL's `/vsicurl/` driver (only each tile's bytes stream over HTTP
  range requests — no full download, memory bounded to one chip) and writes full
  `chip_size` × `chip_size` tiles as GeoTIFF or `.npy`; `write_chips` chips a whole
  search into a dataset + manifest (`.jsonl` — one `ChipRecord` per line — or a
  `.geojson` `FeatureCollection` of chip footprints). Every record carries the
  chip's geographic bbox, CRS, transform, grid position and source pixel window
  plus the acquisition's datetime, place, platform, polarization, incidence angle
  and resolution, stamped with the CC-BY attribution. Fixed size is a promise
  (partial edge tiles dropped), `stride` overlaps tiles, and `min_valid` drops
  mostly-nodata corners. No model is called — pure raster iteration + manifest
  logic, mirroring `umbra_py.load` — so it is fully offline-testable with a real
  on-disk GeoTIFF. The `umbra chips` CLI mirrors `umbra change`'s search-vs-URLs
  interface plus `--local`/`--index-db`.
- **`umbra describe`: VLM scene description (first C2 piece).** Added
  `src/umbra_py/describe.py` (`[ai]` + `[viz]` extras) and the
  `constants.AI_PROVENANCE` note. `umbra describe <item-url>` renders the item's
  quicklook, sends that PNG plus the `UmbraItem.to_llm_context()` card to a
  configured vision model (Anthropic or any OpenAI-compatible endpoint,
  user-supplied key, `requests` only), and returns a validated
  `SceneDescription` — `{summary, observed_features[], confidence, caveats[]}`.
  The model *only* interprets: the picture and metadata are produced
  deterministically, the reply passes the `parse_description` boundary, and every
  description is stamped with the CC-BY attribution and the AI-provenance note, so
  a reading of radar is never mistaken for a measurement. Like `planner.py`, the
  model call is an injectable `Describer` and the render an injectable
  `Renderer`, so the whole feature is offline-testable with no network and no
  model.
- **Semantic task-name aliasing (last open C1 piece).** Added
  `src/umbra_py/semantic.py` (`[ai]` extra): `SemanticTaskIndex` embeds the
  catalog index's distinct task names once (`umbra semantic build`) into a
  schema-versioned SQLite file beside `catalog.db`, and `umbra semantic search`
  ranks them against a query by cosine similarity, printing the `umbra search
  --area …` command for the best match to audit before `--run`. The only model
  call is the injectable `Embedder` (default: an OpenAI-compatible `/embeddings`
  endpoint via `requests`); storage, cosine and ranking are stdlib-only (no
  `numpy`, no `sqlite-vec`), so it is fully offline-testable with a stand-in
  embedder. Resolves `area="grain storage north dakota"` → "Beet Piler - ND",
  which plain string similarity can't and shouldn't fake. Chose a sidecar
  `catalog.semantic.db` over embedding vectors *inside* `catalog.db` so the
  deterministic index and its published snapshot never carry model-derived data a
  core install can't use.
- **Bootstrap local search from the published catalog snapshot.** Added
  `CatalogIndex.from_release()` / `umbra index fetch` (downloads the rolling
  `catalog-index` release's `catalog.db` via the resume-safe `download_url`),
  plus a `built_at` build stamp surfaced as a staleness note in
  `umbra index info`. Surfaced in
  [PR #26](https://github.com/reesehammer/umbra-py/pull/26).
