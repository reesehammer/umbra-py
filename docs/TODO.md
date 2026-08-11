# Outstanding TODOs

This file tracks follow-up items that were intentionally scoped out of merged
PRs. Each entry should link to the PR that surfaced it, point at the code
involved, and describe the smallest change that closes it out.

When you finish one, delete the entry. The record of what shipped lives in
[`CHANGELOG.md`](../CHANGELOG.md) — this file carries only the work that is
still open.

---

## Narrated change in the demo store (both modes shipped — `STRATEGY.md` §8)

- **Surfaced in:** the narration detection-floor PR (#193).
- **Code:** `src/umbra_py/serve.py` (`Renderers.narrate`, `NarrationBudget`,
  `ClientNarrationBudget`, `NarrationAllowlist`, `client_identity`, `POST
  /artifacts/narrate`), `src/umbra_py/cli/explore.py` (`umbra serve --narrate*`),
  `src/umbra_py/showcase.py` (`--narrate`, `featured_narrator`),
  `src/umbra_py/narrate.py`.

Both delivery modes ship — Mode A (`umbra showcase --narrate`, CI-baked static
readings) and Mode B (live `umbra serve --narrate`, with per-client + allowlist
caps). Open follow-ons, none a blocker:

- **The per-client peer address is the socket peer.** Behind a reverse proxy
  every client reads as the proxy unless it is trusted to set a forwarded-for
  header and uvicorn is run with `--proxy-headers`. `client_identity`
  deliberately does *not* honour a client-settable `X-Forwarded-For` (which
  would make the cap trivially evadable); a proxy that must be configured is the
  operator's call. A bearer-token client is unaffected.
- **The per-client tracking dict is bounded only by the daily reset.** A client
  rotating tokens/addresses within a day grows `ClientNarrationBudget._counts`
  until UTC midnight clears it. Fine at any real client count; if a public
  instance ever meets an adversary minting identities, an LRU cap (evicting the
  least-recently-seen) is the shape.
- **The allowlist is a bbox, not a `bbox/collection` or polygon.** A single
  rectangle is the shape a showcase's featured region has; a collection
  allowlist (or an `--intersects`-style polygon) waits for an instance whose
  curated area is not a rectangle. `NarrationAllowlist` is a frozen dataclass
  with room for the extra field.
- **Async / job-queue narration.** The endpoint is synchronous (a model call is
  seconds, like a render); it deliberately does not take `"async": true`,
  because the budget accounting would have to cross the job worker. Wire it if a
  slow model makes the sync hold matter.
- **Narration is baked for the `change` view only.** `timescan` (whole series)
  and `swipe` (interactive) have no single two/three-date pair for the model to
  read, so `_default_featured_narrator` returns `None` there and the CLI says so.
  A per-view reading (e.g. a whole-series summary for timescan) would be a
  different prompt and grounding; it waits for a view that wants one.
- **The bake uses `select_change_frames`, not `best_change_interval`.** The
  reading is of the frames the composite *shows* (so picture and words agree),
  which is the honest demo; the speckle-clearest interval the selector would pick
  can differ. Grounding it on `best_change_interval` instead would mean also
  rendering the composite of the selected pair — worth doing only if a featured
  site's shown pair turns out to be a poor read.

Security note (recorded so the decision is not re-litigated): storing the model
key as a **GitHub Actions secret is correct** for build-time use (Mode A) —
encrypted, masked, not exposed to fork PRs. What it does **not** support is a
*static* Pages site querying through that key directly: a secret shipped to a
browser is a published secret. Mode B holds the key **server-side** and never
sends it to the browser, which is the only way to key *live, arbitrary-scene*
querying.

---

## Workflow-CLI drift follow-ons (`tests/test_workflows.py` shipped)

- **Surfaced in:** the publish-workflow fix (`STRATEGY.md` §8).
- **Code:** `tests/test_workflows.py`, `.github/workflows/publish-index.yml`.

`tests/test_workflows.py` parses every `umbra …` invocation in
`.github/workflows/*.yml` against the real Click command tree, and extracts /
compiles / resolves every `python -c` body against the installed package. Open
follow-ons, none a blocker:

- **The check is a parse, not a run.** It catches renamed, dropped and
  misspelled options — the drift that actually happened — but not an option
  whose *meaning* changed, nor a value that is wrong (`--limit 1200` too small, a
  bad `--out` path). Running the commands would need a bucket crawl and
  credentials. If a semantic break ever ships, the place to catch it is the live
  canary (`live-canary.yml`), not here.
- **`gh` and `pip` invocations are still unchecked.** Their arguments can drift
  too, but neither references a library symbol the way the `python -c` bodies do,
  so a break there is a workflow-syntax problem a run surfaces rather than a
  silent rename. Add a scan for them only if one ever bites.
- **The scan is textual, so a genuinely dynamic invocation would be missed.**
  Nothing builds an `umbra` command line from a shell variable today; if
  something ever does, the extractor will silently skip it. The self-check
  (`test_the_scan_actually_found_the_published_commands`) pins the publish
  pipeline's commands specifically, but it is a roster to keep current, not a
  general guarantee.

---

## SessionStart hook follow-ons (`.claude/hooks/session-start.sh` shipped)

- **Surfaced in:** the agent-session-hardening PR (`STRATEGY.md` §8).
- **Code:** `.claude/hooks/session-start.sh`, `.claude/settings.json`.

A `SessionStart` hook installs umbra-py editable with every extra on a
Claude-Code-on-the-web container and pre-approves the documented dev-loop +
read-only commands. Open follow-ons, none a blocker:

- **Switch to async mode if startup latency matters.** The hook has no
  `{"async": true}` line, so a web session waits for the ~10–30 s install before
  the first turn — the safe default (no race where a check runs before its deps
  exist). If maintainers prefer a faster start, emit
  `{"async": true, "asyncTimeout": 300000}` first and accept that early turns may
  land before the install finishes.
- **Trim the extras for a lighter/faster install.** The hook installs *all*
  extras so nothing import-skips. If a maintainer only ever touches the core, a
  `[dev]`-only install is faster; the full set is the deliberate default so the
  coverage-gated suite runs unabridged.

---

## Index demo-denormalization follow-ons (`umbra index bake` shipped)

- **Surfaced in:** the baked place-label PR (the G2/G6 gaps).
- **Code:** `src/umbra_py/index.py` (`bake_places`, `bake_thumbnails`, the
  `place` / `thumbnail` columns + migrations), `umbra index bake` /
  `bake-thumbnails` in `cli/indexes.py`, `UmbraItem.place` in `models.py`.

`umbra index bake` reverse-geocodes each footprint centroid into a `place`
column, and `bake-thumbnails` caches a small PNG per acquisition; both are baked
into the published weekly snapshot. Open follow-ons, none a blocker:

- **A precomputed centroid column.** The centroid is derived from the stored
  bbox today (cheap), so a `centroid` column is only worth adding if a consumer
  needs to query/sort on it in SQL rather than compute it per row.
- **The published thumbnail sidecar has no total cap.** Each weekly run adds up
  to `--limit` (1500) previews and never drops any, so `catalog.thumbs.db` grows
  monotonically toward whole-catalog coverage (~10–20 KB per 128 px scene). That
  is the intended trajectory (and the download is opt-in), but if it gets
  unwieldy the smallest fix is an export-side bound —
  `export_thumbnails(limit=…, newest_first=True)` — so the published file keeps
  the most recent N rather than everything ever baked.
- **`newest_first` is opt-in, not the default.** `bake_thumbnails` orders by
  `href` unless asked, to keep an existing caller's batching stable. If no caller
  depends on that order, making newest-first the default would be one fewer flag
  to remember on the path where a cap actually matters.
- **The published sidecar has no asset/size record until the next weekly run.**
  Schema v4 records the asset and size beside every baked preview, but until
  `publish-index.yml` re-exports, every fetched preview reads as "unknown" and a
  merge behaves as it did before (`umbra describe --preview` falls back to
  assuming `GEC`). Nothing to do but wait for a run — noted because it is the same
  silent-until-rebuild lag the PMTiles entry has.
- **The bake's *stretch* is still assumed, not recorded.** `bake_thumbnails` has
  no `db` parameter — every preview is the decibel one — so `--no-db` is refused
  without a lookup. If a linear bake is ever wanted, it is a third column and a
  parameter, not a reinterpretation of the two that exist.

---

## Static GitHub Pages showcase follow-ons (`umbra showcase` shipped)

- **Surfaced in:** the GitHub Pages showcase PR (`STRATEGY.md` §8).
- **Code:** `src/umbra_py/showcase.py` (`build_showcase` / `assemble_showcase`),
  `umbra showcase` in `cli/explore.py`, the `Build catalog showcase` step in
  `.github/workflows/docs.yml`.

`umbra showcase` composes the whole-catalog PMTiles map, the interactive explorer
and a landing page into one static directory; the `docs.yml` Pages job publishes
it. Pages is enabled for the repo. Open follow-ons, none a blocker:

- **The hosted page shows one featured view at a time.** `--featured-view
  {change,timescan,swipe}` picks which marquee gallery is rendered and `docs.yml`
  deploys the `change` one; showing more than one view on the same page would
  need the landing page's featured section repeated per view rather than chosen.
- **Auto-stamp the freshness date from the index.** The CI step passes the run
  date to `--updated`; reading the fetched index's `built_at` (as `umbra index
  info` does) would show the *snapshot's* age rather than the build's.

---

## Whole-catalog PMTiles tiling follow-ons (`umbra tiles` shipped)

- **Surfaced in:** the `umbra tiles` PR.
- **Code:** `src/umbra_py/pmtiles.py`, `umbra tiles` in `cli/explore.py`.

`umbra tiles` (a stdlib-only PMTiles v3 writer over acquisition centroids *and*
footprint polygons, + a MapLibre GL viewer) is shipped, and `umbra demo
--pmtiles` reads it. Open follow-ons, none a blocker:

- **Leaf directories for very large catalogs.** The writer emits a single root
  directory, which is spec-valid and ample for the current catalog (thousands of
  tiles). If the tile count ever grows past a comfortable root-directory size,
  add leaf-directory splitting so readers still fetch a small root first.
- **The published archive only gains new properties on its next rebuild.** The
  COG references and the `pol` / `assets` fields reach `catalog.pmtiles` on the
  next `publish-index.yml` run, so until then the hosted showcase shows no "Get
  SAR image" button and its polarization chips filter nothing out (the "never
  hidden by a facet it has no value for" rule keeps every feature visible — the
  honest failure mode, but a silent one).
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
- **Code:** `src/umbra_py/convert.py`, `umbra convert --dem` in `cli/process.py`.

`umbra convert --dem PATH|auto` / `--geoid` / `--rtc --rtc-model
{cosine,area,gamma,facet}` / `--calibrate {sigma0,beta0,gamma0,rcs}`
terrain-orthorectifies, radiometrically flattens and calibrates a SICD, and
stamps `UMBRA_*` GeoTIFF tags. Open follow-on, not a blocker:

- **MultiRTC interop.** Interop with
  [MultiRTC](https://github.com/MultiSAR/MultiRTC) is a heavier,
  research-oriented job and remains deferred.

---

## Noise-floor subtraction follow-ons (`umbra convert --subtract-noise` shipped)

- **Surfaced in:** the noise-subtraction PR (`STRATEGY.md` 5.5).
- **Code:** `src/umbra_py/convert.py` (the noise estimators, `sicd_noise_level`,
  `compare_noise_models`, the `NOISE_*` tags and constants),
  `src/umbra_py/cli/process.py`, `src/umbra_py/load.py`
  (`MEASUREMENT_PROVENANCE_KEYS`), `src/umbra_py/chips.py` (`NoiseSummary`,
  `ChipDataset.noise`).

The receiver's thermal-noise floor is subtracted before any multiplicative
correction. `--noise-model` selects `measured` (product `NoisePoly`), `estimated`
(one constant per scene from the 5th percentile), or `estimated-range` (per range
line, fitted against range). Open follow-ons, none a blocker:

- **A `RELATIVE` noise level is refused rather than used.** It carries the
  *shape* of the floor across the swath, which could flatten the noise-induced
  gradient without claiming an absolute level. That is a different product (a
  relative correction, not a subtraction), so it wants its own name and provenance
  value rather than a quiet reinterpretation of this flag.
- **The estimator's percentile is fixed at the library level.**
  `NOISE_ESTIMATE_PERCENTILE` (5.0) is a module constant nothing overrides. That
  is deliberate: a knob whose right value depends on how much dark ground a scene
  contains is one most callers would turn wrongly, and the honest fix for a scene
  where 5 % is wrong is a *measured* floor. If a class of scenes needs a different
  tail, thread it through as `noise_percentile=` and record it beside
  `UMBRA_NOISE_FLOOR_DB`.
- **The range profile is fitted along rows only.** `"estimated-range"` fits the
  low tail against range and takes the floor constant along azimuth. That is the
  right first axis, but a long collect can drift along azimuth too. A 2-D surface
  fit would cover both; it needs enough dark ground in *both* directions to be
  better rather than merely more flexible, so it wants evidence from a real scene.
- **The fit's degree and trim are library-level constants.**
  `NOISE_PROFILE_DEGREE` (2), `_NOISE_PROFILE_TRIM_DB` (3.0) and
  `_NOISE_PROFILE_MIN_SAMPLES` (16) are not threaded through, for the same reason
  the percentile is not. If a class of scenes needs a different curve, thread it
  through and record it beside `UMBRA_NOISE_FLOOR_SPREAD_DB`.
- **The fitted level carries the same conservative bias as the constant one.** A
  percentile of a speckled noise-only population sits below its mean, so both
  inferred models read low; the profile fixes the *gradient*, not the offset.
  Correcting the offset means assuming a speckle distribution, which is a claim
  about the product's processing — worth doing only alongside a
  `Grid.ImpRespBW`/multilook read that says how many looks a scene actually has.
  The bias is measurable (`compare_noise_models` reports `bias_db`), but on a
  synthetic single-look population, so it confirms the arithmetic rather than
  supplying the factor a real product needs.
- **`compare_noise_models` has never been run on a real product.** The numbers
  are from a synthetic scene, which validates the arithmetic and claims but not
  the estimator against a real receiver's roll-off, real speckle statistics or a
  real multilook. A Canopy product (or any SICD with an `ABSOLUTE` `NoisePoly`)
  run through `--noise-check` would say so; that is a `network`-marked test gated
  on a token, like the Canopy backend's.
- **The comparison found the estimate compressing over dark ground.** Where
  backscatter sinks toward the floor, the fitted profile reads the swing ~30 %
  flat. The subtraction stays conservative, so this is a caveat on quoting
  `UMBRA_NOISE_FLOOR_SPREAD_DB`, not a correction to make. Reporting it *per
  scene* would mean a per-line margin's minimum; worth doing if the spread starts
  being used as a measurement rather than as evidence.
- **A constant estimate's bias on a varying floor is visible but not acted on.**
  `--noise-check` shows `"estimated"` reading low by more than speckle alone
  accounts for when the floor ramps. Nothing warns about it, deliberately: the
  fix is `estimated-range`, and a warning that says "use the other model" on a
  scene where the user chose this one is noise.
- **Nothing sweeps the percentile.** `compare_noise_models(percentile=…)` exposes
  it, so "is 5.0 the right tail?" is answerable but unanswered. It wants the real
  product above first — the answer on a synthetic exponential population is
  arithmetic, not evidence.
- **Nothing summarises `UMBRA_NOISE_FLOOR_SPREAD_DB` across a batch.** The
  chip-run noise roll-up covers the inferred floors' margin and both models'
  floored fraction, but the swing of a fitted profile is per scene and does not
  obviously add up, so it waits for someone who wants it.
- **The margin threshold is one constant for every scene type.**
  `NOISE_MARGIN_WARN_DB` (6 dB) is where the advisory fires, and what counts as
  "enough dark ground" plausibly differs between coastal and inland scenes. It is
  deliberately not a flag: a knob on a heuristic invites tuning it until the
  warning goes away. The number is reported unconditionally, so anyone who
  disagrees can read the margin itself.

---

## Batch survivability / preflight follow-ons (`umbra chips --skip-unsupported` / `--preflight` shipped)

- **Surfaced in:** the noise-subtraction and preflight PRs (`STRATEGY.md` 5.5).
- **Code:** `src/umbra_py/convert.py` (`UnsupportedMeasurementError`,
  `_check_measurement_support`), `src/umbra_py/preflight.py`
  (`sicd_capabilities`, `preflight_items`, `PreflightResult.error_scope`,
  `UnreadableProductError`), `src/umbra_py/chips.py`
  (`write_skipped_manifest`, `ChipDataset.preflight` / `.skipped`),
  `umbra preflight` / `umbra chips --preflight` in `cli/process.py`.

Metadata refusals are typed and skippable, readable over the wire before a
download, wired into `umbra chips --preflight`, parallelised, and recorded in a
`skipped.jsonl` sidecar; read failures are split into product vs transport. Open
follow-ons, none a blocker:

- **Nothing reads the `skipped.jsonl` sidecar back.** There is no
  `read_skipped_manifest` to pair with the writer, because the file is one JSON
  object per line and a loader's own `json.loads` is the whole reader. Add one
  only if a `ChipDataset` ever needs to be reconstituted from a directory.
- **Nothing retries a transport failure beyond the session's own.**
  `_http.default_session` retries 429/5xx and connect/read errors three times
  with backoff, so a blip is already ridden out; what is left is a failure that
  outlived those. The pass is kept, so the cost of not retrying is a download
  rather than a hole. Worth adding only if a real selection turns out to have a
  failure mode the session's retries systematically miss.
- **`403` is transport, deliberately.** It can be a proxy, a signing or a
  bucket-policy problem as easily as an absent object, and the two are not
  distinguishable from the status alone. Guessing "product" would drop a real
  pass; guessing "transport" costs a download. If the open bucket ever answers
  `403` for objects that genuinely do not exist, the fix is a probe rather than a
  reclassification.
- **`umbra preflight --json` reports the scope, nothing consumes it but the
  chipper.** `error_scope` / `missing_count` / `unreadable_count` are in the
  payload for an agent to branch on, but no other surface reads them — `POST
  /artifacts/*` does not preflight, and `umbra convert` operates on one product.
- **The preflight lane count is not bounded by the session's connection pool.**
  Eight matches the catalog walk's fan-out and sits inside `_http._POOL_SIZE`
  (16), but nothing stops `--workers 64`, which would churn connections rather
  than reuse them. Clamp it against the pool size, or grow the pool with the
  request, if anyone asks for that many.
- **A slow preflight read still holds up the progress lines behind it.**
  Consuming in selection order is what keeps the verdicts pairable, so the line
  for pass 3 waits on pass 2 even when it finished first. The reads themselves do
  not wait — only the printing does — so this is cosmetic until someone wants a
  live counter rather than a roster.
- **Only `umbra chips` has the preflight.** `umbra convert` operates on one
  product where the saving is one download; `umbra serve`'s artifact endpoints do
  not convert.
- **The reader knows NITF 2.1 only.** NITF 2.0's security fields are a different
  length, so it is refused by name rather than guessed at. SICD mandates 2.1, so
  nothing in this archive needs the older layout — add the second field table if
  a product ever turns up that does.
- **A `yes` verdict on a product that carries the polynomials needs `numpy`.**
  Every metadata *refusal* is answerable from a core install, but confirming
  support reads coefficients through `convert`'s reader (the `[convert]` extra).
  Splitting the presence check from the coefficient parse would remove that, at
  the cost of the shared code path that keeps the two from disagreeing.
- **A polarization / looks read is still not asked by the preflight.** The view
  carries the whole SICD XML, so either is a few lines — they wait for a caller
  that wants to *select* on them, which is a different question from "can this
  product answer?" and would want its own place in the report.
- **`--rtc` also needs a DEM, which no preflight can check.** The geometry half
  is a fact about the product and is asked; the DEM half is a fact about the
  request (a path, or `auto`'s network fetch), so a cleared pass can still fail
  for want of terrain. The report says "supports --rtc" where it means "states
  the geometry --rtc needs".

---

## Speckle-filtering follow-ons (`umbra convert --speckle-filter` shipped)

- **Surfaced in:** the speckle-filtering PR (`STRATEGY.md` 5.5).
- **Code:** `src/umbra_py/convert.py` (the `SPECKLE_*` machinery),
  `src/umbra_py/load.py` (`_Speckle`, `_filter_slab`, `_pass_looks`, `_halo_grid`,
  the `speckle_filter=` / `speckle_window=` params on `to_stack`),
  `src/umbra_py/cli/process.py`, `src/umbra_py/chips.py`, `src/umbra_py/serve.py`.

`--speckle-filter boxcar|lee` averages speckle down in the power domain, recorded
and refused-on-mix by `to_stack`, with the equivalent number of looks before/after
reported. It reaches `umbra convert`, `umbra stack` / `to_stack` (including
chunked, via a halo read), `umbra chips` (any asset), `POST /artifacts/stats` and
the agent tools. Open follow-ons, none a blocker:

- **Two filters, not the usual four.** Frost, Kuan and Gamma-MAP are the other
  standard local-statistics filters, and refined Lee keeps a *linear* edge better
  than plain Lee. They are all the same shape as `_lee_power` (a weight from local
  moments), so each is a small addition — but each is another knob with its own
  failure mode, and the two that shipped span the honest range (average
  everything, or average where defensible). Add one when a real scene shows the
  pair leaving something on the table.
- **The window is square in image space, not on the ground.** A SICD's row/column
  ground sample distances differ, so an N×N pixel window is a rectangle on the
  ground and the *resolution* a filtered chip carries is anisotropic — only the
  pixel count is recorded. Recording the ground extent of the window beside it
  would say so; it needs a consumer that cares which axis it lost.
- **The ENL estimate is a floor, and nothing says how tight a floor.** Structure
  inside a block deflates its ENL, so the median block reads low on a textured
  scene: the before/after pair is trustworthy as a *ratio*, either level on its
  own conservative. The natural refinement is the spread of the per-block
  distribution (the same move the noise margin diagnostic made). Worth doing if
  the ENL starts being quoted as a measurement rather than as evidence.
- **`lee`'s looks parameter is read once per scene.** It is the scene's own ENL
  (clamped at single-look), right for uniform processing; a scene whose looks
  varied across the swath would want the per-line treatment
  `--noise-model estimated-range` gives the noise floor. No Umbra product is known
  to need it, so it waits for evidence.
- **The chip-side scene looks is a sample, not a whole-scene read.** Nine
  512-pixel windows on a fixed grid, because reading the product whole is what
  streaming a GEC tile by tile avoids. Right for a number the estimator reads to
  ~1 % from ~1700 pooled blocks, but an acquisition whose speckle statistics vary
  across the swath is described by one number. `_sample_offsets` /
  `_SPECKLE_SAMPLE_GRID` are where a denser or per-region read would go; it wants
  a real product that shows the variation.
- **The chip ENL pair describes the sampled windows, not the tiles.** It is a
  per-scene diagnostic (every record of one acquisition carries the same three),
  so a run cannot say which *tiles* the filter bought least on. Measuring per tile
  would be a noisier statistic; worth doing only if someone wants to select chips
  on it rather than scenes.
- **`--clip-bbox` narrows the chip looks sample too.** The grid spans the
  chipping extent, so a clipped run reads its looks from the area of interest —
  correct for the tiles being cut, but the same acquisition clipped two ways can
  report two ENLs. Recording the extent beside the number would say so.
- **A filtered cube reports no ENL.** `_filter_slab` drops the per-scene
  before/after pair because a lazy cube's slabs are read inside deferred tasks
  with nowhere to report back to. Threading it out for the eager path only would
  make a cube's diagnostics depend on how it was read; a per-slice coordinate on
  the cube (like `item_id`) is the shape that would work if someone wants it.
- **A chunked `"lee"` samples the pass; an unchunked one reads it whole.** A
  chunked build is by definition the case where the pass does not fit, so the
  looks estimate comes from ~9 windows rather than all of it, and nothing records
  which. A pass no wider than one sample window is read whole, so the two agree at
  the sizes where they can. `_LOOKS_SAMPLE_GRID` / `_LOOKS_SAMPLE_SIZE` are where
  a denser read would go.
- **The halo costs a wider read, and nothing says so.** Each window reads
  `(chunk + window − 1)²` cells to return `chunk²`, which at the window sizes this
  is for is under 1 %. A line on the non-JSON output would make it visible if
  anyone picks a chunk small enough for it to matter.
- **Chunked/unchunked equality is to one `float32` ulp, not bit-for-bit.** The
  summed-area table reaches a window's total by a different order of additions
  over a halo-sized read than over a whole pass. `tests/test_load.py` asserts it
  at `np.finfo("float32").eps`. Nothing to fix; worth knowing before someone
  writes `assert_array_equal`.
- **`umbra serve` has no `--stack-speckle-*` default.** Every request that wants
  a filter must name it, because a server-set default would be exactly the
  invisible flag the cache-key rule exists to prevent. Worth revisiting only as a
  *documented advertised* default (one the landing page states and the cache key
  hashes), not as a policy.
- **The filter runs on the whole window in memory.** `_box_sum`'s summed-area
  table makes the cost independent of window size but holds a scene-sized float64
  table, on top of the scene-sized power array. `--clip-bbox` is the answer for a
  scene too large to hold; a tiled implementation (overlapping windows, one strip
  at a time) is the fix if the whole-scene path becomes the common one.

---

## Speckle detection-floor follow-ons (`stack_stats`'s `detection` shipped)

- **Surfaced in:** the detection-floor PR (`STRATEGY.md` 5.5).
- **Code:** `src/umbra_py/_specfun.py`, `src/umbra_py/load.py` (`_detection_floor`,
  the `detection` block and per-block sub-records, `looks`),
  `src/umbra_py/narrate.py` (`ChangeStats.detection`),
  `docs/schemas/stack-stats.schema.json`.

`stack_stats` reports what speckle alone would have done to the change it
measured — each pass's `looks` and a `detection` block (spread, false-alarm
fraction, 5 % threshold), per cube and per block, and on the composite path via
`umbra change --narrate`. Both figures are exact (gamma/beta forms in stdlib
`math`) and validated against simulated speckle. Open follow-ons, none a blocker:

- **The floor is per cell, and the observed fraction is over correlated cells.**
  `false_alarm_fraction` is an exact per-cell probability, so it is the right
  expectation whatever the correlation — but the *scatter* of the observed share
  around it is not, because neighbouring cells of an oversampled product are not
  independent. That is why the advisory uses a stated margin
  (`DETECTION_EXCESS_WARN`) rather than a significance test. An autocorrelation
  read (block variance ÷ decimated variance) would supply an independent-cell
  count and sharpen `looks`; it wants a real product to calibrate against.
- **One floor per cube, not one per interval.** The representative `looks` is the
  median of the passes that gave a reading, so a series whose passes genuinely
  differ is described by a middle value (every pass keeps its own `looks`, so the
  disagreement is visible). A per-interval floor would use the pair's two looks in
  the unequal-shape form `regularized_incomplete_beta` already supports; it waits
  for a consumer.
- **The block flag is the margin heuristic, not a significance test.** It reuses
  `DETECTION_EXCESS_WARN`, inheriting the "per cell / correlated cells" limit
  above: a proper per-block test needs an independent-cell count nothing measures
  (the block's `compared_cells` overstates it for an oversampled product).
  Exposing the cell count is the honest half that ships without it.
- **The composite-path floor is scene-wide, not per block.** Like the cube's, it
  describes the pair as a whole; a block's `mean_delta_db` is weighed against the
  scene `cell_sigma_db` but no per-block floor is emitted, for the same reason
  `stack_stats`'s does not reach `spatial`. The natural form is the floor plus
  each block's valid-cell count; it waits for the same consumer.
- **The composite-path looks is read off each whole pass, not windowed.** The
  render holds both bands in memory, so there is no ceiling to lift; a per-pass
  `_estimate_enl` of the whole band is the exact read. If a clipped narration ever
  streamed its passes, the windowed accumulator is the drop-in.
- **`_DETECTION_MAX_LOOKS` is a numerical guard doing a physical job.** A cube
  whose blocks are numerically uniform reads unbounded looks, and the cap keeps
  the beta integral inside double precision. It changes no meaningful answer, but
  a cube pinned at the cap reports `looks: 1024`, a true but strange-looking
  statement. Reporting "no measurable speckle" instead would mean a fourth state;
  it waits for someone to hit it on real data.
- **The looks read is a median over blocks, so the two measurement walks can
  disagree in the last decimal.** Blocks are cut from whatever array is in hand,
  and `windowed=True` hands it windows — so a window narrower than the 16-cell
  block finds none where a whole slice finds several. `looks` is a read of the
  scene rather than one of the exact sums beside it, and the docstring says so.
  Aligning the blocks to the shared grid would remove it, at the cost of a
  block-offset argument threaded through `_block_enl_ratios`.

---

## Area-of-interest clipping follow-ons (`--clip-bbox` on convert / chips shipped)

- **Surfaced in:** the conversion-clipping PR (`STRATEGY.md` 5.5).
- **Code:** `src/umbra_py/convert.py` (`_clip_window`, `bbox=` on
  `sicd_to_geocoded_cog`, `ClipSavings`), `src/umbra_py/chips.py`
  (`_clip_pixel_window`, `ClipSummary`, `bbox=` on `chip_item` / `write_chips`),
  `umbra convert --clip-bbox` / `umbra chips --clip-bbox` in `cli/process.py`.

`sicd_to_geocoded_cog(bbox=…)` reads only the image window covering a lon/lat
rectangle and crops the output to it; `chip_item(bbox=…)` tiles only that window
and, for a complex asset, passes it down as the conversion's clip. What each clip
saved is reported. Open follow-ons, none a blocker:

- **The clip is a rectangle, not a polygon.** The rest of the CLI takes an AOI as
  a *shape* (`--intersects`). A clipped conversion is a north-up raster, so a
  polygon could only mask it after the warp rather than shrink the read — worth
  doing when someone wants the mask, not for the cost.
- **The window search runs on the flat-earth projection even with `--dem`.**
  Terrain moves a ground point far less than the one-lattice-step padding, so a
  DEM-orthorectified clip is still a superset in practice. If a scene over extreme
  relief ever loses an edge pixel, the fix is to grow the pad by the DEM's height
  range × `tan(incidence)` rather than to run the refinement loop twice.
- **The clip is not in the provenance tags, deliberately.** The geotransform
  already states which ground it covers, and `UMBRA_*` records what a pixel value
  *means*; adding it would make a clipped and an unclipped conversion of one site
  disagree on a key for no measurement reason. Revisit only if someone needs to
  tell "clipped to X" from "the scene only covered X".
- **`umbra convert --clip-bbox` takes coordinates, not a place.** `--place` /
  `--area` resolve a name to a rectangle everywhere items are *searched*;
  `convert` operates on a downloaded file and has no search. One shared resolver
  call would do it if the coordinates prove annoying in practice.
- **A `SICD` prepared by a custom `preparer` or a `--work-dir` cache hit reports
  no clip saving.** The `clip_report` callback is only wired to the default
  `_prepare_sicd` (the public `SicdPreparer` signature has no place to report
  through), and a cache hit runs no conversion to price. Both are the honest
  failure mode — a run over freshly-converted scenes reports fully — but a
  custom-preparer caller who wants the number would need the seam widened.

---

## Provenance-consuming follow-ons (`to_stack` refuses mixed conversions)

- **Surfaced in:** the provenance-consumption PR (`STRATEGY.md` 5.5).
- **Code:** `src/umbra_py/load.py` (`MEASUREMENT_PROVENANCE_KEYS`,
  `_shared_provenance`, `stack_provenance` / `StackProvenance`),
  `umbra stack --provenance` in `cli/process.py`, `src/umbra_py/serve.py`
  (`POST /artifacts/provenance`), `src/umbra_py/viz/composites.py`,
  `src/umbra_py/narrate.py`.

`to_stack` reads each source's `UMBRA_*` record and refuses a series that
disagrees on what its pixel values are; the same refusal covers the composite-path
caller that quotes numbers (`render_change_png`), and `stack_provenance` /
`umbra stack --provenance` / `POST /artifacts/provenance` preflight it. Open
follow-ons, none a blocker:

- **The refusal has no override.** There is no `check_provenance=False`, matching
  the polarization refusal it mirrors — a mixed selection is not a measurement. If
  a legitimate "I know, show me anyway" case turns up (comparing a calibrated pass
  against an uncalibrated one *as* the experiment), the escape hatch is one
  keyword on `to_stack` plus a caveat in the summary saying it was used.
- **The picture commands still don't check, now deliberately.** `umbra change` /
  `timescan` / `swipe` take the records `_coregister_bands` returns and ignore
  them — the same tolerance the polarization rule has (a mixed composite is
  confusing to look at; a mixed *number* is wrong). A warning on the picture
  commands is the obvious next step if a mixed composite turns out to mislead in
  practice; it was left out because a warning nobody can act on is noise.
- **A refused pair costs the co-registration first.** `render_change_png` reads
  the records from the datasets `_coregister_bands` opens, so a mixed pair is
  caught *after* the overview reads and before the model call. Failing before the
  reads would need a metadata-only pre-open per source (a second round of range
  requests on the passing path), so the cheap ordering is the one that ships.
- **Only the radiometric keys are grounds for refusal.** `dem`, `geoid` and
  `projection` are carried when every source agrees and silently dropped when they
  don't: they move a pixel's *position*, not its value, and `to_stack` re-grids
  everything anyway. Add them to `MEASUREMENT_PROVENANCE_KEYS` if a DEM mix ever
  produces misregistration worth failing on.
- **A hosted instance is still the missing half of the provenance preflight.**
  `POST /artifacts/provenance` makes the preflight free for a client with nothing
  installed, which is only worth something once a public `umbra serve` exists.
- **`/artifacts/change`, `timescan` and `swipe` have no preflight.** They draw
  rather than measure, so they never refuse a mix. The one composite-path caller
  that *does* quote decibels is `render_change_png`, and it has no HTTP surface.
- **The search-side commands still don't report conversions.** Listing the
  distinct `UMBRA_CALIBRATION` values in a selection the way `umbra search`
  reports polarizations would cost a header read per result on a command whose
  whole promise is that search is metadata-only, which is why the preflight lives
  on the command that was going to open them anyway.
- **Nothing preflights the composite path.** `render_change_png` applies the
  refusal and pays the co-registration first; `stack_provenance` would answer for
  a pair as readily as a series, but wiring it into `umbra change --narrate` needs
  the pair in hand before `_coregister_bands` opens them — the same ordering
  question above.
- **A cube's provenance reaches the render manifest only inside `stats`.** `umbra
  stack --json` emits the shared `{output, items_used, parameters}` manifest,
  which has no provenance field; the record is present only when `--stats` was
  also asked for. Adding it would mean a schema revision, so it waits for a
  consumer.

---

## Published JSON-contract follow-ons (`docs/schemas/` + `tests/test_schemas.py`)

- **Surfaced in:** the schema-contract PR (`STRATEGY.md` §8, design principle 5).
- **Code:** `docs/schemas/`, `umbra_py/_schemas/` (packaged copy),
  `umbra_py.schemas`, `tests/test_schemas.py`, `serve.openapi_components()`.

Sixteen strict schemas, each validated against a real payload, packaged into the
wheel and merged into `umbra serve`'s OpenAPI document. Every `--json` shape the
CLI emits is published. Open follow-ons, none a blocker:

- **The `.geojson` manifest's envelope is unschema'd.** Its feature `properties`
  are validated against `chip-record.schema.json`, but the `FeatureCollection`
  around them carries two non-standard top-level keys (`license`, `attribution`).
  GeoJSON allows foreign members, so nothing is wrong; a consumer reading the
  *file* rather than the records has no contract for the wrapper. One small schema
  `$ref`ing the record, if a consumer ever wants it.
- **The `.parquet` manifest is a schema nothing describes.** It is stac-geoparquet
  (each chip a STAC Item row), so its contract is stac-geoparquet's — but the
  mapping from `ChipRecord` fields to Item `properties` is this project's, and it
  is described only by `_chip_to_stac_item`.
- **A `SceneImage` records the request's `max_size`, not the render's own
  ceiling.** A rendered quicklook is capped by what the COG's overviews supply, so
  `width` can come in under `max_size` for a reason unrelated to a small baked
  preview. Both numbers are in the document, so this is a caveat on interpreting
  them; recording *why* they differ would mean the renderer reporting what it was
  capped by.
- **The watch delta's `query` is open by construction.** Unset filters are dropped
  rather than emitted as nulls, so the schema cannot close it without freezing the
  search signature into a contract. Naming the known keys as optional properties
  while staying open would document them — worth doing if a consumer branches on
  the echo rather than on `new_items`.
- **`umbra ask --json` (and `semantic search --run`) emits a plan then a raw STAC
  item per line.** Only the plan is schema'd; the item lines are source documents
  whose contract is STAC's. That the stream is a plan object followed by
  newline-delimited items is a shape nothing describes, because it is a
  concatenation rather than a document.
- **The packaged schema copy is checked by a parse in the suite, and by Docker in
  CI.** Every environment in the Python matrix installs editable and so exercises
  only the fallback branch; the packaged branch is exercised end to end by the
  Docker smoke test. Building a wheel inside the suite would bring the check
  closer, at the cost of a build backend in the test path.
- **The copy means `docs/schemas/README.md` ships inside the wheel too.**
  `force-include` takes the directory. Harmless (a few KB); excluding it would
  mean listing the schemas individually, which is the drift the directory-level
  include avoids.
- **Only the three schemas the artifact routes emit are OpenAPI components.** The
  other fourteen describe CLI and agent-tool surfaces this server does not have.
  Add one when a route starts emitting it.
- **A cross-file `$ref` cannot be inlined.** `_rewrite_refs` refuses one rather
  than emitting a reference no client can resolve, so publishing `render-manifest`
  or `watch-delta` as a component would mean publishing its target first. A few
  lines when something needs it; deliberately not written on speculation.
- **The complex documents carry property examples, not a whole-document one.**
  `stack-stats`, `chip-dataset`, `chip-record`, `item-context`,
  `scene-description`, `search-plan`, `preflight` and `watch-delta` are large,
  conditional shapes whose most trustworthy example is a real emitted payload —
  which the suite already validates. A hand-authored whole-document example waits
  for a case where the real-payload fixtures are not example enough for a reader.

---

## Register `umbra-mcp` in the MCP registries and Anthropic's directory

- **Surfaced in:** the `umbra-mcp` MCP server PR.
- **Code:** `server.json`, `tests/test_mcp_registry.py`, the `publish-mcp` job in
  `.github/workflows/release.yml`, `src/umbra_py/mcp_server.py`, `pyproject.toml`.

The server is shipped and runnable, the MCP → LangChain → LlamaIndex trilogy is
complete, and the registry half is plumbing. Open follow-ons:

- **The first publish is a maintainer action.** The `publish-mcp` job runs on a
  published GitHub Release, and no release has been cut (the same gate the PyPI
  Trusted Publisher registration sits behind — `STRATEGY.md` §8). Until then
  `io.github.reesehammer/umbra-mcp` is not in the registry, and the registry's
  PyPI ownership check (which reads the `mcp-name:` marker the README carries) has
  never run against a real upload.
- **Anthropic's directory is a separate, manual listing.** The official MCP
  registry is one submission; the vendor directories are their own forms.
- **The schema URL is pinned by hand.** `server.json` names a dated schema version
  (`2025-12-11`) and the test requires a dated one, but nothing notices when the
  registry publishes a newer one. That is the right default (an unpinned schema is
  not reproducible); the network-marked validation test is what would catch the
  pin going stale.

---

## Repeat-imaged-site discovery follow-ons (`umbra sites` shipped)

- **Surfaced in:** the `umbra sites` PR (`STRATEGY.md` §8, "Discovery surface").
- **Code:** `src/umbra_py/coverage.py`, `umbra sites` in `cli/discover.py`,
  `find_repeat_sites` (agent tools), `GET`/`POST /sites` in `serve.py`,
  `CatalogIndex.rank_sites`.

The discovery moat ranks the archive's most repeat-imaged sites whole-archive on
every surface (CLI, agent tools, HTTP), filters on depth / recency / onset /
cadence / baseline, and ranks on depth / recency / span / cadence. Open
follow-ons, none a blocker:

- **The filter inputs are not echoed in the `find_repeat_sites` / `/sites`
  response metadata.** The return carries the resolved `place` / `bbox` / `area`
  but not `active_since`, `min_passes`, `rank_by` or `top`. Add all of them
  together if a consumer ever needs the round-trip, rather than singling one out.
- **On the HTTP surface the `active_*` / `first_*` filters accept a relative
  expression, unlike the strict-ISO `datetime` filter.** `_coerce_date` resolves
  `"6 months ago"` on `GET`/`POST /sites`, which matches the CLI but is a grammar
  the STAC `datetime` query param does not take — a deliberate asymmetry (an
  umbra-specific discovery filter, not a STAC one), noted so it is not mistaken
  for drift.
- **The temporal rankings order by the whole-site figure, not the comparable
  one.** `rank_by=recency`/`span`/`cadence` sort on the site's `last` /
  `span_days` / `median_revisit_days`, deliberately independent of `--rank-by
  comparable`'s analysable subset. A caller who wanted "rank by the *analysable*
  series' recency/span" would need a `comparable_last` / `comparable_span_days`
  read threaded into the sort key; it waits for the same consumer, since the two
  coincide when a site is single-polarization.

---

## Grow the `umbra serve` STAC API (a hosted instance)

- **Surfaced in:** the `umbra serve` STAC API PR.
- **Code:** `src/umbra_py/serve.py`, `pyproject.toml` (`[serve]` extra).

The read-only STAC API is shipped (landing / conformance / collections / items /
`GET`+`POST /search`, `GET`+`POST /sites`, the artifact render routes and `POST
/artifacts/stats` + `provenance`, async jobs, the STAC Query extension). Open
follow-on:

- **A hosted community instance.** The local-first server has no operational cost;
  a public instance is a policy decision (COG-streaming egress) that would make
  the archive queryable with zero install — pair it with the static demo front
  end `umbra showcase` already builds. Gated on the `STRATEGY.md` §6 guardrail
  (talk to Umbra first).

---

## Canopy commercial-archive backend follow-ons (`UmbraCatalog(token=...)` shipped)

- **Surfaced in:** the Canopy backend PR (`STRATEGY.md` 5.1).
- **Code:** `src/umbra_py/catalog.py` (`_search_archive` / `_archive_page`),
  `src/umbra_py/constants.py` (`CANOPY_ARCHIVE_URL`), `umbra search --token`.

The commercial archive is searchable behind the same `search()` interface (bearer
token → STAC API POST search + pagination), offline-tested against a mocked API.
Open follow-ons, none a blocker:

- **Push `product_types` / `area` down as STAC query/filter extensions.** They are
  applied client-side today (exact parity with the open-bucket path). Once the
  concrete Canopy field names are confirmed against the live API, sending them as
  a STAC *query*/*filter* body would let the server pre-filter and cut transferred
  pages. This needs a real token to verify, so it is deferred rather than guessed.
- **Verify request/response shapes against the live Canopy API.** The client is
  built to the STAC API *standard*; confirm the exact search body, collection ids
  and pagination link shape Canopy emits, and adjust if it deviates. Add a
  `network`-marked smoke test gated on a `UMBRA_CANOPY_TOKEN` secret.

---

## C1 natural-language search follow-ons (all four steps shipped)

The four C1 steps — relative dates, the fuzzy task matcher, model-planned `umbra
ask`, and the semantic embedding index — are all shipped, as are the LangChain /
LlamaIndex wrappers and the MCP `search_catalog` semantic mode. Optional
follow-on, not a blocker:

- **Embed task *descriptions*, not just names.** The current index embeds the task
  label; if Umbra publishes per-task descriptions, embedding those too would widen
  recall further.

---

## C2 VLM-in-the-loop follow-ons (`umbra describe` shipped)

- **Surfaced in:** the `umbra describe` PR.
- **Code:** `src/umbra_py/describe.py`, `src/umbra_py/narrate.py` (`[ai]` +
  `[viz]` extras), `constants.AI_PROVENANCE`.

`umbra describe` and `umbra change --narrate` are shipped on the CLI, MCP,
LangChain and LlamaIndex surfaces, with `--preview {render,baked,auto}` reading
the baked local preview instead of re-streaming the COG. Open follow-ons:

- **`--preview auto` costs one index read on a request it will refuse.** It used
  to skip the lookup for a non-`GEC` asset, because the answer was knowable from
  the request alone; with the asset/size record it no longer is. The read is a
  local point query and the alternative is refusing bakes that would have
  answered, so this is the right trade — but it is the reason a `--preview auto`
  run over many scenes touches the index once per scene.
- **`umbra change --narrate` still renders both passes.** The composite is a
  co-registered difference of two full reads, not a single quicklook, so a 128 px
  preview per pass is not the same object — there is no cached artifact to
  substitute. The natural equivalent is a cache of the *composite*, which is what
  `umbra serve`'s artifact cache already is; a CLI-side one would be a new store
  rather than a reuse.
- **Nothing reports what the substitution saved.** As with `--clip-bbox`, the
  command says what it read, not that it skipped an overview stream to read it. A
  line on the non-JSON output would make the flag's value visible where someone is
  deciding whether to use it.

---

## C4/C5 ML dataset follow-ons (`umbra chips` shipped)

- **Surfaced in:** the `umbra chips` PR (`STRATEGY.md` 5.5).
- **Code:** `src/umbra_py/chips.py`, `umbra chips` in `cli/process.py`.

`umbra chips` (fixed-size georeferenced tiles + a `.jsonl` / `.geojson` /
stac-geoparquet manifest, including `--asset SICD`) is shipped. Follow-on, not a
blocker:

- **`CSI` goes down the amplitude path, which is right but undiscussed.** A CSI is
  a colour sub-aperture *image* — already a display raster — so it is streamed
  like a GEC and none of the conversion flags apply. `CPHD` stays out entirely: it
  is phase history rather than a focused image, so there is no image grid to chip
  until something focuses it.

---

## Time-series datacube follow-ons (`to_stack` / `umbra stack` shipped)

- **Surfaced in:** the datacube PR (`STRATEGY.md` §2 / 5.5).
- **Code:** `src/umbra_py/load.py` (`to_stack`, `stack_stats`, `stack_to_geotiff`),
  `umbra stack` in `cli/process.py`, `serve.py` (`StackExecution`).

`to_stack` co-registers acquisitions onto one shared grid (eager or lazy/chunked)
and `stack_stats` reduces it to JSON, both surfaced on the CLI, the agent tools
and `POST /artifacts/stats`. Open follow-ons, none a blocker:

- **The agent tools don't take `windowed`, deliberately.** The MCP / LangChain /
  LlamaIndex `stack_stats` tools build an eager cube at `max_size=512`, where
  there is no ceiling to lift — so `windowed` there would be a model-facing knob
  whose only effect is making the percentiles approximate. Wire it only if those
  tools grow the lazy/chunked build. (They *do* take `speckle_filter`, the
  opposite case: it changes the answer's quality, not the memory.)
- **`/healthz` says nothing about stats capabilities.** Deliberate: the health
  document is kept tiny so a container `HEALTHCHECK` or Kubernetes probe can poll
  it cheaply. Revisit only if something that cannot fetch `/` needs the capability.
- **The capability advertisement describes the *policy*, not injected renderers.**
  `build_app(renderers=…)` replaces the stacking entirely, so a caller that
  injects its own is advertising `StackExecution`'s behaviour rather than its own.
  A `capabilities=` override on `build_app` is the shape if an embedder needs one.
- **The quantile histogram is a Python dict of bin → count.** Fine at the sizes
  this sees (a few thousand occupied bins per pass), but a pass spanning hundreds
  of decibels holds proportionally more. If that ever matters, cap the axis or
  widen the bin rather than reaching for a t-digest.
- **The async job path shares the stack-execution policy.** An `"async": true`
  stats request runs the same renderer on the job executor's thread, so
  `--stack-scheduler threads` there means dask's pool *inside* a pool thread.
  `synchronous` (the default) is the safe pairing; a per-path policy would only be
  worth it if an operator wanted sync requests bounded and jobs fast.
- **Nothing reports the stack-execution policy over HTTP.** The CLI echoes it at
  startup, but a client cannot tell a lazy instance from an eager one — correct,
  since the answers are identical, though an operator debugging memory has to read
  the process's logs.
- **The eager path still opens every source up front.** Both paths open all the
  datasets to resolve the grid (metadata only, but N handles at once). A two-pass
  resolve — footprints first, then reads — would drop that to one at a time; it
  saves handles, not bytes, so it was not worth the churn here.
- **Share the co-registration with `viz`.** `viz._coregister_bands` does the same
  warp-and-decimate for the render commands and predates this. They now differ in
  what they return and in masking, so they were left separate; if a third caller
  appears, extract the shared VRT/grid step.
- **The datacube notebook picks its own site from a live search.**
  `examples/08_time_series_datacube.ipynb` fetches a repeat-imaged task at run
  time, so it cannot pick a site with a *known* story — a curated task id (or an
  `--area` the showcase already features) would make the printed numbers
  reproducible.

---

## C5 archive-embedding follow-ons (`umbra embed` shipped)

- **Surfaced in:** the `umbra embed` PR (`STRATEGY.md` 5.2).
- **Code:** `src/umbra_py/embed.py`, `umbra embed` in `cli/indexes.py`.

`umbra embed` (visual similarity search — one image vector per acquisition in a
sidecar `catalog.embed.db`, `search_similar(item)` and text-to-scene) is shipped,
as is the consume side and the opt-in publish step. Open follow-ons, not blockers:

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
  `composites.py`, `contact_sheet.py`, `maps.py`, `_deps.py`).

The 2 023-line module is now six modules, with `viz/__init__.py` re-exporting
every name it ever exported. Open follow-ons, none a blocker:

- **The private re-exports are a compatibility layer, not a design.**
  `viz/__init__.py` re-exports ~30 underscore-prefixed helpers because six other
  modules import them from `umbra_py.viz`. Pointing each import at the module that
  defines the helper would let the façade shrink to the public surface and drop
  the `F401` per-file-ignore. Deliberately not done here: it would put churn in
  six unrelated modules in a change whose whole claim is that nothing outside
  `viz` moved.
- **`maps.py` is still 800 lines.** It carries three renderers plus the
  popup/legend/attribution/lazy-imagery HTML and the Nominatim geocoder. The
  geocoder in particular is a rate-limited network client with module-level state
  that `index.bake_places` also drives. A `geocode.py` split is the natural next
  seam if the file grows again; it was left alone because moving it would relocate
  the one piece of mutable module state the split deliberately did *not* re-export.

---

## `cli/` package-split follow-ons (`cli.py` → `cli/` shipped)

- **Surfaced in:** the `cli` package-split PR (`STRATEGY.md` §8 structural debt).
- **Code:** `src/umbra_py/cli/` (nine modules + `_root.py` / `_shared.py`).

The 5 522-line module is now nine modules grouped by what the verb does, with the
whole `--help` surface byte-identical to before. Open follow-ons, none a blocker:

- **`indexes.py` carries three sub-groups, not one concern.** At 1 162 lines it is
  the largest because `umbra index`, `umbra semantic` and `umbra embed` were kept
  together as "the local SQLite sidecars". They share a shape but no code beyond
  `_shared`, so splitting them three ways is a one-line-per-module change if it
  grows. Left as one module because three ~400-line files with the same import
  header buy separation the reader did not ask for.
- **The command modules reach the shared plumbing as `_shared.<name>`.** That
  keeps one patch target for the option-group parity suite, but it is a heavier
  idiom than `from ._shared import _gather_items`. If a future change gives the
  parity suite a per-command module map, the qualified form could go back to a
  plain import.
- **`explore.py` groups by "stands something up", the loosest seam.** `mcp` and
  `serve` run servers; `demo`, `tiles` and `showcase` write static artifacts. A
  `publish.py` / `servers.py` division is defensible if either half grows.
- **Nothing checks the module split itself.** The parity suite checks that every
  gather command exposes the shared option groups, but no test asserts that a new
  command lands in a module rather than in `_shared`. That is a convention held by
  review, documented in `AGENTS.md` §2 and the `cli/__init__.py` docstring.

---

## Shared geography option-group follow-ons (`--intersects` everywhere shipped)

- **Surfaced in:** the shared geography-option PR (`STRATEGY.md` §8 structural
  debt).
- **Code:** `src/umbra_py/cli/_shared.py` (`_geometry_option`, `_place_option`,
  `_area_option`, `_resolve_geography`), `src/umbra_py/watch.py`,
  `src/umbra_py/context.py`.

`--bbox` / `--place` / `--intersects` and `--area` / `--fuzzy` are shared option
groups applied to all fourteen gather commands, checked against one roster by
`tests/test_cli_option_groups.py`. Open follow-ons:

- **The date and limit options stay per-command — decided.** `--start` / `--end`
  / `--limit` / `--max-search` are written out per command. The task-name and
  geography groups were worth sharing because their *semantics* are identical
  everywhere and only the wording varied; the date/limit options are not that —
  `--limit`'s default is command-specific (20 / 24 / 100 / 500 / 2000) as well as
  its help text, so a shared decorator would parameterize both and leave one line
  per command anyway. Revisit only if a command ships with a *missing*
  `--start`/`--limit` (the parity suite would be the place to catch it).
- **The MCP `search_catalog` tool cannot plan a polygon.** `umbra ask --aoi` lets
  the planner *select* one of the polygons the caller supplied, by name, rather
  than author coordinates a hallucination could silently move. The other
  model-planned surface takes `bbox` only — the same supply-then-select shape would
  fit it (an operator-configured AOI directory), but an MCP client has no
  equivalent of `--aoi` to pass files through, so it needs a server-side
  convention first.
