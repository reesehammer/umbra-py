# Outstanding TODOs

This file tracks follow-up items that were intentionally scoped out of merged
PRs. Each entry should link to the PR that surfaced it, point at the code
involved, and describe the smallest change that closes it out.

When you finish one, delete the entry (or move it under a short "Done" log at
the bottom if the history is useful).

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
invocation in `.github/workflows/*.yml` against the real Click command tree.
Follow-ons, none a blocker:

- **The first good snapshot still needs a human.** The workflow is weekly +
  `workflow_dispatch`, so until a maintainer dispatches a run (or Monday's cron
  fires), the release stays absent and `umbra index fetch` keeps 404ing. Nothing
  in this repo can create it. Smallest close-out: dispatch the workflow once and
  confirm `catalog.db`, `umbra-open-data.parquet`, `catalog.pmtiles` and
  `catalog.html` land on the release, then confirm the Docs job's `workflow_run`
  trigger (added in the showcase-404 fix) rebuilds the showcase off it.
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

- **Surfaced in:** the baked place-label PR (`docs/DEMO_APP_GAPS.md` G2/G6).
- **Code:** `src/umbra_py/index.py` (`bake_places`, the `place` column + the v1→v2
  migration), `umbra index bake` in `cli.py`, `UmbraItem.place` in `models.py`.

`umbra index bake` reverse-geocodes each acquisition's footprint centroid once at
build time into an additive `place` column, so every `--local` search yields the
label on `UmbraItem.place` and `umbra demo` shows real geographic names with no
render-time geocoding. This closed G2's "no cached place label" denormalization.
Follow-ons that build on it, none a blocker:

- ~~**Wire the baked label through the other consumers.**~~ ✅ **Done.**
  `UmbraItem.to_llm_context()` now prefers `.place` over the task codename;
  `viz.footprint_map` / `timeline_map` (`umbra map` / `--timeline`) use `.place`
  directly and build the Nominatim session lazily, so a fully-baked `--local`
  render never geocodes at render time (falling back to a live call only for
  items still lacking a label); `umbra serve`'s `item_to_stac` surfaces the label
  as a namespaced `umbra:place` STAC property; and the stac-geoparquet export
  (`export._export_doc`) carries `umbra:place` into the published snapshot. In
  each case the baked label is preferred only when present and never overrides a
  value the source document already carries. Deterministic, no new dependency, no
  model call; offline-tested in `tests/test_models.py`, `test_viz.py`,
  `test_serve.py`, and `test_export.py`.
  (The gallery contact sheet groups by task, not a per-item place popup, so it
  has no label to wire.)
- ~~**Bake per-item thumbnails too (G6).**~~ ✅ **Done** (`umbra index
  bake-thumbnails` / `CatalogIndex.bake_thumbnails` / `get_thumbnail`). A small PNG
  per acquisition is rendered once (via the reusable `viz._thumbnail_png`) and
  cached in an additive `thumbnail BLOB` column (the second additive migration,
  `user_version` 2 → 3), so `GET /artifacts/thumbnail/{id}.png` on `umbra serve`
  serves it instantly from local bytes instead of re-streaming the COG. Idempotent
  and injectable, mirroring `bake_places`, so it is fully offline-tested.
  ~~Wire the baked thumbnail into the `umbra demo` client surface.~~ ✅ **Done.**
  `umbra demo --server-url` now leads a clicked scene's detail panel with the
  baked thumbnail from `GET /artifacts/thumbnail/{id}.png` (instant local-bytes
  read, quicklook-render fallback); a scene with no baked thumbnail 404s and the
  `<img>` is dropped via `onerror` (no broken image), and without `--server-url`
  the panel is unchanged and the page stays static. Reuses the one `serverBase`
  the analyze panel already computes; the remote item id is url-encoded into the
  path. Offline-tested in `tests/test_demo.py`.
  ~~The same preview in the `umbra gallery` *contact sheet* client.~~ ✅ **Done.**
  A `--local` / `--index-db` gallery now embeds any thumbnail already baked into
  the index straight from local bytes (`viz.gallery(baked=…)` fed by
  `CatalogIndex.get_thumbnail`, built in the CLI by `_baked_thumbnails`) instead
  of re-streaming the COG overview — instant, offline, and needing **no `viz`
  extra** when every tile is baked (the `_require("rasterio")` fail-fast is now
  raised only when a stream is actually needed). Tiles missing from the bake still
  stream the usual way, and a plain live `umbra gallery` is unchanged.
  Offline-tested in `tests/test_viz.py` (baked-only needs no viz extra; baked +
  streamed mix) and `tests/test_index.py` (`umbra gallery --local` over a
  bake-thumbnailed index streams nothing).
  ~~Remaining optional polish under G6: baking thumbnails into the published
  weekly snapshot (gated on egress, like the place-label bake below).~~ ✅
  **Done**, and **G6 is closed** (`umbra index fetch-thumbnails` /
  `export-thumbnails`, `CatalogIndex.import_thumbnails` / `export_thumbnails`,
  `fetch_prebuilt_thumbnails`, and the two thumbnail steps in
  `publish-index.yml`). The pictures are published as a *separate*
  `catalog.thumbs.db` sidecar rather than a column of the released `catalog.db`,
  because a PNG per acquisition dwarfs the metadata and every `umbra index
  fetch` would otherwise pay for previews most callers never open — the same
  split `catalog.embed.db` makes. The egress gate is answered by making the bake
  incremental: the workflow re-imports the previous sidecar before baking, so a
  run streams only what the crawl added, bounded by `--limit` and spent
  newest-first (`bake_thumbnails(newest_first=True)` — the default `href` order
  is arbitrary with respect to time, so a capped run would starve the freshest
  passes). Offline-tested in `tests/test_index.py`.
- **A precomputed centroid column.** The centroid is derived from the stored bbox
  today (cheap), so a `centroid` column is only worth adding if a consumer needs
  to query/sort on it in SQL rather than compute it per row.
- ~~**Bake in the published snapshot.**~~ ✅ **Done** for place labels
  (`CatalogIndex.bake_places(by_site=True)` / `umbra index bake --by-site`, and
  the `Bake place labels` step in `publish-index.yml`). The blocker was the
  Nominatim rate limit (~1 request/sec), which made one geocode per acquisition
  an overnight job; the "one pin per site" option named here is what shipped.
  Acquisitions sharing a task *and* a ~11 km cell (`_SITE_CELL_DEGREES`) are
  resolved together from their mean centroid and all take that one label — Umbra
  files every pass over a site under one task, so a repeat-imaged archive
  collapses to roughly one lookup per site. Grouping is a pure, deterministic
  function (`index._site_groups`), insertion-ordered so a `--limit`ed batch is
  reproducible and resumable; `--limit` now caps *lookups* rather than items. The
  weekly build bakes (bounded, `continue-on-error`) **before** the derived
  artifacts, so `catalog.db`, the parquet export and `catalog.pmtiles` all
  publish pre-labelled. Offline-tested in `tests/test_index.py`. ~~Remaining
  under this heading: baking **thumbnails** into the published snapshot, which is
  a different cost (a COG overview streamed per acquisition, i.e. egress) rather
  than a rate limit, so it stays open.~~ ✅ **Done too** — see the G6 entry
  above; the egress cost is paid once and then topped up incrementally from the
  previously published sidecar, rather than re-streamed weekly.

Follow-ons from the published thumbnail sidecar itself, none a blocker:

- **The sidecar has no total cap.** Each weekly run adds up to `--limit` (1500)
  previews and never drops any, so `catalog.thumbs.db` grows monotonically
  toward whole-catalog coverage at ~10–20 KB per 128 px scene. That is the
  intended trajectory (and the download is opt-in), but if the asset gets
  unwieldy the smallest fix is an export-side bound —
  `export_thumbnails(limit=…, newest_first=True)`, mirroring the bake — so the
  published file keeps the most recent N rather than everything ever baked.
- **`newest_first` is opt-in, not the default.** `bake_thumbnails` still orders
  by `href` unless asked, to keep an existing caller's batching stable. If no
  caller depends on that order, making newest-first the default would be one
  fewer flag to remember on the path where a cap actually matters.
- **The published thumbnails are 128 px; `bake-thumbnails` defaults to 256.** A
  local bake and the fetched sidecar therefore differ in size, and a merge keeps
  whichever arrived first (`--overwrite` replaces). Recording the bake size in
  the sidecar would let a merge prefer the larger preview rather than the
  earlier one.

---

## Static GitHub Pages showcase follow-ons (`umbra showcase` shipped)

- **Surfaced in:** the GitHub Pages showcase PR (`docs/DEMO_APP_GAPS.md` G7 /
  `STRATEGY.md` §8 demo/hosting).
- **Code:** `src/umbra_py/showcase.py` (`build_showcase` / `assemble_showcase`),
  `umbra showcase` in `cli.py`, the `Build catalog showcase` step in
  `.github/workflows/docs.yml`.

`umbra showcase` composes the whole-catalog PMTiles map (`umbra tiles`), the
interactive explorer (`umbra demo`) and a self-contained landing page into one
static, hostable directory; the `docs.yml` Pages job publishes it to
`/showcase/` beside the docs (non-blocking, main-only). Follow-ons that build on
it, none a blocker:

- **Enable Pages for the repo (maintainer).** The `docs.yml` deploy job (and so
  the showcase publish) is skipped until Settings → Pages → Source is set to
  "GitHub Actions". Until then the showcase builds in CI but isn't served.
- ~~**Precompute a few curated showcase artifacts.**~~ ✅ **Done** for the change
  view (`umbra showcase --featured N` / `--featured-area` /
  `showcase.select_featured_sites`). The landing page led with a live map +
  explorer and *no SAR imagery*, so seeing what the archive looks like meant
  clicking in and waiting on a render. `--featured N` now renders a change
  composite for the N most repeat-imaged sites ahead of time into a relocatable
  `featured/` subdirectory and shows them as a captioned gallery — the R4
  "instant what-SAR-change-looks-like" for the static path. Selection is a pure,
  deterministic function (group by task — Umbra files every pass of a site under
  one task directory — keep tasks with enough dated passes, rank by pass count
  then name); `--featured-area` curates by name instead, and `--featured-frames
  2|3` picks the green/magenta or temporal-RGB view. Every caption states the
  pass count, date range and colour semantics, so a tile is never a picture
  without provenance. The render goes through an injectable `featured_renderer`
  (default: the existing `viz.select_change_frames` + `save_change_composite`,
  streaming only a downsampled overview), so the whole path is offline-tested
  with no network and no `viz` extra; a site that won't render is warned about
  and dropped rather than failing the build. `docs.yml` passes `--featured 6`.
  ~~Remaining under R4: the **swipe** and **timescan** variants of the same
  gallery — `viz.save_swipe_map` writes an HTML page rather than a PNG, so it
  needs a different tile shape (a link card, not an `<img>`), and a timescan
  tile is the same shape as the change tile with a second renderer.~~ ✅ **Done**
  (`umbra showcase --featured-view {change,timescan,swipe}` /
  `assemble_showcase(featured_view=…)` / `showcase.FEATURED_VIEWS`), so **R4 is
  closed**. The gallery precomputed exactly one thing, but the same marquee
  selection feeds the toolkit's other two comparators — a `timescan` collapses a
  site's *whole* pass series into one mean/peak/variability still (3+ passes, and
  it ignores `--featured-frames` by design, so its caption counts every pass), and
  a `swipe` writes a self-contained before/after page over the same two frames
  `select_change_frames` picks for the change view. The four things that actually
  differ between the views live in one `FeaturedView` record: artifact suffix,
  qualifying `min_passes` (via `min_passes_for()`, so a site the view can't render
  is dropped *before* any network work), tile `kind` and the section's copy. That
  last shape difference is what the gallery had to grow: a `"page"` artifact has
  no still to preview, so it renders as a link card in the same frame as an
  `"image"` tile with an identical caption. Selection stays pure, rendering stays
  behind the injectable `featured_renderer` (offline-tested, no `viz` extra), a
  failed render is still warned-and-dropped, and `change` is still the default —
  a showcase built without the flag is byte-identical to before. Follow-on:
  `docs.yml` still deploys the `change` gallery; a hosted page showing more than
  one view at once would need the section repeated per view rather than chosen.
- **Auto-stamp the freshness date from the index.** The CI step passes the run
  date to `--updated`; reading the fetched index's `built_at` (as `umbra index
  info` does) would show the *snapshot's* age rather than the build's.
- ~~**Wire the PMTiles basemap into the explorer itself.**~~ ✅ **Done**
  (`umbra showcase --unified` / `assemble_showcase(unified=True)`). `map.html` and
  `explore.html` were siblings — a whole-catalog viewer you could only click, and
  a filterable explorer over a gathered slice — because the explorer had no way to
  read a tiled archive. It has one now (see the `umbra demo --pmtiles` entry
  below), so `--unified` builds `explore.html` *over* the copied
  `catalog.pmtiles`, writes no `map.html`, and the landing page leads to a single
  explorer covering the whole catalog with the filters. `docs.yml` builds the
  hosted showcase this way. Dropping `--unified` still gives the original pair,
  which remains the right build when you want the slice's footprint outlines and
  on-click COG overlay. ~~Remaining optional polish: the non-unified build is the
  only one that shows footprints, so tiling polygons would close the last gap
  between the two.~~ ✅ **Done** — the archive now carries footprint polygons (see
  the PMTiles section), so the unified explorer draws outlines too. ~~The pair's
  one remaining extra is the on-click "Get SAR image" COG overlay.~~ ✅ **Done**
  — the archive references each acquisition's GEC COG, so the unified explorer
  streams the picture on click too (see the PMTiles section). The non-unified
  pair now differs only in the two fields tiles do not encode (polarizations and
  the per-product asset list).

---

## Whole-catalog PMTiles tiling follow-ons (`umbra tiles` shipped)

- **Surfaced in:** the `umbra tiles` PR (`docs/DEMO_APP_GAPS.md` Path A step 3).
- **Code:** `src/umbra_py/pmtiles.py`, `umbra tiles` in `cli.py`.

`umbra tiles` (a stdlib-only PMTiles v3 writer over acquisition centroids *and*
footprint polygons + a MapLibre GL viewer, no extra, no tippecanoe) is shipped,
closing the demo's full-acquisition-set tiling gap. Follow-ons that build on it,
none a blocker:

- ~~**Wire the PMTiles source into `umbra demo`.**~~ ✅ **Done** (`umbra demo
  --pmtiles PATH-OR-URL` / `build_demo(pmtiles_url=…)`, and the one-page `umbra
  showcase --unified` built on it). The demo embedded its gathered slice as inline
  JSON, which capped the explorer at whatever fits in a download; `--pmtiles`
  swaps the Leaflet cluster for a MapLibre GL vector layer over a whole-catalog
  archive read by range request, so the whole catalog is explorable from a page
  that stays a few KB. The sidebar filters compile to MapLibre expressions
  evaluated inside the tiles and keep `passesFilter`'s exact semantics (including
  "a missing date never fails a date filter"); the detail rows, the baked
  thumbnail preview and the "Analyze this view" panel moved into one shared,
  map-engine-agnostic script both explorers drive, so the two modes cannot drift.
  ~~Vector tiles carry centroids and lean metadata, so the footprint outline and
  the on-click "Get SAR image" overlay remain embedded-slice features~~ — both
  shipped: the outline with the footprint layer below, the overlay with the COG
  reference below it. `--pmtiles` with a search option is a hard error rather than a
  quietly unfiltered page. Offline-tested in `tests/test_demo.py` and
  `tests/test_showcase.py`; the generated page was also exercised in a real
  browser (archive range-reads, every filter, click-to-detail).
- **Leaf directories for very large catalogs.** The writer emits a single root
  directory, which is spec-valid and ample for the current catalog (thousands of
  tiles). If the tile count ever grows past a comfortable root-directory size,
  add leaf-directory splitting (the PMTiles spec's mechanism) so readers still
  fetch a small root first.
- ~~**Tile polygons, not just centroids.**~~ ✅ **Done** (the `footprints`
  source-layer / `pmtiles.FOOTPRINT_LAYER`, `umbra tiles --no-footprints` /
  `--footprint-min-zoom`). A centroid tells you a scene exists but not what it
  covers, and the outline was the last thing the embedded-slice explorer had over
  the whole-archive one. `build_pmtiles` now writes each acquisition twice: its
  centroid in `acquisitions` at every zoom, and its footprint polygon — clipped to
  each tile it touches — in `footprints` from `FOOTPRINT_MIN_ZOOM` (6) up, where a
  footprint first spans more than a pixel (keeping the low-zoom tiles every
  visitor loads first centroid-only). `umbra demo --pmtiles` and `build_viewer`
  draw it as a fill + outline; in the explorer one filter expression drives the
  markers and outlines together and clicking a polygon opens the same detail panel
  as its centroid. Still stdlib-only: the MVT polygon command stream, a
  Sutherland–Hodgman clip against the buffered tile box, and the spec's clockwise
  exterior winding are a few pure functions, verified by decoding the archive's
  own output back into rings (and cross-checked once against an independent MVT
  decoder). A ring spanning more than half the globe (an antimeridian-wrapping
  bbox) keeps its centroid and is not tiled. `--no-footprints` writes the previous
  centroids-only archive, and the metadata advertises only the layers actually
  present, so an older archive draws no outlines rather than erroring.
  Offline-tested in `tests/test_pmtiles.py` and `tests/test_demo.py`.
- ~~**Reference each acquisition's COG so a viewer can show the picture.**~~
  ✅ **Done** (the `cog` + `bounds` properties / `build_pmtiles(cog_asset=…)`,
  `umbra tiles --cog-asset` / `--no-cog`). Tiles carried metadata only, so the
  whole-archive explorer stopped at "a scene exists here" while the
  embedded-slice one could stream the radar image on click — the last capability
  gap between the two. Every tiled feature (centroid *and* footprint) now carries
  a reference to its GEC cloud-optimized GeoTIFF plus the `"S,W,N,E"` bounds to
  place it, and `umbra demo --pmtiles` offers the same "Get SAR image" button
  over it. Kept lean: the product is a sibling of the item's STAC sidecar in the
  public bucket, so what is tiled is the bare filename (~30 bytes) and the page
  rebuilds the URL against the `stac_href` the tiles already carried — a
  non-sibling absolute href is stored whole instead, and an asset that resolves
  to nothing anonymously fetchable is omitted entirely (no button rather than a
  button that 404s). `driver_script(engine=…)` grew a MapLibre `image`-source
  placement beside the Leaflet `imageOverlay` one; everything above the
  placement — CDN load, range-read, overview pick, percentile stretch, canvas
  paint, button state machine — stays one implementation. `--no-cog` writes the
  previous metadata-only archive, and a page reading one simply shows no button.
  Offline-tested in `tests/test_pmtiles.py`, `tests/test_demo.py` and
  `tests/test_lazy_imagery.py`. Follow-ons: the published weekly
  `catalog.pmtiles` only gains the references on its next `publish-index.yml`
  run, so the hosted showcase shows no button until then; and the overlay is a
  bbox-stretched quick look (the same approximation the slice explorer makes),
  not a reprojection.
- ~~**Tile the two list-valued fields (polarizations, the per-product asset
  list).**~~ ✅ **Done** (the `pol` + `assets` properties, and the polarization
  chip row both `umbra demo` modes now carry). These were the last two fields the
  embedded-slice detail panel showed and the whole-archive one could not, so
  tiling them makes the whole-archive explorer a strict **superset** of the slice
  explorer and closes `DEMO_APP_GAPS.md` Path A. Both are comma-joined, because a
  vector-tile property is a scalar; an item with neither tiles no key at all
  rather than an empty string. The larger point is the filter they enabled: the
  sidebar could narrow by place, date and product — three facets about *what you
  get* — but not by the one that decides whether an analysis is meaningful, since
  differencing a VV pass against an HH one puts a scattering difference on the
  time axis and reads it as change. `POST /artifacts/stats` refuses such a
  selection and tells the caller to narrow to one polarization, which is advice
  the explorer's Quantify button could not act on. The slice app tests the list
  it already holds; the whole-archive app compiles the chips to a MapLibre
  `index-of` test evaluated inside the tiles (exact, since no two-letter code
  matches across the separator), and a scene with no `pol` stays visible like one
  with no date. Offline-tested in `tests/test_pmtiles.py` and
  `tests/test_demo.py`. Follow-ons, neither a blocker:
  - **The published archive gains the fields on its next rebuild.** Like the COG
    references above, `catalog.pmtiles` only carries `pol`/`assets` after the next
    `publish-index.yml` run, so until then the hosted showcase's polarization
    chips filter nothing out (every feature lacks the key, and the "never hidden
    by a facet it has no value for" rule keeps them all visible — the honest
    failure mode, but a silent one).
  - **The facet chips are the only place the two modes still differ.** The slice
    app derives its chips from the slice it holds; the whole-archive app offers
    the closed `POLARIZATIONS` set, so a chip can name a polarization the archive
    has none of. Deriving it instead would need a facet summary in the archive
    metadata — worth doing only if the same question comes up for another field.

---

## SICD DEM orthorectification follow-ons (`umbra convert --dem` shipped)

- **Surfaced in:** the DEM terrain-orthorectification PR (`docs/STRATEGY.md` 5.5).
- **Code:** `src/umbra_py/convert.py` (`_refine_gcps_with_dem`,
  `_dem_height_sampler`, `_build_gcps_dem`), `umbra convert --dem` in `cli.py`.

`umbra convert --dem PATH` / `sicd_to_geocoded_cog(dem=…)` terrain-orthorectifies
a SICD against any rasterio-readable elevation model (walk each GCP onto the DEM
surface via the standard ortho fixed-point iteration). Shipped, closing the
geometric half of 5.5's remaining geocoding gap. Follow-ons, none a blocker:

- ~~**Vertical datum / geoid handling.**~~ ✅ **Done** (`umbra convert --geoid
  PATH` / `sicd_to_geocoded_cog(geoid=…)`). Global DEMs (Copernicus GLO-30, SRTM)
  quote height above the EGM geoid, but SICD's HAE projection wants height above
  the ellipsoid; feeding the orthometric height in as-is mislocated relief by
  roughly `N * tan(look_angle)` (the undulation `N` reaches ~±100 m worldwide).
  `--geoid PATH` takes any rasterio-readable undulation grid (e.g. an EGM96/EGM2008
  GeoTIFF) and adds `N` to each sampled DEM height (`hae = orthometric + N`) before
  projecting. It requires `--dem` (it corrects DEM heights) and degrades gracefully
  off the grid (`N=0`). The correction is a pure composition of two
  `(lons, lats) -> heights` samplers (`_geoid_corrected_sampler`) — the geoid grid
  is read with the very same injectable `_dem_height_sampler` — so it is fully
  offline-tested with a hand-written grid, no new dependency, no packaged EGM data.
  Without `--geoid` the output is unchanged (correct to the local geoid–ellipsoid
  separation, ample for map placement).
- ~~**Auto-fetch a geoid grid for the scene.**~~ ✅ **Done** (`umbra_py.geoid` /
  `umbra convert --geoid auto`). `geoid="auto"` / `--geoid auto` is the vertical
  sibling of `--dem auto`: it fetches a global geoid-undulation grid (the compact
  ~4 MB EGM96 15′ model PROJ distributes on `cdn.proj.org`, `us_nga_egm96_15.tif`)
  once, caches it beside the DEM tiles under the same XDG dir, and hands it into
  the shipped `--geoid PATH` correction unchanged — so `--dem auto --geoid auto`
  gives a terrain-corrected *and* vertically-referenced scene with no data hunt.
  Unlike a DEM the EGM grid is a single global file, so there is nothing to tile;
  the fetch reuses the resume-safe `download_url` and is injectable, so the whole
  download-and-cache path is offline-tested with a stub downloader (no network, no
  new dependency, no packaged EGM data). `us_nga_egm08_25.tif` (EGM2008 2.5′) is a
  higher-resolution alternative on the same CDN, selectable via
  `fetch_geoid_grid(name=…)`.
- ~~**Radiometric terrain flattening (geometric cosine correction).**~~ ✅
  **Done** (`umbra convert --rtc` / `sicd_to_geocoded_cog(rtc=True)`). Terrain
  orthorectification fixes *where* a pixel lands but not *how bright* it is; radar
  backscatter is modulated by the local incidence angle, so slopes tilted toward
  the radar look bright and slopes tilted away look dark from geometry alone.
  `--rtc` (which requires `--dem`) removes that: after geocoding, each pixel is
  scaled in the power domain by `cos(reference)/cos(local_incidence)`, with the
  local incidence angle derived from the DEM's local slope (surface normal,
  `_terrain_normals`) and the scene look geometry (`SCPCOA.IncidenceAng`/`AzimAng`,
  `_scene_look_geometry`). The reference defaults to the scene incidence, so flat
  terrain is unchanged and only slopes are flattened (`--rtc-ref-angle` overrides
  it). The physics is a pure-numpy core (`_terrain_normals`, `_look_unit_vector`,
  `_cos_local_incidence`, `_terrain_flatten_factor`, `_apply_terrain_flattening`)
  with closed-form planar-slope behaviour, fully offline-tested with hand-built
  arrays; only the DEM-on-grid resample (`_terrain_flatten_on_grid`) touches
  rasterio, wired in through a `post_warp` hook on `_warp_gcps_to_cog`. DEM gaps
  and radar-shadow slopes degrade gracefully (factor clamped to ±10 dB, gaps pass
  through unchanged). No model call, no new dependency.
- ~~**Projected-area (foreshortening) RTC model.**~~ ✅ **Done** (`umbra convert
  --rtc --rtc-model area` / `sicd_to_geocoded_cog(rtc_model="area")`). The default
  `--rtc` uses the 3-D local incidence angle in a *cosine* correction, which folds
  the azimuth-direction tilt (which does not foreshorten) into the correction. The
  `area` model scales power by `sin(local_range_incidence)/sin(reference)`,
  measuring incidence in the *range–vertical* plane (`_range_local_incidence`,
  `_foreshortening_factor`), so it corrects the range-direction foreshortening and
  layover that dominate radiometric terrain distortion while leaving a pure azimuth
  slope unchanged. On flat terrain both models reduce to the scene incidence, so
  only slopes change; DEM gaps and layover degrade gracefully (factor one over
  gaps, floored/clamped in layover). New public constant `RTC_MODELS`; `rtc_model`
  defaults to `"cosine"`, so existing calls are unchanged. Pure-numpy core, no model
  call, no new dependency, offline-tested in `tests/test_convert.py` (flat /
  range-ramp / azimuth-slope geometry, the cosine-vs-area distinction, layover/gap
  handling, end-to-end + CLI).
- ~~**Per-pixel facet-area (gamma-nought) RTC model.**~~ ✅ **Done** (`umbra convert
  --rtc --rtc-model gamma` / `sicd_to_geocoded_cog(rtc_model="gamma")`). A third
  selectable model. The `cosine` model normalises against the *ground*-projected
  area and `area` handles only the range-plane foreshortening; `gamma` scales power
  by `cos(reference) * nz / cos(local_incidence)`, normalising by the local
  illuminated *facet* area projected into the plane perpendicular to the look
  direction (the gamma-nought convention) — the full 3-D facet normal *plus* the
  true tilted-facet-area term `nz = cos(slope)` both other models omit (a facet
  whose ground-projected area is one pixel has true area `1/nz`, so its illuminated
  area per pixel scales as `cos(local_incidence)/nz`). Flat terrain (`nz == 1`) is
  unchanged; only slopes change. Third value in `RTC_MODELS`; `rtc_model` still
  defaults to `"cosine"`. Pure-numpy core (`_facet_area_factor`), no model call, no
  new dependency, offline-tested in `tests/test_convert.py` (flat unchanged, the
  exact `nz`-scaling vs the cosine factor, DEM-gap safety, shadow/clamp floor,
  end-to-end differ-from-cosine-and-area + CLI).
- ~~**Illuminated-area facet integration in image space.**~~ ✅ **Done**
  (`umbra convert --rtc --rtc-model facet` / `sicd_to_geocoded_cog(rtc_model="facet")`).
  The other three models correct a pixel from its own slope, which makes layover
  structurally invisible to them: where terrain is steeper than the look direction
  several ground facets image into one radar cell and their returns *sum* there, so
  the flat ground a ridge folds onto has no slope of its own to correct and comes
  back untouched. `facet` integrates in the radar's own geometry instead — every
  facet is projected into the scene's `(slant_range, azimuth)` frame
  (`_radar_coordinates`), its illuminated area (the true tilted area `cell / nz`
  projected perpendicular to the look direction) is accumulated into the cell it
  images into (`_accumulate_radar_area`, bilinear so the accumulation is a smooth
  partition), and each pixel is normalised by the **total** in its cell, so
  everything folded together is suppressed together. The reference is the same
  integration over flat ground in the same geometry, so the binning and the scene
  edges cancel exactly and flat terrain is unchanged; over a planar range slope,
  where nothing folds, it reduces to the `area` × `gamma` product, which is what the
  tests pin the arithmetic to. Fourth value in `RTC_MODELS`; `rtc_model` still
  defaults to `"cosine"`. Pure-numpy core, no model call, no new dependency,
  offline-tested in `tests/test_convert.py`.
- ~~**Radiometric calibration.**~~ ✅ **Done** (`umbra convert --calibrate
  {sigma0,beta0,gamma0,rcs}` / `sicd_to_geocoded_cog(calibration=…)` /
  `sicd_to_amplitude_geotiff(calibration=…)` / `CALIBRATION_TYPES` /
  `sicd_calibration_types`). Every `--rtc` model normalised *detected amplitude*,
  so the output was a relative image: correct within itself and incomparable with
  any other scene. Calibration scales pixel **power** by the scale-factor
  polynomial in the SICD's own `Radiometric` metadata, so the value becomes a
  physical quantity — the `sigma0` / `beta0` / `gamma0` backscatter coefficients
  (unit ground / slant-plane / perpendicular-to-look area) or `rcs`, the absolute
  radar cross-section in m². Applied **in image space**, before the warp, because
  that is where the polynomials are defined: they are functions of image
  coordinates in metres from the SCP, so `_calibration_scale` evaluates them over
  `(row + FirstRow − SCPPixel.Row) × Grid.Row.SS` (and the column equivalent),
  which keeps a constant polynomial flat, lets a higher-order one track the
  across-swath variation, and offsets a chip by its own origin rather than tilting
  it. It **composes with `--rtc`**: both are power-domain factors on the same
  raster and share one application path, so `--rtc-model facet --calibrate gamma0`
  is the terrain-flattened gamma-nought product every RTC entry above said it was
  not. The metadata caveat became a *check* rather than a footnote — a product
  with no `Radiometric` block (which is most of Umbra's open data) raises a
  self-describing error naming what it does carry, the CLI reports it as a message
  rather than a traceback, and `sicd_calibration_types` answers the question
  without trying; a scale factor that evaluates non-positive or non-finite
  anywhere is rejected rather than clamped. Pure-numpy core, no new dependency,
  offline-tested in `tests/test_convert.py`. Follow-ons, neither a blocker:
  - **Noise-level subtraction.** SICD's `Radiometric.NoiseLevel` describes the
    noise-equivalent floor; subtracting it in the power domain before the scale
    factor would make low-backscatter surfaces (calm water, shadow) honest rather
    than floor-limited. Left out because it needs the `NoisePoly` /
    `NoiseLevelType` handling (absolute vs. relative) that no Umbra product
    currently exercises.
  - ~~**Nothing downstream records the calibration.**~~ ✅ **Done**
    (`convert.conversion_tags` / `read_conversion_tags` / `umbra convert
    --provenance`). Every raster the module writes now carries namespaced
    `UMBRA_*` GeoTIFF metadata naming the calibration, the RTC model **and the
    reference incidence angle it resolved to** (the scene angle when none was
    asked for, so the tag records what ran rather than what was requested), the
    DEM/geoid actually used, the projection, the resampling kernel, the amplitude
    scale, a one-line `UMBRA_UNITS` statement of what a pixel value *is*, the
    umbra-py version, and the CC-BY licence + attribution — design principle 4
    applied to a derivative product. Steps that did **not** run report `"none"`
    rather than a missing key, so an absent tag never has to be read as either
    "not applied" or "not recorded", and only the source's *file name* is
    recorded (a local directory is not provenance and should not travel). Read
    back with `read_conversion_tags` (prefix stripped, lower-cased), `umbra
    convert --provenance FILE` (JSON, takes no `DST`), or plain `gdalinfo` —
    they are ordinary tags, so the whole ecosystem can read them. The geocoded
    path stamps the in-memory dataset before the COG driver copies it out, so
    the tags reach the emitted file. Pure-stdlib tag construction, no new
    dependency, no model call; offline-tested in `tests/test_convert.py`.
    Follow-on, not a blocker: nothing *consumes* the tags yet — `to_xarray` /
    `to_stack` could refuse to stack rasters whose `UMBRA_CALIBRATION` or
    `UMBRA_SCALE` disagree, the same "a mixed selection is not a measurement"
    check `POST /artifacts/stats` makes for polarization.
- **MultiRTC interop.** Interop with
  [MultiRTC](https://github.com/MultiSAR/MultiRTC) is a heavier,
  research-oriented job and remains deferred.
- ~~**Auto-fetch a DEM for the scene footprint.**~~ ✅ **Done** (`umbra_py.dem` /
  `umbra convert --dem auto`). `dem="auto"` / `--dem auto` resolves the 1°×1°
  Copernicus GLO-30 tiles covering the scene's projected footprint, pulls them
  from the public AWS Open Data bucket (skipping the ocean gaps that 404, merging
  several into a mosaic), and terrain-orthorectifies against the result — no more
  hand-finding a DEM. The tile math is stdlib-only and offline-tested; the fetch
  reuses the resume-safe `download_url` and is injectable, so the whole path is
  covered without network (only the multi-tile mosaic touches `rasterio`).

---

## Register `umbra-mcp` in the MCP registries and Anthropic's directory

- **Surfaced in:** the `umbra-mcp` MCP server PR (`AI_INTEGRATION_IDEAS.md` B1).
- **Code:** `src/umbra_py/mcp_server.py`, `pyproject.toml` (`[mcp]` extra,
  `umbra-mcp` console script).

The server itself is shipped and runnable (`umbra mcp` / `uvx umbra-mcp`), but
registering it in the public MCP registries and Anthropic's directory — the
discovery half of the deliverable — is still open. Follow-ons named in the B1
doc: ~~a LangChain community tool wrapper reusing the same tool shapes~~ ✅
**done** (`umbra_py.langchain` / `[langchain]` extra, see below); ~~the parallel
LlamaIndex wrapper~~ ✅ **done** (`umbra_py.llamaindex` / `[llamaindex]` extra —
`umbra_tools()` returns the same ten JSON callables (reused verbatim from
`mcp_server`, so all three front doors cannot drift) as native
`FunctionTool`s, plus the three render tools re-implemented natively (returning
a `RenderResult` whose string form is the caption and whose `.png` rides on the
`ToolOutput.raw_output`) so the surface never pulls in the MCP SDK;
offline-tested in `tests/test_llamaindex.py`); and returning the
polarization-mixing warning as structured text alongside the `change_composite`
image block is still open. With MCP → LangChain → LlamaIndex all shipped, the
agent-framework reach trilogy is complete.

---

## Acquisition-property search filters follow-ons (polarization / incidence / resolution shipped)

- **Surfaced in:** the SAR acquisition-property filters PR (`STRATEGY.md` §3 /
  `AI_INTEGRATION_IDEAS.md` §B2 STAC follow-on).
- **Code:** `src/umbra_py/models.py` (`UmbraItem.matches_filters`),
  `catalog.py` / `index.py` (`search`, `search_live`, `_search_archive`),
  `umbra search` in `cli.py` (`_acquisition_filter_options`),
  `mcp_server.py` (`search_catalog`), `context.py` (`_SEARCH_PARAMETERS`).

`search(polarizations=…, min_incidence=…, max_incidence=…, max_resolution=…)`
filters by the SAR-native acquisition properties across the live walk, the local
index, the read-through search, the Canopy archive, `umbra search` and the MCP
`search_catalog` tool — one shared predicate (`UmbraItem.matches_filters`), no
schema change, deterministic and offline-tested. Additive follow-ons, none a
blocker:

- ~~**Wire the filters into the render/analysis commands.**~~ ✅ **Done.**
  `change`, `timescan`, `swipe`, `gallery`, `map` and `chips` now carry the shared
  `@_acquisition_filter_options` decorator (`--pol` / `--min-incidence` /
  `--max-incidence` / `--max-resolution`), threaded through `_gather_items` via
  `_acquisition_filter_kwargs`, so `umbra change --pol VV` gathers a
  single-polarization series *directly* instead of relying on the after-the-fact
  mixed-polarization warning (the warning still fires for an unfiltered mixed
  selection). The filters apply only in search mode — passing explicit item URLs
  is unaffected — and the set filters are recorded in the `--json` render
  manifest's `parameters` (only when set, so an unfiltered render's manifest is
  unchanged; `chips` writes its own manifest and is unaffected). Uses the same
  predicate every other surface shares (`UmbraItem.matches_filters`), so no new
  filtering logic; offline-tested in `tests/test_acquisition_filters.py` (each of
  the six commands forwards the kwargs; the unset-→`None` case).
- ~~**Expose the filters on the `umbra serve` STAC Query extension.**~~ ✅
  **Done.** `umbra serve`'s `/search` and `/collections/{id}/items` now filter on
  the SAR acquisition properties three ways — GET params (`?polarizations=VV,VH`,
  `min_incidence`, `max_incidence`, `max_resolution`), plain top-level POST body
  fields, and a proper STAC **Query extension** object using the namespaced
  property names (`{"sar:polarizations": {"in": ["VV"]}}`,
  `{"view:incidence_angle": {"gte": 20, "lte": 40}}`,
  `{"sar:resolution": {"lte": 0.5}}`). `parse_query` now returns a
  `QueryFilters` NamedTuple and gained a numeric range operator (`gte`/`lte`
  together for incidence) alongside the existing scalar operators; an
  unsupported operator or non-numeric value is a hard `400`, never a silent drop
  (`parse_polarizations` / `_as_float` / `_opt_float` do the coercion). The
  filters push down to the same `UmbraItem.matches_filters` predicate every other
  surface shares (both `CatalogIndex` and the live `UmbraCatalog` `search`
  already accept them), so `pystac-client` and OpenAPI agents now get the "every
  surface agrees" bar. GET pagination carries the filters into the `next` link.
  Offline-tested in `tests/test_serve.py` (parser mapping + operator/value
  rejection, and endpoint filtering by polarization / incidence range / max
  resolution across GET, POST top-level, POST query object, top-level override,
  and pagination survival).
- ~~**Let `umbra ask` plan the filters.**~~ ✅ **Done.** The planner's JSON
  schema now carries `polarizations`, `min_incidence`, `max_incidence`, and
  `max_resolution` (`SearchPlan` fields, `_PLAN_KEYS`, the system-prompt schema
  block), and `planner.parse_plan` — the determinism boundary — validates each
  before it becomes a filter: polarizations upper-cased/de-duplicated (an open
  set, like `serve.parse_polarizations`), incidence/resolution coerced to
  positive floats (`_coerce_positive_float`; a hallucinated `0` is a
  self-describing `AskError`), and inverted incidence bounds rejected like a
  start-after-end date. The resolved filters render into the audited
  `umbra search …` command (`--pol` repeatable, `--min-incidence` /
  `--max-incidence` / `--max-resolution`) and flow through
  `SearchPlan.to_search_kwargs()` into the same `UmbraItem.matches_filters`
  predicate every other surface shares, so a plain sentence ("VV scenes at low
  incidence over Utah") now resolves to a real filtered search. No new model call
  beyond the existing planning step, no new dependency; offline-tested in
  `tests/test_planner.py`. This closes the last named surface in this
  acquisition-filter follow-on.

## Grow the `umbra serve` STAC API (query extensions + a hosted instance)

- **Surfaced in:** the `umbra serve` STAC API PR (`AI_INTEGRATION_IDEAS.md` B2 /
  `DEMO_APP_GAPS.md` Path B).
- **Code:** `src/umbra_py/serve.py`, `pyproject.toml` (`[serve]` extra).

The read-only STAC API is shipped (landing / conformance / collections / items /
`GET`+`POST /search` with bbox, datetime, ids and token pagination), and now
renders artifacts on demand (`GET /artifacts/quicklook/{id}.png`, `POST
/artifacts/change`, `POST /artifacts/timescan`, `POST /artifacts/swipe`, and the
one that is numbers rather than a picture, `POST /artifacts/stats`), each
disk-cached by its inputs and wrapping the existing `viz` / `load` functions
behind injectable renderers. `GET /artifacts/thumbnail/{id}.png` serves the baked
quicklook thumbnail (`umbra index bake-thumbnails`) straight from the index with
no render. The `umbra demo` front end now calls these endpoints (see
the Done log), closing the self-serve R4 loop. **Async job semantics for long
renders are now shipped** (see the Done log): a composite request can opt in to
`"async": true`, get a `202 Accepted` + a job id, poll `GET /jobs/{id}`, and
fetch the result from `GET /jobs/{id}/result` (the disk cache is the result
store). **The STAC Query extension now exposes the index's Umbra-specific filters**
(see the Done log): `/search` and `/collections/{id}/items` take
`product_types`, `area`, `fuzzy` **and the SAR acquisition properties**
(`polarizations` / `sar:polarizations`, `min_incidence` / `max_incidence` /
`view:incidence_angle`, `max_resolution` / `sar:resolution`) — as GET params,
top-level POST fields, or a STAC `query` object — advertised via the
`item-search#query` conformance class. Open follow-ons:

- ~~**Geometry `intersects`.**~~ ✅ **Done** (stale entry — it shipped with the
  polygon `intersects` search, see the CHANGELOG). `serve.parse_intersects`
  accepts a GeoJSON geometry on `GET /search` (as a JSON string) and in the POST
  body, rejects it alongside `bbox` per the spec, and threads it through
  `run_search` to the backend, where `CatalogIndex` pushes the polygon's bbox
  into SQL as a prefilter and then runs the exact `UmbraItem.intersects_polygon`
  test. Offline-tested in `tests/test_geometry.py`.
- **A hosted community instance.** The local-first server has no operational
  cost; a public instance is a policy decision (COG-streaming egress) that would
  make the archive queryable with zero install — pair it with the demo front end
  in `DEMO_APP_GAPS.md` Path B.

---

## Canopy commercial-archive backend follow-ons (`UmbraCatalog(token=...)` shipped)

- **Surfaced in:** the Canopy backend PR (`docs/STRATEGY.md` 5.1).
- **Code:** `src/umbra_py/catalog.py` (`_search_archive` / `_archive_page`),
  `src/umbra_py/constants.py` (`CANOPY_ARCHIVE_URL`), `umbra search --token`.

The commercial archive is now searchable behind the same `search()` interface
(bearer token → STAC API POST search + `rel="next"` pagination, offline-tested
against a mocked API). Open follow-ons, none a blocker:

- **Push `product_types` / `area` down as STAC query/filter extensions.** They
  are applied client-side today (exact parity with the open-bucket path). Once
  the concrete Canopy field names are confirmed against the live API, sending
  them as a STAC *query*/*filter* body would let the server pre-filter and cut
  transferred pages. This needs a real token to verify, so it is deliberately
  deferred rather than guessed.
- ~~**`get_item(id)` against the archive.**~~ ✅ **Done.**
  `UmbraCatalog.get_item(item_id)` is the keyed-retrieval complement to
  `search`'s listing: it POSTs the STAC API `ids` search extension
  (`{"ids": [item_id], "limit": 1}`) to the same `/archive/search` endpoint the
  search path uses — so no new endpoint is guessed and the whole path is
  offline-tested against a mocked API (`tests/test_canopy.py`). It requires a
  token (the open bucket has no id→item index — resolve an open-data item from a
  sidecar URL or `CatalogIndex.get`), guards against a server that ignores the
  `ids` filter (only the exact id is accepted), and inherits `_archive_page`'s
  bearer auth + 401/403/500 handling. Surfaced on the CLI as `umbra info <id>
  --token` (with the `$UMBRA_CANOPY_TOKEN` fallback), the retrieval sibling of
  `umbra search --token`. ~~Still open: wiring the archive lookup into the MCP
  `get_item` tool (the MCP server has no token concept yet — a separate
  surface).~~ ✅ **Done.** `umbra_py.mcp_server` now reads `$UMBRA_CANOPY_TOKEN`
  (via `_canopy_token()` — a secret configured once in the server's env, never a
  model-supplied tool argument): when set, `search_catalog` and `watch_site`
  query the commercial archive (`source: "canopy-archive"`) and `get_item`
  resolves a bare acquisition id through `UmbraCatalog(token=...).get_item(id)`
  (a full `://` URL is still read directly as an open-data sidecar). The
  `_search_source(local, token)` guard rejects `local=True` with a token (the
  archive has no local index), and the server's `instructions` announce archive
  mode when a token is configured. Offline-tested in `tests/test_mcp_server.py`
  with a fake archive catalog (no credentials, no network); the token is only
  ever handed to the catalog, never surfaced in a result.
- **Verify request/response shapes against the live Canopy API.** The client is
  built to the STAC API *standard*; confirm the exact search body, collection
  ids, and pagination link shape Canopy emits, and adjust if it deviates. Add a
  `network`-marked smoke test gated on a `UMBRA_CANOPY_TOKEN` secret.
- ~~**Wire `--token` into the visual commands.**~~ ✅ **Done.** `map`, `gallery`,
  `change`, `timescan`, `swipe` and `chips` now take the same `--token` (shared
  `_token_option`, `$UMBRA_CANOPY_TOKEN` fallback, and a `_check_token_not_local`
  guard against combining it with `--local` / `--index-db`), threaded through
  `_gather_items` → `_search_source(local, db_path, token)` to the commercial
  backend — so a paying user renders and analyses the archive they pay for with
  the identical flags. Offline-tested in `tests/test_cli_token.py` against a
  `responses`-mocked STAC API (dispatch, token→archive flow, per-command wiring,
  env-var fallback, mutual-exclusion guard). The whole-catalog explorers (`demo`,
  `tiles`) and the embedding/index builders are deliberately left on the open
  bucket — they gather large catalog slices, where a live paid-archive walk is out
  of place.

---

## C1 natural-language search follow-ons (all four steps now shipped)

The four C1 steps — relative dates (`dates.py`), the deterministic fuzzy task
matcher (`fuzzy.py`), the model-planned `umbra ask` (`planner.py`), and the
semantic embedding index (`semantic.py`) — are all shipped (see the **Done**
log). Optional follow-ons that build on them, not blockers:

- ~~**LangChain tool wrapper** reusing the semantic matcher (same shapes,
  different registration) — worth doing for reach.~~ ✅ **Done**
  (`umbra_py.langchain` / `[langchain]` extra). `umbra_tools()` wraps the MCP
  server's deterministic tool callables (including `search_catalog`'s
  `semantic=True` mode) as native `StructuredTool`s — no duplicated business
  logic, so the LangChain and MCP surfaces cannot drift; the render tools are
  re-implemented natively (returning the PNG as a `content_and_artifact`
  artifact) so the LangChain surface never pulls in the MCP SDK. Offline-tested
  in `tests/test_langchain.py` (surface, schema inference, invocation, artifact,
  guards). ~~The parallel **LlamaIndex** `FunctionTool` wrapper — same callables,
  a third registration — is the remaining reach step.~~ ✅ **Done**
  (`umbra_py.llamaindex` / `[llamaindex]` extra). `umbra_tools()` wraps the same
  ten MCP callables as native `FunctionTool`s (single source of truth, no drift)
  plus the three render tools re-implemented natively (a `RenderResult` carrying
  the caption as its string form and the PNG on `.png`, surfaced as
  `ToolOutput.raw_output`, so the surface never pulls in the MCP SDK).
  Offline-tested in `tests/test_llamaindex.py`. The MCP → LangChain → LlamaIndex
  agent-framework reach trilogy is now complete.
- ~~**MCP `search_catalog` semantic mode.**~~ ✅ **Done.** `search_catalog`
  gained a `semantic=True` flag (with a `min_score` cosine threshold and a
  `search-by-description` prompt): it treats `area` as a plain-language *site
  description*, resolves it to the closest task names by meaning via the prebuilt
  `SemanticTaskIndex` (`_resolve_semantic_area`), searches the best one over the
  chosen backend, and returns `resolved_area` + the ranked `semantic_matches` so
  the agent can audit and retry — giving agents the "describe a site you can't
  name" aliasing the CLI's `umbra semantic search` has. Gated (like the CLI) on a
  prebuilt semantic index and the `[ai]` embedding key; `semantic` and `fuzzy` are
  mutually exclusive, and the only model call is turning the query into a vector
  (an injectable embedder), so the whole path is offline-tested in
  `tests/test_mcp_server.py` with a deterministic concept embedder (resolve,
  no-match empty-audit trail, missing-index/missing-key errors, `area` required,
  fuzzy mutual-exclusion) — no key, no network.
- **Embed task *descriptions*, not just names.** The current index embeds the
  task label; if Umbra publishes per-task descriptions, embedding those too would
  widen recall further.

---

## C2 VLM-in-the-loop follow-ons (`umbra describe` shipped)

- **Surfaced in:** the `umbra describe` PR (`AI_INTEGRATION_IDEAS.md` C2).
- **Code:** `src/umbra_py/describe.py` (`[ai]` + `[viz]` extras),
  `constants.AI_PROVENANCE`.

`umbra describe` (scene description) is shipped — a vision model reads the
rendered quicklook plus the A3 context card and returns a provenance-stamped
`{summary, observed_features[], confidence, caveats[]}`. The rest of C2 is still
open and builds on the same boundary:

- ~~**`umbra change --narrate`** (the second half of C2).~~ ✅ **Done**
  (`src/umbra_py/narrate.py`). After rendering a change composite, `compute_change_stats`
  divides the co-registered scene into a coarse grid and measures the mean *signed*
  backscatter change in decibels per block; the composite PNG and that dB grid go to
  a VLM, which returns a validated `ChangeNarration` (`{summary, changes[], confidence,
  caveats[]}`) grounded in — and carrying — the deterministic grid, so every statement
  cites a number, not vibes. Reuses `describe.py`'s provider plumbing and the
  `parse_*` boundary, stamps every narration with CC-BY + `AI_PROVENANCE`, and (like
  `describe`) the model call is an injectable `Narrator` and the render an injectable
  `ChangeRenderer`, so the whole path is offline-tested with no `[ai]`/`[viz]` extra.
- ~~**MCP `narrate_change` tool.**~~ ✅ **Done.** `umbra-mcp` gained a
  `narrate_change(urls, asset, db, max_size, model)` tool (plus a `narrate-change`
  workflow prompt) wrapping `narrate()` unchanged — the sibling of `describe_scene`
  on the MCP surface, and the **second** (and only other) tool that consults a model.
  It composites two or three same-polarization passes, computes the deterministic
  per-block dB grid, has the model narrate *only* the change the numbers support, and
  returns the validated `ChangeNarration` dict with the grid embedded as
  `change_stats` so an agent can audit every statement. Gated (like the CLI) on the
  `[ai]` key — it raises the same setup error and never runs implicitly — refuses mixed
  polarizations before any render or model call (the same guard `change_composite`
  holds), and holds the determinism boundary (`AI_INTEGRATION_IDEAS.md` §A4): the
  picture and the numbers are deterministic, the model only interprets, and every
  narration is stamped with CC-BY + `AI_PROVENANCE`. Offline-tested in
  `tests/test_mcp_server.py` with an injected narrator + render (no `[ai]`/`[viz]`
  extra, no key, no network), including the mixed-polarization refusal and the
  missing-key setup error. ~~Remaining reach follow-on: surface `narrate_change`
  on the LangChain / LlamaIndex wrappers too.~~ ✅ **Done.** `narrate_change` is now
  registered in both `umbra_py.langchain` and `umbra_py.llamaindex` `_JSON_TOOLS`
  (imported verbatim from `mcp_server`, so no drift — the same callable on all
  three front doors), bringing the LangChain / LlamaIndex inventories to full
  parity with the MCP server's thirteen tools. It is the second opt-in model tool
  on those surfaces (with `describe_scene`), gated on the `[ai]` key so it never
  runs implicitly, and holds the same determinism/`AI_PROVENANCE` boundary.
  Offline-tested in `tests/test_langchain.py` / `tests/test_llamaindex.py`
  (surface parity, same-callable no-drift, an end-to-end narration through each
  wrapper with an injected narrator + render, and the mixed-polarization refusal);
  the README agent-tool inventories and the two module docstrings were updated.
  This completes the MCP → LangChain → LlamaIndex agent-framework reach for the
  change-narration capability.
- ~~**MCP `describe_scene` tool.**~~ ✅ **Done.** `umbra-mcp` gained a
  `describe_scene(url, asset, db, max_size, model)` tool (plus a `describe-scene`
  workflow prompt) wrapping `describe()` unchanged, so an MCP client gets the
  structured `{summary, observed_features, confidence, caveats}` reading directly.
  It is one of the **two tools on the server that consult a model** (with
  `narrate_change` above), a deliberate opt-in
  exception gated (like the CLI) on the `[ai]` key — it raises the same setup error
  and never runs implicitly. The boundary holds: the picture and the metadata card
  are deterministic, the model only interprets (its reply passes `parse_description`),
  and every reading is stamped with CC-BY + `AI_PROVENANCE`. Offline-tested in
  `tests/test_mcp_server.py` with an injected describer + render (no `[ai]`/`[viz]`
  extra, no key, no network), including the missing-key setup error. The module
  docstring's "nothing here calls a model" invariant was revised to name this
  single, honest exception.
- **A `describe` render is a fresh S3 read every call.** When the demo/thumbnail
  bake (`DEMO_APP_GAPS.md` G6) lands, feed the cached quicklook into `describe`
  via its injectable `render=` hook instead of re-streaming the COG.

---

## C3 monitoring follow-ons (`umbra watch` shipped)

- **Surfaced in:** the `umbra watch` PR (`AI_INTEGRATION_IDEAS.md` C3).
- **Code:** `src/umbra_py/watch.py`, `umbra watch` in `cli.py`.

`umbra watch` (idempotent delta detection) is shipped — it searches, diffs the
results against the set of acquisitions previous runs already reported (state in
the `CatalogIndex` `meta` table), returns only the new ones, and remembers them,
so cron / a GitHub Action / an agent loop can supply the schedule. No model is
called. The remaining C3 pieces build on it:

- ~~**MCP `watch_site` tool / prompt.**~~ ✅ **Done** (stale entry — it shipped
  with the MCP server's tool inventory). `watch_site` wraps the deterministic
  `watch()` callable unchanged, derives the watch name from the query via
  `watch_key` when none is given, persists state in the same `CatalogIndex`
  `meta` table (`MetaWatchStore`) so a watch survives across sessions, and is
  registered on the LangChain and LlamaIndex surfaces from the same callable.
  A `watch-site` prompt ships beside it.
- ~~**A packaged monitoring recipe/notebook.**~~ ✅ **Done**
  (`examples/06_site_monitoring.ipynb`). The standing-analyst notebook wires
  `umbra watch` → `select_change_frames` → `save_change_composite` into one
  runnable, self-checking example: it stands up a watch over a repeat-imaged
  site, asserts the first run reports every pass as new, asserts an immediate
  re-run reports **zero** (the idempotency guarantee a scheduler depends on),
  then composites the new passes into a change image. It holds the gallery's
  grain (a small deterministic search with `assert`s in every code cell, no
  model call — `umbra change --narrate` is described as the optional VLM
  follow-on in prose), so it is guarded offline by `tests/test_examples.py` and
  executes end-to-end under `pytest -m network`. The `viz` extra renders the
  composite; the notebook also points at `MetaWatchStore` for cross-run
  persistence and the `watch_site` MCP tool for the conversational path.

---

## C4/C5 ML dataset follow-ons (`umbra chips` shipped)

- **Surfaced in:** the `umbra chips` PR (`AI_INTEGRATION_IDEAS.md` C4 /
  `STRATEGY.md` 5.5).
- **Code:** `src/umbra_py/chips.py`, `umbra chips` in `cli.py`.

`umbra chips` (fixed-size, georeferenced ML tiles + a `.jsonl`/`.geojson`
manifest, `[load]` extra, no model call) is shipped. Follow-ons that build on it,
not blockers:

- ~~**Publish the chip manifest as stac-geoparquet.**~~ ✅ **Done.** `umbra chips
  --manifest chips.parquet` (and `write_manifest_parquet` / any `.parquet`
  manifest path) writes the manifest as stac-geoparquet — each chip as one STAC
  Item row (footprint geometry + the same fields as the `.jsonl` record as
  properties, the chip file as its `data` asset) — reusing the `[export]` extra's
  `stac_geoparquet.arrow` writer, so a large chip set is queryable by DuckDB /
  geopandas / pyarrow without loading every line. Format is chosen by the manifest
  extension, so the CLI is unchanged beyond accepting `.parquet`; it needs the
  `[export]` extra alongside `[load]`, stays deterministic (no model call), and is
  offline-tested in `tests/test_chips.py` (round-tripped through pyarrow, incl. the
  null-datetime row).
- **Chip the complex products.** The chipper reads amplitude rasters (GEC/CSI);
  chipping SICD/CPHD would need the slant-plane handling that `convert.py`
  begins — related to the still-open SICD → geocoded COG gap in `STRATEGY.md` 5.5.

---

## Time-series datacube follow-ons (`to_stack` / `umbra stack` shipped)

- **Surfaced in:** the datacube PR (`docs/STRATEGY.md` §2 / 5.5).
- **Code:** `src/umbra_py/load.py` (`to_stack`, `stack_to_geotiff`,
  `STACK_EXTENTS`, `_stack_bounds`, `_mask_slice`), `umbra stack` in `cli.py`.

`to_stack` co-registers several acquisitions onto one shared EPSG:4326 grid and
returns a `(time, y, x)` `xarray.DataArray`; `stack_to_geotiff` / `umbra stack`
write the same cube as a multi-band GeoTIFF. Deterministic, no model call,
behind the existing `[load]` extra. Follow-ons that build on it, none a blocker:

- ~~**A projected (equal-area) output grid.**~~ ✅ **Done** (`to_stack(crs=…)` /
  `stack_to_geotiff(crs=…)` / `umbra stack --crs`). `crs="utm"`
  (`STACK_AUTO_CRS`) resolves the UTM zone containing the stacked ground from
  the sources' own footprints and warps the shared grid there, so cells are
  metre-sized and equal-area and a cell count is an area; any other value is a
  CRS name validated through `rasterio` (a typo raises rather than warping to
  nothing). `bbox` / `--clip-bbox` stays lon/lat either way, the written GeoTIFF
  records the resolved CRS in its tags, and `crs=None` keeps the lon/lat default
  unchanged. Offline-tested in `tests/test_load.py`.
- ~~**Lazy / chunked reads.**~~ ✅ **Done** (`to_stack(lazy=True)` /
  `stack_to_geotiff(lazy=True)` / `umbra stack --lazy`, the new `[dask]` extra).
  Each acquisition is one `dask` task and one chunk, so nothing is fetched until
  something asks for values and the cube stops costing `max_size²` × the number
  of passes. The grid is still resolved eagerly from every footprint — it is
  what makes the slices comparable — so a non-overlapping series still fails at
  the call rather than inside a later reduction. The consumers moved with it:
  `_write_stack_geotiff` writes band by band, and `stack_stats` streams the
  series (at most the first, previous and current pass resident), which made its
  memory a function of the grid rather than of the series for eager cubes too.
  The spatial breakdown became `_BlockChanges`, accumulating each block's
  pass-to-pass steps as the passes arrive; every `_pair_change` was already
  independent, so the numbers are unchanged (asserted by comparing a lazy cube's
  whole statistics object to an eager one's). Offline-tested in
  `tests/test_load.py`. Follow-ons, none a blocker:
  - ~~**Chunk *within* a slice, not just across the series.**~~ ✅ **Done**
    (`to_stack(chunk_size=N)` / `stack_to_geotiff(chunk_size=N)` / `umbra stack
    --lazy --chunk-size N`). One chunk per acquisition made the unit of work a
    whole slab, so a single pass at a large `max_size` was still read and held
    whole — a floor of `max_size²` floats no amount of streaming lowered.
    `chunk_size` cuts each pass into `N`-square windows read independently, so
    the unit becomes `N²` and the achievable sharpness stops depending on how
    much of one scene fits in memory. The windowed read needed no new reader:
    `_sub_grid` restricts the shared `_StackGrid` to a window's rows/columns —
    same CRS, same cell size, edges on the parent's cell boundaries — so
    `_open_slab` reads it unchanged and a window is pixel-identical to that
    region of the whole-slab read (pinned by stacking a per-pixel ramp both ways
    and comparing exactly, partial edge windows included). The multiplied
    request count this entry named is real and stated rather than hidden
    (⌈h/N⌉ × ⌈w/N⌉ reads per pass instead of one), which is why it is opt-in and
    why `chunk_size` without `lazy` is a hard error rather than a silent no-op.
    `_write_stack_geotiff` writes band-by-band **and window-by-window**, driven
    by the cube's own chunks (`_cube_windows`), so the file path never
    re-materialises what the reader avoided; a cube with no windows takes the
    previous whole-band write and the file is byte-identical. Offline-tested in
    `tests/test_load.py`. ~~Follow-on, not a blocker: **`stack_stats` still holds
    one slice per pass** — its per-pass distribution reports medians and
    percentiles, which need the whole pass, so measuring a cube keeps the
    ceiling writing one no longer has. Streaming it would mean approximate
    quantiles (a t-digest or a two-pass histogram), i.e. changing the numbers,
    which is a bigger decision than a chunk size.~~ ✅ **Done**
    (`stack_stats(windowed=True)` / `umbra stack --stats-windowed`). The walk is
    turned inside out — one window of the shared grid outside, the series inside
    — so three *windows* are resident instead of three slices and the ceiling
    writing no longer has is gone from measuring too. The decision this entry
    deferred was taken the narrow way: everything that is a count or a sum stays
    **exact** (`_PairAccum` for every change record, including the per-block
    breakdown, and `_DistAccum` merging spread with Chan's parallel-variance
    update), and only the percentiles are estimated, from a mergeable 0.05 dB
    histogram on the decibel axis (`_QuantileSketch`, good to about a bin) rather
    than a t-digest. What makes an approximation acceptable here is that it is
    *labelled*: `quantile_method` / `quantile_bin_db` and a caveat sentence
    appear exactly when the numbers are estimates, and a default summary is
    byte-identical to before. Blocks are cut from the shared grid, not from a
    window, so a misaligned window edge leaves the breakdown identical (pinned by
    a 7-wide window against a 3-block grid on a 24-wide cube). Offline-tested in
    `tests/test_load.py`. Follow-ons, neither a blocker:
    - ~~**The server and the agent tools don't expose it.**~~ ✅ **Done for the
      server** (`"windowed": true` on `POST /artifacts/stats`). The decision this
      entry demanded was taken the visible way: it is a **request option**
      (`stats_options`), not an instance policy, so it enters
      `artifact_cache_key` for free and the failure mode named here — a cached
      artifact whose quantiles depend on a flag nobody can see — cannot arise.
      The exact numbers are pinned identical through the endpoint's own renderer
      (means, spreads, valid-cell counts, every change record, the whole
      `spatial` breakdown) with only the percentiles moving, by at most a bin,
      and the response carries `quantile_method` / `quantile_bin_db` so a client
      can tell them apart. It needs the instance's cube to be chunked to lower
      anything, so `--stack-chunk-size`-less instances refuse it with a `400`
      naming the flag (before the `load` import), and `umbra serve` echoes the
      capability at startup. Offline-tested in `tests/test_serve.py`.
      Follow-ons, neither a blocker:
      - **The agent tools still don't take it, deliberately.** The MCP /
        LangChain / LlamaIndex `stack_stats` tools build an eager cube at
        `max_size=512`, where there is no ceiling to lift — so `windowed` there
        would be a model-facing knob whose only effect is making the percentiles
        approximate. Wire it only if those tools ever grow the lazy/chunked build
        that would make it mean something; the server was the front door with the
        memory problem.
      - **Only the refusal advertises the capability.** A client discovers that
        an instance can measure in windows by asking and reading the `400`;
        nothing in the landing page or `/healthz` says so up front. Cheapest fix
        if it matters: a field on the landing page's `stats` link.
    - **The histogram is a Python dict of bin → count.** Fine at the sizes this
      sees (a few thousand occupied bins per pass), but a pass spanning hundreds
      of decibels holds proportionally more. If that ever matters, cap the axis
      or widen the bin rather than reaching for a t-digest.
  - ~~**`umbra serve`'s `POST /artifacts/stats` still stacks eagerly.**~~ ✅
    **Done** (`umbra serve --stack-lazy` / `--stack-chunk-size N` /
    `--stack-scheduler {synchronous,threads}`, i.e. `serve.StackExecution`
    threaded through `build_app(stack_execution=…)` into `default_renderers`).
    Exactly the shape this entry named: it *is* left to the operator, but as a
    supported instance-wide setting rather than as a gap. Both conditions it
    listed are answered in place — the extra is the server's to install (without
    it a stats request answers `501` naming `umbra-py[dask]`, like a missing
    `load`), and the scheduler is an explicit choice defaulting to
    `synchronous`, so a render runs on the request's own worker and the
    container's thread count stays whatever its ASGI server was configured with;
    `threads` opts into dask's pool. `processes` is deliberately not offered
    (the chunks stream COG bytes through GDAL handles that do not fork cleanly).
    `dask.config.set` is entered as a context manager around the one render so
    the choice cannot leak into another thread or a caller's process, and the
    eager default never imports `dask`. The policy is *not* in
    `artifact_cache_key`: a lazy cube's numbers are identical to an eager one's,
    so flipping it invalidates nothing — pinned by rendering one request under
    all four policies and comparing the JSON bytes. Offline-tested in
    `tests/test_serve.py`. Follow-ons, neither a blocker:
    - **The async job path shares the policy.** A `"async": true` stats request
      runs the same renderer on the job executor's thread, so `--stack-scheduler
      threads` there means dask's pool *inside* a pool thread. `synchronous`
      (the default) is the safe pairing; a per-path policy would only be worth
      it if an operator wanted sync requests bounded and jobs fast.
    - **Nothing reports the policy over HTTP.** The CLI echoes it at startup,
      but a client cannot tell a lazy instance from an eager one — correct, since
      the answers are identical, though an operator debugging memory has to read
      the process's own logs.
  - **The eager path still opens every source up front.** Both paths open all
    the datasets to resolve the grid (metadata only, but N handles at once). A
    two-pass resolve — footprints first, then reads — would drop that to one at a
    time; it saves handles, not bytes, so it was not worth the churn here.
- **Share the co-registration with `viz`.** `viz._coregister_bands` does the
  same warp-and-decimate for the render commands and predates this. They now
  differ in what they return (bare arrays + bounds vs. a labelled cube) and in
  masking (`viz` keeps raw values for its own stretch), so they were left
  separate rather than forced into one function; if a third caller appears,
  extract the shared VRT/grid step.
- ~~**A notebook in the gallery.**~~ ✅ **Done**
  (`examples/08_time_series_datacube.ipynb`). Exactly the flow named here —
  search a repeat-imaged task → one polarization → `to_stack` → the baseline vs.
  latest dB delta as a map — plus the reductions that shipped after this entry
  was written: `stack_stats` for the per-pass series and the net first→last
  record, `blocks=3` for the peak block and the ASCII heat-grid, and
  `block_series=True` for that block's whole pass-to-pass sequence. The cube is
  built with `crs=STACK_AUTO_CRS`, since `changed_area_km2` is the answer only a
  projected grid can give, and the notebook opens on why `04`'s per-scene means
  are not the same measurement. Self-checking like the rest (the asserts include
  that the peak interval is a member of the series it was picked from), guarded
  offline by `tests/test_examples.py` and executed by it under
  `pytest -m network`; it falls back to `extent="union"` when a task's footprints
  don't all overlap and caps at six passes because the cube is in memory (a cap
  `to_stack(lazy=True)` now lifts, though the notebook keeps it: its point is the
  flow, and it should not need an extra beyond `[load]` to run).
  Follow-on, not a blocker: it fetches its own site from a live search, so the
  notebook cannot pick a site with a *known* story — a curated task id (or an
  `--area` the showcase already features) would make the printed numbers
  reproducible and give the narrative something specific to point at.
- ~~**Surface it on the agent front doors.**~~ ✅ **Done** (`umbra_py.stack_stats`
  / `umbra stack --stats` / the `stack_stats` tool on MCP, LangChain and
  LlamaIndex, plus the `quantify-change` MCP prompt). The raw array was never the
  thing to hand a model; `stack_stats(cube)` reduces it to JSON — per-pass
  distribution, the signed dB change against the previous pass, and a net
  first-to-last record with `changed_area_km2` when the grid is projected — which
  is what crossed the "images/JSON are the API" boundary. The agent tool defaults
  to `crs="utm"` and the dB scale so the numbers are equal-area and radiometric
  without the model choosing; the CLI reuses the same callable, with `--out` now
  optional so `--stats` alone measures without writing. Offline-tested in
  `tests/test_load.py` and `tests/test_mcp_server.py`.
- ~~**A spatial breakdown over the whole series.**~~ ✅ **Done**
  (`stack_stats(cube, blocks=N)` / `umbra stack --blocks N` / the `blocks`
  argument on the `stack_stats` tool across MCP, LangChain and LlamaIndex). The
  merge named here: every pass is cut into the same N×N grid
  `narrate.compute_change_stats` uses for two passes (the two share
  `_compass_label` / `_split_slices`, so a block's compass label means one
  thing), and each block reports its own `net_change`, `bounds`, a
  `center_lonlat` to map or geocode it by, and the `peak_interval` it moved most
  between — plus a `peak_block` headline and a north-up ASCII `grid_text`. It
  exists because a scene-wide mean dilutes a localized change (a corner that
  brightens 12 dB reads as 0.75 dB over 16 blocks). Unobserved blocks answer
  `None`, not zero. `--blocks` implies `--stats` and rides in the render
  manifest's `stats` field; the agent default is `0` so the payload only grows
  when a model asks *where*. Offline-tested in `tests/test_load.py` and
  `tests/test_mcp_server.py`. ~~Still open under this heading: the block series is
  reported per block rather than as a per-block *time series* — each block's full
  pass-to-pass sequence is computed and then reduced to its peak, so surfacing
  all of it is a payload decision, not new arithmetic.~~ ✅ **Done**
  (`stack_stats(blocks=N, block_series=True)` / `umbra stack --block-series` /
  the `block_series` argument on the `stack_stats` tool across MCP, LangChain and
  LlamaIndex / `"block_series": true` on `POST /artifacts/stats`). Exactly the
  payload decision named here: each block now carries a `series` array of every
  consecutive pass-to-pass record, oldest first, in the same shape as
  `peak_interval` — which is what tells a steady drift apart from a single step,
  a distinction one peak cannot carry. The loop that found the peak collects
  instead of comparing and `peak_interval` became a `max()` over what it
  collected, so the peak is visibly a member of the sequence and the cost is
  response size alone. Opt-in, and it needs `blocks`: a series with no grid to
  hang on is a hard error raised before any of the work (a `400` on the server),
  not a silently dropped flag. An unobserved block reports an empty series,
  matching its `None` net change, and the default is off so every existing
  payload is byte-identical. Offline-tested in `tests/test_load.py`,
  `tests/test_serve.py` and `tests/test_mcp_server.py`. ~~Follow-on, not a blocker:
  nothing *renders* the series yet — the `umbra demo` Quantify readout is the
  natural first client (see the sparkline follow-on below, which the per-block
  series now also feeds).~~ ✅ **Done** — the Quantify readout sparklines both
  series (see the entry below), so `block_series` has its first client.
- ~~**The same reduction on `umbra serve`.**~~ ✅ **Done** (`POST
  /artifacts/stats`). The STAC API façade served four artifacts and every one was
  a picture; this one answers the same change question in JSON. It reuses the
  composite endpoints' whole shape — `ids` or a `bbox`/`datetime` query, the
  content-addressed disk cache, the `"async": true` job flow — with its own
  option normaliser (`stats_options`: `extent` / `crs` / `clip_bbox` / `blocks` /
  `change_threshold_db`, defaulting to a UTM grid and the decibel scale like the
  agent tool rather than to the compositors' defaults) and its own frame picker
  (`stats_frames`), which additionally refuses a mixed-polarization selection
  because the HH/VV difference would land on the time axis and read as change.
  `Renderers` gained a `stats` member, so the route is offline-testable with no
  `load` extra installed. Offline-tested in `tests/test_serve.py`. Both
  follow-ons are now closed:
  - ~~**A "Quantify" button in the `umbra demo` analyze panel.**~~ ✅ **Done.**
    The explorer's analyze panel offered Change / Timescan / Swipe — three
    pictures — so a visitor could see that a site changed but not say by how
    much. **Quantify** is the numeric fourth: it POSTs the same filtered
    acquisitions to `POST /artifacts/stats` and reads the reduction out in the
    sidebar — the mean dB change first→last with its direction, the fraction of
    the site past the change threshold and that area in km², the block that
    moved most and the interval it moved in, and the north-up `grid_text`
    heat-grid. The request always asks for `blocks: 3`, since a scene-wide mean
    dilutes a change that moved one corner. The panel *formats* the server's
    numbers and computes none of its own (so the page and `umbra stack --stats`
    cannot disagree), carries the document's CC-BY attribution and calibration
    caveat into the browser, and is the panel both explorers share, so the
    embedded-slice and PMTiles pages gained the button together. Offline-tested
    in `tests/test_demo.py`. ~~Follow-on, not a blocker: the readout prints the
    net first→last record and the peak block, but the per-pass series
    (`doc.passes`) is fetched and unused — a sparkline of pass-to-pass change
    would use it, and is a presentation decision, not new data. The endpoint now
    also takes `"block_series": true`, so the same sparkline could be drawn *per
    block* (the peak block's own history) rather than only scene-wide.~~ ✅
    **Done** — both sparklines ship, and they are the first client of
    `stack_stats(block_series=True)`. The readout led with two numbers that
    cannot tell two different histories apart: a corner drifting a decibel every
    pass and one that jumped twelve once and held come back as the same
    `net_change` plus `peak_interval`. It now draws the *sequence* underneath
    each — one signed, zero-baselined SVG bar per consecutive pass-to-pass step,
    scaled to the largest step in that series and captioned with it (a bar chart
    with no stated scale is decoration) — for the site as a whole (from each
    pass's `change_vs_previous`, the field that was fetched and unused) and for
    the block the server named as the peak (from its `series`, which is why the
    request now sends `block_series: true` beside `blocks: 3`). Built as DOM
    elements via `createElementNS` with a per-bar `<title>` tooltip — never
    `innerHTML`, so a remote string still cannot parse as markup — and an
    `aria-label` naming the largest step; the bar colours are the two the
    readout's `.brighter` / `.dimmer` prose already uses, so the picture and the
    sentence cannot disagree. Both explorers gained it together (it lives in the
    shared analyze panel), the panel still formats and never computes (the only
    arithmetic is the pixel scale), and a series with nothing to compare draws
    nothing rather than an empty frame. The one cost is response size:
    `blocks: 3` with `block_series` is 9 blocks × (passes − 1) steps, of which
    the page plots one block's. Offline-tested in `tests/test_demo.py`; the
    generated JS was additionally exercised outside pytest against a synthetic
    stats document (a five-pass series with a single-step corner, and the
    degenerate no-comparable-pair case).
  - ~~**A client mistake that reads as a server error.**~~ ✅ **Done.** A
    render's `ValueError` — footprints that don't all overlap under
    `extent="intersection"` being the common one, and the one the Quantify
    button makes easy to hit from a filtered view — now maps to a `400` carrying
    the explanation instead of a `500`. Fixed in the shared place named here, so
    it holds for every artifact route (`_serve_artifact`) and on the async path
    too (`_run_job`, a `failed` job whose result endpoint answers `400`).
    Offline-tested in `tests/test_serve.py`.

---

## C5 archive-embedding follow-ons (`umbra embed` shipped)

- **Surfaced in:** the `umbra embed` PR (`AI_INTEGRATION_IDEAS.md` C5 /
  `STRATEGY.md` 5.2).
- **Code:** `src/umbra_py/embed.py`, `umbra embed` in `cli.py`.

`umbra embed` (visual similarity search — one image vector per acquisition in a
sidecar `catalog.embed.db`, `search_similar(item)` and text-to-scene, `[ai]` +
`[viz]` extras) is shipped. Follow-ons that build on it, not blockers:

- ~~**Publish the embedding table with the nightly index.**~~ ✅ **Consume side +
  publish plumbing done** (`umbra embed fetch` / `fetch_prebuilt_embeddings` /
  `SceneEmbeddingIndex.from_release`, the embedding sibling of `umbra index fetch`
  / `umbra tiles --fetch`). A fresh install now pulls a published
  `catalog.embed.db` from the rolling `catalog-index` release straight to the
  sibling of the catalog index and queries it with **no rebuild** — only the query
  still needs an embedding key. The weekly `publish-index.yml` gained an **opt-in,
  non-blocking** step that builds and uploads `catalog.embed.db` (recording the
  embedding model prominently in the release notes) — gated on a maintainer-set
  `OPENAI_API_KEY` secret and `continue-on-error`, so it never affects the
  deterministic index publish and costs nothing until a key is configured (the
  same "plumbing shipped, publish is a maintainer action" shape as the PyPI
  release). Constants `CATALOG_EMBED_ASSET` / `CATALOG_INDEX_EMBED_URL`; the fetch
  path is fully offline-tested (`tests/test_embed.py`, mocked release download +
  round-tripped DB, model label preserved). Remaining maintainer action: set the
  secret to actually publish the first table. (A stac-geoparquet embedding-table
  form is still an option if a non-umbra-py consumer wants it.)
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

- **Surfaced in:** the `viz` package-split PR (`CODEBASE_ANALYSIS.md` P3 #19 /
  `STRATEGY.md` §8 structural debt).
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
- ~~**`cli.py` is now the outlier at 5 282 lines.**~~ ✅ **Done** — see the
  `cli/` package-split entry below. It was split the same way, along the seams
  the commands already had.

---

## `cli/` package-split follow-ons (`cli.py` → `cli/` shipped)

- **Surfaced in:** the `cli` package-split PR (`CODEBASE_ANALYSIS.md` P3 #18/#19
  / `STRATEGY.md` §8 structural debt), which the `viz/` entry above named.
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

## Shared geography option-group follow-ons (`--intersects` everywhere shipped)

- **Surfaced in:** the shared geography-option PR (`CODEBASE_ANALYSIS.md` P3 #18
  / `STRATEGY.md` §8 structural debt).
- **Code:** `src/umbra_py/cli.py` (`_geometry_option`, `_place_option`,
  `_resolve_geography`), `src/umbra_py/watch.py` (`watch_key`),
  `src/umbra_py/context.py` (`_SEARCH_PARAMETERS`).

`--bbox` / `--place` / `--intersects` are one shared group applied to all
fourteen gather commands, resolved by one helper that also enforces the
polygon-vs-rectangle exclusion. This closed the geography half of P3 #18's
"common Click option groups" extraction and gave every front door the polygon
filter. What is still open:

- ~~**`umbra map` has no `--area` (or `--fuzzy`).**~~ ✅ **Done**, together with
  the task-name half of P3 #18 (`cli._area_option`, `tests/conftest.py`'s
  `GATHER_COMMANDS`, `tests/test_cli_option_groups.py`). `umbra map` takes
  `--area` / `--fuzzy` threaded into its `_gather_items` call, so the verb whose
  job is showing where the archive has imagery no longer needs a bbox looked up
  first; `umbra index build` / `update` gained the matching `--fuzzy` beside the
  `--area` they already had. But the fix this entry described would have left the
  cause in place, so it was closed the way the geography half was: `--area` is one
  shared `_area_option` (generic help, bespoke wording kept per command exactly as
  `_place_option` documents), and the gather-command roster moved to
  `tests/conftest.py` so **both** shared groups are checked against the same list —
  every command on it must expose the group *and* forward it to the backend. A new
  gather command that forgets either now fails a test instead of shipping a front
  door with fewer filters than its siblings, which is the failure that produced
  this entry.
- **The rest of P3 #18: the date and limit options.** `--start` / `--end` /
  `--limit` / `--max-search` are still written out per command, and the decision
  this entry used to leave open is now made: **don't extract them.** The
  task-name and geography groups were worth sharing because their *semantics* are
  identical everywhere and only the wording varied — an override mechanism
  (or the "keep bespoke help inline" convention) buys real drift-prevention. The
  date and limit options are not that: `--limit`'s default is command-specific
  (20 / 24 / 100 / 500 / 2000) as well as its help text ("Max results to plot" vs
  "Max tiles" vs "Max acquisitions to load"), so a shared decorator would have to
  parameterize both and would leave one line per command anyway — indirection with
  no invariant behind it. Revisit only if a command ships with a *missing*
  `--start`/`--limit` (the parity suite would be the place to catch it), which is
  the evidence that would change the call. The *gathering* half (`_gather_items` /
  `_search_source`) is already shared.
- ~~**`umbra ask` cannot plan a polygon.**~~ ✅ **Done** (`umbra ask --aoi` /
  `planner.AreaOfInterest` / `ask(aois=…)` / `parse_plan(aois=…)`). The decision
  this entry left open — whether a model should emit polygon coordinates at all —
  resolved the way it hinted: it should not, so it cannot. The caller supplies the
  areas it already has (`--aoi delta.geojson`, repeatable, `NAME=PATH` to name one,
  otherwise the file stem); each is parsed by `_resolve_intersects` *before* the
  model is involved, the prompt lists them by name/part-count/bounds and gains a
  single `aoi` key, and `parse_plan` resolves that name against the closed set at
  the determinism boundary. An unknown name is a self-describing `AskError` listing
  the valid ones; a name when no areas were supplied is an error rather than a
  quietly unfiltered whole-world search; `aoi` beside `place`/`bbox` is refused
  like every other pair of spatial filters. The choice flows through
  `SearchPlan.to_search_kwargs()` as `intersects` (the same rings, the same
  `UmbraItem.intersects_polygon` test every surface shares) and renders as `umbra
  search --intersects delta.geojson`, pointing back at the user's file rather than
  inlining the ring dump; `--json` reports `{"name", "source", "bbox"}`. With no
  `--aoi` the prompt is byte-identical to before. Offline-tested in
  `tests/test_planner.py`. Follow-on, not a blocker: the *other* model-planned
  surface, the MCP `search_catalog` tool, takes `bbox` only — the same
  supply-then-select shape would fit it (an operator-configured AOI directory
  rather than a CLI flag), but an MCP client has no equivalent of `--aoi` to pass
  files through, so it needs a server-side convention first.

---

## Done

- **Branch-coverage gate + Codecov badge in CI (`CODEBASE_ANALYSIS.md` P2 #16 /
  `STRATEGY.md` §8 structural debt).** The 991-test offline suite already covered
  the package thoroughly, but nothing enforced or surfaced the number. Wired
  `pytest --cov` into the `test-all-extras` CI job (the only job with every extra
  installed, so the visual / serve / convert / agent modules actually run rather
  than import-skip): it now runs under `coverage` in branch mode behind a
  `--cov-fail-under=88` floor (a couple of points under the current ~90 % so
  normal per-Python-release fluctuation doesn't red an unrelated PR), and uploads
  `coverage.xml` to Codecov. Coverage config lives in `[tool.coverage.run]` /
  `[tool.coverage.report]` in `pyproject.toml` (source `umbra_py`, branch mode,
  interactive-only / `TYPE_CHECKING` branches excluded); `pytest-cov` was already
  a `[dev]` dep. The Codecov upload is strictly non-blocking (`fail_ci_if_error:
  false` + a `codecov.yml` pinning status to `informational` with no PR
  comments), so an unconfigured/flaky Codecov can never block a merge — the
  enforcing gate is the local `--cov-fail-under`. README gained CI + Codecov
  badges. No runtime dependency, deterministic, offline. Follow-ons, none a
  blocker: raise the floor toward the real figure once it's stable across a few
  runs; add a `codecov.yml` `patch` target once the project is registered on
  Codecov so new code carries its own coverage; optionally enable the Codecov PR
  comment if maintainers want per-PR deltas surfaced inline.
- **One-command Docker self-hosting of `umbra serve` + a `/healthz` probe
  (`STRATEGY.md` §8 demo/hosting, `DEMO_APP_GAPS.md` G7).** The read-only STAC
  API could be run locally (`pip install 'umbra-py[serve]'; umbra serve`) but had
  no packaging story, so standing it up on a host meant a Python install and a
  hand-rolled `umbra index fetch` + process manager. Shipped a `Dockerfile`,
  `docker-compose.yml`, `.dockerignore` and `docker-entrypoint.sh` so
  `docker compose up` fetches the published index snapshot on first boot (into a
  `/data` volume, no S3 crawl) and serves the STAC API — the image runs
  unprivileged, persists index + render cache across restarts, doubles as the CLI
  (`docker run --rm umbra-py search …`), and is tuned by env vars
  (`UMBRA_SERVE_LIVE`, `UMBRA_FETCH_INDEX`, `UMBRA_INDEX_URL`, `UMBRA_SERVE_ARGS`,
  a `UMBRA_EXTRAS=serve,viz` build arg for the render endpoints). Added a
  purpose-built **`GET /healthz`** liveness/readiness endpoint to `build_app`
  (pure builder `serve.health_document`): `200` once the server is up, with a
  `ready` flag distinguishing a still-fetching first boot — wired to the image's
  `HEALTHCHECK` and fit for a Kubernetes probe. A new `docker.yml` CI job builds
  the image and smoke-tests it end to end (CLI passthrough, `docker compose
  config`, and a live-mode server answering `/` + `/healthz` with no external
  network); `/healthz` is offline-tested in `tests/test_serve.py`. Documented in
  a new `docs_src/deploy.md` and a README "Self-host it with Docker" section.
  This closed the Docker half of the G7 packaging/hosting gap. **Still open under
  G7:** a GitHub Pages deploy of the static `umbra demo` / `catalog.pmtiles`
  showcase (the docs site already deploys to Pages; the showcase is the remaining
  piece).
- **SAR acquisition-property filters on the `umbra serve` STAC Query extension.**
  The read-only STAC API previously exposed only `product_types` / `area` /
  `fuzzy` over `/search` and `/collections/{id}/items`, even though the
  `CatalogIndex`/`UmbraCatalog` `search` (and every other surface — CLI, MCP,
  render commands) also filter by polarization, incidence and resolution. Wired
  those SAR properties through the API three ways: GET params (`polarizations`,
  `min_incidence`, `max_incidence`, `max_resolution`), plain top-level POST body
  fields, and a proper STAC **Query extension** object with the namespaced
  property names — `sar:polarizations` (`in`/`eq`, bare list/string),
  `view:incidence_angle` (a `gte`/`lte` range, either or both bounds) and
  `sar:resolution` (`lte`, or a bare-number shorthand). `parse_query` now returns
  a `QueryFilters` NamedTuple and grew a numeric range parser (`view:incidence_angle`
  is the first property to take two operators in one object) alongside the
  existing scalar-operator path; new pure helpers `parse_polarizations` /
  `_as_float` / `_opt_float` do the coercion, and an unsupported operator or a
  non-numeric value is a hard `400` so a client's filter is never silently
  dropped. The filters push down to the same `UmbraItem.matches_filters`
  predicate the whole codebase shares (no new filtering logic), and GET
  pagination carries them into the `next` link. Fully offline-tested through the
  in-process `TestClient` (parser mapping + operator/value rejection; endpoint
  filtering by polarization / incidence range / max resolution across GET, POST
  top-level, POST query object, top-level-overrides-query, and pagination
  survival). Was the "expose the filters on the `umbra serve` STAC Query
  extension" acquisition-filter follow-on and the `umbra serve` open item — the
  last surface that couldn't filter on the SAR properties, so "every surface
  agrees" now holds.
- **Download content-integrity verification against the S3 ETag MD5
  (`docs/CODEBASE_ANALYSIS.md` P1 #5 / §3.2).** `download_url` already verified
  the received byte count against `Content-Length` and used `If-Range` + a stored
  ETag so a resume can't splice two objects; this closes the remaining §3.2 item —
  *content* verification. When the server exposes a single-part S3 `ETag` (the
  object's hex MD5) and `verify=True` (the default), the finished file is streamed
  through MD5 and compared, so on-the-wire corruption a correct length can't catch
  fails loudly with a `Checksum mismatch` `DownloadError`. A mismatch means the
  complete-length bytes are wrong (a resume can't repair them), so the `.part` and
  its `.etag` validator are discarded and a retry re-downloads cleanly rather than
  "resuming" a full-but-corrupt file. Multipart ETags (`"<hash>-<n>"`) are not a
  plain MD5 of the bytes, so `_single_part_md5` skips them rather than raising a
  spurious mismatch; `verify=False` opts out for callers that don't want the extra
  read of a multi-GB file (it threads through `download_asset` / `download_item`
  via `**kwargs`). New helpers `_single_part_md5` (quote/weak-prefix/case
  normalization, `-<n>` rejection) and `_file_md5` (streamed, memory-bounded);
  fully offline-tested in `tests/test_download.py` (matching MD5 passes,
  corrupt-body mismatch discards the `.part`, multipart-ETag skip, `verify=False`
  opt-out, and a resumed append verifying the *whole* object's MD5). No new
  dependency (stdlib `hashlib`), no model call.
- **Publish + fetch the whole-catalog `catalog.pmtiles` basemap (`umbra tiles
  --fetch`).** The weekly `publish-index.yml` workflow now tiles the freshly
  built index (`umbra tiles --local`, no second crawl) into a single-file
  `catalog.pmtiles` and writes a `catalog.html` MapLibre viewer pointed at the
  published archive's stable release URL, uploading both to the rolling
  `catalog-index` release beside `catalog.db` / `umbra-open-data.parquet`. The
  consume side mirrors `CatalogIndex.from_release`: `pmtiles.fetch_prebuilt_pmtiles`
  (resume-safe `download_url` of the release asset, default
  `pmtiles.default_pmtiles_path` = `catalog.pmtiles` beside the cached
  `catalog.db`, honouring `$UMBRA_PMTILES`) and a new `umbra tiles --fetch`
  mode (`--out` optional, `--url` override, `--viewer` writes a local viewer)
  give a fresh install a fast, zoom-anywhere whole-archive map with no crawl and
  no index — the visual sibling of `umbra index fetch`, and the published
  artifact worth offering upstream (`STRATEGY.md` 5.2, `DEMO_APP_GAPS.md` Path A
  step 3). Stdlib-only and fully offline-tested (mocked release download +
  round-tripped archive). This closed the "Publish `catalog.pmtiles` with the
  nightly index" PMTiles follow-on above.
- **Read-through catalog search — `CatalogIndex.search_live` / `umbra search
  --local --live` (`docs/CODEBASE_ANALYSIS.md` §4.4 / P3 #21).** The transparent
  middle between the instant-but-stale local index and the always-current live
  walk, the "make the index the default path" gap. `search_live` answers the
  whole query from the local index *and* walks only acquisitions at or after the
  index's freshness horizon (its newest `acq_date` minus `overlap_days`), merges
  the two streams (`heapq.merge` on the `(task, acq_date, href)` key) and
  de-duplicates by sidecar href, so an acquisition the index already holds is
  never yielded twice and the result is what a single fresh search would return.
  With `refresh=True` (the default) each genuinely new acquisition the delta
  discovers is upserted as it is yielded — the read-through cache warms, so the
  next call walks even less — committing (and re-stamping `built_at`) only when a
  row was actually added; a read-only index catches the `OperationalError` and
  disables the write-back rather than failing the search. `umbra search --local
  --live` exposes it (and `--live` without `--local` is a clean error). It reuses
  the same recent-only sidecar pruning `CatalogIndex.update` relies on and is
  delivered as an explicit method + flag rather than an implicit mode change to
  `search`, so a plain `search` is unchanged. Fully offline-tested in
  `tests/test_index.py` (horizon derivation, merge/dedup, cache warming,
  `refresh=False`, start-bound interaction, empty-index seed, and the two CLI
  paths) with an injected catalog. Was `docs/CODEBASE_ANALYSIS.md` §4.4's last
  open item and P3 #21.
- **Keyed single-item lookup on the catalog index (`umbra serve` follow-on).**
  `/collections/{id}/items/{item_id}` previously resolved a single item by
  filtering an id-scoped `run_search` in the serve layer — a scan of the ordered
  result set. Added `CatalogIndex.get(item_id) -> UmbraItem | None`, an
  `idx_items_id`-backed point lookup (the retrieval complement to `search`'s
  listing), and a `serve.get_one(source, item_id)` helper that uses it when the
  backend is a `CatalogIndex` and falls back to the id-filtered search for the
  live `UmbraCatalog`, which only lists. The new index is additive — added to
  `_SCHEMA` with `CREATE INDEX IF NOT EXISTS`, so existing databases (including a
  fetched snapshot) gain it on the next open with no `PRAGMA user_version` bump,
  exactly the additive path the schema-version marker was landed to enable.
  Fully offline-tested (`tests/test_index.py`, `tests/test_serve.py`): found /
  missing / index-present, plus the keyed-vs-listing dispatch in `get_one`.
  Was `docs/CODEBASE_ANALYSIS.md` §4.5 and this file's `umbra serve` open item.
  The Canopy-archive `get_item(id)` (a keyed fetch against the commercial STAC
  API) has since shipped too — see the Canopy section — so the retrieval interface
  now has a keyed lookup on both the local index and the commercial archive.
- **Structured `--json` success output on the remaining commands (A1 follow-on).**
  The A1 error contract already shipped (structured stderr errors with `hint`,
  `docs/schemas/error.schema.json`); this completes the success side so every
  command that produces a result has a machine-readable stdout shape. `umbra
  download --json` emits a `[{asset, path, bytes, sha256}, …]` array (hashing each
  written file with a streaming SHA-256), `umbra index info --json` prints the
  `CatalogIndex.stats()` summary plus `path`/`size_bytes`, and the five render
  commands (`change`, `timescan`, `swipe`, `gallery`, `map`) print a `{output,
  items_used, parameters}` manifest — with an optional `sidecars` map for the
  auxiliary files a command writes (e.g. `umbra change --narrate`'s narration
  JSON). Human progress/warnings and the `--place` "Resolved …" status line were
  moved to (or kept on) stderr so stdout carries the JSON object alone. Three
  schemas published under `docs/schemas/` (`download`, `index-info`,
  `render-manifest`) and documented in `docs/schemas/README.md`, under the same
  compatibility rules as `__all__`. Fully offline-tested in `tests/test_cli_json.py`
  with injected renderers/downloads (no network, no `viz` extra). Was
  `AI_INTEGRATION_IDEAS.md` §A1's last open item.
- **STAC Query extension on `umbra serve` — expose the index's `product_types` /
  `area` / `fuzzy` filters over `/search`.** The read-only STAC API previously
  answered only the STAC *core* filters (bbox, datetime, ids), even though the
  `CatalogIndex` it wraps also filters by product type and free-text task/site
  `area` (with an optional token-wise `fuzzy` widen). Wired those two
  Umbra-specific filters through the API: `run_search` and `_do_search` now
  thread `product_types` / `area` / `fuzzy` down to the backend's `search`
  (which both `CatalogIndex` and the live `UmbraCatalog` already accept, so the
  same query works against either), and the endpoints accept them three ways —
  GET query params on `/search` and `/collections/{id}/items`
  (`?product_types=GEC,SICD&area=Beet+Piler&fuzzy=true`), plain top-level POST
  body fields, and a proper STAC **Query extension** object
  (`{"query": {"product_types": {"in": ["GEC"]}, "area": {"like": "Beet"}}}`,
  with bare-value shorthands). Two new pure parsers do the work offline —
  `parse_product_types` (comma/list → canonical `PRODUCT_ASSETS`, an unknown
  type is a `400`, not a silent empty result) and `parse_query` (maps the Query
  object onto the two fields; an unsupported property or operator is a hard
  `400` so a client's filter is never silently dropped). The
  `item-search#query` conformance class is now advertised, and GET pagination
  carries the filters into the `next` link. Fully offline-testable through the
  existing in-process `TestClient` harness (no network, no `viz` extra). Was
  `AI_INTEGRATION_IDEAS.md` B2 / `DEMO_APP_GAPS.md` Path B's "query extensions"
  follow-on and this file's `umbra serve` open item.
- **MCP `find_similar` / `find_similar_text` tools — visual similarity search over
  the flagship server (C5 follow-on).** Surfaced the shipped `umbra embed`
  capability (`SceneEmbeddingIndex.similar_to_item` / `similar_to_text`) as two
  tools on `umbra-mcp` (`src/umbra_py/mcp_server.py`), plus a `find-similar-scenes`
  prompt. `find_similar(url)` renders + embeds the query item's quicklook and ranks
  the pre-embedded archive by cosine similarity (image-to-image, query excluded from
  its own results); `find_similar_text(query)` ranks the stored image vectors against
  a text query (text-to-scene, joint CLIP-family model). Both reuse the existing
  `SceneEmbeddingIndex` unchanged, gate on a prebuilt sidecar `catalog.embed.db`
  (a self-describing `FileNotFoundError` pointing at `umbra embed build` when
  absent) and the `[ai]` embedding key, and return `SceneMatch` records as compact
  cards carrying each acquisition's STAC `href` so a match hands straight to
  `get_item` / `quicklook` / `change_composite`. It holds the server's determinism
  boundary (`AI_INTEGRATION_IDEAS.md` §A4/§6.1): the only model call is turning the
  query image/text into a vector (the injectable `default_image_embedder` /
  `default_text_embedder`), while rendering, storage and ranking are deterministic
  — so the whole path is offline-tested with a stand-in embedder and renderer, no
  `[viz]`/network. Named in `AI_INTEGRATION_IDEAS.md` §C5 and this file's C5
  follow-ons.
- **Async job semantics for long `umbra serve` renders (`202 Accepted` + poll).**
  Added a small in-memory job queue to `src/umbra_py/serve.py` so a composite
  render need not hold a request for its whole duration. A `POST /artifacts/change`
  / `timescan` / `swipe` request that carries `"async": true` gets a `202 Accepted`
  and a job document back immediately; the render runs on a background pool
  (`ARTIFACT_JOB_WORKERS`, injectable via `build_app(..., job_executor=...)`).
  `GET /jobs/{id}` polls status (`queued` → `running` → `succeeded` | `failed`)
  and `GET /jobs/{id}/result` serves the finished artifact — from the *same*
  content-addressed disk cache the synchronous path writes, so there is no
  separate result store and an async request whose key is already cached returns
  an already-`succeeded` job with no work. Frame resolution/validation stays
  synchronous, so a bad request (too few acquisitions, malformed bbox) is still a
  fast `400`, never a doomed job; a failed render becomes a `failed` job whose
  result endpoint mirrors the sync path's status (`501` for a missing `viz`
  extra, `500` otherwise). The default synchronous behavior is unchanged when
  `"async"` is absent. New pure builder `job_to_dict` and the injectable executor
  keep it offline-testable without wall-clock timing. This was
  `DEMO_APP_GAPS.md` Path B step 2's remaining item.
- **`POST /artifacts/swipe` + the demo front end that calls the render
  endpoints (closes the self-serve R4 loop).** Added the fourth artifact
  endpoint to `src/umbra_py/serve.py`: `POST /artifacts/swipe` wraps
  `viz.swipe_map` (before/after co-registered passes) and returns a
  self-contained **HTML** page — so `_serve_artifact` grew a `media_type`/
  `suffix` so a swipe caches to its own `.html` entry, distinct from the PNG
  composites, and `Renderers` grew a `swipe` field (injectable, offline-tested
  like the rest). `swipe_frames` collapses a many-frame query to its temporal
  endpoints (first/last). `umbra serve` now also sets a permissive read-only
  CORS policy so a browser page on another origin can call it. The front end:
  `build_demo(..., server_url=...)` / `umbra demo --server-url` adds an "Analyze
  this view" sidebar panel whose Change / Timescan / Swipe buttons POST the
  currently-filtered acquisitions (chronological, sampled to a bounded cap) to
  the matching endpoint and render the returned artifact in place (swipe opens
  its interactive map in a new tab). With no `server_url` the page stays a fully
  static single file, unchanged. This was `DEMO_APP_GAPS.md` R4 / Path B step 3
  — the last self-serve-demo gap.
- **`umbra embed`: archive scene embeddings / visual similarity search (C5).**
  Added `src/umbra_py/embed.py` (`[ai]` + `[viz]` extras). `umbra embed build`
  renders each acquisition's quicklook once (reusing `umbra describe`'s injectable
  renderer — only downsampled overviews stream over HTTP) and embeds it into a
  vector stored in a schema-versioned sidecar `catalog.embed.db` beside the catalog
  index, keyed by item id and idempotent (a rebuild only embeds what is new; a
  scene whose asset won't render is skipped, not fatal). `umbra embed similar
  <url>` renders + embeds the query item and returns the archived scenes that look
  most like it (image-to-image, the query excluded from its own results); `umbra
  embed search "…"` ranks the stored image vectors against a text query
  (text-to-scene, with a joint CLIP-family model); `umbra embed info` reports the
  count, model and dimension. The only model calls are turning an image or a text
  query into a vector — both injectable (`ImageEmbedder` / text `Embedder`, default
  an OpenAI-compatible multimodal `/embeddings` endpoint via `requests`,
  user-supplied key) — while rendering, storage, `cosine_similarity` (reused from
  `umbra_py.semantic`) ranking and thresholding are stdlib-only (no `numpy`, no
  `sqlite-vec`), so the whole feature is offline-testable with a deterministic
  stand-in embedder and renderer. Chose a sidecar `catalog.embed.db` over embedding
  vectors *inside* `catalog.db` so the deterministic index and its published
  snapshot never carry model-derived data a core install can't use — the same
  boundary `umbra semantic` uses. A `SceneMatch` is a pointer back to a real
  acquisition (id, task, datetime, STAC href), never a model-authored fact.
- **`umbra chips`: ML dataset preparation (C4).** Added `src/umbra_py/chips.py`
  (`[load]` extra). `chip_item` walks an acquisition's geocoded GeoTIFF one window
  at a time via GDAL's `/vsicurl/` driver (only each tile's bytes stream over HTTP
  range requests — no full download, memory bounded to one chip) and writes full
  `chip_size` × `chip_size` tiles as GeoTIFF or `.npy`; `write_chips` chips a whole
  search into a dataset + manifest (`.jsonl` — one `ChipRecord` per line — or a
  `.geojson` `FeatureCollection` of chip footprints). Every record carries the
  chip's geographic bbox, CRS, transform, grid position and source pixel window
  plus the acquisition's datetime, place, platform, polarization, incidence angle
  and resolution, stamped with the CC-BY attribution. Fixed size is a promise
  (partial edge tiles dropped), `stride` overlaps tiles, and `min_valid` drops
  mostly-nodata corners. No model is called — pure raster iteration + manifest
  logic, mirroring `umbra_py.load` — so it is fully offline-testable with a real
  on-disk GeoTIFF. The `umbra chips` CLI mirrors `umbra change`'s search-vs-URLs
  interface plus `--local`/`--index-db`.
- **`umbra describe`: VLM scene description (first C2 piece).** Added
  `src/umbra_py/describe.py` (`[ai]` + `[viz]` extras) and the
  `constants.AI_PROVENANCE` note. `umbra describe <item-url>` renders the item's
  quicklook, sends that PNG plus the `UmbraItem.to_llm_context()` card to a
  configured vision model (Anthropic or any OpenAI-compatible endpoint,
  user-supplied key, `requests` only), and returns a validated
  `SceneDescription` — `{summary, observed_features[], confidence, caveats[]}`.
  The model *only* interprets: the picture and metadata are produced
  deterministically, the reply passes the `parse_description` boundary, and every
  description is stamped with the CC-BY attribution and the AI-provenance note, so
  a reading of radar is never mistaken for a measurement. Like `planner.py`, the
  model call is an injectable `Describer` and the render an injectable
  `Renderer`, so the whole feature is offline-testable with no network and no
  model.
- **Semantic task-name aliasing (last open C1 piece).** Added
  `src/umbra_py/semantic.py` (`[ai]` extra): `SemanticTaskIndex` embeds the
  catalog index's distinct task names once (`umbra semantic build`) into a
  schema-versioned SQLite file beside `catalog.db`, and `umbra semantic search`
  ranks them against a query by cosine similarity, printing the `umbra search
  --area …` command for the best match to audit before `--run`. The only model
  call is the injectable `Embedder` (default: an OpenAI-compatible `/embeddings`
  endpoint via `requests`); storage, cosine and ranking are stdlib-only (no
  `numpy`, no `sqlite-vec`), so it is fully offline-testable with a stand-in
  embedder. Resolves `area="grain storage north dakota"` → "Beet Piler - ND",
  which plain string similarity can't and shouldn't fake. Chose a sidecar
  `catalog.semantic.db` over embedding vectors *inside* `catalog.db` so the
  deterministic index and its published snapshot never carry model-derived data a
  core install can't use.
- **Bootstrap local search from the published catalog snapshot.** Added
  `CatalogIndex.from_release()` / `umbra index fetch` (downloads the rolling
  `catalog-index` release's `catalog.db` via the resume-safe `download_url`),
  plus a `built_at` build stamp surfaced as a staleness note in
  `umbra index info`. Surfaced in
  [PR #26](https://github.com/reesehammer/umbra-py/pull/26).
