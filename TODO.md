# Outstanding TODOs

This file tracks follow-up items that were intentionally scoped out of merged
PRs. Each entry should link to the PR that surfaced it, point at the code
involved, and describe the smallest change that closes it out.

When you finish one, delete the entry (or move it under a short "Done" log at
the bottom if the history is useful).

---

## Asset hrefs are empty in current Umbra STAC items

- **Surfaced in:** [PR #5](https://github.com/theminiverse/umbra-py/pull/5) (smoke-testing the SAR image overlay against live data)
- **Code:** `src/umbra_py/models.py` (`UmbraItem.asset_href`)

Current Umbra STAC items publish every asset entry with `"href": ""`:

```json
"assets": {
  "2025-04-02-18-21-46_UMBRA-09_MM.tif": {
    "href": "",
    "type": "application/octet-stream",
    "title": "RAW_COLLECT",
    "roles": ["data"]
  }
}
```

The asset *key* is the actual filename, but the `href` that `asset_href()`
returns is the empty string. Older items (see `tests/data/sample_item.json`)
had populated hrefs like `..._GEC.tif`, which is why the offline tests still
pass and this didn't surface until live testing against 2024+ data.

**Impact:**
- `download_url` / `download_asset` / `download_item` — `requests` raises
  `MissingSchema` when given an empty URL.
- `image_overlay` (PR #5) — rasterio raises
  `CPLE_IllegalArgError: Missing url parameter`.
- `umbra info` still prints a usable summary, but its asset list is
  technically misleading since nothing is reachable.

**Fix sketch:** in `UmbraItem.asset_href`, when the stored `href` is
empty/missing, derive the URL by joining the asset *key* (the filename) to
the item's own `href`. A `HEAD` against the obvious path (the STAC
directory) returns 404 — the binary assets likely live under a parallel
prefix (`sar-data/...` instead of `stac/...`), so this fix needs a short
spike to confirm the rewrite rule against Umbra's docs or a real working
URL before shipping.

**Acceptance:**
- A network-marked test downloads the first few bytes of a GEC asset
  from a recent (2025) item.
- `umbra download <recent-item-url> --asset GEC` actually writes the file.
- `footprint_map([recent_item], imagery=True)` renders the SAR image
  (verifies the URL resolution flows through to rasterio as well).

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
