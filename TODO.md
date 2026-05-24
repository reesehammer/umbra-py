# Outstanding TODOs

This file tracks follow-up items that were intentionally scoped out of merged
PRs. Each entry should link to the PR that surfaced it, point at the code
involved, and describe the smallest change that closes it out.

When you finish one, delete the entry (or move it under a short "Done" log at
the bottom if the history is useful).

---

## Walk the `sar-data/` STAC v2 tree for richer data coverage

- **Surfaced in:** [PR #5](https://github.com/theminiverse/umbra-py/pull/5) (investigating why only 1/60 items returned downloadable imagery)
- **Code:** `src/umbra_py/catalog.py` (`UmbraCatalog.search`, `_walk`)

`UmbraCatalog.search` walks the v1 STAC tree under `s3://.../stac/`, which
contains thousands of items but is mostly metadata-only -- almost every
item has `href=""` on every asset, and references `umbra:task_id` values
that don't correspond to any directory in the public bucket. A real-world
search of 60 GEC items in 2025 returned exactly **one** with downloadable
data.

The actual published imagery lives under `sar-data/tasks/<NamedTask>/<task-uuid>/<acquisition>/`
(e.g. `sar-data/tasks/AIR/15ccc0d8-.../2025-12-06-07-52-28_UMBRA-10/`),
each with its own `*.stac.v2.json` sidecar. These items are NOT reachable
from the v1 STAC catalog -- they have different STAC ids and aren't
cross-linked.

PR #5 added `available_task_ids()` + `search(data_available_only=True)` as
a stopgap: it prunes v1 STAC items to those whose `task_id` happens to
match a top-level UUID bucket directory (currently 4 of ~80 task dirs).
This is correct but covers a tiny fraction of what's actually published.

**Fix sketch:** add a second catalog walker that enumerates
`sar-data/tasks/<task>/*/*.stac.v2.json` via S3 listings, parses each
sidecar into an `UmbraItem`, and either (a) returns these alongside v1
results, or (b) replaces the v1 walk entirely once we're confident
coverage is complete. The v2 sidecars have populated asset names but
their `href` values point at a *private* bucket -- we'd still use the
existing key-based URL derivation for public download paths.

**Acceptance:**
- A search across a known date range returns at least 10x more items with
  downloadable data than the current v1 walk.
- Items returned from the new walker pass `image_overlay` end to end.
- Bucket-listing performance is acceptable (paginated, with date pruning
  where possible).

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
