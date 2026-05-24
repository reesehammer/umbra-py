# TODO / Known issues

Working backlog of things worth fixing in `umbra-py`. Add new items at the
top of the relevant section.

## Bugs

### Asset hrefs are empty in current Umbra STAC items

**Symptom.** `UmbraItem.asset_href("GEC")` returns `""` (and downstream:
`download_item` writes nothing; `image_overlay` fails inside rasterio
with `CPLE_IllegalArgError: Missing url parameter`) on most items
currently published to Umbra's open catalog.

**Discovered.** 2026-05-24, while smoke-testing the SAR image overlay
feature (PR #5) against live data for `2024-02-08`.

**Cause.** Umbra has been publishing STAC items where every asset
entry's `href` is the empty string, e.g.:

```json
"assets": {
  "2025-04-02-18-21-46_UMBRA-09_MM.tif": {
    "href": "",
    "type": "application/octet-stream",
    "title": "RAW_COLLECT",
    "roles": ["data"]
  },
  ...
}
```

The asset **key** is the actual filename, but the `href` that
`asset_href()` reads is blank. Older items (see
`tests/data/sample_item.json`) had populated hrefs like
`..._GEC.tif`, which is why the offline tests pass and this didn't
surface until live testing.

**Scope of impact.**
- `download_url` / `download_asset` / `download_item` — pass an empty
  URL to `requests`, which raises `MissingSchema`.
- `image_overlay` — passes `"/vsicurl/"` (just the prefix) to rasterio.
- `umbra info <item-url>` — summary works, but the printed asset list
  is technically misleading since nothing is reachable.

**Possible fix.** In `models.UmbraItem.asset_href`, when the stored
`href` is empty/missing, derive the URL by treating the asset *key* as
a relative path and resolving it against the item's own URL
(`urljoin(self.href, key)`). Needs verification that this actually
points at the data on S3 — initial `HEAD` against the STAC directory
returned 404, so the data probably lives under a parallel path
(`sar-data/...` instead of `stac/...`) and we'll need to ask Umbra or
read their documentation to find the right rewrite rule.

**Acceptance criteria.**
- A live network test that downloads at least the first few bytes of a
  GEC asset from a recent item (e.g. anything from 2025).
- `umbra download <recent-item-url> --asset GEC` actually writes the
  file.
- `footprint_map([recent_item], imagery=True)` renders the SAR image.

## Nice to have

_(empty)_
