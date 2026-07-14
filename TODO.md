# Outstanding TODOs

This file tracks follow-up items that were intentionally scoped out of merged
PRs. Each entry should link to the PR that surfaced it, point at the code
involved, and describe the smallest change that closes it out.

When you finish one, delete the entry (or move it under a short "Done" log at
the bottom if the history is useful).

---

## Bootstrap local search from the published catalog snapshot

- **Surfaced in:** [PR #26](https://github.com/reesehammer/umbra-py/pull/26) (stac-geoparquet export)
- **Code:** `src/umbra_py/index.py` (`CatalogIndex`), `src/umbra_py/cli.py` (`_search_source`)

PR #26 publishes `umbra-open-data.parquet` + `catalog.db` on the rolling
`catalog-index` release, but a fresh install still has to crawl (or manually
download the `.db`) before `umbra search --local` works. The consume side is
missing: fetch the published snapshot on demand so whole-catalog search is
instant out of the box.

**Fix sketch:** add a fetch step (e.g. `umbra index fetch`, or a
`CatalogIndex.from_release()` classmethod) that downloads the released
`catalog.db` to `default_index_path()` via the existing resume-safe
`download_url`, then everything else (`--local` search) just works. Include a
staleness note in `umbra index info` (snapshot build date vs today).

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
