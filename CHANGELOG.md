# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Fuzzy task matching for `--area` search (`docs/AI_INTEGRATION_IDEAS.md` C1 —
  the second deterministic step of Phase 3).** `--area` (and the
  `UmbraCatalog.search` / `CatalogIndex.search` `area=` argument, and the MCP
  `search_catalog` tool) stays a literal case-insensitive substring by default;
  passing `--fuzzy` / `fuzzy=True` widens it to a token-wise match resolved by a
  new stdlib-only `umbra_py.fuzzy` module (`task_matches` / `matching_tasks`,
  exported at the top level). The fuzzy match is **word-order- and
  punctuation-independent and tolerant of a small typo** — so `"utah
  centerfield"`, `"centerfield utah"` and `"centrfield"` all still reach
  `"Centerfield, Utah"` — while requiring *every* query token to match, which
  keeps precision. It is a **strict superset** of the substring match (it never
  drops a result), and the live (`UmbraCatalog`) and indexed (`CatalogIndex`)
  search paths share the one matcher and are tested to agree. **No model is
  called at runtime**, so it stays inside the library's determinism boundary and
  is fully offline-testable. `--fuzzy` is available on `search` and on the
  area-taking render commands (`change`, `timescan`, `swipe`, `gallery`).
  Semantic aliasing (`"grain storage north dakota"` → `"Beet Piler - ND"`) is
  deliberately out of scope — it needs the future embedding index, not plain
  string similarity.
- **Natural-language date bounds for search (`docs/AI_INTEGRATION_IDEAS.md` C1 —
  the deterministic first step of Phase 3).** `--start` / `--end` (and the
  `UmbraCatalog.search` / `CatalogIndex.search` keyword arguments, and the MCP
  `search_catalog` tool) now accept human date expressions in addition to
  `YYYY-MM-DD`: a bare year or year-month (`2024`, `2024-03`), the keywords
  `today` / `yesterday` / `tomorrow`, a relative offset (`3 months ago`,
  `a week ago`), or a period (`this month`, `last year`). Resolution is a new
  stdlib-only `umbra_py.dates.parse_date_bound` (exported at the top level) that
  uses plain calendar arithmetic — **no model call at runtime**, so it stays
  inside the library's determinism boundary and is fully offline-testable. It is
  *bound-aware*: a span expression snaps to its first day as a `--start` and its
  last day as an `--end`, so `--start 2024 --end 2024` covers the whole year and
  `--end last month` includes the last day of that month. Because every command
  that takes a date range funnels through the single `_coerce_date` choke point,
  `search`, `index build`, `change`, `timescan`, `swipe`, `map` and `gallery`
  all gain this at once. Full ISO dates behave exactly as before.
- **`llms.txt` context bundle (`docs/AI_INTEGRATION_IDEAS.md` A2 — the last open
  Phase 2 item).** `umbra_py.llms_txt()` / `llms_full_txt()` (CLI: `umbra
  llms-txt [--full]`) render the [llms.txt-convention](https://llmstxt.org/)
  Markdown that a language model pulls in to learn how to *drive* the library —
  the *user* agent guide, complementing `AGENTS.md` (the contributor guide) and
  the machine-readable `umbra context` JSON. The concise `llms.txt` is the
  index; `llms-full.txt` is the self-contained bundle: the determinism boundary,
  the domain knowledge (reusing `llm_context()`), the full CLI command reference
  introspected from the live command tree, the AI-native interfaces, and each
  core module's explanatory docstring. It is assembled entirely from facts
  already in the package — module docstrings are read via `ast` rather than by
  importing the modules, so the generator is deterministic and stdlib-only and
  runs in the bare core install without pulling in a heavy extra. The committed
  repo-root `llms.txt` / `llms-full.txt` are that rendered output; a golden test
  keeps them from drifting (regenerate with `umbra llms-txt > llms.txt && umbra
  llms-txt --full > llms-full.txt`).
- **Local-index rendering for the visual commands (`docs/DEMO_APP_GAPS.md` G2 /
  Path A step 2).** `umbra map`, `gallery`, `swipe`, `change` and `timescan` now
  accept the same `--local` / `--index-db` options as `umbra search`, so they
  render from a prebuilt catalog index (`umbra index fetch` / `umbra index
  build`) instead of re-walking S3 on every invocation. Previously only `umbra
  search` could use the index; a fully built `catalog.db` did nothing for the
  visual commands, which each re-crawled the bucket live — the gap
  `DEMO_APP_GAPS.md` named as the next step to a fast, self-serve demo (R5). The
  search backend is chosen by the shared `_gather_items` helper (the same
  `CatalogIndex`-vs-live `UmbraCatalog` split `search` already used), so every
  filter behaves identically to the live path; only acquisitions already in the
  index are returned. The path flag is `--index-db` (not `--db`) because the
  render commands already use `--db` for the decibel stretch. Without `--local`
  the commands walk S3 live exactly as before.
- **`umbra serve`: a read-only STAC API façade over the catalog index
  (`docs/AI_INTEGRATION_IDEAS.md` B2 / `docs/DEMO_APP_GAPS.md` Path B step 1).**
  Umbra publishes a *static* STAC catalog and **no** search API, which is
  exactly what breaks the standard geospatial tooling — `pystac-client`, the
  QGIS STAC plugin, `stac-browser` and leafmap all speak the STAC API *search*
  protocol and have nothing to query. This serves that protocol over
  `CatalogIndex`, so pointing any STAC client at `http://localhost:8000` makes
  Umbra's open archive searchable like Sentinel-1 or Landsat. It is the
  browser-facing sibling of `umbra-mcp`: same index underneath, a different
  front door, and the shared foundation the demo application (`DEMO_APP_GAPS.md`
  Path B) wants. Run it with `umbra serve`; needs the new `[serve]` extra
  (`pip install "umbra-py[serve]"`).

  - **Endpoints:** the STAC API landing page (`/`), `/conformance`,
    `/collections`, `/collections/{id}`, `/collections/{id}/items`,
    `/collections/{id}/items/{item_id}`, and STAC item search over both
    `GET /search` and `POST /search` (bbox, datetime interval, ids, limit, and
    opaque-token pagination). FastAPI generates the OpenAPI document at
    `/openapi.json` and interactive docs at `/docs` for free — the schema'd REST
    surface OpenAPI-driven agents consume without custom glue.
  - **Index-first:** every query is a local SQL read against the prebuilt
    `catalog.db` (`umbra index fetch`), so the server answers in milliseconds
    rather than re-walking S3. `--live` opts into a per-request S3 walk (slow)
    for a quick try without an index; a missing index returns `503` with a hint.
  - **Deterministic, thin edge** (mirrors `umbra-mcp`): the STAC documents are
    built by plain, offline functions with no web-framework dependency (so they
    are unit-testable in the core install), and the CC-BY attribution travels in
    the landing page and collection metadata. A fresh backend is opened and
    closed per request, so the app is safe under FastAPI's thread pool.
- **`umbra-mcp`: a Model Context Protocol server over the library (the flagship
  AI-integration deliverable, `docs/AI_INTEGRATION_IDEAS.md` B1 / Phase 2).**
  Umbra publishes no STAC API, so this library *is* the query layer — and this
  server exposes it over MCP, turning any MCP client (Claude Desktop / Code and
  others) into a zero-install, natural-language front door to a 17+ TB public
  SAR archive. Run it with `umbra mcp`, `umbra-mcp`, or `uvx umbra-mcp` (stdio
  transport); needs the new `[mcp]` extra (`pip install "umbra-py[mcp]"`).

  - **Tools** (thin wrappers over the existing public API): `search_catalog`
    (returns compact `to_llm_context()` cards, not full STAC JSON, to protect
    the context window), `get_item`, `geocode_place`, `index_stats`,
    `quicklook`, `change_composite`, `timescan`, and `download_asset` (gated by
    a two-step size-confirmation handshake). The three imagery tools return the
    rendered PNG as an MCP **image content block**, so the model *sees* the
    radar scene.
  - **Resources:** `umbra://context` (the `llm_context()` document) and
    `umbra://index/stats`. **Prompts:** packaged `monitor-site` and
    `survey-region` workflows.
  - **Deterministic core, AI at the edges** (the `[ai]`/determinism policy in
    `AGENTS.md`): nothing here calls a model — the server searches, geocodes and
    renders; the client's model plans and narrates. `change_composite` refuses
    to mix polarizations (HH vs VV are not comparable), and the CC-BY
    attribution line travels with every result.
- **AI-legible surface (Tier A groundwork): context cards, an `llm_context()`
  document, and `__geo_interface__`.** The friction in using Umbra's open data
  is interpretive — knowing *what to ask for* — which is exactly what a language
  model answers well when it has the domain facts in context. This lands the
  zero-dependency, deterministic groundwork the flagship MCP server and every
  later AI phase consume (`docs/AI_INTEGRATION_IDEAS.md` Phase 1):

  - `UmbraItem.to_llm_context()` — a compact, explanation-rich context card:
    like `metadata_summary()` but every present product type carries a one-line
    explanation, the polarizations carry the change-detection caveat, and the
    CC-BY attribution line travels with the data. Surfaced on the CLI as
    `umbra info <url> --json`.
  - `umbra_py.llm_context()` / `umbra context` — the library's self-describing
    document (product-type table, search-parameter semantics, license rules) an
    agent pulls into context to drive umbra-py in one shot.
  - `UmbraItem.__geo_interface__` / `ItemCollection.__geo_interface__` — the
    Python geo-interface protocol, so geopandas / shapely / leafmap ingest a
    search with zero glue (`gpd.GeoDataFrame.from_features(results)`).

  All of it is deterministic and offline (no network, no model call); the
  determinism boundary is now written into `AGENTS.md`.
- **Fetch the prebuilt catalog index (`CatalogIndex.from_release`, `umbra index
  fetch`).** The weekly workflow already publishes a `catalog.db` snapshot on
  the rolling `catalog-index` release, but a fresh install still had to crawl
  the whole S3 bucket before `umbra search --local` returned anything. The new
  fetch step downloads that snapshot straight to the default index path via the
  existing resume-safe `download_url`, so whole-catalog local search works out
  of the box — no crawl:

  ```bash
  umbra index fetch                 # download the weekly snapshot (seconds)
  umbra search --local --area "Centerfield, Utah"   # instant, offline
  ```

  ```python
  from umbra_py import CatalogIndex

  with CatalogIndex.from_release() as index:   # download + open
      for item in index.search(area="centerfield"):
          print(item.summary())
  ```

  `umbra index build` now stamps the index with a `built_at` date, and
  `umbra index info` reports it with staleness (e.g. `built : 2026-07-14 (1
  day(s) ago)`) so a downloaded snapshot's age is visible. This is the consume
  side of the publish workflow shipped in PR #26 — the last prerequisite the
  strategy, demo, and AI-integration docs named before the demo / MCP / STAC-API
  layers.
- **stac-geoparquet catalog export (`export_geoparquet`, `umbra index
  export`).** A local `CatalogIndex` makes *your* searches fast, but everyone
  still pays for their own crawl of Umbra's bucket. The new export writes an
  index out as a single [stac-geoparquet](https://stac-geoparquet.org/) file —
  the entire catalog searchable in seconds with DuckDB, geopandas, pyarrow or
  rustac, no server, no crawl, no umbra-py needed on the consuming side. Each
  row is the full STAC item, with a `self` link injected back to its sidecar
  JSON so query results lead straight to the data files (items without a
  footprint geometry are skipped and counted):

  ```bash
  umbra index build                                  # walk S3 once
  umbra index export --out umbra-open-data.parquet   # ship the catalog
  ```

  ```python
  from umbra_py import CatalogIndex, export_geoparquet

  with CatalogIndex("umbra.db") as index:
      export_geoparquet(index.search(), "umbra-open-data.parquet")
  ```

  A new scheduled workflow (`.github/workflows/publish-index.yml`) rebuilds
  the full index weekly and publishes `umbra-open-data.parquet` + `catalog.db`
  on the rolling `catalog-index` GitHub release, so users can search the whole
  catalog without ever crawling it. New public `export_geoparquet`; new
  `export` extra (`stac-geoparquet`). Project strategy notes tracking this and
  related ideas live in `docs/STRATEGY.md`.
- **Interactive full-resolution viewer (`view`, `umbra view`).** Every other
  rendering surface collapses a scene to a fixed picture — `quicklook` writes
  one downsampled PNG — which throws away the resolution that makes Umbra
  special (a GEC scene is ~25 cm imagery). `view` starts a tiny local tile
  server and opens a Leaflet map in the browser; as you pan and zoom, only the
  tiles in view stream from the cloud-optimized GeoTIFF via HTTP range requests
  (at the COG overview matching your zoom) and are warped into the Web-Mercator
  map grid — native-resolution exploration with no full download:

  ```bash
  umbra view <item-json-url> --db        # Ctrl-C to stop
  ```

  ```python
  from umbra_py import view
  view(item, db=True)                    # opens the browser
  ```

  The contrast stretch is computed once over a whole-scene overview and shared
  by every tile, so neighbouring tiles don't seam; tiles are warped through
  GDAL into true Web Mercator, so the imagery lines up with the OpenStreetMap
  basemap (unlike the bbox-stretch quick-look approximation used by the
  browser-side lazy overlay). `make_viewer_server(item, ...)` returns the
  unstarted server for embedding. Requires the `viz` extra.
- **Local catalog index (`CatalogIndex`, `umbra index`).** Umbra has no STAC
  API, so every search re-walks the public S3 bucket — fine once, slow on
  repeat. The new `CatalogIndex` persists the items a walk discovers into a
  local SQLite database and answers searches from SQL, so a repeat (or
  overlapping) search is a near-instant local query instead of a fresh crawl:

  ```bash
  umbra index build --area "Centerfield" --start 2024-01-01 --end 2024-12-31
  umbra search --local --area "Centerfield" --product GEC
  umbra index info
  ```

  ```python
  from umbra_py import CatalogIndex

  with CatalogIndex("umbra.db") as index:
      index.build(area="centerfield")            # walk S3 once, persist
      list(index.search(area="centerfield"))     # local, no network
  ```

  Run `umbra index build` (or `CatalogIndex.build()`) with **no filters to
  index the whole catalog** — one long, one-time crawl that makes every later
  `--local` search instant — or pass the usual `--area`/`--bbox`/`--start`/
  `--end` to scope it to a slice. The CLI shows a live running tally while it
  walks (a `progress` callback on `build`).

  Each acquisition is one row keyed by its sidecar URL, carrying the columns
  the filters need (acquisition date, bounding box, task, product assets) plus
  the full STAC JSON so items rebuild without another network round trip.
  `CatalogIndex.search` mirrors `UmbraCatalog.search` (bbox / date / product /
  area / limit / max_per_task); `build` is an idempotent upsert, so an index
  refreshes and grows incrementally. It's a deliberate, reusable building block
  — the substrate for a shared, prebuilt catalog (walk once, ship the `.db`) or
  a service layered on this library. `umbra search` gains `--local` / `--db`
  to query an index instead of S3; the index path defaults to `$UMBRA_INDEX_DB`
  or `~/.cache/umbra-py/catalog.db`. New public `CatalogIndex` and
  `default_index_path`. No new dependencies (SQLite is stdlib).
- **Timescan composite (`umbra timescan`).** Collapse a site's *entire* time
  series into a single temporal-statistics image, rather than the 2–3 dates
  `umbra change` is limited to. Each pixel is summarised across all passes and
  mapped to color — **red = mean** backscatter, **green = peak**, **blue =
  temporal standard deviation (variability)**:

  ```bash
  umbra timescan --area "Centerfield" --start 2024-01-01 --end 2024-12-31 \
      --out timescan.png --db
  ```

  Stable terrain (no variability) renders gray/yellow; anything that came and
  went over the series — ships cycling through a berth, vehicles in a lot, a
  field flooding — has high variability and glows blue/cyan, turning a whole
  archive into one glanceable "where did activity happen" picture. Accepts 3+
  STAC item URLs directly or a search (`--area`/`--bbox`/`--place` +
  `--start`/`--end`, preferring a single polarization). `--place` geocodes a
  name to a bounding box like the other search commands. Reuses the
  change-detection
  co-registration; only downsampled overviews are streamed via range requests.
  New public `timescan_composite` / `save_timescan_composite` functions.
  Requires the `viz` extra.
- **Gallery groups acquisitions by task.** `umbra gallery` (and
  `gallery` / `save_gallery`) now lay the contact sheet out as labelled
  per-task sections, so repeat passes of one site sit next to each other under
  the task's name (e.g. "Centerfield, Utah") instead of being scattered through
  one flat grid. A single-task gallery stays a flat grid. The new
  `UmbraItem.task` property exposes the task label an item belongs to.
- **Search by place name (`--place`).** The `search`, `map`, and `gallery`
  commands now accept `--place` (and there's a public `geocode_place` function)
  so you can search a fuzzy geography instead of hand-typing a bounding box:

  ```bash
  umbra gallery --place California --out california.html
  umbra search --place "Tokyo" --start 2024-01-01 --end 2024-12-31
  ```

  The name is forward-geocoded to a bounding box via OpenStreetMap Nominatim
  (the inverse of the existing reverse-geocoder used for map popups), and the
  resolved place is echoed so you can confirm the match. The box is rectangular
  — searching `California` also catches footprints in the box's corners that
  fall just outside the state outline — matching the bbox-overlap semantics the
  rest of the search already uses. Mutually exclusive with `--bbox`. Raises the
  new `GeocodeError` when a name can't be resolved.
- **Interactive search gallery / contact sheet.** New `umbra gallery` CLI
  command and `gallery` / `save_gallery` functions take a search (area + dates,
  or a bbox / product filter) and render a grid of streamed SAR quicklook
  thumbnails into one self-contained HTML page — each tile linking to its STAC
  item with a footprint sketch:

  ```bash
  umbra gallery --area Centerfield --out gallery.html
  ```

  It's the missing "browse the catalog visually" primitive: only downsampled
  cloud-optimized GeoTIFF overviews are fetched (via HTTP range requests, in
  parallel) — never a full download — so you can *see* what a search returned
  before committing to multi-gigabyte SAR files. Thumbnails default to the
  radiometrically-correct decibel stretch; any item that can't be previewed
  falls back to its footprint sketch, so one bad acquisition never sinks the
  page. Each tile also carries a collapsible **URLs** panel with the asset's
  direct download URL (the GEC GeoTIFF, for `curl` / GDAL `/vsicurl`) and the
  STAC item URL (for `umbra info | download | quicklook | load`), each in a
  click-to-select box so you can copy a URL straight into another command.
  Built directly on the existing `quicklook` + lazy-overview reader. Requires
  the `viz` extra.
- **Rich notebook rendering for items and search results.** `UmbraItem` now
  has a Jupyter `_repr_html_`, so an item displayed in a notebook renders as a
  card — a metadata table next to an inline SVG sketch of its ground footprint
  (north up) — instead of a bare `repr`. The new `ItemCollection` (a drop-in
  `list` subclass, exported from the package root) renders a *list* of results
  as a wrapping gallery of those cards:

  ```python
  from umbra_py import UmbraCatalog, ItemCollection
  results = ItemCollection(UmbraCatalog().search(area="rome", limit=8))
  results  # -> gallery of metadata cards (offline, core install, no network)
  ```

  Both representations are pure-stdlib and offline by default — displaying an
  item never triggers a network read, so notebooks stay snappy and the feature
  works without any extras. Pass `ItemCollection(..., thumbnails=True)` to opt
  into streamed SAR quicklook thumbnails (decibel-stretched, only the overview
  bytes are fetched per the existing `quicklook` path; needs the `viz` extra).
  Thumbnails are fetched lazily on display, and any item that can't be
  previewed falls back to its footprint card, so a repr never raises. This is
  the lowest-friction way to *see* what a search returned without leaving the
  notebook.
- **Interactive before/after SAR swipe maps.** New `umbra swipe` CLI command
  and `swipe_map` / `save_swipe_map` functions render two passes of the same
  site into a single self-contained HTML map with a draggable divider: the
  *before* acquisition fills the left of the seam, *after* the right, and
  dragging the handle wipes one over the other across the same ground. SAR's
  backscatter is stable between passes, so anything that changed — a ship that
  docked, a field that flooded, a building that rose — snaps in and out as you
  sweep the seam. Where `change_composite` bakes the comparison into one
  colored still and `change_animation` flips between dates, this lets you
  *feel* the change interactively. Like `umbra change`, it works two ways: pass
  two STAC URLs in chronological order, or search a site by
  `--area`/`--bbox` + `--start`/`--end` and it compares the earliest and latest
  pass (preferring a single polarization). The two acquisitions are
  co-registered onto their shared footprint intersection (the same warp
  `change_composite` uses), so both sides cover identical ground at identical
  scale and line up across the seam; only the requested overview resolution of
  each cloud-optimized GeoTIFF is streamed, no full download. `--db` selects
  the radiometrically-correct decibel stretch. `image_overlay` gained a
  matching `db=` option. Requires the `viz` extra.
- **Analysis-ready loading into `xarray` (the "load" step).** New
  `to_xarray(item)` turns a geocoded Umbra GeoTIFF into a georeferenced
  `xarray.DataArray` — `y`/`x` coordinate axes in the raster's native CRS,
  CRS / affine transform / bounds / acquisition metadata in `.attrs`, and the
  CC BY 4.0 attribution carried along — so the data drops straight into the
  scientific Python stack (`xarray`/`dask`/`matplotlib`/`scikit-image`/
  `rioxarray`). This is the missing verb in the project's "discover, **load**,
  download, analyze" tagline: previously you had to hand-roll `rasterio`
  windowing and coordinate construction to get an array. `bbox=` reads only a
  geographic sub-window (reprojected to the raster's CRS first), `max_size=`
  decimates via the cloud-optimized GeoTIFF overviews, and `db=` returns the
  radiometric decibel scale. Because the source is a COG read through
  `/vsicurl/`, only the requested window/resolution is streamed over HTTP range
  requests — no multi-gigabyte download. New `load` extra
  (`pip install "umbra-py[load]"`, pulls in `xarray` + `rasterio` + `numpy`).
  A file-producing companion `to_geotiff(item, dest)` and an `umbra load
  <item-url> --out scene.tif` CLI command write the same clipped/decimated
  scene to a single-band float32 GeoTIFF (in the source CRS, nodata as `NaN`)
  for QGIS / GDAL users who want a file rather than an in-memory array; both
  honor `--bbox` / `--max-size` / `--db`.
- **Animated SAR time-lapses across a whole series.** Where a change
  composite collapses 2–3 dates into one colored image, `umbra change`
  now also produces an animated GIF over *any* number of acquisitions when
  `--out` ends in `.gif` —
  `umbra change --area "Centerfield" --start 2024-01-01 --end 2024-12-31
  --out lapse.gif --db`. Every matched acquisition becomes a frame, all
  co-registered onto the shared footprint intersection so the site stays put
  and only the scene evolves; each frame is a SAR quicklook stamped with its
  acquisition date. `--fps` sets playback speed and `--colormap` pseudo-colors
  the frames. Explicit-URL mode lifts its 2–3 cap for `.gif` output (pass as
  many as you like). New public `change_animation` / `save_change_animation`
  functions; `select_change_frames(..., frames=None)` returns the whole
  single-polarization series for this path. Requires the `viz` extra.
- **One-command change composites by site + time range.** `umbra change`
  gained a search mode: instead of passing 2–3 STAC URLs, give
  `--area "<site>"` (or `--bbox`) with `--start`/`--end` and it gathers the
  site's acquisitions and auto-selects the dates to composite —
  `umbra change --area "Centerfield" --start 2024-01-01 --end 2024-12-31
  --out change.png`. `--frames {2,3}` picks how many dates (default 2),
  spread evenly from earliest to latest across the matched range. Selection
  prefers a single polarization (the largest same-polarization group), since
  compositing HH against VV would render the polarization difference as fake
  "change"; if no same-polarization pair exists it falls back to comparing
  across polarizations and warns. The chosen acquisitions are printed before
  rendering. Exposed as a reusable `select_change_frames(items, frames=2)`
  helper in the public API. The explicit-URL form still works; the two modes
  are mutually exclusive.
- **Search by area name** via a new `area=` argument on
  `UmbraCatalog.search` and an `umbra search --area "<name>"` CLI flag.
  Umbra files every pass of a site under one named task directory (e.g.
  `sar-data/tasks/Centerfield, Utah/`), so `--area centerfield` returns
  just that site's acquisitions. The match is a case-insensitive substring
  on the task-directory name, applied *before* each directory is listed, so
  non-matching tasks are skipped entirely — making a name-scoped search much
  faster than an unfiltered walk. This is the ergonomic way to gather the
  co-located passes a change composite needs: `umbra search --area X` →
  pick 2–3 same-polarization URLs → `umbra change`.
- **Multi-temporal SAR change composites** via new `change_composite` /
  `save_change_composite` functions and an `umbra change <url> <url>
  [<url>] --out change.png` CLI command. Pass 2–3 acquisitions of the
  same site (e.g. items from one Umbra task) in chronological order; the
  bands are co-registered onto a shared lon/lat grid (each cloud-optimized
  GeoTIFF is read at a downsampled resolution via HTTP range requests and
  warped so the same output pixel is the same ground location on every
  date), percentile-stretched, and assigned to color channels. Unchanged
  ground stays gray while change is tinted by *when* it happened: for two
  dates, **green** = backscatter that appeared in the later pass, **magenta**
  = backscatter that vanished; for three dates, an earliest→latest red/green/
  blue temporal-RGB. Only the area imaged on every pass is colored (pixels
  missing from any acquisition are transparent), and `--db` switches to the
  radiometrically-correct decibel stretch. This is SAR's signature change-
  detection view with no manual co-registration. Requires the `viz` extra.
  The percentile/dB stretch shared with the quicklook path was factored into
  a `_normalize_band` helper.
- **Standalone SAR quicklooks** via new `quicklook` / `save_quicklook`
  functions and an `umbra quicklook <item-url> --out scene.png` CLI
  command. This is the lowest-friction way to *see* an Umbra
  acquisition: it streams a downsampled preview of the item's
  cloud-optimized GeoTIFF via HTTP range requests (no multi-gigabyte
  download, no Folium map, no GIS) and writes a plain image whose
  format follows the output extension. The raster is read in its
  native, already-geocoded projection — a faithful look at the pixels
  rather than a map-placeable warp. Two SAR-specific rendering options:
  `--db` switches to a decibel (log-amplitude) stretch — the
  radiometrically-correct view that reveals terrain texture and urban
  structure the default linear stretch crushes toward black — and
  `--colormap NAME` (e.g. `viridis`, `magma`) pseudo-colors the result
  through any matplotlib colormap. Tunables match the map overlays:
  `--asset` (default `GEC`), `--max-size` (default 2048), `--percentile`
  (default `2,98`). Requires the `viz` extra. The `_stretch_to_rgba`
  helper grew matching `db` / `colormap` parameters, and the rasterio
  read shared with `image_overlay` was factored into `_read_sar_band`.
- **Browser-side lazy SAR imagery** via a new `lazy_imagery=True` kwarg
  on `footprint_map` and `timeline_map`, plus a matching
  `umbra map --lazy-imagery` CLI flag. Each popup gets a "Get SAR
  image" button; on click, the page lazily loads
  [`geotiff.js`](https://geotiffjs.github.io/) (from a pinned CDN),
  streams a low-resolution overview of the GEC cloud-optimized GeoTIFF
  directly from the Umbra public bucket via HTTP range requests,
  applies the same percentile-and-transparent-invalid-pixels stretch
  Python's `_stretch_to_rgba` uses, and drops it on the map as a plain
  Leaflet `L.imageOverlay` placed at the item's footprint. Second
  click removes it. A 200-item map weighs ~30 KB regardless of how
  many items it carries — users only pay the fetch cost for items they
  actually open. Works with `--timeline` (scrub to a moment, click the
  polygon, see the actual SAR), and is mutually exclusive with the
  pre-baked `--imagery` overlay path. Tunables: `lazy_imagery_asset`
  (default `"GEC"`), `lazy_imagery_percentile` (default `(2.0, 98.0)`).

  Decoding runs on the main thread (no Web Workers), so the saved HTML
  works whether opened over http(s) **or** straight off disk
  (`file://`). Placement stretches the geocoded raster onto its
  lat/lon footprint bbox rather than reprojecting — a quick-look
  approximation; use `imagery=True` for a pixel-accurate, GDAL-
  reprojected overlay.


- `umbra_py.timeline_map` / `save_timeline_map` and a matching `umbra
  map --timeline` CLI flag: render search results as a
  TimestampedGeoJson layer so Umbra's coverage accumulates beneath a
  play button + slider. Each footprint surfaces at its acquisition
  timestamp and keeps the same metadata popup as `footprint_map`.
  Tunables: `period` (slider step, ISO 8601 — `"PT1H"`/`"P1D"`/`"P7D"`
  match a day's / month's / year's search density), `duration` (how
  long each footprint stays visible — `None` accumulates, an ISO
  duration fades it back out), `auto_play`, `loop`, `transition_time`,
  and `geocode` / `geocode_zoom` (same Nominatim reverse-geocoding
  behavior as `footprint_map` — the resolved place name is baked into
  the popup before it ships into the TimestampedGeoJson payload, since
  the plugin renders properties verbatim). The CLI's existing
  `--geocode/--no-geocode` flag now flows through to `--timeline` too.
  `--timeline` is still rejected with `--imagery` (animating base64
  SAR rasters across the slider is a separate, larger lift) or with
  non-HTML output extensions.
- `UmbraCatalog.search(max_per_task=N)` (and `--max-per-task N` on `umbra
  search` / `umbra map`): cap how many items are yielded from any one
  `sar-data/tasks/<task>/` directory. Each task is repeated imaging of
  the same area, so `--max-per-task 1` swaps the usual "every revisit of
  a few sites" output for "one acquisition per distinct site" — much
  better diversity on a map.
- `umbra map --imagery-max-size N` to control how big each SAR overlay
  is read at. Default stays 1024 (modest HTML size); bump to 2048 or
  4096 for sharper overlays at quadratically larger filesizes. Useful
  when you want to zoom in on a single acquisition; remember SAR is
  inherently speckled, so higher resolutions also reveal more noise.
- A small 3-line satellite-orbit animation runs on stderr during
  `umbra map` and `umbra search` to show the catalog walk is making
  progress. Auto-suppressed when stderr isn't a TTY (CI, piped output)
  so captured logs stay clean.

### Fixed
- **Critical: S3 listings silently truncated at 1,000 keys.** The bucket
  lister built `ListObjects` URLs without the `list-type=2` parameter, so S3
  served the **V1** API — which ignores the `continuation-token` the code
  sends and never returns the `NextContinuationToken` it looks for. Every
  listing therefore stopped after its first page: any task directory with more
  than 1,000 objects (e.g. *Centerfield, Utah*) had acquisitions **silently
  missing from every search, index build, gallery, timescan, and change
  detection**, and once Umbra publishes its 1,001st task, whole tasks would
  vanish from top-level discovery with no error. Both `_list_prefix` (delimited
  task discovery) and `_stream_keys` (per-task streaming) now send
  `list-type=2`, so `continuation-token` is honored and every page is consumed.
  Covered by offline regression tests that drive both listers across two
  truncated pages, plus a `network`-marked test asserting a >1,000-key task
  streams past its first page against the live bucket. This is the prerequisite
  the strategy/demo/AI-integration docs name for any "full catalog" work —
  search results are complete again.
- **NumPy 2.5 `DeprecationWarning` from raster reads.** `to_xarray` /
  `to_geotiff` and the viz overview readers (`quicklook`, change/swipe
  composites) read a single band via rasterio's scalar-index `read(1, …)`
  path, which squeezes the band axis with an in-place `ndarray.shape`
  assignment — deprecated in NumPy 2.5, so every read emitted a warning on
  Python 3.12+/NumPy ≥2.5. These now read with a list index into a 3-D
  `out_shape` and drop the band axis explicitly (`read([1], …)[0]`), which
  returns the identical array with no in-place reshape. Output is unchanged;
  the warnings are gone.
- `UmbraItem.asset_href` now resolves a public, fetchable HTTPS URL for
  items built directly from a published STAC sidecar (i.e. `umbra info`,
  `umbra download`, `umbra quicklook`, or `UmbraItem.from_dict(get_json(url))`).
  Umbra's `*.stac.v2.json` sidecars list asset hrefs as `s3://` URLs into a
  *private* processing bucket; the old code returned those verbatim, so
  `rasterio`/CURL failed with `Protocol "s3" not supported` and downloads
  pointed at an inaccessible bucket. The download products actually sit next
  to the sidecar in the open bucket, so any non-HTTP(S) href is now rewritten
  to the sibling public URL relative to the item's own sidecar `href` — which
  also fixes named-task layouts (`tasks/<name>/<task_id>/<acq>/…`) where
  reconstructing from `umbra:task_id` alone produced a 404. `UmbraCatalog.search`
  was unaffected (it already rebuilt public hrefs while walking the bucket).

### Changed
- **Breaking:** `UmbraCatalog.search` now walks Umbra's live data layout
  at `sar-data/tasks/<task>/[<uuid>/]<acquisition>/` (each acquisition has
  a `*.stac.v2.json` sidecar) instead of the legacy `stac/catalog.json`
  tree. The v1 tree is mostly metadata stubs that reference data Umbra
  never published — a 60-item v1 search returned exactly one downloadable
  item. The v2 walker enumerates the actual published acquisitions, so
  every item returned has resolvable asset URLs. Date pruning still works:
  acquisition directory names start with `YYYY-MM-DD-HH-MM-SS`, and the
  walker skips subtrees outside the requested `start` / `end` range.
  Provide a date range — without one the walker scans every published
  acquisition, which takes minutes.
- **Breaking:** `UmbraCatalog(root_url=...)` is gone. Configure the bucket
  via `UmbraCatalog(bucket=..., region=...)` if you ever need a non-default
  endpoint.

### Removed
- **Breaking:** `UmbraCatalog.available_task_ids()` and the
  `search(data_available_only=...)` flag, plus the matching
  `umbra search --available-only` / `umbra map --available-only` flags.
  They were stopgaps that filtered the v1 walk; the v2 walker only ever
  returns items whose data is published, so the filter is redundant.
- **Breaking:** `umbra_py.constants.DEFAULT_STAC_ROOT` (was never publicly
  re-exported).

### Added
- `umbra_py.viz` module for visualizing search results.
  - `item_to_feature`, `items_to_featurecollection`, `write_geojson`:
    convert items to GeoJSON for QGIS, leafmap, Earth Engine, geopandas,
    deck.gl, or any other tool that reads GeoJSON. The third coordinate of
    Umbra's 3D footprints is stripped so they render in 2D viewers.
  - `footprint_map`, `save_footprint_map`: build an interactive Folium map
    of one or more acquisitions, with auto-fit bounds and a metadata popup
    per item. Requires the `viz` extra.
  - `UmbraItem.to_geojson()` convenience method.
- `umbra map` CLI subcommand: search the catalog and write an interactive
  HTML map (`--out footprints.html`) or a GeoJSON FeatureCollection
  (`--out footprints.geojson`) to disk.
- `UmbraItem.asset_href` now resolves empty hrefs in recent Umbra STAC
  items. Umbra currently publishes every asset with `"href": ""` and
  expects consumers to reconstruct the URL from `umbra:task_id` and a
  rename mapping (`<base>_MM.tif` -> `<base>_GEC.tif`, etc.). Items with
  populated hrefs are returned unchanged, so older catalogs and the
  offline test fixture keep working. Unblocks live downloads and the SAR
  image overlay against 2024+ items.
- SAR image overlays on the Folium map.
  - `image_overlay(item)`: stream a downsampled preview of an item's GEC
    cloud-optimized GeoTIFF via HTTP range requests (no full download),
    apply a percentile contrast stretch to handle SAR's wide dynamic
    range, reproject to lat/lon if needed, and return a Folium
    `ImageOverlay` ready to drop onto any map.
  - `footprint_map(items, imagery=True)` / `umbra map --imagery`: one-call
    convenience that combines footprints with the SAR imagery. Each
    overlay is embedded as a base64 PNG so the resulting HTML file is
    self-contained — no tile server required.
  - The `viz` extra now also pulls in `rasterio` and `numpy` for the
    image-overlay path; folium-only users are unaffected.
  - `footprint_map(items, imagery=True)` is resilient to per-item
    failures: when one item's GEC asset is unreachable (404, network
    error, missing pixels), it emits a `UserWarning` and continues, so
    the remaining footprints and overlays still render. Umbra's public
    bucket has many STAC items whose binary data was never published,
    and the previous behavior crashed the whole map on the first one.
  - `image_overlay` now raises `AssetNotFoundError` with a clear message
    when the asset's URL can't be resolved (empty href, no
    `umbra:task_id`), instead of passing an empty URL to rasterio.
  - `footprint_map` now also draws a small always-visible circle marker
    at each footprint's centroid and a fixed-position legend in the
    top-right corner. Filled markers indicate items whose SAR imagery
    was rendered; outlined markers are footprint-only. This solves the
    "I have items, but I can't see any dots at world zoom" problem
    Umbra footprints are only a few km across.

## [0.1.0] - 2026-05-22

Initial release. Discovery + download core for Umbra's open SAR data.

### Added
- `UmbraCatalog`: search Umbra's static STAC catalog by bounding box, date
  range, and product type, with date-based pruning of the catalog tree so a
  constrained search only fetches relevant day catalogs.
- `UmbraItem`: lightweight dataclass over STAC items with metadata accessors
  (platform, product type, polarizations, resolution, incidence angle, …),
  bbox derivation from 3D geometry, and human-readable summaries.
- Anonymous HTTPS downloads (`download_url`, `download_asset`, `download_item`)
  with resume support and progress callbacks.
- `umbra` CLI with `search`, `info`, and `download` commands.
- Optional `convert` extra: `sicd_to_amplitude_geotiff` for inspection-quality
  amplitude extraction from SICD.
- Project scaffolding: Apache 2.0 license, packaging, CI, tests, and docs.

[Unreleased]: https://github.com/theminiverse/umbra-py/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/theminiverse/umbra-py/releases/tag/v0.1.0
