# Outstanding TODOs

This file tracks follow-up items that were intentionally scoped out of merged
PRs. Each entry should link to the PR that surfaced it, point at the code
involved, and describe the smallest change that closes it out.

When you finish one, delete the entry (or move it under a short "Done" log at
the bottom if the history is useful).

---

## README "See where your search landed" uses removed `data_available_only` kwarg

- **Surfaced in:** the analysis-ready `to_xarray` PR (branch
  `claude/kind-einstein-6bz3gj`).
- **Code:** `README.md` "See where your search landed" snippet
  (`UmbraCatalog().search(..., data_available_only=True)`).

`search()` dropped the `data_available_only` flag when the catalog moved to
the v2 walker (see the **Removed** section of `CHANGELOG.md`), but the README
example still passes it, so the snippet now raises `TypeError`. The v2 walker
only returns published items, so the flag is unnecessary.

**Fix sketch:** delete `, data_available_only=True` (and the explanatory
comment above it) from that snippet, matching the corrected
`to_xarray` examples. Scoped out here to keep this PR to the load feature.

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
