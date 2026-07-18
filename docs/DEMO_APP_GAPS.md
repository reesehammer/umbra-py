# umbra-py — Gap Analysis: Full-Catalog Interactive Demo Application

*Question under analysis: can this repository, today, power a demo application
that renders the **full Umbra open-data catalog in a map UX** with intuitive
UI controls for exploring **every product capability** (quicklooks, galleries,
change composites, timescans, swipe comparisons, analysis-ready loading)?*

*Short answer: **partially**. The repo can generate impressive static demo
artifacts today, but a productized, interactive, full-catalog application is
not supported yet — the repo is a library + CLI that emits one-shot static
files, not an application platform. This document inventories exactly what
works now, what's missing, and two costed paths to close the gap. Companion to
[`CODEBASE_ANALYSIS.md`](CODEBASE_ANALYSIS.md) and
[`AI_INTEGRATION_IDEAS.md`](AI_INTEGRATION_IDEAS.md).*

---

## 1. What is supported today (the honest inventory)

You can assemble a compelling *scripted* demo right now with zero new code:

| Capability | Command / API | Demo quality today |
|---|---|---|
| Catalog on a map | `umbra map --out map.html` (Folium HTML; footprints + metadata popups + legend) | Good up to a few hundred items |
| On-click SAR imagery | `umbra map --lazy-imagery` (per-popup "Get SAR image" button streams the COG in-browser via geotiff.js; HTML stays ~30 KB) | Genuinely slick; works from `file://` or static hosting |
| Coverage-over-time animation | `umbra map --timeline --timeline-period P7D` (play button + scrubber) | Good |
| Visual catalog browsing | `umbra gallery` (parallel-streamed thumbnail contact sheet, grouped by task) | Good |
| Before/after comparison | `umbra swipe` (draggable divider, self-contained HTML) | Good |
| Change detection | `umbra change` (2–3 date composite or GIF time-lapse) | Good |
| Activity summary | `umbra timescan` (whole-series temporal statistics) | Good |
| Whole-catalog metadata | `umbra index build` (SQLite of every acquisition) + `umbra search --local` | Works, with caveats below |
| GeoJSON export | `write_geojson` / `umbra map --out x.geojson` (zero-dependency) | Solid building block |

Two structural facts about all of the above:

1. **Every output is a static, one-shot artifact.** A parameter change (new
   date range, different product filter) means re-running a CLI command and
   opening a new file. There is no interactive re-query.
2. **The UI controls are whatever Folium plugins provide** — popups, a time
   slider, a swipe divider. There is no faceted filtering, no bbox draw tool,
   no search box, no "click a site → run a product demo" flow. Those require
   an application layer this repo intentionally doesn't have yet.

So: a **guided demo** (operator drives the CLI, audience sees the artifacts)
is fully supported today. A **self-serve demo application** (user explores
freely in one UX) is not.

---

## 2. Target definition

Assumed requirements for "productized demo application," inferred from the
question — worth confirming before building:

- **R1** Full catalog visible on one map (every task, every acquisition).
- **R2** Interactive filters: date range, product type, area/place, platform.
- **R3** Click an item → metadata + instant quicklook.
- **R4** Trigger product capabilities from the UI: gallery of a site, swipe
  two passes, change composite, timescan — without touching a terminal.
- **R5** Fast (no multi-minute S3 walks in the user's critical path).
- **R6** Hostable/shareable (a URL, not a folder of files).
- **R7** Demo-grade polish: loading states, attribution, sensible defaults.

---

## 3. The gaps, ordered by how hard they block the target

### G1 — The catalog you'd render is silently incomplete (blocker) — **fixed** (PR #29)

✅ **Resolved.** The S3 pagination bug from `CODEBASE_ANALYSIS.md` §4.1
(`list-type=2` missing → V1 responses → truncation at 1,000 keys/task) is
fixed: both listers now send `list-type=2`, so full-library builds include the
largest, most demo-worthy tasks (Centerfield, Utah verified streaming past
1,000 keys against the live bucket). This was the single prerequisite everything
else in this document waited on — the remaining gaps (G2–G8) are now the
critical path to a demo application.

### G2 — The prebuilt dataset is fetchable and now feeds the visual commands too

- ✅ **Prebuilt dataset now downloadable.** The weekly workflow publishes a
  `catalog.db` snapshot on the rolling `catalog-index` release, and
  `umbra index fetch` / `CatalogIndex.from_release()` pulls it to the default
  index path — so an operator no longer runs the long crawl to satisfy R1/R5,
  and a build pipeline can bootstrap from the snapshot in seconds. (A
  GeoJSON / PMTiles export for a JS front end is still the build-pipeline step
  in Path A below.)
- ✅ **The visual commands now read the index (Path A step 2, done).** `map`,
  `gallery`, `swipe`, `change` and `timescan` accept the same `--local` /
  `--index-db` options as `search` and route through a shared `_gather_items`
  helper, so a fully built/fetched index renders whole-catalog maps and galleries
  from local SQL in milliseconds instead of re-walking S3. Previously only
  `umbra search` could use the index. (The path flag is `--index-db` because the
  render commands already use `--db` for the decibel stretch.) This was the
  "required before any fast demo flow exists" wiring — it is now in place.
- ✅ **Cached place labels now baked into the index** (`umbra index bake`).
  Reverse geocoding used to run at *render* time, where Nominatim's 1 req/s cap
  makes labelling thousands of items impractical, so the explorer fell back to
  the task codename. `CatalogIndex.bake_places()` reverse-geocodes each
  footprint centroid once at build time (idempotent, injectable geocoder) into a
  new additive `place` column — the first real migration the schema-versioning
  was landed to enable (`user_version` 1 → 2, in place, no rebuild). Every
  `search`/`get` then yields the label on `UmbraItem.place`, so `umbra demo
  --local` (and any `--local` visual command) shows real geographic names
  instantly, with zero per-render geocoding, and `umbra index info` reports label
  coverage. ✅ **The baked label now flows through every read surface too:**
  `UmbraItem.to_llm_context()` prefers it over the task codename, `umbra map` /
  `--timeline` use it directly (so a fully-baked `--local` render never geocodes
  at render time — the Nominatim session is built lazily only for still-unlabelled
  items), `umbra serve` surfaces it as a namespaced `umbra:place` STAC property,
  and the stac-geoparquet export carries `umbra:place` into the published
  snapshot. Still open: a precomputed **centroid** column (cheap — it is derived
  from the stored bbox today) and a **cached thumbnail** bake (G6).

### G3 — Application layer: a self-serve static explorer now ships (`umbra demo`)

> **Update (2026-07-15):** the first **self-serve interactive application** has
> shipped as `umbra demo` (`umbra_py.demo`, no extra required) — Path A's front end
> (step 4), delivered as a self-contained artifact rather than a separate JS
> build toolchain, so it holds the repo's "one hostable HTML file" grain. One
> page over a whole gathered slice of the catalog now carries the interactive
> controls this section and G4 named as absent: **client-side faceted filters**
> (free-text site/id search, a data-bounded date-range slider, product-type
> chips), **marker clustering** (`Leaflet.markercluster`) so it scales past the
> Folium polygon ceiling, a detail panel that draws the selected footprint, and
> the click-to-quicklook SAR overlay (reusing the proven `_lazy_imagery`
> geotiff.js driver via a `window.umbraLazyMap` fallback). It routes through the
> same `_gather_items` helper, so `--local` builds it from the prebuilt index in
> milliseconds (R5). This meets **R1–R3, R6–R7** for the gathered slice with zero
> runtime infrastructure. R4's *render actions over any site* now have a backend
> — the on-demand `/artifacts/...` endpoints on `umbra serve` (Path B step 2,
> shipped) render quicklook/change/timescan for any site; what remains is wiring
> this front end to call them (and a `swipe` endpoint). The
> full-acquisition-set PMTiles tiling for the truly whole-catalog view has since
> shipped as `umbra tiles` (Path A step 3, see G4).

For the *server-backed* end state (Path B), the remaining options for the
application layer are one of:

- **a queryable API.** This now exists for both client classes. For *AI*
  clients: the `umbra-mcp` MCP server (`AI_INTEGRATION_IDEAS.md` §B1, shipped)
  is a queryable, schema'd tool surface over the catalog. For a *browser* front
  end: the `umbra serve` read-only STAC API (§B2, ✅ **now shipped**) serves
  `/search`, `/collections` and `/collections/{id}/items` over the same
  `CatalogIndex`, plus an OpenAPI doc at `/docs`. A MapLibre/leafmap front end
  (or any `pystac-client` / stac-browser client) can now query the catalog
  live — R2's interactive filters and R4's per-site actions have a backend to
  call. What remains for a full app is the front end itself, and (for R4 over
  *any* site) the on-demand artifact endpoints in Path B. Alternatively, still
  server-free:
- **a static data export + JS front end** (GeoJSON/PMTiles + MapLibre — the
  export half exists as `write_geojson`; the front end does not).

This was the central gap. With `umbra serve` shipped — and now its on-demand
`/artifacts/...` render endpoints — the queryable-API *and* the render-over-any-site
halves are built: **R2's interactive filters have a backend to call, and R4's
per-site render actions (quicklook/change/timescan) do too.** What is still
missing for a *self-serve* app is the front end (a MapLibre/leafmap client) that
calls them, plus a `swipe` endpoint.

### G4 — Scale ceiling of the current map rendering — **closed** (`umbra tiles`)

- A Folium HTML embeds every footprint + popup in the DOM; at full-catalog
  scale (thousands of polygons) load time and interaction degrade. ✅ **Partly
  addressed:** `umbra demo` clusters item centroids with
  `Leaflet.markercluster` and draws a footprint polygon only for the *selected*
  item (a level-of-detail strategy — thousands of clustered points instead of
  thousands of DOM polygons), so it scales far past the Folium map. ✅ **Now
  fully addressed for the whole acquisition set:** `umbra tiles`
  (`umbra_py.pmtiles`, no extra) pre-cuts the catalog's acquisition centroids
  into a **PMTiles** vector-tile pyramid, so a map fetches only the tiles in the
  current view instead of holding the whole catalog in the DOM or an inline JSON
  blob. It needs no tippecanoe — the geometry is points, so the Mapbox Vector
  Tile + PMTiles v3 encoder is pure standard library and offline-testable by
  decoding its own output; `--viewer` emits a self-contained MapLibre GL page
  over the archive. This was the build-pipeline step Path A step 3 named as
  open.
- `--imagery` (eager overlays) base64-embeds a PNG per item — unusable beyond
  a few dozen items (the docstrings say so honestly). `--lazy-imagery` is the
  right pattern and already proven in-repo; a demo app should generalize it
  (it currently binds to one asset type, GEC, and one interaction, the popup
  button).
- Practical numbers: the top-level task listing currently fits in one page
  (< 1,000 tasks), so a `max_per_task=1` "one pin per site" world view is
  cheap; the full acquisition set is what needs clustering/tiling.

### G5 — Product capabilities aren't orchestratable from a UI — **shipped end-to-end (server + front end)**

`change`, `swipe`, `timescan`, `gallery` began as separate CLI invocations that
write files. R4 ("demo every capability from the UI") requires either:

- **precomputation**: a pipeline that renders these artifacts for a curated
  set of showcase sites at build time and links them from the map (works
  fully static, zero runtime cost, but only for curated sites), or
- **on-demand execution**: server endpoints that wrap
  `change_composite` / `swipe_map` / `timescan_composite` and return the
  artifact (any site).

✅ **The on-demand path now exists on `umbra serve`.** The STAC API façade
mounts three render endpoints alongside search: `GET
/artifacts/quicklook/{item_id}.png`, `POST /artifacts/change` and `POST
/artifacts/timescan`. They resolve the requested acquisitions from the same
`CatalogIndex` (by `ids`, or a `bbox` + `datetime` query — subsampling
deterministically when a query resolves to more frames than a composite takes),
call the existing `umbra_py.viz` functions unchanged, and return a PNG. This
meets **R4 over any site**, not just a curated set. The library functions were
already cleanly callable (good separation); the endpoints are purely the
orchestration/delivery layer this section named as the gap. Rendering is
synchronous for a first, honest slice — a composite streams a downsampled
overview per pass and returns in seconds; the async job/progress semantics for
long renders are the ledgered follow-on (`TODO.md`).

✅ **The front end now calls them, and the fourth product (swipe) is wired.**
`POST /artifacts/swipe` renders `viz.swipe_map` (an interactive before/after
HTML page) alongside the three PNG composites, and `umbra serve` sets a
permissive read-only CORS policy so a browser page on any origin can reach it.
`umbra demo --server-url <serve URL>` adds an "Analyze this view" sidebar panel
whose Change / Timescan / Swipe buttons POST the currently-filtered acquisitions
to the matching endpoint and render the returned artifact in place (swipe opens
its map in a new tab). This is the R4 "run this analysis here" affordance over
*any* site, closing the self-serve loop; without `--server-url` the page stays a
fully static single file.

✅ **Async job semantics for the longest renders now ship too.** A composite
request that carries `"async": true` no longer holds the request for the whole
render: the server queues it on a small background pool and returns
`202 Accepted` + a job id, the client polls `GET /jobs/{id}`
(`queued` → `running` → `succeeded` | `failed`), and fetches the finished
artifact from `GET /jobs/{id}/result`. The disk cache *is* the result store, so a
completed job's result is a cache entry (and an already-cached key returns an
already-`succeeded` job with no work). Nothing under this heading now remains
open.

### G6 — No thumbnail/artifact caching layer — **closed**

Every gallery render re-streams thumbnails from S3; every lazy-imagery click
re-fetches COG overviews; nothing was cached across artifacts or sessions. ✅
**The `umbra serve` render endpoints cache to disk** keyed by a content hash
of the render's kind, ordered frame ids and options, so a repeat request for the
same quicklook/change/timescan is a file read (`X-Umbra-Cache: hit`) rather than
a re-render. ✅ **The one-time thumbnail bake now ships too** — `umbra index
bake-thumbnails` (`CatalogIndex.bake_thumbnails`) renders a small (256-px default)
PNG per acquisition once at build time and caches the bytes in the index's new
additive `thumbnail` column (`user_version` 2 → 3), so the *first* view is instant
rather than S3-bound. `umbra serve` serves it from `GET
/artifacts/thumbnail/{id}.png` (`CatalogIndex.get_thumbnail`) — no render, an
offline file read, `404` → fall back to `/artifacts/quicklook`. It mirrors
`bake_places`: idempotent (only unbaked items are rendered), `--limit` for bounded
batches, an unrenderable scene skipped and retried, and an **injectable** renderer
(default `viz._thumbnail_png`) so the whole path is offline-tested. ✅ **The
`umbra demo` client now consumes it:** with `--server-url` set, clicking a scene
leads its detail panel with the baked thumbnail pulled from
`GET /artifacts/thumbnail/{id}.png` (an instant local-bytes read, falling back to
a quicklook render), so the flagship explorer opens with a radar picture rather
than metadata alone — the heavier on-click "Get SAR image" COG overlay stays the
deeper look. A scene with no baked thumbnail 404s and the element is dropped (no
broken image); without `--server-url` the panel is unchanged and the page stays
fully static. Optional polish that remains: the same preview in the `umbra
gallery` contact sheet, and baking thumbnails into the published weekly snapshot.

### G7 — No packaging or hosting story

No `demo/` app, no Dockerfile, no `umbra demo` command, no GitHub Action that
builds and publishes demo artifacts, no Pages deployment. Also relevant:
the package isn't on PyPI (analysis doc P0 #2), so even the operator-driven
demo starts with a git clone.

### G8 — Demo-grade UX polish is outside Folium's reach

Attribution display (CC-BY is mandatory — the string exists in `constants.py`
but the map templates surface it only in popups), loading/progress states,
mobile layout, keyboard/accessibility, error surfaces ("this COG failed to
stream") — Folium's template model gives little control over any of these.
This is not a criticism of the library (Folium is the right choice for
notebook output); it's a signal that the demo front end should be its own
small JS app, not a bigger Folium template.

---

## 4. Two viable paths to close the gaps

### Path A — Static-first demo (recommended first; ~1–2 weeks of focused work)

No servers, hostable on GitHub Pages, and every piece builds on something
already in the repo:

1. ✅ Fix G1 (`list-type=2`) — prerequisite, **done in PR #29**.
2. ✅ Wire `--local`/`--index-db` into the visual CLI commands (G2, small) —
   **done**: `map`/`gallery`/`swipe`/`change`/`timescan` render from the index.
3. ✅ **PMTiles tiling of the full acquisition set — done** (`umbra tiles`).
   `umbra index build`/`fetch` → `umbra tiles --local --out catalog.pmtiles`
   emits a single-file vector-tile pyramid of every acquisition centroid, and
   `--viewer` writes a MapLibre GL page over it. The doc originally sketched this
   as `export catalog.geojson → tile with tippecanoe`; because the geometry is
   points, the whole encoder is pure standard library (no external binary), so it
   runs in a core install and is offline-tested by decoding its own output.
   **The basemap is now also *published*:** the weekly `publish-index.yml`
   workflow tiles the freshly built index and uploads `catalog.pmtiles` (plus a
   `catalog.html` viewer pointed at it) to the rolling `catalog-index` release
   beside `catalog.db`, and `umbra tiles --fetch` / `fetch_prebuilt_pmtiles()`
   pull it — so a fresh install (and a Pages showcase) gets the whole-archive map
   with no local tiling step, the artifact `STRATEGY.md` 5.2 wants to offer
   upstream. Still open under this step as optional polish: baking per-item
   thumbnails + place labels into a static `assets/` tree, and rendering showcase
   swipe/change/timescan/gallery artifacts for ~6–10 curated sites.
4. ✅ **Front end (done, delivered as an artifact): `umbra demo`.** Rather than a
   separate `demo/` MapLibre build toolchain, the front end ships as a
   self-contained HTML generator (`umbra_py.demo`) in the library's own grain: a
   Leaflet + `Leaflet.markercluster` page with a cluster layer, a date-range
   slider and product-type chips filtering client-side, a free-text site search,
   an item click → metadata card + "open STAC item" + the lazy COG overlay
   (reusing the proven `_lazy_imagery` geotiff.js driver). It reads the prebuilt
   index (`--local`), so the whole build is offline and near-instant. The
   *fully* whole-catalog view now also ships as a PMTiles source (`umbra tiles`,
   step 3) with its own MapLibre GL viewer. Still open as polish: baked
   thumbnails/labels, and showcase "Swipe / Change / Timescan" buttons linking
   precomputed artifacts (R4 for curated sites).
5. Publish via Pages from the same Action.

Meets R1–R3, R5–R7 fully; meets R4 for curated sites only. Zero runtime
infrastructure, zero abuse surface, and the build pipeline doubles as the
nightly prebuilt-index publisher recommended in the analysis doc (#17).

### Path B — Server-backed application (the productized end state; ~3–5 weeks)

Adds on-demand capability over Path A rather than replacing it:

1. ✅ `umbra serve` (FastAPI, `[serve]` extra): read-only **STAC API** over
   `CatalogIndex` (search/collections/items) — the same component proposed
   for AI integration (B2), so this work is shared, not duplicated. **Shipped**;
   the STAC search backend the rest of this path builds on now exists. The STAC
   **Query extension** now also exposes the index's `product_types` / free-text
   `area` / `fuzzy` filters over `/search`, so a client can pre-filter the
   catalog server-side, not just by bbox/datetime/ids.
2. ✅ **Artifact endpoints wrapping the existing library functions —
   shipped, now with async job semantics.** `GET /artifacts/quicklook/{id}.png`,
   `POST /artifacts/change`, `POST /artifacts/timescan` and `POST /artifacts/swipe`
   render on demand over *any* site and cache each result to disk keyed by its
   inputs (fixes G5, and G6 for these endpoints). A composite request can opt in
   to `"async": true` to get a `202 Accepted` + a job id, poll `GET /jobs/{id}`,
   and fetch the result from `GET /jobs/{id}/result` (the disk cache is the result
   store) — so a large `max_size` or a long timescan no longer holds a request for
   its whole duration. Nothing here remains open.
3. Front end grows "run this analysis here" affordances against those
   endpoints; everything else from Path A is reused.
4. Dockerfile + compose for one-command self-hosting; a public instance is a
   policy decision (egress cost of COG streaming is the main consideration),
   not a technical one.

### Requirement coverage

| Req | Today (`umbra demo`) | Path A | Path B |
|---|---|---|---|
| R1 full catalog on map | ✓ gathered slice, clustered; ✓ full acquisition set via `umbra tiles` PMTiles + MapLibre viewer | ✓ | ✓ |
| R2 interactive filters | ✓ (client-side: search, date range, product chips) | ✓ (client-side) | ✓ (server queries) |
| R3 click → quicklook | ✓ (lazy COG overlay + metadata card) | ✓ (baked + lazy) | ✓ |
| R4 product demos from UI | ✓ any site (`umbra serve` renders quicklook/change/timescan/swipe; `umbra demo --server-url` wires the front end to call them) | curated sites only | ✓ any site |
| R5 fast | ✓ (`--local` prebuilt index) | ✓ (prebuilt data) | ✓ |
| R6 hostable URL | ✓ (one static HTML file) | ✓ (Pages) | ✓ (container) |
| R7 polish | ✓ (filters, attribution, loading states) | ✓ | ✓ |

---

## 5. Bottom line

- **Today**: supported as an *operator-driven* demo (the CLI produces impressive
  one-file artifacts) **and now as a self-serve explorer for a gathered slice of
  the catalog** — `umbra demo` emits a single interactive page with client-side
  filters, clustered markers, and click-to-quicklook SAR, hostable as one static
  file. This closes R1–R3 and R5–R7 for the gathered slice with zero runtime
  infrastructure (G3 met, G4 partly met).
- **Now also self-serve for R4**: `umbra serve` renders
  quicklook/change/timescan/swipe over *any* site on demand and caches the
  results (Path B step 2), and `umbra demo --server-url` wires the front-end
  "run this analysis here" affordance that calls those endpoints (Path B step 3)
  — the last self-serve-demo gap. Long renders are no longer blocking: a
  composite request can opt in to `"async": true` for a `202 Accepted` + a
  `GET /jobs/{id}` poll loop, with `GET /jobs/{id}/result` serving the finished
  artifact from the disk cache. (Former blockers gone: the pagination bug is
  fixed in PR #29, the visual commands render from the prebuilt index via
  `--local`, the self-serve front end ships as `umbra demo`, the on-demand
  artifact endpoints ship on `umbra serve`, and the demo now calls them.)
- **Now also the *truly whole-catalog* view**: `umbra tiles` tiles the full
  acquisition set into a single-file PMTiles vector pyramid (pure standard
  library, no tippecanoe) and `--viewer` renders it with MapLibre GL — a map that
  fetches only the tiles in view, so it stays fast at whole-archive scale
  (Path A step 3). Async job semantics for the longest renders, formerly listed
  here, are also shipped.
- **Not yet**: optional demo polish under Path A step 3 — baking per-item
  thumbnails / place labels into a static `assets/` tree, and precomputed
  showcase swipe/change/timescan artifacts for a handful of curated sites.
- **The good news**: nothing structural is in the way. The library's clean
  separation (search → items → render functions) means the demo app is
  additive — a build pipeline + a small MapLibre front end (Path A), with the
  STAC-API server (Path B) as the shared foundation this document and the AI
  integration plan both want. The single prerequisite for anything labeled
  "full catalog" — the two-line pagination fix — is now landed (PR #29), so
  Path A's remaining steps are unblocked.
