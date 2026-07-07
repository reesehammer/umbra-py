# Outstanding TODOs

This file tracks follow-up items that were intentionally scoped out of merged
PRs. Each entry should link to the PR that surfaced it, point at the code
involved, and describe the smallest change that closes it out.

When you finish one, delete the entry (or move it under a short "Done" log at
the bottom if the history is useful).

---

## CCD: sensor-model coregistration for arbitrary repeat-pass SICD pairs

- **Surfaced in:** PR #23 (coherent change detection), during live QA.
- **Code:** `src/umbra_py/ccd.py` (`_register_shift` / `coherence` / `coherent_change`).

`umbra ccd` coregisters a pair with a single global sub-pixel **translation**.
That only aligns images already on a shared pixel grid (a coherent collect on
near-identical geometry). Two independently-focused Umbra SICDs of one site are
each formed on their own slant plane, so the mapping between them has
scale/rotation/higher-order terms a translation can't capture: full-resolution
amplitude correlation between such a pair measured ~0.02 (coarse, 6x-decimated,
~0.49), and coherence pins at the noise floor everywhere. `coherent_change` now
*warns* on a noise-floor result, but the pair still can't be processed.

No near-simultaneous same-platform SICD pairs (< 30 min apart) exist in the open
catalog, so a genuinely coherent, grid-sharing pair may not be available in the
open data at all — verify before investing.

**Fix sketch:** resample the secondary onto the reference's grid via the SICD
sensor model (+ a DEM) before the coherence estimate — i.e. proper InSAR-grade
coregistration (see `sarpy`'s projection/ortho helpers). Large effort; gate it
on finding at least one open-data pair that actually coheres.

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
