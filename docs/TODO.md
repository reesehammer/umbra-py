# Outstanding TODOs

This file tracks follow-up items that were intentionally scoped out of merged
PRs. Each entry should link to the PR that surfaced it, point at the code
involved, and describe the smallest change that closes it out.

When you finish one, delete the entry. The record of what shipped lives in
[`CHANGELOG.md`](../CHANGELOG.md) — this file carries only the work that is
still open.

---

## Narrated change in the demo store (both modes shipped — `STRATEGY.md` §8)

- **Surfaced in:** the narration detection-floor PR (#193), which made the
  "scan a series, pick the pair worth narrating" selection honest — the interval
  whose change stands clear of the speckle floor. Defined in `STRATEGY.md` §8
  ("Bring the VLM to the browsing user"). **Mode B shipped (live `umbra serve
  --narrate`); Mode A shipped (`umbra showcase --narrate`, the CI bake into the
  static Pages showcase).**
- **Code:** `src/umbra_py/load.py` (`select_change_interval`,
  `best_change_interval`), `src/umbra_py/serve.py` (`Renderers.narrate`,
  `narrate_options`, `NarrationBudget`, `ClientNarrationBudget`,
  `NarrationAllowlist`, `client_identity`, `narrate_capabilities`, `POST
  /artifacts/narrate`, the landing `narrate` link, `build_app`/`serve` narrator +
  budget/allowlist params), `src/umbra_py/cli/explore.py` (`umbra serve
  --narrate` / `--narrate-model` / `--narrate-daily-limit` /
  `--narrate-client-limit` / `--narrate-allow-bbox`), `src/umbra_py/narrate.py`
  (the existing narration it feeds), `tests/test_load.py`, `tests/test_serve.py`.

The two model capabilities exist on the CLI/MCP surfaces
(`umbra change --narrate` for a pair; `stack_stats` over a `to_stack` cube for a
series) but were invisible to someone browsing the hosted `umbra showcase` /
`umbra demo` explorer. **Mode B** surfaces both over HTTP, composed: a
deterministic selector finds the pair worth looking at, the existing narration
reads it. What shipped, and what is left:

- ~~**1. The candidate selector.**~~ **shipped** — `select_change_interval`
  (pure: reads a `stack_stats` payload, returns the consecutive interval whose
  `changed_fraction` most exceeds the cube's `detection.false_alarm_fraction`,
  with `stands_clear` against `DETECTION_EXCESS_WARN`) and `best_change_interval`
  (builds the cube, reduces it, applies the selector, returns the two
  `UmbraItem`s ready to narrate). Pure/offline for the selector; `load`-extra for
  the orchestration. It is the honest answer to "*which* two of these fifteen
  passes?" — a number picks the frames, never the model.
- ~~**4. Live narration behind a hosted `umbra serve` (Mode B).**~~ **shipped** —
  `POST /artifacts/narrate` (opt-in via `umbra serve --narrate` + a server-side
  key) narrates a pair directly and **scans a longer series first** via
  `best_change_interval`, returning the narration with the chosen
  `selected_interval`. Guardrails that shipped: opt-in (`501` when disabled), the
  content-addressed cache (a repeat request calls no model), and a per-day spend
  ceiling (`NarrationBudget` / `--narrate-daily-limit`, counted only on
  cache-miss calls, `429` when spent). The key is the instance's, never a request
  field. What is still open, and gated on standing up a *public* instance (the
  §5.6 Umbra conversation first):
  - ~~**Per-client rate limiting.**~~ **shipped** — `umbra serve
    --narrate-client-limit N` caps live model calls *per client* per UTC day
    (`ClientNarrationBudget`, keyed by `client_identity` — a bearer token, hashed
    rather than stored, else the peer address), checked *before* the global
    `NarrationBudget` so one caller cannot burst through the whole day's budget,
    counted only on a cache-miss call, and answering a `429` that names the
    per-client limit. What is still open, and smaller:
    - **The peer address is the socket peer.** Behind a reverse proxy every
      client reads as the proxy unless it is trusted to set a forwarded-for header
      and uvicorn is run with `--proxy-headers`. `client_identity` deliberately
      does *not* honour a client-settable `X-Forwarded-For` (which would make the
      cap trivially evadable); a proxy that must be configured is the operator's
      call. A bearer-token client is unaffected.
    - **The tracking dict is bounded only by the daily reset.** A client rotating
      tokens/addresses within a day grows `ClientNarrationBudget._counts` until
      UTC midnight clears it. Fine at any real client count; if a public instance
      ever meets an adversary minting identities, an LRU cap (evicting the
      least-recently-seen, which frees at most one slot's worth of budget) is the
      shape.
  - ~~**A curated allowlist.**~~ **shipped** — `umbra serve --narrate-allow-bbox
    min_lon,min_lat,max_lon,max_lat` (`NarrationAllowlist`) bounds the endpoint to
    a curated area, refusing with `403` any scene whose footprint *centroid* falls
    outside it — before the cache, either budget or the model, and *failing
    closed* on a footprint-less scene. The centroid, not the footprint overlap, is
    the test on purpose: a huge footprint clipping the corner of the area is not
    *in* it. What is still open, and smaller:
    - **It is a bbox, not the `bbox/collection` this entry sketched.** A single
      rectangle is the shape a showcase's featured region has; a collection
      allowlist (or a polygon, `--intersects`-style) waits for an instance whose
      curated area is not a rectangle. `NarrationAllowlist` is a frozen dataclass
      with room for the extra field.
  - **Async / job-queue narration.** The endpoint is synchronous (a model call is
    seconds, like a render); it deliberately does not take `"async": true`,
    because the budget accounting would have to cross the job worker. Wire it if a
    slow model makes the sync hold matter.
  - ~~**Expose the selector on the CLI / MCP.**~~ **shipped** — `umbra stack
    --pick-interval` and the `pick_change_interval` MCP / LangChain / LlamaIndex
    tool scan a whole series and return the pass-pair whose change stands
    furthest clear of the speckle floor, with the two URLs ready to hand to
    `umbra change --narrate` / `narrate_change` — so the scan → narrate chain no
    longer needs a hosted `umbra serve`. Both are thin adapters over the same
    `best_change_interval`, so the server, the shell and the agent surfaces
    cannot drift; `--pick-interval` is its own mode (like `--provenance`) and
    defaults the grid to UTM like the `stack_stats` tool. See the CHANGELOG.
- ~~**2 & 3. Mode A (precompute in CI, serve static).**~~ **shipped** —
  `umbra showcase --narrate` narrates each featured `change` site at build time
  and bakes the result into the page: a summary under the tile plus a
  `featured/<slug>.narration.json` sidecar with the dB grid it cites, so the
  static Pages visitor reads a cached narration with no live model call and no key
  near the browser. It reuses the same narration Mode B ships (it narrates the
  *same* two passes the composite shows, `select_change_frames` for both), is an
  injectable `featured_narrator` seam on `assemble_showcase` (offline-testable,
  like `featured_renderer`), and is gated so a keyless build or a non-`change`
  view skips cleanly rather than failing the deploy. `docs.yml` passes the repo
  model-key secret to the main-only showcase build and runs `--narrate`; a fork PR
  (no secrets) ships the gallery without readings. See the CHANGELOG. What is
  still open, and smaller:
  - **Narration is baked for the `change` view only.** `timescan` (whole series)
    and `swipe` (an interactive page) have no single two/three-date pair for the
    model to read, so `_default_featured_narrator` returns `None` there and the
    CLI says so. A per-view reading (e.g. a whole-series summary for timescan)
    would be a different prompt and a different grounding; it waits for a view
    that wants one.
  - **The bake uses `select_change_frames`, not `best_change_interval`.** The
    reading is of the frames the composite *shows* (so picture and words agree),
    which is the honest demo; the speckle-clearest interval the selector would
    pick can differ. Grounding it on `best_change_interval` instead would mean
    also rendering the composite of the selected pair, so the two still agree —
    worth doing only if a featured site's shown pair turns out to be a poor
    read.

Security note (the maintainer's question, recorded so the decision is not
re-litigated): storing the model key as a **GitHub Actions secret is correct and
already the established build-time pattern** (encrypted, masked, not exposed to
fork PRs) — it is exactly how the scene-embedding step is keyed. What it does
**not** support is a *static* Pages site "querying through that key" directly: a
secret shipped to a browser is a published secret. Mode B (shipped) holds the key
**server-side** in `umbra serve` and never sends it to the browser, so a static
front end calls the server, not the model — which is the only way to key *live,
arbitrary-scene* querying. Mode A (deferred) is the zero-exposure alternative
that keeps every model call in CI and serves cached results.

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
- ~~**Only `umbra` invocations are checked.** The workflows also call `gh`,
  `python -c` and `pip` with arguments that can drift (the `python -c` in the
  tiling step imports `umbra_py.pmtiles.save_viewer` and
  `constants.CATALOG_INDEX_PMTILES_URL` by name, so a rename there breaks the
  same run and no test would notice).~~ **shipped for `python -c`** — the same
  suite now extracts every `python -c` body (quote-aware, so a snippet's own `;`
  and `|` are not mistaken for shell operators), compiles it (a syntax error is
  drift), and resolves every name it reads from `umbra_py` against the installed
  package: an `import umbra_py.x` that no longer resolves and a `p.save_viewer`
  whose attribute was renamed both fail a pull request, which is the exact class
  of break that would kill the weekly publish while the Click parse stayed green.
  It is the "import the names" option this entry sketched, and it stays offline —
  it imports only the `umbra_py` modules the snippets name (all stdlib-only
  today), and an import that fails for want of an *optional* dependency is treated
  as an absent extra in the core `[dev]` test job rather than as drift (told apart
  by which module `ModuleNotFoundError` reports missing). `test_a_renamed_library
  _symbol_would_be_caught` pins the `save_viewer` rename the way
  `test_the_drift_that_broke_the_publish_would_be_caught` pins the `--db` typo,
  and `test_the_scan_actually_found_the_python_snippets` guards against a scanner
  that silently matches nothing. See the CHANGELOG. What is still open, and
  smaller:
  - **`gh` and `pip` invocations are still unchecked.** Their arguments can drift
    too, but neither references a library symbol the way the `python -c` bodies
    do, so a break there is a workflow-syntax problem a run surfaces rather than a
    silent rename. Add a scan for them only if one ever bites.
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
- ~~**`mypy` disagrees between the hook's environment and CI's.**~~ **shipped** —
  the disagreement was that CI's `type-check` job installed only `[dev]`, so
  Pillow was absent and import-ignored, while the hook (and `test-all-extras`)
  install every extra, so Pillow's stubs *were* checked and
  `viz/composites.py`'s `Image.ADAPTIVE` read as `[attr-defined]` — a failing
  `mypy` line on every stubs-present dev/agent session even though CI was green.
  Two changes close it at the root: the call site now references
  `Image.Palette.ADAPTIVE` (the real typed attribute Pillow's own internals use,
  clean under both environments — not a `cast`/`# type: ignore`, which
  `warn_unused_ignores`/`warn_redundant_casts` would have flagged in whichever
  environment made it redundant), and a new `type-check-all-extras` CI job runs
  the same `mypy` with all extras installed, the type-check mirror of
  `test-all-extras`, so a misuse of a stub-bearing extra fails a PR instead of
  greeting the next agent session. See the CHANGELOG.

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
- ~~**`umbra chips` exposes the flag but not the check.**~~ **shipped** —
  `umbra chips --skip-unsupported` / `write_chips(skip_unsupported=True)` carries
  on past an acquisition whose own metadata cannot support the request and
  records it on `ChipDataset.skipped` (a `SkippedAcquisition`: which pass, when,
  the product's own words for why, and the refusal's hint), so a batch over a
  mixed archive reports which ones could rather than dying on the first that
  could not. What made it safe to do at all is the *type*: the five
  product-metadata refusals in `convert.py` now raise
  `UnsupportedMeasurementError` (an `UmbraError` **and** a `ValueError`, so no
  existing caller changed), which is the only exception the skip catches — a
  download failure or a corrupt product still ends the run. The check also moved
  ahead of the pixel read (`_check_measurement_support`, called where
  `_check_speckle_window` already was), so a scene that cannot answer costs its
  header rather than a whole complex read. What is still open, and smaller:
  - ~~**The skips are in the summary, not in the manifest.**~~ **shipped** — a
    run that left anything out writes a `skipped.jsonl` sidecar beside the
    manifest (`write_skipped_manifest`, `write_chips(skipped_manifest=…)`,
    `ChipDataset.skipped_path`, and `skipped_manifest` in the `--json` payload):
    one JSON object per left-out pass, `SkippedAcquisition.to_dict()` verbatim,
    so a training loader reading `out_dir` sees what the run reported to whoever
    was watching it. The shape this entry predicted is the shape that shipped —
    a sidecar rather than manifest rows, since a skipped acquisition has no chip
    and would be a record with no path, bbox or transform in a one-row-per-chip
    schema — plus two decisions it did not make: it is always `.jsonl` whatever
    format the manifest is (the three manifest formats describe *tiles*), and it
    is written only when there is something to record, so a clean run leaves
    exactly the files it left before and the file's presence is itself the
    statement. What is still open, and smaller:
    - ~~**The sidecar carries no footprint.**~~ **shipped** —
      `SkippedAcquisition.bbox` carries the acquisition's own footprint
      (`UmbraItem.bbox`, EPSG:4326 `[min_lon, min_lat, max_lon, max_lat]`),
      populated from the item that is in hand at both skip points (the
      conversion-refusal and the preflight-drop), so the sidecar locates a hole
      in space as `datetime` locates it in time. That answers the question this
      entry deferred *from the directory itself* — a loader reconstituting a time
      series over an area of interest can tell a hole that falls over its site
      from one that never overlapped it without re-running the search that
      produced the selection, which is the case a self-describing artifact exists
      for. It is a required-nullable field on `chip-skipped.schema.json` (null
      when the source item stated no footprint, so absence is a value rather than
      a missing key), emitted as the four-number list a `ChipRecord.bbox` already
      is. See the CHANGELOG.
    - **Nothing reads it back.** There is no `read_skipped_manifest` to pair
      with the writer, because the file is one JSON object per line and a
      loader's own `json.loads` is the whole reader. Add one only if a
      `ChipDataset` ever needs to be reconstituted from a directory.
  - ~~**The check is per acquisition, discovered one at a time.**~~ **shipped** —
    `umbra preflight` / `sicd_capabilities` / `preflight_items`
    (`src/umbra_py/preflight.py`) walk the NITF's own fixed-width file header to
    locate the XML data extension segment and fetch it with two HTTP range
    requests, so "which of these twenty can be calibrated?" costs tens of
    kilobytes per acquisition instead of the product. The verdict is
    `convert._check_measurement_support` applied to an attribute view over the
    parsed XML, so it cannot become a second opinion about what a product
    supports. Stdlib + `requests`: no `sarpy`, no `numpy`, no extra. What is still
    open, and smaller:
    - ~~**Nothing wires the preflight into the batch.**~~ **shipped** — `umbra
      chips --preflight` / `write_chips(preflight=True)` reads each acquisition's
      metadata by range request before anything is downloaded and drops the
      passes that cannot answer. The design question this entry deferred — a
      batch that silently drops scenes is the failure mode `--skip-unsupported`
      avoided — was answered by making a preflighted drop *the same object* as a
      survived refusal: a `SkippedAcquisition` on `ChipDataset.skipped` with the
      product's own words and its hint, plus one new field, `stage`
      (`"conversion"` / `"preflight"`), since when the refusal was found is the
      only thing the two routes do not share. The settings come from the run's
      own `SicdConversion`, so the preflight asks the conversion's question by
      construction. `ChipDataset.preflight` (`chips.PreflightSummary`) reports
      what asking cost against the download it removed, counting only the dropped
      products as saved. What is still open, and smaller:
      - ~~**A read failure is kept, and that is a policy rather than a fact.**~~
        **shipped** — and the shape it took is not the `strict` mode this entry
        sketched. A flag would have made the caller choose between two policies;
        what was actually missing is that "could not be read" named two
        different pieces of news, and only one of them is a policy question at
        all. `UnreadableProductError` names the other: the item lists no such
        asset, nothing is at the href (HTTP 404/410, or a local path that is not
        there), what is there is not a NITF, is a NITF this cannot lay out, or
        carries no SICD XML that parses. Each is final, and dropping such a pass
        needs no flag because keeping it was never the cautious choice — it fails
        inside `chip_item` as a plain read error, which `skip_unsupported`
        deliberately does not catch, so a preflighted run ended on an
        acquisition its own preflight had ruled out. `PreflightResult.error_scope`
        (`"product"` / `"transport"`, plus `.readable` / `.final`) carries the
        classification, `_error_scope` sorts by type with everything
        unrecognised counted as transport, and `PreflightSummary.missing`
        counts the drops apart from the kept `unreadable`. What is still open,
        and smaller:
        - **Nothing retries a transport failure beyond the session's own.**
          `_http.default_session` retries 429/5xx and connect/read errors three
          times with backoff, so a blip is already ridden out before the
          preflight sees it; what is left is a failure that outlived those, and
          a per-item retry on top would be a second policy over the same wire.
          The pass is kept, so the cost of not retrying is a download rather
          than a hole. Worth adding only if a real selection turns out to have
          a failure mode the session's retries systematically miss.
        - **`403` is transport, deliberately.** It can be a proxy, a signing or
          a bucket-policy problem as easily as an absent object, and the two are
          not distinguishable from the status alone. Guessing "product" there
          would drop a real pass, which is the one error this design will not
          make; guessing "transport" costs a download. If the open bucket ever
          starts answering `403` for objects that genuinely do not exist, the
          fix is a probe rather than a reclassification.
        - **`umbra preflight --json` reports the scope, nothing consumes it but
          the chipper.** `error_scope` / `missing_count` / `unreadable_count`
          are in the payload for an agent to branch on, but no other surface
          reads them — `POST /artifacts/*` does not preflight, and `umbra
          convert` operates on one product where the distinction is the
          difference between two error messages.
      - ~~**The preflight is serial.**~~ **shipped** — `preflight_items(workers=…)`
        (`DEFAULT_PREFLIGHT_WORKERS`, 8) reads the selection through a small thread
        pool, with `umbra preflight --workers` and `umbra chips
        --preflight-workers` on the two front doors. The check spends round trips
        rather than bytes, so a 40-pass site was 40 sequential latencies in front
        of a batch whose whole point is not paying twice — the one cost the
        preflight itself added. What makes it safe to widen is that the
        concurrency is a *schedule*, not an answer: the reads are independent, the
        verdicts are consumed in the order they were asked in (the chip run pairs
        them against its own selection positionally, so completion order would
        attach one pass's refusal to another), and `progress` is still called once
        per acquisition, in that order, from the calling thread — so a CLI
        callback never has to be thread-safe. `workers=1` runs no pool at all, and
        the lane count is capped at the number of products, so a one-scene
        preflight is the code that shipped before. `PreflightReport.workers`
        records the width it actually used. What is still open, and smaller:
        - **The lane count is not bounded by the session's connection pool.**
          Eight matches the catalog walk's fan-out and sits inside
          `_http._POOL_SIZE` (16), but nothing stops `--workers 64`, which would
          churn connections rather than reuse them. Clamp it against the pool
          size, or grow the pool with the request, if anyone finds a reason to ask
          for that many.
        - **A slow read still holds up the progress lines behind it.** Consuming
          in selection order is what keeps the verdicts pairable, so the line for
          pass 3 waits on pass 2 even when it finished first. The reads
          themselves do not wait — only the printing does — so this is cosmetic
          until someone wants a live counter rather than a roster.
      - **Only `umbra chips` has it.** `umbra convert` operates on one product,
        where the preflight's saving is one download and the refusal is already
        cheap, so it was left alone. The other batch-shaped consumer is
        `umbra serve`'s artifact endpoints, which do not convert.
    - **The reader knows NITF 2.1 only.** NITF 2.0's security fields are a
      different length, so every subsequent offset would land somewhere
      arbitrary; it is refused by name rather than guessed at. SICD mandates 2.1,
      so nothing in this archive needs the older layout — add the second field
      table if a product ever turns up that does.
    - **A `yes` on a product that carries the polynomials needs `numpy`.** Every
      metadata gate runs first (so every *refusal* is answerable from a core
      install, which is the case the open archive is), but confirming support
      reads the coefficients through `convert`'s own reader, which requires the
      `[convert]` extra the conversion needs anyway. Splitting the presence check
      from the coefficient parse would remove that, at the cost of the shared
      code path that is the whole reason the two cannot disagree.
    - ~~**Only the two `Radiometric` questions are asked.**~~ **shipped** —
      `umbra preflight --rtc` / `preflight_items(rtc=True)` asks the third
      metadata-dependent correction's question too, and
      `SicdCapabilities.look_geometry` reports the scene-centre
      `(incidence, azimuth)` a product states. `umbra chips --preflight` picks it
      up from the run's own `SicdConversion.rtc`. What is still open, and
      smaller:
      - **A polarization / looks read is still not asked.** The view carries the
        whole SICD XML, so either is a few lines — they wait for a caller that
        wants to *select* on them, which is a different question from "can this
        product answer?" and would want its own place in the report.
      - **`--rtc` also needs a DEM, which no preflight can check.** The
        geometry half is a fact about the product and is now asked; the DEM half
        is a fact about the request (a path, or `auto`'s network fetch), so a
        cleared pass can still fail for want of terrain. Nothing pretends
        otherwise, but the report says "supports --rtc" where it means "states
        the geometry --rtc needs".
  - ~~**Only the two metadata-dependent corrections are typed.**~~ **shipped** —
    `_scene_look_geometry` raises `UnsupportedMeasurementError` rather than a
    bare `ValueError`, so a missing `SCPCOA` is skippable by `umbra chips
    --skip-unsupported`, reaches the `--json` error envelope with a hint, and is
    asked off the metadata (`_check_measurement_support(rtc=True)`) before the
    complex read, the DEM fetch and the warp it would otherwise have refused
    after. It stays a `ValueError` too, so no existing caller changed.

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
- ~~**Nothing says how much speckle is *left*.**~~ **shipped** — see the
  detection-floor entry below, which is where its follow-ons live.
- **The filter runs on the whole window in memory.** `_box_sum`'s summed-area
  table makes the cost independent of the window size but holds a scene-sized
  float64 table, on top of the scene-sized power array `_detected_power` already
  makes. That is the same order the noise estimators already work at, and
  `--clip-bbox` is the answer for a scene too large to hold; a tiled
  implementation (overlapping windows, one strip at a time) is the fix if the
  whole-scene path becomes the common one.

---

## Speckle detection-floor follow-ons (`stack_stats`'s `detection` shipped)

- **Surfaced in:** the detection-floor PR (`STRATEGY.md` 5.5, which the
  speckle-filtering entry above named).
- **Code:** `src/umbra_py/_specfun.py` (`trigamma`,
  `regularized_incomplete_beta`), `src/umbra_py/load.py`
  (`DETECTION_FALSE_ALARM_TARGET`, `DETECTION_EXCESS_WARN`,
  `_DETECTION_MAX_LOOKS`, `_DETECTION_MAX_THRESHOLD_DB`, `_DB_PER_NEPER`,
  `_speckle_change_sigma_db`, `_speckle_false_alarm`, `_detection_threshold_db`,
  `_LooksAccum`, `_detection_floor`, the two caveats and the `looks` field on
  both measurement walks, and — for the per-block floor — the `detection=`
  parameter on `_spatial_breakdown`, each block's `detection` sub-record and
  `peak_block.stands_clear`), `docs/schemas/stack-stats.schema.json`
  (`$defs/detection`, `$defs/blockDetection`, `$defs/pass.looks`,
  `$defs/peakBlock.stands_clear`), `tests/test_specfun.py`.

`stack_stats` reports what speckle alone would have done to the change it just
measured: each pass's `looks` read off the cube's own blocks, and a `detection`
block giving an unchanged cell's decibel spread, the false-alarm fraction at the
requested threshold, and the threshold that would hold it to 5 %. Both figures
are exact rather than approximated — the gamma/beta forms, computed in stdlib
`math` — and the whole thing is validated against simulated speckle and against a
cube of two realisations of one unchanged surface. Follow-ons, none a blocker:

- **The floor is per cell, and the observed fraction is over correlated cells.**
  `false_alarm_fraction` is an exact per-cell probability, so it is the right
  expectation for the observed share whatever the spatial correlation — but the
  *scatter* of the observed share around it is not, because neighbouring cells of
  an oversampled product are not independent. That is why the advisory uses a
  stated margin (`DETECTION_EXCESS_WARN`) rather than a significance test: a
  proper one needs an independent-cell count nothing here measures. An
  autocorrelation read (the ratio of a block's variance to its decimated
  variance) would supply it, and would also sharpen `looks`; it wants a real
  product to calibrate against rather than an argument.
- **One floor per cube, not one per interval.** The representative `looks` is the
  median of the passes that gave a reading, so a series whose passes genuinely
  differ (one filtered, one not — which `to_stack` would refuse — or simply one
  much noisier) is described by a middle value. Every pass keeps its own `looks`,
  so the disagreement is visible; a per-interval floor would use the pair's two
  looks in the unequal-shape form `regularized_incomplete_beta` already supports.
  It waits for a consumer, since the threshold it would be reported against is
  one number for the whole cube.
- ~~**The floor does not reach `spatial`.**~~ **shipped** — a `blocks=N`
  breakdown now carries the floor per block: each block that had two comparable
  passes gets a `detection` sub-record — the cube-wide per-cell
  `false_alarm_fraction`, the block's own `compared_cells`, and whether its net
  `changed_fraction` `stands_clear` of the floor by the same
  `DETECTION_EXCESS_WARN` margin the cube-level advisory uses — and `peak_block`
  gains a `stands_clear` so the headline mover carries its own verdict (the
  biggest block-mover can still sit inside the floor, which is exactly the case a
  reader needs told). The shape is the one this entry sketched, "the floor plus
  that block's cell count", and the cell count travels *with* the flag on purpose:
  the floor is an exact per-cell expectation whatever a block's size, but a block
  is measured over far fewer cells than the scene, so its observed share scatters
  more widely around it — a bare `stands_clear` would be the "reading a block's
  excess as a finding when it is sampling" trap this entry named, so the caveat
  and the block record both say to read `stands_clear` together with
  `compared_cells`. It closes the loop the summary already advertised: the
  "does not stand clear … read the spatial breakdown for a block where the change
  does" caveat now points at a breakdown that carries the floor it names. What is
  still open, and smaller:
  - **The block flag is the margin heuristic, not a significance test.** It
    reuses `DETECTION_EXCESS_WARN` exactly as the cube-level `stands_clear` does,
    so it inherits the same limit named in the "per cell / correlated cells"
    entry above: a proper per-block significance test needs an independent-cell
    count nothing here measures (the block's `compared_cells` overstates it for an
    oversampled product), and an independent-cell binomial would be
    anti-conservative in exactly the wrong direction. Exposing the cell count is
    the honest half that can ship without it; the significance test waits for the
    same autocorrelation read `looks` does.
- ~~**Nothing reports the floor on the composite path.**~~ **shipped** —
  `umbra change --narrate` quotes a signed dB delta per block and grounds a model
  on it, which is the other surface where "is this bigger than speckle?" is the
  reader's first question. `narrate.ChangeStats` now carries a `detection` block
  (`narrate._change_detection_floor`), read off the two co-registered passes the
  grid is differenced between: each pass's looks via `convert._estimate_enl` of
  its detected power, reduced by `load._detection_floor` — the cube's own
  functions rather than a second implementation, so the shape is
  `docs/schemas/stack-stats.schema.json`'s `$defs/detection` exactly and a reader
  parses one contract for the cube and the composite alike. It reaches the model
  (the block is in `build_narrate_messages`'s scene card and the system prompt
  now teaches the floor as the bar a change must clear) and the reader (a
  `ChangeNarration.to_text` line says whether the observed `changed_fraction`
  stands clear of it). Validated the same way `stack_stats` was: on two
  single-look realisations of one unchanged surface the predicted
  `false_alarm_fraction` lands on the observed `scene_changed_fraction`. What is
  still open, and smaller:
  - **The floor is scene-wide, not per block.** Like the cube's, it describes the
    pair as a whole; a block's `mean_delta_db` is weighed against the scene
    `cell_sigma_db` in the prompt but no per-block floor is emitted, for the same
    reason `stack_stats`'s does not reach `spatial` (a block's share is far
    noisier around the floor). The natural form is the floor plus each block's
    valid-cell count, and it waits for the same consumer.
  - **Looks is read off each whole pass, not windowed.** The composite holds both
    bands in memory (the render already does), so there is no ceiling to lift and
    `_LooksAccum`'s mergeable-histogram path is not needed here; a per-pass
    `_estimate_enl` of the whole band is the exact read. If a clipped narration
    ever streamed its passes, the windowed accumulator is the drop-in.
- **`_DETECTION_MAX_LOOKS` is a numerical guard doing a physical job.** A cube
  whose blocks are numerically uniform (a synthetic raster, a heavily quantised
  one) reads unbounded looks, and the cap keeps the beta integral inside double
  precision. It changes no answer that was ever meaningful — the false-alarm
  fraction is already below `1e-50` there — but a cube pinned at the cap reports
  `looks: 1024`, which is a true statement about its cells and a strange-looking
  one. Reporting "no measurable speckle" instead would mean a fourth state in the
  block; it waits for someone to hit it on real data.
- **The looks read is a median over blocks, so the two measurement walks can
  disagree in the last decimal.** Blocks are cut from whatever array is in hand,
  and `windowed=True` hands it windows — so a window narrower than the 16-cell
  block finds none where a whole slice finds several. `looks` is a read of the
  scene rather than one of the exact sums beside it (the same status
  `umbra convert`'s ENL pair has), and the docstring says so. Aligning the blocks
  to the shared grid rather than to the array would remove it, at the cost of a
  block-offset argument threaded through `_block_enl_ratios`.

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
- ~~**Nothing reports what a clip saved.**~~ **shipped** — `umbra convert
  --clip-bbox` prints a `clipped` line pricing the pixels read against the pixels
  the whole product holds and the ratio (`read 480,000 of 4,000,000 scene px
  (12.0%)`), so the flag's value is visible at the moment someone is deciding
  whether to use it. The figure comes from a new non-breaking `clip_report`
  callback on `sicd_to_geocoded_cog` (a `ClipSavings` frozen dataclass, invoked
  once before the read; a caller who does not pass it is unchanged) rather than
  from the `UMBRA_*` tags, since a clip changes which ground is written — which the
  geotransform already states — not what a pixel value means, so it stays out of
  the provenance keys for the same reason the entry above records. What is still
  open, and smaller:
  - ~~**`umbra chips --clip-bbox` does not report it.**~~ **shipped** — a clipped
    chip run rolls up what each acquisition read onto `ChipDataset.clip` (a new
    `chips.ClipSummary`): total window vs total scene pixels, the overall fraction,
    and the per-scene `min`/`max` fraction. `umbra chips` prints it
    (`clipped: read … across N scene(s) (…%)`, the batch form of the single
    conversion's `clipped` line, via `_echo_chip_clip_report`) and `--json` carries
    a `clip` block, published as a conditional key on
    `chip-dataset.schema.json` — present only when the run was clipped, so an
    ordinary run's payload is unchanged. Both loader paths reach it through one new
    `chip_item(clip_report=…)` callback (mirroring `sicd_to_geocoded_cog`'s): the
    `GEC`/`CSI` half is priced in `chip_item` from the tile window against the
    source raster's own size, and the `--asset SICD` half rides the callback down
    through the default `_prepare_sicd` into the conversion, which is what knows the
    whole-scene size the already-clipped COG no longer carries. Counted per
    acquisition like the noise and speckle roll-ups, but *accumulated* during the
    run rather than derived from the records — the clip saving is deliberately
    neither a `ChipRecord` field nor a `UMBRA_*` tag, so there is nothing in the
    manifest to derive it from. See the CHANGELOG. What is still open, and smaller:
    - **A `SICD` prepared by a custom `preparer` or a `--work-dir` cache hit
      reports no clip saving.** The callback is only wired to the default
      `_prepare_sicd`, since the public `SicdPreparer` signature has no place to
      report through, and a cache hit runs no conversion to price (the saving was
      on the run that built the COG). Both are the honest failure mode — a run over
      freshly-converted scenes reports fully — but a custom-preparer caller who
      wants the number would need the seam widened to carry a report.

---

## Provenance-consuming follow-ons (`to_stack` refuses mixed conversions)

- **Surfaced in:** the provenance-consumption PR (`STRATEGY.md` 5.5, which the
  DEM entry above named).
- **Code:** `src/umbra_py/load.py` (`MEASUREMENT_PROVENANCE_KEYS`,
  `_source_provenance`, `_comparable_record`, `_shared_provenance`,
  `_as_geotiff_tags`, `stack_provenance` / `StackProvenance` /
  `ProvenanceGroup` / `UnreadableSource`, the
  `provenance` attr on `to_xarray` / `to_stack` and key on `stack_stats`),
  `umbra stack --provenance` + `_echo_stack_provenance` / `_provenance_label`
  in `src/umbra_py/cli/process.py`,
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
- ~~**The refusal is discovered by hitting it.**~~ **shipped** —
  `stack_provenance` / `umbra stack --provenance` reads each source's `UMBRA_*`
  record from its raster header, groups the selection by
  `MEASUREMENT_PROVENANCE_KEYS` and reports whether `to_stack` would accept it,
  before the grid, the warp or a single pixel. The shape this entry sketched is
  the shape that shipped, plus the half it did not ask for: the refusal's own
  advice ("use only the acquisitions that share one") is now a subset with URLs
  attached (`StackProvenance.largest`), so it is a command rather than a
  diagnosis. The verdict is `_shared_provenance`'s own, called on the same
  records, and the grouping runs on `_comparable_record` factored out of it — so
  a cleared selection cannot then be refused by the stack it cleared. What is
  still open, and smaller:
  - ~~**`POST /artifacts/stats` still reports the mix only as its `400`.**~~
    **shipped** — `POST /artifacts/provenance` (and the `stack_provenance` agent
    tool beside it) asks the question from the surfaces that answer for people
    who installed nothing. The shape this entry predicted is *not* the shape
    that shipped, and for a reason worth recording: the landing page reports
    what an **instance** supports, which is a fact about the server and belongs
    in a document fetched once, whereas a selection's conversions are a fact
    about the request and can only be answered per request. So it is a route
    rather than a field — but a route that takes `/artifacts/stats`'s own body
    and vets it through the same `stats_frames`, which is what makes it a
    preflight *of that request* rather than of a lookalike selection. Three
    decisions it made: a mix answers `200` (reporting the mix is the point; the
    `400` it quotes is still what `/artifacts/stats` gives), it is uncached (the
    read is kilobytes, and a re-converted source is exactly where a
    content-addressed answer goes stale), and it is not routed through the
    injectable `renderers` (an injectable provenance would be the second opinion
    the whole construction rules out). What is still open, and smaller:
    - **A hosted instance is still the missing half.** The endpoint makes the
      preflight free for a client with nothing installed, which is only worth
      something once a public `umbra serve` exists — the follow-on the
      `umbra serve` section below tracks. Nothing here waits on it.
    - **`/artifacts/change`, `timescan` and `swipe` have no preflight.** They
      draw rather than measure, so they never refuse a mix in the first place
      (the tolerance `_shared_provenance`'s `action` argument records). The one
      composite-path caller that *does* quote decibels is
      `render_change_png`, and it has no HTTP surface — see the composite entry
      below.
    - ~~**The response is not in `docs/schemas/`.**~~ **shipped** —
      `docs/schemas/stack-provenance.schema.json`, beside the two other
      measurement documents (`stack-stats`, `preflight`) the same PR published.
      See the schema-contract entry below for what is still open there.
  - **The search-side commands still don't report conversions.** This entry's
    original suggestion — list the distinct `UMBRA_CALIBRATION` values in a
    selection the way `umbra search` reports polarizations — is orthogonal to
    the preflight and still not done. It would cost a header read per result on
    a command whose whole promise is that search is metadata-only, which is why
    the preflight lives on the command that was going to open them anyway.
  - **Nothing preflights the *composite* path.** `render_change_png` applies the
    same refusal to the pair it quotes decibels between, and pays the
    co-registration first (see below). `stack_provenance` would answer for a
    pair as readily as for a series; wiring it into `umbra change --narrate`
    would need the pair in hand before `_coregister_bands` opens them, which is
    the same ordering question that entry already describes.
- **A cube's provenance reaches the render manifest only inside `stats`.**
  `umbra stack --json` emits the shared `{output, items_used, parameters}`
  manifest (`docs/schemas/render-manifest.schema.json`), which has no provenance
  field; the record is present only when `--stats` was also asked for. Adding it
  to the manifest would mean a schema revision, so it waits for a consumer.

---

## Published JSON-contract follow-ons (`docs/schemas/` + `tests/test_schemas.py`)

- **Surfaced in:** the schema-contract PR (`STRATEGY.md` §8, design principle 5).
- **Code:** `docs/schemas/` (`stack-stats`, `stack-provenance`, `preflight`,
  `chip-dataset`, `chip-record`, `chip-skipped`, `item-context`,
  `scene-description`, `search-plan`, `watch-delta`, `task-matches`,
  `scene-matches`, and the `$ref`s from `render-manifest`, `chip-dataset` and
  `watch-delta`), `tests/test_schemas.py`, the `jsonschema` entry in `[dev]`, the
  `--help` pointers on `umbra stack --stats / --provenance`, `umbra preflight
  --json`, `umbra chips --json`, `umbra info / describe / ask / watch --json` and
  `umbra semantic search / embed similar / embed search --json`.

Sixteen schemas now, each strict and each validated against a payload from the
surface that emits it, plus the meta checks (valid draft 2020-12, `$id` matches
filename, the README table names every file and no file it does not, and every
`examples` entry validates against the subschema it sits on). Every `--json`
shape the CLI emits is published. Follow-ons, none a blocker:

- ~~**`umbra chips --json` has no schema.**~~ **shipped** — `chip-dataset`,
  `chip-record` and `chip-skipped`, three documents because a chip run has three
  consumers (the `--json` summary, the manifest a loader reads line by line, the
  `skipped.jsonl` sidecar in the directory), with the summary `$ref`ing the
  sidecar's schema for its own `skipped` entries. What is still open there:
  - **The `.geojson` manifest's envelope is unschema'd.** Its feature
    `properties` are validated against `chip-record.schema.json`, but the
    `FeatureCollection` around them carries two non-standard top-level keys
    (`license`, `attribution`). GeoJSON allows foreign members, so nothing is
    wrong; a consumer reading the *file* rather than the records simply has no
    contract for the wrapper. One small schema `$ref`ing the record, if a
    consumer ever wants it.
  - **The `.parquet` manifest is a schema nothing describes.** It is
    stac-geoparquet (each chip as a STAC Item row), so its contract is
    stac-geoparquet's rather than this project's — but the mapping from
    `ChipRecord` fields to Item `properties` is this project's, and it is
    described only by `_chip_to_stac_item`.
- ~~**Several smaller `--json` surfaces still have no schema.**~~ **shipped** —
  `item-context`, `scene-description`, `search-plan`, `watch-delta`,
  `task-matches` and `scene-matches`. Six rather than the four this entry listed,
  because the entry's own parenthetical was wrong: `umbra info --json` does *not*
  emit the source STAC item, it emits `UmbraItem.to_llm_context()` — an explained
  reading of one, and the single most-read document in the library, since the
  agent tools return it and a watch delta carries one per new acquisition. So it
  is this project's contract rather than STAC's, and `watch-delta` `$ref`s it
  instead of restating the card. What is still open, and smaller:
  - **A `SceneImage` records the request's `max_size`, not the render's own
    ceiling.** A rendered quicklook is capped by what the COG's overviews can
    supply, so `width` can come in under `max_size` for a reason that has nothing
    to do with a baked preview being small — and a consumer reading the pair as
    "rendered means full size" would mis-attribute it. Both numbers are in the
    document, so this is a caveat on interpreting them rather than a missing
    field; recording *why* they differ would mean the renderer reporting what it
    was capped by.
  - **The watch delta's `query` is open by construction.** Unset filters are
    dropped rather than emitted as nulls, so the object carries whichever of the
    search's parameters the run actually used and the schema cannot close it
    without freezing the search signature into a contract. Naming the known keys
    as optional properties while staying open would document them without
    constraining them — worth doing if a consumer starts branching on the echo
    rather than on `new_items`.
  - **`umbra ask --json` emits a plan and then, with `--run`, a raw STAC item per
    line.** Only the plan is schema'd; the item lines are the source documents,
    whose contract is STAC's. That the stream is a plan object followed by
    newline-delimited items is a shape nothing describes, because it is a
    concatenation rather than a document. Same for `umbra semantic search --run`.
- ~~**`umbra serve` has an OpenAPI document and the schemas do not appear in
  it.**~~ ~~**The schemas live in `docs/`, so a wheel does not carry them.**~~
  **shipped, both** — they were one item: the document could not carry a
  contract nothing in `src/` could load. `umbra_py.schemas`
  (`load_schema` / `schema_names` / `schema_path`, stdlib only) reads them, the
  wheel carries a copy of `docs/schemas/` as package data
  (`umbra_py/_schemas/`, via `[tool.hatch.build.targets.wheel.force-include]`)
  while the directory stays the one home its `$id`s name, and
  `serve.openapi_components()` merges the three contracts the artifact routes
  emit into the generated document as `StackStats` / `StackProvenance` /
  `RenderJob` with each route's `responses=` `$ref`ing one. What is still open,
  and smaller:
  - **The packaged copy is checked by a parse in the suite, and by Docker in
    CI.** `test_the_wheel_ships_the_schemas_where_the_accessor_looks` reads
    `pyproject.toml` and asserts the `force-include` target matches
    `schemas.PACKAGE_DATA_DIR`, because every environment in the Python matrix
    installs editable and so exercises only the *fallback* branch. What a parse
    cannot see is a build *context* that lacks the files: `docker.yml` caught
    exactly that on the first run of this change, since the image copied only
    `pyproject.toml`, `README.md` and `src/`, and a `force-include` is mandatory
    — `FileNotFoundError: Forced include not found: /app/docs/schemas`, before
    a line of Python ran. That is the right failure (an installed package has no
    checkout to fall back to, so an image without the schemas would raise on
    `/openapi.json` instead), and the `Dockerfile` + `.dockerignore` now carry
    `docs/schemas` deliberately. Between the two, the packaged branch is
    exercised end to end by the Docker smoke test rather than by the suite;
    building a wheel inside the suite would bring the check closer, at the cost
    of a build backend in the test path.
  - **The copy means `docs/schemas/README.md` ships inside the wheel too.**
    `force-include` takes the directory, so the reader's table is package data
    as well. Harmless (a few KB) and arguably useful next to the files it
    describes; excluding it would mean listing the schemas individually, which
    is the drift the directory-level include avoids.
  - **Only the three schemas the artifact routes emit are components.** The
    other fourteen describe CLI and agent-tool surfaces this server does not
    have, so publishing them in *its* OpenAPI document would be a claim rather
    than a contract. Add one when a route starts emitting it.
  - **A cross-file `$ref` cannot be inlined.** `_rewrite_refs` refuses one
    rather than emitting a reference no client can resolve, so publishing
    `render-manifest` or `watch-delta` as a component would mean publishing its
    target as a component first and pointing the ref at it. A few lines when
    something needs it; deliberately not written on speculation.
- ~~**Nothing checks that a schema's `examples` validate against its own
  schema.**~~ **shipped** — `tests/test_schemas.py` walks every schema in
  `docs/schemas/` and validates each `examples` entry against the subschema it
  sits on, at every depth, resolving `$defs` and cross-file `$ref`s through the
  same registry the payload checks use — so a `examples` value that drifts from
  the shape it illustrates (an enum value renamed, a number turned string, a
  field a strict schema no longer allows) fails the build rather than misleading
  a consumer who copies it. Two self-tests keep the check from going vacuous
  (that it found a real corpus, and that a deliberately-drifted example is
  caught). The clause this entry left ("worth it once an example is more than a
  one-line value") is now met: nine consumer-facing contracts (`error`,
  `download`, `index-info`, `render-manifest`, `render-job`, `task-matches`,
  `scene-matches`, `chip-skipped`, `stack-provenance`) gained a top-level
  whole-document `examples` entry — a complete, checked instance a consumer can
  parse against — and the check validates those against the whole schema too.
  See the CHANGELOG. What is still open, and smaller:
  - **The complex documents still carry property examples, not a whole-document
    one.** `stack-stats`, `chip-dataset`, `chip-record`, `item-context`,
    `scene-description`, `search-plan`, `preflight` and `watch-delta` are large,
    conditional shapes whose most trustworthy example is a real emitted payload —
    which the suite already validates from the surface that produces it. A
    hand-authored whole-document example for each would be checked by the same
    loop; it waits for a case where the real-payload fixtures are not example
    enough for a reader.

---

## Register `umbra-mcp` in the MCP registries and Anthropic's directory

- **Surfaced in:** the `umbra-mcp` MCP server PR.
- **Code:** `server.json`, `tests/test_mcp_registry.py`, the `publish-mcp` job in
  `.github/workflows/release.yml`, `src/umbra_py/mcp_server.py`, `pyproject.toml`
  (`[mcp]` extra, `umbra-mcp` console script).

The server itself is shipped and runnable (`umbra mcp` /
`uvx --from 'umbra-py[mcp]' umbra-mcp`), the agent-framework reach trilogy
(MCP → LangChain → LlamaIndex) is complete, and the registry half is now
plumbing rather than a project: `server.json` is the manifest, the `publish-mcp`
job submits it after the PyPI upload of a release, and
`tests/test_mcp_registry.py` derives the command from `pyproject.toml` so the
manifest, the README, `llms.txt` and the module docstrings cannot state
different commands. ~~The invocation every one of those surfaces documented —
the console script handed to `uvx` on its own, without the distribution or the
extra — did not work at all~~ — fixed in the same change; see the CHANGELOG.
What is still open:

- **The first publish is a maintainer action.** The `publish-mcp` job runs on a
  published GitHub Release, and no release has been cut (the same gate the PyPI
  Trusted Publisher registration sits behind — `STRATEGY.md` §8, "maintainer /
  relationship actions"). Until then `io.github.reesehammer/umbra-mcp` is not in
  the registry, and the registry's PyPI ownership check — which fetches the
  distribution's own long description and looks for the `mcp-name:` marker the
  README now carries — has never run against a real upload.
- **Anthropic's directory is a separate, manual listing.** The official MCP
  registry is one submission; the vendor directories are their own forms.
- **The schema URL is pinned by hand.** `server.json` names a dated schema
  version (`2025-12-11`) and `tests/test_mcp_registry.py` requires it to be a
  dated one, but nothing notices when the registry publishes a newer one. That
  is the right default — an unpinned schema is not reproducible — and the
  network-marked validation test is what would catch the pin going stale.
- ~~**`change_composite` drops its polarization-mixing warning.**~~ **shipped** —
  `_require_same_polarization` refuses a *visible* mix (two passes whose known
  polarizations differ) before any render, but could not see a pass carrying no
  `sar:polarizations` metadata, so it composited and handed the agent a picture
  with no signal that same-polarization was *unverified* — where an HH-vs-VV mix
  would read as false change. `change_composite` now rides a structured caution
  text block (`_polarization_advisory`) beside the image whenever a pass lacks the
  metadata, naming the gap, and leaves a fully verified selection (image +
  caption) unchanged. The caption, which carries the attribution, stays last.

---

## Repeat-imaged-site discovery follow-ons (`umbra sites` shipped)

- **Surfaced in:** the `umbra sites` PR (`STRATEGY.md` §8, "Discovery surface").
- **Code:** `src/umbra_py/coverage.py` (`SiteCoverage`, `site_coverage`,
  `rank_site_coverage`), `umbra sites` in `src/umbra_py/cli/discover.py`,
  `tests/test_coverage.py`; the ranking is
  `src/umbra_py/showcase.py`'s `select_featured_sites` reused.

`umbra sites` ranks the archive's most repeat-imaged sites and summarises each
one's coverage (passes, date span, revisit cadence, footprint, products, and
`--json` pass URLs) — the discovery step before every `change` / `timescan` /
`stack` verb, single-sourced against the showcase's featured-gallery selector.
Follow-ons that build on it, none a blocker:

- ~~**The answer is CLI-only; the agent surfaces don't have it.**~~ **shipped** —
  `find_repeat_sites` is on the MCP server, the LangChain and LlamaIndex wrappers
  (one shared callable, so the three cannot drift, with the parity tests extended
  to pin it), a thin adapter over `rank_site_coverage` exactly the way
  `pick_change_interval` is over `best_change_interval`. It gathers the pool with
  the same backend selection and filters `search_catalog` takes and returns each
  site's passes oldest-first, so `find sites → pick-interval → narrate-change` is
  a complete chain a model can drive with no site known in advance. Its emitted
  shape (`SiteCoverage.to_dict()`) is published as `site-coverage.schema.json` and
  validated against a real payload, so `umbra sites --json` is schema'd too. See
  the CHANGELOG. What is still open, and smaller:
  - ~~**No `umbra serve` route**~~ **shipped** — see the next item.
- ~~**No `umbra serve` route.**~~ **shipped** — `GET /sites`
  (`serve.run_sites` / `serve.sites_result`, mounted in `build_app`) ranks
  "best-covered sites in this bbox" over HTTP, reusing the API's own STAC search
  for the filtered pool and the *same* `rank_site_coverage` selector for the
  ranking, so the discovery moat is on every surface (CLI, agent tools, HTTP). It
  was the `GET /sites`-shaped route this entry sketched, and it turned out not to
  be gated on a public instance at all: it is useful to anyone self-hosting via
  the shipped Docker setup, and it is the discovery half — `GET /sites → POST
  /artifacts/stats` — of the "queryable with zero install" promise the artifact
  routes made only for the analysis half. Its records reference the committed
  `site-coverage.schema.json` in the generated OpenAPI document (a new
  `CORE_OPENAPI_SCHEMAS` / `core_openapi_components`, since the route is mounted
  whether or not the artifact routes are, unlike the three artifact contracts),
  and the route is advertised on the landing page's `sites` link. See the
  CHANGELOG. What is still open, and smaller:
  - ~~**No `POST /sites` for a polygon body.**~~ **shipped** — `POST /sites`
    (`serve.post_sites`) mirrors the `GET`/`POST /search` pair: the same
    `run_sites` ranking and `site-coverage` records, but the body carries
    `intersects` as a GeoJSON *object* rather than the JSON-string query param
    `GET` needs, plus the SAR filters as top-level fields or a STAC `query`
    object exactly as `POST /search` accepts them (a top-level field overrides
    `query`). A new `_opt_int` makes a malformed `top`/`limit`/`min_passes` a
    `400` rather than a silent truncation; the route is advertised on the landing
    page (a second `sites` link, `method: POST`) and references the committed
    `site-coverage` contract in the generated OpenAPI document beside the `GET`.
    See the CHANGELOG.
  - ~~**The route re-lists the pool per request.**~~ **shipped** — `run_sites`
    routes an index backend through `CatalogIndex.rank_sites`, so `GET /sites`
    ranks whole-archive (`GROUP BY task`) on the normal serving mode, exactly as
    `umbra sites --local` does. `limit` now sizes only the re-listed pool a
    `--live` instance uses (no index to group over there). The drop-in was
    correct on any self-hosted instance, so it did not in fact wait on a public
    one; a `limit=1` test pins that a tiny pool cap can no longer shrink a site's
    measured depth on an index. See the CHANGELOG.
- ~~**The pool is a flat search, so a site's rank is only as deep as `--limit`.**~~
  **shipped** — `CatalogIndex.rank_sites` answers a site's depth as a `GROUP BY
  task` over the *whole* index, and `umbra sites --local` / `--index-db` routes
  through it, so a deeply-imaged site is ranked by all its passes rather than by
  the arbitrary window a `--limit`-capped, `(task, acq_date)`-ordered pool
  admitted (which favoured alphabetically-early tasks). The
  SQL-expressible filters (`bbox` / date / `area` / `fuzzy` / `product`) are
  counted directly and only the top tasks' documents are then read to summarise;
  the polygon and acquisition-property filters, which run per item in Python, take
  an uncapped-pool path that is still whole-archive. The ranking is
  `select_featured_sites`' own and the summary is `site_coverage`, single-sourced
  so the deep path cannot disagree with `umbra sites` / `find_repeat_sites` /
  the featured gallery, and a test pins it byte-for-byte against the uncapped-pool
  ranking for every filter. `--limit` is now a live-/`--token`-path pool size only.
  See the CHANGELOG. What shipped after it:
  - ~~**The agent tools still re-list a pool.**~~ **shipped** — `find_repeat_sites`
    (MCP / LangChain / LlamaIndex), whose `local` parameter already selected the
    index backend via `_search_source`, now routes that backend through
    `CatalogIndex.rank_sites` rather than re-listing a `limit`-capped `search` — the
    same drop-in the `--local` CLI and `GET /sites` use, one surface further out. So
    the shallow-rank limit is gone from the last discovery surface, and `limit`
    sizes only the live/`--token` pool (no index to `GROUP BY` there). Two tests
    pin it: a deep site whose passes fall outside a `limit`-sized pool is still
    ranked by all of them, and every filter forwards to `rank_sites` with no `limit`
    re-list. **This closes the whole-archive-ranking gap on every surface.** See the
    CHANGELOG.
- ~~**Nothing filters the discovery answer by recency.**~~ **shipped** —
  `active_since` keeps only sites whose newest dated pass is on or after a date, on
  every surface (`umbra sites --active-since`, `find_repeat_sites`, `GET`/`POST
  /sites`, `CatalogIndex.rank_sites`), single-sourced through
  `select_featured_sites` (pool path) and `CatalogIndex.rank_sites`'s
  `HAVING … AND MAX(acq_date) >= ?` (whole-archive), pinned byte-identical between
  the two. It is the whole-site recency gate `--start` could not express (that
  truncates every series to a window; this selects whole sites and keeps each
  survivor's full history) and adds no field to the `site-coverage` contract. See
  the CHANGELOG. What is still open, and smaller:
  - **The filter input is not echoed in the `find_repeat_sites` / `/sites`
    response metadata.** The return carries the resolved `place` / `bbox` / `area`
    but not `active_since`, `min_passes`, `rank_by` or `top` — the ranking inputs
    are all left off the echo alike, so a caller that wants to record what it asked
    for reads it from its own request. Add all four together if a consumer ever
    needs the round-trip, rather than singling out this one.
  - ~~**It gates on the site's *newest* pass, so there is no "quiet since" or
    activity-*window* query.**~~ **shipped for `active_before`** — `active_before`
    (`MAX(acq_date) <= ?`) is the twin upper bound on the same axis, on every surface
    (`umbra sites --active-before`, `find_repeat_sites`, `GET`/`POST /sites`,
    `CatalogIndex.rank_sites`), so the moat now selects dormant series ("stopped
    imaging") and, set with `active_since`, sites whose newest pass falls within a
    window. It reuses `active_since`'s exact single-sourcing and byte-identical
    SQL-vs-pool pinning, snapping a span expression to its last day (symmetric with
    `end`, where `active_since` snaps to the first day). See the CHANGELOG. What is
    still open, and smaller:
    - ~~**The `MIN(acq_date)` complement is not selected on.**~~ **shipped** —
      `first_since` / `first_before` add the onset (first-seen) axis, the twins of the
      `active_*` recency pair one end of the activity interval over, on every surface
      (`umbra sites --first-since` / `--first-before`, `find_repeat_sites`,
      `GET`/`POST /sites`, `CatalogIndex.rank_sites`): `first_since` (`MIN(acq_date)
      >= ?`) keeps a newly-appeared series (earliest pass on or after a date),
      `first_before` (`MIN(acq_date) <= ?`) a long-established one, and set together
      they bound the onset to a window. They reuse `active_since`'s exact single-sourcing
      and byte-identical SQL-vs-pool pinning (pure aggregates, so no full scan), the same
      `dates.parse_date_bound` grammar (`first_before` snapping a span to its last day
      like `active_before`), and are **orthogonal** to the recency pair — so
      `--first-since X --active-before Y` finds series that appeared after X and are
      already dormant by Y, the "different question" this entry named, now askable
      *together with* the recency one. A site's activity interval is reported as `first`
      / `last`; the selection is now two-sided on **both**. See the CHANGELOG.
  - **On the HTTP surface `active_since` accepts a relative expression, unlike the
    strict-ISO `datetime` filter.** `_coerce_date` resolves `"6 months ago"` on
    `GET`/`POST /sites`, which is convenient and matches the CLI, but it is a
    grammar the STAC `datetime` query param does not take — a deliberate asymmetry
    (this is an umbra-specific discovery filter, not a STAC one), noted so it is not
    mistaken for drift.
- ~~**Nothing filters the discovery answer by cadence.**~~ **shipped** —
  `max_revisit` keeps only sites whose **worst-case** gap between consecutive passes
  is at most `N` days, on every surface (`umbra sites --max-revisit`,
  `find_repeat_sites`, `GET`/`POST /sites`, `CatalogIndex.rank_sites`), so the moat
  now selects the *monitorable* sites — those with no blind spot longer than `N` days
  — where before it could only report each site's cadence. It measures the same depth
  `--rank-by` does (`coverage._passes_cadence`, the cadence twin of
  `_min_passes_depth`): under `--rank-by comparable` the *analysable* series' worst
  gap, so an off-polarization pass filling a gap no change verb can use cannot make a
  site read as tighter than it is. Unlike the recency bounds a worst *consecutive* gap
  is not a SQL aggregate, so the index path applies the identical `_passes_cadence` in
  Python on the same per-task items it already reads (pinned byte-identical to the pool
  path) and drops the raw-count SQL `LIMIT` when the filter is set, so a tightly-imaged
  site outside the raw top-`top` is promoted rather than truncated. See the CHANGELOG.
  What is still open, and smaller:
  - ~~**Only the *worst* gap is selected on, not the typical one.**~~ **shipped** —
    `max_revisit` gates `max_revisit_days` (the widest stretch a change could have gone
    unseen, one long hole disqualifying a site however tight the rest of its series), and
    now `median_revisit` gates `median_revisit_days` (a site *usually* imaged often,
    tolerating the odd gap) — the softer "typically frequent" question beside the strict
    "never blind for longer than N days" one. It is the same shape on a different figure,
    on every surface at once (`umbra sites --median-revisit`, `find_repeat_sites`,
    `GET`/`POST /sites`, and `CatalogIndex.rank_sites` for `--local`), single-sourced
    through the same two functions the worst-case bound is (`select_featured_sites`,
    `CatalogIndex.rank_sites`, the index path applying the identical
    `coverage._passes_median_revisit` in Python since a median of consecutive gaps is no
    more a SQL aggregate than a max of them, dropping the raw-count `LIMIT` when set,
    pinned byte-identical to the pool path). The report carries both figures, so the two
    are genuine complements — a mostly-tight series with one outage passes the median
    filter but fails the worst-case one, and vice versa — and set together they demand
    "usually imaged every A days *and* never blind for longer than B". It adds no field
    to the `site-coverage` contract (a filter input, like `max_revisit`), so no schema
    moved. **With it the moat selects on cadence from both readings — worst-case and
    typical.** See the CHANGELOG.
- ~~**Nothing filters the discovery answer by observation baseline (span).**~~
  **shipped** — `min_span` keeps only sites whose observation span (first dated pass to
  last) is at least `N` days, on every surface (`umbra sites --min-span`,
  `find_repeat_sites`, `GET`/`POST /sites`, `CatalogIndex.rank_sites`), so the moat now
  selects the *long-baseline* sites — those watched over a long enough window for a
  *slow* change (subsidence, construction, deforestation) to be visible — where before
  it could only report each site's `span_days`. It is the baseline (duration) axis, a
  genuine complement to cadence: `--max-revisit` bounds the worst *gap* (reliability),
  `--min-span` the total *baseline* (duration), so a tight-cadence short-window site is
  dropped by a span bound and a long-baseline sparse one kept. It measures the same
  depth `--rank-by` does (`coverage._passes_span`, the baseline twin of
  `_passes_cadence`): under `--rank-by comparable` the *analysable* series' span, so
  off-polarization passes bracketing the range cannot inflate the baseline past the
  differenceable series. Like the cadence bound the comparable-subset span is not a SQL
  aggregate, so the index path applies the identical `_passes_span` in Python (pinned
  byte-identical to the pool path) and drops the raw-count SQL `LIMIT` when set, so a
  long-baseline site outside the raw top-`top` is promoted. See the CHANGELOG. What is
  still open, and smaller:
  - ~~**A `max_span` upper bound is not selected on.**~~ **shipped** — `max_span`
    (`coverage._passes_max_span`, the `<=` twin of `_passes_span`) keeps only sites whose
    baseline is at most `N` days (a *short-lived* series, imaged intensively then
    stopped), on every surface (`umbra sites --max-span`, `find_repeat_sites`,
    `GET`/`POST /sites`, `CatalogIndex.rank_sites`), so set with `--min-span` the two
    bound each site's baseline to a window (`min_span <= span <= max_span`), symmetric
    with `active_since` / `active_before`. It reuses `min_span`'s exact single-sourcing
    (the same `select_featured_sites` / `CatalogIndex.rank_sites` pair, the index path
    applying the identical `_passes_max_span` in Python and dropping the raw-count
    `LIMIT` when set so a short-baseline site outside the raw top-`top` is promoted,
    pinned byte-identical between the SQL and pool paths for every cutoff and window),
    gates the *analysable* series' span under `--rank-by comparable`, and drops a site
    with no measurable span so the window admits only a confirmed baseline. It adds no
    field to the `site-coverage` contract (a filter input, like the floor). See the
    CHANGELOG. **With it the baseline axis is two-sided (floor / ceiling / window),
    matching the recency axis — the discovery moat's four axes (depth, recency, cadence,
    baseline) are complete.**

---

## Grow the `umbra serve` STAC API (a hosted instance)

- **Surfaced in:** the `umbra serve` STAC API PR.
- **Code:** `src/umbra_py/serve.py`, `pyproject.toml` (`[serve]` extra).

The read-only STAC API is shipped (landing / conformance / collections / items /
`GET`+`POST /search` with bbox, datetime, geometry `intersects`, ids and token
pagination), ranks the archive's most repeat-imaged sites
(`GET`+`POST /sites`, the discovery step in front of the analysis routes),
renders artifacts on demand
(`GET /artifacts/quicklook/{id}.png`, `GET /artifacts/thumbnail/{id}.png`, `POST
/artifacts/change`, `.../timescan`, `.../swipe`, and the one that is numbers
rather than a picture, `POST /artifacts/stats` — with `POST /artifacts/provenance`
as its preflight, the one route that neither renders nor caches) with an async job
flow for long renders, and exposes the index's Umbra-specific filters through the
STAC Query extension.
Open follow-on:

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
