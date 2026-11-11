# Repeat-imaged sites

`find_repeat_sites` ranks the tasks Umbra has imaged more than once. Change
tools (`pick_change_interval`, `stack_stats`, `change_composite`,
`narrate_change`, `timescan`) answer what changed at a site. Each of them
needs the site first. Umbra files every pass of an area under one task, so a
site's coverage is how many dated passes share that task. This tool ranks
those sites. No model is called.

`find_repeat_sites` then `pick_change_interval` then `narrate_change` is a
complete chain when no site is known yet. When the name shares words with a
task (`beet piler`), call this or `search_catalog(area=..., fuzzy=True)`
before `semantic=True`.

The MCP tool description is the short form of this page. The same ranking is
what `umbra sites` and the static showcase's featured gallery use, so those
surfaces agree on what "most repeat-imaged" means.

## What the filters do

`bbox`, `intersects`, `place`, `area`, `fuzzy`, `start`, `end`, `products`,
`polarizations`, `min_incidence`, `max_incidence`, and `max_resolution` bound
the **pool** of acquisitions the ranking is computed over. They do not trim a
site's history after it has qualified, except `start` / `end`, which decide
which passes enter the pool at all.

| Argument | Effect |
| --- | --- |
| `bbox` | WGS84 `[min_lon, min_lat, max_lon, max_lat]`. When it is set, `place` is not geocoded. |
| `intersects` | One GeoJSON `Point`, or a `Polygon` or `MultiPolygon`, optionally wrapped in a `Feature`, `FeatureCollection`, or `GeometryCollection`. A point matches footprints that contain it. Several Points are refused. Cannot be combined with `bbox` or `place`. |
| `place` | Free-text name, geocoded to a bbox only when `bbox` is omitted. |
| `area` | Task-name substring. `fuzzy=True` is a deterministic loose match (word order, punctuation, typos). No model. If it matches no task and `bbox`, `intersects`, and `place` are omitted, it is geocoded. |
| `start`, `end` | Inclusive bounds on which passes are counted. ISO date, bare year or month, or a relative expression (`6 months ago`). |
| `products` | Keep passes that expose one of `GEC`, `CSI`, `SIDD`, `SICD`, `CPHD`. Case-insensitive. |
| `polarizations` | Keep passes exposing any listed polarization. Passes missing it are dropped. |
| `min_incidence`, `max_incidence` | Incidence-angle bounds in degrees. Passes missing incidence are dropped. |
| `max_resolution` | Keep passes this fine or finer, in metres. Passes missing resolution are dropped. |

`top` (default 20) is how many sites come back, best first. `min_passes`
(default 2, the minimum a change composite can use) is how deep a series must
be. That depth is the comparable series when `rank_by` is `comparable`, and
raw dated passes otherwise.

## `rank_by`

| Value | Order |
| --- | --- |
| `passes` (default) | Raw dated-pass count. |
| `comparable` | Depth of the largest same-polarization dated subset a change tool can difference. `min_passes` then applies to that subset, so `rank_by="comparable", min_passes=3` keeps only sites whose differenceable series is at least three passes. Prefer this when the next call is `pick_change_interval` or `stack_stats`. It agrees with `passes` when every dated pass of every site shares one polarization. |
| `recency` | Newest dated pass first. A depth order buries a live site under a deeper dormant one. |
| `span` | Longest first-to-last baseline first. |
| `cadence` | Tightest **median** revisit gap first. One long outage does not bury an otherwise regular series. The worst gap stays a filter (`max_revisit_days`), not the sort key. |

`recency`, `span`, and `cadence` still qualify a site with `min_passes` on
**raw** depth, so a temporal order only returns sites with a series worth
ordering. An unknown `rank_by` raises `ValueError`.

## Recency, onset, cadence, baseline

These select whole sites. They do not truncate each series the way `start` /
`end` do. A site that passes keeps its full history inside the pool.

`active_since` keeps sites whose newest pass is on or after that date (still
being collected). `active_before` keeps sites whose newest pass is on or
before that date (a dormant series). Both together are a window on the newest
pass: `active_since <= last <= active_before`. The grammar matches `start` /
`end`, except a bare year or month on `active_before` covers that whole
period (`active_before="2024"` is last imaged on or before 2024-12-31), the
same way `end` does.

`first_since` / `first_before` are the onset pair: they gate the **earliest**
pass. `first_since` keeps series that appeared on or after the date.
`first_before` keeps series first imaged on or before it, and a bare year or
month snaps to the last day of that period. Both together bound the onset.
Onset and recency are independent, so `first_since=X, active_before=Y` finds
series that appeared after X and had gone dormant by Y. Omit either to apply
no bound on that side.

`max_revisit_days` drops a site whose **worst** gap between consecutive
passes exceeds that many days, including a site with fewer than two passes in
the gated series. `median_revisit_days` drops a site whose **median** gap
exceeds the bound, so one long stretch does not fail a site that is usually
imaged often. Set both to require both. A non-positive value raises
`ValueError`.

`min_span_days` / `max_span_days` bound the first-to-last span in days
(`min_span_days <= span <= max_span_days` when both are set). Cadence is the
gap; span is how long the site has been watched at all. A site with no
measurable span is dropped, so the window only admits a confirmed baseline.
A non-positive value raises `ValueError`.

Under `rank_by="comparable"` the cadence and span filters measure the
differenceable subset. Otherwise they measure the whole dated series. They
are independent of `active_since` / `active_before`.

## `limit` and `local`

`limit` (default 500) sizes the pool only for a live open-bucket walk or a
Canopy archive search. There is no index to group over, so a larger `limit`
sees more sites and deeper series and reads more of the archive. `area`
narrows that pool instead.

On a local index the depth is the whole archive: one group-by over every
pass of the task (`CatalogIndex.rank_sites`). `limit` does not apply. A
deeply imaged site is not ranked from whatever the first `limit` rows
happened to be.

`local` matches `search_catalog`. Omit it to use the on-disk index when one
is present, and a live listing otherwise. `local=True` requires the index.
`local=False` forces a live walk. When `UMBRA_CANOPY_TOKEN` is set, the pool
comes from the commercial archive, and `local=True` is an error because that
archive has no local index.

## What comes back

`sites` is best-first. Each record is a site coverage object:

- `task` and place `label`
- `passes`, `first`, `last`, `span_days`
- `min_revisit_days`, `median_revisit_days`, `max_revisit_days` (the last is the longest gap)
- `comparable_min_revisit_days`, `comparable_median_revisit_days`, `comparable_max_revisit_days` for the differenceable subset (they diverge from the raw figures when a cross-polarization pass fills a gap the change tools cannot use)
- union `bbox`, `products`, `polarizations`
- `comparable_polarizations`: the single polarization the `comparable_*` figures use, a subset of `polarizations` when the site is mixed
- `hrefs`: the site's pass STAC URLs, **oldest first**, ready for `pick_change_interval`, `change_composite`, or `stack_stats`

Also `count`, the pool `source`, the resolved `place` / `bbox` / `area`, a
`query` echo of `rank_by`, `top`, `min_passes`, and the recency, onset,
cadence, and baseline bounds as passed, and the attribution line. The record
shape is `site-coverage` in the published JSON schemas.

When `area` is geocoded, the result adds `matched_by` (`geocode`),
`resolved_place`, and `resolved_bbox`. A name Nominatim does not know, or a
geocoder that is busy or unreachable, sets `hint` and returns no sites.
`resolved_area` is the `area` that was tried. A task-name hit adds nothing.
