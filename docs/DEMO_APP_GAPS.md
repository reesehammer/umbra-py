# umbra-py — Demo-Application Gap Analysis (consolidated)

> **This document has been consolidated.** It analysed whether the repo could
> power a full-catalog interactive demo application and inventoried the gaps
> (`G1`–`G8`) plus two build paths (Path A static-first, Path B server-backed).
> Nearly all of it has shipped: the pagination fix (G1, PR #29), the visual
> commands reading the prebuilt index (G2), the `umbra demo` self-serve
> explorer (G3), marker clustering + `umbra tiles` PMTiles (G4), the on-demand
> `umbra serve` render endpoints with async jobs (G5), the thumbnail bake and
> disk cache (G6), and the map attribution (G8).
>
> To keep status in one place, this file no longer carries the full gap
> analysis. Instead:
>
> - **What shipped** → [`CHANGELOG.md`](../CHANGELOG.md).
> - **What's still open** → [`STRATEGY.md` §8](STRATEGY.md#8-current-status--remaining-critical-path)
>   and [`TODO.md`](../TODO.md). The G7 packaging story is now **shipped**: the
>   Dockerfile + compose for one-command self-hosting of `umbra serve` (with a
>   first-boot index fetch and a `/healthz` probe), *and* the **GitHub Pages
>   deploy of the static `umbra demo` / `catalog.pmtiles` showcase** (`umbra
>   showcase` composes the whole-catalog map + interactive explorer + a landing
>   page, and the `docs.yml` Pages job publishes `site/showcase/` beside the
>   docs). **G6 is now fully closed too**: the baked per-item thumbnails are
>   *published*, as a separate opt-in `catalog.thumbs.db` sidecar (`umbra index
>   fetch-thumbnails`) so `catalog.db` stays small, topped up incrementally each
>   week rather than re-streamed from the bucket. The **R4 precomputed-artifact
>   polish is shipped too**, for the change
>   view: `umbra showcase --featured N` renders a change composite per marquee
>   site into `featured/` and shows them as a captioned gallery on the landing
>   page (the Pages job passes `--featured 6`). And **Path A's last structural
>   cap is gone**: `umbra demo --pmtiles` gives the interactive explorer a
>   whole-catalog vector-tile source (the sidebar filters compile to MapLibre
>   expressions evaluated inside the tiles), so `umbra showcase --unified` — what
>   the Pages job now deploys — is *one* page covering every acquisition with the
>   filters, instead of a click-only map beside a sliced explorer. **R4 is now
>   fully closed**: `umbra showcase --featured-view {change,timescan,swipe}`
>   renders the same marquee selection as a whole-series timescan composite or as
>   a self-contained before/after swipe page (shown as a link card, since an
>   interactive page has no still to preview), so all three variants of the
>   featured gallery ship. Remaining optional polish: baking thumbnails/labels
>   into the *published* snapshot. **Footprint polygons are
>   now tiled too** (a `footprints` layer clipped per tile, from zoom 6 up), so the
>   whole-archive explorer draws coverage shape as you zoom in. **And the on-click
>   "Get SAR image" COG overlay now works over the whole archive too** — the last
>   capability the embedded-slice explorer had over it: each tiled feature
>   references its GEC cloud-optimized GeoTIFF (a bare filename resolved against
>   the `stac_href` the tiles already carry) plus the bounds to place it, and the
>   shared geotiff.js driver grew a MapLibre placement beside its Leaflet one, so
>   any acquisition in the archive is one click from its radar picture. **And the
>   last two fields are tiled now too** — `pol` and `assets`, comma-joined since a
>   vector-tile property is a scalar — which closed the difference *and* bought a
>   facet neither explorer had: a **polarization filter** (chips in both modes; a
>   MapLibre `index-of` test inside the tiles for the whole-archive one). That is
>   the facet deciding whether a change measurement is *valid* rather than merely
>   what it shows — `POST /artifacts/stats` refuses a mixed-polarization
>   selection, advice the page previously had no control to follow. **Path A is
>   closed**: the whole-archive front end is a strict superset of the
>   embedded-slice one.
>
> The original item IDs (`G1`–`G8`, `R1`–`R7`, Path A/B step numbers) are still
> cited from source docstrings; the detail behind each is in this file's git
> history.
