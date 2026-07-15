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

## Done

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
