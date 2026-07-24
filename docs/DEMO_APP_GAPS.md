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
>   docs). Remaining optional polish: baking thumbnails/labels into the
>   *published* snapshot, and precomputed showcase swipe/change/timescan
>   artifacts for a handful of curated sites.
>
> The original item IDs (`G1`–`G8`, `R1`–`R7`, Path A/B step numbers) are still
> cited from source docstrings; the detail behind each is in this file's git
> history.
