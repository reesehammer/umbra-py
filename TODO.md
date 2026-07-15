# Outstanding TODOs

This file tracks follow-up items that were intentionally scoped out of merged
PRs. Each entry should link to the PR that surfaced it, point at the code
involved, and describe the smallest change that closes it out.

When you finish one, delete the entry (or move it under a short "Done" log at
the bottom if the history is useful).

---

## Asset classifier: `"tif"` substring check can never match uppercased name

- **Surfaced in:** [PR #2](https://github.com/theminiverse/umbra-py/pull/2) ("Notes for reviewers")
- **Origin PR:** [PR #1](https://github.com/theminiverse/umbra-py/pull/1)
- **Code:** `src/umbra_py/models.py:27-29` (`_classify_asset`)

`_classify_asset` builds `name = f"{key} {asset.get('href', '')}".upper()` and
then checks `"tif" in name`. Because `name` is uppercased, the lowercase
substring `"tif"` can never match — the branch is dead code.

In practice the parallel `"geotiff" in media` check (against the lowercased
media type) catches Umbra's COGs, so no regression has been observed. But an
item that only declares `image/tiff` (no `geotiff` substring) would slip
through and never be classified as a GeoTIFF asset.

**Fix sketch:** either compare against the lowercased name
(`".tif" in name.lower()`) or use `"TIF" in name` to match the existing upper-cased
string. Add a regression test in `tests/` covering an asset whose media type is
plain `image/tiff` and whose href ends in `.tif`.

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
`GET`+`POST /search` with bbox, datetime, ids and token pagination). Open
follow-ons:

- **Query extensions.** `/search` currently supports the STAC core filters; the
  index also filters by free-text `area` (task/site substring) and
  `product_types`, which aren't yet exposed over the API. Wiring the STAC
  *query*/*filter* extension (or simple extra query params) would let clients
  use them. Geometry `intersects` needs more than the stored footprint bbox.
- **Single-item lookup cost.** `/collections/{id}/items/{item_id}` filters by id
  in the serve layer (a scan of the ordered result set). At catalog scale that's
  fine; if the index grows, add a `CatalogIndex.get(item_id)` keyed lookup and
  call it from `get_item`.
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

- **C5 archive embeddings (exploratory).** Chips are the prerequisite for
  precomputing per-acquisition image embeddings and a `search_similar(item)` /
  text-to-scene capability (`AI_INTEGRATION_IDEAS.md` C5). Publish the embedding
  table with the nightly index so no user recomputes it.
- **Publish the chip manifest as stac-geoparquet.** The manifest is JSONL /
  GeoJSON today; a `.parquet` option (reusing the `[export]` extra's
  stac-geoparquet plumbing) would let DuckDB / geopandas query a large chip set
  without loading every line.
- **Chip the complex products.** The chipper reads amplitude rasters (GEC/CSI);
  chipping SICD/CPHD would need the slant-plane handling that `convert.py`
  begins — related to the still-open SICD → geocoded COG gap in `STRATEGY.md` 5.5.

---

## Done

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
