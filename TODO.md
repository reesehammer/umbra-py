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

## Finish C1 natural-language search (fuzzy task matching + `umbra ask`)

- **Surfaced in:** the relative-date-bounds PR (`AI_INTEGRATION_IDEAS.md` C1 —
  deterministic first step now shipped in `src/umbra_py/dates.py`).
- **Code:** `src/umbra_py/catalog.py` / `index.py` (`area` matching),
  `pyproject.toml` (a future `[ai]` extra).

The relative-date resolver is done; the other two C1 pieces named in the doc are
still open:

- **Fuzzy / alias task matching.** `area=` is a literal case-insensitive
  substring today (live and index paths agree). Add fuzzy/alias matching so
  `area="grain storage north dakota"` can reach the "Beet Piler - ND" task —
  first with plain string similarity, then optionally an embedding index over
  task names/descriptions (sqlite-vec inside `catalog.db`, `[ai]` extra). Keep
  the deterministic substring path as the default so nothing regresses.
- **`umbra ask "…"` (`[ai]` extra).** Hand the user's sentence plus the A2
  `llm_context()` document to a configured model and return the *deterministic
  command it maps to*, shown before running. The LLM plans; the library
  executes; the user audits. This is the home for range keywords with
  hemisphere-dependent meaning (`"last winter"`) that the deterministic
  `parse_date_bound` intentionally rejects.

---

## Done

- **Bootstrap local search from the published catalog snapshot.** Added
  `CatalogIndex.from_release()` / `umbra index fetch` (downloads the rolling
  `catalog-index` release's `catalog.db` via the resume-safe `download_url`),
  plus a `built_at` build stamp surfaced as a staleness note in
  `umbra index info`. Surfaced in
  [PR #26](https://github.com/reesehammer/umbra-py/pull/26).
