# Outstanding TODOs

This file tracks follow-up items that were intentionally scoped out of merged
PRs. Each entry should link to the PR that surfaced it, point at the code
involved, and describe the smallest change that closes it out.

When you finish one, delete the entry. The record of what shipped lives in
[`CHANGELOG.md`](../CHANGELOG.md) — this file carries only the work that is
still open.

---

## Workflow-CLI drift follow-ons (`tests/test_workflows.py` shipped)

- **Surfaced in:** the publish-workflow fix (`STRATEGY.md` §8, "getting the
  published artifacts to actually exist").
- **Code:** `tests/test_workflows.py`, `.github/workflows/publish-index.yml`.

Both of the only two `Publish catalog index` runs died on `umbra tiles --local
--db catalog.db` (the option is `--index-db`), and because the tiling step ran
before the release step the whole crawl went with it, so the `catalog-index`
release was never created. The invocation is fixed, the uploads now sit with the
steps that build them, and `tests/test_workflows.py` parses every `umbra …`
invocation in `.github/workflows/*.yml` against the real Click command tree. Run
3 (dispatched 2026-07-27) then succeeded, so the release and all five of its
artifacts exist. Follow-ons, none a blocker:

- **The check is a parse, not a run.** It catches renamed, dropped and
  misspelled options — the drift that actually happened — but not an option
  whose *meaning* changed, nor a value that is wrong (`--limit 1200` being too
  small, a bad `--out` path). Running the commands would need a bucket crawl and
  credentials, which is why the cheap check is the one that exists. If a
  semantic break ever ships, the place to catch it is the live canary
  (`live-canary.yml`), not here.
- **Only `umbra` invocations are checked.** The workflows also call `gh`,
  `python -c` and `pip` with arguments that can drift (the `python -c` in the
  tiling step imports `umbra_py.pmtiles.save_viewer` and
  `constants.CATALOG_INDEX_PMTILES_URL` by name, so a rename there breaks the
  same run and no test would notice). Extending the scan to `python -c` bodies
  is a small addition — compile them, or import the names — if that ever bites.
- **The scan is textual, so a genuinely dynamic invocation would be missed.**
  Nothing builds an `umbra` command line from a shell variable today; if
  something ever does, the extractor will silently skip it. The self-check
  (`test_the_scan_actually_found_the_published_commands`) pins the publish
  pipeline's commands specifically for this reason, but it is a roster to keep
  current, not a general guarantee.

---

## SessionStart hook follow-ons (`.claude/hooks/session-start.sh` shipped)

- **Surfaced in:** the agent-session-hardening PR (`STRATEGY.md` §8).
- **Code:** `.claude/hooks/session-start.sh`, `.claude/settings.json`.

A `SessionStart` hook now installs umbra-py editable with every extra on a
Claude-Code-on-the-web container (mirroring CI's `test-all-extras` job) so the
linters, type-checker, test suite and `umbra` CLI all work from the first turn;
`.claude/settings.json` also pre-approves the documented dev-loop + read-only
commands. It runs synchronously. Follow-ons that build on it, none a blocker:

- **Switch to async mode if startup latency matters.** The hook has no
  `{"async": true}` line, so a web session waits for the ~10–30 s install before
  the first turn — the safe default (no race where a check runs before its deps
  exist). If maintainers prefer a faster session start, emit
  `{"async": true, "asyncTimeout": 300000}` first and accept that early turns may
  land before the install finishes.
- **Trim the extras for a lighter/faster install.** The hook installs *all*
  extras so nothing import-skips. If a maintainer only ever touches the core, a
  `[dev]`-only install (matching the core CI matrix) is faster; the full set is
  the deliberate default so the coverage-gated suite runs unabridged.
- **`mypy` disagrees between the hook's environment and CI's.** Surfaced by the
  `stack_stats` PR. CI's `type-check` job installs only `[dev]`, so Pillow is
  absent and `[tool.mypy]`'s import-ignore covers it; the hook installs every
  extra, so Pillow's stubs *are* checked and `viz/composites.py`'s `Image.ADAPTIVE`
  reads as `[attr-defined]`. CI is green and the code is correct (`ADAPTIVE` is a
  real Pillow constant the stubs place elsewhere), but every remote agent session
  starts with one failing `mypy` line. Smallest fix: `cast` the constant or
  narrow the ignore at that call site, so the documented dev loop is clean in
  both environments.

---

## Index demo-denormalization follow-ons (`umbra index bake` shipped)

- **Surfaced in:** the baked place-label PR (the G2/G6 demo-denormalization
  gaps).
- **Code:** `src/umbra_py/index.py` (`bake_places`, `bake_thumbnails`, the
  `place` / `thumbnail` columns + the migrations), `umbra index bake` /
  `bake-thumbnails` in `cli/indexes.py`, `UmbraItem.place` in `models.py`.

`umbra index bake` reverse-geocodes each acquisition's footprint centroid once at
build time into an additive `place` column, and `umbra index bake-thumbnails`
caches a small PNG per acquisition, so every `--local` search and every render
surface reads both from local bytes. Both are baked into the published weekly
snapshot (`catalog.db` and the `catalog.thumbs.db` sidecar). Follow-ons that
build on that, none a blocker:

- **A precomputed centroid column.** The centroid is derived from the stored bbox
  today (cheap), so a `centroid` column is only worth adding if a consumer needs
  to query/sort on it in SQL rather than compute it per row.
- **The published thumbnail sidecar has no total cap.** Each weekly run adds up
  to `--limit` (1500) previews and never drops any, so `catalog.thumbs.db` grows
  monotonically toward whole-catalog coverage at ~10–20 KB per 128 px scene.
  That is the intended trajectory (and the download is opt-in), but if the asset
  gets unwieldy the smallest fix is an export-side bound —
  `export_thumbnails(limit=…, newest_first=True)`, mirroring the bake — so the
  published file keeps the most recent N rather than everything ever baked.
- **`newest_first` is opt-in, not the default.** `bake_thumbnails` still orders
  by `href` unless asked, to keep an existing caller's batching stable. If no
  caller depends on that order, making newest-first the default would be one
  fewer flag to remember on the path where a cap actually matters.
- ~~**The published thumbnails are 128 px; `bake-thumbnails` defaults to 256.**~~
  **shipped** — schema v4 records the asset and the size beside every baked
  preview (`items.thumbnail_asset` / `items.thumbnail_size`, and the matching
  sidecar columns), so `import_thumbnails` keeps a local bake unless the incoming
  one is a *larger* preview of the *same* product instead of keeping whichever
  arrived first. Where either side is unrecorded the two are not comparable and
  the local bake stays, which is what makes the change invisible until the
  published sidecar is republished with the record. The same record is what let
  the C2 entry below stop inferring what a preview must have been. What is still
  open, and smaller:
  - **The published sidecar has no record until the next weekly run.** Until
    `publish-index.yml` re-exports, every fetched preview reads as "unknown", so
    a merge behaves exactly as it did before and `umbra describe --preview` still
    falls back to assuming `GEC`. Nothing to do but wait for a run — noted
    because it is the same silent-until-rebuild lag the PMTiles entry below has.
  - **The bake's *stretch* is still assumed, not recorded.** `bake_thumbnails`
    has no `db` parameter — every preview is the decibel one — so `--no-db` is
    refused without a lookup. If a linear bake is ever wanted, it is a third
    column and a parameter, not a reinterpretation of the two that exist.

---

## Static GitHub Pages showcase follow-ons (`umbra showcase` shipped)

- **Surfaced in:** the GitHub Pages showcase PR (`STRATEGY.md` §8 demo/hosting).
- **Code:** `src/umbra_py/showcase.py` (`build_showcase` / `assemble_showcase`),
  `umbra showcase` in `cli/explore.py`, the `Build catalog showcase` step in
  `.github/workflows/docs.yml`.

`umbra showcase` composes the whole-catalog PMTiles map (`umbra tiles`), the
interactive explorer (`umbra demo`) and a self-contained landing page into one
static, hostable directory; the `docs.yml` Pages job publishes it to
`/showcase/` beside the docs (non-blocking, main-only). Follow-ons that build on
it, none a blocker:

- **Enable Pages for the repo (maintainer).** The `docs.yml` deploy job (and so
  the showcase publish) is skipped until Settings → Pages → Source is set to
  "GitHub Actions". Until then the showcase builds in CI but isn't served.
- **The hosted page shows one featured view at a time.** `--featured-view
  {change,timescan,swipe}` picks which marquee gallery is rendered and `docs.yml`
  deploys the `change` one; showing more than one view on the same page would
  need the landing page's featured section repeated per view rather than chosen.
- **Auto-stamp the freshness date from the index.** The CI step passes the run
  date to `--updated`; reading the fetched index's `built_at` (as `umbra index
  info` does) would show the *snapshot's* age rather than the build's.

---

## Whole-catalog PMTiles tiling follow-ons (`umbra tiles` shipped)

- **Surfaced in:** the `umbra tiles` PR (the demo's full-acquisition-set tiling
  gap).
- **Code:** `src/umbra_py/pmtiles.py`, `umbra tiles` in `cli/explore.py`.

`umbra tiles` (a stdlib-only PMTiles v3 writer over acquisition centroids *and*
footprint polygons, each feature carrying its COG reference plus the
polarization / asset lists, + a MapLibre GL viewer — no extra, no tippecanoe) is
shipped, and `umbra demo --pmtiles` reads it, so the whole-archive explorer is a
superset of the embedded-slice one. Follow-ons, none a blocker:

- **Leaf directories for very large catalogs.** The writer emits a single root
  directory, which is spec-valid and ample for the current catalog (thousands of
  tiles). If the tile count ever grows past a comfortable root-directory size,
  add leaf-directory splitting (the PMTiles spec's mechanism) so readers still
  fetch a small root first.
- **The published archive only gains new properties on its next rebuild.** The
  COG references and the `pol` / `assets` fields reach `catalog.pmtiles` on the
  next `publish-index.yml` run, so until then the hosted showcase shows no "Get
  SAR image" button and its polarization chips filter nothing out (every feature
  lacks the key, and the "never hidden by a facet it has no value for" rule keeps
  them all visible — the honest failure mode, but a silent one).
- **The COG overlay is a bbox-stretched quicklook, not a reprojection.** The same
  approximation the embedded-slice explorer makes; worth revisiting only if the
  placement error becomes visible at the zooms people actually use.
- **The facet chips are the only place the two explorer modes still differ.** The
  slice app derives its chips from the slice it holds; the whole-archive app
  offers the closed `POLARIZATIONS` set, so a chip can name a polarization the
  archive has none of. Deriving it instead would need a facet summary in the
  archive metadata — worth doing only if the same question comes up for another
  field.

---

## SICD DEM orthorectification follow-ons (`umbra convert --dem` shipped)

- **Surfaced in:** the DEM terrain-orthorectification PR (`STRATEGY.md` 5.5).
- **Code:** `src/umbra_py/convert.py` (`_refine_gcps_with_dem`,
  `_dem_height_sampler`, `_build_gcps_dem`, the RTC models, `conversion_tags`),
  `umbra convert --dem` in `cli/process.py`.

`umbra convert --dem PATH|auto` / `--geoid PATH|auto` / `--rtc --rtc-model
{cosine,area,gamma,facet}` / `--calibrate {sigma0,beta0,gamma0,rcs}`
terrain-orthorectifies, radiometrically flattens and calibrates a SICD, and
stamps what actually ran into `UMBRA_*` GeoTIFF tags. Follow-ons, none a
blocker:

- **MultiRTC interop.** Interop with
  [MultiRTC](https://github.com/MultiSAR/MultiRTC) is a heavier,
  research-oriented job and remains deferred.

---

## Noise-floor subtraction follow-ons (`umbra convert --subtract-noise` shipped)

- **Surfaced in:** the noise-subtraction PR (`STRATEGY.md` 5.5, which the DEM
  entry above named).
- **Code:** `src/umbra_py/convert.py` (`_noise_level_type`,
  `_noise_coefficients`, `_noise_power`, `_estimate_noise_power`,
  `_estimate_noise_profile`, `_check_percentile`, `_detected_power`,
  `_subtract_noise`, `_denoise_amplitude`, `sicd_noise_level`,
  `compare_noise_models`, `NoiseModelComparison`, `NoiseModelAgreement`,
  `_floor_agreement`, `INFERRED_NOISE_MODELS`, `NOISE_MODELS`,
  `NOISE_ESTIMATE_PERCENTILE`, `NOISE_PROFILE_DEGREE`, `NOISE_MARGIN_WARN_DB`,
  `NoiseSubtraction`, `_margin_db`, `_NOISE_PROVENANCE`, the `NOISE_SUBTRACTION` /
  `NOISE_FLOOR_DB` / `NOISE_FLOORED_FRACTION` / `NOISE_FLOOR_MARGIN_DB` /
  `NOISE_FLOOR_SPREAD_DB` tags),
  `src/umbra_py/cli/process.py` (`_echo_noise_report`, `_echo_chip_noise_report`),
  `src/umbra_py/load.py`
  (`MEASUREMENT_PROVENANCE_KEYS`, `_STEP_NOT_RUN`), `src/umbra_py/chips.py`
  (`SicdConversion.noise_subtract` / `.noise_model`,
  `ChipRecord.noise_subtraction` / `.noise_floored_fraction` /
  `.noise_floor_margin_db`, `NoiseSummary`, `_summarise_noise`,
  `ChipDataset.noise`, `_reported_number`),
  `umbra convert --subtract-noise --noise-model` /
  `umbra chips --subtract-noise --noise-model` in `cli/process.py`.

The receiver's own thermal-noise floor is subtracted from detected power before
any multiplicative correction scales it. Where that floor comes from is
`--noise-model`: `measured` reads the product's own
`Radiometric.NoiseLevel.NoisePoly` (only an `ABSOLUTE` level is accepted),
`estimated` infers one constant per scene from the 5th percentile of the image's
own power — which is what works on Umbra's open products, since they generally
carry no `Radiometric` block at all — and `estimated-range` infers one per range
line and fits those against range, so an inferred floor follows the swath instead
of leaving the constant model's gradient behind (its swing is reported in
`UMBRA_NOISE_FLOOR_SPREAD_DB`). The three record themselves as different things
(`UMBRA_NOISE_SUBTRACTION` of `"absolute"` / `"estimated"` / `"estimated-range"`,
plus `UMBRA_NOISE_FLOOR_DB` for the inferred ones) and `to_stack` refuses a series
that mixes any two of them. Each subtraction also
reports what it did to the scene it ran on — `UMBRA_NOISE_FLOORED_FRACTION` and,
for the estimate, `UMBRA_NOISE_FLOOR_MARGIN_DB` — which `umbra convert` prints
and turns into an advisory below `NOISE_MARGIN_WARN_DB`, and which a chip run
carries into every manifest record and rolls up across the batch
(`ChipDataset.noise`). Follow-ons, none a blocker:

- **A `RELATIVE` noise level is refused rather than used.** It carries real
  information — the *shape* of the floor across the swath — which could flatten
  the noise-induced gradient without claiming an absolute level. That is a
  different product (a relative correction, not a subtraction), so it wants its
  own name and its own provenance value rather than a quiet reinterpretation of
  this flag.
- **The estimator's percentile is fixed at the library level.**
  `NOISE_ESTIMATE_PERCENTILE` (5.0) is a module constant that
  `_estimate_noise_power` takes as a default and nothing above it overrides — not
  `sicd_to_geocoded_cog`, not the CLI. That is deliberate: a knob whose right
  value depends on how much dark ground a scene happens to contain is a knob
  most callers would turn wrongly, and the honest fix for a scene where 5% is
  wrong is a *measured* floor, not a tuned guess. If a class of scenes turns out
  to need a different tail (very high incidence, all-water), thread it through
  as `noise_percentile=` and record it beside `UMBRA_NOISE_FLOOR_DB` so the
  number stays reproducible.
- **The range profile is fitted along rows only.** `"estimated-range"` fits the
  low tail against the row coordinate, which is range in a SICD, and takes the
  floor as constant along azimuth. That is the right first axis — the antenna
  elevation pattern and range spreading are what make a floor vary — but a long
  collect can also drift along azimuth (receiver temperature, azimuth beam
  shape). A 2-D surface fit would cover both; it needs enough dark ground in
  *both* directions to be better rather than merely more flexible, so it wants
  evidence from a real scene before it is written.
- **The fit's degree and trim are library-level constants.**
  `NOISE_PROFILE_DEGREE` (2), `_NOISE_PROFILE_TRIM_DB` (3.0) and
  `_NOISE_PROFILE_MIN_SAMPLES` (16) are not threaded through
  `sicd_to_geocoded_cog` or the CLI, for the same reason
  `NOISE_ESTIMATE_PERCENTILE` is not: a knob whose right value depends on how a
  particular scene's dark ground is distributed is one most callers would turn
  wrongly, and the honest fix where the default is wrong is a *measured* floor.
  If a class of scenes needs a different curve, thread it through and record it
  beside `UMBRA_NOISE_FLOOR_SPREAD_DB` so the number stays reproducible.
- **The fitted level carries the same conservative bias as the constant one.** A
  percentile of a speckled noise-only population sits below that population's
  mean, so both inferred models read low; the profile fixes the *gradient*, not
  the offset. Correcting the offset means assuming a speckle distribution (a
  known factor for N-look intensity), which is a claim about the product's
  processing rather than about its pixels — worth doing only alongside a
  `Grid.ImpRespBW`/multilook read that says how many looks a scene actually has.
  The bias is now *measurable* rather than only argued (`compare_noise_models`
  reports it as `bias_db`), which is what would make such a correction checkable
  — but the measurement is on a synthetic single-look population, so it confirms
  the arithmetic rather than supplying the factor a real product needs.
- ~~**Nothing compares the fitted floor against a measured one.**~~ **shipped** —
  `compare_noise_models` / `umbra convert --noise-check` runs the inferred models
  over a product that *does* declare an `ABSOLUTE` level and differences each
  against its own `NoisePoly`, split into the offset (`bias_db`) and what is left
  after granting it (`shape_error_db`). The synthetic-SICD route is the one that
  shipped — a fixture whose stated floor is the floor its pixels were built from
  — since no Umbra open product carries the metadata. What is still open, and
  smaller:
  - **A real product has never been run through it.** The numbers below are from
    a synthetic scene, which validates the arithmetic and the claims but not the
    estimator against a real receiver's roll-off, real speckle statistics or a
    real multilook. A Canopy product (or any SICD with an `ABSOLUTE` `NoisePoly`)
    run through `--noise-check` would say so; that is a `network`-marked test
    gated on a token, like the Canopy backend's.
  - **The comparison found the estimate compressing over dark ground.** Where
    backscatter sinks toward the floor, the fitted profile reads the swing ~30%
    flat — the low tail stops being a separate population at the far edge before
    it does at the near edge. The subtraction stays conservative, so this is a
    caveat on quoting `UMBRA_NOISE_FLOOR_SPREAD_DB`, not a correction to make.
    Reporting it *per scene* would mean a second statistic that says how
    separated the two populations were per range line — the margin diagnostic
    is that number for the scene as a whole, so the natural form is a per-line
    margin's minimum. Worth doing if the spread starts being used as a
    measurement rather than as evidence that the constant model was missing
    something.
  - **A constant estimate's bias on a varying floor is now visible but not
    acted on.** `--noise-check` shows `"estimated"` reading low by more than
    speckle alone accounts for when the floor ramps, because a pooled percentile
    lands near the near-range end. Nothing warns about it, deliberately: the fix
    is `estimated-range`, which already exists, and a warning that says "use the
    other model" on a scene where the user chose this one is noise.
  - **Nothing sweeps the percentile.** `compare_noise_models(percentile=…)` is
    the one surface that exposes it, so the obvious next question — is 5.0 the
    right tail? — is now answerable but unanswered. It wants the real product
    above first: the answer on a synthetic exponential population is arithmetic,
    not evidence.
- ~~**The two diagnostics reach `umbra convert`, not `umbra chips`.**~~
  **shipped** — `ChipRecord.noise_floored_fraction` / `.noise_floor_margin_db`
  put both numbers in every manifest record (so a loader filters the affected
  scenes out without opening a raster), and `ChipDataset.noise` /
  `chips.NoiseSummary` rolls them up across the run — counted per acquisition,
  since the diagnostics describe the scene a chip was cut from — for the
  `--json` payload and the two-or-three-line report `umbra chips` prints. The
  decision this entry deferred was what a batch should *say*: a count rather
  than a line per scene ("2 of 22 scene(s) had under 6 dB of margin"), silent
  when the estimate held everywhere, and an advisory rather than a refusal for
  the same reason it is one per scene. Open, and smaller: the roll-up covers the
  *inferred* floors' margin and both models' floored fraction, but nothing
  summarises `UMBRA_NOISE_FLOOR_SPREAD_DB` across a batch — the swing of a fitted
  profile is per scene and does not obviously add up, so it waits for someone who
  wants it.
- **The margin threshold is one constant for every scene type.**
  `NOISE_MARGIN_WARN_DB` (6 dB) is where the advisory fires, and what counts as
  "enough dark ground" plausibly differs between a coastal scene and an inland
  one. It is deliberately not a flag: a knob on a heuristic invites tuning it
  until the warning goes away, which is the opposite of the point. The number is
  reported unconditionally, so anyone who disagrees with the threshold can read
  the margin itself.
- **`umbra chips` exposes the flag but not the check.** There is no chip-side
  equivalent of `sicd_noise_level`, so a batch over acquisitions whose metadata
  varies fails on the first product that cannot support it rather than reporting
  which ones can. Worth doing if a mixed-metadata archive turns up.

---

## Speckle-filtering follow-ons (`umbra convert --speckle-filter` shipped)

- **Surfaced in:** the speckle-filtering PR (`STRATEGY.md` 5.5, which the
  noise-floor entry above named).
- **Code:** `src/umbra_py/convert.py` (`SPECKLE_FILTERS`,
  `SPECKLE_WINDOW_DEFAULT`, `SPECKLE_ENL_GAIN_WARN`, `SpeckleFiltering`,
  `_check_speckle_window`, `_box_sum`, `_local_moments`, `_estimate_enl`,
  `_boxcar_power`, `_lee_power`, `_filter_speckle`, `_speckle_tag_values`, the
  `SPECKLE_FILTER` / `SPECKLE_WINDOW` / `SPECKLE_ENL_BEFORE` / `SPECKLE_ENL_AFTER`
  / `SPECKLE_LOOKS` tags, `_ENL_BLOCK`, `_ENL_BLOCK_WINDOWS`, `_ENL_PERCENTILE`,
  `_ENL_MIN_VALID`), `src/umbra_py/load.py` (`MEASUREMENT_PROVENANCE_KEYS`, the
  `stack_stats` caveat, and — for the cube's own filtering — `_Speckle`,
  `_filter_slab`, `_resolve_speckle`, `_filtered_provenance`, the
  `speckle_filter=` / `speckle_window=` parameters on `to_stack` /
  `stack_to_geotiff`), `src/umbra_py/cli/process.py` (`_echo_speckle_report`,
  `--speckle-filter` / `--speckle-window` on `stack`, `convert` and `chips`),
  `src/umbra_py/chips.py` (`SicdConversion.speckle_filter` / `.speckle_window`,
  `ChipRecord.speckle_filter` / `.speckle_window`).

Speckle — the interference pattern coherent illumination makes on a rough
surface, whose standard deviation equals its mean on a single look — is averaged
down by `--speckle-filter boxcar` (the multilook) or `lee` (averaging only where a
window is no more variable than speckle alone explains), in the power domain, last
in image space. The filter and its window are recorded and refused-on-mix by
`to_stack`; what the filter achieved is recorded as the scene's equivalent number
of looks before and after. Follow-ons, none a blocker:

- **Two filters, not the usual four.** Frost, Kuan and Gamma-MAP are the other
  standard local-statistics filters, and refined Lee is the directional variant
  that keeps a *linear* edge better than plain Lee. They are all the same shape as
  `_lee_power` (a weight computed from local moments), so each is a small addition
  — but each is also another knob with its own failure mode, and the two that
  shipped span the honest range: average everything, or average where averaging is
  defensible. Add one when a real scene shows the existing pair leaving something
  on the table.
- **The window is square in image space, not on the ground.** A SICD's row and
  column ground sample distances differ (range and azimuth), so an N×N pixel
  window is a rectangle on the ground. That is the right domain to filter in — the
  radar's own grid is where speckle is one independent sample per pixel — but it
  means the *resolution* a filtered chip carries is anisotropic, and only the
  pixel count is recorded. Recording the ground extent of the window beside it
  would say so; it needs a consumer that cares which axis it lost.
- **The ENL estimate is a floor, and nothing says how tight a floor.** Structure
  inside a block deflates that block's ENL, so the median block reads low on a
  textured scene: the pair before/after is trustworthy as a *ratio*, and either
  level on its own is conservative. The natural refinement is the spread of the
  per-block distribution (how much the blocks disagreed), which would say whether
  the scene gave a clean read or a mixed one — the same move the noise estimator's
  margin diagnostic made. Worth doing if the ENL starts being quoted as a
  measurement rather than as evidence that the filter worked.
- **`lee`'s looks parameter is read once per scene.** It is the scene's own ENL
  (clamped at single-look), which is right for a product with uniform processing;
  a scene whose looks varied across the swath — a multilook that changed with
  range — would want the same per-line treatment `--noise-model estimated-range`
  gives the noise floor. No Umbra product is known to need it, so it waits for
  evidence rather than being written on the analogy.
- ~~**Nothing filters the *published* GEC products.**~~ **shipped for the cube** —
  `to_stack(speckle_filter=…)` / `umbra stack --speckle-filter` averages each pass
  down on the shared grid, so the same filters reach a GEC read straight from the
  bucket. The provenance question this entry deferred was answered by *not*
  inventing a second vocabulary: a cube whose cells were averaged over an N-cell
  window is an N-window-filtered raster, so it records itself in `umbra convert`'s
  own `speckle_filter` / `speckle_window` keys and every consumer of those keys
  (the `stack_stats` caveat, the written GeoTIFF's tags, the `to_stack` refusal)
  works unchanged. Filtering an already-filtered series is refused rather than
  composed. What is still open, and smaller:
  - ~~**`chip_item` is the other loader, and it does not filter.**~~ **shipped** —
    `umbra chips --speckle-filter` applies to any asset: on `GEC`/`CSI` the tiles
    themselves are averaged, on `SICD` the request is routed into
    `SicdConversion` so the scene is filtered in image space before geocoding.
    The two questions a tile loop raises were answered rather than approximated —
    a half-window **halo** per tile (so a filtered tile is bit-for-bit the region
    of the whole-scene filter, and overlapping tiles agree about shared ground)
    and `lee`'s looks read **once per acquisition** by `_scene_speckle`, from a
    fixed 3×3 grid of sample windows pooled at block level, since it is a
    property of the product rather than of the pixels one tile covers.
    `_filter_speckle` gained the `looks=` parameter that carries it. What is
    still open, and smaller:
    - **The scene's looks is a sample, not a whole-scene read.** Nine 512-pixel
      windows on a fixed grid, because reading the product whole is the thing
      streaming a GEC tile by tile exists to avoid. That is the right trade for a
      number the estimator reads to about a percent from ~1700 pooled blocks, but
      it means an acquisition whose speckle statistics genuinely vary across the
      swath (a multilook that changed with range — the same case the per-line
      noise model exists for) is described by one number. `_sample_offsets` and
      `_SPECKLE_SAMPLE_GRID` are where a denser or per-region read would go; it
      wants a real product that shows the variation first.
    - **The ENL pair describes the sampled windows, not the tiles.** It is a
      per-scene diagnostic by design (every record of one acquisition carries the
      same three), so a run cannot say which *tiles* the filter bought least on —
      only which scenes. Measuring per tile would be a different statistic and a
      noisier one (a 512-pixel read of a ratio of noisy estimates); worth doing
      only if someone wants to select chips on it rather than scenes.
    - **`--clip-bbox` narrows the sample too, which is right but undiscussed.**
      The grid spans the *chipping extent*, so a clipped run reads its looks from
      the area of interest rather than the collect. That is the correct scope for
      the tiles being cut, but it means the same acquisition clipped two ways can
      report two ENLs. Both are honest reads of what was chipped; recording the
      extent beside the number would say so.
  - **A filtered cube reports no ENL.** `_filter_speckle` measures the equivalent
    looks either side of the filter and `_filter_slab` drops the pair: it is a
    per-scene diagnostic, and a lazy cube's slabs are read inside deferred tasks
    with nowhere to report back to. Threading it out for the *eager* path only
    would make a cube's diagnostics depend on how it was read, which is worse
    than not having them; a per-slice coordinate on the cube (like `item_id`) is
    the shape that would work if someone wants the number.
  - ~~**`chunk_size` and the filter are mutually exclusive.**~~ **shipped** —
    `to_stack(speckle_filter=…, chunk_size=N)` composes. `_halo_grid` grows each
    window by half a filter window and `_open_slab(crop=…)` throws the margin
    away after filtering, so a filtered chunked cube is the whole-pass filter's
    own answer; and `_pass_looks` resolves `"lee"`'s speckle parameter once per
    pass — a fixed 3×3 grid of 512-cell sample windows, blocks pooled before the
    percentile, the same shape `chips._scene_speckle` uses per acquisition — as
    one deferred task every window of that pass depends on. `boxcar` needs no
    such parameter and so costs no such read. What it bought is on the server:
    `"windowed": true` and `"speckle_filter"` are now one request on a chunked
    instance. What is still open, and smaller:
    - **A chunked `"lee"` samples the pass; an unchunked one reads it whole.**
      The sample is why: a chunked build is by definition the case where the pass
      does not fit. A pass no wider than one sample window is read whole, so the
      two agree exactly at the sizes where they can — but a genuinely large pass
      gives a looks estimate from ~9 windows rather than from all of it, and
      nothing records which. `_LOOKS_SAMPLE_GRID` / `_LOOKS_SAMPLE_SIZE` are
      where a denser read would go; it wants a real product whose looks vary
      enough for the difference to show.
    - **The halo costs a wider read, and nothing says so.** Each window reads
      `(chunk + window − 1)²` cells to return `chunk²`, which at the window sizes
      this is for (512–2048 cells against a 5-cell filter) is under 1%. It is
      still a cost the `--chunk-size` help does not quantify; a line on the
      non-JSON output would make it visible if anyone ever picks a chunk small
      enough for it to matter.
    - **Equality with the unchunked cube is to one `float32` ulp, not
      bit-for-bit.** The summed-area table reaches a window's total by a
      different order of additions when accumulated over a halo-sized read than
      over a whole pass. The windows themselves are the same cells, so this is
      float rounding rather than a seam — `tests/test_load.py` asserts it at
      `np.finfo("float32").eps`. Nothing to fix; worth knowing before someone
      writes `assert_array_equal`.
  - ~~**`POST /artifacts/stats` cannot ask for it.**~~ **shipped** —
    `"speckle_filter": "boxcar" | "lee"` (plus an optional odd
    `"speckle_window"`) is a request field on the stats endpoint and a parameter
    on the `stack_stats` agent tool, passed straight to
    `to_stack(speckle_filter=…)`. It is in the artifact cache key, as
    `"windowed"` established, because it moves the numbers. It was that option's
    exact complement for a while — filtering needed each pass whole, so it was a
    `400` on the `--stack-chunk-size` instance `"windowed"` requires, and the
    pair was unsatisfiable everywhere — until the halo read above made the two
    compose: a chunked instance honours both, `speckle_filter` has no instance
    condition left, and the request-level pair refusal is gone. What is still
    open, and smaller:
    - ~~**The landing page still doesn't advertise either capability.**~~
      **shipped** — the `stats` link's `umbra:options` reports both, with the
      would-be `400`'s own text as the `reason` on the unsupported one (see the
      datacube section's entry, where the follow-ons live).
    - **`umbra serve` has no `--stack-speckle-*` default.** Every request that
      wants a filter must name it, because a server-set default would be exactly
      the invisible flag the cache key rule exists to prevent. Worth revisiting
      only as a *documented advertised* default (one the landing page states and
      the cache key hashes), not as a policy.
- **The filter runs on the whole window in memory.** `_box_sum`'s summed-area
  table makes the cost independent of the window size but holds a scene-sized
  float64 table, on top of the scene-sized power array `_detected_power` already
  makes. That is the same order the noise estimators already work at, and
  `--clip-bbox` is the answer for a scene too large to hold; a tiled
  implementation (overlapping windows, one strip at a time) is the fix if the
  whole-scene path becomes the common one.

---

## Area-of-interest clipping follow-ons (`--clip-bbox` on convert / chips shipped)

- **Surfaced in:** the conversion-clipping PR (`STRATEGY.md` 5.5, which the
  `umbra chips --asset SICD` entry above named).
- **Code:** `src/umbra_py/convert.py` (`_clip_window`, `_reader_shape`, the
  `origin=` parameters on `_build_gcps` / `_build_gcps_dem` /
  `_calibrate_amplitude` / `_scene_geo_bbox`, `bounds=` on `_warp_gcps_to_cog`,
  `bbox=` on `sicd_to_geocoded_cog`), `src/umbra_py/chips.py`
  (`_clip_pixel_window`, `SicdConversion.bbox`, `bbox=` on `chip_item` /
  `write_chips`), `umbra convert --clip-bbox` / `umbra chips --clip-bbox` in
  `cli/process.py`.

`sicd_to_geocoded_cog(bbox=…)` reads only the image window covering a lon/lat
rectangle, sizes the control points, the calibration polynomials and the
`--dem auto` fetch to it, and crops the output to the request; `chip_item(bbox=…)`
tiles only that window and, for a complex asset, passes it down as the
conversion's clip. Follow-ons, none a blocker:

- **The clip is a rectangle, not a polygon.** The rest of the CLI takes an area
  of interest as a *shape* (`--intersects`), and the shared geography group is
  where that lives. A clipped conversion is a north-up raster, so a polygon could
  only mask it after the warp rather than shrink the read — worth doing when
  someone wants the mask, not for the cost.
- **The window search runs on the flat-earth projection even with `--dem`.**
  Terrain moves a ground point far less than the one-lattice-step padding, so a
  DEM-orthorectified clip is still a superset in practice. If a scene over
  extreme relief ever loses an edge pixel, the fix is to grow the pad by the
  DEM's own height range × `tan(incidence)` rather than to run the refinement
  loop twice.
- **The clip is not in the provenance tags, deliberately.** The output's
  geotransform already states which ground it covers, and `UMBRA_*` records what
  a pixel value *means*; adding it would make a clipped and an unclipped
  conversion of one site disagree on a key for no measurement reason. Revisit
  only if someone needs to tell "clipped to X" from "the scene only covered X".
- **`umbra convert --clip-bbox` takes coordinates, not a place.** `--place` /
  `--area` resolve a name to a rectangle everywhere items are *searched*;
  `convert` operates on a downloaded file and has no search, so the geocoder is
  not wired in. One shared resolver call would do it if the coordinates prove
  annoying in practice.
- **Nothing reports what a clip saved.** The command says what it wrote, not
  that it read 4% of the product to write it. A line on the non-JSON output
  (window pixels vs. scene pixels) would make the flag's value visible at the
  moment someone is deciding whether to use it.

---

## Provenance-consuming follow-ons (`to_stack` refuses mixed conversions)

- **Surfaced in:** the provenance-consumption PR (`STRATEGY.md` 5.5, which the
  DEM entry above named).
- **Code:** `src/umbra_py/load.py` (`MEASUREMENT_PROVENANCE_KEYS`,
  `_source_provenance`, `_shared_provenance`, `_as_geotiff_tags`, the
  `provenance` attr on `to_xarray` / `to_stack` and key on `stack_stats`),
  `src/umbra_py/convert.py` (`conversion_provenance`),
  `src/umbra_py/viz/composites.py` (`_coregister_bands`),
  `src/umbra_py/narrate.py` (`ChangeStats.provenance`, `render_change_png`,
  `build_narrate_messages`).

`to_stack` now reads each source's `UMBRA_*` conversion record and refuses a
series that disagrees on what its pixel values are; the shared record rides the
cube into `stack_stats`, the written GeoTIFFs and `POST /artifacts/stats`. The
same refusal now covers the one *composite*-path caller that quotes numbers —
`render_change_png`, behind `umbra change --narrate` — via the records
`_coregister_bands` collects while the sources are open. Follow-ons, none a
blocker:

- **The refusal has no override.** There is no `check_provenance=False`, matching
  the polarization refusal it mirrors — a mixed selection is not a measurement,
  so the fix is to re-convert or to stack fewer acquisitions. If a legitimate
  "I know, show me anyway" case turns up (comparing a calibrated pass against an
  uncalibrated one *as* the experiment), the escape hatch is one keyword on
  `to_stack` plus a caveat in the summary saying it was used.
- **The picture commands still don't check, now deliberately.** `umbra change` /
  `timescan` / `swipe` take the records `_coregister_bands` returns and ignore
  them — the same tolerance the polarization rule has (a mixed composite is
  confusing to look at; a mixed *number* is wrong), and the records are now there
  for free if that ever stops being the right call. What changed is that the one
  caller on that path which *does* quote decibels, `render_change_png`, refuses.
  A warning on the picture commands is the obvious next step if a mixed composite
  turns out to mislead in practice; it was left out because a warning nobody can
  act on is noise.
- **A refused pair costs the co-registration first.** `render_change_png` reads
  the records from the datasets `_coregister_bands` opens, so a mixed pair is
  caught *after* the overview reads and before the model call — the expensive,
  billable step. Failing before the reads would need a metadata-only pre-open per
  source (a second round of range requests on the passing path, which is the
  common one), so the cheap ordering is the one that ships.
- **Only the radiometric keys are grounds for refusal.** `dem`, `geoid` and
  `projection` are carried when every source agrees and silently dropped when
  they don't: they move a pixel's *position*, not its value, and `to_stack`
  re-grids everything anyway. A series orthorectified against two different DEMs
  therefore stacks. Add them to `MEASUREMENT_PROVENANCE_KEYS` if a DEM mix ever
  produces misregistration worth failing on.
- **The refusal is discovered by hitting it.** Nothing enumerates a selection's
  conversions ahead of the call — no `umbra stack --provenance`-style preflight,
  and `POST /artifacts/stats` reports the mix only as the text of its `400`.
  Cheapest fix if it matters: have the search-side commands report the distinct
  `UMBRA_CALIBRATION` values in a selection the way they report polarizations.
- **A cube's provenance reaches the render manifest only inside `stats`.**
  `umbra stack --json` emits the shared `{output, items_used, parameters}`
  manifest (`docs/schemas/render-manifest.schema.json`), which has no provenance
  field; the record is present only when `--stats` was also asked for. Adding it
  to the manifest would mean a schema revision, so it waits for a consumer.

---

## Register `umbra-mcp` in the MCP registries and Anthropic's directory

- **Surfaced in:** the `umbra-mcp` MCP server PR.
- **Code:** `src/umbra_py/mcp_server.py`, `pyproject.toml` (`[mcp]` extra,
  `umbra-mcp` console script).

The server itself is shipped and runnable (`umbra mcp` / `uvx umbra-mcp`), and
the agent-framework reach trilogy (MCP → LangChain → LlamaIndex) is complete.
What is still open:

- **Registration is a maintainer action.** Listing the server in the public MCP
  registries and Anthropic's directory — the discovery half of the deliverable —
  has not been done.
- **`change_composite` drops its polarization-mixing warning.** Returning that
  warning as structured text alongside the image block would let an agent see
  why a composite is suspect instead of only handing back the picture.

---

## Grow the `umbra serve` STAC API (a hosted instance)

- **Surfaced in:** the `umbra serve` STAC API PR.
- **Code:** `src/umbra_py/serve.py`, `pyproject.toml` (`[serve]` extra).

The read-only STAC API is shipped (landing / conformance / collections / items /
`GET`+`POST /search` with bbox, datetime, geometry `intersects`, ids and token
pagination), renders artifacts on demand (`GET /artifacts/quicklook/{id}.png`,
`GET /artifacts/thumbnail/{id}.png`, `POST /artifacts/change`, `.../timescan`,
`.../swipe`, and the one that is numbers rather than a picture, `POST
/artifacts/stats`) with an async job flow for long renders, and exposes the
index's Umbra-specific filters through the STAC Query extension. Open follow-on:

- **A hosted community instance.** The local-first server has no operational
  cost; a public instance is a policy decision (COG-streaming egress) that would
  make the archive queryable with zero install — pair it with the static demo
  front end `umbra showcase` already builds.

---

## Canopy commercial-archive backend follow-ons (`UmbraCatalog(token=...)` shipped)

- **Surfaced in:** the Canopy backend PR (`STRATEGY.md` 5.1).
- **Code:** `src/umbra_py/catalog.py` (`_search_archive` / `_archive_page`),
  `src/umbra_py/constants.py` (`CANOPY_ARCHIVE_URL`), `umbra search --token`.

The commercial archive is searchable behind the same `search()` interface
(bearer token → STAC API POST search + `rel="next"` pagination, offline-tested
against a mocked API), including keyed `get_item` lookups, the visual commands
and the MCP server. Open follow-ons, none a blocker:

- **Push `product_types` / `area` down as STAC query/filter extensions.** They
  are applied client-side today (exact parity with the open-bucket path). Once
  the concrete Canopy field names are confirmed against the live API, sending
  them as a STAC *query*/*filter* body would let the server pre-filter and cut
  transferred pages. This needs a real token to verify, so it is deliberately
  deferred rather than guessed.
- **Verify request/response shapes against the live Canopy API.** The client is
  built to the STAC API *standard*; confirm the exact search body, collection
  ids, and pagination link shape Canopy emits, and adjust if it deviates. Add a
  `network`-marked smoke test gated on a `UMBRA_CANOPY_TOKEN` secret.

---

## C1 natural-language search follow-ons (all four steps now shipped)

The four C1 steps — relative dates (`dates.py`), the deterministic fuzzy task
matcher (`fuzzy.py`), the model-planned `umbra ask` (`planner.py`), and the
semantic embedding index (`semantic.py`) — are all shipped, as are the LangChain
/ LlamaIndex wrappers and the MCP `search_catalog` semantic mode (see the
CHANGELOG). Optional follow-on that builds on them, not a blocker:

- **Embed task *descriptions*, not just names.** The current index embeds the
  task label; if Umbra publishes per-task descriptions, embedding those too would
  widen recall further.

---

## C2 VLM-in-the-loop follow-ons (`umbra describe` shipped)

- **Surfaced in:** the `umbra describe` PR.
- **Code:** `src/umbra_py/describe.py`, `src/umbra_py/narrate.py` (`[ai]` +
  `[viz]` extras), `constants.AI_PROVENANCE`.

`umbra describe` (scene description) and `umbra change --narrate` (change
narration grounded in a deterministic per-block dB grid) are shipped on the CLI,
MCP, LangChain and LlamaIndex surfaces. Open follow-ons:

- ~~**A `describe` render is a fresh S3 read every call.**~~ **shipped** —
  `umbra describe --preview {render,baked,auto}` / `describe(preview=…,
  previews=…)` reads the quicklook already baked into the local index
  (`CatalogIndex.get_thumbnail`, fed in as a `BakedPreviews` callable so the
  module stays stdlib-only) instead of re-streaming the COG, on the CLI and on
  the MCP surface the LangChain / LlamaIndex tools derive from. It also drops the
  `viz` extra from the path entirely, so a description can run on an `[ai]`-only
  install. The decision this entry left implicit was made explicitly: because a
  preview is smaller (and `GEC`, and dB) it is *evidence*, not an implementation
  detail, so the default stays `render`, the picture is recorded
  (`SceneDescription.image`), a smaller one adds a deterministic caveat, and a
  request the bake cannot answer is refused by `baked_preview_refusal` rather
  than substituted. What is still open, and smaller:
  - ~~**The index records a preview's bytes, not how they were made.**~~
    **shipped** — schema v4's `items.thumbnail_asset` / `.thumbnail_size` (see
    the index entry above) let `baked_preview_refusal` check the request against
    the bake rather than against an assumption about it: a `--asset CSI` bake
    answers a `--asset CSI` reading, a bake of another product is refused *naming
    what it is*, and `SceneDescription.image.asset` reports the product actually
    read. `BakedPreviews` returns a `BakedPreview` record rather than bytes, and
    the lookup moved ahead of the refusal, since which product a preview is of is
    a fact about the scene rather than about the request. A preview with no
    record reads as unknown and keeps the old assumed rule — absence is not a
    claim. What is still open, and smaller:
    - **`--preview auto` now costs one index read on a request it will refuse.**
      It used to skip the lookup entirely for a non-`GEC` asset, because the
      answer was knowable from the request alone; it no longer is. The read is a
      local point query against an indexed column and the alternative is
      refusing bakes that would have answered, so this is the right trade — but
      it is a trade, and it is the reason a `--preview auto` run over many scenes
      touches the index once per scene.
  - **`umbra change --narrate` still renders both passes.** The composite is a
    co-registered difference of two full reads, not a single quicklook, so a
    128 px preview per pass is not the same object at all — there is no cached
    artifact to substitute. The natural equivalent is a cache of the *composite*,
    which is what `umbra serve`'s artifact cache already is; a CLI-side one would
    be a new store rather than a reuse.
  - **Nothing reports what the substitution saved.** As with `--clip-bbox`, the
    command says what it read, not that it skipped an overview stream to read it.
    A line on the non-JSON output would make the flag's value visible where
    someone is deciding whether to use it.

---

## C4/C5 ML dataset follow-ons (`umbra chips` shipped)

- **Surfaced in:** the `umbra chips` PR (`STRATEGY.md` 5.5).
- **Code:** `src/umbra_py/chips.py`, `umbra chips` in `cli/process.py`.

`umbra chips` (fixed-size, georeferenced ML tiles + a `.jsonl` / `.geojson` /
stac-geoparquet manifest, `[load]` extra, no model call) is shipped, including
`--asset SICD`, which geocodes each complex acquisition through the `umbra
convert` pipeline and cuts the identical tiles from the result. Follow-ons that
build on it, not blockers:

- **`CSI` still goes down the amplitude path, which is right but undiscussed.**
  A CSI is a colour sub-aperture *image* — already a display raster — so it is
  streamed like a GEC and none of the conversion flags apply to it. `CPHD` stays
  out entirely: it is phase history rather than a focused image, so there is no
  image grid to chip until something focuses it.

---

## Time-series datacube follow-ons (`to_stack` / `umbra stack` shipped)

- **Surfaced in:** the datacube PR (`STRATEGY.md` §2 / 5.5).
- **Code:** `src/umbra_py/load.py` (`to_stack`, `stack_stats`,
  `stack_to_geotiff`, `STACK_EXTENTS`, `_stack_bounds`, `_mask_slice`),
  `umbra stack` in `cli/process.py`.

`to_stack` co-registers several acquisitions onto one shared grid (lon/lat or a
projected `crs=`, eagerly or lazily via `lazy=True` / `chunk_size=N`) and
returns a `(time, y, x)` `xarray.DataArray`; `stack_stats` reduces it to JSON
(per-pass distribution, pass-to-pass change, an N×N spatial breakdown, optional
per-block series, optional windowed streaming), and both are surfaced on the
CLI, the agent tools and `POST /artifacts/stats`. Follow-ons that build on it,
none a blocker:

- **The agent tools don't take `windowed`, deliberately.** The MCP / LangChain /
  LlamaIndex `stack_stats` tools build an eager cube at `max_size=512`, where
  there is no ceiling to lift — so `windowed` there would be a model-facing knob
  whose only effect is making the percentiles approximate. Wire it only if those
  tools ever grow the lazy/chunked build that would make it mean something; the
  server was the front door with the memory problem. (They *do* take
  `speckle_filter`, which is the opposite case: it changes the answer's quality
  rather than the memory, so it means something at any cube size.)
- ~~**Only the refusal advertises an instance's stats capabilities.**~~
  **shipped** — the landing page's `stats` link carries `umbra:options`
  (`serve.stats_capabilities`): `windowed` and `speckle_filter` each report
  `supported`, an unsupported one carries the `reason` its `400` would have
  given, and `stacking` names the instance's policy. Advertisement and refusal
  are one function (`serve.stats_option_refusal`), and the suite drives the
  renderer against the page to check it. What is still open, and smaller:
  - **`/healthz` still says nothing about it.** Deliberate: the health document
    is kept tiny so a container `HEALTHCHECK` or a Kubernetes probe can poll it
    cheaply, and a probe is not a client picking request options. Revisit only
    if something that cannot fetch `/` needs the capability.
  - **The advertisement describes the *policy*, not the injected renderers.**
    `build_app(renderers=…)` replaces the stacking entirely, so a caller that
    injects its own is advertising `StackExecution`'s behaviour rather than its
    own — the same scope the policy itself already has (it "applies only to the
    default renderers"). A `capabilities=` override on `build_app` is the shape
    if an embedder ever needs one.
  - ~~**Nothing advertises the always-refused pair.**~~ **moot** — `windowed`
    *and* `speckle_filter` together is no longer refused anywhere: the halo read
    (`to_stack(speckle_filter=…, chunk_size=N)`) made the two compose, so the
    request-level check is gone and a chunked instance honours both. There is no
    always-refused pair left to advertise.
- **The quantile histogram is a Python dict of bin → count.** Fine at the sizes
  this sees (a few thousand occupied bins per pass), but a pass spanning hundreds
  of decibels holds proportionally more. If that ever matters, cap the axis or
  widen the bin rather than reaching for a t-digest.
- **The async job path shares the stack-execution policy.** An `"async": true`
  stats request runs the same renderer on the job executor's thread, so
  `--stack-scheduler threads` there means dask's pool *inside* a pool thread.
  `synchronous` (the default) is the safe pairing; a per-path policy would only
  be worth it if an operator wanted sync requests bounded and jobs fast.
- **Nothing reports the stack-execution policy over HTTP.** The CLI echoes it at
  startup, but a client cannot tell a lazy instance from an eager one — correct,
  since the answers are identical, though an operator debugging memory has to
  read the process's own logs.
- **The eager path still opens every source up front.** Both paths open all the
  datasets to resolve the grid (metadata only, but N handles at once). A two-pass
  resolve — footprints first, then reads — would drop that to one at a time; it
  saves handles, not bytes, so it was not worth the churn here.
- **Share the co-registration with `viz`.** `viz._coregister_bands` does the
  same warp-and-decimate for the render commands and predates this. They now
  differ in what they return (bare arrays + bounds vs. a labelled cube) and in
  masking (`viz` keeps raw values for its own stretch), so they were left
  separate rather than forced into one function; if a third caller appears,
  extract the shared VRT/grid step.
- **The datacube notebook picks its own site from a live search.**
  `examples/08_time_series_datacube.ipynb` fetches a repeat-imaged task at run
  time, so it cannot pick a site with a *known* story — a curated task id (or an
  `--area` the showcase already features) would make the printed numbers
  reproducible and give the narrative something specific to point at.

---

## C5 archive-embedding follow-ons (`umbra embed` shipped)

- **Surfaced in:** the `umbra embed` PR (`STRATEGY.md` 5.2).
- **Code:** `src/umbra_py/embed.py`, `umbra embed` in `cli/indexes.py`.

`umbra embed` (visual similarity search — one image vector per acquisition in a
sidecar `catalog.embed.db`, `search_similar(item)` and text-to-scene, `[ai]` +
`[viz]` extras) is shipped, as is the `umbra embed fetch` consume side and the
opt-in publish step in `publish-index.yml`. Follow-ons that build on it, not
blockers:

- **Publishing the first embedding table is a maintainer action.** The weekly
  workflow's embedding step is gated on a maintainer-set `OPENAI_API_KEY` secret
  and is `continue-on-error`, so it costs nothing and publishes nothing until the
  secret is set. (A stac-geoparquet embedding-table form is still an option if a
  non-umbra-py consumer wants one.)
- **A native vector index at scale.** Ranking is a brute-force cosine scan today
  (instant at catalog scale, no binary dependency). If the archive grows to
  hundreds of thousands of scenes, the schema leaves room to swap in `sqlite-vec`
  or an ANN index behind the same `similar()` API.
- **A SAR-tuned encoder.** The default targets a generic CLIP-family multimodal
  `/embeddings` endpoint; a SAR-specific encoder (once one is broadly available)
  would sharpen recall for radar-specific scene types. The `model` label already
  guards against silently mixing encoders in one index.

---

## `viz/` package-split follow-ons (`viz.py` → `viz/` shipped)

- **Surfaced in:** the `viz` package-split PR (`STRATEGY.md` §8 structural debt).
- **Code:** `src/umbra_py/viz/` (`__init__.py`, `geojson.py`, `raster.py`,
  `composites.py`, `contact_sheet.py`, `maps.py`, `_deps.py`), the
  `per-file-ignores` entry in `pyproject.toml`, the `viz/__init__.py` row in
  `llms_txt._MODULE_GUIDE`.

The 2 023-line module is now six modules along the seams the code already had,
with `viz/__init__.py` re-exporting every name it ever exported (public *and*
private), so no caller changed. Follow-ons that build on it, none a blocker:

- **The private re-exports are a compatibility layer, not a design.**
  `viz/__init__.py` re-exports ~30 underscore-prefixed helpers because six other
  package modules (`models`, `index`, `demo`, `narrate`, `describe`, `viewer`)
  import them from `umbra_py.viz`. Pointing each of those imports at the module
  that actually defines the helper (`from .viz.raster import _thumbnail_png`)
  would let the façade shrink to the public surface and drop the `F401`
  per-file-ignore. Deliberately not done here: it would put churn in six
  unrelated modules in a change whose whole claim is that nothing outside `viz`
  moved.
- **`maps.py` is still 800 lines.** It carries three renderers (footprint,
  timeline, swipe) plus the popup/legend/attribution/lazy-imagery HTML and the
  Nominatim geocoder. The geocoder in particular is not a map — it is a
  rate-limited network client with module-level state that `index.bake_places`
  also drives. A `geocode.py` split is the natural next seam if the file grows
  again; it was left alone here because moving it would relocate the one piece
  of mutable module state (`_LAST_GEOCODE_AT`, `_GEOCODE_CACHE`) that the split
  deliberately did *not* re-export.

---

## `cli/` package-split follow-ons (`cli.py` → `cli/` shipped)

- **Surfaced in:** the `cli` package-split PR (`STRATEGY.md` §8 structural debt),
  which the `viz/` entry above named.
- **Code:** `src/umbra_py/cli/` (`__init__.py`, `__main__.py`, `_root.py`,
  `_shared.py`, `discover.py`, `scenes.py`, `process.py`, `composites.py`,
  `atlas.py`, `explore.py`, `indexes.py`), the `per-file-ignores` entry in
  `pyproject.toml`, the repo map in `AGENTS.md`.

The 5 522-line module is now nine modules grouped by what the verb does, with
`cli/__init__.py` re-exporting every name it defined and the whole `--help`
surface byte-identical to before. Follow-ons that build on it, none a blocker:

- **`indexes.py` carries three sub-groups, not one concern.** At 1 162 lines it
  is the largest of the nine because `umbra index`, `umbra semantic` and
  `umbra embed` were kept together as "the local SQLite sidecars". They share a
  shape (build / fetch / info over a `.db` beside the catalog index) but no
  code beyond `_shared`, so splitting them three ways is a one-line-per-module
  change if it grows again. Left as one module here because three ~400-line
  files with the same import header buy separation the reader did not ask for.
- **The command modules reach the shared plumbing as `_shared.<name>`.** That
  is what keeps one patch target for the option-group parity suite (which
  iterates over all fourteen gather commands), but it is a heavier idiom than
  `from ._shared import _gather_items` at every call site. If a future change
  gives the parity suite a per-command module map (`conftest` already owns the
  roster), the qualified form could go back to a plain import.
- **`explore.py` groups by "stands something up", which is the loosest seam.**
  `mcp` and `serve` run servers; `demo`, `tiles` and `showcase` write static
  artifacts that a server or Pages then hosts. They were put together because
  the showcase composes the other four's outputs, but a `publish.py` /
  `servers.py` division is defensible if either half grows.
- **Nothing checks the module split itself.** The parity suite checks that every
  gather command exposes the shared option groups, and `tests/test_llms_txt.py`
  checks the generated command list, but no test asserts that a new command
  lands in a module rather than in `_shared`. That is a convention held by
  review, documented in `AGENTS.md` §2 and the `cli/__init__.py` docstring.

---

## Shared geography option-group follow-ons (`--intersects` everywhere shipped)

- **Surfaced in:** the shared geography-option PR (`STRATEGY.md` §8 structural
  debt).
- **Code:** `src/umbra_py/cli/_shared.py` (`_geometry_option`, `_place_option`,
  `_area_option`, `_resolve_geography`), `src/umbra_py/watch.py` (`watch_key`),
  `src/umbra_py/context.py` (`_SEARCH_PARAMETERS`).

`--bbox` / `--place` / `--intersects` and `--area` / `--fuzzy` are shared option
groups applied to all fourteen gather commands, checked against one roster
(`conftest.GATHER_COMMANDS`) by `tests/test_cli_option_groups.py`. What is still
open:

- **The date and limit options.** `--start` / `--end` / `--limit` /
  `--max-search` are still written out per command, and the decision this entry
  used to leave open is now made: **don't extract them.** The task-name and
  geography groups were worth sharing because their *semantics* are identical
  everywhere and only the wording varied — an override mechanism (or the "keep
  bespoke help inline" convention) buys real drift-prevention. The date and limit
  options are not that: `--limit`'s default is command-specific
  (20 / 24 / 100 / 500 / 2000) as well as its help text ("Max results to plot" vs
  "Max tiles" vs "Max acquisitions to load"), so a shared decorator would have to
  parameterize both and would leave one line per command anyway — indirection with
  no invariant behind it. Revisit only if a command ships with a *missing*
  `--start`/`--limit` (the parity suite would be the place to catch it), which is
  the evidence that would change the call. The *gathering* half (`_gather_items` /
  `_search_source`) is already shared.
- **The MCP `search_catalog` tool cannot plan a polygon.** `umbra ask --aoi` lets
  the planner *select* one of the polygons the caller supplied, by name, rather
  than author coordinates a hallucination could silently move. The other
  model-planned surface takes `bbox` only — the same supply-then-select shape
  would fit it (an operator-configured AOI directory rather than a CLI flag), but
  an MCP client has no equivalent of `--aoi` to pass files through, so it needs a
  server-side convention first.
