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

### G1 — The catalog you'd render is silently incomplete (blocker; fix exists)

The S3 pagination bug verified in `CODEBASE_ANALYSIS.md` §4.1 (`list-type=2`
missing → V1 responses → truncation at 1,000 keys/task) means any
"full library" build today **omits acquisitions from the largest, most
demo-worthy tasks** (Centerfield, Utah already truncates). A full-catalog demo
is the exact use case that makes this bug visible. Two-line fix + tests;
nothing else in this document is worth doing before it.

### G2 — No published full-catalog dataset, and the index only feeds `search`

- A full `umbra index build` is a long crawl every operator must run
  themselves; there is no published nightly `catalog.db` (or GeoJSON /
  PMTiles export) to download. **R1/R5 need a prebuilt dataset.**
- Verified in `cli.py`: **only `umbra search` accepts `--local`/`--db`**. The
  visual commands (`map`, `gallery`, `swipe`, `change`, `timescan`) each
  instantiate `UmbraCatalog()` and re-walk S3 live (cli.py:981, 1119, 500,
  679, 821). Even with a fully built index, `umbra map` cannot use it. The
  library layer is fine (viz functions take item lists, and
  `CatalogIndex.search` yields `UmbraItem`s), so this is CLI wiring, not
  architecture — but it's required before any fast demo flow exists.
- The index lacks demo-oriented denormalizations: no precomputed centroid,
  no cached place label (Nominatim at 1 req/s cannot label thousands of items
  at render time), no cached thumbnail. Baking these in at build time turns
  the index into a real demo backend.

### G3 — No application layer: static HTML generator, not an app

The repo's output surface is Folium-rendered, self-contained HTML. For an
application you need one of:

- **a queryable API** (the `umbra serve` STAC-API façade proposed in
  `AI_INTEGRATION_IDEAS.md` §B2 — does not exist yet), or
- **a static data export + JS front end** (GeoJSON/PMTiles + MapLibre — the
  export half exists as `write_geojson`; the front end does not).

Without one of these there is no way to implement R2's interactive filters or
R4's on-demand product actions. This is the central gap: **everything else on
this list is incremental; this one is a new component.**

### G4 — Scale ceiling of the current map rendering

- A Folium HTML embeds every footprint + popup in the DOM; at full-catalog
  scale (thousands of polygons) load time and interaction degrade. No
  clustering (`MarkerCluster`), no vector tiling, no level-of-detail strategy
  exists in the repo.
- `--imagery` (eager overlays) base64-embeds a PNG per item — unusable beyond
  a few dozen items (the docstrings say so honestly). `--lazy-imagery` is the
  right pattern and already proven in-repo; a demo app should generalize it
  (it currently binds to one asset type, GEC, and one interaction, the popup
  button).
- Practical numbers: the top-level task listing currently fits in one page
  (< 1,000 tasks), so a `max_per_task=1` "one pin per site" world view is
  cheap; the full acquisition set is what needs clustering/tiling.

### G5 — Product capabilities aren't orchestratable from a UI

`change`, `swipe`, `timescan`, `gallery` are separate CLI invocations that
write files. R4 ("demo every capability from the UI") requires either:

- **precomputation**: a pipeline that renders these artifacts for a curated
  set of showcase sites at build time and links them from the map (works
  fully static, zero runtime cost, but only for curated sites), or
- **on-demand execution**: server endpoints that wrap
  `change_composite` / `swipe_map` / `timescan_composite` and return the
  artifact (any site, needs a backend + job semantics — these renders take
  seconds to tens of seconds, so the API needs async/progress affordances
  the library's spinner obviously doesn't provide).

Neither exists today. Note the library functions themselves are cleanly
callable for this (good separation); the gap is purely the orchestration and
delivery layer.

### G6 — No thumbnail/artifact caching layer

Every gallery render re-streams thumbnails from S3; every lazy-imagery click
re-fetches COG overviews; nothing is cached across artifacts or sessions. A
demo app wants a one-time thumbnail bake (e.g. 256-px PNG per acquisition,
~a few KB each, stored alongside or inside the index) so the map and gallery
feel instant. The `_thumbnail_data_uri` machinery is reusable as-is for the
bake step.

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

1. Fix G1 (`list-type=2`) — prerequisite.
2. Wire `--local`/`--db` into the visual CLI commands (G2, small).
3. **Build pipeline** (new, scheduled GitHub Action):
   `umbra index build` → export `catalog.geojson` (exists) → tile to
   **PMTiles** with tippecanoe for the full acquisition set → bake per-item
   thumbnails + place labels into a static `assets/` tree → render showcase
   artifacts (swipe/change/timescan/gallery) for ~6–10 curated sites.
4. **Front end** (new, `demo/` directory): a small **MapLibre GL** app —
   PMTiles source, cluster layer, date-range slider and product-type chips
   filtering client-side, item click → baked thumbnail + metadata card +
   "open STAC item" + lazy COG overlay (port of the existing, proven
   `_lazy_imagery` geotiff.js driver), showcase sites get "Swipe / Change /
   Timescan" buttons linking the precomputed artifacts.
5. Publish via Pages from the same Action.

Meets R1–R3, R5–R7 fully; meets R4 for curated sites only. Zero runtime
infrastructure, zero abuse surface, and the build pipeline doubles as the
nightly prebuilt-index publisher recommended in the analysis doc (#17).

### Path B — Server-backed application (the productized end state; ~3–5 weeks)

Adds on-demand capability over Path A rather than replacing it:

1. `umbra serve` (FastAPI, `[serve]` extra): read-only **STAC API** over
   `CatalogIndex` (search/collections/items) — the same component proposed
   for AI integration (B2), so this work is shared, not duplicated.
2. Artifact endpoints wrapping the existing library functions:
   `GET /quicklook/{id}.png`, `POST /change`, `POST /swipe`, `POST /timescan`
   with async job semantics (renders take seconds–minutes) and a disk cache
   (fixes G5/G6 for *any* site, not just curated ones).
3. Front end grows "run this analysis here" affordances against those
   endpoints; everything else from Path A is reused.
4. Dockerfile + compose for one-command self-hosting; a public instance is a
   policy decision (egress cost of COG streaming is the main consideration),
   not a technical one.

### Requirement coverage

| Req | Today | Path A | Path B |
|---|---|---|---|
| R1 full catalog on map | ✗ (truncated + slow + Folium ceiling) | ✓ | ✓ |
| R2 interactive filters | ✗ | ✓ (client-side) | ✓ (server queries) |
| R3 click → quicklook | partial (lazy-imagery popup) | ✓ (baked + lazy) | ✓ |
| R4 product demos from UI | ✗ | curated sites only | ✓ any site |
| R5 fast | ✗ (live S3 walk) | ✓ (prebuilt data) | ✓ |
| R6 hostable URL | ✗ (local files) | ✓ (Pages) | ✓ (container) |
| R7 polish | ✗ | ✓ | ✓ |

---

## 5. Bottom line

- **Today**: supported as an *operator-driven* demo — the CLI produces
  genuinely impressive artifacts (lazy-imagery timeline maps, swipe
  comparisons, timescans) that show off every capability one file at a time.
- **Not today**: a self-serve, full-catalog, interactive application — the
  repo has no API/server layer, the visual commands can't use the local
  index, the full-catalog data itself is silently truncated by the pagination
  bug, and Folium HTML doesn't scale to the whole archive or to app-grade UX.
- **The good news**: nothing structural is in the way. The library's clean
  separation (search → items → render functions) means the demo app is
  additive — a build pipeline + a small MapLibre front end (Path A), with the
  STAC-API server (Path B) as the shared foundation this document and the AI
  integration plan both want. The single prerequisite for anything labeled
  "full catalog" is the two-line pagination fix.
