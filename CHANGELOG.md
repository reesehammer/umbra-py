# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **`max_revisit`: a worst-case-cadence filter on the discovery moat, selecting the
  sites imaged often enough to monitor, on every surface (`coverage.py`,
  `showcase.py`, `index.py`, `cli/discover.py`, `serve.py`, `mcp_server.py`,
  `tests/test_coverage.py`, `tests/test_showcase.py`, `tests/test_index.py`,
  `tests/test_serve.py`, `tests/test_mcp_server.py`).** The discovery moat *reported*
  each site's revisit cadence (`max_revisit_days` and its comparable twin) but could
  only rank and qualify sites by depth and recency — so it could not answer "which
  repeat-imaged sites have **no blind spot longer than N days**?", the question that
  separates a monitorable target from a deep-but-bursty series where a change could
  have slipped through a months-long gap unseen. `umbra sites --max-revisit DAYS`
  (and `find_repeat_sites(max_revisit_days=…)`, `GET`/`POST /sites?max_revisit=`, and
  `CatalogIndex.rank_sites(max_revisit_days=…)` for `--local`) keeps only sites whose
  **worst-case** gap between consecutive passes is at most `DAYS`, dropping a site
  with any longer stretch and a site with fewer than two passes (no measurable
  cadence, dropped like `active_since` drops an undatable site). It measures the same
  depth `--rank-by` does (`coverage._passes_cadence`, the cadence twin of
  `_min_passes_depth`): under `--rank-by comparable` it gates the *analysable*
  series' worst gap (`comparable_max_revisit_days`), so a site whose raw cadence
  looks tight only because an off-polarization pass fills a gap no change verb can
  use is not admitted. It is single-sourced through the same two functions the other
  filters are — `showcase.select_featured_sites` (the pool path) and
  `CatalogIndex.rank_sites` (the whole-archive index) — but, unlike the recency
  filters, the worst *consecutive* gap is not a SQL aggregate, so the index path
  applies the identical `_passes_cadence` in Python on the same per-task items it
  already reads to summarise (byte-identical to the pool path, pinned by the
  index-vs-pool test for every cutoff) and drops the raw-count SQL `LIMIT` when the
  filter is set — as the comparable ranking already does — so a tightly-imaged site
  outside the raw top-`top` is promoted rather than truncated before the filter runs.
  It is orthogonal to `--active-since` / `--active-before` (cadence vs. recency of the
  newest pass) and distinct from `--start` / `--end` (which bound the passes). On the
  HTTP surface a non-positive bound is a clean client error (`gt=0` on `GET`, a `400`
  on `POST`), and the `umbra sites` empty-result message names the cadence bound it
  applied and offers `--max-revisit` to loosen. Adds no field to the `site-coverage`
  contract (a filter input, like the recency bounds), so no schema moved. **With it
  the discovery moat *selects* on all three axes it reports — depth, recency and now
  cadence — not only ranks and qualifies on depth.**
- **`active_before`: the upper recency bound on the discovery moat, completing the
  activity-window selection on every surface (`showcase.py`, `coverage.py`,
  `index.py`, `cli/discover.py`, `serve.py`, `mcp_server.py`,
  `tests/test_showcase.py`, `tests/test_coverage.py`, `tests/test_index.py`,
  `tests/test_serve.py`, `tests/test_mcp_server.py`).** `active_since` added the
  moat's missing *recency* axis but gated only one side of it — a site's **newest**
  pass being on or *after* a date (still-active sites). It had no way to ask the
  complement: which repeat-imaged sites have gone *dormant* (stopped imaging), or
  which sites' latest pass falls *within* a window. `umbra sites --active-before
  DATE` (and `find_repeat_sites(active_before=…)`, `GET`/`POST /sites?active_before=`,
  and `CatalogIndex.rank_sites(active_before=…)` for `--local`) keeps only sites
  whose newest dated pass is on or *before* `DATE`, and set together with
  `--active-since` the two bound the site's latest pass to a window
  (`active_since <= last <= active_before`). It takes the same grammar
  `active_since` does (an ISO date, a bare year/month, or a relative expression like
  `"6 months ago"`), but a span expression snaps to its *last* day — a bare
  year/month covers the whole named period, so `--active-before 2024` means "last
  imaged on or before 2024-12-31" — symmetric with the `--end` bound, where
  `--active-since` snaps to the first day. It is single-sourced through the same two
  functions `active_since` is: `showcase.select_featured_sites` (the pool path)
  gates each site's newest pass in Python, and `CatalogIndex.rank_sites` answers it
  whole-archive in the *same* `GROUP BY` — a twin `HAVING … AND MAX(acq_date) <= ?`
  clause that costs nothing beyond the group already computed and drops an undatable
  site (a NULL `MAX` fails `<= ?`) exactly as the pool path does. The byte-identical
  index-vs-pool pinning test now covers `active_before` and the combined window for
  every cutoff, so CLI, agent tools, the hosted API and the featured gallery cannot
  disagree about which sites are dormant. On the HTTP surface a malformed date is a
  clean `400` (coerced in the route with `is_end=True`, like `end`), and the `umbra
  sites` empty-result message names the recency window it applied and offers both
  `--active-*` bounds to loosen. Adds no field to the `site-coverage` contract (a
  filter input, like `active_since`), so no schema moved. This completes the
  discovery moat's recency axis: the moat can now *select* on activity in both
  directions — still-active, dormant, or a latest-pass window — alongside the depth
  and cadence it reports.
- **`active_since`: a recency filter on the discovery moat, on every surface
  (`showcase.py`, `coverage.py`, `index.py`, `cli/discover.py`, `serve.py`,
  `mcp_server.py`, `tests/test_showcase.py`, `tests/test_coverage.py`,
  `tests/test_index.py`, `tests/test_serve.py`, `tests/test_mcp_server.py`).**
  The discovery moat could rank the archive's most repeat-imaged sites and report
  each one's `first` / `last` dates, but it could not *select* the sites still
  being imaged — the difference between a historical series and a live monitoring
  target, which is exactly the site a curious analyst would want to task (§1's
  funnel). `--start` / `--end` could not express it: those bound which *passes*
  enter the pool, truncating every series to a window, so they answer "the recent
  slice of every site" rather than "which whole sites are still active." `umbra
  sites --active-since DATE` (and `find_repeat_sites(active_since=…)`, `GET`/`POST
  /sites?active_since=`, and `CatalogIndex.rank_sites(active_since=…)` for
  `--local`) keeps only sites whose **newest** dated pass is on or after `DATE`,
  and keeps each survivor's *full* history — a whole-site recency gate, orthogonal
  to `--rank-by` and `--min-passes` (it measures the site's latest pass, whatever
  depth the ranking counts). It accepts anything `--start`/`--end` do (an ISO date,
  a bare year/month, or a relative expression like `"6 months ago"`), reusing
  `dates.parse_date_bound` rather than adding a second parser. It is single-sourced
  through the two functions all four surfaces forward to:
  `showcase.select_featured_sites` (the search-pool path, which `rank_site_coverage`
  and the live/`--token` backends use) applies it in Python on each site's newest
  pass, and `CatalogIndex.rank_sites` answers it whole-archive in the *same*
  `GROUP BY` — a `HAVING … AND MAX(acq_date) >= ?` clause that costs nothing beyond
  the group already computed and is exact under either ranking (a site's latest pass
  does not depend on the polarization grouping `comparable` re-ranks by). A test
  pins the index and pool paths byte-identical for every recency cutoff, so CLI,
  agent tools, the hosted API and the featured gallery cannot disagree about which
  sites are still active. On the HTTP surface a malformed date is a clean `400`
  (coerced in the route, like `start`/`end`), and the `umbra sites` empty-result
  message names the recency bound it applied. Adds no field to the `site-coverage`
  contract (it is a filter input, like `min_passes` / `rank_by`), so no schema
  moved. This adds the one discovery axis the moat lacked — *recency* — alongside
  the depth (`passes` / `comparable`) and cadence it already reports.
- **`--rank-by comparable` now also floors `--min-passes` on analysable depth, on
  every discovery surface (`coverage.py`, `showcase.py`, `index.py`,
  `cli/discover.py`, `serve.py`, `mcp_server.py`,
  `tests/test_coverage.py`, `tests/test_showcase.py`, `tests/test_index.py`).**
  The comparable-figure workstream made a site's *ranking* honest about
  analysable depth — `--rank-by comparable` orders by `comparable_passes`, the
  largest single-polarization dated subset a change verb can actually difference,
  so a broad-but-mixed site cannot outrank a deeper single-polarization one. But
  the *qualification floor* was left on the raw count: `min_passes` still counted
  every dated pass, so `umbra sites --rank-by comparable --min-passes 3` ranked by
  usable depth yet admitted sites whose differenceable series was only one or two
  passes deep — the exact "raw count overstates what is analysable" error the
  ranking corrects, still present on the threshold. `min_passes` now measures the
  same depth `rank_by` ranks by (`coverage._min_passes_depth`, the qualification
  twin of `_rank_sort_key`): under `"comparable"` a site qualifies on its
  `comparable_passes` depth, so `--rank-by comparable --min-passes N` means "sites
  whose differenceable series is at least `N` passes deep" — not "sites with `N`
  raw passes, ranked by their usable depth". Because `comparable_passes <= passes`
  this only ever *narrows* a comparable ranking (it never admits a site the raw
  floor rejected), and the default `"passes"` ranking is untouched, so nothing
  that shipped — the featured gallery, every default `umbra sites` run — changes.
  It is single-sourced through the two functions all four surfaces forward to:
  `showcase.select_featured_sites` (the search-pool path, which
  `rank_site_coverage` and the live/`--token` backends use) and
  `CatalogIndex.rank_sites` (the whole-archive index path), where the SQL
  `HAVING COUNT(*) >= min_passes` clause stays a valid *superset* pre-filter
  (comparable depth is never above the raw count) and the true floor on
  `comparable_passes` is applied in Python before the re-rank — so CLI
  (`umbra sites`), agent tools (`find_repeat_sites`), the hosted API
  (`GET`/`POST /sites`) and the featured gallery cannot disagree about who
  qualifies. `umbra sites`' empty-result message names the depth it measured
  (`No site … has 3+ comparable passes` under comparable ranking). This closes the
  discovery moat's last raw-count-vs-analysable-depth gap: the comparable series is
  now what the answer *ranks by* and *qualifies on*, not only what it reports.
- **The workflow drift check now covers `python -c` bodies, not just `umbra …`
  invocations (`tests/test_workflows.py`, `docs/TODO.md`).** `tests/test_workflows.py`
  already parsed every `umbra …` command in `.github/workflows/*.yml` against the
  real Click tree — the guard that would have caught the `--db`/`--index-db` typo
  that killed the first two `Publish catalog index` runs. But the same publish
  pipeline also drives the library from Python: the tiling step ends with
  `python -c "import umbra_py.pmtiles as p, umbra_py.constants as c;
  p.save_viewer(c.CATALOG_INDEX_PMTILES_URL, …)"`, referencing two library
  symbols *by name* that the Click parse (which only sees `umbra` argv) cannot
  see — so a rename of `save_viewer` or a move of `CATALOG_INDEX_PMTILES_URL`
  would break the weekly run silently, the exact failure mode the suite exists to
  prevent, one interpreter over. The suite now extracts every `python -c` body
  (`_python_snippets`, quote-aware so a snippet's own `;` and `|` are not
  mistaken for shell operators the way the CLI scan's separator split would),
  compiles it (a syntax error is drift too), and resolves every name it reads
  from `umbra_py` against the installed package (`_umbra_name_errors` /
  `_umbra_aliases`): an `import umbra_py.x` that no longer resolves, a `from
  umbra_py.x import y` whose `y` is gone, and a `p.save_viewer` whose attribute
  was renamed all fail a pull request. It stays offline and lean like the rest of
  the suite — it imports only the `umbra_py` modules the snippets actually name
  (all stdlib-only today), and an import that fails for want of an *optional*
  dependency is treated as an absent extra in the core `[dev]` test job rather
  than as drift, told apart by which module `ModuleNotFoundError` reports missing
  (the `umbra_py` one that was named → drift; a third-party package underneath →
  absent extra). `test_a_renamed_library_symbol_would_be_caught` pins the
  `save_viewer` rename the way `test_the_drift_that_broke_the_publish_would_be_caught`
  pins the `--db` typo, and `test_the_scan_actually_found_the_python_snippets`
  guards against a scanner that silently matches nothing.
- **`umbra chips --clip-bbox` now reports what the clip read across the batch
  (`chips.py`, `cli/process.py`, `docs/schemas/chip-dataset.schema.json`,
  `tests/test_chips.py`, `tests/test_schemas.py`).** `umbra convert --clip-bbox`
  already prices its clip (the entry below), but the chipper — the loader that
  turns a *site's* passes into a training set, which is where clipping to an area
  of interest is the normal case rather than the exception — reported only the
  chips it wrote. A clipped run now rolls up what each acquisition read against the
  pixels its whole product holds onto `ChipDataset.clip` (a new `ClipSummary`), the
  batch form of the `clipped` line the single conversion prints: total window vs
  total scene pixels, the overall fraction, and the per-scene `min`/`max` fraction
  so an evenly-clipped run reads apart from one that read most of some passes and
  little of others. `umbra chips` prints it (`clipped: read 480,000 of 4,000,000
  scene px across 12 scene(s) (12.0%)`) and `--json` carries a `clip` block,
  published as a new conditional key on `chip-dataset.schema.json` — present only
  when the run was clipped, so an ordinary run's payload is unchanged. Counted per
  acquisition like the noise and speckle roll-ups, but *accumulated* during the run
  rather than derived from the records: the clip saving is deliberately neither a
  `ChipRecord` field nor a `UMBRA_*` tag (a clip changes which ground is written,
  which the transform already states, not what a pixel value means), so there is
  nothing in the manifest to derive it from. It reaches both loader paths through
  one new `chip_item(clip_report=…)` callback mirroring `sicd_to_geocoded_cog`'s: on
  a published `GEC`/`CSI` it is priced in `chip_item` from the tile window against
  the source raster's own size; on a `SICD` the callback rides down through the
  default `_prepare_sicd` into the conversion, which is what knows the whole-scene
  size the already-clipped COG no longer carries (a custom `preparer` or a
  `--work-dir` cache hit reports nothing, since no conversion runs to price). Tests
  pin the amplitude path pricing a quarter-scene read, the callback staying silent
  without a `bbox`, the SICD callback threading through `_prepare_sicd`, the run
  roll-up and its absence on an unclipped run, the `ClipSummary` arithmetic, and the
  `clip` block validating against the schema from a real clipped run.
- **`umbra convert --clip-bbox` now reports what the clip saved (`convert.py`,
  `cli/process.py`, `tests/test_convert.py`).** `--clip-bbox` turns a ground
  rectangle into the image window covering it and reads only that window, so the
  scene-sized amplitude array, the warp over it and the scene-sized output on disk
  never exist — but the command reported only what it *wrote*, so the value of the
  flag was invisible at the moment someone is deciding whether to use it. It now
  prints a `clipped` line pricing the pixels read against the pixels the whole
  product holds and the ratio (`read 480,000 of 4,000,000 scene px (12.0%)`). The
  number is *not* recorded in the output's `UMBRA_*` tags — a clip changes which
  ground is written, which the geotransform already states, not what a pixel value
  means, so tagging it would make a clipped and an unclipped conversion of one site
  disagree on a provenance key for no measurement reason — so it comes from a new
  non-breaking `clip_report` callback on `sicd_to_geocoded_cog` (invoked once,
  before the read, with a `ClipSavings` frozen dataclass; a caller who does not
  pass it sees no behaviour change) rather than being read back from the file, the
  way the noise and speckle reports are. This is the processing saving, not the
  bytes fetched: the download is whole-product either way, a slant-plane NITF
  having no map grid to range-read. Tests pin the callback firing exactly once with
  figures matching the window the reader was actually asked for, not firing at all
  on a whole-scene conversion, the pure `ClipSavings` arithmetic (including an
  empty scene reporting `0` rather than dividing by zero), and the `clipped` line
  reaching the CLI output.
- **`SiteCoverage.comparable_polarizations` — name *which* polarization the
  analysable change series is, on every discovery surface at once (`coverage.py`,
  `cli/discover.py`, `mcp_server.py`, `docs/schemas/site-coverage.schema.json`,
  `tests/test_coverage.py`, `tests/test_schemas.py`).** The comparable-figure
  workstream gave every raw coverage figure an analysable-series twin — how *deep*
  the differenceable series is (`comparable_passes`), how *long*
  (`comparable_span_days`), its *cadence* (`comparable_min`/`median`/`max_revisit_days`)
  and *which passes* (`comparable_hrefs`) — but never said *what* that series is.
  The comparable group is the largest set of dated passes sharing one polarization
  (the pool `select_change_frames` draws from before the mixed-polarization refusal
  every analysis verb enforces), yet the discovery answer named only
  `polarizations`, the union across the *whole* site — so a reader who ranked by
  `comparable` and got "3 usable passes" could not tell whether the analysable
  series was VV or HH without opening `comparable_hrefs` and reading a URL.
  `comparable_polarizations` is the group's own shared signature, taken from the
  group `_largest_comparable_group` already selects (so the name and the passes it
  names cannot disagree): a strict subset of `polarizations` when the site is mixed
  — the one signature the whole comparable depth/span/cadence is measured over, and
  the one a `--pol`-style filter would keep to reproduce `comparable_hrefs` — and
  equal to `polarizations` when every dated pass already shares one signature. It
  is an empty tuple both when the comparable group carries no polarization metadata
  (the empty-signature group, exactly as `polarizations` is empty then) and when no
  pass is dated at all (`comparable_passes` is 0, so there is no group to name), the
  two told apart by `comparable_passes` — an array (possibly empty), never null,
  like `polarizations`, since a signature is a set that can be empty rather than a
  scalar that can be absent. It reaches every discovery surface through the
  single-sourced `SiteCoverage.to_dict()` with no per-surface plumbing — `umbra
  sites --json`, `find_repeat_sites` (MCP / LangChain / LlamaIndex), and
  `GET`/`POST /sites` on `umbra serve` (via the committed `site-coverage` contract,
  which gains it as a required array) — and the human-readable `umbra sites` output
  names it inline: the `pol` line reads `HH, VV (usable: VV)` when the site spans
  more than one signature, silent when it does not. **With it every raw coverage
  figure's analysable twin is complete not just in magnitude but in identity: the
  discovery answer says how deep, how long, how often, which passes *and which
  polarization* the change series a verb can actually difference is.** Tests pin it
  naming the usable subset when the site is mixed, equal to `polarizations` under
  one signature, the whole dual-pol signature when the passes share one, empty for
  no-metadata and for nothing-dated (told apart by `comparable_passes`), and
  validating from the CLI against the committed schema.
- **`rank_by="passes" | "comparable"` — rank the repeat-imaged-site discovery
  answer by *analysable* depth, on every discovery surface at once (`coverage.py`,
  `showcase.py`, `index.py`, `cli/discover.py`, `mcp_server.py`, `serve.py`,
  `tests/test_coverage.py`, `tests/test_showcase.py`, `tests/test_index.py`,
  `tests/test_mcp_server.py`, `tests/test_serve.py`).** The comparable-figure
  workstream gave every raw coverage figure an analysable-series twin in the
  *report* — `comparable_passes` is how many of a site's passes a change verb can
  actually difference (the largest single-polarization dated subset), which the
  raw pass count overstates whenever a site's passes span several polarizations or
  carry undated ones. But the *ranking* still ordered by raw pass count
  everywhere, so the discovery moat (`STRATEGY.md` §3) could surface a
  broad-but-mixed site above a deeper single-polarization series a change run would
  actually prefer — and, at a small `top`, could crowd the deeper series off the
  list entirely. `rank_by="comparable"` orders by that differenceable depth
  instead: a site with three same-polarization passes now outranks one with five
  passes split across three polarizations, because three is the change series the
  first supports and two is the second's. The default stays `"passes"` (the raw
  count the static showcase's featured gallery wants — more acquisitions to
  precompute, whatever their polarization mix), so nothing shipped changes silently;
  the two coincide exactly when every dated pass of every site shares one
  polarization. It reaches every surface as one forwarded argument — `umbra sites
  --rank-by {passes,comparable}`, `find_repeat_sites(rank_by=…)` (MCP / LangChain /
  LlamaIndex), `GET`/`POST /sites?rank_by=…` on `umbra serve`, and
  `CatalogIndex.rank_sites(rank_by=…)` under `umbra sites --local` — single-sourced
  through `select_featured_sites` and one shared `coverage._rank_sort_key`, so no
  two surfaces can order a comparable ranking differently. The key is applied
  *before* the `top` truncation (and, on the whole-archive index path, the
  candidate SQL's raw-count `LIMIT` is dropped for the comparable ranking), so a
  deeply-analysable site outside the raw top-`top` is promoted rather than lost —
  the same whole-archive-not-a-capped-pool correction the discovery moat already
  made for raw depth, now for analysable depth. An unknown ranking is a
  self-describing `ValueError` (a `400` on the HTTP surface) naming the accepted
  set, checked once in `coverage._check_ranking` that every surface shares. No
  schema change — the `SiteCoverage` payload is unchanged; only its order is.
- **`SiteCoverage.comparable_min_revisit_days` / `comparable_median_revisit_days`
  — the tightest and typical revisit of the *analysable* change series, completing
  the comparable cadence on every discovery surface at once (`coverage.py`,
  `cli/discover.py`, `mcp_server.py`, `docs/schemas/site-coverage.schema.json`,
  `tests/test_coverage.py`, `tests/test_schemas.py`).**
  `comparable_max_revisit_days` gave the discovery answer's *worst* gap an honest
  twin — measured over the largest single-polarization dated subset a `change` /
  `timescan` / `stack` / `pick_change_interval` run can actually difference, not
  over the whole dated range an off-polarization pass distorts — but the *shortest*
  and *typical* gaps (`min` / `median_revisit_days`) were still quoted over every
  dated pass, so two thirds of the revisit line a discovery answer prints could
  still mislead in exactly the way the max twin was added to fix. A lone
  cross-polarization pass landing between two same-polarization ones makes the raw
  shortest gap read *tighter* than the differenceable series ever is (a VV series
  imaged days 1 and 20 with an HH pass on day 10 reads a 10-day shortest gap where
  the VV change run faces a real 19-day one), and the typical figure drifts
  whenever off-series passes are woven through the cadence. `comparable_min_revisit_days`
  and `comparable_median_revisit_days` report the honest numbers: the shortest and
  median gaps between consecutive passes of the `comparable_passes` group — the
  same pool `select_change_frames` draws from and the passes `comparable_hrefs`
  hands onward — so the whole revisit cadence (shortest, typical, worst) can be
  read over the series a change run actually has. Each equals its raw counterpart
  when every dated pass shares one polarization, and each is `null` with fewer than
  two comparable passes, exactly as `min_revisit_days` / `median_revisit_days` are
  with fewer than two dated ones. Single-sourced from the same `comparable_gaps`
  (over `_largest_comparable_group`) that already backs `comparable_max_revisit_days`,
  so the shortest, typical and worst comparable gaps cannot disagree about which
  passes the comparable series is. Added to the single-sourced
  `SiteCoverage.to_dict()`, so they reach all four discovery surfaces with no drift
  — `umbra sites --json`, `find_repeat_sites` (MCP / LangChain / LlamaIndex), and
  `GET`/`POST /sites` on `umbra serve` (via the committed `site-coverage` contract,
  which gains each as a required nullable number). **With the three, every raw
  coverage figure — `passes`, `hrefs`, `span_days` and the full min/median/max
  revisit cadence — now has an analysable-series twin, closing the comparable-figure
  workstream.** Tests pin each twin diverging from its raw counterpart when
  off-series passes distort the raw cadence, equal under one polarization, null
  with fewer than two comparable passes, and validating from the CLI against the
  schema.
- **`SiteCoverage.comparable_max_revisit_days` — the worst-case revisit of the
  *analysable* change series, on every discovery surface at once (`coverage.py`,
  `cli/discover.py`, `mcp_server.py`,
  `docs/schemas/site-coverage.schema.json`, `tests/test_coverage.py`,
  `tests/test_schemas.py`).** `comparable_span_days` gave the discovery answer's
  temporal *reach* an honest twin — the window the differenceable series covers,
  not the whole dated range — but the *cadence* figures (`min` / `median` /
  `max_revisit_days`) were still measured over every dated pass, so the one that
  most decides whether a series is worth analysing could mislead in both
  directions. `max_revisit_days` is the widest stretch a change could have gone
  unseen; measured over all dated passes it counts gaps a change run cannot
  actually use. A cross-polarization pass landing inside a gap of a
  single-polarization series makes the raw cadence read *tighter* than the series
  is — a VV series imaged Jan 1 and Jan 20 with a lone HH pass on Jan 10 reads a
  10-day longest gap, when the VV change run faces a real 19-day one — while a
  wide gap between off-polarization passes, irrelevant to that run, can *inflate*
  the raw figure past anything in the comparable series. `comparable_max_revisit_days`
  reports the honest number: the longest gap between two consecutive passes of the
  `comparable_passes` group — the largest set of dated passes sharing one
  polarization, the exact pool `select_change_frames` draws from and the passes
  `comparable_hrefs` hands onward — so the worst-case revisit quoted is the one the
  series a discovery answer feeds to `umbra change` / `stack` /
  `pick_change_interval` actually has. It is the cadence counterpart of
  `comparable_span_days`: equal to `max_revisit_days` when every dated pass shares
  one polarization, and `null` with fewer than two comparable passes exactly as
  `max_revisit_days` is with fewer than two dated ones. Single-sourced from the
  same `_largest_comparable_group` that already backs `comparable_passes` /
  `comparable_hrefs` / `comparable_span_days`, so the count, the URLs, the span
  and now the cadence cannot disagree about which passes the comparable series is.
  Added to the single-sourced `SiteCoverage.to_dict()`, so it reaches all four
  discovery surfaces with no drift — `umbra sites` (the revisit line gains an
  `(Nd across the usable series)` note, shown only when the comparable series' own
  longest gap differs from the all-passes one so a clean single-pol site stays a
  three-number line), `find_repeat_sites` (MCP / LangChain / LlamaIndex), and
  `GET`/`POST /sites` on `umbra serve` (via the committed `site-coverage`
  contract, which gains the field as a required nullable number). Tests pin it
  wider than the raw max when a cross-polarization pass fills a gap, narrower when
  an off-series gap inflates the raw figure, equal under one polarization, null
  with fewer than two comparable passes, and validating from the CLI against the
  schema.
- **`SiteCoverage.comparable_span_days` — how long the *analysable* change series
  actually runs, on every discovery surface at once (`coverage.py`,
  `cli/discover.py`, `docs/schemas/site-coverage.schema.json`,
  `tests/test_coverage.py`, `tests/test_schemas.py`).** `comparable_passes` fixed
  the *count* a discovery answer reports — the raw `passes` overstates depth
  whenever a site's passes span more than one polarization — but the *temporal*
  fields (`span_days` and the revisit figures) were still measured over every
  dated pass, so a site could report a long, roomy span borrowed from passes no
  analysis verb can difference against the rest. A site imaged Jan→Aug in HH but
  only Jan→Jun in VV has a VV change series that covers five months, not eight;
  `span_days` said eight. `comparable_span_days` reports the honest window: whole
  days spanned by the `comparable_passes` group — the largest set of dated passes
  sharing one polarization, the exact pool `select_change_frames` draws from — so
  the discovery answer's temporal reach matches the series the analysis verb it
  hands off to can build. It is the temporal twin of `comparable_passes`
  undercutting `passes`: below `span_days` means off-polarization or undated passes
  stretch the full range past the analysable window, equal to `span_days` when
  every dated pass shares one polarization, and `null` with fewer than two
  comparable passes exactly as `span_days` is with fewer than two dated ones. It is
  single-sourced from the one `_largest_comparable_group` that already backs
  `comparable_passes` / `comparable_hrefs`, so the count, the URLs and the span
  cannot disagree about which passes the comparable series is. Added to the
  single-sourced `SiteCoverage.to_dict()`, so it reaches all four discovery
  surfaces with no drift — `umbra sites` (the pass line's `usable` clause gains an
  `over Nd` note when the comparable subset covers a narrower window than the whole
  range, shown only when it undercuts so a clean single-pol site stays a one-number
  line), `find_repeat_sites` (MCP / LangChain / LlamaIndex), and `GET`/`POST /sites`
  on `umbra serve` (via the committed `site-coverage` contract, which gains the
  field as a required nullable integer). Tests pin it below the raw span on a
  bracketed mixed-polarization site, equal to it under one polarization, null with
  fewer than two comparable passes, and validating from the CLI against the schema.
- **`SiteCoverage.comparable_hrefs` — the pass URLs of a site's usable subset,
  ready to hand an analysis verb straight through, on every discovery surface at
  once (`coverage.py`, `cli/discover.py`,
  `docs/schemas/site-coverage.schema.json`, `tests/test_coverage.py`,
  `tests/test_schemas.py`).** `comparable_passes` reports *how deep* a site's
  analysable change series is, but the discovery answer still handed onward only
  `hrefs` — *every* pass. Following that answer into `umbra change` / `stack` /
  `pick_change_interval` therefore trips the very mixed-polarization refusal
  `comparable_passes` measures: a site reporting `comparable_passes: 3` beside four
  `hrefs` (three VV, one HH) is a trap, since the four URLs it offers are exactly
  the selection those verbs reject. `comparable_hrefs` closes that gap — it is the
  subset of `hrefs` belonging to the `comparable_passes` group (the largest set of
  dated passes sharing one polarization, oldest-first), so the discovery → analysis
  chain (`find_repeat_sites → pick_change_interval`, `GET /sites → POST
  /artifacts/stats`) can pipe a selection straight through that cannot be refused.
  Where `hrefs` is the whole roster to choose from, `comparable_hrefs` is the
  choice already made. It is single-sourced with `comparable_passes`: both derive
  from one `_largest_comparable_group` (now returning the group's passes, ties
  broken by the polarization tuple exactly as `select_change_frames` breaks them),
  so `len(comparable_hrefs)` equals `comparable_passes` and the count and the URLs
  cannot disagree. Added to the single-sourced `SiteCoverage.to_dict()`, so it
  reaches all four discovery surfaces with no drift — `umbra sites --json`,
  `find_repeat_sites` (MCP / LangChain / LlamaIndex), and `GET`/`POST /sites` on
  `umbra serve` (via the committed `site-coverage` contract, which gains the field
  as a required array). Tests pin the subset to the comparable group's URLs
  oldest-first, equal to `hrefs` under one polarization, dropping the undated and
  the off-polarization passes `hrefs` keeps, and empty when nothing is dated.
- **`SiteCoverage.comparable_passes` — how many of a repeat-imaged site's passes
  can actually be differenced together, on every discovery surface at once
  (`coverage.py`, `cli/discover.py`, `docs/schemas/site-coverage.schema.json`,
  `tests/test_coverage.py`, `tests/test_schemas.py`).** The discovery ranking
  counts every acquisition (`passes`), but every analysis verb the answer feeds —
  `change` / `timescan` / `stack`, `stack_stats`, `change --narrate` — refuses a
  *mixed-polarization* selection (HH and VV measure different scattering), and an
  undated pass cannot be ordered onto a time axis at all. So a raw pass count
  overstates a site's *analysable* depth whenever its passes span more than one
  polarization or some are undated. `comparable_passes` reports the honest figure:
  the largest set of dated passes sharing one polarization — the exact pool
  `viz.composites.select_change_frames` draws from before that refusal bites, so
  the discovery answer and the verb it hands off to cannot disagree about how deep
  a change series the site supports. It turns the existing `polarizations` field
  from a *warning* ("more than one means not all comparable") into an actionable
  *count*, which is the difference between knowing a site is mixed and knowing how
  many passes survive the mix. Being a pure function of the passes it stays out of
  the nullable-cadence family: it is always an integer (0 when nothing is dated,
  equal to the dated-pass count under a single polarization), added to the
  single-sourced `SiteCoverage.to_dict()` so it reaches all four discovery
  surfaces with no drift — `umbra sites` (a `… usable` clause on the pass line,
  shown only when it undercuts the raw count so a clean single-pol site stays a
  one-number line), `find_repeat_sites` (MCP / LangChain / LlamaIndex), and
  `GET`/`POST /sites` on `umbra serve` (via the committed `site-coverage` contract,
  which gains the field as a required non-null integer). Tests pin a mixed-pol
  site's comparable depth below its pass count, a single-pol site's equal to it,
  and undated passes excluded.
- **`SiteCoverage.max_revisit_days` — the longest gap in a repeat-imaged site's
  coverage, on every discovery surface at once (`coverage.py`, `cli/discover.py`,
  `mcp_server.py`, `docs/schemas/site-coverage.schema.json`,
  `tests/test_coverage.py`, `tests/test_schemas.py`).** The discovery answer
  already reported a site's *shortest* and *typical* revisit
  (`min_revisit_days` / `median_revisit_days`) but not its *longest* — and the
  longest gap is the one figure that decides a site's worst-case temporal
  resolution: the widest stretch in which a change could have happened unseen.
  Read beside the median it also separates a site imaged on a steady cadence
  (`max` near the median) from a bursty or thinning one (`max` far above it) —
  which two sites with the same pass count and the same median can otherwise hide.
  That thinning is exactly where the open archive stops answering and on-demand
  tasking is the pitch (`STRATEGY.md` §1), so surfacing it shortens the path from
  free data to a tasking decision. It completes the min/median/max revisit trio on
  the single-sourced `SiteCoverage.to_dict()`, so it reaches all four discovery
  surfaces with no drift — `umbra sites` (a new `… longest gap` field on the
  revisit line), `find_repeat_sites` (MCP / LangChain / LlamaIndex), and
  `GET`/`POST /sites` on `umbra serve` (via the committed `site-coverage` contract,
  which gains the field as a required nullable — null with fewer than two dated
  passes, exactly like the other two). A test pins that two sites with identical
  pass counts and medians are told apart only by the new field.
- **`POST /sites` — the GeoJSON-body twin of `GET /sites`, so the discovery route
  takes an area-of-interest polygon as an object (`serve.py`,
  `tests/test_serve.py`).** `GET /sites` ranks the archive's most repeat-imaged
  sites but, like `GET /search`, has to take `intersects` as a JSON-*string* query
  param — awkward for a real polygon. `POST /sites` mirrors the `GET`/`POST
  /search` pair exactly: same `run_sites` ranking, same `site-coverage` records,
  same filters, but the body carries `intersects` as a GeoJSON object and the SAR
  filters as top-level fields *or* a STAC `query` object (a top-level field
  overrides the same field in `query`, identical to `POST /search`). `limit` sizes
  the pool only on a `--live` backend and `top` / `min_passes` cap and qualify the
  ranking as on `GET`; a new `_opt_int` coerces the body's paging/ranking fields so
  a malformed `top`/`limit` is a `400` rather than a silent truncation. The route
  is advertised on the landing page (a second `sites` link with `method: POST`),
  references the committed `site-coverage` contract in the generated OpenAPI
  document alongside the `GET`, and is documented on the README and in `llms.txt`.
  This completes the `GET`/`POST` symmetry `/search` already had for the one
  discovery route that lacked it.
- **`find_repeat_sites` ranks whole-archive on the local index — the discovery
  moat's whole-archive ranking now reaches the *last* surface (`mcp_server.py`,
  `tests/test_mcp_server.py`).** `CatalogIndex.rank_sites` had already made
  `umbra sites --local` (CLI) and `GET /sites` (hosted API) measure a site's depth
  across the *entire* index — one `GROUP BY task`, no pool cap — but the agent
  tool (`find_repeat_sites`, shared across the MCP / LangChain / LlamaIndex
  surfaces) still re-listed a `limit`-capped `search` even when its `local`
  backend was an index, so a deeply-imaged site whose passes fell just outside the
  first `limit` rows read as shallower than it is. It now routes an index backend
  through `CatalogIndex.rank_sites` — the same drop-in `umbra sites --local` and
  `GET /sites` use, one surface further out — so all four surfaces (CLI, hosted
  API, the static showcase's featured gallery, and the agent tools) rank the same
  whole-archive way and cannot disagree about a deep site's depth. `limit` now
  sizes only the live/`--token` pool (a live catalog or the Canopy archive has no
  index to `GROUP BY` over); the docstring says so. Two tests pin it: a deep site
  whose passes fall outside a `limit`-sized pool is still ranked by all of them,
  and every filter (`bbox` / `intersects` / `area` / `fuzzy` / `product_types` /
  the SAR properties, plus `top` / `min_passes`) forwards to `rank_sites` with no
  `limit` re-list. **This closes the discovery moat's one remaining whole-archive
  ranking gap (`STRATEGY.md` §8): every surface now measures depth over the whole
  index rather than a capped pool.**
- **`CatalogIndex.rank_sites` — whole-archive, index-native site ranking, so
  `umbra sites --local` measures a site's depth across the *entire* index
  (`index.py`, `cli/discover.py`, `tests/test_index.py`, `tests/test_coverage.py`).**
  `umbra sites` ranks the archive's most repeat-imaged sites — the discovery step
  before `change` / `timescan` / `stack` — but `rank_site_coverage` ranks whatever
  pool it is handed, so `--local` capped that pool at `--limit` acquisitions and a
  site with many passes just *outside* the first `--limit` rows read as shallower
  than it is (worse: the pool is ordered by `(task, acq_date)`, so the cap
  admitted alphabetically-early tasks and dropped the rest). Umbra files every
  pass of a site under one task, so a site's depth is a `GROUP BY task` the index
  can answer over its whole contents — no pool cap. `rank_sites` does exactly
  that: it counts each site's dated passes directly in SQL for the
  SQL-expressible filters (`bbox` / `start` / `end` / `area` / `fuzzy` /
  `product_types`), then loads only the top tasks' documents to summarise — cheap
  even whole-archive. The polygon (`intersects`) and acquisition-property
  (`polarizations`, incidence, resolution) filters run per item in Python (as in
  `search`), so when any is set it ranks the full *uncapped* matching stream
  instead — still whole-archive, identical to the pool path, just without the cap.
  The ranking is `select_featured_sites`' own (dated passes per task, most first,
  task name breaking ties, `min_passes` qualifying), single-sourced so `umbra
  sites`, `find_repeat_sites`, the featured gallery and now the deep local path
  cannot disagree about what "most repeat-imaged" means; each site is summarised
  by the same `site_coverage`, so a `--local` record is byte-identical to a live
  one bar its depth. `umbra sites --local` (and `--index-db`) now routes here, so
  `--limit` is a live-/`--token`-path pool size only and is documented as such.
  A test pins the deep path exactly against the uncapped-pool ranking for every
  filter, so the two answers cannot drift. **This closes the discovery moat's one
  remaining `--local` gap (`STRATEGY.md` §8): the whole-archive "deepest series"
  answer no longer waits behind a pool cap.**
- **`GET /sites` — the repeat-imaged-site discovery step, now on the `umbra
  serve` STAC API (`serve.py`, `tests/test_serve.py`).** `umbra sites` (CLI) and
  `find_repeat_sites` (MCP / LangChain / LlamaIndex) already answer *which* site
  has a time series worth analysing, but the hosted HTTP surface — the one built
  so a client can query the archive with **zero install** — could only list
  acquisitions, not rank sites. So the deterministic scan → analyse chain the
  analysis routes assume (`POST /artifacts/stats` / `change` measure *what*
  changed, given the passes) still started one step short over HTTP: nothing told
  a client *which* passes to send. `GET /sites` closes that gap. It takes the same
  `bbox` / `intersects` / `datetime` / `product_types` / `area` / `fuzzy` and SAR
  filters `GET /search` takes (`top` capping the answer and `min_passes` the
  qualifying depth), and ranks through the *same* `rank_site_coverage` selector
  `umbra sites`, `find_repeat_sites` and the static showcase's featured gallery
  use — single-sourced so no surface can disagree about what "most repeat-imaged"
  means. On an index the depth is measured **whole-archive** (via
  `CatalogIndex.rank_sites`' `GROUP BY task` over the entire catalog), so a deep
  site ranks by all its passes rather than by whatever a pool cap admitted;
  `limit` sizes the re-listed pool only on a `--live` backend, which has no index
  to group over. Each returned site is a `site-coverage`
  record (passes, date span, revisit cadence, union footprint, products,
  polarizations, and the pass `hrefs` **oldest-first**, ready to send straight to
  `POST /artifacts/stats` / `change`), and the response's items reference the
  committed `site-coverage.schema.json` in the generated OpenAPI document — the
  contract re-homed, not restated, so an OpenAPI-driven client reads the same
  shape the CLI and agent tools emit. The route is always mounted (pure search +
  ranking, no `viz`/`load` extra) and advertised on the landing page's `sites`
  link. No model is called: a number ranks the sites (`STRATEGY.md` §7's
  determinism boundary applied to discovery). **This puts the discovery moat
  (`STRATEGY.md` §3) on every surface — CLI, agent tools, and now HTTP.**
- **`find_repeat_sites` — the repeat-imaged-site discovery step, now on the agent
  surfaces (`mcp_server.py`, `langchain.py`, `llamaindex.py`, and the
  `site-coverage` schema).** `umbra sites` ranks the archive's most
  repeat-imaged sites — where change detection has something to measure — but the
  answer was CLI-only, so the deterministic scan → narrate chain the strategy
  describes (`pick_change_interval` → `narrate_change`) still started one step in:
  nothing told an agent *which* site to point those verbs at. `find_repeat_sites`
  closes that gap. It is a thin adapter over `rank_site_coverage` (the same
  selector the static showcase's featured gallery uses, single-sourced so the two
  cannot disagree about what "most repeat-imaged" means), gathering the pool with
  the same backend selection and geography/date/product/SAR filters
  `search_catalog` takes, and returning each site's coverage — passes, date span,
  revisit cadence, footprint, products, and the pass URLs **oldest-first**, ready
  to hand straight to `pick_change_interval`. So `find_repeat_sites →
  pick_change_interval → narrate_change` is a complete chain a model can drive
  with no site known in advance — the §3 discovery moat reached from every agent
  front door. It ships on all three (MCP, LangChain, LlamaIndex) as the *same*
  callable, so the surfaces cannot drift, with the parity tests extended to keep
  it that way. The shape it emits — `SiteCoverage.to_dict()`, already the
  `umbra sites --json` contract — is now published as `site-coverage.schema.json`
  and validated against a real payload from both the CLI and the record, so
  `umbra sites --json` is no longer an unschema'd surface (design principle 5,
  "agents are users"). No model is called: a number ranks the sites (`STRATEGY.md`
  §7's determinism boundary applied to discovery).

### Fixed
- **Restore the dependency security audit's signal so a real CVE is not lost in
  weekly false red (`.github/workflows/security-audit.yml`).** The scheduled
  `pip-audit` job had failed three Mondays running (issue #149) for two reasons,
  neither a vulnerability in anything umbra-py ships or depends on. First,
  `pip-audit --strict` audits the *entire* installed environment, which includes
  the runner's own **pip** — and pip 24.0 carries six advisories (wheel/tar
  extraction path-traversal, `console_scripts` handling), all fixed in pip ≥
  26.1.2. pip is the installer, not a dependency umbra-py declares, and those CVEs
  cannot reach anyone importing the library; but they turned the audit red every
  week, so a genuine CVE landing in a real dependency would have shown the *same*
  red and gone unnoticed — a monitor that always fails monitors nothing. Second,
  `--strict` also fails on any distribution it cannot resolve to an advisory, and
  the job installs umbra-py itself with `-e`, so the project's own editable
  checkout — not on PyPI — was a second standing failure independent of the pip
  one. The job now (1) upgrades the environment's pip to current before auditing
  (`uv pip install --system --upgrade pip`), patching the ambient tooling rather
  than muting the scanner with `--ignore-vuln`, and (2) audits with
  `pip-audit --skip-editable` instead of `--strict`, which drops the local
  editable package from the scan while keeping every third-party dependency
  audited — a real CVE in one still fails the job, since pip-audit exits non-zero
  on any finding whether or not `--strict` is set (verified against a pinned
  vulnerable dependency). The audit is once again a trustworthy signal about the
  project's dependency tree, which is the state a clean `v0.1.0` release needs.
- **Type-check the all-extras surface in CI so a stub-bearing extra's misuse
  fails a PR instead of every agent session (`viz/composites.py`, `ci.yml`,
  `pyproject.toml`).** CI's `type-check` job installed only `[dev]`, so mypy
  import-ignored every optional library and never saw a misuse of one that
  *does* ship stubs. `viz/composites.py` called `Image.ADAPTIVE`, which Pillow's
  stubs place on the `Image.Palette` enum rather than the module, so it was
  invisible to CI (green) yet failed `[attr-defined]` on every environment with
  Pillow present — the SessionStart hook's, `test-all-extras`', and every remote
  coding-agent session, each of which opened with one failing `mypy` line. The
  call site now uses `Image.Palette.ADAPTIVE` (the real typed attribute Pillow's
  own internals reference, runtime-identical, and clean in *both* environments —
  a `cast`/`# type: ignore` would have been redundant in whichever install made
  it so, tripping `warn_unused_ignores`/`warn_redundant_casts`), and a new
  `type-check-all-extras` CI job runs mypy with all extras installed (at the
  3.12 target numpy's PEP 695 stubs require; the core `type-check` job keeps
  umbra-py's own annotations checked at the real 3.10 floor) — the type-check
  mirror of `test-all-extras` — so this class of drift is caught at the root
  rather than rediscovered by the next agent.
- **Retry a model endpoint's transient failures and surface its real error
  message (`_http.py`, `describe.py`, `planner.py`).** The model POST was a plain
  request that raised only on HTTP ≥ 400 and reported nothing but the status.
  That missed the failure that skipped one featured narration: a gateway like
  OpenRouter returns **HTTP 200 with an error body** (not a completion) when an
  upstream provider hiccups, so the reply had no `choices` and the code raised a
  bare "Unexpected OpenAI response shape: 'choices'", discarding the provider's
  actual message. Both the describe/narrate and the ask paths now go through a
  shared `post_model_json` that (1) retries a dropped connection, an HTTP 429/5xx,
  and the HTTP-200-with-error-body case a few times with backoff — riding out a
  one-off blip that a lone narration would otherwise lose — and (2) raises with
  the provider's own words (`"...returned an error: <message>"`) so a persistent
  failure says *why* in the build log. A genuine verdict (400 bad request, 401
  bad key, 402 out of credit) is still raised at once, not retried.
- **Bound the completion on the OpenAI-compatible model requests so an
  OpenRouter key isn't refused (`describe.py`, `planner.py`).** The
  OpenAI-compatible path (`umbra describe`, `umbra change --narrate`,
  `umbra serve --narrate`, `umbra ask`) sent no `max_tokens`, so a gateway that
  reserves the model's whole output budget against the key's credit limit —
  OpenRouter does — returned `HTTP 402` ("you requested up to 16384 tokens but
  can only afford …") and every narration/description/plan failed, even though a
  reply is a short JSON of a few hundred tokens. Both requests now cap the
  completion at 1024, matching what the Anthropic path already sent (Anthropic
  requires the field). Surfaced by the first live `umbra showcase --narrate` run
  in CI: the featured composites rendered but every reading was skipped on a 402.

### Added
- **`umbra sites` — rank the archive's most repeat-imaged sites, the discovery
  step before every change/stack verb (`coverage.py`, `cli/discover.py`).**
  Every analysis verb this library ships — `umbra change`, `timescan`, `stack`,
  `change --narrate`, the `stack_stats` cube — begins with a question none of
  them answers: *which* site has a time series worth looking at? Umbra files
  every pass of an area under one task directory, so a site's coverage is just
  how many dated passes share it, and the best-covered are exactly where change
  detection has something to measure. The static showcase already picked them
  (`select_featured_sites`) for its featured gallery, but that ranking was
  invisible to anyone not building a showcase. `umbra sites` turns it into a
  first-class discovery answer: it ranks the sites and reports each one's
  coverage — pass count, date span, revisit cadence (shortest and typical gap),
  union footprint, products and polarizations — with `--json` adding the pass
  URLs oldest-first, ready to pipe straight into `umbra change` / `stack`. It is
  a full gather command (the shared geography, task-name and acquisition-property
  option groups; `--local` / `--token` / bbox / place / polygon / dates), so it
  joins `conftest.GATHER_COMMANDS` and the option-group parity suites cover it.
  The new `coverage.py` is pure and dependency-free (stdlib +
  `UmbraItem`, exercised entirely offline) and reuses the showcase's selector for
  the grouping, so the two surfaces cannot disagree about what "most
  repeat-imaged" means — only the per-site summary is new. See `docs/TODO.md` for
  the deferred agent-tool / `umbra serve` follow-ons.
- **Validate every published schema's `examples`, and give the agent-facing
  contracts a whole-document example to parse (`docs/schemas/*.schema.json`,
  `tests/test_schemas.py`).** `examples` is a JSON Schema *annotation* — a
  validator never checks its members — so an example that drifted from the shape
  it illustrates (an enum value renamed, a number turned string, a field a strict
  schema no longer allows) shipped as valid-looking documentation a consumer
  copying it would get wrong. `tests/test_schemas.py` now walks every schema in
  `docs/schemas/` and validates each `examples` entry against the subschema it
  sits on — at every depth, resolving `$defs` and cross-file `$ref`s through the
  same registry the payload checks use — so a schema's example is held to the
  same contract as the payloads it documents. Two self-tests keep the check
  honest (that it found a real corpus rather than validating nothing, and that a
  deliberately-drifted example is caught). The same change gives nine
  consumer-facing contracts (`error`, `download`, `index-info`, `render-manifest`,
  `render-job`, `task-matches`, `scene-matches`, `chip-skipped`,
  `stack-provenance`) a top-level whole-document `examples` entry — a complete,
  now-checked instance beside the field-by-field descriptions, so an agent or a
  script reading the contract has a concrete document to parse against, not only
  a shape to satisfy. Closes the last open item in the machine-readable-contracts
  group (design principle 5, "agents are users"). See `docs/schemas/README.md`.
- **Carry the missing pass's footprint into a chip run's `skipped.jsonl`
  sidecar, so a hole is located in space as well as in time (`chips.py`,
  `docs/schemas/chip-skipped.schema.json`).** `SkippedAcquisition` recorded
  *which* pass a run could not include and *when* it was acquired, but not
  *where* it was — so a training loader reading the sidecar could tell a time
  series had a hole but not whether the hole fell over the area of interest it
  cared about. A new `SkippedAcquisition.bbox` carries the acquisition's own
  footprint (`UmbraItem.bbox`, EPSG:4326 `[min_lon, min_lat, max_lon, max_lat]`),
  populated from the item that is already in hand at both routes to a skip — the
  conversion refusal (`--skip-unsupported`) and the preflight drop
  (`--preflight`) — so the record is described the same either way. That answers
  the natural question ("was this dropped pass over my site?") from the directory
  itself, without re-running the search that produced the selection — the
  self-describing-artifact bar the sidecar was written to (design principle 5,
  agents are users). It is a required-nullable field on
  `chip-skipped.schema.json` (null when the source item stated no footprint, so
  absence is a value a consumer reads rather than a missing key it must guess at),
  emitted as the four-number list a `ChipRecord.bbox` already is. Closes the
  batch-survivability follow-on in `TODO.md`.
- **Ride a structured polarization-caution block alongside a `change_composite`
  MCP picture when same-polarization could not be *verified* (`mcp_server.py`).**
  `_require_same_polarization` refuses a *visible* mix — two passes whose known
  polarizations differ, HH vs VV measuring different scattering — before any
  render. What it could not see is a pass that carries no `sar:polarizations`
  metadata at all: the composite rendered and the agent got a picture with no
  signal that whether every pass measured the same scattering was *unknown*, even
  though an HH-vs-VV mix there would read as false change. The tool now returns an
  advisory text block (`_polarization_advisory`) beside the image whenever a pass
  lacks the metadata — "N of M pass(es) carry no polarization metadata, so
  umbra-py could not verify they share one polarization" — so an agent handed the
  composite also sees why it may be suspect (design principle: images are the API
  — return the artifact *with its provenance*, and agents are users). A fully
  verified selection (every pass declaring one and the same polarization) is
  unchanged: image + caption, no caution. The caption, which carries the CC-BY
  attribution, stays last.
- **Carry the speckle detection floor into `stack_stats`'s spatial breakdown, so
  a `blocks=N` grid says which *block* stands clear of speckle — not only whether
  the cube does (`load.py`, `docs/schemas/stack-stats.schema.json`).** The
  detection floor (what interference alone would produce, PRs #192/#193) was
  scene-wide: it said whether the *cube* changed, while the flagship spatial
  breakdown ("which part of a site moved") gave each block a `changed_fraction`
  with no floor to weigh it against — so the very caveat that tells a reader to
  "read the spatial breakdown for a block where the change stands clear" pointed
  at a breakdown that could not answer. Now each block that had two comparable
  passes carries a `detection` sub-record: the cube-wide per-cell
  `false_alarm_fraction`, the block's own `compared_cells`, and whether its net
  `changed_fraction` `stands_clear` of the floor by the same
  `DETECTION_EXCESS_WARN` margin the cube-level advisory uses; and `peak_block`
  gains a `stands_clear`, so the headline mover carries its own verdict — the
  biggest block-mover can still sit inside the floor, which is exactly the case a
  reader needs told. The cell count travels *with* the flag on purpose: the floor
  is an exact per-cell expectation whatever a block's size, but a block is
  measured over far fewer cells than the whole scene, so its observed share
  scatters more widely around it — a bare `stands_clear` would invite reading a
  block's excess as a finding when it is sampling, so the block record and a new
  caveat both say to read `stands_clear` together with `compared_cells`. It is
  the same one `to_dict()` reaching every surface (`umbra stack --stats`,
  `POST /artifacts/stats`, the `stack_stats` agent tool) with no new request field
  or flag, and the strict schema gains `$defs/blockDetection` (validated against a
  speckled cube, since the constant-valued fixture reads `looks: null` and so
  carries no floor at any level). Both fields are present only when the cube
  carried a `detection` floor, so a cube too small to read looks is unchanged.
- **Harden the live narration endpoint (Mode B) for a public instance:
  per-client rate limiting and a curated allowlist (`serve.py`,
  `cli/explore.py`).** `POST /artifacts/narrate` is an *unauthenticated proxy
  over the operator's model budget*, and its only guards were a content-addressed
  cache and a single instance-wide daily cap — so one client could burst through
  the whole day's budget, and the endpoint could be pointed at any scene in the
  archive to run up spend. Both were named in `STRATEGY.md` §8 / `TODO.md` as
  what stands between the shipped local endpoint and a public one. This adds the
  two: `umbra serve --narrate-client-limit N` caps live model calls *per client*
  per UTC day (`ClientNarrationBudget`, keyed by `client_identity` — a bearer
  token when present, hashed rather than stored, else the peer address), checked
  before the global cap so a single caller cannot drain it and answering a `429`
  that names the per-client limit; and `umbra serve --narrate-allow-bbox
  min_lon,min_lat,max_lon,max_lat` bounds the endpoint to a curated area
  (`NarrationAllowlist`), refusing with `403` any scene whose footprint centroid
  falls outside it — before the cache, either budget or the model, and *failing
  closed* on a scene whose footprint is unknown. Both caps count only calls that
  reach the model, so a cache hit is spared, and both reset at UTC midnight like
  the global one. Neither is a request field — they are the instance's policy,
  like the model and its key — and all three (the daily cap, the per-client cap,
  the allowed bbox) are advertised on the landing page's `narrate` link under
  `umbra:options` (`narrate_capabilities`, mirroring the `stats` link's
  `stats_capabilities`), so a client reads an instance's spend policy from `/`
  rather than by tripping a `403`/`429`. Unbounded by default (opt-in like the
  endpoint itself), so an existing instance is unchanged.
- **Bake precomputed change narrations into the static showcase (Mode A):
  `umbra showcase --narrate` (`showcase.py`, `cli/explore.py`, `docs.yml`).**
  The VLM "what changed here?" reading existed only behind a live
  `umbra serve --narrate` (Mode B) — invisible to a visitor browsing the static
  GitHub Pages showcase, which calls no model (a key shipped to a browser is a
  published key). This adds the zero-exposure delivery: at **build time**, with
  the model key held in CI, `umbra showcase --narrate` narrates each featured
  `change` site and bakes the result into the page — a plain-language summary
  under the tile plus a `featured/<site>.narration.json` sidecar carrying the dB
  grid it cites — so the browsing user reads a cached narration with **no live
  model call and no key near the browser**.

  It narrates **the same two passes the composite shows** (`select_change_frames`
  picks them for both), so the picture and the words describe one pair. It reuses
  the exact narration Mode B ships (`umbra_py.narrate`), gated so a keyless build
  or a non-`change` view skips cleanly rather than failing the deploy: a narrator
  that returns nothing or errors leaves the tile untouched — the pictures are the
  showcase, the readings are the bonus. The bake is an injectable
  `featured_narrator` seam on `assemble_showcase` (like `featured_renderer`), so
  the whole feature is offline-testable with no model. `docs.yml` passes the
  repo's model-key secret (`OPENROUTER_API_KEY` / `ANTHROPIC_API_KEY` /
  `OPENAI_API_KEY`) to the main-only showcase build and runs it with `--narrate`;
  a fork PR (no secrets) simply ships the gallery without readings. Every baked
  narration keeps its CC-BY attribution and AI-provenance note, and the tile
  labels it "AI reading" and links the numbers, so a model's reading of radar is
  never mistaken for ground truth. See `STRATEGY.md` §8 (this closes the
  "bring the VLM to the browsing user" workstream — both modes now shipped).
- **Power the AI features with an OpenRouter API key: `OPENROUTER_API_KEY` is a
  first-class provider for `umbra describe`, `umbra change --narrate`,
  `umbra serve --narrate` and `umbra ask` (`constants.py`, `describe.py`,
  `narrate.py`, `planner.py`).** OpenRouter (https://openrouter.ai) is an
  OpenAI-compatible gateway — one key reaches many providers' models over the
  same `/chat/completions` shape (including `image_url` multimodal) umbra-py
  already speaks — so it needed no new HTTP client, only recognition: set
  `OPENROUTER_API_KEY` and every model-consulting feature routes to
  `https://openrouter.ai/api/v1` with a vision-capable default model
  (`openai/gpt-4o-mini`) and OpenRouter's two (optional) ranking headers. It was
  reachable before only by knowing to set `OPENAI_API_KEY` + `OPENAI_BASE_URL`
  by hand; now it is discoverable and named in every setup error and `--help`.

  It is checked **before** the generic `OPENAI_API_KEY` — because setting an
  OpenRouter key is an unambiguous opt-in, nobody sets it by accident — so it
  wins over a stray `OPENAI_API_KEY` in the environment, while a native
  `ANTHROPIC_API_KEY` still takes precedence. `OPENROUTER_BASE_URL` overrides the
  host, and the existing model overrides (`UMBRA_NARRATE_MODEL` /
  `UMBRA_DESCRIBE_MODEL` / `UMBRA_ASK_MODEL`, or `--model`) select an OpenRouter
  model like `anthropic/claude-3.5-sonnet`. The determinism boundary is
  unchanged: OpenRouter is only ever the model at the interpretive edge; every
  narration/description still carries the CC-BY attribution and the AI-provenance
  note, and no measurement comes from the model.
- **Expose the change-interval selector on the CLI and the agent tools:
  `umbra stack --pick-interval` and the `pick_change_interval` MCP / LangChain /
  LlamaIndex tool (`cli/process.py`, `mcp_server.py`, `langchain.py`,
  `llamaindex.py`).** `best_change_interval` — the deterministic "which two of a
  site's passes is the change worth looking at between?" scan — was library-only,
  so the scan → narrate chain existed whole only behind a hosted `umbra serve
  --narrate`. This brings its first half to the two front doors built so nobody
  has to stand up a server: a shell (`umbra stack --pick-interval`) and a
  model (`pick_change_interval`) can now scan a whole series and get back the one
  consecutive pass-pair whose measured change stands **furthest clear of the
  speckle detection floor**, with the two STAC URLs ready to hand straight to
  `umbra change --narrate` / the `narrate_change` tool. A number picks the
  frames, never the model (`STRATEGY.md` §7's determinism boundary applied to
  frame selection), so the choice is reproducible and safe to quote.

  Both surfaces are thin adapters over the same `best_change_interval`, so there
  is no new selection logic and the three ways to ask it (server, shell, agent)
  cannot drift. `--pick-interval` is its own mode — it reduces the cube to one
  answer rather than writing it or its whole statistics, so it does not pair with
  `--out` / `--stats` / `--provenance`, the way `--provenance` already stands
  alone — and it defaults the grid to the site's UTM zone (like the
  `stack_stats` tool) so the change fractions the pick is made on weigh equal
  ground. When the series' own largest change is still inside the speckle floor
  the pair is still offered, with `stands_clear: false` and a warning that the
  difference may be interference rather than change; a series with fewer than two
  comparable passes or no measured change returns no pair rather than a false
  one. The agent tool refuses a mixed-polarization or single-pass selection like
  its `stack_stats` sibling. See `TODO.md` (the VLM-in-the-store workstream, where
  the deferred internet-facing hardening still lives).
- **Serve change narration over HTTP, opt-in and guarded: `POST
  /artifacts/narrate` on `umbra serve --narrate` (`serve.py`,
  `load.select_change_interval` / `load.best_change_interval`).** The two
  model-in-the-loop capabilities — *narrate the change between two passes*
  (`umbra change --narrate`) and *scan a site's whole series to find the pair
  worth looking at* (`stack_stats` over a `to_stack` cube) — lived only on the
  CLI/MCP surfaces, invisible to someone browsing the hosted archive. This brings
  both to the server, composed into one endpoint: two or three passes are
  narrated directly, and a longer series is **scanned first** — the pass-to-pass
  interval whose measured change stands furthest clear of the speckle detection
  floor (#193's number) is the pair narrated, chosen by a number rather than by
  the model (the §7 determinism boundary applied to frame selection). The chosen
  interval rides out on the response's `selected_interval`.

  The selection is a new pair of deterministic functions in `load.py`:
  `select_change_interval` (pure — reads a `stack_stats` payload, returns the
  interval maximising `changed_fraction - detection.false_alarm_fraction`, with
  `stands_clear` against the same `DETECTION_EXCESS_WARN` margin the reduction's
  own advisory uses) and `best_change_interval` (builds the cube, reduces it,
  applies the selector, returns the two `UmbraItem`s ready for
  `narrate`). Both are exported from the package.

  Because narration is the one renderer that **spends money per call**, it is
  opt-in and guarded rather than always-on. It is mounted only when the instance
  was started with `umbra serve --narrate` and a server-side model key
  (`ANTHROPIC_API_KEY` / `OPENAI_API_KEY`, optionally `--narrate-model`); a
  request to a server without it is a `501`, and the landing page advertises the
  `narrate` link only when it is enabled. The result is cached like every other
  artifact, so a repeat request returns the same narration with **no model call**
  and spends nothing. A per-day ceiling (`--narrate-daily-limit N`,
  `serve.NarrationBudget`) caps the *live* calls — counted only on a cache miss
  that actually reaches the model, `429` when the day is spent — and resets at UTC
  midnight. The key is read once at startup and held server-side; it is never a
  request field, so no client can point one instance at another's model or
  budget. A model failure maps to `502` (upstream), a malformed request to `400`,
  a missing extra to `501`. Deterministic and offline-testable throughout: the
  model boundary is an injected narrator and the render an injected renderer, so
  the endpoint, the budget, the cache behaviour and the long-series scan are all
  covered without a network call. This is Mode B of the demo-store narration
  workstream (`docs/STRATEGY.md` §8); the static-precompute Mode A is deferred by
  decision and would reuse these same two functions.
- **Carry the detection floor onto the composite path: `umbra change --narrate`
  reports what speckle alone would have produced (`ChangeStats.detection`,
  `narrate._change_detection_floor`).** The detection floor `stack_stats` added
  answered "is this bigger than speckle?" for a datacube, but the other surface
  where that is the reader's first question — a two-pass change narration — quoted
  a signed dB delta per grid block and grounded a vision model on it with no floor
  at all. On single-look imagery of ground that did **not** change, speckle by
  itself moves two cells in three past a 3 dB threshold, so a narration that
  reports brightening in the northeast without saying the interference under it
  has a 7.9 dB spread is handing the model — and the reader — a number with its
  meaning withheld.

  `compute_change_stats` now measures the floor for the two co-registered passes
  it differences the grid between and puts it on `ChangeStats.detection`. Each
  pass's equivalent number of looks is read off its own blocks with
  `convert._estimate_enl` of detected power (amplitude squared) over the cells
  imaged on both passes, and the two are reduced to one floor by
  `load._detection_floor` — the cube's *own* functions rather than a second
  implementation, so the block is `docs/schemas/stack-stats.schema.json`'s
  `$defs/detection` exactly (`looks`, `cell_sigma_db`, `false_alarm_fraction`,
  `false_alarm_target`, `target_threshold_db`) and a reader parses one contract
  for the cube and the composite alike. It is `None` when neither pass held enough
  homogeneous ground to read a looks estimate off (a scene smaller than one
  16-cell block), because a floor nobody could measure is not a floor.

  It reaches both audiences the narration has. The model is grounded on it: the
  `detection` block travels in `build_narrate_messages`'s scene card and the
  system prompt now teaches the floor as the bar a change must clear — treat
  `scene_changed_fraction` as evidence only insofar as it exceeds
  `false_alarm_fraction`, and a block within about one `cell_sigma_db` of zero as
  indistinguishable from interference. The reader is too: a
  `ChangeNarration.to_text` line states whether the observed change stands clear
  of the floor (`changed >= false_alarm_fraction * DETECTION_EXCESS_WARN`, the
  same margin the cube's advisory uses). And the whole thing flows to the JSON
  sidecar and `umbra change --narrate --json` through `ChangeStats.to_dict()`
  with no new flag. Checked the way `stack_stats`'s floor was — on two
  single-look realisations of one unchanged surface every flagged cell is a false
  alarm, and the predicted `false_alarm_fraction` lands on the observed
  `scene_changed_fraction` — plus that averaging the pair (more looks) brings the
  floor down, which is what prices a speckle filter in the units of the answer.
  Deterministic, offline, and behind the existing `[ai]` + `[viz]` extras; no
  model is consulted to compute a single number of it.
- **Say what speckle alone would have produced: `stack_stats` reports a
  detection floor (`detection`, per-pass `looks`,
  `docs/schemas/stack-stats.schema.json`, `src/umbra_py/_specfun.py`).** Every
  change number this library has ever produced was quoted without the one
  quantity that decides how to read it. `changed_fraction` counts the cells that
  moved more than `change_threshold_db` between two passes — and on single-look
  SAR imagery of ground that did **not** change, speckle by itself moves two
  cells in three past a 3 dB threshold. A reduction that reports 0.66 and says
  nothing about the 0.67 that interference would have produced anyway is not
  reporting a measurement; it is reporting a number with its meaning withheld.

  `stack_stats` now measures the floor and states it. Each pass gains `looks`,
  its equivalent number of looks read off the cube's own 16-cell blocks
  (`convert._block_enl_ratios`, the same reduction `umbra convert
  --speckle-filter` reports its ENL pair from), and a multi-pass cube gains a
  `detection` block: `cell_sigma_db`, the decibel spread of an *unchanged*
  cell's pass-to-pass difference; `false_alarm_fraction`, the share of unchanged
  cells speckle alone pushes past the requested threshold; and
  `target_threshold_db`, the threshold that would hold that share to
  `false_alarm_target` (5 %). A caveat quotes all three on every multi-pass
  cube, and a second one fires when the observed `changed_fraction` does not
  stand clear of the floor — the finding rather than the context, since that is
  a series with no scene-wide change the threshold can tell from interference.

  Three things make it a measurement rather than a rule of thumb. It is
  **exact**: an L-look intensity is a gamma variate, so the false-alarm rate is
  `2 * I_(1/(1+f))(L, L)` and the spread is `(10/ln 10) * sqrt(2 psi'(L))`,
  where a normal approximation on the decibel axis is wrong by tens of per cent
  near one look — which is exactly where the answer is most alarming. Both
  special functions are ~80 lines of stdlib `math` (`_specfun.py`) rather than a
  SciPy dependency for two calls, pinned against closed forms and a direct
  integration. It is **read off the cube** rather than off the source products,
  because `to_stack` decimates onto a shared grid and decimation averages
  speckle down, so the looks that matter are the ones the cells being quoted
  actually have. And it is **conservative by construction**: scene structure
  inflates a block's variance and so deflates its looks, so the floor is an
  upper bound on the false alarms rather than a flattering estimate.

  The claim is checked end to end rather than argued: two independent
  single-look realisations of the *same* surface are ground that did not change,
  so every cell the detector flags is a false alarm — and the predicted
  `false_alarm_fraction` lands within half a per cent of the observed
  `changed_fraction` (0.664 against 0.667). It also puts a number on what the
  speckle filter buys in the units of the answer rather than of the window: a
  filtered cube reads more looks, so its floor, its spread and its 5 % threshold
  all come down. Because the document is one `to_dict()`, it reaches `umbra
  stack --stats`, `POST /artifacts/stats` and the `stack_stats` agent tool with
  no new request field and no new flag — nothing to opt into, which is the point
  for a caveat this load-bearing.
- **Make the zero-install MCP front door runnable, and publish it where agents
  look (`server.json`, `release.yml`'s `publish-mcp` job,
  `tests/test_mcp_registry.py`).** The MCP server has been the project's
  zero-install claim since it shipped — "any MCP client becomes a
  natural-language front door to the archive, nothing installed" — and every
  surface that stated the command stated one that does not run. The README, the
  two `llms.txt` documents, the module docstring and `umbra mcp --help` all told
  an agent to hand the `umbra-mcp` console script to `uvx` on its own. That
  script lives in the **`umbra-py`** distribution, so the short form resolves to
  a distribution by the script's name — there is none — and would not have
  installed the `[mcp]` extra the server needs even if there were. The command
  is `uvx --from 'umbra-py[mcp]' umbra-mcp`, and it now says so in all five
  places, plus a paste-in client configuration in the README for a client that
  cannot run a shell.

  The reason it could rot unnoticed is that nothing in the repository could tell
  a command from a sentence, so the fix is a check rather than five edits.
  `tests/test_mcp_registry.py` *derives* the invocation from the packaging —
  `[project.name]`, `[project.scripts]`, `[project.optional-dependencies]` — and
  requires every `uvx` mention in the documented surfaces to lex to exactly that
  argv. Renaming the extra, the console script or the distribution fails on the
  rename and names it, which is the same "parse it, don't run it" shape
  `tests/test_workflows.py` gives the weekly workflows.

  With the command real, the listing is worth having. `server.json` publishes it
  to the [MCP registry](https://registry.modelcontextprotocol.io/) as
  `io.github.reesehammer/umbra-mcp` (the `io.github.` namespace GitHub OIDC
  grants this repository), declaring the four environment variables an operator
  can set — the Canopy token that switches the same search tools to the
  commercial archive, the local index path, and the two model keys the two
  opt-in AI tools use — every one of them optional, because the open archive
  needs no credentials. The manifest is checked the same way the command is:
  its version tracks `__version__`, its package identifier is this
  distribution, its runtime arguments must compose to the derived argv, its
  declared variables must be ones the package actually reads, and its name's
  owner segment must match the repository that will publish it (a mismatch is
  rejected by the registry a week after it merges, not at review). One
  assertion is about an ambiguity rather than a fact: a client that renders
  `uvx <runtimeArguments> <identifier>` appends a stray `umbra-py` to the
  command, which is harmless *only* because the entry point takes no arguments —
  so the suite pins that signature rather than trusting it.

  Submission is a job on the existing release pipeline (`publish-mcp`, after the
  PyPI upload) rather than a new workflow, because the ordering is a hard
  requirement, not a preference: the registry proves ownership of a PyPI package
  by fetching its long description and looking for an `mcp-name:` marker — which
  the README now carries, and which `pyproject.toml` ships as that description —
  so publishing before the version is resolvable on PyPI is a guaranteed
  failure. The job waits for it, bounded, and says so if it never arrives.
  Cutting the first release and becoming findable from any MCP client are now
  one action. See `docs/STRATEGY.md` §5.3.
- **Put the published contracts where the HTTP surface reads them
  (`umbra_py.schemas`, `docs/schemas/render-job`, the OpenAPI components).**
  Sixteen schemas were published, strict and checked against a real payload —
  and unreachable from anything but a clone. They live in `docs/`, so a wheel
  did not carry them and nothing in `src/` could load one, which had two
  consequences worth naming. A consumer could not validate against the version
  it installed. And `umbra serve`, whose whole claim for the non-MCP half of the
  agent ecosystem is that a client consumes it "from the generated OpenAPI
  document alone", described `POST /artifacts/stats` as returning a bare object
  while `stack-stats.schema.json` described it exactly. The contract and the
  document a generated client actually reads had no connection.

  `umbra_py.schemas` is that connection: `load_schema("stack-stats")`,
  `schema_names()`, `schema_path()`, stdlib only (these are data files, not a
  validator — validating stays the consumer's choice). The packaging decision it
  rests on is that the schemas keep **one home**, `docs/schemas/`, which is the
  path every schema's own `$id` names and where they are read on GitHub, and the
  wheel carries a *copy* of that directory as package data (`umbra_py/_schemas/`,
  a `force-include`) the same way `py.typed` is a build artifact of a
  source-tree fact. So the accessor reads the packaged copy first and falls back
  to the checkout it was imported from, which is what makes an editable install —
  the documented dev loop, and CI — resolve the same files a wheel ships.
  Nothing at runtime can notice those two ends disagreeing, so a test parses
  `pyproject.toml` and checks the `force-include` target against the accessor's
  own constant, the same way `tests/test_workflows.py` parses the workflows. The
  `Dockerfile` and `.dockerignore` carry `docs/schemas` for the same reason a
  wheel does: an installed package has no checkout to fall back to, so an image
  built without them would raise on `/openapi.json` — and because a
  `force-include` is mandatory, it fails the build instead, which is the honest
  place for it.

  What it buys is a self-describing API. The three contracts the artifact routes
  emit are merged into the generated document as components — `StackStats`,
  `StackProvenance` and `RenderJob` — and every route's response `$ref`s one, so
  a generated client gets the same shape the CLI's `--json` and the agent tools
  hand back. They are the committed files rather than a restatement: only
  `$schema` and `$id` are dropped (OpenAPI 3.1 declares the dialect for the whole
  document, and an `$id` would re-base the internal pointers onto a URL nothing
  can fetch, so `#/$defs/pass` is re-rooted to
  `#/components/schemas/StackStats/$defs/pass` instead), and the identity comes
  back as `x-umbra-schema-id`. A cross-file `$ref` — the form `render-manifest`
  and `watch-delta` use — is **refused** rather than inlined, because a
  reference no client can resolve is worse than a shape it was never given. The
  suite asserts the component equals the file after that rewrite, that every
  `$ref` in the whole document resolves, and that an `--no-artifacts` instance
  publishes none of them, since a component nothing references would describe a
  shape that instance does not emit.

  The routes that return *bytes* say so too (`image/png`, `text/html`) rather
  than advertising FastAPI's default `application/json` alongside a picture they
  never emit. And the one document the HTTP surface emits that had no contract
  at all now has one: `docs/schemas/render-job.schema.json`, the job every
  `"async": true` request answers with and `GET /jobs/{id}` returns — the
  document a client *polls*, so its conditional halves are the contract's
  substance (a `result` link exactly when `status` is `succeeded`, `cache`
  present only then, `error` only on `failed`, and a `started` that is null both
  while queued and for a job born succeeded from the cache, which ran no work).
  Seventeen schemas now.

- **Publish the surfaces an agent reads as contracts, and check them
  (`docs/schemas/item-context`, `scene-description`, `search-plan`,
  `watch-delta`, `task-matches`, `scene-matches`).** The two previous entries
  covered the documents that carry a *measurement* and the ones that describe a
  *dataset*. What was left were the surfaces read by a model or a scheduler
  rather than by a person — the ones design principle 5 ("agents are users") is
  actually about — and none of them had a contract. Six schemas closes that set:
  every `--json` shape this project emits is now published, except the raw STAC
  item behind `umbra info`'s argument, whose contract is STAC's own.

  `item-context.schema.json` is the one they share, and the reason it went
  first: `UmbraItem.to_llm_context()` is the single most-read document in the
  library — `umbra info --json` prints one, the `search_catalog` / `get_item`
  agent tools return them, and a watch delta carries one per new acquisition —
  and it is not the source STAC item but a compact, *explained* reading of one,
  which makes it this project's contract rather than STAC's. So `watch-delta`
  `$ref`s it for its `new_items` instead of restating the card, the rule
  `render-manifest` already follows for `stack-stats` and `chip-dataset` for
  `chip-skipped`: one question, one schema, wherever it is emitted from.

  What these documents had to get right is different from what the measurement
  ones did, and it is the same thing in all five of the rest: **which fields a
  model wrote**. A scene description's `summary`, `observed_features`,
  `confidence` and `caveats` are model-authored; its `attribution` and
  `provenance` are stamped on by the library and *cannot* be set by a reply,
  which is the entire reason the document exists rather than the prose the model
  returned. A search plan's `rationale` is the one model-authored string in it
  and never becomes a filter, while its `aoi` names a polygon the caller
  supplied — the geometry half of the determinism boundary, where a
  hallucination can pick the wrong area but cannot move one. A match's `score`
  is a number a test can recompute. That distinction was true before and
  documented only in docstrings; it is now in the contracts a consumer parses.

  The nullable and conditional halves are in the contract for the same reason
  they were for `stack-stats`, because they are what a consumer gets wrong: a
  `confidence` of null (the model hedged nothing, or hedged in a word this does
  not recognise — an off-menu level is dropped rather than passed through), a
  `SceneImage` whose `width` disagrees with the `max_size` that was asked for
  (a baked preview is 128–256 px where a render defaults to 1024, and that
  disagreement is *evidence* rather than a detail), a watch delta's `query`
  object being open by construction because unset filters are dropped rather
  than emitted as nulls, and an empty `matches` list being an answer — nothing
  cleared the threshold — rather than a failure.

  Each is validated against a payload from the surface that emits it, through
  the CLI where there is one, with the model step injected so no test calls one.
  Every schema stays strict, so a key added to a context card and not to its
  contract fails the build rather than somebody's agent. The seven `--json`
  flags involved (`info`, `describe`, `ask`, `watch`, `semantic search`,
  `embed similar`, `embed search`) name their schema in `--help`, and each
  `to_dict()` names it in its docstring.
- **Publish the chip dataset as a contract, and check it (`docs/schemas/chip-dataset`,
  `chip-record`, `chip-skipped`).** The previous entry closed the *measurement*
  documents and named the surface to do next: `umbra chips`, on the argument that
  a training loader is the consumer most likely to parse a payload it never
  printed. That is literally true of a chip run — the file a loader opens is
  `manifest.jsonl`, written by one process and read by another, quite possibly
  months later — and it had no contract at all.

  Three schemas, because a chip run has three consumers reading three different
  things. `chip-dataset.schema.json` is what `umbra chips --json` prints
  (`ChipDataset.to_dict`): the grid, the acquisitions in it, the conversion
  settings, and the roll-ups. `chip-record.schema.json` is one training tile
  (`ChipRecord.to_dict`) — where it is, what the acquisition was, how usable the
  tile is, and what the processing chain did to its pixels. The record rather
  than the manifest *file* is the contract, because all three manifest formats
  carry the same one: a `.geojson` feature's `properties` and a `.parquet` row
  are the `.jsonl` line, and the suite validates the first two against the same
  schema to keep that true. `chip-skipped.schema.json` is one left-out pass,
  which is both a line of the `skipped.jsonl` sidecar and an entry of the
  summary's `skipped` array — so the summary `$ref`s it rather than restating
  it, the rule `render-manifest` already follows for `stack-stats`.

  What the contract had to get right is the *conditional* half, since a chip
  payload is mostly optional blocks: `conversion` appears only when a complex
  product was geocoded, `noise` only when a floor came off, `speckle` only when a
  window ran, `skipped` / `skipped_count` / `skipped_manifest` only when a pass
  was left out, and `preflight` only when the archive was asked first. That is
  the design — a plain GEC run's payload is unchanged by any of those features
  existing, so the *absence* of `skipped` states "every acquisition offered was
  chipped" rather than defaulting — and it is now stated in the schema rather
  than implied by a docstring. Both ends are pinned against real runs: a plain
  raster run that carries none of the five, and a converted one that carries all
  of them.

  The nullable half is in the contract for the same reason it was for
  `stack-stats`: `noise_floor_margin_db` is null for a *measured* floor, which
  assumes nothing about the scene and so has nothing to report;
  `speckle_looks` is null for `boxcar`, which needs no such parameter; the
  per-scene diagnostics repeat across every chip of one acquisition by design,
  because they describe the scene the tile was cut from. A consumer that read
  those as per-chip numbers, or a null as a zero, would draw the wrong
  conclusion from a correct file.

  `tests/test_schemas.py` validates the *emitted* document rather than the
  Python dict — the conversion's `bbox` is a tuple in the dataclass and an array
  on stdout, and it is the array a schema describes, so the round-trip is part
  of the check. Every schema stays strict, so a field added to a chip record and
  not to its contract fails the build rather than somebody's data loader.
  `umbra chips --json`'s `--help` names both schemas it produces.
- **Publish the measurement documents as contracts, and check them
  (`docs/schemas/stack-stats`, `stack-provenance`, `preflight`).** "Agents are
  users; users are agents" is the design principle the JSON surfaces were built
  on, and `docs/schemas/README.md` calls those shapes public API — but only four
  existed, for the error object, the download manifest, the index summary and
  the render manifest. The three documents an agent is most likely to *parse*
  rather than glance at had none. `stack_stats` in particular is the library's
  largest payload and its only one that is a measurement: per-pass distribution,
  the signed decibel change between consecutive passes, the net first-to-last
  change, the spatial breakdown naming which block moved and between which two
  passes, and the caveats that say what those numbers do and do not mean. A
  consumer had the docstring and the examples, which is not a contract.

  Three schemas now describe them, each pinned against a real payload:
  `stack-stats.schema.json` (`umbra_py.stack_stats`, `umbra stack --stats
  --json`, `POST /artifacts/stats`, the `stack_stats` agent tool),
  `stack-provenance.schema.json` (`umbra_py.stack_provenance`, `umbra stack
  --provenance --json`, `POST /artifacts/provenance`, the `stack_provenance`
  agent tool) and `preflight.schema.json` (`umbra preflight --json`, and the
  same verdicts `umbra chips --preflight` acts on). One schema per *question*
  rather than per surface, which is the shape the code already had: each
  document is one `to_dict()` that three front doors emit unchanged, so a shell,
  an HTTP client and a model read one contract. `render-manifest.schema.json`
  stops describing its `stats` key as a free-form object and `$ref`s the stats
  schema instead — the same rule applied between two schemas.

  The nullable and conditional halves are in the contract rather than implied,
  because they are exactly what a consumer gets wrong: `net_change` is null for
  a single-pass cube, `changed_area_km2` and `cell_area_m2` are null on a
  geographic grid (counting cells there measures nothing), a pass with no valid
  cell reports null statistics, `provenance` is present only when the sources
  recorded one — so its *absence* means "the published products as delivered"
  rather than "unknown" — and `quantile_method` / `quantile_bin_db` appear only
  under `windowed`, where the percentiles are histogram estimates. A schema that
  described only the happy payload would have been a worse promise than none.

  `tests/test_schemas.py` is what makes them contracts rather than
  documentation. Every schema here is now validated with a real JSON Schema
  validator (`jsonschema`, a new `[dev]` dependency) against a payload from the
  surface that emits it — including the two that previously had only a key-set
  comparison, which catches a renamed field and nothing else: not a type that
  changed, not a value that went null, not a key that appeared. Every schema is
  strict (`additionalProperties: false`), so a field added to a payload and not
  to its contract fails the build instead of a consumer. The suite also holds
  the parts that rot quietly: each file is a valid draft 2020-12 schema, each
  `$id` matches its filename (a cross-file `$ref` resolves against it), and the
  README table names every file and no file it does not.

  The claim that three surfaces emit one document is not re-tested here — it is
  already pinned per surface in `tests/test_serve.py` and
  `tests/test_mcp_server.py`, so validating the one `to_dict()` validates all
  three. `umbra stack --stats --json`, `--provenance` and `umbra preflight
  --json` now name their schema in `--help`, as the download and index-info
  commands already did.
- **Ask it from the front doors that answer for people who installed nothing
  (`POST /artifacts/provenance`, the `stack_provenance` agent tool).** The
  provenance preflight shipped as a library function and a CLI flag, which
  covers everyone with a checkout. The two surfaces built so nobody needs one —
  `umbra serve` and the MCP / LangChain / LlamaIndex tools — still learned that
  a selection was two conversions by *spending* the measurement: the mix came
  back as `POST /artifacts/stats`'s `400`, carrying advice ("use only the
  acquisitions that share one") about a subset it could not name. A hosted
  client had no cheaper way to ask, and an agent had nothing to do with the
  answer but guess.

  `POST /artifacts/provenance` takes the body you would send to
  `/artifacts/stats` — the same `ids` or `bbox`/`datetime` query, normalised by
  the same `stats_options`, vetted into the same frames by `stats_frames` — and
  returns `StackProvenance.to_dict()`: `agrees`, the `groups` largest-first with
  the `hrefs` to re-run on, `shared` when they agree, `refusal` when they do
  not, and `unreadable` for sources that could not be opened. Taking the stats
  body is what makes the answer *about* the request it precedes: the preflight
  is asked about the frames that endpoint would actually stack, so a cleared
  report cannot be contradicted by the stats call it cleared — the same
  construction that keeps the verdict `to_stack`'s own.

  A mixed selection is a `200`. Reporting the mix is what was asked for, and the
  same mix at `/artifacts/stats` is still the `400` this response quotes
  verbatim. Only a selection that could not be measured at all — fewer than two
  passes, or mixed polarizations — is a `400` here, because there is no stack to
  preflight.

  It is the one artifact route that neither renders nor caches. Not cached
  because a re-converted source is exactly the case a content-addressed answer
  would get wrong, and because the read is kilobytes: a question asked to avoid
  spending a render should not need a job document of its own. Not routed
  through the injectable `renderers` because the report is not a render, and an
  injectable provenance would be precisely the second opinion the design rules
  out. The landing page advertises it as a `provenance` link beside `stats`.

  `stack_provenance(urls, asset="GEC")` is the same question as an agent tool,
  registered on the MCP server and carried to LangChain and LlamaIndex by the
  shared `_JSON_TOOLS` roster (the same callable on all three — no drift). It
  shares `stack_stats`' two preconditions, so anything that tool would accept is
  something this one answers for, and its docstring tells a model the two moments
  to reach for it: after a provenance refusal, where `groups[0]["hrefs"]` is the
  subset to retry on, and before a long series it did not assemble itself.

  All three surfaces emit one document. The CLI's `--json`, the endpoint's body
  and the tool's return value are the same `to_dict()`, so a shell, an HTTP
  client and a model read one schema.
- **Ask whether a series is one measurement before stacking it
  (`stack_provenance`, `umbra stack --provenance`).** `to_stack` refuses to
  co-register passes whose conversions disagree — a calibrated pass differenced
  against an uncalibrated one puts the difference between two *conversions* on
  the time axis and reports it as change on the ground. That refusal is right,
  and it had two gaps. It was discoverable only by hitting it: the check runs
  inside `to_stack`, so a caller learned a selection was mixed by asking for the
  cube. And its own advice — "use only the acquisitions that share one" — named
  a subset it did not identify, which on a forty-pass site is not advice.

  `stack_provenance(items)` answers both. It reads each acquisition's `UMBRA_*`
  record straight from its raster header, groups the selection by
  `MEASUREMENT_PROVENANCE_KEYS`, and returns a `StackProvenance`: whether the
  series `agrees`, the `refusal` if it does not, the `shared` record if it does,
  the `groups` largest-first, and `largest` — the biggest set of acquisitions
  that *do* agree, with the item URLs to re-run on. The subset the refusal could
  only gesture at is now a command.

  What it costs is the opens a stack performs anyway: one COG header per
  acquisition, a range request of kilobytes, and no pixels at all (a test spies
  on `DatasetReader.read` to keep that true). What it saves is everything after
  them — the shared grid, the warp, and every decimated read. It is the move
  `umbra preflight` makes for a chip run, one layer up.

  The verdict is not a second opinion: `_shared_provenance` — the function that
  raises — is called on the same records, so `report.refusal` is verbatim the
  `ValueError` a stack of that selection would raise, and `report.shared` is
  verbatim the record it would carry. The grouping runs on
  `_comparable_record`, factored out of that same function, so a preflight
  cannot group by one rule while the refusal compares by another.

  A source that cannot be *read* is reported apart, on `report.unreadable`,
  rather than grouped: an item listing no such asset, an href with nothing
  behind it, bytes that are not a raster. That is the line
  `PreflightResult.error_scope` draws in the chip preflight — a failed read is
  not a product declaring its pixels are something else — and here it means such
  a pass does not make the series *mixed*, it makes the answer *incomplete*, and
  the report says which.

  On the CLI, `umbra stack --provenance` prints the grouping, the largest
  agreeing subset and a ready-to-run `umbra stack` line for it; `--json` emits
  the same report as one object. It is refused alongside `--out` / `--stats`
  rather than combined with them, because the point of the flag is to ask before
  paying and pairing it with the work would hide which of the two answered. The
  common case reads as what it is — a series of published GECs prints "no
  umbra-py conversion (a published product as Umbra ships it)" rather than seven
  `none`s.
- **Tell a failed read that is a verdict apart from one that is a hiccup
  (`UnreadableProductError`, `PreflightResult.error_scope`,
  `PreflightSummary.missing`).** `umbra chips --preflight` reads each
  acquisition's SICD metadata over the wire and drops the passes that declare
  they cannot answer, before a single product is downloaded. Some of those reads
  do not produce a declaration at all, and the rule for those was one line:
  *keep the pass* — a failed read is not a product saying it cannot answer, and
  dropping a scene over something transient would put a hole in a dataset that
  the archive never had.

  That is the right rule for a timeout. It is the wrong rule for the case it was
  mostly hitting, because "could not be read" was covering two different pieces
  of news. A connection that drops says nothing about the product. An item that
  lists no `SICD` asset, an href with nothing behind it, bytes that are not a
  NITF, a NITF carrying no SICD XML — those say everything, and say it as
  finally as any refusal does. No later attempt changes the answer.

  Keeping *those* was not the cautious choice it looked like. A pass with no
  readable product fails inside `chip_item` as a plain read error, and
  `--skip-unsupported` catches `UnsupportedMeasurementError` and nothing else,
  on purpose — a batch that swallows unknown errors is one nobody can trust. So
  the sequence was: the preflight establishes that an acquisition cannot be
  chipped, keeps it out of caution, hands it to the chipper, and the run ends on
  it. A check that had already found the answer was not allowed to use it.

  The fix is the same move `UnsupportedMeasurementError` was: give the family a
  name. `UnreadableProductError` is raised by every path in `preflight.py` where
  what is at an href is not a product this can read — the magic bytes, the NITF
  version, a truncated or non-numeric header field, a missing XML segment, XML
  that does not parse, plus an HTTP 404/410 and a local file that is not there,
  which are the same statement made by the source rather than by the bytes. It
  is an `UmbraError` and nothing narrower, so every `except UmbraError` around a
  read is unchanged; `AssetNotFoundError` is left as itself, since "the item
  lists no such asset" is the same kind of fact already named.

  `PreflightResult.error_scope` reports the classification (`"product"` /
  `"transport"`, with `.readable` and `.final` as the two questions a caller
  actually asks), and `_error_scope` sorts by type: the two named errors are the
  product's, and **everything else is transport**, so an unforeseen failure
  takes the cautious branch by construction and the worst a new error mode can
  do is cost a download rather than silently remove a pass from a dataset. The
  403 that could be a proxy problem stays transport for the same reason; only
  404 and 410 mean "there is nothing here".

  What a chip run does with each is then just the classification. A transport
  failure is kept and counted `unreadable`, exactly as before. A product failure
  is dropped and counted `missing`, recorded as the same `SkippedAcquisition`
  with `stage="preflight"` a refusal produces, carrying the reader's own words —
  because the dataset's hole is the same shape however the pass came to be
  absent. The two counts are separate on `PreflightSummary` and named separately
  by `umbra chips` and `umbra preflight`, since only one of them is worth asking
  about again: "no readable product" is a verdict about the archive, "could not
  be reached" is a verdict about the attempt.
- **Write the hole into the dataset, not only into the run that built it
  (`skipped.jsonl`, `write_skipped_manifest`, `ChipDataset.skipped_path`).**
  Five changes now stand between a chip batch and an archive that cannot answer
  every question asked of it: the refusal has a name
  (`UnsupportedMeasurementError`), `--skip-unsupported` survives it,
  `--preflight` sees it coming, a worker pool keeps that check from becoming the
  stall, and `--rtc` joined the two corrections that were already treated this
  way. Every one of them ends the same place — `ChipDataset.skipped`, the
  `--json` payload, and a line on the console naming the pass and quoting the
  product's own words for why.

  All three of those are reports to somebody who was *watching*. The artifact a
  chip run exists to produce is a directory, and a training loader opens it
  months later, on another machine, and sees files. `manifest.jsonl` describes
  the tiles that exist; nothing in the directory said which ones were meant to
  and do not. A dataset that dropped nine of twenty-two passes was
  indistinguishable on disk from one that was only ever offered thirteen — and
  the difference between those two is whether a gap in a time series is the
  archive's or the pipeline's.

  So a run that could not include every acquisition it was offered now writes a
  `skipped.jsonl` sidecar beside the manifest: one JSON object per left-out
  pass, `SkippedAcquisition.to_dict()` verbatim, so `item_id`, `datetime`, the
  refusal's own words, the recovery `hint` and the `stage` it was found at read
  identically from the file and from the run. `stage` is the field that earns
  its place here — `"preflight"` versus `"conversion"` is the one thing the two
  routes to a hole do not share, and it is what tells a reader whether a pass
  was never downloaded or downloaded and refused.

  Three decisions, each the same one this chain has made before. It is a
  **sidecar** rather than rows in the manifest because the manifest's schema is
  one row per chip and a skipped acquisition has no chip: putting it there means
  a record with no path, no bbox and no transform that every consumer of that
  schema must then learn to ignore. It is always `.jsonl`, whatever format the
  manifest beside it is, because the three manifest formats are three ways of
  describing *tiles* and none of them is what a missing acquisition is. And it
  is written **only when there is something to record**, so a run with no hole in
  it leaves exactly the files it left before — the file's presence is itself the
  statement, the same rule that keeps the `skipped` block out of a clean run's
  `--json` payload.

  `write_chips(skipped_manifest=…)` names or suppresses it and follows
  `manifest`, so `manifest=None` still means "collect the records, write
  nothing". `ChipDataset.skipped_path` and the `--json` payload's
  `skipped_manifest` report where it went, and `umbra chips` prints the path
  under the per-acquisition report — because the console is the one place that
  report reaches somebody who is watching, and the file is the only place it
  reaches somebody who is not.
- **Name the refusal terrain flattening forces, and ask it over the wire
  (`umbra preflight --rtc`, `SicdCapabilities.look_geometry`).** Three of
  `umbra convert`'s corrections depend on the product describing itself, and
  until now only two of them were treated that way. `--calibrate` and
  `--noise-model measured` read the SICD's `Radiometric` block, raise
  `UnsupportedMeasurementError` when it cannot answer, are skippable
  (`umbra chips --skip-unsupported`), reach the `--json` error envelope with a
  hint, and are answerable ahead of a download (`umbra preflight`). The third,
  `--rtc`, reads the collection geometry out of the same file's `SCPCOA` block —
  and did none of it. A product that stated no geometry raised a bare
  `ValueError`, so a chip batch could not carry on past it, an agent could not
  branch on it, and nothing could see it coming.

  It is the same *kind* of fact as the other two — what the flattening needs is
  in the file or it is not, and the honest responses are a different setting or a
  different scene rather than a fix to the request — so it is now the same type.
  `_scene_look_geometry` raises `UnsupportedMeasurementError` (still a
  `ValueError`, so every caller that caught one still does), which makes the
  whole existing machinery apply with no new vocabulary: `--skip-unsupported`
  records the pass on `ChipDataset.skipped` in the product's own words and moves
  on, and the refusal renders into the JSON envelope with a hint pointing at the
  unflattened conversion.

  And the question moved to where it can be asked cheaply, twice over.
  `_check_measurement_support` takes `rtc=`, so a product that cannot be
  flattened says so from its **header** rather than after a multi-gigabyte
  complex read, a DEM fetch and a warp — the strongest version of the ordering
  argument the `Radiometric` checks already made, because `--rtc` is the most
  expensive thing to discover late. `umbra preflight --rtc` /
  `preflight_items(rtc=True)` then asks it over the wire, before anything is
  downloaded at all, through the conversion's own check applied to the
  range-read XML — so a preflight that says yes still cannot be contradicted by
  the conversion it clears. `umbra chips --preflight` picks it up from the run's
  own `SicdConversion.rtc`, which is what keeps the preflight's question the
  conversion's question by construction.

  What a product *does* state is reported beside the calibrations and the noise
  level: `SicdCapabilities.look_geometry` is the scene-centre
  `(incidence, azimuth)`, in `--json` and on the per-scene report line. Most
  products carry it — which is exactly why it was worth wiring up rather than
  assuming. The absent case is the one nobody plans for, and it was the one that
  cost the most to find.
- **Stop the check that runs before a batch from being the batch's slowest part
  (`umbra preflight --workers`, `umbra chips --preflight-workers`,
  `preflight_items(workers=…)`).** The header-only preflight replaced a
  whole-product download per acquisition with two small range requests, which
  answered the cost question completely: a forty-pass site went from tens of
  gigabytes to a few hundred kilobytes. What it did not answer is the *other*
  cost, the one that only appeared once the bytes were gone. The walk was serial,
  so a forty-pass site was forty sequential round trips — latency, not bandwidth
  — standing in front of a run whose entire argument is that you should not pay
  for the same thing twice. On the command built to make a batch cheap, the check
  had become the batch's slowest part.

  The reads are independent, so they are now issued through a small thread pool:
  `workers=` on `preflight_items` (`DEFAULT_PREFLIGHT_WORKERS`, 8 — the catalog
  walk's own sidecar fan-out, and inside the shared session's connection pool, so
  the concurrency costs no reconnections), `--workers` on `umbra preflight` and
  `--preflight-workers` on `umbra chips`. Wall time follows `N / workers`; not one
  byte of what is transferred changes.

  What makes it safe to widen is that the concurrency is a **schedule and not an
  answer**, and the design is what holds that line. The verdicts are *consumed* in
  the order they were asked in, not in the order they arrive: `write_chips` pairs
  them against its own selection positionally (`zip(items, report.results,
  strict=True)`) precisely because two passes of one task can share an id, so a
  completion-ordered result would silently attach one pass's refusal to another —
  the quietest possible way for a dataset to be wrong. The futures are submitted
  up front and read in sequence, so every lane is busy and the answer is the
  serial walk's answer. `progress` keeps its contract exactly: one call per
  acquisition, in selection order, counting up, from the calling thread — a CLI
  callback that echoes a line never has to become thread-safe.

  The old path is still reachable and still the default where it is the right one:
  `workers=1` runs no pool at all, and the effective lane count is capped at the
  number of products, so a one-scene preflight is the code that shipped before
  this, thread and all. `PreflightReport.workers` records the width actually used
  — a fact about how the answer was obtained rather than about the answer, which
  is why it is reported and not part of any cache key.
- **Spend the preflight on the batch it was built for (`umbra chips
  --preflight`, `write_chips(preflight=True)`).** The header-only read shipped as
  a command you run *beside* a chip run: `umbra preflight` told you which of a
  site's passes could be calibrated, and then you re-typed the selection into
  `umbra chips`. The batch itself still discovered a refusal by attempting the
  conversion — which for a complex asset means downloading the whole
  multi-gigabyte NITF to read the header that would have said no. So the saving
  existed and was manual: the two commands knew the same thing and only one of
  them was paying for it.

  `--preflight` wires them together. Each acquisition's SICD XML is read over the
  wire first (the same two range requests, tens of kilobytes), the conversion's
  own support check is run against it, and the passes that cannot answer are
  dropped before a single product is fetched. The settings asked about come from
  the run's own `SicdConversion` rather than from separate parameters, so the
  question the preflight asks is by construction the one the conversion will ask:
  a pass it clears cannot then be refused for a reason it could have seen.

  What made this a design decision rather than a flag is what a batch that
  silently drops scenes would be — exactly the failure mode `--skip-unsupported`
  was careful to avoid. So a preflighted drop is recorded the same way a survived
  refusal is: a `SkippedAcquisition` on `ChipDataset.skipped`, carrying which
  pass, when, the product's own words for why, and the recovery hint. The one
  thing the two routes do not share is *when* the refusal was found, and that is
  the one field added — `stage` (`"conversion"` or `"preflight"`) — because a
  preflighted pass was never downloaded, which is the whole saving. A dataset's
  hole is described identically either way.

  That saving is reported rather than asserted. `ChipDataset.preflight` (a
  `PreflightSummary`, in the `--json` payload and on the way out) is what asking
  cost against what it removed: *"Preflight read 452.6 KB of product headers from
  22 acquisition(s) and dropped 9, saving 31.4 GB of download."* The claim is
  deliberately narrow — a supported pass is downloaded anyway, so its header read
  is overhead rather than something avoided, and only the dropped products are
  counted as saved.

  An acquisition whose metadata cannot be *read* — a missing asset, an HTTP
  failure, a product that is not a NITF — is **kept** in the run and counted as
  `unreadable`. A failed read is not a product declaring it cannot answer, so it
  does not get to remove a scene from a dataset; the batch finds out the
  expensive way instead, which is the right response to something that may be
  transient. `--preflight` composes with `--skip-unsupported` and both are worth
  passing: the metadata answers only two of the questions a conversion asks, so
  anything else still refuses at conversion time. Asking for it on a `GEC`/`CSI`
  asset is refused rather than ignored — those carry no SICD metadata to ask and
  are streamed tile by tile rather than downloaded — and the refusal is a
  parameter error raised before any search runs.
- **Ask a complex product what it can support before downloading it (`umbra
  preflight`, `umbra_py.sicd_capabilities`).** The two corrections that depend on
  a product describing itself — radiometric calibration and a `measured` noise
  floor — refuse on most of Umbra's open archive, and refusing is the point. What
  was expensive was *finding out*: a SICD's metadata lives inside the NITF, so
  learning that a pass cannot be calibrated meant downloading the pass. Over a
  site's twenty passes that is tens of gigabytes spent to be told no. The previous
  change made that refusal survivable (`--skip-unsupported`) and cheap per scene
  (the check moved ahead of the pixel read); it could not make the *discovery*
  cheap, and named the header-only read as the different job it would take.

  This is that job. A NITF states its own layout in a fixed-width file header, so
  the SICD XML — a data extension segment near the end of the file — can be
  located by arithmetic on ~30 bytes of segment table and fetched with two HTTP
  range requests. `sicd_capabilities(url)` returns what the product declares
  (which of `sigma0` / `beta0` / `gamma0` / `rcs` its `Radiometric` block carries,
  whether its noise level is `ABSOLUTE`, `RELATIVE` or absent, plus the scene's
  identity), what reading it cost, and — from the range response's
  `Content-Range` — the download it did not do. `preflight_items` asks it of a
  whole selection.

  The verdict is not a second opinion about what a product supports. The parsed
  XML is presented through an attribute view shaped like a `sarpy` SICD (a
  polynomial's exponent-addressed `<Coef>` children densified into the `Coefs`
  grid the conversion reads), so `convert._check_measurement_support` — the same
  function the conversion runs, calling the same coefficient readers — produces
  the answer. A preflight that says yes and a conversion that then refuses cannot
  disagree; what differs is only where the metadata came from.

  `umbra preflight` puts it on the CLI with the shared search options every
  gather command has, so it takes the same `--area` / `--bbox` / `--place` /
  `--intersects` selection as the `umbra chips --asset SICD` run it clears the way
  for, and reports the cost it saved: *"0 of 20 acquisition(s) support --calibrate
  gamma0. Read 412.0 KB of product headers instead of 61.3 GB of product."* An
  acquisition whose metadata cannot be read at all — a missing asset, an HTTP
  failure, a file that is not a NITF — is recorded as its own verdict rather than
  ending the walk, because a preflight that dies on the nineteenth scene has
  failed at the one thing it is for.

  The whole path is stdlib plus the already-core `requests`: no `sarpy`, no
  `numpy`, no `[convert]` extra, so "can this archive answer my question?" is
  answerable from a core install. NITF 2.0 is refused by name rather than misread
  (its security fields are a different length, so every offset would land
  somewhere arbitrary), as are a truncated header, a non-numeric length field, a
  product with no XML segment and unparseable XML.
- **Name the refusal a product forces, so a batch can survive it and an agent
  can branch on it (`UnsupportedMeasurementError`, `umbra chips
  --skip-unsupported`).** Two of the conversion's corrections depend on the
  product describing itself: radiometric calibration reads the SICD's
  `Radiometric` scale-factor polynomials, and `--noise-model measured` reads its
  `Radiometric.NoiseLevel.NoisePoly`. Umbra's open products generally carry
  neither, and refusing has always been the point — a scaling or a subtraction
  by an invented number is indistinguishable in the output from a measured one.

  What the refusal lacked was a *name*. It was a bare `ValueError`, which made
  it indistinguishable from "you asked for a calibration that does not exist",
  and that had two costs.

  The first was a batch. `umbra chips --asset SICD --calibrate gamma0` over a
  site's twenty passes ended on the first product whose metadata came up short
  and took the nineteen already chipped with it — the expensive path, one
  whole-product download per scene. `write_chips(skip_unsupported=True)` /
  `umbra chips --skip-unsupported` now catches exactly `UnsupportedMeasurementError`,
  records the pass on `ChipDataset.skipped` as a `SkippedAcquisition` (which
  acquisition, when, and the product's own words for why, plus the hint the
  refusal named) and moves on. So the dataset **states** its hole rather than
  having one: the skips print at the end of a run, ride out in `--json` as
  `skipped_count` / `skipped`, and are absent entirely from a run that skipped
  nothing. Nothing else is caught — a download failure, a missing asset, a
  corrupt product still ends the run, because a batch that swallows unknown
  errors is a batch whose output nobody can trust.

  The second was the error contract. `umbra convert` wraps a `ValueError` as a
  `ClickException`, and `cli.main` only recovers an `UmbraError` from its cause,
  so the most common failure a complex conversion has — "this product cannot be
  calibrated" — was the one that never reached the `--json` error envelope. It
  does now, with a stable `error` name and an actionable `hint` (`Use
  --noise-model estimated ...`, `Ask for one of: sigma0 ...`) on each of the
  five product-metadata refusals.

  The type is a `ValueError` as well as an `UmbraError`, because that is what it
  was before it had a name: every `except ValueError` around a conversion keeps
  working, and the new class is something to catch *more* narrowly rather than
  instead. And it is scoped: a malformed request — an unknown calibration name,
  a percentile outside the distribution, an even filter window — stays a bare
  `ValueError`, since the caller can fix that one where this one is a fact about
  the file.

  Both entry points also now ask the question **before** they read. The checks
  used to happen where the polynomials are first evaluated, which is after a
  multi-gigabyte complex product has been pulled through amplitude detection, so
  a conversion that could never have succeeded still spent the whole read
  finding out. `_check_measurement_support` runs off the metadata immediately
  after the reader opens — the ordering `_check_speckle_window` already had —
  and it calls the same coefficient readers the conversion will, so it cannot
  drift into a second opinion about what a product supports. The inferred noise
  models are deliberately not covered: their floor comes from the scene's own
  pixels, so reading them is exactly what they need to do.
- **Record what a baked preview *is*, so it stops having to be assumed (index
  schema v4: `items.thumbnail_asset` / `items.thumbnail_size`).** The bake stored
  a preview's bytes and nothing about how they were made, which was fine while
  the pixels were only ever *shown* — a gallery tile, a `demo` popup, a
  `/artifacts/thumbnail/{id}.png`. It stopped being fine the moment something had
  to decide whether the cached picture was the *same picture* as the one being
  asked for, and that happened twice.

  `umbra describe --preview baked` hands the preview to a vision model, so it had
  to refuse every asset but `GEC` — including a `umbra index bake-thumbnails
  --asset CSI` someone baked deliberately, which was exactly the picture the
  request wanted. And a sidecar merge had to keep whichever preview arrived
  first, which made the resolution of a scene's preview a fact about the order
  two commands were run in: the published `catalog.thumbs.db` is baked at 128 px
  where `bake-thumbnails` defaults to 256, so `fetch-thumbnails` either kept your
  256 px bake or (with `--overwrite`) replaced it with the smaller published one,
  and neither is what anybody meant.

  So `bake_thumbnails` now records the asset and the size it was asked to render,
  and both halves read it:

  - `CatalogIndex.get_preview()` returns a `BakedPreview` (the bytes *and* what
    they are a picture of) beside the unchanged bytes-only `get_thumbnail()`, and
    `baked_preview_refusal` checks the request against that record. A `CSI` bake
    answers a `CSI` reading, a `GEC` one is refused *naming what it actually is*,
    and `SceneDescription.image.asset` reports the product read rather than the
    one assumed. The `BakedPreviews` seam accordingly returns a record rather
    than bytes, and the preview lookup now happens *before* the refusal is
    computed — which product a preview is of is a fact about that scene, so it
    cannot be known without the read.
  - `import_thumbnails` keeps a local bake unless the incoming one is a larger
    preview of the same product. Where either side is unrecorded the two are not
    comparable and the local bake stays, so nothing about the published sidecar's
    current behaviour changes until it is republished with the record.

  Two things are deliberately left assumed. The *stretch* is not recorded because
  the bake has no say in it — every preview is the decibel one — so `--no-db` is
  still refused without a lookup. And a preview with no record is read as
  "unknown", which falls back to `BAKED_PREVIEW_ASSET`: absence is not a claim,
  and treating it as one is what would let a `v3` index quietly answer a `CSI`
  request with a `GEC` picture.

  The schema step is additive and migrated in place (`v3 -> v4`), so an existing
  index keeps its baked pixels and reports them as unrecorded; the sidecar's two
  new columns are added to a file that lacks them on export and read as absent on
  import. As with every version bump, an index written by this build is refused
  by an older umbra-py with the message that says to upgrade — which is what the
  `PRAGMA user_version` stamp exists to do.
- **Read a scene from the preview this machine already has (`umbra describe
  --preview {render,baked,auto}`).** Every description streamed a fresh
  cloud-optimized GeoTIFF overview from S3, once per call, forever — including
  on the surfaces built so nobody has to install anything (the MCP / LangChain /
  LlamaIndex `describe_scene`), where a hosted server re-read the same handful of
  popular scenes for every client. Meanwhile the picture was already here: the
  thumbnail bake landed, the weekly publish ships a whole-catalog
  `catalog.thumbs.db`, and `umbra index fetch-thumbnails` puts it a command away.
  Nothing connected the two, so the AI reading path spent the one budget this
  project's guardrails single out (Umbra's egress) on bytes it had a local copy
  of.

  `--preview baked` / `auto` (`describe(preview=…, previews=…)`) reads the cached
  quicklook instead. What that buys is not only the range read: the render is the
  *only* reason the C2 capability needed `rasterio`, so a description from a baked
  preview runs on an install carrying nothing but the `[ai]` extra — and runs
  offline.

  It is opt-in, and the reason is the same one the noise and speckle work keeps
  arriving at: it changes the evidence. A baked preview is a 128–256 px
  decibel-stretched `GEC` quicklook where `--max-size` defaults to 1024, so a
  reading of one is not the reading of the other, and a description that does not
  say which cannot be compared with a description that read the other. So the
  default is unchanged (`render`, byte-identical to before), the picture is
  recorded rather than assumed — `SceneDescription.image` / the `"image"` key of
  `--json`: the source, the asset, and the PNG's *real* pixel dimensions, read
  from its header, so it is what the model saw rather than what was asked for —
  and a preview smaller than the render it stood in for adds a caveat saying so,
  deterministically, after the model's own and never asked of it.

  And the cases where the cached picture is not a smaller version of the
  requested one but a *different* one — a non-`GEC` asset, a linear stretch — are
  refused rather than substituted, because the index stores a preview's bytes and
  nothing about how they were made. `baked_preview_refusal` is one function with
  two uses, the shape `umbra serve`'s `stats_option_refusal` established: under
  `--preview baked` its string is the error, under `auto` it is the reason that
  scene gets rendered instead. Each refusal names the fix, and tells the two
  apart that need different ones — no index here (`umbra index fetch` +
  `fetch-thumbnails`) versus this scene not baked in it (`umbra index
  bake-thumbnails`, or `--preview auto`).
- **Average the speckle down on the largest cube that can be built
  (`to_stack(speckle_filter=…, chunk_size=N)`).** The two options that lift this
  library's measurement ceiling were mutually exclusive, and the refusal was
  load-bearing: `chunk_size` is what lets a cube be stacked sharper than one
  scene fits in memory, `speckle_filter` is what stops a per-cell decibel delta
  from being mostly interference, and asking for both raised. So the sharpest
  cube the library could build was also the noisiest one it could measure — and
  on a hosted instance the pair was worse than inconvenient: `"windowed": true`
  *requires* a chunked server, so **every** `umbra serve` honoured exactly one of
  the two, and a client that wanted a filtered measurement of a large series had
  nowhere to send it.

  The two obstacles that made a window the hard place to filter are answered
  rather than approximated — the same pair `umbra chips` answered tile by tile,
  and the pair the old refusal named as what it would take.

  A filter window straddling a chunk edge: every window is now read with a
  half-window **halo** and cropped after filtering, so the cells that survive
  were averaged over the neighbours they have on the pass rather than over a
  truncated window. A filtered chunked cube is therefore the whole-pass filter's
  own answer — to within one `float32` ulp, since a summed-area table reaches a
  window total by a different order of additions when it was accumulated over 12
  cells than over the pass. That is the difference between a window edge and a
  *seam*, and a seam here is not cosmetic: a discontinuity in the smoothing lands
  in `stack_stats` as change, strongest wherever the ground has the most
  structure.

  And `lee`'s speckle parameter, which is a property of the product's processing
  rather than of the few hundred cells one window covers: it is resolved once per
  pass (`_pass_looks`) as a single deferred task every window of that pass depends
  on, so one part of a scene is never smoothed harder than the part beside it. It
  is a *sample* of the pass — a fixed 3×3 grid of 512-cell windows whose blocks
  are pooled before the percentile, exactly as `umbra chips` reads it once per
  acquisition — because a chunked build is by definition the case where the pass
  does not fit in memory. A pass small enough to sample whole gives the identical
  number, and `boxcar` needs no such parameter, so a chunked `boxcar` cube is
  cell-for-cell the unchunked one and costs no extra read at all.

  What this buys is one request. `POST /artifacts/stats` now takes `"windowed":
  true` **and** `"speckle_filter"` together on a chunked instance, so a hosted
  measurement of a series too large to hold can also be the one with the
  interference averaged out of it. The exclusivity is gone from all three places
  it was stated rather than only from the library: `to_stack` no longer refuses
  the pair, `stats_options` no longer refuses it at the request (it was refused
  there precisely because it was unsatisfiable *everywhere*), and
  `stats_option_refusal` has no `speckle_filter` condition left — which the
  landing page's `umbra:options` reports as `supported: true` on every instance,
  by the same one function that raises the refusals, so the advertisement still
  cannot drift from what the endpoint does.
- **Say what a hosted instance can be asked for (`umbra:options` on the landing
  page's `stats` link).** `POST /artifacts/stats` takes two request options
  whose availability is not the request's to decide: `"windowed": true` needs
  the cube built in windows (`umbra serve --stack-lazy --stack-chunk-size N`)
  and `"speckle_filter"` needs each pass whole, so they are exact complements
  and **every instance honours exactly one of them**. Which one was discoverable
  only by sending a request and reading the `400` — the startup echo tells the
  operator, not the caller — which is a poor deal for the client this endpoint
  exists for: an agent or a browser front end that installed nothing locally and
  cannot see the server's flags.

  The landing page now carries the answer. Each option under the `stats` link's
  `umbra:options` reports whether this server supports it and, when it does not,
  the `reason` it would be refused with, beside a `stacking` line naming the
  instance's policy — so a client picks the option that works before spending a
  request, and can tell the operator which flag to change rather than only that
  it was told no.

  The advertisement is the refusal rather than a description of it. Both come
  from one function (`serve.stats_option_refusal`): the renderer raises the
  string it returns and the landing page publishes the same string, so a page
  that claims support the endpoint does not give is not a drift that can happen.
  That tie is checked rather than asserted — for all three instance shapes the
  suite reads the page, then drives the *renderer*: the refused option must
  raise a `ValueError` whose message is character-for-character the advertised
  `reason`, and the supported one must render. Advertising is derived from the
  policy the route already holds, so nothing about the refusals, the cache key
  or the answers changed.
- **Average the speckle down on the way into a training set
  (`umbra chips --speckle-filter {boxcar,lee}` on *any* asset).** The filter
  reached the complex archive (`umbra convert`), the datacube (`umbra stack`)
  and the server, and stopped at the one loader whose output is not a picture or
  a number but a **dataset**: `umbra chips` exposed `--speckle-filter` only on
  the `--asset SICD` path, where it was the conversion's flag, so a chip set cut
  from Umbra's *published* GEC rasters — which is most chip sets — could not be
  smoothed at all. That is the place it matters most and the place it was
  missing: a model trained on single-look tiles is being shown an interference
  pattern whose standard deviation equals the signal's mean, and unlike a caveat
  on a measurement, nothing downstream can put that back.

  `--speckle-filter` now applies to every asset, running wherever it is most
  correct for the one it is filtering. On `GEC` / `CSI` the **tiles** are
  averaged, which is the first (and only) point at which those pixels exist in
  this library. On `SICD` the request is routed into `SicdConversion` and the
  scene is filtered in the radar's own image space before geocoding, where
  speckle is one independent sample per pixel — the same flag, placed where it is
  more correct, rather than a second knob. Naming both, differently, is refused
  rather than silently resolved.

  Two things made the tile loop the hard place to put this, and both are answered
  rather than approximated — the pair `to_stack`'s `chunk_size` refusal named as
  what would be needed. **A window straddling a tile boundary:** each tile is read
  with a half-window halo, filtered, and cropped back, so every chip pixel
  averages the neighbours a whole-scene filter would have given it. That is not a
  close approximation but an identity — a filtered tile is bit-for-bit equal to
  that region of the scene filtered whole, which is what makes two *overlapping*
  tiles (`--stride < --chip-size`) agree about the ground they share instead of
  carrying a seam a model would learn. **`lee`'s speckle parameter:** it is read
  once per acquisition from a fixed 3×3 grid of full-resolution sample windows
  and pooled at the block level before the percentile is taken, so it estimates
  the scene rather than averaging nine estimates of it. Per tile it would have
  made the filter smooth a tile over water differently from the one beside it
  over a city, for no reason in the data; the grid is fixed rather than random so
  a chip set is reproducible, and it is a sample rather than a whole-scene read
  because the chipper's promise is that only the bytes of the tiles cross the
  network. `_filter_speckle` gained the `looks=` parameter that carries it.

  What the filter cost and what it bought are both recorded. Every
  `ChipRecord` carries `speckle_filter` / `speckle_window` — the pair that says a
  chip's *resolution* as opposed to its pixel size — and now
  `speckle_enl_before` / `speckle_enl_after` / `speckle_looks` as well, on
  **either** path: the equivalent number of looks either side of the window,
  which is the only honest answer to whether the resolution it spent bought
  anything (a window averaging N pixels buys fewer than N looks on a product
  sampled finer than it resolves, as Umbra's are). `ChipDataset.speckle` rolls
  them up across the run the way `ChipDataset.noise` already does — per
  acquisition, since the numbers describe the scene each tile was cut from — so
  `umbra chips` prints, and `--json` reports, the median gain plus a count of the
  scenes the window bought little on. Advisory, never a refusal: a scene textured
  everywhere is legitimate imagery, and `lee` leaving it alone is the filter
  working.

  The chips record themselves in `umbra convert`'s own `UMBRA_SPECKLE_*` tags
  rather than a second vocabulary, for the reason the datacube does: a tile whose
  cells were averaged over an N-pixel window *is* an N-window-filtered raster, so
  `to_stack`'s refusal to difference a filtered pass against an unfiltered one,
  the `stack_stats` caveat and `gdalinfo` all work on a chip unchanged. Filtering
  tiles cut from an already-filtered raster is refused rather than composed — two
  averagings leave a resolution neither window names. `min_valid` decides on the
  tile as read, so which tiles a run drops is identical filtered or not, and a
  run that asks for no filter is byte-for-byte unchanged, summary field included.
- **Let a hosted measurement average the speckle down too
  (`"speckle_filter"` on `POST /artifacts/stats`, and on the `stack_stats` agent
  tool).** The filter that shipped last reached the library and the CLI and
  stopped there, so every measurement made *through the server* — the front door
  built precisely so a QGIS user, a browser front end or an OpenAPI-driven agent
  could measure a site without installing anything — was unfiltered, and no
  request could ask otherwise. On a quiet site that is not a missing convenience:
  single-look speckle scatters as widely as its own mean, so a per-cell decibel
  delta between two unfiltered passes is mostly interference, and the endpoint
  whose whole purpose is returning numbers a program can act on was returning the
  noisiest ones the chain can produce.

  A request may now send `"speckle_filter": "boxcar" | "lee"` (with an optional
  odd `"speckle_window"`, default 5) and the option rides straight through to
  `to_stack(speckle_filter=…)` — the same arithmetic, the same shared grid, the
  same power domain, and the same record. Nothing in `stack_stats` changed: the
  cube carries `umbra convert`'s own `speckle_filter` / `speckle_window` keys, so
  the response's `caveats` state the trade (a less noisy estimate of each cell;
  the effective resolution of a window rather than of a cell) with no server-side
  vocabulary of its own.

  Where the option *lives* is the decision this makes. It is a request field in
  the artifact cache key rather than an instance policy like `--stack-lazy`,
  because — like `"windowed"` and unlike the lazy build — it **moves the
  numbers**: a cached artifact whose values depended on a server flag nobody
  could see is the failure mode both rules exist to prevent. And it is the exact
  complement of `"windowed"`: filtering needs each pass whole, so it is refused
  (`400`) on an instance started with `--stack-chunk-size`, where `"windowed"` is
  refused on one *without*. That makes the pair unsatisfiable on every instance,
  so they are refused together at the request rather than one instance at a time;
  an unknown filter name or a window that cannot be centred is likewise a `400`
  from `stats_options` rather than an error raised from inside a datacube build.
  `umbra serve` echoes at startup which of the two an instance can honour, and an
  unapplied window is normalised away so it can never split the cache for an
  artifact it had no effect on.

  The agent surfaces get the same pair (`stack_stats(urls=[…],
  speckle_filter="lee")` on MCP / LangChain / LlamaIndex), where — unlike
  `"windowed"`, deliberately left out because those tools build an eager
  512-pixel cube with no ceiling to lift — it answers a question a model actually
  has: the numbers look noisy against a picture that looks quiet. Validation is
  left to `to_stack`'s own check, which runs before a source is opened, so a
  model that invents a filter name gets the library's message and costs no bytes.
- **Bring the speckle averaging to the products people actually use
  (`umbra stack --speckle-filter {boxcar,lee}` / `to_stack(speckle_filter=…)`).**
  The correction that shipped last is the largest one the pipeline makes and the
  only one that never reached this library's main subject. Speckle — the
  interference pattern coherent illumination makes on a rough surface, whose
  standard deviation equals its mean on a single look — is averaged down by
  `umbra convert --speckle-filter`, which works in the radar's own image space
  and therefore only on complex SICD products. Umbra's **published GEC rasters**
  arrive already geocoded and never go through that pipeline, so the archive this
  library exists to make usable had no way to average speckle at all — and every
  number `stack_stats` reports from a cube of them carried that uncertainty
  undocumented and unaddressed, which is exactly the gap `--noise-model
  estimated` closed for the noise floor.

  `to_stack(speckle_filter=…, speckle_window=…)` closes it one step down the
  chain: each pass is filtered on the cube's **shared grid**, after
  co-registration, in the power domain — where a mean is the surface's
  backscatter rather than the ~2.5 dB-low geometric mean a mean of decibels
  would give. The filters are `convert`'s own (`umbra_py.convert._filter_speckle`
  is the arithmetic, reused rather than reimplemented, so `"boxcar"` and
  `"lee"` cannot come to mean two things): `"boxcar"` averages the window
  unconditionally, `"lee"` only where the window is no more variable than
  speckle alone would explain, so edges and points survive.

  Where it runs is the design, not an accident of plumbing. This is the first
  point at which a source exists on the cube's own grid, so the window averages
  *the cells the cube reports* — which makes the resolution it spends the
  resolution a measurement of that cube quotes. Filtering earlier, where speckle
  is one independent sample per pixel, remains `umbra convert`'s job; a series
  that already records a filter is **refused** rather than filtered twice,
  because two averagings leave an effective resolution neither window names and
  the record could only claim one of them.

  Both halves of the trade are recorded in the keys `umbra convert` already
  writes (`speckle_filter` / `speckle_window`), because they describe the raster
  rather than who made it. So the machinery built for the converted archive
  applies unchanged: `stack_stats` states the trade (a less noisy estimate of
  each cell; the resolution of a window rather than of a cell),
  `stack_to_geotiff` / `umbra stack --out` stamp it into the written cube's
  `UMBRA_*` tags, and `to_stack` refuses to difference a filtered cube against
  an unfiltered pass — the smoothing would read as change, strongest exactly
  where the ground has the most structure.

  Two things it will not do, both self-describing rather than silent: it cannot
  be combined with `chunk_size` (a filter window would straddle two windows read
  independently, and `"lee"` reads its speckle parameter off the whole pass, so
  a chunked cube would stop equalling the unchunked one it is documented to
  match — `lazy=True` alone, one chunk per pass, filters fine and is pinned
  identical to the eager path), and a misspelt filter or an even window fails at
  the call rather than from inside a deferred read. `umbra stack
  --speckle-filter lee --speckle-window 5` is the same on the CLI, in the
  `--json` manifest's parameters and in the file it writes.
- **Average the speckle down, and say what that cost (`umbra convert
  --speckle-filter {boxcar,lee}`).** Every correction the conversion pipeline had
  targeted something the *sensor* added — a geometric brightness swing, an
  arbitrary scale, a thermal noise floor. What is left after all of them is not an
  error at all, and it is larger than any of them: coherent illumination of a
  rough surface interferes with itself, so a single-look pixel's power is
  exponentially distributed about the surface's true backscatter, with a standard
  deviation **equal to its mean**. That is speckle. It is why a single Umbra pixel
  is a poor measurement of a surface even after `--calibrate --subtract-noise`
  made it physical, why a pixel-by-pixel difference between two passes is mostly
  speckle rather than change, and why every SAR workflow averages before it
  measures. Nothing in the library did that averaging, so the pipeline's last step
  produced calibrated numbers whose dominant uncertainty was undocumented and
  unaddressed.

  `speckle_filter=` on `sicd_to_geocoded_cog` / `sicd_to_amplitude_geotiff` does
  it, in the power domain — where speckle's statistics are defined, and where a
  mean is the surface's backscatter rather than the ~2.5 dB-low geometric mean a
  mean of decibels would give. Two filters, because they answer different
  questions: `"boxcar"` averages the `--speckle-window` window unconditionally
  (the multilook — most variance removed for a given window, and blind to the edge
  it averages across), and `"lee"` compares each window's variability against what
  speckle *alone* would produce and averages only where the two agree, so edges,
  points and textured ground survive (Lee 1980). Its speckle parameter is read off
  the scene rather than assumed, and clamped at single-look: no product carries
  fewer looks than one, so a lower read is the estimator meeting texture, and
  believing it would be licence to smooth structure away.

  The filter is opt-in and not defaulted, because what it *spends* is the reason
  to use this archive: a window that averages N pixels reports ground N pixels
  across. So the trade is recorded on both sides. `UMBRA_SPECKLE_FILTER` /
  `UMBRA_SPECKLE_WINDOW` say what was done and are in
  `load.MEASUREMENT_PROVENANCE_KEYS`, so `to_stack` refuses to difference a
  filtered pass against an unfiltered one — or a 3-pixel average against a
  9-pixel one — since the smoothing would otherwise be read as change, strongest
  exactly where the ground has the most structure. `stack_stats` states both
  halves in a caveat (a less noisy estimate of each cell; the resolution of a
  window rather than of a pixel), because which half matters depends on the block
  size being quoted.

  And what the filter *achieved* is measured rather than claimed:
  `UMBRA_SPECKLE_ENL_BEFORE` / `_AFTER` report the scene's equivalent number of
  looks either side of it — the median block's `mean² / variance` of detected
  power, the standard measure of how much speckle is left. That number is the one
  the window size cannot supply: a 5×5 boxcar averages 25 *pixels* but only as
  many independent *looks* as the product's sampling provides, and Umbra samples
  finer than it resolves, so the achieved ENL lands below the pixel count. `umbra
  convert` prints the pair (`Equivalent looks 1.0 -> 12.7, of 25 pixels
  averaged`) and, under `SPECKLE_ENL_GAIN_WARN`, says the window bought little and
  why that can be the honest outcome rather than a fault. The estimator is
  calibrated in `tests/test_convert.py` against synthetic single-look imagery,
  where speckle can be *made* rather than faked: it reads 1.0 on unfiltered
  imagery and within 10 % of N² after an N-pixel boxcar, which is what the
  block sizing (`_ENL_BLOCK_WINDOWS`) exists to keep true — a block only a couple
  of windows across holds too few independent samples to divide by, and reads
  15–25 % high. Structure biases it *down*, so it is a floor on the looks present
  rather than a claim about them.

  `umbra chips --speckle-filter` carries the same to a training set, where the
  trade is a different one: a single-look chip teaches a model the interference
  pattern as much as the surface, and a filtered chip teaches it a surface at
  coarser resolution. Because that decides what a model can learn to see, every
  `ChipRecord` carries `speckle_filter` / `speckle_window` — read back from the
  geocoded raster's own tags, so the manifest reports the processing rather than
  the request — and both are part of `SicdConversion.cache_key`, so a filtered
  conversion never stands in for an unfiltered one in `--work-dir`.

- **Measure the inferred noise floor against a measured one
  (`compare_noise_models` / `umbra convert --noise-check`).** The two inferred
  noise models shipped on an argument. `--noise-model estimated` reads a scene's
  low power tail as its receiver floor and `estimated-range` fits that read per
  range line, and both were justified in prose: a percentile of a speckled
  noise-only population sits *below* that population's mean, so the estimate is
  biased low but consistently so; the per-line fit therefore recovers the
  across-swath **shape** a single scalar cannot. Neither claim could be checked
  on the archive the models exist for, because a product that states no floor
  states no truth either — so the whole chain rested on reasoning nobody could
  put a number to.

  `compare_noise_models(src)` supplies that number where a product *does* declare
  an `ABSOLUTE` noise level. It runs the inferred models over the product's own
  pixels, evaluates its `NoisePoly` over the same grid, and differences the two
  in decibels — split into exactly the two parts the claims are about:
  `bias_db`, the median offset (expected negative, because under-subtraction is
  the safe direction), and `shape_error_db`, the RMS error *after* granting that
  offset, which is how well the inferred floor follows the real one across the
  image. `measured_spread_db` is the premise beside them: how much swing was
  there to find at all. Nothing is written and no conversion runs — this is the
  measurement, not a correction — and `bbox=` compares over one window, with the
  `NoisePoly` evaluated at the image coordinates that window actually occupies,
  without which the truth would be read off the wrong part of the swath.

  On a synthetic SICD whose stated floor *is* the floor its pixels were built
  from (`tests/test_convert.py`), the answer is decisive: against a floor ramping
  10 dB across the swath, the fitted profile recovers the swing to 0.1 dB and
  scores a shape error of 0.2 dB where the constant estimate scores 2.9 dB —
  which is precisely the ramp's own deviation about its midpoint, i.e. everything
  a scalar had to leave behind. Both models read low by about 5.5 dB, within
  1 dB of each other, exactly as a fifth percentile of an exponentially
  distributed noise-only population should.

  Two things the measurement found that the argument had not. A constant estimate
  is biased low on a *varying* floor even with no speckle at all, because a
  percentile pooled over the whole scene lands near the near-range end of a ramp
  rather than at its middle — a second error, separate from the shape error, and
  now reported apart from it. And where backscatter sinks toward the floor (land
  returning about what the receiver does at far range), the fitted profile reads
  the swing ~30% flat: the low tail stops being a separate population at the far
  edge before it does at the near edge. The subtraction stays conservative in
  both cases, but `UMBRA_NOISE_FLOOR_SPREAD_DB` understates on such a scene, which
  is a thing to know before quoting it.

  `umbra convert SRC --noise-check` prints the comparison as JSON (no `DST`, like
  `--provenance`), honours `--clip-bbox`, and refuses on a product with no
  absolute level by naming what it does carry — the same refusal `--noise-model
  measured` makes, for the same reason. The estimators' percentile is exposed as
  a keyword *here and nowhere else*: this is the surface where the number is
  being measured rather than trusted, so sweeping it is the point.
- **Say which scenes in a training set the noise estimate should not have been
  trusted on (`umbra chips`).** Every noise subtraction already measured its own
  two limits on the scene it ran on — `UMBRA_NOISE_FLOORED_FRACTION`, how much of
  the image the floor drove to the sensor's sensitivity limit, and
  `UMBRA_NOISE_FLOOR_MARGIN_DB`, how far the scene's median power sat above an
  *inferred* floor, which is the estimator's own assumption turned into a number.
  `umbra convert` prints both for the one raster it writes. A chip run converts
  *many* — twenty passes over a site is an ordinary dataset build — and there the
  numbers reached the chip GeoTIFFs' tags and stopped: nothing in the manifest, no
  line on the way out. A training set could therefore contain a handful of scenes
  whose dark tail was ground rather than receiver, with the evidence sitting
  unread inside the files.

  Two surfaces, because the question has two audiences. Each `ChipRecord` now
  carries `noise_floored_fraction` and `noise_floor_margin_db`, read back from the
  converted raster's own tags like `calibration` and `noise_subtraction` beside
  them, so a loader can drop the affected scenes with a one-line manifest filter
  and never open a raster. And the run reports one `NoiseSummary` roll-up —
  scenes, models, how many reported a margin, how many sat under
  `NOISE_MARGIN_WARN_DB`, the narrowest margin and the worst floored fraction —
  on `ChipDataset.noise`, in the `--json` payload, and as up to three lines on the
  way out (*"noise floor: estimated-range, subtracted on 22 scene(s)"*, and where
  it applies, *"2 of 22 scene(s) had under 6 dB of margin"*).

  It is counted per **acquisition**, not per chip: the diagnostics describe the
  scene a chip was cut from, so every chip of one pass repeats them and counting
  chips would weight a wide scene more heavily than a narrow one for no reason.
  The summary is derived from the records rather than accumulated during the run,
  so it cannot disagree with the manifest beside it, and it is absent entirely
  from a run where no floor came off — a `GEC` dataset's output is unchanged by
  this existing. The advisory stays an advisory, as it is per scene: a uniformly
  bright scene is legitimate imagery, and the honest fix where the margin matters
  is `--noise-model measured`, not a differently-tuned guess.
- **Refuse to *measure* change between passes converted differently
  (`render_change_png` / `umbra change --narrate`).** `to_stack` already reads
  each source's `UMBRA_*` conversion record and refuses a datacube whose slices
  disagree on what a pixel value is, because differencing a calibrated pass
  against an uncalibrated one puts the gap between the two *conversions* on the
  time axis and reports it as change on the ground. The composite path was
  exempt from that rule on the argument that a mixed picture is merely confusing
  to look at — but one caller on that path does not make a picture. `umbra change
  --narrate` divides the co-registered scene into a grid and quotes a signed
  **decibel** delta per block, ships it as a JSON sidecar the module's own
  docstring calls auditable, and hands it to a vision model as the ground truth
  it is told not to contradict. Every one of those numbers was a difference of
  two rasters nothing had checked.

  `_coregister_bands` now returns each source's conversion record alongside the
  bands and bounds. It is collected there because that is the only place it is
  free — the datasets are already open, and re-opening a remote COG to ask what
  its pixels mean would cost a second round of range requests, which is exactly
  the reason `conversion_provenance` exists as a separate function from
  `read_conversion_tags`. `render_change_png` checks it through the same
  `load._shared_provenance` the datacube uses, on the same
  `MEASUREMENT_PROVENANCE_KEYS` (`calibration`, `noise_subtraction`, `rtc_model`,
  `scale`, `units`), so a mixed pair raises before a single number is computed
  and before the model call that would quote it. The refusal names the key, both
  values and an acquisition on each side; a converted raster mixed with a
  published GEC is caught too, since an untagged raster is its own value.

  The check stops there deliberately: `change_composite`, `timescan_composite`,
  `change_animation` and `swipe_map` take the third return value and ignore it.
  They make pictures, and the project's existing polarization rule already draws
  that line — `POST /artifacts/stats` refuses a mixed-polarization *selection*
  while the picture endpoints tolerate one. This is that line applied to what the
  pixel values are rather than to how they were received.

  What the sources agree on is carried rather than dropped. `ChangeStats` gains a
  `provenance` field that reaches `to_dict()`, so the narration sidecar says
  whether its decibels are a calibrated coefficient or relative amplitude, and
  `build_narrate_messages` puts the record in the model's ground-truth block as
  `pixel_values` — only when there is one, so the prompt for the usual case
  (published GEC products, which umbra-py did not convert) is byte-identical to
  before. `_shared_provenance` gained one keyword, `action`, so the refusal says
  "measure change between" rather than "stack"; the reason a mix is not a
  measurement is the same either way.
- **Let the inferred noise floor follow the swath: `umbra convert --noise-model
  estimated-range` / `sicd_to_geocoded_cog(noise_model="estimated-range")`.** The
  entry below shipped an estimated floor with two named limits, and the first of
  them — *one constant, so it cannot follow the across-swath variation* — is not
  a rounding error. A receiver's sensitivity varies with range; the measured
  floor is a polynomial for exactly that reason. Subtracting a scalar from a
  scene whose floor spans several decibels therefore under-subtracts at one edge
  of the swath and over-subtracts at the other, leaving behind a gradient that
  tracks the geometry rather than the ground — which is the artefact the
  subtraction exists to remove, reintroduced by the model that made it usable on
  Umbra's open archive.

  `"estimated-range"` takes the same low-tail read **per range line**. SICD
  stores range along the image rows (`Grid.Row` is the range direction), so a row
  is a set of azimuth samples at one range and its own fifth percentile is the
  receiver at that range. What makes a per-line read usable on a real scene is
  that the profile is a **fit** rather than a lookup: a degree-2 polynomial in
  the row coordinate, which interpolates over the lines that had no dark ground
  to read, and which is then redone without the lines sitting more than 3 dB
  *above* it. That trim is deliberately one-sided — ground contamination can only
  push a line's low tail up, never down, so a line far above the curve is one the
  estimator could not read while a line far below it is noise-only and is exactly
  what should be believed.

  What it adds is the *shape*, and the entry is careful to claim only that. A
  percentile of a speckled noise-only population sits below that population's
  mean by an amount the percentile sets, so both inferred models read a floor
  that is conservatively low — and because that bias is very nearly the same
  decibel offset on every line, it lowers the fitted curve without bending it.
  Under-subtraction is the safe direction (it leaves a little of the receiver in
  rather than taking real backscatter out), and it is the gradient, not the
  offset, that a constant floor puts into a scene.

  It is recorded as a **third** thing rather than as a better `"estimated"`:
  `UMBRA_NOISE_SUBTRACTION` reads `"estimated-range"`, which — since that key is
  in `load.MEASUREMENT_PROVENANCE_KEYS` — is what makes `to_stack` refuse to
  difference a fitted profile against a constant guess, exactly as it already
  refuses an inferred floor against a measured one. `stack_stats` gets its own
  caveat, because reusing the constant model's wording would understate one limit
  (it *does* follow the swath now) and overstate the other (it still assumes each
  pass contained dark ground somewhere along range). The new
  `UMBRA_NOISE_FLOOR_SPREAD_DB` reports the peak-to-peak swing of the fitted
  floor — the number that answers "was there anything here for the constant model
  to have missed?", and the one `umbra convert` prints beside the existing two
  diagnostics. It stays off the constant estimate (whose spread is zero by
  construction) and off the measured floor (whose variation is the product's own
  metadata), so a tag's presence means something. `umbra chips --noise-model
  estimated-range` carries the same to a training set, where the flat floor was
  visible as an offset between chips cut from opposite edges of one swath; the
  chip cache key already covers the model, so a re-run cannot reuse the constant
  model's product.
- **Make the noise subtraction say what it did to the scene:
  `UMBRA_NOISE_FLOORED_FRACTION` and `UMBRA_NOISE_FLOOR_MARGIN_DB`.** The two
  entries below shipped a correction with two honestly-stated limits — it clamps
  every pixel the floor meets or exceeds, and the estimated model assumes the
  scene contained dark ground to read at all — and stated them in a docstring.
  On any *particular* conversion, neither was visible. A scene where the
  subtraction floored half the image and one where it floored nothing produced
  the same output, the same tags and the same line on the way out; so did a scene
  with 30 dB of water under its buildings and one that was uniformly bright,
  where the fifth percentile taken off is not the receiver but the ground.

  Both numbers were already being computed and thrown away. `_subtract_noise`
  compares power against the floor, so it knows exactly how many finite pixels
  landed on `_NOISE_RESIDUAL_FLOOR` — the "how much of this image is at the
  sensor's sensitivity limit?" fraction, which is unrecoverable afterwards
  because a floored pixel and a genuinely floor-valued one are the same value
  once written. `_estimate_noise_power` has the scene's whole finite power
  distribution in hand to take a percentile of, so the median it needs to be
  compared against costs one more pass over an array that already exists. Both
  now come back in a `NoiseSubtraction` record and into the raster's own
  provenance: `UMBRA_NOISE_FLOORED_FRACTION` for either model, and
  `UMBRA_NOISE_FLOOR_MARGIN_DB` — how far the scene's median power sits above the
  inferred floor — for the estimate.

  The margin is the estimator's own assumption made checkable. It works because a
  SAR scene's dark surfaces are a *different population* from its ordinary
  backscatter, so the distance between the fifth percentile and the median is the
  evidence that they were: wide, and the low tail was noise; narrow, and the
  scene had no noise-dominated population and the subtraction removed signal.
  `umbra convert` prints both diagnostics and, below `NOISE_MARGIN_WARN_DB`
  (6 dB — a factor of four in power), says the scene had little dark ground to
  read and points at `--noise-model measured`. It stays an **advisory, never a
  refusal**: a uniform scene is a legitimate scene, "how bimodal is this image?"
  is a heuristic, and the honest fix where it matters is a measured floor rather
  than a differently-tuned guess. The CLI reads the numbers back out of the file
  it just wrote rather than taking them through a return value, so what is
  printed is exactly what the raster will still say tomorrow.

  They are diagnostics of a scene, not claims about what a pixel value *means*,
  which is why they are deliberately **not** in
  `load.MEASUREMENT_PROVENANCE_KEYS`: no two real passes agree on how much of
  each was floored, so recording them there would have ended every series
  `to_stack` could build. They are carried into a cube's `attrs["provenance"]`
  only under the existing "every source agrees" rule, so a stack quotes them when
  they are the same number and stays silent rather than quoting one pass's for
  the whole series.
- **Make the noise correction reach the archive it was built for: `umbra convert
  --noise-model estimated` / `sicd_to_geocoded_cog(noise_model="estimated")`.**
  The noise-floor subtraction below reads the product's own
  `Radiometric.NoiseLevel` polynomial — the right number, and one Umbra's open
  products do not have. They generally ship with no `Radiometric` block at all,
  so `--subtract-noise` raised a clean, correct, useless error on essentially
  every scene in the open archive: a shipped correction that could not be
  applied to the data this library exists for.

  `--noise-model` names where the floor comes from, the same shape `--rtc` /
  `--rtc-model` already has. `measured` (the default, and exactly the previous
  behaviour) reads the product's metadata. `estimated` infers the floor from the
  scene: a SAR image's darkest surfaces — calm water, radar shadow, smooth
  ground — return essentially nothing, so what is recorded there is the
  receiver, and the low tail of the image's own power distribution *is* the
  noise floor. `_estimate_noise_power` takes its 5th percentile (low enough that
  ordinary land backscatter sits well above it, high enough not to land in the
  speckle tail of the darkest pixels — the floor wanted is the mean power of the
  noise-dominated population, not its minimum), in the linear power domain
  whichever scale it was handed, ignoring the warp's nodata. Everything
  downstream is unchanged: the same `_subtract_noise` in the same position,
  first and on raw detected power, because what differs between the two models
  is the provenance of the number and not the physics.

  The trade is real, so it is named rather than smoothed over. The estimate is
  one scalar, so it cannot follow the across-swath variation a `NoisePoly`
  describes; and it assumes the scene contains dark ground at all — over imagery
  that is bright everywhere, the 5th percentile *is* ground and subtracting it
  removes signal. Which is why the inference is never allowed to wear a
  measurement's clothes. `UMBRA_NOISE_SUBTRACTION` records `"estimated"`, a
  distinct value from the measured floor's `"absolute"` (unchanged, so rasters
  converted before this option existed still compare equal to ones converted
  after it), and the level it inferred rides along in a new
  `UMBRA_NOISE_FLOOR_DB` tag — for the same reason `RTC_REFERENCE_DEG` exists,
  since an inferred number nobody can read back is not reproducible. Because
  `noise_subtraction` is already in `load.MEASUREMENT_PROVENANCE_KEYS`, that one
  distinct value is what makes `to_stack` **refuse** a series that differences an
  inferred floor against a measured one — a mix where the coarse "was noise
  subtracted?" question agrees and the gap between the two numbers would land on
  the time axis as change. `stack_stats` adds a second caveat naming the
  estimate's two limits when a cube carries one, `umbra convert` says
  "noise-estimated" rather than "noise-subtracted" on the way out, and `umbra
  chips --noise-model` carries the whole thing to a training set (it is part of
  `SicdConversion.cache_key`, so a `--work-dir` run that estimated the floor
  never hands back the COG a run that measured it left behind).

- **Subtract the sensor before measuring the ground: `umbra convert
  --subtract-noise` / `sicd_to_geocoded_cog(noise_subtract=True)`.** A
  calibrated pixel was a physical number and, over a dark surface, the wrong
  one. What a radar records is the ground's echo **plus** its own receiver
  thermal noise, and the two add in power; over calm water, radar shadow, wet
  snow or dry sand the second term is most of the total. Scaling that sum by the
  product's calibration polynomial produced a value that was precise, physical
  and a report of the *sensor's sensitivity* rather than of the scene — and
  because the noise floor varies across the swath, it put a gradient in the
  answer that tracked the geometry instead of the ground. Every low-backscatter
  measurement the pipeline could make was floor-limited, silently.

  Now the floor comes off first. `_noise_coefficients` reads the SICD's own
  `Radiometric.NoiseLevel.NoisePoly` — the thermal-noise power in dB as a
  function of image coordinates — `_noise_power` evaluates it per pixel and
  converts to the linear domain, and `_subtract_noise` takes it off the detected
  power. Placement is the whole design: the flattening and the calibration are
  multiplicative power-domain factors, so they commute with each other and with
  the warp, while this one is *subtractive* and is therefore applied first, in
  image space, on raw detected power, before either scales what is left. Getting
  that order wrong (`scale × power − noise` instead of `scale × (power −
  noise)`) yields another plausible-looking raster, which is why a test pins the
  result against both expressions and asserts it is the first.

  The polynomial is evaluated at the image coordinates the pixels actually came
  from (`origin=` on `_denoise_amplitude`, exactly as for the scale factors), so
  a `--clip-bbox` window gets the floor its own part of the swath had. Where the
  estimated noise meets or exceeds the measured power the residual is floored
  rather than driven negative: those pixels are at the sensor's limit, which is
  a statement about the radar and not a measurement of the ground, and the floor
  is the same "as dark as this raster goes" value the log scale already clamps
  at. Nodata stays nodata.

  A subtraction is only as real as the metadata behind it, so the refusals are
  the other half of the feature. Only an `ABSOLUTE` `NoiseLevelType` can be
  subtracted; a `RELATIVE` one describes how the floor *varies* without stating
  what it is, and subtracting it would mean inventing the absolute offset —
  invisible in the output and indistinguishable from a real correction. That,
  a missing `NoiseLevel`, a missing `NoisePoly` and a product with no
  `Radiometric` block at all each raise a self-describing error naming what the
  product does carry, and `sicd_noise_level(path)` answers the same question
  ahead of time (the role `sicd_calibration_types` plays for the scale factors).
  As with calibration, Umbra's open products generally carry none of this, so
  the honest answer there is the error rather than a number.

  It composes with everything already in the pipeline: `--rtc-model facet
  --calibrate gamma0 --subtract-noise` is a terrain-flattened gamma-nought
  coefficient with the receiver's own floor removed. It reaches the ML on-ramp
  too — `umbra chips --subtract-noise` (`SicdConversion.noise_subtract`, part of
  the conversion's cache key) — so a training set over water or shadow teaches a
  model the ground rather than the sensor.

  And it is recorded, because a noise-subtracted raster and a raw one are
  pixel-for-pixel indistinguishable after the fact: `UMBRA_NOISE_SUBTRACTION` is
  written into every raster the module emits (`"absolute"` or `"none"`, read
  back by `read_conversion_tags` / `umbra convert --provenance` / `gdalinfo`),
  chips carry it into both the manifest (`ChipRecord.noise_subtraction`) and the
  tile's own tags, and it is now one of `load.MEASUREMENT_PROVENANCE_KEYS` — so
  `to_stack` refuses a series that subtracted the floor from some passes and not
  others, the same way it already refuses a mixed calibration. Over a dark cell
  that mix *is* the difference between the two passes, so it would have been
  reported as change on the ground. A cube that did subtract it says so in
  `stack_stats`' caveats.

  One compatibility rule came with that new key: a raster carrying umbra-py
  provenance but no `NOISE_SUBTRACTION` tag — converted by an earlier version,
  which had no such step and therefore did not run it — now reads as `"none"`
  rather than as the `(unrecorded)` sentinel, so adding a measurement key does
  not retroactively split a series that agrees. A raster with *no* umbra-py
  record at all (every published GEC) is still `(unrecorded)`: there the silence
  is about the whole conversion rather than one step of it.
- **Convert (and chip) the area of interest, not the whole collect:
  `umbra convert --clip-bbox` / `sicd_to_geocoded_cog(bbox=…)`.** The SICD
  pipeline was all-or-nothing. `sicd_to_geocoded_cog` opened the product, read
  `reader[:, :]` — every complex sample — detected amplitude over all of it,
  calibrated all of it, projected a control lattice across all of it, warped all
  of it and wrote all of it, and there was no way to ask for less. That is the
  right default when the scene *is* the subject, but the common case is a site:
  a few hundred metres of ground inside tens of square kilometres collected at
  16–25 cm. Keeping that corner cost the whole scene twice over — a scene-sized
  complex array and a scene-sized float raster resident, a scene-sized warp, and
  a scene-sized COG on disk — which on a full-resolution product is the
  difference between a laptop finishing and a laptop swapping. `umbra chips
  --asset SICD` inherited the same bill per acquisition, so building a training
  set over one site converted every pass whole to keep a fraction of each.

  Now the ground rectangle is turned back into the image window that covers it.
  `_clip_window` projects a lattice to ground with the same model the control
  points use and keeps every lattice **cell** whose corner extent touches the
  request — cells rather than points, so an area smaller than one lattice step
  is still found — then pads by one step. Only that window is read from the
  product (`reader[row0:row1, col0:col1]`), and everything downstream is sized
  to it: the control points are projected at the scene coordinates the window
  really occupies but labelled with the array's own rows and columns (`origin=`
  on `_build_gcps` / `_build_gcps_dem`), the radiometric scale-factor
  polynomials are evaluated at those same image coordinates (`origin=` on
  `_calibrate_amplitude` — the correction `ImageData.FirstRow` already makes for
  a chipped *product*, applied to a chip this library cut itself), `--dem auto`
  fetches the tiles covering the window rather than the scene, and the geocoded
  output is cropped to the request (`bounds=` on `_warp_gcps_to_cog`,
  intersected with the control-point extent so asking for more ground than the
  scene holds returns the overlap rather than a nodata margin). The pixel size
  is still derived from the whole input, so a clip chooses *which* ground is
  written and not how finely it is sampled — the pixels are the pixels the
  whole-scene conversion would have produced there, which the tests pin by
  geocoding a marked scene both ways and checking the mark lands in the same
  place. A bbox that misses the scene is a self-describing error naming the
  scene's footprint, not an empty raster.

  The clip is deliberately **not** recorded in the `UMBRA_*` provenance tags:
  the output's own geotransform states exactly which ground it covers, and the
  tags say what a pixel value *means* rather than where it is — so a clipped and
  an unclipped conversion of the same site still agree on every measurement key
  and stack together under the `to_stack` provenance rule.

  The window search is a superset by construction (the smallest axis-aligned
  image rectangle containing a rotated ground region, padded), and it runs on
  the flat-earth projection even when a DEM is given — terrain moves a point far
  less than the padding, and being generous costs a few image columns while
  being tight would silently clip the edge of what someone asked for.
- **`umbra chips --clip-bbox`: chip a site, not a scene.** The same flag, the
  same lon/lat convention and the same distinction from `--bbox` that `umbra
  stack --clip-bbox` already established (`--bbox` filters which acquisitions
  the search returns; `--clip-bbox` restricts what is read out of each one).
  `chip_item(bbox=…)` / `write_chips(bbox=…)` tile only that window and number
  each chip's `row`/`col` from its corner, for every asset. On `--asset SICD` it
  is *also* the conversion's clip (`SicdConversion.bbox`, part of the
  `cache_key` so a clipped COG never stands in for a whole-scene one in
  `--work-dir`), which is where the cost of chipping the complex archive
  actually lives: one flag, one decision, applied both to the tiling and to the
  geocode that feeds it. The whole-product download is unchanged — a slant-plane
  NITF has no map grid to range-read — so the saving is in the processing and
  the disk, which is stated rather than implied.
- **The measurement chain reads the provenance it writes: `to_stack` refuses to
  mix conversions, and a cube says which one made it.** `umbra convert` has
  stamped `UMBRA_*` GeoTIFF tags into every raster it writes since the
  provenance PR — calibration, RTC model and resolved reference angle, DEM/geoid,
  projection, amplitude scale — and `read_conversion_tags` / `umbra convert
  --provenance` read them back. Nothing *acted* on them. That left the gap the
  tags were written to close still open one step downstream: two rasters
  converted with different settings are pixel-for-pixel indistinguishable, so
  `to_stack` would happily co-register a terrain-flattened gamma-nought pass
  against a raw one and put the difference between the two **conversions** on
  the time axis, where `stack_stats` reports it as change on the ground. The
  numbers looked exactly like a measurement.

  Now the sources are asked. Every dataset `to_stack` opens is read for its
  conversion record (`conversion_provenance`, the parsing half of
  `read_conversion_tags` split out so an already-open dataset needn't be
  re-opened over the network), and a disagreement on any of
  `MEASUREMENT_PROVENANCE_KEYS` — `calibration`, `rtc_model`, `scale`, `units` —
  is a `ValueError` **before any warping**, naming the key, both values and an
  acquisition standing for each side. A raster with no umbra-py tags at all is
  its own value (`(unrecorded)`), so stacking a converted product against a
  published GEC is caught too; a series of published GECs all agree on it, which
  is why the ordinary path is unaffected. The keys deliberately left out are the
  ones that *should* vary per pass: `source` (a different scene each time) and
  `rtc_reference_deg` (each scene's own resolved incidence angle). It is the
  same "a mixed selection is not a measurement" rule `POST /artifacts/stats`
  already applies to polarization — and because that endpoint already turns a
  `ValueError` from a render into a `400`, the refusal reaches HTTP (sync and
  async job paths alike) with no change to `serve.py`.

  What the sources *agree* on is carried forward rather than discarded.
  `to_xarray` surfaces a single raster's record as `attrs["provenance"]` —
  exactly what `read_conversion_tags` reads off that file, so the two answers
  cannot drift — and `to_stack` carries the shared record onto the cube. From
  there it propagates: `to_geotiff` and the datacube writer stamp it back as
  `UMBRA_*` tags, so a derivative answers "what is a pixel here?" like its
  sources did, and `stack_stats` reports it as a `provenance` key and lets it
  correct the two caveats that are claims about the pixel values — a calibrated
  cube no longer tells you its decibels are relative, and a terrain-flattened one
  says the terrain component of the look geometry was flattened rather than
  implying nothing was. The key is **absent**, not empty, when there is nothing
  to report, so its absence reads as "these are the published products as
  delivered" rather than "unknown". `umbra stack --stats`, the `stack_stats`
  agent tools and `POST /artifacts/stats` all return the enriched summary for
  free.
- **ML training data from the *complex* archive: `umbra chips --asset SICD`.**
  `umbra chips` cut tiles from the amplitude products only — `CHIPPABLE_ASSETS`
  was `("GEC", "CSI")`, and the module said so in as many words: the complex
  products "live in the slant plane and are not display rasters, so chipping
  them makes no sense." That was true when it was written. It stopped being true
  when `umbra convert` shipped: SICD → geocoded COG, with DEM orthorectification,
  four terrain-flattening models and radiometric calibration. The gap left
  behind was that the toolkit could turn a complex product into a physical,
  map-ready raster, and separately cut training tiles — but not both, so a model
  built on Umbra data was built on the *derived* products and never on the
  full-resolution ones that are the point of 16–25 cm SAR.

  `--asset SICD` closes it by composing the two rather than reimplementing
  either. Each acquisition is fetched, geocoded through
  `sicd_to_geocoded_cog`, and then chipped by the **same window loop** that
  reads a GEC — one new context manager (`_chip_source`) decides what the loop
  opens, and nothing about a chip's shape, filtering, manifest or naming
  changes. Because the conversion is that conversion, `--dem` / `--geoid` /
  `--rtc` / `--rtc-model` / `--rtc-ref-angle` / `--calibrate` /
  `--convert-resolution` / `--resampling` all apply, so
  `--rtc-model facet --calibrate gamma0` produces chips carrying a terrain-
  flattened **gamma-nought** backscatter coefficient — a number that means the
  same thing in two scenes taken from different angles, which is the difference
  between a model that transfers and one that memorises brightness. The library
  handle is `SicdConversion`, a frozen dataclass whose fields are passed
  straight through; the one option deliberately withheld is `decibels`, because
  the chipper's own `db` flag already chooses the scale and letting the
  conversion choose it too would mean two paths and a calibrated chip whose
  decibels were the decibels of something else.

  **The provenance is read back, not remembered.** Each record's `calibration`
  and `rtc_model` come from the converted raster's own `UMBRA_*` tags, so the
  manifest reports the processing that ran rather than the processing that was
  asked for, and a step that did not run is `null` rather than the string
  `"none"` the tags use (one translation, in one place). The whole tag set is
  copied into every chip GeoTIFF, so a tile says what its pixel values are
  without the manifest beside it — `conversion_tags`' rule, applied to the tile.
  A GEC chip carries no such tags and both fields stay `null`: a published
  product is not something this library made, and the absence is the answer.

  **The cost is stated rather than hidden.** Unlike the GEC path — where only a
  tile's bytes cross the network — a SICD has no map grid to range-read, so the
  product is downloaded whole before anything can be cut from it. So the mode is
  opt-in, one scene is resident at a time (a temporary directory, removed after
  the acquisition), and `--work-dir` keeps both the download and the geocoded
  COG: a re-run reuses a scene already converted **with the same settings**,
  keyed by a digest of `SicdConversion`, so changing `--calibrate` renames the
  cache entry rather than silently chipping the previous product. Both halves
  are resumable — `download_asset` resumes a partial NITF, and an existing COG
  is not rebuilt. Passing a conversion flag with an amplitude `--asset` is a
  usage error, not a silent no-op, since a quietly ignored `--calibrate` yields
  an uncalibrated dataset its owner believes is calibrated.

  The download-and-geocode step is an injectable `preparer` (the seam
  `describe` / `narrate` use for their renders), so the whole path — grid,
  provenance read-back, work-dir lifetime, cache key, CLI wiring and the
  amplitude-asset refusal — is offline-tested in `tests/test_chips.py` with no
  `sarpy`, no network and no multi-gigabyte NITF. No model is called. Needs the
  `convert` extra alongside `load`.
- **The windowed reduction reaches the hosted instance: `"windowed": true` on
  `POST /artifacts/stats`.** `stack_stats(windowed=True)` (below) lifted the
  measurement's memory ceiling for a local cube, and `umbra serve --stack-lazy
  --stack-chunk-size N` (below) lifted the *build*'s for a hosted one — but the
  server still reduced what it had carefully streamed a whole slice at a time,
  so an instance chunked to hold a scene in pieces re-materialised each pass to
  measure it. The callers that ceiling binds are the ones who installed nothing
  locally, which is the reason the endpoint exists.

  **The decision this had been waiting on is which side of the request boundary
  it sits on, and it is the opposite side from `--stack-lazy`.** That one is an
  instance policy deliberately kept *out* of `artifact_cache_key` because it
  cannot move a figure. This one estimates the percentiles it no longer holds a
  pass for — so it is a **request option** (`stats_options`), which puts it in
  the cache key for free: two clients asking different questions of the same
  passes get different cache entries, and no cached artifact's quantiles depend
  on a server flag nobody can see. The failure mode named when this was deferred
  is the one that decided it.

  Everything that is a count or a sum stays exact — pinned end to end through
  the endpoint's own renderer, not just the library: the per-pass means, spreads
  and valid-cell counts, every change record and the whole `spatial` breakdown
  are identical to the exact reduction's, and only `median` / `p5` / `p95` move,
  by at most one 0.05 dB bin. `quantile_method` / `quantile_bin_db` and the
  caveat travel with the JSON, so a client can tell the two kinds of number
  apart without knowing how the server was started.

  It needs windows to walk, so on an instance without `--stack-chunk-size` it is
  **refused** (`400`, naming the flag) rather than silently answered with worse
  percentiles and identical memory — the same "a flag that cannot do what it
  says is an error, not a no-op" rule that makes `chunk_size` without `lazy` and
  `block_series` without `blocks` hard errors. The refusal lands before the
  `load` import, so it costs a status code and not an extra. `umbra serve` now
  echoes the capability at startup when the instance is chunked, since the
  policy became client-visible the moment it gated a request field. Offline-
  tested in `tests/test_serve.py`.
- **The last of the datacube's memory ceiling, on the side that *measures* it:
  `stack_stats(windowed=True)` / `umbra stack --stats-windowed`.** `lazy=True`
  and then `chunk_size=N` took the ceiling off building and *writing* a cube —
  a series is fetched a pass at a time, and a pass a window at a time, so
  neither the number of acquisitions nor the size of one scene sets how much
  archive can be stacked sharp. The reduction that *reads* the cube did not
  move with them: `stack_stats` calls `cube.isel(time=i).values`, so measuring
  materialised a whole `max_size²` slice per pass. A cube could be stacked
  sharper than you could measure it, which is the wrong way round — the
  measurement is the answer, the file is the intermediate.

  `windowed=True` turns the walk inside out: the outer loop is one window of the
  shared grid — the cube's own chunks — and the series is walked *inside* it, so
  what is resident is three windows rather than three slices. Peak memory
  follows `chunk_size`, and a cube too big to hold a slice of is now measurable
  and not only writable.

  **The trade is named rather than hidden**, because this is the one place in
  the chain where streaming cannot be free. Every count, mean, standard
  deviation and change number is still **exact**: they are counts and sums, so a
  window folds into an accumulator (`_PairAccum`, which `_pair_change` and the
  per-block breakdown now both go through, and `_DistAccum`, which merges spread
  with Chan's parallel-variance update rather than recomputing it). A
  *percentile* is the exception — it needs the whole distribution, which is
  exactly what a window-by-window walk never has — so each pass's `median` /
  `p5` / `p95` become estimates from a mergeable 0.05 dB histogram
  (`_QuantileSketch`), good to about one bin. The axis is decibels whatever the
  cube holds, because a fixed-width dB bin is a fixed *ratio* of amplitude and
  quantiles survive the monotone transform, so the estimate is equally good at
  the dark and bright ends. And the summary **says which numbers those are**:
  `quantile_method` / `quantile_bin_db` plus a caveat sentence, so an estimate
  can never be mistaken for a measurement. A default (non-windowed) summary is
  byte-identical to the one this mode did not exist for — the keys appear only
  when they mean something.

  Blocks are cut from the shared grid, not from a window, so a window edge is
  not a block edge: `_BlockChanges` accumulates each block's *overlap* with each
  window, which is what keeps the breakdown identical when the two grids are
  deliberately misaligned. The windows themselves come from the helper the
  writer already used, renamed `_write_windows` → `_cube_windows` now that
  reading shares it. `umbra stack --stats-windowed` implies `--stats` and
  pairs with `--lazy --chunk-size N` (an unchunked cube is one window, i.e. the
  read this replaced with estimated percentiles). Offline-tested in
  `tests/test_load.py`: a windowed summary is compared field for field against
  the whole-slice walk's — counts, means, spreads, every change record, the
  per-block series and the ASCII heat-grid — including across `extent="union"`
  ground only one pass covers, with the percentiles pinned to within a bin
  separately, and the resident-window claim pinned by recording every slab the
  walk materialises.
- **The hosted instance gets the datacube's memory ceiling lifted too:
  `umbra serve --stack-lazy`.** `to_stack(lazy=True)` / `chunk_size=` (below)
  took the ceiling off a *local* cube, but `POST /artifacts/stats` — the one
  server endpoint that stacks a whole series — kept building its cube eagerly,
  so a hosted instance still held every pass at once. That is the endpoint where
  it matters most: it is the only route whose cost grows with the **number of
  acquisitions** rather than with one render, and its callers are the ones who
  installed nothing locally (a browser, an OpenAPI agent, a QGIS user), so the
  ceiling they hit is one they cannot raise.

  `umbra serve --stack-lazy [--stack-chunk-size N] [--stack-scheduler ...]`
  hands the server the same two levers: one `dask` task per pass, and windows
  within a pass. It is an **instance-wide policy** (`serve.StackExecution`,
  threaded through `build_app(stack_execution=…)` to `default_renderers`) and
  never a request field, for two reasons the endpoint cannot decide for a
  client: it needs the `dask` extra installed *on the server*, and it needs a
  decision about how many threads one request may spend. That second one is why
  `--stack-scheduler` exists and why it defaults to `synchronous` — a request
  handler that quietly starts a thread pool per render is a worse surprise than
  a slower one; `threads` opts into dask's pool for a faster single render that
  multiplies under concurrent ones. (`processes` is deliberately not offered:
  the chunks stream COG bytes through GDAL handles that do not fork cleanly.)

  The policy is scoped and inert by construction. `dask.config.set` is entered
  as a context manager around the one render, so an instance's choice cannot
  leak into another thread's render or a caller's process; the eager default
  never imports `dask` at all. And because a lazy cube's numbers are *identical*
  to an eager one's — only the peak memory differs — the policy is deliberately
  **not** part of `artifact_cache_key`: an operator flips it without
  invalidating a single cached artifact or moving a figure a client already
  fetched, which the tests pin by rendering the same request under all four
  policies and comparing the JSON bytes. An impossible policy (`--stack-chunk-size`
  without `--stack-lazy`) fails when the server starts rather than on the first
  stats request, and `--stack-lazy` without the extra installed answers `501`
  naming it, exactly like a missing `load`. The CLI echoes the resolved policy at
  startup so an operator can see it took. Offline-tested in `tests/test_serve.py`.
- **The last of the datacube's memory ceiling: `to_stack(chunk_size=N)` /
  `umbra stack --lazy --chunk-size N`.** `lazy=True` (below) made the cube stop
  costing `max_size²` × the number of passes, but it left the other half of the
  ceiling standing: with one chunk per acquisition the smallest unit of work is
  a **whole slice**, so a single pass at a large grid is still read and held
  whole. At 8192 px that is 256 MB of `float32` per pass, and it is a floor no
  amount of streaming lowers — the sharpness a site could be stacked at was
  still set by how much of *one scene* fits in memory.

  `chunk_size=N` cuts each pass into `N`-square windows that are read
  independently, so the unit of work becomes `N²` and `max_size` stops being
  bounded by a single slice. The windowing is exact rather than approximate: a
  window is the parent grid restricted to its own rows and columns
  (`_sub_grid`), so its cell size, CRS and edges are the parent's and every
  window read is pixel-identical to that region of the whole-slab read — there
  is no seam where two windows meet, which the tests pin by stacking a
  per-pixel ramp both ways and comparing the arrays exactly. The price is
  request count, and it is stated rather than hidden: each window opens the
  source and issues its own range requests, so a pass costs ⌈h/N⌉ × ⌈w/N⌉ reads
  instead of one. That is why it is opt-in, why the window wants to be a decent
  fraction of the grid (512–2048) rather than a tile, and why `chunk_size`
  without `lazy` is a hard error — on an eager cube it would bound nothing while
  looking like it did.

  The writer moved with it, or the file path would have re-materialised what the
  reader just avoided: `_write_stack_geotiff` now writes each band **window by
  window**, driven by the cube's own chunks (`_write_windows`), so
  `stack_to_geotiff` / `umbra stack --out` never holds a whole band. A cube with
  no windows — eager, or lazy but chunked only across the series — reports one
  whole-band window and takes exactly the write it took before, so the file is
  byte-identical however it was read. `stack_stats` is unchanged and still
  materialises one slice per pass: its distribution statistics are medians and
  percentiles, which need the pass whole, so measuring a cube keeps the ceiling
  that writing one no longer has (noted in `TODO.md`).
- **Datacubes that outgrow memory: `to_stack(lazy=True)` / `umbra stack --lazy`.**
  The time-series cube had a hard ceiling nobody could raise from the outside:
  every pass was read eagerly, so a cube cost `max_size²` × the number of
  acquisitions in RAM, and a long series had to be traded against resolution.
  Twelve passes at 4096 px is ~800 MB before a single reduction runs — which is
  why `examples/08` caps itself at six passes, and why the honest advice for a
  two-year archive of a site was "stack it coarse". The primitive this library
  exists to provide (see `docs/STRATEGY.md` §5.5: the *load* half `stackstac` /
  `odc-stac` cannot do here) was bounded by the analyst's laptop.

  `lazy=True` removes the trade rather than widening it. Each acquisition
  becomes **one `dask` task and one chunk**, so nothing is fetched until
  something asks for values, and the cube's cost stops scaling with the length
  of the series. What is *not* deferred is deliberate: the shared grid is still
  resolved eagerly from every source's footprint — that is what makes the slices
  comparable — so a non-overlapping series or an off-target `bbox` still fails
  immediately, at the call, rather than hours later inside a reduction.

  Deferring the reads would buy little if the consumers then materialised the
  cube anyway, so both were taught to walk it a slice at a time.
  `stack_to_geotiff` / `_write_stack_geotiff` write **band by band**, so the
  file-producing path is memory-bounded end to end. `stack_stats` was turned
  inside out: it now streams the series, holding at most the first, the previous
  and the current pass, which makes its memory a function of the *grid* rather
  than of the series — and that is an improvement for eager cubes too, which
  previously held the whole thing twice (float64 values plus a dB view). The
  spatial breakdown (`blocks=N` / `--blocks`) moved with it: `_BlockChanges`
  accumulates each block's pass-to-pass steps as the passes arrive instead of
  slicing a block's whole history out of a resident cube. Every `_pair_change`
  call was already independent of every other, so only the loop order changed —
  the records, the peak intervals, the `--block-series` sequences and the ASCII
  heat-grid are identical, which the tests assert by comparing a lazy cube's
  statistics to an eager one's *as a whole object*.

  `dask` is its own extra (`pip install "umbra-py[dask]"`), not a widening of
  `[load]` and not part of `[all]`: it brings a task scheduler, an eager cube is
  the right default at scene scale, and nothing else in the package needs it.
  Asking for `lazy=True` without it raises before any bytes are streamed, naming
  the extra that fixes it. The cube, the GeoTIFF and the statistics are
  identical either way — only what is resident differs — so `--lazy` changes no
  output and is absent from the render manifest by design. Offline-tested in
  `tests/test_load.py` (nothing read until `.compute()`, one chunk per
  acquisition, eager/lazy equality across `intersection` and `union` including
  the NaN padding, eager grid validation, the missing-extra error, byte-equal
  GeoTIFFs, one materialised slice per pass, and the CLI flag end to end). This
  closes the "lazy / chunked reads" follow-on in `TODO.md`.
- **A converted scene now says what it is: conversion provenance in the raster's
  own metadata.** The conversion chain can place a SICD on the map (`--dem` /
  `--geoid`), take the terrain out of its brightness (`--rtc`, four models), and
  make the remaining number physical (`--calibrate`) — but the GeoTIFF it wrote
  carried no trace of any of it. Two scenes converted with different settings
  were pixel-for-pixel indistinguishable after the fact: a raster of `gamma0` in
  dB and a raster of relative amplitude look identical, and the only record of
  which one you were holding was your shell history. A physical measurement
  nobody can attribute to a calibration is not a measurement.

  `conversion_tags()` fixes that at the only place it survives — inside the
  file. Every raster `umbra_py.convert` writes now carries namespaced GeoTIFF
  metadata (`UMBRA_*`) recording the calibration, the terrain-flattening model
  **and the reference incidence angle it resolved to**, the DEM and geoid
  actually used, the projection, the resampling kernel, the amplitude scale, a
  one-line statement of what a pixel value *is* (`UMBRA_UNITS`: `dB (gamma0)`,
  `amplitude (sqrt sigma0)`, `dB (relative amplitude)`), the umbra-py version
  that produced it, and the CC-BY licence and attribution — which is design
  principle 4 (license propagation) applied to a derivative product, not just to
  the data it came from.

  Two details are deliberate. Every processing step is reported *including the
  ones that did not run* — `"none"`, never a missing key — so a reader never has
  to decide whether an absent tag means "not applied" or "not recorded". And the
  source is recorded by **file name only**: the local directory a product
  happened to sit in is not provenance, and it would travel with the artifact to
  places it does not belong.

  Read it back with `read_conversion_tags(path)` (prefix stripped and
  lower-cased, so `read_conversion_tags(p)["calibration"]` is the question you
  actually have), with `umbra convert --provenance FILE` (the same dict as JSON;
  the flag reads and writes nothing, so it takes no `DST`), or with plain
  `gdalinfo` — the tags are ordinary GeoTIFF metadata, so every reader in the
  ecosystem can see them without knowing umbra-py exists. A raster umbra-py did
  not convert answers `{}` rather than guessing.

  The geocoded path writes its tags into the in-memory dataset *before* the COG
  driver copies it out, so they survive the copy into the file a user actually
  gets — the one thing worth a test of its own. Pure-stdlib tag construction (no
  rasterio needed to build or assert on the dict), no new dependency, no model
  call; offline-tested in `tests/test_convert.py` (the full tag set, the
  did-not-run values, the slant-plane subset, the path-not-leaked rule, the COG
  round-trip, both writers end-to-end, and the CLI read path). This closes the
  "nothing downstream records the calibration" follow-on in `TODO.md`.
- **`umbra convert --calibrate`: pixel values that mean something outside their
  own scene.** The conversion chain could place a SICD on the map
  (`--dem`/`--geoid`) and take the terrain out of its brightness (`--rtc`, four
  models up to the layover-measuring `facet` integration), but the number left in
  each pixel was still *relative* — detected amplitude in whatever arbitrary units
  the product's pixels carry, comparable within one image and with nothing else.
  Two scenes of the same ground could not be differenced in physical terms, and no
  measurement made from them could be quoted. `--calibrate` /
  `sicd_to_geocoded_cog(calibration=…)` closes that: it scales pixel **power** by
  the scale-factor polynomial in the SICD's own `Radiometric` metadata, so the
  output is a physical quantity — `sigma0`, `beta0` and `gamma0`, the backscatter
  coefficients referenced to unit ground, slant-plane and perpendicular-to-look
  area, or `rcs`, the absolute radar cross-section in m² (`CALIBRATION_TYPES`).

  It is applied **in image space**, before the warp, because that is where the
  polynomials are defined: they are functions of image coordinates measured in
  metres from the scene centre point, so `_calibration_scale` evaluates them over
  `(row + FirstRow − SCPPixel.Row) × Grid.Row.SS` and the column equivalent —
  which means a constant polynomial (the common case) gives a flat scale, a
  higher-order one tracks the across-swath variation the product describes, and a
  chip is offset by its own origin rather than silently tilted.

  Calibration and `--rtc` **compose**, which is the point: both are power-domain
  factors on the same raster, so they share one application path
  (`_apply_calibration` → `_apply_terrain_flattening`) and applying both simply
  multiplies them. `--rtc-model facet --calibrate gamma0` is therefore a
  terrain-flattened **gamma-nought** product — the thing every RTC entry in this
  changelog has said it was *not* — whose decibels mean the same thing across
  scenes, dates and sensors. In the default decibel scale the output is that
  coefficient in dB directly; in linear it is calibrated amplitude, whose square
  is the coefficient.

  The honest half matters as much as the arithmetic: a calibration is only ever as
  real as the metadata behind it. Umbra's open products generally ship *without* a
  `Radiometric` block, so asking for one that isn't there raises a self-describing
  error naming what the product does carry (and the CLI reports it as a message,
  not a traceback) instead of emitting a calibrated-looking number.
  `sicd_calibration_types(path)` answers the same question ahead of time — useful
  when deciding whether a scene can enter a calibrated stack. A scale factor is a
  positive power ratio by construction, so a polynomial that evaluates
  non-positive or non-finite anywhere on the image grid is rejected rather than
  clamped: a silently repaired calibration is worse than none.

  Pure-numpy core, no model call, no new dependency; offline-tested in
  `tests/test_convert.py` (the SCP-relative evaluation, the chip origin, constant
  and higher-order polynomials, the non-positive rejection, the dB/linear
  equivalence, end-to-end slant-plane and geocoded scaling, exact composition with
  `--rtc`, the uncalibrated-product refusal, and the CLI surface). This closes
  radiometric calibration in `STRATEGY.md` 5.5; MultiRTC interop remains deferred.
- **`umbra index fetch-thumbnails`: the published snapshot now carries the SAR
  pictures, not just the metadata.** `umbra index bake-thumbnails` has been able
  to render a quicklook per acquisition into the index for a while, and every
  consumer reads it — `umbra serve`'s `GET /artifacts/thumbnail/{id}.png`, the
  `umbra demo` detail panel, a `--local` gallery. But nobody had those bytes
  without baking them, and baking them means streaming a cloud-optimized GeoTIFF
  overview *per scene*: the one derived artifact in this project that is worth
  moving rather than recomputing. The weekly `publish-index.yml` workflow now
  bakes them centrally and publishes `catalog.thumbs.db` on the rolling
  `catalog-index` release, so `umbra index fetch-thumbnails` fills a local
  index's previews with a single download and no range read at all.

  It ships as a **separate sidecar** rather than a `thumbnail` column inside the
  published `catalog.db`, for the same reason `catalog.embed.db` is separate: a
  PNG per acquisition dwarfs the metadata it hangs off, and folding it in would
  make every `umbra index fetch` — the command whose entire promise is *instant
  local search, no crawl* — pay for pixels most callers never open. So the
  metadata download stays small and the pixels are one opt-in command
  (`CatalogIndex.export_thumbnails` / `import_thumbnails`,
  `fetch_prebuilt_thumbnails`, `default_thumbs_path`). Merging is additive and
  non-destructive: rows the index doesn't hold are ignored (a sidecar from a
  newer crawl is not an error) and a thumbnail already baked locally is kept
  unless you pass `--overwrite`.

  That same sidecar is what makes the weekly bake **incremental**, which is what
  makes publishing it polite at all. The workflow rebuilds `catalog.db` from
  scratch every Monday, so every thumbnail column starts `NULL`; re-importing
  last week's published sidecar before baking means a run streams only the
  acquisitions added since, instead of re-streaming the whole archive weekly
  against Umbra's bucket (the §6 "keep the crawl polite" guardrail applied to
  egress rather than listings). The bake is `--limit`-bounded per run, and
  `umbra index bake-thumbnails --newest-first` — new here — is what makes that
  cap a *priority* rather than a lottery: the default `href` ordering is
  arbitrary with respect to time, so a capped run would leave the freshest
  passes, the ones a demo or a monitoring view opens on, unbaked the longest.
  Undated items sort last, having no claim to being recent. The publish step is
  split from the bake step and refuses to clobber the accumulated release asset
  with an empty export, so a run that times out mid-bake still publishes what it
  and every earlier run baked.

  This closes the last **demo / hosting polish** item on `STRATEGY.md` §8 (the
  `DEMO_APP_GAPS.md` G6 thumbnail denormalization, whose remaining half was
  "bake them into the *published* snapshot"). Offline-tested end to end in
  `tests/test_index.py` — round-trip between two indexes, the keep-vs-overwrite
  merge policy, unknown rows ignored, export-as-upsert, a non-sidecar file
  rejected as `IndexSchemaError`, the newest-first ordering, and the three CLI
  paths (`export-thumbnails`, `fetch-thumbnails --from`, and the download path
  with the fetch helper stubbed).
- **`umbra map --area` / `--fuzzy`: the last gather command that could not name
  a site can.** Umbra files every pass of a site under one named task directory,
  so `--area "Centerfield"` lists just that directory instead of scanning the
  archive — the cheapest filter the catalog has. Every gather command exposed it
  except `umbra map`, which meant the one verb whose entire job is *showing you
  where the archive has imagery* was also the one that made you find a site's
  bounding box before you could ask about it. It now takes `--area` and its
  typo-tolerant `--fuzzy` widener like its siblings, threaded into the same
  `_gather_items` call, so `umbra map --area "Centerfield" --out coverage.html`
  draws one site's coverage directly.

  `umbra index build` / `umbra index update` gained the matching `--fuzzy` too:
  they already took `--area` but not the flag that widens it, so an index could
  not be scoped by the same spelling the search commands accept.

  This closes the task-name half of the `CODEBASE_ANALYSIS.md` P3 #18 shared-
  option extraction, and it closes it the way the geography half was closed —
  by removing the thing that caused the gap rather than only the gap. `--area`
  was written out by hand on thirteen commands, and writing an option out per
  command is exactly what lets a command miss it (the same drift had already
  cost the polygon filter thirteen commands and `--place` three). It is now one
  shared `_area_option` definition beside `_fuzzy_option`, and both groups are
  checked against **one roster** of gather commands (`conftest.GATHER_COMMANDS`,
  which `tests/test_geometry.py` now shares): `tests/test_cli_option_groups.py`
  asserts every command on it exposes `--area`/`--fuzzy` *and* forwards them to
  the search backend, so adding a gather command without the group fails a test
  instead of quietly shipping a front door with fewer filters than its siblings.
  Commands whose help text says something specific about what the name scopes
  keep their own wording — the same convention `_place_option` documents.
- **`umbra convert --rtc --rtc-model facet`: the terrain correction that
  measures layover, because it integrates in the radar's geometry instead of
  correcting pixel by pixel.** The three shipped RTC models all answer the same
  question — *given this pixel's own slope, how much brighter is it than flat
  ground would be?* — with three increasingly complete per-pixel terms: the
  cosine of the 3-D local incidence angle, the range-plane foreshortening area,
  and the tilted facet's true area (`gamma`). What none of them can express is
  the failure that dominates a mountain scene: where terrain is steeper than the
  look direction, *several* patches of ground backscatter into one radar cell
  and their returns sum there. Layover is not a pixel being mis-scaled; it is
  several pixels sharing one measurement. A model that only ever looks at one
  slope at a time is structurally unable to see it — and so the flat valley
  floor a ridge folds onto comes back "corrected" to exactly 1.0, its own slope
  being zero.

  `facet` integrates instead (Small 2011, *Flattening Gamma*). Every terrain
  facet is projected into the scene's own `(slant_range, azimuth)` geometry
  (`_radar_coordinates`, the same plane-wave look vector the other models use),
  its illuminated area — the true tilted area `cell / nz`, projected onto the
  plane perpendicular to the look direction — is accumulated into the radar cell
  it images into (`_accumulate_radar_area`, bilinear so the accumulation is a
  smooth partition rather than a nearest-cell histogram), and every pixel is then
  normalised by the **total** area in its cell. Folded ground reads the summed
  area of everything folded with it, and all of it is suppressed together, which
  is the correction the per-pixel models cannot make.

  Two properties keep it honest rather than merely different. The reference is
  the *same integration* run over flat ground in the same geometry, so the
  binning and the scene edges cancel exactly and flat terrain comes back at
  exactly one — the invariant every other model holds. And over a planar range
  slope, where nothing folds, the integration reduces to the product of the
  `area` and `gamma` factors, the closed form those two carry between them; the
  tests assert that composition across four slopes, so the arithmetic is pinned
  to something independently derivable instead of to its own output.

  Fourth value in `RTC_MODELS`, selected by `--rtc-model facet` /
  `sicd_to_geocoded_cog(rtc_model="facet")`; `rtc_model` still defaults to
  `"cosine"`, so every existing call is byte-identical. Shadowed facets
  contribute no area, DEM gaps pass through untouched, and the radar grid is
  sized from the pixel spacing and coarsened rather than grown without bound
  under extreme relief. Pure-numpy core, no model call, no new dependency,
  offline-tested in `tests/test_convert.py` (radar coordinates, coincident-facet
  accumulation, flat-unchanged, azimuth-slope-unchanged, the planar
  area×gamma composition, layover suppression the `gamma` model misses, the
  reference-angle offset, end-to-end differ-from-all-three + CLI). This closes
  the image-space facet integration named in `STRATEGY.md` 5.5; what stays open
  there is calibration itself (Umbra's open products are not radiometrically
  calibrated) and MultiRTC interop.
- **`umbra ask --aoi`: the planner can finally mean a *shape*, because it now
  chooses one instead of drawing it.** `--intersects` reached every other front
  door in the previous change, but a plan still carried only `bbox`/`place`, so
  the one interface people reach for in plain language — *"scenes over this
  watershed since March"* — was the one that silently degraded a polygon to the
  rectangle around it. The gap was never an oversight. A hallucinated date is
  caught by `parse_date_bound`; a hallucinated ring is a perfectly plausible
  polygon over the wrong ground, and no layer downstream can tell. Letting a
  model emit coordinates would have been the first place in the package where a
  model output *became* a coordinate — the exact thing the determinism boundary
  (`AI_INTEGRATION_IDEAS.md` §A4) exists to forbid.

  So the model never writes a polygon; it picks one. You supply the areas you
  already have — `umbra ask "what changed over the delta this spring?" --aoi
  delta.geojson --aoi levees.geojson` (repeatable, `NAME=PATH` to name one
  explicitly, otherwise the file stem) — and each is parsed by the deterministic
  layer *before* the model sees anything. The prompt then lists them by name,
  part count and bounds, gains a single `aoi` key, and states the rule plainly:
  there is no way to write coordinates, and a name that is not on the list is
  rejected. `parse_plan` enforces exactly that — an unknown name is a
  self-describing `AskError` listing the valid ones, a name with *no* areas
  supplied is an error rather than a quietly unfiltered whole-world search (the
  one failure a polygon filter exists to prevent), and `aoi` beside `place` or
  `bbox` is refused like every other pair of spatial filters.

  A selected area flows through `SearchPlan.to_search_kwargs()` as `intersects`
  — the same exterior rings, the same `UmbraItem.intersects_polygon` test every
  other surface shares — and renders into the audited command as `umbra search
  --intersects delta.geojson`, pointing back at the user's own file rather than
  inlining a ring dump. `--json` reports the choice as `{"name", "source",
  "bbox"}`, which is what makes a plan auditable without the coordinates. With
  no `--aoi` supplied the prompt is byte-identical to before, so a model is never
  offered a filter the caller cannot honour. New public `AreaOfInterest`; no new
  dependency, no second model call. Offline-tested in `tests/test_planner.py`
  (prompt listing, name resolution incl. case, all four rejections, command and
  JSON rendering, and the CLI end-to-end sending the parsed rings to the search
  backend).
- **`--intersects` on every command that gathers acquisitions, not just `umbra
  search` — an area of interest is a polygon, and now every front door accepts
  one (`CODEBASE_ANALYSIS.md` P3 #18, the shared-option-group extraction).** The
  library, the local index and the `umbra serve` STAC API have filtered by
  polygon since the geometry search shipped, but only one of fourteen CLI
  commands exposed it. Everything else — `map`, `gallery`, `change`, `timescan`,
  `swipe`, `demo`, `tiles`, `showcase`, `stack`, `chips`, `embed build`, `index
  build`, `index update`, `watch` — was rectangle-only, so a coastline, a border,
  a catchment or a port had to be over-approximated by its bounding box and the
  surplus scenes culled by hand afterwards. That is the culling the polygon test
  already does exactly: `umbra change --intersects aoi.geojson` now composites
  the passes that actually cover the AOI, and `umbra index build --intersects
  country.geojson` builds a country-shaped index in one flag.

  The drift was the point. `--bbox`, `--place` and `--intersects` were written
  out per command, so they had diverged: `--intersects` existed on one command,
  and `change` / `swipe` / `chips` had no `--place` at all. They are one group
  now — two shared decorators (`_geometry_option`, `_place_option`) and one
  shared resolver (`_resolve_geography`) that also enforces the
  polygon-vs-rectangle exclusion, so all fourteen commands reject the
  combination with the same message and cannot disagree with `umbra search`
  about what a geometry means. `--place` therefore lands on `change`, `swipe`
  and `chips` in the same change, and `umbra showcase` stopped geocoding (and
  echoing) the same `--place` twice, since its explorer gather and its marquee
  gather now share one resolution.

  `watch_key` gained an `intersects` parameter so two different AOIs are two
  different watches; an unset filter is dropped before hashing, so a scheduled
  watch keeps the name — and therefore the stored state — it had before the
  option existed. The agent-facing context card (`umbra context` /
  `llms-full.txt`) documents `intersects` beside `bbox`, which it had been
  missing. No library change: `UmbraCatalog.search`, `CatalogIndex.search`,
  `build` and `update` already took the kwarg. Offline-tested in
  `tests/test_geometry.py` (every command exposes the flag, forwards the parsed
  polygon, and rejects it beside a rectangle; an end-to-end `umbra map --local
  --intersects` over a real index; the watch key's stability).
- **Filter the archive by the facet that decides whether an answer is valid —
  polarization chips in both `umbra demo` modes, and the two list-valued fields
  the vector tiles withheld (`docs/DEMO_APP_GAPS.md` Path A, the last structural
  difference between the two explorers).** The explorer could filter by place,
  date and product type — three facets about *what you get*. The one it could
  not filter by is the one that decides whether an analysis is meaningful at
  all: HH and VV image different scattering, so differencing a VV pass against an
  HH one puts a physics difference on the time axis and reads it as change.
  `POST /artifacts/stats` already refuses such a selection outright and tells the
  caller to "filter the selection to one polarization" — advice the page had no
  control to follow, which made the Quantify button dead-endable from an
  ordinary filtered view. A **Polarization** chip row now sits under the product
  chips in both modes, with the same "on unless explicitly toggled off" rule, so
  an untouched sidebar still hides nothing.

  Making it work over the *whole archive* meant the tiles had to carry the field.
  `umbra tiles` now writes two more properties per feature — `pol` and `assets`,
  the item's polarizations and its available products — comma-joined, because a
  vector-tile property is a scalar. The embedded-slice app tests the list it
  already holds; the whole-archive app compiles the chips to a MapLibre
  `index-of` test evaluated inside the tiles, which is exact rather than
  approximate since no two-letter polarization code can match across a
  separator. An item with no `sar:polarizations` tiles no `pol` key at all and
  stays visible — the same "never hidden by a facet it has no value for" rule the
  missing-date guard applies. Those same two fields were the *only* thing the
  embedded-slice detail panel still showed that the whole-archive one could not,
  so the whole-archive explorer — what `umbra showcase --unified` deploys to
  GitHub Pages — is now a strict **superset** of the slice explorer, and Path A
  closes. Four chip rows across two apps read one rule, so they are built by one
  shared `window.umbraChipRow` helper rather than four copies that could drift.
  Offline-tested in `tests/test_pmtiles.py` and `tests/test_demo.py`.
- **An on-ramp for the datacube — `examples/08_time_series_datacube.ipynb` (the
  last open follow-on of the `to_stack` PR, `TODO.md`).** The stacking chain grew
  a lot in this release — a co-registered cube, a projected grid, a JSON
  reduction, a spatial breakdown, per-block histories, an HTTP endpoint and a
  button in the explorer — and none of it had a runnable example. The gallery's
  nearest notebook, `04`, is the thing this capability exists to correct: it
  averages each pass over *its own* grid, so two passes' means describe
  different ground and a moved footprint reads as change. Notebook `08` runs the
  honest version end to end — search a repeat-imaged task, collapse it to one
  polarization, `to_stack(crs="utm")` onto a shared equal-area grid,
  `stack_stats` for the per-pass series and the net first→last record (with a
  real `changed_area_km2`, which only a projected grid can report), `blocks=3`
  for the peak block and the north-up ASCII heat-grid, `block_series=True` for
  that block's whole pass-to-pass sequence, and finally the baseline-to-latest dB
  delta as a map, since the picture and the numbers come out of the same cube.

  It follows the gallery's discipline: a small deterministic search, `assert`s at
  the end of the code cells (including that the peak interval is a *member* of
  the series it was picked from), cleared outputs, CC-BY attribution in the
  narrative, and the calibration caveat the reduction itself carries. It falls
  back from `extent="intersection"` to `"union"` when a task's footprints don't
  all overlap, and caps the series at six passes because a cube is held in
  memory. `tests/test_examples.py` picks it up automatically (well-formed, code
  parses, only public `umbra_py` names, attribution present) and executes it
  under `pytest -m network`; it was additionally run end-to-end against the live
  bucket while being written. `examples/README.md` and the docs site's notebook
  index list it.
- **Draw the history, not just its endpoints — pass-to-pass sparklines in the
  `umbra demo` Quantify readout (the last open follow-on of the `stack_stats`
  demo wiring, and the first client of `block_series`, `TODO.md`).** The
  explorer's numeric readout led with two figures — a net first-to-last change
  and the block that moved most, "mostly between" two dates — and neither can
  tell two genuinely different histories apart: a corner that drifted a decibel
  every pass and one that jumped twelve once and held come back as the same
  `net_change` and the same `peak_interval`. The sequence that separates them was
  already in the document and unplotted: each pass carries its
  `change_vs_previous`, and, since the previous release note, each block carries
  its whole `series`. The readout now draws both — one signed, zero-baselined SVG
  bar per consecutive pass-to-pass step, for the site as a whole and for the
  block the server named as the peak, so the shape of the change is visible
  beside its size.

  A bar chart with no stated scale is decoration, so each sparkline is scaled to
  the largest step *in that series* and captioned with it (count, span, and that
  step's decibels and dates), every bar carries a dated `<title>` tooltip, and
  the figure gets an `aria-label` naming its largest step. The bars are built as
  DOM elements through `createElementNS` — never `innerHTML`, so a remote string
  still cannot parse as markup — and take the two colours the readout's
  `.brighter` / `.dimmer` prose already uses, so the picture and the sentence
  cannot say different things. The panel still formats and computes nothing: the
  only arithmetic is the pixel scale, and every decibel printed is the server's.
  The Quantify request accordingly sends `block_series: true` beside its existing
  `blocks: 3` (the one cost is response size — 9 blocks × (passes − 1) steps, of
  which the page plots one block's), and because it lives in the analyze panel
  both explorers share, the embedded-slice and whole-archive PMTiles pages gained
  it together. A series with nothing to compare — a single pass, or ground no two
  passes both saw — draws nothing rather than an empty frame, and a server that
  answers without the per-block series simply shows the scene-wide sparkline.
  Offline-tested in `tests/test_demo.py`; the generated JavaScript was also
  exercised outside pytest against a synthetic `/artifacts/stats` document (a
  five-pass series with a single-step corner, and the degenerate
  no-comparable-pair case).
- **Each block's whole history, not just its loudest moment —
  `stack_stats(blocks=N, block_series=True)` (the last open follow-on of the
  `stack_stats(blocks=N)` PR, `TODO.md`).** The spatial breakdown answers *where*
  a site moved and *when*, but the "when" it reports is a single number per
  block: the consecutive interval that block moved most in. That reduction
  throws away the distinction between two genuinely different histories — a
  corner that drifted a couple of decibels every pass and one that jumped twelve
  once and held both come back as one `peak_interval` — and the steps it was
  picked from were already computed and discarded. `block_series=True` keeps
  them: every block gains a `series` array of every consecutive pass-to-pass
  record, oldest first, in exactly the same shape as `peak_interval`, so a
  block's trend is plottable rather than inferred and the peak is visibly a
  member of the sequence rather than a separately-derived number. It is payload,
  not arithmetic — the loop that found the peak now collects instead of
  comparing, and `peak_interval` is a `max()` over what it collected — so the
  cost is the response size alone, which is why it is opt-in and why it needs a
  `blocks` grid to hang on (asking for a series with no grid is a hard error
  before any work is done, not a silently dropped flag). A block with nothing to
  compare — ground only one pass observed — reports an empty series, the same
  honesty as its `None` net change.

  Reachable from every front door the reduction already had, with the same
  argument name and the same default (off, so every existing payload is
  byte-identical): `umbra stack --blocks 6 --block-series` on the CLI (which
  refuses `--block-series` without `--blocks`), `block_series=True` on the
  `stack_stats` tool across MCP / LangChain / LlamaIndex (the wrappers infer it
  from the one shared callable, so the three surfaces cannot drift), and
  `"block_series": true` on `POST /artifacts/stats`, where it normalises through
  `stats_options` — so it is part of the content-addressed cache key and a series
  request never collides with the plain breakdown, and a series without blocks is
  a `400` carrying the explanation rather than a `500`. The tool description
  tells an agent to ask for it only when the *shape* of a block's history is the
  question, since it is the largest thing this reduction emits. No model call, no
  new dependency; offline-tested in `tests/test_load.py` (the sequence and its
  chaining, peak-is-a-member, opt-in and the missing-grid refusal, empty series
  over unobserved ground, and the two CLI paths), `tests/test_serve.py` (option
  normalisation, distinct cache entry, the `400`) and `tests/test_mcp_server.py`.
- **A "Quantify" button in the `umbra demo` analyze panel — the self-serve
  explorer can now *measure*, not only look (the first open follow-on of the
  `POST /artifacts/stats` PR, `TODO.md`; `DEMO_APP_GAPS.md` R4).** With
  `--server-url` the explorer offered Change / Timescan / Swipe, and every one
  of them was a picture: a visitor could see that a site changed but had no way
  to say *by how much* without leaving the page for the CLI. The numeric
  endpoint that answers that shipped last week and had no client. **Quantify**
  is it: the fourth button POSTs the same currently-filtered acquisitions to
  `POST /artifacts/stats` and reads the reduction out in the sidebar — the mean
  decibel change between the first and last pass with its direction, the
  fraction of the site that moved past the change threshold **and that area in
  km²** (the endpoint stacks on the site's UTM grid, so the number means
  something), which block moved most and between which two passes, and the
  north-up ASCII heat-grid of signed change per block. The request always asks
  for the `blocks: 3` breakdown, because a scene-wide mean *dilutes* a change
  that moved one corner hard — the same reason `blocks` exists at all.
  Everything the panel prints is a number the server measured; it formats and
  never computes, so the page and `umbra stack --stats` cannot disagree, and the
  document's CC-BY attribution and its calibration caveat ride along with the
  numbers into the browser (design principle 4). The panel is the one both
  explorers share, so the embedded-slice and whole-archive PMTiles pages gain
  the button together and cannot drift; the readout is built with `textContent`,
  so a remote string never parses as HTML; and with no `--server-url` the page
  is a fully static single file exactly as before. Offline-tested in
  `tests/test_demo.py`, and the generated panel was additionally driven against
  a real `stack_stats` document end to end.

  Alongside it, a **client mistake stopped reading as a server error** (the
  stats PR's other follow-on): `_serve_artifact` and the async job runner mapped
  only `MissingDependencyError`, so bad input to a render — acquisitions whose
  footprints share no ground under `extent="intersection"`, the failure this
  button makes easy to hit from a filtered view — surfaced as a `500` (or a
  `500` job) that a caller can only read as "the server broke". A render's
  `ValueError` is now a `400` carrying the explanation, on the synchronous and
  async paths alike and for every artifact route, not just stats. Offline-tested
  in `tests/test_serve.py`.

- **Measure a site over HTTP: `POST /artifacts/stats` on `umbra serve` (the last
  open follow-on of the datacube PR, `TODO.md`; `STRATEGY.md` §5.5 / §7.2).**
  The STAC API façade could already *show* change over any site on demand — a
  quicklook, a change composite, a timescan, a swipe map — but every artifact it
  served was a picture, which a person reads and a program cannot. The reduction
  that turns those pictures into numbers (`to_stack` → `stack_stats`) was
  reachable from the CLI (`umbra stack --stats`) and from the agent front doors
  (`stack_stats` on MCP / LangChain / LlamaIndex), and from HTTP not at all.
  `POST /artifacts/stats` closes that: the same request shape as the composite
  endpoints (`ids`, or a `bbox` + `datetime` query; the same content-addressed
  disk cache; the same `"async": true` opt-in to a `202` + `GET /jobs/{id}`
  poll), answering with the reduction as JSON instead of an image — per-pass
  decibel statistics, the signed change against the previous pass, how much
  ground moved past `change_threshold_db` **in km²**, and with `"blocks": N` the
  spatial breakdown naming which part of the site moved and between which two
  passes. Two defaults deliberately differ from the picture endpoints, matching
  the `stack_stats` agent tool rather than the compositors: the shared grid is
  the site's **UTM zone** (so a cell count is an area and `changed_area_km2`
  means something — `"crs": null` opts back into lon/lat, where areas come back
  `null` rather than wrong) and values are **decibels**, the scale on which a
  ratio of backscatter is a difference. `"clip_bbox"` narrows the measurement to
  a sub-area inside the scenes, distinct from `"bbox"`, which chooses *which*
  acquisitions are measured. Unlike the composites it **refuses to mix
  polarizations** (a `400`, the same refusal the agent tool makes): a
  mixed-polarization composite is merely confusing to look at, but a
  mixed-polarization *number* is wrong, because the HH-vs-VV difference lands on
  the time axis and reads as change. The renderer stays injectable like its
  siblings — `Renderers` gained a `stats` member — so the route is fully
  offline-testable in the core install, and the real one imports `load` lazily,
  surfacing a missing extra as the usual `501`. Advertised as a `stats` link on
  the landing page and covered by `--no-artifacts`. Offline-tested in
  `tests/test_serve.py` (option defaults and validation, the polarization
  refusal, JSON round-trip + cache hit/miss, `blocks` as a distinct cache entry,
  the async job flow, and the production renderer threading its options into
  `to_stack` / `stack_stats`).

- **Where *and* when a site changed: `stack_stats(blocks=N)` / `umbra stack
  --blocks` / the agent tools' `blocks` argument (third follow-on of the
  datacube PR, `TODO.md`; `STRATEGY.md` §5.5).** The library had two change
  reductions and each answered half the question: `narrate.compute_change_stats`
  cuts *two* passes into a coarse grid to say **where** change sits, and
  `stack_stats` walks the whole series to say **when** it happened and how much
  ground moved. Neither could answer both, and the gap mattered because a
  scene-wide mean *dilutes* a localized change — a corner that brightens 12 dB
  reads as 0.75 dB across a 16-block scene, small enough to dismiss. `blocks=N`
  now cuts every pass into the same N×N grid and reports each block separately,
  so the answer is spatial and temporal at once: a `spatial` key with one record
  per block carrying its `row`/`col`, the plain-language `compass` label
  `narrate` already uses (the two reductions share `_compass_label` and
  `_split_slices`, so a block means the same thing in both), `bounds` in the
  cube's own CRS, a `center_lonlat` to map or reverse-geocode it by, its own
  `net_change` (identical fields to the scene-wide one, from the same
  `_pair_change`) and its `peak_interval` — the consecutive pair of passes that
  block moved most between, named by item id and timestamp. Alongside them
  `peak_block` names the block that moved most overall so nothing has to scan
  the grid, and `grid_text` renders the net signed change as a north-up ASCII
  heat-grid (`.` for ground no two passes both observed) — the same shape
  `umbra change --narrate` grounds its narration on, so a model reading both
  sees one spatial vocabulary. Unobserved blocks report `None` rather than a
  change of zero, so `extent="union"` padding still can never read as change,
  and `changed_area_km2` per block obeys the same projected-grid rule as
  everywhere else. `umbra stack --blocks N` prints it and implies `--stats` (so
  `--blocks 6` alone measures a site spatially without writing a file), and it
  rides inside the render manifest's `stats` field under `--json` (see
  `docs/schemas/render-manifest.schema.json`). The same argument is on the
  `stack_stats` tool across all three agent front doors (MCP, LangChain,
  LlamaIndex), defaulting to `0` so the payload stays small until a model asks
  *where*, and the packaged `quantify-change` MCP prompt now asks for `blocks=6`
  and tells the model to locate change by the peak block's compass label and
  centre rather than by eye. No model call anywhere in the path; offline-tested
  in `tests/test_load.py` (a corner that brightens on the last pass only, so both
  axes are hand-checkable: +12.04 dB in the northeast block, 0.0 elsewhere, and
  the last interval named as the one that moved; block geometry, the lon/lat
  centre, and unobserved blocks under `extent="union"`) and
  `tests/test_mcp_server.py`.

- **The datacube as an answer, not an array: `stack_stats` / `umbra stack
  --stats` / the `stack_stats` agent tool (second follow-on of the datacube PR,
  `TODO.md`; `STRATEGY.md` §7.5).** `to_stack` produced the co-registered cube
  but left every consumer to reduce it themselves — and the reduction is where
  the domain knowledge sits (mask the padding, compare on the log scale, refuse
  to call a geographic cell count an area). Worse, the cube was invisible to the
  agent front doors: `change_composite` and `timescan` could show a model *where*
  a site changed, and nothing could tell it *how much*. `stack_stats(cube)` now
  reduces a cube to plain JSON — one record per pass (`valid_fraction` plus
  `mean`/`median`/`std`/`p5`/`p95` in the cube's own units) with the signed
  change against the pass before it, and one net first-to-last record. Change is
  **always** reported in decibels, whether the cube holds dB or linear
  amplitude, because a ratio of backscatter is a difference on the log scale; a
  cell counts as changed once it moves past `change_threshold_db` (3 dB, the
  same default `umbra change --narrate` grounds its narration on), reported as
  `brightened_fraction` / `dimmed_fraction` / `changed_fraction` and — only when
  the cube's grid is projected — as `changed_area_km2`, so `--crs utm` is what
  turns a count into a measurement and a lon/lat grid answers `None` rather than
  something wrong. Only cells observed on *both* passes are compared, so
  `extent="union"` padding can never read as change. The complement to
  `narrate.compute_change_stats`, which blocks *two* passes spatially to say
  where change sits: this walks the whole series to say when it happened and how
  much ground moved. Every payload carries the CC-BY line and the caveats an
  interpretation needs (the open products are not radiometrically calibrated, so
  decibels are relative to the same ground on another date; look geometry moves
  backscatter too). `umbra stack --stats` prints the object — and `--out` is now
  optional, so `--stats` alone measures a site without writing a file, while
  both together write the GeoTIFF *and* measure it from the one stack (the
  "Wrote …" note moves to stderr so stdout stays a single parseable object, and
  under `--json` the statistics ride inside the render manifest's new optional
  `stats` field, see `docs/schemas/render-manifest.schema.json`). The same
  callable is registered as the `stack_stats` tool on all three agent front
  doors (MCP, LangChain, LlamaIndex) — defaulting to `crs="utm"` and the decibel
  scale so an agent's numbers are equal-area and radiometric by construction,
  refusing mixed polarizations like the render tools, and joined by a packaged
  `quantify-change` MCP prompt that pairs the measurement with a timescan. No
  model call anywhere in the path; offline-tested in `tests/test_load.py`
  (hand-checkable 6.02 dB doublings, area on a projected grid vs. `None` on a
  geographic one, thresholding, union overlap-only comparison, the CLI's three
  output modes) and `tests/test_mcp_server.py`.

- **A projected, equal-area datacube grid: `to_stack(crs=…)` / `umbra stack
  --crs` (first follow-on of the datacube PR, `TODO.md`).** `to_stack` built its
  shared grid in lon/lat, which is the right default — one grid works anywhere,
  and comparing a cell to *itself* across dates is unaffected — but degrees are
  not a unit of ground: cells stretch with latitude, so counting changed cells
  is not measuring an area and distances are distorted. The cube was therefore
  honest for *change* and wrong for *quantity*, and the only fix was to
  reproject the result afterwards (resampling twice). `crs=` now names the CRS
  the shared grid is built in, so the co-registration lands there directly:
  `crs="utm"` (`STACK_AUTO_CRS`) resolves the UTM zone containing the stacked
  ground — read off the sources' own footprints, so a caller who doesn't know
  the zone still gets metre-sized, equal-area cells — and any other value is a
  CRS name (`"EPSG:32633"`, a PROJ or WKT string) validated through `rasterio`,
  so a typo raises here instead of silently warping to nothing. `bbox` /
  `--clip-bbox` stays lon/lat whatever the cube's CRS (transformed internally,
  and still reported in the caller's own degrees when it misses the site), the
  grid derivation is unchanged beyond its units, and the `attrs["crs"]` /
  `attrs["bounds"]` / coordinate axes follow the target CRS. `stack_to_geotiff`
  and `umbra stack --crs` pass it through, and the written GeoTIFF now carries
  the *resolved* CRS in its tags, so a file built with `--crs utm` says which
  zone it landed in. Default behavior is unchanged (`crs=None` → EPSG:4326).
  Deterministic, no model call, no new dependency (the same `[load]` extra),
  and offline-tested in `tests/test_load.py`: the auto-UTM zone/hemisphere
  derivation, uniform near-square cells in metres, an explicit CRS, a rejected
  bad CRS, lon/lat clipping under a projected cube, and the CLI end-to-end.

- **Time-series datacubes: `umbra_py.to_stack` / `stack_to_geotiff` / `umbra
  stack` (`STRATEGY.md` §2–§3 — the `stackstac`/`odc-stac` parity gap).** The
  library could load *one* scene into a labelled array (`to_xarray`) and could
  render a *picture* of several (`umbra change`, `umbra timescan`), but there
  was no way to get the multi-date **numbers** — the primitive every multi-date
  SAR analysis actually starts from. Elsewhere in the STAC ecosystem that step
  is `stackstac` / `odc-stac`; neither can be pointed at Umbra, because both
  assume a STAC *API* and a common projected grid, and successive passes over
  one site are delivered in whatever UTM zone and at whatever extent each
  acquisition happened to use. So the co-registration has to be done here.
  `to_stack(items)` warps every acquisition onto one shared EPSG:4326 grid and
  returns an `xarray.DataArray` with dims `("time", "y", "x")` — slices ordered
  oldest-first, each carrying its `item_id` on the time axis, nodata and
  non-positive pixels always `NaN` so `cube.mean("time")` / `.std("time")` /
  `.diff("time")` are honest per-ground-cell statistics rather than per-scene
  ones. `extent="intersection"` (the default) keeps only the ground *every*
  pass saw, so no cell has a gap, and says so plainly when the footprints don't
  all overlap; `extent="union"` (`STACK_EXTENTS`) keeps all ground *any* pass
  saw and pads each slice with `NaN` outside its own footprint. `bbox` clips,
  `max_size` caps the shared grid, `db` stacks the decibel scale (where a
  backscatter ratio becomes a subtraction). Kept honest about the geometry: the
  lon/lat grid stretches with latitude — the same quick-look approximation
  `umbra change` / `umbra timescan` make, fine for comparing a cell to *itself*
  across dates — and the docstring says to reproject before measuring area.
  Efficient by construction: each source is opened as a full-resolution
  `WarpedVRT` and then read *decimated* through a window, so GDAL serves the
  matching cloud-optimized GeoTIFF overview instead of every full-res tile (the
  same hard-won pattern `viz._coregister_bands` uses — reading a coarse VRT
  whole would force a full-res source read and thousands of range requests).
  `stack_to_geotiff` writes the cube as a multi-band float32 GeoTIFF — one band
  per acquisition, oldest first, each band described by its timestamp and item
  id, with the ids/datetimes and the CC-BY attribution in the file tags — so
  the time axis survives into QGIS, GDAL or anything that isn't Python. The
  `umbra stack` CLI mirrors `umbra timescan`'s search-vs-URLs interface (the
  shared `--local` / `--index-db`, `--token`, `--fuzzy`, `--json` and
  acquisition-filter option groups) and warns, like the render commands do,
  when a selection mixes polarizations — a polarization difference on the time
  axis reads as change. Deterministic, no model call, no new dependency (the
  existing `[load]` extra), and fully offline-tested in `tests/test_load.py`
  against real on-disk GeoTIFFs: time ordering and provenance, intersection vs
  union (including the `NaN` padding), a step-edge test that two scenes on
  *different* source resolutions land their edge on the same output column
  (pixel alignment is the whole promise), the dB scale, the undated/empty/bad-
  extent/no-overlap errors, the multi-band round-trip and the CLI.

- **Per-site place-label baking (`umbra index bake --by-site`) and a pre-labelled
  published snapshot (`STRATEGY.md` §8 demo/hosting — "bake place labels into the
  published weekly snapshot").** `CatalogIndex.bake_places` resolved one
  reverse-geocode per *acquisition*, and OpenStreetMap Nominatim's usage policy
  caps traffic at ~1 request/sec — so labelling a whole catalog was an overnight
  job, and the weekly `catalog.db` everyone fetches shipped with no labels at all.
  Every `--local` map, gallery and `umbra demo` therefore fell back to the task
  codename ("Beet Piler - ND") unless the user ran a bake themselves.
  `bake_places(by_site=True)` geocodes **once per site** instead: Umbra files
  every pass over a site under one task directory, so acquisitions sharing a task
  *and* a ~11 km cell (`_SITE_CELL_DEGREES`) are resolved together from their mean
  centroid and all take that one label. A repeat-imaged archive is mostly repeat
  passes, so the throttled call count drops by roughly the average
  passes-per-site — the difference between "a whole catalog is impractical" and "a
  bounded step in the weekly build". The grouping is a pure, deterministic
  function (`index._site_groups`, insertion-ordered so a `--limit`ed batch is
  reproducible and resumable), and the label is a coarse place name for a
  footprint a few km across, so one per site is the same answer per-item
  geocoding converges on. A task whose passes straddle a cell boundary just costs
  an extra lookup — the failure direction is a redundant call, never a
  mislabelled item — and passes of one task that are genuinely far apart still get
  their own labels. `--limit` now caps *lookups* rather than items (the rate limit
  is what it exists to bound); the default per-item mode and everything else about
  the bake — idempotent, only `NULL` labels touched, an unresolved item retried
  next run — is unchanged. The weekly `publish-index.yml` gained a bounded,
  non-blocking `umbra index bake --by-site --limit 1200` step *before* the
  derived artifacts, so the fetched `catalog.db`, the stac-geoparquet export and
  the `catalog.pmtiles` basemap all arrive pre-labelled; a slow or unavailable
  geocoder costs the run some labels, never the publish. Needs no extra (the
  Nominatim call goes through the core `requests` session), no model call, and is
  offline-tested in `tests/test_index.py` with a counting stand-in geocoder
  (one-lookup-per-site, distant passes of one task kept apart, two sites sharing a
  cell kept apart, the `--limit` semantics, and the CLI flag).

- **Timescan and swipe views for the showcase's featured gallery — the last open
  R4 item (`DEMO_APP_GAPS.md` R4 / `STRATEGY.md` §8 demo polish).** The featured
  gallery precomputed exactly one thing: a two-or-three-date change composite per
  marquee site. But the same deterministic selection feeds the toolkit's other
  two comparators, and each answers a question the change composite can't — *what
  did this site do across its whole history?* and *what actually moved between
  these two dates?* `umbra showcase --featured-view {change,timescan,swipe}` now
  picks which:
  - **`timescan`** collapses a site's **entire** pass series into one
    `viz.timescan_composite` still (red = mean backscatter, green = peak, blue =
    temporal variability), so ground that came and went glows blue/cyan. It needs
    3+ passes rather than 2, and its caption counts every pass composited — not
    `--featured-frames` of them, which the view deliberately ignores.
  - **`swipe`** writes a self-contained `viz.swipe_map` **page** per site — two
    co-registered passes behind a draggable divider — over the same two frames
    `select_change_frames` picks for the change view, so the two views tell the
    same story about a site.
  The view is one record (`showcase.FeaturedView`, in `FEATURED_VIEWS`) carrying
  the four things that actually differ: the artifact extension, the passes a site
  must have, the tile shape and the section's copy. That last difference is the
  one the gallery had to grow for: a swipe map is HTML with no still to preview,
  so a `"page"` artifact renders as a **link card** in the same frame as an
  `"image"` tile — identical caption, identical provenance line, so the two
  shapes read as one gallery. `min_passes_for()` keeps the qualifying bar with
  the view, so `--featured-view timescan` drops the two-pass sites *before* any
  network work rather than failing a render. Everything else is unchanged:
  selection stays the pure `select_featured_sites`, rendering still goes through
  the injectable `featured_renderer` (so the whole path is offline-tested with no
  network and no `viz` extra), a site that won't render is still warned about and
  dropped rather than fatal, and the default is still `change` — a showcase built
  without the new flag is byte-identical to one built before it. Offline-tested
  in `tests/test_showcase.py`.
- **Click-to-quicklook SAR imagery over the *whole archive* — the tiles now
  reference each acquisition's COG (`DEMO_APP_GAPS.md` Path A / `STRATEGY.md` §8
  demo polish).** The whole-archive explorer (`umbra demo --pmtiles`, what the
  hosted `umbra showcase --unified` deploys) could tell you a scene exists, where
  it is and what it covers — but not what it *looks like*. Streaming the picture
  on click was the last capability the embedded-slice explorer had over it, for a
  data reason: vector tiles carried lean metadata and no per-asset COG URL. They
  do now. `build_pmtiles` writes two more properties on every feature (centroid
  *and* footprint): `cog`, a reference to the acquisition's GEC cloud-optimized
  GeoTIFF, and `bounds`, its footprint as the `"S,W,N,E"` string the shared
  driver places overlays with. So **any acquisition in the archive is one click
  from its actual radar image**, at whole-catalog scale, from a static page.
  The reference stays lean: the published product is a *sibling* of the item's
  STAC sidecar in the public bucket, so what is tiled is the bare filename
  (~30 bytes) and the page rebuilds the URL against the `stac_href` the tiles
  already carried — an absolute href that is not a sibling is stored whole
  instead, and only `http(s)` survives on either path (these strings come from
  remote metadata and end up in a `fetch()`). An asset that resolves to nothing
  anonymously fetchable is omitted entirely, so a scene without one shows no
  button rather than one that 404s. On the page side,
  `_lazy_imagery.driver_script(engine=…)` grew a **MapLibre placement** (an
  `image` source plus a `raster` layer, slotted under the acquisition layers via
  `window.umbraOverlayBeforeId` so the markers that opened it stay clickable)
  beside the existing Leaflet `imageOverlay` one; everything above the placement
  — the SRI-pinned geotiff.js load, the range-read, the overview pick, the
  percentile stretch, the canvas paint and the button state machine — remains a
  single implementation, as does the button builder both explorers call.
  `umbra tiles --cog-asset` picks the product (default `GEC`; `CSI` also works)
  and `--no-cog` writes the previous metadata-only archive; `umbra demo
  --pmtiles --no-lazy-imagery` builds the page without the driver. An archive
  tiled before this change simply shows no button, and the published weekly
  `catalog.pmtiles` gains the references on its next `publish-index.yml` run.
  Offline tested in `tests/test_pmtiles.py` (basename collapse, the absolute-href
  fallback, the driver's bounds order, unresolvable assets, the tile round trip
  through both layers, metadata fields, CLI), `tests/test_demo.py` and
  `tests/test_lazy_imagery.py` (both placements, id sanitising, layer ordering,
  and that the two engines share everything above the placement).
- **Footprint polygons in the whole-catalog vector tiles — coverage shape in the
  hosted explorer (`TODO.md` "Tile polygons, not just centroids" /
  `STRATEGY.md` §8 demo polish).** `umbra tiles` tiled one *centroid* per
  acquisition, so the whole-archive explorer (`umbra demo --pmtiles`, what
  `umbra showcase --unified` deploys) could only ever draw a marker — zooming in
  never revealed *what a scene covers*, and the embedded-slice explorer's
  footprint outline was the one thing it still had over the whole-archive one.
  `build_pmtiles` now writes each acquisition **twice**: its centroid in the
  `acquisitions` layer at every zoom, and its footprint polygon — clipped to
  every tile it touches — in a new `footprints` layer from
  `FOOTPRINT_MIN_ZOOM` (6) up, where a footprint first spans more than a pixel.
  Both explorers draw the new layer as a translucent fill plus an outline: in
  `umbra demo --pmtiles` the sidebar's one filter expression drives the markers
  and the outlines together (a hidden scene cannot leave its footprint drawn) and
  clicking a polygon opens the same detail panel as clicking its centroid, and
  `build_viewer`'s minimal page gets the same fill/outline pair and popup. The
  encoder stays **stdlib-only** — no tippecanoe, no geometry dependency: the MVT
  polygon command stream (MoveTo / LineTo / ClosePath), Sutherland–Hodgman
  clipping against the buffered tile box, and the clockwise exterior-ring winding
  the spec requires are all a few pure functions, and the archive is verified by
  decoding its own output back into rings. A footprint spanning more than half the
  globe (a bbox wrapping the antimeridian, where the lon/lat ring is not the
  footprint) keeps its centroid and is not tiled, rather than painting a
  world-wide row. `umbra tiles --no-footprints` writes the previous
  centroids-only archive, and `--footprint-min-zoom` moves the threshold; the
  archive metadata advertises whichever layers are actually present, so an older
  centroids-only archive simply draws no outlines in the new viewers. Offline
  tested in `tests/test_pmtiles.py` (polygon + property round trip through a
  full geometry decoder, the min-zoom boundary, seam clipping into both tiles,
  winding regardless of input order, the antimeridian guard, metadata, CLI) and
  `tests/test_demo.py`; the encoded tiles were also cross-checked against an
  independent MVT decoder.
- **Whole-archive interactive explorer — `umbra demo --pmtiles` and the one-page
  `umbra showcase --unified` (`DEMO_APP_GAPS.md` Path A / `STRATEGY.md` §8 demo
  polish).** The explorer and the whole-catalog map were separate artifacts for a
  structural reason: `umbra demo` embedded its acquisitions in the page as JSON,
  which is the right shape for a search result but caps the explorer at whatever
  fits in a download, so covering the archive meant `umbra tiles`' click-only
  MapLibre viewer with no filters. `umbra demo --pmtiles PATH-OR-URL` (or
  `build_demo(pmtiles_url=…)`) removes the cap: the page swaps its embedded-slice
  Leaflet cluster for a **MapLibre GL vector layer over a whole-catalog
  `.pmtiles` archive** — the one `umbra tiles` writes and `publish-index.yml`
  already publishes — and the browser range-reads only the tiles in view, so
  **every acquisition in the catalog is explorable from a page that stays a few
  kilobytes**. The sidebar is unchanged and now filters the whole archive: the
  free-text search, date range and product chips compile to MapLibre filter
  expressions evaluated inside the tiles (`index-of` over place/id, lexical date
  bounds, per-product exclusions), matching `passesFilter`'s semantics down to
  "a missing date never fails a date filter". The detail-row builder, the baked
  thumbnail preview (G6) and the whole "Analyze this view" panel (R4) were
  factored into one shared, map-engine-agnostic script both explorers drive, so
  the two modes cannot drift; a server-backed whole-archive page keeps both
  `umbra serve` affordances. The trade is documented rather than papered over:
  vector tiles carry centroids and lean metadata, not footprint polygons or
  per-asset COG URLs, so the footprint outline and the on-click "Get SAR image"
  overlay stay embedded-slice features. `umbra showcase --unified` /
  `assemble_showcase(unified=True)` builds the showcase on top of it as **one
  page instead of two**: `explore.html` reads the copied `catalog.pmtiles`
  directly, `map.html` is not written, and the landing page sends a visitor to a
  single explorer covering the whole catalog *with* the filters. Both modes
  refuse to silently ignore what they can't honour — `--pmtiles` with a search
  option, or `--unified` without a basemap / with `--no-explore`, is an error, not
  a quietly different page. `.github/workflows/docs.yml` now builds the hosted
  showcase with `--unified` (still `continue-on-error`, still main-only). Needs no
  extra and no network to generate; deterministic and offline-tested in
  `tests/test_demo.py` and `tests/test_showcase.py`, and the generated page was
  exercised end to end in a real browser (archive range-reads, every filter,
  click-to-detail). Without `--pmtiles` / `--unified` every existing page is
  unchanged. This closes the "wire the PMTiles source into `umbra demo`" and
  "wire the PMTiles basemap into the explorer itself" follow-ons in `TODO.md`.
- **Precomputed change composites on the static showcase — `umbra showcase
  --featured` (`DEMO_APP_GAPS.md` R4 / `STRATEGY.md` §8 demo polish).** The
  hosted showcase gave a first-time visitor a map and an explorer but *no SAR
  imagery*: seeing what this archive actually looks like meant clicking into the
  explorer and waiting on a render. `umbra showcase --featured N` now renders a
  change composite for the `N` most repeat-imaged sites in the catalog ahead of
  time, writes them to a relocatable `featured/` subdirectory, and puts them on
  the landing page as a captioned gallery — so the first thing a visitor sees is
  *what SAR change looks like*, with no render round-trip, no account and no
  server. Site selection is a new pure function,
  `select_featured_sites(items, count=…, min_passes=…)`: Umbra files every pass
  of a site under one task directory, so the tasks with the most acquisitions in
  the candidate pool are exactly the ones worth precomputing; sites are ranked by
  pass count then task name, making the choice reproducible for a given pool
  rather than dependent on iteration order. A maintainer can curate explicitly
  instead with a repeatable `--featured-area` (matched like `--area`, one search
  per name), and `--featured-frames 2|3` picks the two-colour (green = new or
  brighter, magenta = gone or dimmer) or three-date temporal-RGB view; each
  caption states the pass count, the date range and the colour semantics, so a
  tile is never a picture without provenance. Rendering goes through an
  injectable `featured_renderer` (defaulting to the existing
  `viz.select_change_frames` + `viz.save_change_composite`, which stream only a
  downsampled overview per scene), so the whole feature is offline-tested with no
  network and no `viz` extra; a site whose asset won't render is warned about and
  dropped, never fatal — one bad scene costs its own tile, not the showcase. New
  public API `select_featured_sites` / `FeaturedSite`; `--featured` defaults to
  `0`, so a showcase built without it is byte-identical to before and stays
  stdlib-only. The `.github/workflows/docs.yml` Pages job now passes
  `--featured 6` (still `continue-on-error`, still main-only). This closes the
  R4 "precompute showcase artifacts for ~6–10 curated sites" item on the
  `STRATEGY.md` §8 critical path.
- **A SessionStart hook + permission allowlist for remote coding-agent sessions
  (`STRATEGY.md` §8 agent-session hardening).** A fresh Claude-Code-on-the-web
  container arrives with `uv` and the CLI linters on `PATH` but *without*
  umbra-py installed, so `pytest`, `mypy`, and the `umbra` CLI all fail until an
  agent hand-runs the editable install — the first turn of every web session was
  spent re-deriving the setup in `AGENTS.md` §3. Added `.claude/hooks/session-start.sh`,
  a `SessionStart` hook registered in a new `.claude/settings.json`, that installs
  the package editable with **every** extra
  (`uv pip install --system -e ".[dev,all,mcp,serve,ai,langchain,llamaindex]"`),
  mirroring CI's `test-all-extras` job so the whole offline suite runs rather than
  import-skipping the viz / serve / convert / load / agent modules. It is gated on
  `$CLAUDE_CODE_REMOTE` (a no-op locally, where `AGENTS.md` §3's venv flow owns
  setup), idempotent (safe on resume/clear/compact — `uv` no-ops when nothing
  changed), non-interactive, and synchronous so the environment is ready before
  the first agent turn (no race where a check runs before its dependencies exist).
  The same `settings.json` ships a conservative `permissions.allow` list for the
  project's documented dev loop and read-only commands (`uv pip install`,
  `ruff check`/`format`, `mypy`, `pytest`, `pre-commit run`, `umbra`, and
  read-only `git status`/`diff`/`log`/`show`/`branch`/`add`), cutting the
  permission prompts a web session would otherwise raise for its own CI checks —
  no outward or destructive command (push, commit, reset) is pre-approved. No
  runtime code changes and nothing on the published package (the `.claude/`
  tree is dev tooling only), so it cannot affect users or the test suite. This
  closes the agent-session-hardening item on the `STRATEGY.md` §8 critical path,
  advancing the project's "agents are users; users are agents" principle
  (`STRATEGY.md` §7.5) on the surface where this repo is itself developed.
- **`narrate_change` on the LangChain and LlamaIndex tool surfaces — full
  agent-framework parity (`AI_INTEGRATION_IDEAS.md` C2 / `STRATEGY.md` §5.4
  agent-reach follow-on).** The C2 number-grounded change-narration tool
  (`umbra change --narrate`) shipped on the MCP server as `narrate_change` — the
  sibling of `describe_scene` — but the LangChain and LlamaIndex wrappers still
  exposed only twelve of the server's thirteen tools, so `narrate_change` was the
  one MCP tool an agent built on those two frameworks could not reach. It is now
  registered on both `umbra_py.langchain.umbra_tools()` and
  `umbra_py.llamaindex.umbra_tools()`, bringing all three front doors (MCP,
  LangChain, LlamaIndex) to the identical inventory. As with every JSON tool, it
  is the **same** deterministic callable the MCP server exposes — imported
  verbatim, not re-wrapped — so the surfaces cannot drift, and it holds the
  determinism boundary (`AI_INTEGRATION_IDEAS.md` §A4): the change composite and
  the per-block decibel grid are produced deterministically, the model **only
  interprets** (its reply passes the `parse_narration` boundary), and every
  narration is stamped with the CC-BY attribution and an `AI_PROVENANCE` note. It
  is now the **second** opt-in model tool on these surfaces (with
  `describe_scene`), gated on the `[ai]` key exactly as on the CLI and MCP, so it
  never runs implicitly; a text-only or no-`viz` install still drops it via
  `include_render=False` only for the render tools (the model tools gate
  themselves at call time). Offline-tested in `tests/test_langchain.py` and
  `tests/test_llamaindex.py` (surface parity, same-callable no-drift, an
  end-to-end narration through each wrapper with an injected narrator + render,
  and the mixed-polarization refusal) with no `[ai]`/`[viz]` extra, no key, and no
  network; the README LangChain/LlamaIndex tool inventories and the module
  docstrings are updated. This completes the MCP → LangChain → LlamaIndex reach
  for the change-narration capability, closing the last named tool-parity
  follow-on across the agent front doors.
- **A branch-coverage gate + Codecov badge in CI (`CODEBASE_ANALYSIS.md` P2 #16
  / `STRATEGY.md` §8 structural debt).** The test suite was already
  comprehensive (991 offline tests mirroring the whole package) but nothing
  turned that into a visible, enforced number. The `test-all-extras` CI job —
  the one job that installs every optional extra, so the visual / serve /
  convert / agent modules actually execute instead of import-skipping — now runs
  `pytest` under `coverage` with branch coverage and a `--cov-fail-under=88`
  floor, so a change that drops coverage below the bar fails CI. The floor sits a
  couple of points under the current ~90 % branch figure so ordinary
  fluctuation across Python patch releases doesn't turn the build red on an
  unrelated PR. Coverage config lives in `[tool.coverage.run]` /
  `[tool.coverage.report]` in `pyproject.toml` (source-scoped to `umbra_py`,
  branch mode, with the interactive-only / `TYPE_CHECKING` branches excluded);
  `pytest-cov` was already a `[dev]` dependency, so no new install. The job also
  uploads `coverage.xml` to Codecov (non-blocking — `fail_ci_if_error: false` —
  and gated by `codecov.yml` to `informational` status with no PR comments, so
  an unconfigured or flaky Codecov integration can never block a merge; the
  enforcing gate is the local `--cov-fail-under`), and the README gains CI and
  Codecov badges alongside the existing ones. Deterministic, offline, and adds
  no runtime dependency — purely a quality signal for contributors and a
  credibility marker for the ecosystem-visibility push (`STRATEGY.md` §5.3).
- **MCP `narrate_change` tool — a number-grounded VLM reading of *what changed*
  over the flagship server (C2 second half, `AI_INTEGRATION_IDEAS.md` C2).** The
  CLI's `umbra change --narrate` (module `narrate.py`) has a vision model narrate
  the change between two or three passes of a site, grounded in a deterministic
  per-block decibel grid so every statement cites a number rather than vibes — but
  an MCP/agent client could only get the raw `change_composite` image, not the
  reading. Added `narrate_change(urls, asset, db, max_size, model)` to `umbra-mcp`,
  the sibling of `describe_scene` and now the second (and only other) tool on the
  server that consults a model: it composites the same-polarization passes, computes
  the `ChangeStats` dB grid, hands both the picture and the numbers to the model
  behind the packaged SAR-literacy prompt, and returns the validated
  `{summary, changes[], confidence, caveats[]}` narration with the grid embedded as
  `change_stats` so an agent can audit every statement against a recomputable number.
  It wraps the shipped `narrate()` unchanged, so the CLI and MCP surfaces cannot
  drift; it refuses mixed polarizations before any render or model call (the same
  guard `change_composite` holds); and it holds the determinism boundary
  (`AI_INTEGRATION_IDEAS.md` §A4) — the composite and the grid are deterministic, the
  model only interprets (its reply passes the `parse_narration` boundary and never
  becomes a coordinate, URL, or measurement), and every narration is stamped with the
  CC-BY attribution and the `AI_PROVENANCE` note. Gated (like the CLI) on the `[ai]`
  extra plus a user-supplied vision key: it raises a helpful setup error when none is
  configured, so it never runs implicitly. Shipped with a packaged `narrate-change`
  workflow prompt (search → composite → narrate → present as an AI interpretation,
  citing the `change_stats` blocks) and announced in the server's instructions.
  Because the model call is an injectable `Narrator` and the render an injectable
  `ChangeRenderer`, the whole path is offline-tested in `tests/test_mcp_server.py`
  with deterministic stand-ins — validated narration returned and grid carried
  through, the mixed-polarization refusal, and the missing-key setup error — with no
  `[ai]`/`[viz]` extra and no network. This completes the C2 "VLM-in-the-loop" pair
  (`describe_scene` reads one scene; `narrate_change` narrates change) on the
  AI-native surface.
- **`umbra ask` now plans the SAR acquisition-property filters (`STRATEGY.md`
  §3 acquisition-filter follow-on).** The polarization / incidence-angle /
  resolution filters already shipped on every other surface — `umbra search`,
  the local index, the Canopy archive, the render/analysis commands, the MCP
  `search_catalog` tool, and the `umbra serve` STAC Query extension — but the
  natural-language planner (`umbra ask`) couldn't emit them, so "VV scenes at
  low incidence over Utah" lost the radar half of the request. The planner's JSON
  schema now carries `polarizations`, `min_incidence`, `max_incidence`, and
  `max_resolution` (`SearchPlan` fields, `_PLAN_KEYS`, and the system-prompt
  schema block), and `parse_plan` — the determinism boundary — validates each one
  before it can become a filter: polarizations are upper-cased and de-duplicated
  (an open `VV`/`VH`/`HH`/`HV` set, unvalidated against a fixed vocabulary exactly
  like `serve.parse_polarizations`, so an unknown value simply matches nothing),
  incidence and resolution are coerced to positive floats (a hallucinated
  `max_resolution: 0` is a self-describing `AskError`, not a silent bad query),
  and inverted `min_incidence`/`max_incidence` bounds are rejected like a
  start-after-end date. The resolved filters render into the audited
  `umbra search …` command (`--pol` repeatable, `--min-incidence` /
  `--max-incidence` / `--max-resolution`) via `plan_to_argv`, flow through
  `SearchPlan.to_search_kwargs()` into the same
  `UmbraItem.matches_filters` predicate every other surface shares, and appear in
  the `--json` plan. No model call beyond the existing planning step and no new
  dependency — the whole path is offline-tested in `tests/test_planner.py`
  (validation of each filter, upper-casing/de-duplication, the positive-number and
  inverted-bounds rejections, command rendering, and a CLI `--run` that forwards
  every filter to the backend). This closes the last named surface in the
  acquisition-filter follow-on: the filters are now reachable from a plain
  sentence, deterministically validated.
- **GitHub Pages showcase of the static `umbra demo` / `catalog.pmtiles`
  explorer (`STRATEGY.md` §8 demo/hosting, `DEMO_APP_GAPS.md` G7).** The toolkit
  already produced every piece of a zero-install catalog demo — the
  whole-archive PMTiles basemap (`umbra tiles`), the interactive explorer
  (`umbra demo`), the published snapshots a fresh install fetches with no crawl —
  but had nowhere to *put* them. New `umbra showcase` (module `showcase.py`,
  `build_showcase` / `assemble_showcase`, both exported) composes them into one
  self-contained, hostable directory: `index.html` (a dependency-free landing
  page linking the pieces plus install/docs/source, carrying the mandatory CC-BY
  attribution and the not-affiliated disclaimer), `map.html` (the MapLibre viewer
  over the whole-catalog basemap, with the `.pmtiles` archive copied in beside it
  so the folder is relocatable), and `explore.html` (the `umbra demo` explorer
  over a gathered slice — `--max-per-task 1` by default for a one-pin-per-site
  overview). The basemap comes from a local `--pmtiles PATH` or `--fetch-pmtiles`
  (the same published `catalog.pmtiles` `umbra tiles --fetch` pulls); `--no-explore`
  makes a map-only page. The `.github/workflows/docs.yml` Pages job now runs
  `umbra index fetch` + `umbra showcase` after the mkdocs build and publishes
  `site/showcase/` beside the docs — a **non-blocking, main-only** step, so a
  not-yet-published `catalog-index` release or a failed fetch never breaks the
  docs deploy. It is a *composer*, not a new renderer: `build_showcase` is a pure
  string builder and `assemble_showcase` only copies a file and calls the existing
  `save_viewer` / `save_demo` writers, so it needs no network and no `viz` extra
  and is fully offline-tested in `tests/test_showcase.py` (landing-page cards /
  stats / attribution and card-dropping; the three-file assemble + basemap copy;
  map-only / explore-only; the same-file-in-dest guard; and the CLI's build,
  `--no-explore`, `--fetch-pmtiles`, and the source/guard errors). Documented in
  `docs_src/deploy.md` and linked from the docs landing page. This closes the
  GitHub Pages half of the G7 packaging/hosting gap — the last named demo/hosting
  code item in `STRATEGY.md` §8.
- **One-command Docker self-hosting of `umbra serve` + a `/healthz` probe
  (`STRATEGY.md` §8 demo/hosting, `DEMO_APP_GAPS.md` G7).** The repo now ships a
  `Dockerfile`, a `docker-compose.yml`, a `.dockerignore` and a
  `docker-entrypoint.sh`, so `docker compose up` (or `docker run -p 8000:8000
  umbra-py`) stands the read-only STAC API up with no local Python install. On
  first boot the entrypoint fetches the published catalog index snapshot into a
  `/data` volume (no S3 crawl), then serves `/search`, `/collections`, the
  OpenAPI docs at `/docs` and a new **`GET /healthz`** liveness/readiness probe.
  `/healthz` returns `200` once the HTTP server is up (liveness) and its body's
  `ready` flag reports whether the search backend can answer queries yet
  (readiness — the first-boot fetch may still be in flight), so it fits a Docker
  `HEALTHCHECK` and a Kubernetes probe directly (new pure builder
  `serve.health_document`, wired into `build_app`). The image runs unprivileged,
  persists the index + render cache to the `/data` volume, and doubles as the
  CLI (`docker run --rm umbra-py search …`); env vars tune it (`UMBRA_SERVE_LIVE`
  walks S3 with no index, `UMBRA_FETCH_INDEX=0` skips the fetch, `UMBRA_INDEX_URL`
  points at a mirror, `UMBRA_SERVE_ARGS` forwards flags), and a
  `--build-arg UMBRA_EXTRAS=serve,viz` build adds the on-demand `/artifacts/…`
  render endpoints. A new `docker.yml` CI job builds the image and smoke-tests
  it end to end (CLI passthrough, compose validation, and a live-mode server
  answering `/` and `/healthz` with no external network); `/healthz` is
  offline-tested in `tests/test_serve.py`. Docs: a new `docs_src/deploy.md`
  reference and a README "Self-host it with Docker" section. This closes the
  Docker half of the demo/hosting critical path (the static GitHub Pages
  showcase deploy remains open).
- **SAR acquisition-property filters on the `umbra serve` STAC Query extension
  (`STRATEGY.md` §3 "every surface agrees", `TODO.md` acquisition-filter
  follow-on).** The `umbra serve` STAC API previously exposed only
  `product_types` / `area` / `fuzzy` over `/search` and
  `/collections/{id}/items`, even though every other surface (search, the render
  commands, the MCP server) can also filter by the SAR-native properties. It now
  filters on them too — three ways, matching the existing product-type pattern:
  GET params (`?polarizations=VV,VH&min_incidence=20&max_incidence=40&max_resolution=0.5`),
  plain top-level POST body fields, and a proper STAC **Query extension** object
  using the namespaced property names (`{"sar:polarizations": {"in": ["VV"]}}`,
  `{"view:incidence_angle": {"gte": 20, "lte": 40}}`,
  `{"sar:resolution": {"lte": 0.5}}`). `parse_query` gained a numeric range
  operator (`gte`/`lte` together for the incidence range) alongside its scalar
  operators and now returns a `QueryFilters` NamedTuple; an unsupported operator
  or a non-numeric value is a hard `400`, never a silent drop. The filters push
  down to the same `UmbraItem.matches_filters` predicate every other surface
  shares (no new filtering logic, no schema change, no model call, no new
  dependency), and GET pagination carries them into the `next` link — so
  `pystac-client`, the QGIS STAC plugin and OpenAPI-driven agents can now filter
  the archive by polarization / incidence / resolution. Offline-tested in
  `tests/test_serve.py`. This was the last discovery surface that couldn't filter
  on the SAR properties.
- **SAR acquisition-property filters on the render/analysis commands (`STRATEGY.md`
  §3 "every surface agrees", `TODO.md` acquisition-filter follow-on).** The
  `--pol` / `--min-incidence` / `--max-incidence` / `--max-resolution` filters
  shipped on `umbra search` and the MCP `search_catalog` tool; they now also apply
  to the six commands that render or export the archive — `umbra change`,
  `timescan`, `swipe`, `gallery`, `map` and `chips`. Each grows the shared
  acquisition-filter options, threaded through the common `_gather_items` search
  helper, so e.g. `umbra change --area "Beet Piler" --pol VV` gathers a
  single-polarization series *directly* instead of relying on the after-the-fact
  mixed-polarization warning (HH and VV image different physics, so a mixed change
  composite can show polarization difference as apparent change). The filters
  apply only in search mode (passing explicit item URLs is unaffected), reuse the
  one shared predicate (`UmbraItem.matches_filters`) every other surface uses (no
  new filtering logic, no schema change, no model call, no new dependency), and
  the set values are recorded in the `--json` render manifest's `parameters` for
  reproducibility (only when set, so an unfiltered render's manifest is unchanged).
  Offline-tested in `tests/test_acquisition_filters.py`. Still ledgered in
  `TODO.md`: exposing the filters on the `umbra serve` STAC Query extension and in
  `umbra ask`.
- **SAR acquisition-property search filters — polarization, incidence angle and
  resolution — across every discovery surface (`STRATEGY.md` §3 "discovery is
  the moat", `AI_INTEGRATION_IDEAS.md` §B2 STAC follow-on).** Search already
  filtered by geography (`bbox` / `intersects` / `place`), date and product type
  — but not by the SAR-native properties an analyst reaches for next, so those
  had to be filtered client-side after the fact (the "same 500 lines of glue"
  the strategy names). `search(...)` now accepts `polarizations` (keep items
  exposing at least one, e.g. `["VV"]` — the filter that keeps a change
  comparison like-with-like), `min_incidence` / `max_incidence` (view
  incidence-angle bounds in degrees) and `max_resolution` (keep items at least
  this fine, in metres). They are threaded through **every discovery surface so
  the backends agree**: the live open-bucket walk, the local `CatalogIndex`, the
  read-through `search_live`, the Canopy commercial archive (applied client-side
  like `product_types`), `umbra search` (`--pol` / `--min-incidence` /
  `--max-incidence` / `--max-resolution`), and the MCP `search_catalog` tool
  (so agents filter too). The metadata is already parsed on every `UmbraItem`
  (`sar:polarizations`, `view:incidence_angle`, `sar:resolution_*`), so no
  schema change is needed — the shared predicate `UmbraItem.matches_filters`
  runs in Python on each candidate, exactly as the polygon test does. Each
  filter is a **hard predicate**: a set filter excludes an item lacking that
  property (the STAC Query-extension convention), deliberately unlike the
  geometric filters' coarser-datum fallback. No model is called and no
  dependency is added; the whole surface is offline-tested
  (`tests/test_acquisition_filters.py` across the predicate, index, live walk,
  archive, CLI and MCP). These filters now also reach the render/analysis
  commands (`change`, `timescan`, `swipe`, `gallery`, `map`, `chips` — see the
  entry above); wiring them into the `umbra serve` STAC Query extension and
  `umbra ask` remains an additive follow-on in `TODO.md`.

### Fixed
- **The SAR overlay's de-rotation now actually runs: geotiff.js resolves tag
  values lazily, so reading the affine as a property always saw `undefined`.**
  The georeferenced placement shipped earlier read the raster's affine with
  `fd.ModelTransformation`, a plain property access on the object
  `getFileDirectory()` returns. A real geotiff.js `FileDirectory` does not carry
  tags that way — values are resolved on demand through `hasTag` / `loadValue`,
  and `loadValue` is asynchronous because a tag stored outside the IFD costs
  another range request. So the read yielded `undefined` for every file,
  `rasterGeoreference` returned null, and the driver took its own
  unreadable-georeferencing fallback: the STAC bbox stretch it was written to
  replace. The published explorer kept drawing every scene rotated, exactly as
  before, with no error anywhere to say so.

  The affine is now read through `hasTag`/`loadValue`, which makes the placement
  step a promise the driver awaits before decoding pixels. Behaviour is
  otherwise unchanged: same envelope, same resample, same bbox fallback for a
  file that genuinely carries no georeferencing.

  **The test doubles are what let this through, so they were the real fix.**
  `fakeImage` handed back a plain object with the tags as properties, so the
  suite validated an interface geotiff.js does not have — every assertion passed
  against a shape that never occurs. The stub now implements `hasTag`/`loadValue`
  and resolves on a later turn of the event loop, so it exercises the accessors
  the library actually exposes and would catch an ordering mistake in the
  driver's chain. Verified beyond the doubles by running the georeferencing code
  *extracted from the generated `explore.html`* against the real pinned
  geotiff.js bundle and the real Black River GEC: it now places from the file's
  own affine, with an envelope matching GDAL's dataset bounds to the last digit.
- **`umbra-mcp` runs on the current SDK again — ported to `mcp` 2.0 — and its
  image tools reach a client for the first time.** `mcp` 2.0.0 renamed
  `mcp.server.fastmcp` to `mcp.server.mcpserver` and `FastMCP` to `MCPServer`.
  Nothing else in the surface this server uses moved: the constructor,
  `add_tool`, `resource`, `prompt`, `run` and `Image` all keep their
  signatures, so the port itself is the rename plus the extra's floor moving
  from `mcp>=1.2,<2` to `mcp>=2`. The old module does not exist in 2.x and the
  new one does not exist in 1.x, so the server cannot straddle it — hence a
  floor rather than a range.

  **Driving the ported server over a real stdio client surfaced a bug that was
  never about 2.0.** `quicklook`, `change_composite` and `timescan` — the three
  tools this module's docstring calls the differentiator, the ones that return
  the rendered PNG so the model *sees* the radar scene — failed every single
  invocation with `ToolError: Unable to serialize unknown type`. The SDK
  derives a structured-output schema from a tool's return annotation and then
  serialises the result against it; these return `list[Any]` holding an `Image`
  content block, which has no JSON form. It reproduces identically on 1.29, so
  it had been broken since structured output landed — invisible because every
  test called the tool *functions* directly and nothing exercised the path a
  client actually takes. The three are now registered with
  `structured_output=False`; the eleven JSON tools keep their output schema.

  Two tests close the gap that hid it: one drives `quicklook` through
  `server.call_tool` and asserts an `ImageContent` + `TextContent` pair comes
  back, the other asserts the opt-out is scoped — image tools carry no output
  schema, JSON tools still do. Verified end to end against a real stdio session
  too (`initialize` → `tools/call`), which returns an `image/png` block and the
  attribution caption.
- **SAR overlays now land on the ground they image: the browser driver reads the
  COG's own georeferencing instead of stretching it onto the footprint bbox.**
  Clicking "Get SAR image" on the published showcase explorer put the scene on
  the map rotated — the coastline running the wrong way across Black River,
  Jamaica, with the imagery ignoring the OpenStreetMap basemap underneath it.

  The premise the placement rested on was wrong. `_lazy_imagery.py` documented
  Umbra's GEC rasters as "north-up UTM" and reasoned that stretching such a grid
  onto its lat/lon bounding box skews it only slightly over a few-km scene. A GEC
  is **not** north-up: its pixel grid is rotated to the collect geometry, which
  is why the four grid corners *are* the acquisition's STAC footprint polygon and
  why that polygon is drawn as a tilted quad in the first place. The angle is
  whatever the collect azimuth was — 77° for the reported scene, and different
  for every acquisition in the archive — so bbox placement was not a small skew
  but a rotation of the whole image. (The CRS was wrong in the docstring too:
  GECs ship both in WGS84 geographic *and* in WGS84 UTM zones.)

  The driver now reads the raster's affine (a GeoTIFF `ModelTransformation`) and
  CRS geokey from the full-resolution IFD — overview IFDs carry no geo tags —
  resamples the decoded overview onto a north-up lat/lon grid, and places *that*
  at the grid's own envelope. Inverting the two CRSs GECs use costs a few lines
  of Snyder series rather than a second CDN dependency, and the pixel → lon/lat
  map is an affine fit through the four grid corners: exact for a geographic
  raster, and within a couple of metres for a UTM one over a scene this size —
  well inside one rendered pixel. A file whose georeferencing the driver can't
  read still renders on the item's `data-bounds` footprint bbox, exactly as
  before. Both engines are fixed by one change: the Leaflet build (`umbra map
  --lazy-imagery`, the embedded-slice `umbra demo`) and the MapLibre build (the
  whole-archive PMTiles explorer, which is what the showcase publishes).

  **The same premise had broken the Python overlay path**, which the old
  docstring pointed at as the pixel-accurate alternative: `viz._read_sar_band`
  skipped its `WarpedVRT` whenever the source was already EPSG:4326, so a
  rotated geographic GEC — the majority of the recent archive — went onto
  `folium` maps unwarped and just as rotated. It is now warped whenever the grid
  is not north-up, which is the condition that actually matters. `umbra view`,
  the change/timescan composites and `umbra chips` were never affected (they
  warp unconditionally or keep the native grid).

  The arithmetic is offline-tested for real rather than grepped for: the
  driver's georeferencing chunk is exercised under `node` against the reported
  acquisition's actual transform (envelope, per-pixel source lookup, the
  north-up and unreadable-CRS paths) and its UTM inverse is checked against
  pyproj's answer in both hemispheres; the Python path gets a rotated-GeoTIFF
  regression test.
- **The `mcp` extra is capped below 2.** `mcp` 2.0.0 was published on
  2026-07-28 and removed the `mcp.server.fastmcp` module, which
  `umbra_py.mcp_server` imports `FastMCP` and `Image` from. The extra asked for
  `mcp>=1.2` with no upper bound, so from that release onward a fresh
  `pip install "umbra-py[mcp]"` resolved to a version the server cannot import
  at all — `umbra-mcp` died with `ModuleNotFoundError` before serving a single
  tool, and CI's all-extras job stopped at collecting `tests/test_mcp_server.py`.

  `mcp>=1.2,<2` is the honest constraint for code written against the 1.x
  FastMCP API: it resolves to 1.29.0, the newest release that still exposes the
  module. This is a pin correction, not a migration — porting `mcp_server.py` to
  the 2.0 API is its own change, and the cap carries a comment saying so, the
  same shape as the existing `ruff` cap right below it.
- **The weekly catalog publish now actually publishes: `umbra tiles --index-db`,
  not `--db`.** Both of the only two `Publish catalog index` runs this project
  has ever had died in the same place — `umbra tiles --local --db catalog.db`,
  answered with `Error: No such option '--db'`. The gather commands spell the
  index path `--index-db` precisely because `--db` already means the decibel
  stretch on the render commands, and the workflow had the render spelling.

  The blast radius was everything the project publishes, because the tiling step
  ran *before* the release step: a completed bucket crawl, 2 725 freshly baked
  place labels and a good stac-geoparquet export were thrown away with it, so the
  rolling `catalog-index` release was **never created at all**. `umbra index
  fetch` — the "instant local search, no crawl" the README leads with — 404'd for
  the project's entire existence, and with it `umbra-open-data.parquet`,
  `catalog.pmtiles`, the thumbnail sidecar and the GitHub Pages showcase built
  from them. The two earlier entries below fixed layers of the *same* outage
  (the export crash that broke the first run, and the docs job that then hid the
  missing showcase); this is the one that was still keeping the release empty.

  Two changes, because the typo and the damage it did are separate faults:

  - **The invocation is corrected**, and the ordering with it. Each artifact is
    now uploaded by the step that builds it, and the crawl is published *before*
    anything is derived from it — the index is the promise, the basemap is
    derived from the index, and a failure deriving the basemap should cost the
    basemap and not the snapshot. The tiling step stays blocking, so the run
    still goes red; it just no longer takes eight minutes of crawl down with it.
  - **The drift fails a pull request instead of a Monday morning.** New
    `tests/test_workflows.py` extracts every `umbra …` invocation from
    `.github/workflows/*.yml` — including ones continued across backslashes and
    ones nested in `$(…)` substitutions — and parses each against the real Click
    command tree, so an option renamed in `src/` and not in the workflow fails on
    the commit that renames it. This is the same shape as the gather-command
    parity suite: the workflows are the only CLI callers that nothing else
    exercises, since `publish-index.yml` runs weekly and `docs.yml`'s showcase
    step only on `main`. The scan asserts what it found, so a matcher that
    silently stops matching cannot pass quietly — the lesson of the
    `continue-on-error` entry below, applied to the test itself.
- **`umbra tiles` now tiles the baked place label, not just the task codename.**
  `pmtiles._item_properties` set each tiled feature's `place` from `item.task`,
  so a `umbra tiles --local` over a baked index — including the published
  `catalog.pmtiles` — showed codenames in the whole-archive explorer while the
  same index rendered real place names everywhere else. It now prefers
  `item.place` and falls back to the task, matching what `umbra demo` and the
  stac-geoparquet export already did.
- **`umbra index export` (stac-geoparquet) no longer crashes on catalog
  drift in the `providers` property (issue #102).** Most Umbra acquisitions
  encode the STAC `providers` property as a list of provider objects
  (spec-correct), but a handful carry a single bare object. stac-geoparquet
  infers one Arrow type per column, so a column that is a list on some rows
  and a scalar on others aborted the whole export with
  `ArrowInvalid: cannot mix list and non-list, non-null values`. This crashed
  the weekly `publish-index` workflow so the rolling `catalog-index` release
  was never produced, which in turn made the live catalog canary fail with a
  404 fetching the missing `catalog.db`. `export_geoparquet` now normalizes
  any property that drifts between list and scalar across the exported items,
  wrapping the scalar occurrences in single-element lists — lossless, and for
  `providers` the spec-correct shape (`item.raw` is never mutated). The live
  canary also now skips, rather than errors, when the `catalog-index` release
  asset isn't published yet, since that availability gap is not the catalog
  drift the canary exists to catch. Covered by `tests/test_export.py`.
- **The hosted showcase no longer 404s indefinitely after a missed index
  publish.** Same root cause as the entry above, one layer further out. With no
  `catalog-index` release to fetch, the docs workflow's showcase step died on
  `umbra index fetch` before `umbra showcase` ever ran, so `site/showcase/` was
  simply absent from the deployed site and
  `https://reesehammer.github.io/umbra-py/showcase/` — a link both `docs_src/index.md`
  and `docs_src/deploy.md` advertise — returned a 404. Two things kept it that
  way. The step was `continue-on-error: true`, which reports a *failed* step as
  having succeeded: the Docs run went green, so nothing anywhere said the
  showcase had been dropped. And the docs only rebuild on a push to `main`,
  while the showcase's content comes from the release rather than the repo — so
  even once an index was finally published, the site kept serving the
  showcase-less build until some unrelated commit happened along.

  The step now swallows the failure explicitly instead, emitting a warning
  annotation and a job-summary note naming the likely cause, so the deploy still
  goes out (it must — the docs are not hostage to the catalog snapshot) but the
  run that dropped the showcase says so. It also verifies `site/showcase/index.html`
  exists rather than trusting the exit status, and removes a partially written
  directory, since half a showcase deploys worse than none. Separately, Docs now
  also runs on `workflow_run` completion of **Publish catalog index**, so a newly
  published snapshot refreshes the showcase — and a first one makes it appear —
  without waiting for the next unrelated push.
- **CC-BY data attribution now shown on the interactive maps
  (`DEMO_APP_GAPS.md` G8).** Umbra open data is CC-BY-4.0, which requires the
  data credit be displayed wherever the data is used. The Folium maps
  (`umbra map`, `umbra map --timeline`, `umbra swipe`) surfaced the notice only
  inside per-marker popups, while the default basemap credited only the
  OpenStreetMap *tiles* — the Umbra footprints and SAR overlays drawn on top
  (the licensed data) had no visible attribution. A shared `viz._add_attribution`
  helper now registers `constants.ATTRIBUTION` with Leaflet's attribution control
  on every generated map, so the credit sits beside the OSM notice — the standard
  place a web map shows its data sources, matching what `umbra demo`,
  `umbra gallery`, and `umbra tiles` already do. Emitted as a Folium
  `MacroElement` (the same runtime-script mechanism as the swipe shim), so the
  notice is baked into the saved HTML and is offline-tested in
  `tests/test_viz.py`. No new dependency, no behaviour change beyond the added
  credit line.

### Security
- **Defused XML parsing of the S3 bucket listing + a scheduled `pip-audit`
  dependency audit (`CODEBASE_ANALYSIS.md` §6 P2 #13 / P2 #14, §5.2.5).** The
  catalog's core discovery path parses S3 `ListObjectsV2` responses — remote,
  untrusted XML (the listing base is configurable) — and did so with the stdlib
  `xml.etree`, which is exposed to the entity-expansion ("billion laughs") and
  external-entity (XXE) attack classes. `UmbraCatalog._parse_listing` now routes
  both listing parse sites through **`defusedxml`** (`forbid_dtd=True`), so a DTD,
  internal entity expansion, or external reference is rejected outright and turned
  into a clean `CatalogError` instead of memory exhaustion or a filesystem read.
  `defusedxml` (pure-Python, zero transitive deps) is added to the core
  dependencies; offline-tested with billion-laughs / XXE / malformed payloads and
  an end-to-end hostile listing response. Separately, a new
  `.github/workflows/security-audit.yml` runs `pip-audit --strict` against the
  full resolved dependency tree weekly (and on demand), opening a tracking issue
  on a finding — the same non-blocking canary pattern as the live-catalog run,
  chosen over a hard PR gate because advisories land continuously on transitive
  deps the project doesn't control. Closes the two remaining security-hygiene
  items the codebase analysis named as open.
- **Subresource Integrity on the browser-side `geotiff.js` loader
  (`CODEBASE_ANALYSIS.md` §3.4 / P2 #12).** The lazy-imagery driver
  (`umbra map --lazy-imagery`, `umbra demo`) fetches `geotiff.js` from a pinned
  CDN URL on first click; it now injects that `<script>` with a pinned SHA-384
  `integrity` digest (`_lazy_imagery.GEOTIFF_SRI`) and `crossorigin="anonymous"`,
  so the browser verifies the fetched bytes before executing them. A compromised
  CDN or hijacked package release can no longer run arbitrary script in every map
  a user has generated — a digest mismatch falls through the existing `onerror`
  path to a clean "Fetch failed" instead of running unverified code. The digest
  is reproducible from the npm registry tarball (unpkg serves it verbatim), and
  the recompute recipe is documented inline so it survives version bumps without
  reaching the egress-restricted CDN host. Offline-tested in
  `tests/test_lazy_imagery.py` (digest shape; the injected `<script>` carries the
  digest and a CORS fetch). No new dependency, no behavior change on the happy
  path. This closes the last open security-review item for code the project
  controls; Folium's own vendored CDN assets remain out of scope.

### Changed
- **Planning docs consolidated: `TODO.md` moved under `docs/`, the three
  analysis snapshots removed.** `TODO.md` sat at the repo root while every other
  planning document lived in `docs/`; it is now `docs/TODO.md`, and it carries
  only *open* work — the `## Done` log and every struck-through entry it had
  accumulated are gone, because `CHANGELOG.md` is the record of what shipped and
  keeping a second one meant maintaining the same history twice (the file went
  from 1 735 lines to about a third of that, with every genuinely open follow-on
  preserved, including the ones that were nested inside completed entries).
  `docs/AI_INTEGRATION_IDEAS.md`, `docs/CODEBASE_ANALYSIS.md` and
  `docs/DEMO_APP_GAPS.md` — already reduced to pointer stubs after their plans
  were executed and consolidated into `docs/STRATEGY.md` — are deleted, and the
  references to them cleaned up across `AGENTS.md`, `docs/STRATEGY.md`,
  `docs/schemas/README.md`, the workflows, the module docstrings and the test
  section comments (their historical item IDs — `C1`, `G6`, `P3 #18`, … — still
  appear in this changelog, which is where that history belongs). The one
  user-visible surface that changed is `llms.txt`: its "Optional" section now
  links `docs/TODO.md` instead of the removed AI-integration roadmap.
- **`cli.py` is now a `cli/` package — the last outlier module, split the way
  `viz.py` was (`CODEBASE_ANALYSIS.md` P3 #18/#19 / `STRATEGY.md` §8).** With
  `viz` split, `cli.py` was by a wide margin the largest module in the package
  (5 522 lines, more than twice the next one) and the only one still mixing
  several unrelated concerns in one namespace: twenty-eight commands across
  three sub-groups, from `umbra search` to the SICD conversion pipeline to the
  PMTiles showcase builder to three SQLite sidecar managers, plus the shared
  option groups they all hang off. Every CLI change edited the same file, and
  finding the command you wanted meant scrolling past six families of verbs to
  reach the seventh.

  It is now nine modules along the seams the commands already had, grouped by
  *what the verb does* rather than by which library module it calls:
  `_root.py` (the Click group, the `UMBRA_JSON_ERRORS` envelope, `main()`),
  `_shared.py` (the option groups — geography, task name, acquisition
  properties, token, manifest — and how a command obtains its items),
  `discover.py` (`search`, `watch`, `info`, `context`, `llms-txt`, `ask`),
  `scenes.py` (`describe`, `download`, `quicklook`, `view`, `load`),
  `process.py` (`stack`, `convert`, `chips`), `composites.py` (`change`,
  `timescan`, `swipe`), `atlas.py` (`map`, `gallery`), `explore.py` (`mcp`,
  `serve`, `demo`, `tiles`, `showcase`) and `indexes.py` (`index`, `semantic`,
  `embed`). Import order matters exactly once — `_root` defines the group the
  command modules decorate — and `cli/__init__.py` imports them all, which is
  what registers the commands.

  **Nothing moved as far as a user is concerned, and that is checked rather
  than claimed.** The full `--help` surface — the group, all twenty-eight
  commands, all three sub-groups' subcommands, every option and every help
  string — is byte-identical to the pre-split output, and every one of the 74
  definitions is AST-identical to its pre-split form except for three
  mechanical rewrites: relative imports one level deeper (`.foo` → `..foo`),
  the shared helpers referenced through the module that defines them
  (`_gather_items` → `_shared._gather_items`), and the thirteen copies of
  `UmbraItem.from_dict(get_json(url), href=url)` collapsed into the one new
  name in the change, `_shared._item_from_url`. `cli/__init__.py` re-exports
  every name the old module defined, so `from umbra_py.cli import cli`, the
  `umbra` / `umbra-py` console scripts, `python -m umbra_py.cli` (via a new
  `__main__.py`) and the `mkdocs-click` CLI reference are all unchanged.

  The one visible difference is the test seam, the same one the `viz` split
  had: a helper is now patched on the module that *defines* it rather than on
  the façade, because a re-exported function is looked up in its own module at
  call time. That was kept to a single target instead of nine — the command
  modules reach the shared plumbing as `_shared.<name>`, so
  `monkeypatch.setattr("umbra_py.cli._shared._gather_items", …)` still holds
  for *every* command, which is what lets the option-group parity suite keep
  patching one thing while iterating over all fourteen gather commands.
  `get_json`, `UmbraCatalog`, `geocode_place` and `UmbraItem` moved to the same
  target for the same reason; the five render entry points tests stub
  (`save_gallery`, `write_geojson`, `save_change_composite`,
  `save_timescan_composite`, `save_swipe_map`) are patched on the command
  module that calls them. Same 1 323 offline tests, same coverage, no runtime
  behaviour changed. This closes the `cli.py` follow-on in `TODO.md` and the
  structural-debt group in `STRATEGY.md` §8 bar the conditional R\*Tree upgrade.
- **`viz.py` is now a `viz/` package — the last named piece of structural debt
  on the critical path (`CODEBASE_ANALYSIS.md` P3 #19 / `STRATEGY.md` §8).** At
  2 023 lines it was the second-largest module and the one every visual surface
  imports: GeoJSON conversion, reverse geocoding, Folium maps, streaming COG
  reads, stretches, quicklooks, thumbnails, co-registration, change/timescan
  composites, animations and the HTML contact sheet all shared one namespace, so
  reading it meant scrolling past four unrelated concerns to reach the fifth and
  every change touched the same file. It is now six modules along the seams the
  code already had: `geojson.py` (items → GeoJSON, no dependencies),
  `raster.py` (range-request COG reads, stretches, quicklooks, thumbnails),
  `composites.py` (co-registration, change / timescan / animation),
  `contact_sheet.py` (the standalone HTML gallery), `maps.py` (Folium footprint
  / timeline / swipe maps, and the rate-limited Nominatim geocoder), and
  `_deps.py` (`_require`, the single `viz`-extra gate).

  **Nothing moved as far as a caller is concerned.** `viz/__init__.py`
  re-exports every name the old module had — the public functions *and* the
  private helpers that `models.py`, `index.py`, `demo.py`, `narrate.py`,
  `describe.py` and `viewer.py` import from `umbra_py.viz` — so
  `from umbra_py.viz import quicklook`, `viz.change_composite(...)` and
  `monkeypatch.setattr("umbra_py.viz.save_change_composite", …)` all behave
  exactly as before. The move is verifiable rather than asserted: every
  definition is AST-identical to its pre-split form except for six relative
  imports whose level went from `.` to `..`. The one deliberate rename is the
  gallery module (`contact_sheet.py`), because a submodule named `gallery`
  would be shadowed by the `gallery` function re-exported beside it.

  The one thing that *is* different is where an internal helper is patched. A
  submodule binds what it calls at import time (`from .raster import
  _stretch_to_rgba`), so stubbing a private now means naming the module that
  calls it — `umbra_py.viz.maps._stretch_to_rgba`, not the package — which is
  what the ~50 retargeted `monkeypatch` sites in `tests/test_viz.py`,
  `test_index.py`, `test_geocode.py` and `test_describe.py` now do. Patching a
  *public* function on the package still reaches every caller outside `viz`,
  because they resolve it through that namespace at call time. No behaviour
  change, no new dependency, no test removed: the same 1 214 offline tests pass.
- **Consolidated the planning docs so status lives in one place.** The four
  `docs/*.md` planning/analysis documents had become living status logs whose
  ✅-shipped narration duplicated the CHANGELOG and whose open items overlapped
  `TODO.md` — so identifying the current critical path meant reading ~260 KB of
  mostly-completed notes. `docs/STRATEGY.md` is now the single home for the
  project's enduring context (thesis, ecosystem landscape, design principles,
  guardrails) plus a concise "current status & remaining critical path" section
  (§8); the shipped history stays in `CHANGELOG.md` and the per-PR follow-ons in
  `TODO.md`. `docs/CODEBASE_ANALYSIS.md`, `docs/DEMO_APP_GAPS.md`, and
  `docs/AI_INTEGRATION_IDEAS.md` are reduced to short pointer stubs (their
  filenames and historical item IDs — `C1`, `G6`, `P2 #11`, `5.5`, … — are kept
  so the ~25 source-docstring citations and the `llms.txt` links still resolve;
  the full text remains in git history). `AGENTS.md`'s determinism-boundary
  reference now points at `STRATEGY.md` §7.
- **A `mypy` type-check gate now verifies the `py.typed` promise
  (`CODEBASE_ANALYSIS.md` P2 #11).** The package ships a `py.typed` marker, so
  downstream type checkers trust its inline annotations — but nothing in CI
  verified those annotations were actually consistent, so the library shipped an
  *unchecked* promise. A new `type-check` job in `.github/workflows/ci.yml` runs
  `mypy` on every PR, backed by a `[tool.mypy]` config (`warn_unused_ignores` +
  `warn_redundant_casts` on, so stale ignores/casts can't accumulate). It runs
  against a core `[dev]` install: the optional, un-stubbed third-party libraries
  behind the extras (`rasterio`, `fastapi`, `sarpy`, `folium`, `PIL`, `click`,
  `mcp`, …) are import-ignored, so the gate checks *umbra-py's own* types rather
  than flapping on dependencies it doesn't control. `mypy`, `types-requests` and
  `types-defusedxml` are added to the `dev` extra. Landing the gate surfaced and
  fixed **18 genuine type issues** across 7 modules, several of them latent bugs:
  a `date > None` comparison in `CatalogIndex.search_live`'s freshness-horizon
  logic (`index.py`), a `datetime.isoformat()` on a possibly-`None` value in the
  timeline map builder and a `None`-unsafe sort key in the change-frame selector
  (`viz.py`), a `.submit()` on a possibly-`None` executor in the async artifact
  path (`serve.py`), a possibly-`None` href handed to `_has` during an
  incremental index update (`index.py`), and loosely-`object`-typed search
  backends in the CLI and MCP server (now the precise `UmbraCatalog | CatalogIndex`
  union, with `close()` guarded by `isinstance` narrowing). All fixes are
  behavior-preserving; the full offline suite is unchanged and green.
- **`umbra gallery --local` renders from baked thumbnails (`DEMO_APP_GAPS.md`
  G6).** The thumbnail bake shipped the primitive (`umbra index bake-thumbnails`)
  and the `umbra serve` / `umbra demo` consumers, but the *gallery* contact sheet
  still re-streamed every tile's cloud-optimized overview from S3 at render time.
  Now a `--local` / `--index-db` gallery embeds any thumbnail already baked into
  the index straight from local bytes — instant, offline, and (when every tile is
  baked) with **no `rasterio`**, so a core install over a fetched/baked
  `catalog.db` renders the visual browse in milliseconds. Only tiles missing from
  the bake are streamed the usual way, so a partially-baked index degrades
  gracefully, and a plain live `umbra gallery` is unchanged. `viz.gallery` gained
  an optional `baked` (`{id: PNG bytes}`) argument fed by
  `CatalogIndex.get_thumbnail`; the `rasterio` requirement is now raised only when
  a stream is actually needed. Deterministic, no model call, no new dependency;
  offline-tested in `tests/test_viz.py` (baked-only needs no viz extra, baked +
  streamed mix) and `tests/test_index.py` (`umbra gallery --local` over a
  bake-thumbnailed index streams nothing).
- **Per-pixel facet-area (gamma-nought) RTC model — `umbra convert --rtc
  --rtc-model gamma` / `sicd_to_geocoded_cog(rtc_model="gamma")` (`STRATEGY.md`
  5.5).** A third radiometric-terrain-flattening model alongside the default
  `cosine` and the range-plane `area`. It scales power by
  `cos(reference) * nz / cos(local_incidence)` — normalising by the local
  illuminated *facet* area projected into the plane perpendicular to the look
  direction (the gamma-nought convention). It uses the full 3-D facet normal (like
  `cosine`, unlike the range-plane `area`) *and* adds the true tilted-facet-area
  term `nz = cos(slope)` that both other models omit: a facet whose ground-projected
  area is one pixel has true area `1/nz`, so the illuminated area per pixel scales
  as `cos(local_incidence)/nz`. On flat terrain `nz == 1` and the local incidence
  equals the scene incidence, so with the default reference flat ground is left
  unchanged and only slopes are flattened. Like the other two it is an honest
  first slice — a normalisation of *detected amplitude*, not a calibrated product,
  and *not* the full image-space illuminated-area facet integration (Small 2011,
  with layover accumulation) or MultiRTC interop, which remain deferred. New value
  in the public `RTC_MODELS` constant; `rtc_model` still defaults to `"cosine"`,
  so existing calls are unchanged. The physics is a pure-numpy core
  (`_facet_area_factor`) offline-tested against closed-form planar-slope behaviour
  (flat → unchanged, the exact `nz`-scaling relative to the cosine factor, DEM-gap
  safety, and the shadow/clamp floor), with only the DEM-on-grid resample touching
  rasterio.
- **Instant SAR thumbnail preview in `umbra demo` (`DEMO_APP_GAPS.md` G6).** The
  baked-thumbnail bake shipped the primitive (`umbra index bake-thumbnails`) and
  the server endpoint (`GET /artifacts/thumbnail/{id}.png`) but left the flagship
  self-serve explorer unwired. Now, with `umbra demo --server-url` pointing at a
  running `umbra serve`, clicking a scene *leads* its detail panel with a small
  SAR picture pulled from that endpoint — the quicklook thumbnail served straight
  from the index as an offline local-bytes read (falling back to a live quicklook
  render for a scene not yet baked), so the funnel's front door opens with a
  radar image, not metadata alone. The heavier on-click "Get SAR image" COG
  overlay stays the deeper look. A scene with no baked thumbnail 404s and the
  `<img>` is dropped via `onerror` (never a broken image); the remote item id is
  url-encoded into the path (the base is the trusted server URL); and the preview
  reuses the single `serverBase` the "Analyze this view" panel already computes.
  Without `--server-url` the detail panel is unchanged and the page stays a fully
  static single file. Offline-tested in `tests/test_demo.py` (the generator is
  stdlib-only — no `viz` extra, no network).
- **Rendered documentation site — mkdocs-material + mkdocstrings + mkdocs-click
  (`CODEBASE_ANALYSIS.md` §5.2 #6 / P3 #20).** The project graduates from a
  README doing a docs site's job to being the front door of a real one — the
  highest-leverage remaining code investment for discoverability, and the anchor
  the `llms.txt` idea pointed at. `mkdocs.yml` + `docs_src/` author the site
  (`docs_dir` is `docs_src/`, so the internal strategy/analysis Markdown under
  `docs/` stays unpublished). The **API reference** is generated by mkdocstrings
  from the docstrings the package already ships; the **CLI reference** is
  generated by `mkdocs-click` straight from the Click group, so neither can drift
  from the code or from `umbra --help`. `.github/workflows/docs.yml` builds the
  site `--strict` on every PR (a broken cross-reference fails review) and deploys
  to GitHub Pages from `main`; the deploy waits only on a maintainer enabling
  Pages. New `[docs]` extra (`mkdocs-material`, `mkdocstrings[python]`,
  `mkdocs-click`); README gains a docs badge + link.
- **Native LlamaIndex tool adapter — `umbra_py.llamaindex`
  (`AI_INTEGRATION_IDEAS.md` B1 / C1).** Completes the agent-framework reach
  trilogy — MCP → LangChain → LlamaIndex — the "same shapes, a third
  registration" step named in `TODO.md`. `umbra_tools()` returns the catalog as
  native LlamaIndex `FunctionTool`s ready for `ReActAgent.from_tools(...)` or a
  tool-calling agent. There is **no new business logic**: the nine JSON tools
  (`search_catalog`, `get_item`, `geocode_place`, `index_stats`, `download_asset`,
  `watch_site`, `find_similar` / `find_similar_text`, `describe_scene`) reuse the
  MCP server's deterministic callables verbatim, so all three front doors cannot
  drift; each tool's name/description is inferred from the function docstring and
  its argument schema from the signature. *Images are the API*: LlamaIndex has no
  `content_and_artifact` split, so the `quicklook` / `change_composite` /
  `timescan` render tools — re-implemented natively so the surface never pulls in
  the MCP SDK — return a `RenderResult` whose string form is the caption and whose
  `.png` (the `ToolOutput.raw_output`) carries the raw PNG for a downstream
  multimodal model to *see* the radar scene; pass `include_render=False` for a
  JSON-only surface. The determinism boundary is preserved — `describe_scene`
  stays the one opt-in model call. New `[llamaindex]` extra (`llama-index-core` —
  the lightweight tool package, not the full framework — plus `viz`), wired into
  the all-extras CI job, and fully offline-tested in `tests/test_llamaindex.py`
  (surface, schema inference, invocation, PNG `RenderResult`, guards) with no key
  and no network.
- **Native LangChain / LangGraph tool adapter — `umbra_py.langchain`
  (`AI_INTEGRATION_IDEAS.md` B1 / C1).** *Agents are the new first-time users*:
  the MCP server puts the 17+ TB SAR archive in front of MCP-native clients, and
  this adds the **same** tool surface to the other large population of agent
  builders — anyone assembling an agent with LangChain / LangGraph.
  `umbra_tools()` returns the catalog as native LangChain `StructuredTool`s ready
  for `model.bind_tools(...)` or LangGraph's `create_react_agent`. There is **no
  new business logic**: the nine JSON tools (`search_catalog`, `get_item`,
  `geocode_place`, `index_stats`, `download_asset`, `watch_site`, `find_similar` /
  `find_similar_text`, `describe_scene`) reuse the MCP server's deterministic
  callables verbatim, so the two front doors cannot drift; each tool's schema is
  inferred from the function signature and its description from the docstring.
  *Images are the API*: the `quicklook` / `change_composite` / `timescan` render
  tools are re-implemented natively — so the LangChain surface never pulls in the
  MCP SDK — and return the PNG via LangChain's `content_and_artifact` response
  format (a text caption on the `ToolMessage` content, the raw PNG on
  `.artifact`), so a downstream multimodal model still *sees* the radar scene;
  pass `include_render=False` for a JSON-only surface. The determinism boundary is
  preserved — `describe_scene` stays the one opt-in model call. New `[langchain]`
  extra (`langchain-core` — the lightweight tool package, not the full framework —
  plus `viz`), wired into the all-extras CI job, and fully offline-tested in
  `tests/test_langchain.py` (surface, schema inference, invocation, PNG artifact,
  guards) with no key and no network. (The parallel LlamaIndex `FunctionTool`
  wrapper has since shipped too — see the `umbra_py.llamaindex` entry above.)
- **Fetchable prebuilt scene-embedding table — `umbra embed fetch`
  (`STRATEGY.md` 5.2 / `AI_INTEGRATION_IDEAS.md` C5).** Building the visual
  similarity index (`umbra embed`, C5) embeds every quicklook in the archive — the
  one expensive, model-backed step — so a fresh install got the searchable index
  (`umbra index fetch`) and the whole-catalog basemap (`umbra tiles --fetch`) for
  free but had to render and embed thousands of scenes itself before `umbra embed
  similar` returned anything. This closes that: `umbra embed fetch` /
  `fetch_prebuilt_embeddings()` / `SceneEmbeddingIndex.from_release()` pull a
  published `catalog.embed.db` from the rolling `catalog-index` GitHub release
  straight to the sibling of the catalog index, so visual similarity search works
  with **no rebuild** — only the *query* still needs an embedding key (the archive
  vectors arrive pre-built) — the embedding sibling of `umbra index fetch` /
  `umbra tiles --fetch`. New constants `CATALOG_EMBED_ASSET` /
  `CATALOG_INDEX_EMBED_URL`; the fetch path calls **no model** and adds **no
  dependency** (it reuses the resume-safe `download_url`), and is fully
  offline-tested in `tests/test_embed.py` (mocked release download + round-tripped
  DB, model label preserved, overwrite, and the CLI). Because the vectors are
  model-derived and model-*specific* — unlike the deterministic `catalog.db` /
  `catalog.pmtiles` — the *publish* is opt-in: `.github/workflows/publish-index.yml`
  gained a gated, `continue-on-error` step that builds and uploads
  `catalog.embed.db` (recording the embedding model prominently in the release
  notes) only when a maintainer has set an `OPENAI_API_KEY` secret, so it never
  affects the deterministic index publish and costs nothing until configured. This
  is exactly the static, host-anywhere artifact `STRATEGY.md` 5.2 wants to offer
  upstream — publish it beside `catalog.json` and the ecosystem gets scene
  similarity search over Umbra data for free.
- **Download content-integrity verification against the S3 ETag MD5
  (`docs/CODEBASE_ANALYSIS.md` §3.2 / P1 #5).** `download_url` already verified
  the received byte count against `Content-Length` and used `If-Range` + a stored
  ETag so a resume can't splice two different objects; this closes the remaining
  §3.2 item — *content* verification. When the server exposes a single-part S3
  `ETag` (the object's hex MD5) and `verify=True` (the new default), the finished
  file is streamed through MD5 and compared, so on-the-wire corruption a correct
  byte count can't catch fails loudly with a `Checksum mismatch` `DownloadError`.
  A mismatch means the complete-length bytes are wrong — a resume can't repair
  them — so the `.part` and its `.etag` validator are discarded and a retry
  re-downloads cleanly rather than "resuming" a full-but-corrupt file. Multipart
  ETags (`"<hash>-<n>"`) are not a plain MD5 of the bytes and are skipped rather
  than raising a spurious mismatch; `verify=False` opts out for callers that don't
  want the extra read of a multi-GB file (it threads through `download_asset` /
  `download_item`). New `verify` keyword on `download_url`; stdlib `hashlib` only,
  **no new dependency and no model call**, fully offline-tested in
  `tests/test_download.py` (matching MD5 passes, corrupt-body mismatch discards
  the `.part`, multipart-ETag skip, `verify=False` opt-out, and a resumed append
  verifying the *whole* object's MD5). This is the reliability floor under the
  library's core job — fetching multi-GB SAR products — and closes the last
  open item under `TODO.md`'s download-hardening ledger.
- **Projected-area (foreshortening) RTC model — `umbra convert --rtc
  --rtc-model area` (`STRATEGY.md` 5.5).** Radiometric terrain flattening (`--rtc`)
  shipped as the geometric cosine correction `cos(reference)/cos(local_incidence)`,
  which uses the full 3-D local incidence angle and so folds azimuth-direction tilt
  into the correction. This adds a second, selectable model,
  `sicd_to_geocoded_cog(rtc_model="area")` / `--rtc-model area`, that scales power
  by `sin(local_range_incidence)/sin(reference)`: it measures incidence in the
  *range–vertical* plane, so it targets the range-direction foreshortening and
  layover that dominate radiometric terrain distortion — separating them from the
  azimuth tilt that does not foreshorten. On flat terrain both reduce to the scene
  incidence angle (default reference), so flat ground is left unchanged and only
  slopes are corrected; DEM gaps and layover degrade gracefully (factor forced to
  one over gaps, floored/clamped in layover). It is an honest first-order step
  toward area-based gamma-nought normalisation, **not** the full illuminated-area
  facet integration (Small 2011) or MultiRTC interop, which remain deferred. New
  public constant `RTC_MODELS` and the `rtc_model=` keyword (default `"cosine"`,
  so existing calls are unchanged); the physics is a pure-numpy core
  (`_range_local_incidence`, `_foreshortening_factor`) with closed-form
  planar-slope behaviour, **no model call and no new dependency**, offline-tested
  in `tests/test_convert.py` (flat/range-ramp/azimuth-slope geometry, the
  cosine-vs-area distinction, layover/gap handling, the end-to-end and CLI paths).
  This advances the last remaining code item on `STRATEGY.md` 5.5's radiometric-RTC
  line.
- **stac-geoparquet chip manifest — `umbra chips --manifest chips.parquet`
  (`AI_INTEGRATION_IDEAS.md` C4 / `STRATEGY.md` 5.5).** `umbra chips` wrote its
  training-tile manifest as `.jsonl` (one record per line) or `.geojson` (a
  `FeatureCollection`), both stdlib-only — fine for a small run, but a large chip
  set forces a consumer to read every line. This adds a third format: a `.parquet`
  manifest written as [stac-geoparquet](https://stac-geoparquet.org/), so a chip
  dataset is one column-oriented file DuckDB, geopandas or pyarrow can query
  without loading it whole — exactly what the SAR foundation-model / change-detection
  audience (`STRATEGY.md` 5.5's "audience most likely to contribute back") reaches
  for at scale. Each chip becomes one STAC Item row (its footprint geometry, the
  acquisition datetime, and the same fields as the `.jsonl` record as properties,
  with the chip file as the item's `data` asset), reusing the same
  `stac_geoparquet.arrow` writer as `umbra_py.export`. Format is still chosen by
  the manifest filename's extension, so the CLI is unchanged beyond accepting
  `.parquet`. It stays in the project's determinism boundary (**no model call** —
  pure manifest logic) and needs the `[export]` extra alongside `[load]`; new
  public API `write_manifest_parquet`, fully offline-tested in
  `tests/test_chips.py` (round-tripped through pyarrow, including the null-datetime
  case). This closes the "publish the chip manifest as stac-geoparquet" follow-on
  in `TODO.md`.
- **SICD-convert showcase notebook — `examples/07_sicd_amplitude.ipynb`
  (`STRATEGY.md` 5.4 / 5.5).** Completes the example gallery with a runnable front
  door for the flagship SICD → geocoded COG capability. Every other notebook uses
  the already-geocoded `GEC` asset; the complex `SICD` lives in the radar slant
  plane and won't open on a map without the sensor-model geocoding `umbra convert`
  provides — extensive code that had no tutorial. The notebook takes one open-data
  SICD, detects its amplitude in the slant plane (asserting the CRS is `None`),
  geocodes it onto a north-up EPSG:4326 COG with `sicd_to_geocoded_cog`, and
  asserts the result is EPSG:4326, carries COG overviews, and lands on the
  acquisition's catalog footprint. Like the rest of the gallery it is
  self-checking (a small deterministic search with `assert`s in every code cell,
  **no model call**) and guarded offline by `tests/test_examples.py`; it executes
  end-to-end under `pytest -m network` using a curated small scene (`Centerfield,
  Utah`, ~370 MB, converts in under a minute), and the live-execution guard now
  also `importorskip`s `sarpy` (the `convert` extra). Terrain orthorectification
  (`--dem auto`), the geoid correction, and `--rtc` are named in prose as the next
  step. This finishes workstream 5.4.
- **Standing-analyst monitoring notebook — `examples/06_site_monitoring.ipynb`
  (`AI_INTEGRATION_IDEAS.md` C3 / `STRATEGY.md` 5.4).** SAR's killer application is
  monitoring — the same site re-imaged pass after pass — and the primitives for it
  (`umbra watch`, `umbra change`, the `watch_site` MCP tool) had all shipped
  without one runnable example wiring them into the standing-analyst loop. This
  adds it: the notebook stands up a `watch()` over a repeat-imaged site, asserts
  the first run reports every pass as new *and* an immediate re-run reports **zero**
  (the idempotency a scheduler depends on), then hands the new passes to
  `select_change_frames` → `save_change_composite` for the "new pass lands →
  composite → notify" action, naming `umbra change --narrate`, `MetaWatchStore`
  persistence and the `watch_site` MCP tool as the next steps. Like the rest of the
  gallery it is self-checking (a small deterministic search with `assert`s in every
  code cell, **no model call**) and guarded offline by `tests/test_examples.py`,
  and it executes end-to-end under `pytest -m network` (`viz` extra for the
  composite). The still-planned SICD-convert showcase notebook is renumbered `06`
  → `07`.
- **Baked SAR quicklook thumbnails in the catalog index — `umbra index
  bake-thumbnails` / `CatalogIndex.bake_thumbnails()` /
  `CatalogIndex.get_thumbnail()` (`docs/DEMO_APP_GAPS.md` G6).** Closes the last
  open piece of the "No thumbnail/artifact caching layer" gap. Every gallery,
  `umbra demo` preview and `umbra serve` quicklook otherwise re-streams a scene's
  cloud-optimized GeoTIFF overview from S3 at *render* time, so the first view of
  a whole catalog is network-bound and slow. `umbra index bake-thumbnails` renders
  a small (`--size`, default 256 px) PNG preview per acquisition once at build
  time and caches the bytes in a new additive `thumbnail` column, so a later
  `GET /artifacts/thumbnail/{item_id}.png` on `umbra serve` — a new endpoint that
  wraps `get_thumbnail()` — is an instant, offline file read instead of an S3 COG
  stream (a `404` falls back to `/artifacts/quicklook`). The render-side sibling
  of `umbra index bake`, it shares that command's discipline: **idempotent** (only
  acquisitions without a baked thumbnail are rendered, so a re-run bakes just what
  was added since), `--limit` for bounded batches, and a scene that can't be
  rendered is skipped and retried next run rather than aborting the batch. The
  schema migrates additively in place (`user_version` 2 → 3 — the second exercise
  of the migration path versioning was landed to enable), so an existing or fetched
  `catalog.db` gains the column on the next open. The renderer is **injectable**
  (default `viz._thumbnail_png`, needing the `viz` extra), so the whole path — bake,
  point-lookup, the server endpoint, coverage in `umbra index info` /
  `docs/schemas/index-info.schema.json` — is offline-tested with a stand-in
  renderer, no network and no `viz` extra. No model is called; the baked bytes never
  ride on `search`/`get` (which would bloat every `UmbraItem` with a PNG).
- **Baked place labels now flow through every read surface (`docs/DEMO_APP_GAPS.md`
  G2 follow-on).** `umbra index bake` writes a reverse-geocoded label onto
  `UmbraItem.place`, but until now only `umbra demo` consumed it — every other
  surface still fell back to the task codename or re-geocoded at render time
  (behind Nominatim's 1 req/s cap). This wires the baked label through the rest:
  `UmbraItem.to_llm_context()` (the A3 agent context card) prefers `.place` over
  the task codename; `footprint_map` / `timeline_map` (`umbra map`, `--timeline`)
  use `.place` directly and skip the live geocode entirely — so a fully-baked
  `--local` render with `--geocode` never touches the network, building the
  Nominatim session lazily only for items still lacking a label; `umbra serve`
  surfaces the label as a namespaced `umbra:place` STAC property so STAC clients
  show a real place name; and the stac-geoparquet export (`umbra index export`)
  carries `umbra:place` into the published snapshot, so a DuckDB / geopandas
  consumer reads the label without re-geocoding every row. In each case the
  baked label is preferred only when present and never overrides a value the
  source document already carries. Deterministic, no new dependency, no model
  call; offline-tested across models, viz, serve, and export.
- **Baked place labels in the catalog index — `umbra index bake` /
  `CatalogIndex.bake_places()` / `UmbraItem.place` (`docs/DEMO_APP_GAPS.md`
  G2).** Turning the shared index into a *labelled* demo backend, the
  denormalization G2 named as the change that does it. Reverse geocoding
  (coordinates → a human place name) used to run only at *render* time, where
  OpenStreetMap Nominatim's 1 req/s cap makes labelling thousands of
  acquisitions impractical — so `umbra demo` and the maps fell back to the Umbra
  task *codename*, not a geographic name. `umbra index bake` resolves each
  acquisition's footprint centroid to a place label ("Reykjavík, Iceland") once
  at build time and caches it in the index, so every `--local` `search`/`get`
  yields it on the new `UmbraItem.place` attribute for free and `umbra demo
  --local` shows real place names instantly, with zero per-render geocoding (the
  free-text site search matches on them too). The bake is **idempotent** (only
  unlabelled items are geocoded, so a re-run labels just what was added since,
  and `--limit` bakes a large catalog in bounded batches) and the geocoder is
  injectable, so the whole path is offline-tested with a stand-in — no network.
  This ships as the **first real schema migration** the index versioning was
  landed to enable: `place` is an additive nullable column, so a version-1 (or
  legacy version-0) `catalog.db` — including a fetched snapshot — is migrated in
  place on open (`user_version` 1 → 2, the column added, every row preserved)
  rather than rebuilt or rejected. Re-indexing an acquisition (`umbra index
  update`) now upserts via `ON CONFLICT` so it refreshes the STAC columns but
  **preserves** a baked label (the label is keyed on the footprint, not the
  document). `umbra index info` reports label coverage (`labeled` in the `--json`
  object, `docs/schemas/index-info.schema.json`; a "places: N of M labelled" line
  in the human summary). No new dependency and no model call.
- **Semantic "describe the site" search on the `umbra-mcp` MCP server —
  `search_catalog(area=…, semantic=True)` (`docs/AI_INTEGRATION_IDEAS.md` §C1
  follow-on).** The embedding-backed task-name aliasing shipped complete on the
  CLI (`umbra semantic search`), but the agent surface — the project's
  highest-leverage front door — only reached the deterministic `fuzzy=` token
  match, so a plain-language *site description* couldn't be aliased to a task
  name. The new `semantic=True` flag resolves `area` to the closest task names
  by meaning through the shipped `SemanticTaskIndex` (so `"grain storage north
  dakota"` reaches `"Beet Piler - ND"`, an alias sharing no word with the label
  that `fuzzy` cannot and should not fake), searches the best match over the
  chosen backend, and returns `resolved_area` plus the ranked `semantic_matches`
  so the resolution is auditable and retryable. A `min_score` cosine threshold
  drops weak aliases (a low-confidence description returns an empty audit trail
  rather than an arbitrary top pick), and a `search-by-description` prompt
  packages the workflow. `semantic` and `fuzzy` are mutually exclusive; the mode
  is gated (like the CLI) on a prebuilt semantic index and the `[ai]` embedding
  key, so it never runs implicitly. The only model call is turning the query
  into a vector (an injectable embedder); the whole path is offline-tested in
  `tests/test_mcp_server.py` with a deterministic concept embedder — no key, no
  network, no new dependency.
- **Polygon `intersects` spatial search — a true footprint filter, not just a
  bounding box (`docs/AI_INTEGRATION_IDEAS.md` §B2 STAC follow-on).** Discovery
  is the project's moat (`docs/STRATEGY.md` §3), and its only spatial filter was
  a rectangle: a coast, a border, or any drawn area of interest dragged in a lot
  of empty ocean and neighbouring land. `search(intersects=…)` now keeps only
  acquisitions whose footprint intersects a caller-supplied GeoJSON polygon —
  the standard STAC `intersects` every geo tool already speaks — threaded
  through every search surface so the two backends agree: the live
  `UmbraCatalog` walk, the SQLite `CatalogIndex` (its bounding box pushed into
  SQL as a cheap prefilter, the exact polygon test then run in Python),
  `CatalogIndex.search_live`, the Canopy commercial archive (the polygon POSTed
  as the STAC `intersects` and re-checked client-side), `umbra search
  --intersects <file.geojson | inline JSON>`, the `umbra serve` STAC API
  (`GET`/`POST /search`, mutually exclusive with `bbox` per the spec), and the
  `search_catalog` MCP tool. The geometry itself is a new dependency-free core
  (`umbra_py._geometry`): a stdlib GeoJSON polygon parser (`Polygon` /
  `MultiPolygon`, or a `Feature` / `FeatureCollection` wrapping one) and
  closed-form intersection primitives (bbox reject, segment-crossing, ray-cast
  point-in-polygon) over plain `(lon, lat)` tuples — no shapely, no compiled
  geometry stack in the base install. `UmbraItem.intersects_polygon` tests the
  item's *actual* footprint (a tighter filter than the bbox
  `intersects_bbox`), falling back to the bbox when a footprint is absent. Holes
  and antimeridian-spanning polygons are handled over-inclusively (they can only
  keep an item, never wrongly drop one — the safe direction for a discovery
  filter) and documented as such. No model is called; the whole path is
  offline-tested (`tests/test_geometry.py`) across the core, item, catalog,
  index, CLI, STAC API and MCP surfaces.
- **Canopy commercial-archive backend on the `umbra-mcp` MCP server — a token
  concept for the flagship AI surface (`docs/STRATEGY.md` 5.1 follow-on /
  `docs/AI_INTEGRATION_IDEAS.md` §B1).** The paid-archive funnel already ran end
  to end on the CLI, but the MCP server — the project's highest-leverage surface —
  only reached the free open bucket. `umbra_py.mcp_server` now reads
  `$UMBRA_CANOPY_TOKEN` once from the server's environment (`_canopy_token()` — a
  secret the operator configures in the MCP client's `env` block, never a tool
  argument the client's model handles or can leak): when set, `search_catalog` and
  `watch_site` query Umbra's authenticated commercial archive (`source:
  "canopy-archive"`) and `get_item` resolves a bare acquisition id through the
  shipped `UmbraCatalog.get_item` STAC `ids` lookup (a full `://` URL is still read
  directly as an open-data sidecar). So a paying Canopy customer discovers,
  monitors and retrieves the archive they pay for through the same conversation a
  newcomer learned on the free data — the funnel made literal on the surface that
  matters most. `_search_source(local, token)` rejects `local=True` with a token
  (the live archive has no local index), and the server's `instructions` announce
  archive mode when a token is configured. No model is called and no new dependency
  is added — pure backend-selection wiring; the token is only ever handed to the
  Canopy catalog (never surfaced in a result), and the whole path is offline-tested
  (`tests/test_mcp_server.py`) against a fake archive catalog with no credentials
  and no network.
- **`describe_scene` MCP tool + `describe-scene` prompt — a SAR-literate VLM
  reading of one scene over MCP (`docs/AI_INTEGRATION_IDEAS.md` §C2 follow-on).**
  The `umbra-mcp` server surfaces the shipped `umbra describe` C2 capability:
  `describe_scene(url, asset, db, max_size, model)` renders the acquisition's
  quicklook, sends it with the item's context card behind the packaged
  SAR-literacy prompt, and returns a validated `{summary, observed_features,
  confidence, caveats}` reading — so an MCP client can get "what am I looking at?"
  answered inside the same conversation that searched and viewed the scene. It is
  the **one tool on the server that consults a model**, a deliberate, opt-in
  exception to the otherwise-deterministic tool surface: gated (like the CLI) on
  the `[ai]` key so it never runs implicitly, with the boundary intact — the
  picture and metadata are produced deterministically, the model only interprets
  (its reply passes the `parse_description` boundary and never becomes a
  coordinate, URL, or filter), and every reading carries the CC-BY attribution
  plus the `AI_PROVENANCE` note. The describer and render are injectable, so the
  whole tool is offline-tested (`tests/test_mcp_server.py`) with no `[ai]`/`[viz]`
  extra, no key, and no network — including the missing-key setup error. The
  server module's "nothing here calls a model" invariant was revised to name this
  single, honest exception. No new dependency.
- **Adoption / community scaffolding — `CITATION.cff`, `SECURITY.md`,
  `CODE_OF_CONDUCT.md` (`docs/STRATEGY.md` 5.3 / `docs/CODEBASE_ANALYSIS.md` P2
  #14, P3 #22).** The library is feature-complete; the binding constraint on the
  strategy's "widen the funnel" thesis is now discoverability and citability, not
  capability. This lands the code-side pieces of workstream 5.3 ("make adoption
  visible where Umbra looks"): a machine-readable `CITATION.cff` (Citation File
  Format 1.2.0) so GitHub renders a "Cite this repository" button and Zenodo /
  citation managers can read the metadata — academic citations are the currency
  an open-data program exists to generate; a `SECURITY.md` disclosure policy
  (private GitHub advisory reporting, plus the honest security posture: anonymous
  HTTPS, no auth surface, remote-content/generated-HTML as the trust boundary);
  and a Contributor Covenant 2.1 `CODE_OF_CONDUCT.md`. Together they complete
  GitHub's Community Standards profile. `CITATION.cff`'s `version` is kept in sync
  with `umbra_py.__version__` by an offline, stdlib-only guard
  (`tests/test_citation.py`), mirroring the golden-file discipline the
  `llms.txt` bundle already uses. README gains "Citing umbra-py" and "Community"
  sections linking all three. No code surface changes and no new dependency.
- **Keyed single-item lookup against the Canopy commercial archive —
  `UmbraCatalog.get_item(item_id)` / `umbra info <id> --token` (`docs/STRATEGY.md`
  5.1 follow-on).** `search` covers *listing* the paid archive; this adds the
  *retrieval* complement — a keyed fetch of one acquisition by STAC id. It is
  implemented with the STAC API `ids` search extension over the same
  `/archive/search` endpoint the search path already POSTs to (`POST {"ids":
  [item_id], "limit": 1}`), so it introduces no new endpoint and stays
  offline-testable against a mocked API, and it inherits `_archive_page`'s bearer
  auth plus the helpful 401/403 "token rejected" and 500 wrapping. It requires a
  Canopy token (the open bucket is a static catalog with no id→item index — resolve
  an open-data item from its sidecar URL or from a built index with
  `CatalogIndex.get`), and guards against a server that ignores the `ids` filter by
  accepting only the exact id requested. On the CLI, `umbra info` gains `--token`
  (with the `$UMBRA_CANOPY_TOKEN` fallback): with a token the argument is an
  archive item id resolved via the keyed lookup, without one it stays the
  open-data sidecar-URL read it has always been — the retrieval sibling of `umbra
  search --token`, so the commercial archive now has a keyed lookup matching the
  local index's `CatalogIndex.get`. No model call and no new dependency; the whole
  path is offline-tested (`tests/test_canopy.py`, `tests/test_cli_token.py`).
- **Detection-chips example notebook — `examples/05_detection_chips.ipynb`
  (`docs/STRATEGY.md` 5.4 / `docs/AI_INTEGRATION_IDEAS.md` B3).** The ML-dataset
  half of the notebook gallery, and the workflow the model-training audience (SAR
  foundation models, change detection — the audience most likely to contribute
  back) reaches for first. It cuts one scene into fixed-size, georeferenced
  training chips with `umbra chips` — walked a window at a time straight out of
  the geocoded COG over `/vsicurl` range reads, so there is no full download and
  memory stays bounded to one tile — and reads back the manifest that makes each
  chip trainable: its geographic bbox, CRS and affine transform, and the
  acquisition's look-angle, resolution, polarization and CC-BY license. Like the
  other notebooks it is self-checking (a deterministic one-day search with
  `assert`s in every code cell) and guarded offline by `tests/test_examples.py`
  (well-formed, cells parse, only public `umbra_py` symbols, CC-BY present),
  executable end-to-end under `pytest -m network`. No new code surface and no
  model call.
- **Amplitude time-series example notebook —
  `examples/04_amplitude_time_series.ipynb` (`docs/STRATEGY.md` 5.4 /
  `docs/AI_INTEGRATION_IDEAS.md` B3).** With every capability built, the binding
  constraint on adoption is the notebook gallery — the greatest-hits SAR
  workflows, runnable, that double as live evals. The three shipped notebooks
  cover search→quicklook, streaming a GEC into `xarray`, and a two-pass change
  composite; this adds the *monitoring* greatest-hit. It reduces a site's repeat
  passes to one scalar each (mean backscatter in dB, from `to_xarray(..., db=True)`
  over streamed decimated overviews — no full download) and plots the trend — the
  whole-scene scalar complement to `umbra timescan` (which keeps the map) and
  `umbra change` (which compares two passes in color). Like the others it is
  self-checking (a small deterministic search with `assert`s in every code cell)
  and guarded offline by `tests/test_examples.py`, executable end-to-end under
  `pytest -m network`. No new code surface and no model call.
- **Radiometric terrain flattening — `umbra convert --rtc` /
  `sicd_to_geocoded_cog(rtc=True)` (`docs/STRATEGY.md` 5.5).** Terrain
  orthorectification (`--dem`) fixes *where* each pixel lands but not *how bright*
  it is, and radar backscatter is strongly modulated by the local incidence angle
  — so on relief a slope tilted toward the radar looks bright and one tilted away
  looks dark from geometry alone. `--rtc` (which requires `--dem`) removes that
  geometric modulation: after geocoding, each pixel is scaled in the power domain
  by the cosine correction `cos(reference) / cos(local_incidence)`, where the
  local incidence angle comes from the DEM's local slope (its surface normal) and
  the scene look geometry (`SCPCOA.IncidenceAng` / `AzimAng`). The reference
  defaults to the scene incidence angle, so flat terrain is left unchanged and
  only slopes are flattened (`--rtc-ref-angle` overrides it). This is an honest
  first slice: a geometric normalisation of *detected amplitude*, not a calibrated
  gamma-nought RTC product (Umbra's open products are not radiometrically
  calibrated), documented as exactly that. It holds the module's grain — the
  physics is a pure-numpy core (terrain normals, look vector, correction factor)
  with closed-form behaviour over a planar slope, so it is fully offline-tested
  with hand-built arrays; only resampling the DEM onto the output grid touches
  rasterio, and DEM gaps / radar-shadow slopes degrade gracefully (factor clamped,
  gaps pass through unchanged). No new dependency and no model call. This closes
  the geometric half of 5.5's remaining `MultiRTC`/RTC gap; full gamma-nought area
  normalisation and MultiRTC interop remain open follow-ons.
- **The Canopy commercial-archive `--token` now works on the render/analysis
  verbs, completing the funnel to full parity (`docs/STRATEGY.md` 5.1).**
  `umbra search --token …` (or `$UMBRA_CANOPY_TOKEN`) has long pointed the same
  `search()` interface at Umbra's authenticated Canopy archive instead of the
  open bucket, but every other verb routed through `_gather_items`, which dropped
  the token — so a paying customer could *search* the paid archive on the CLI but
  not *render or analyse* it. `map`, `gallery`, `change`, `timescan`, `swipe` and
  `chips` now take the same `--token` (with the `$UMBRA_CANOPY_TOKEN` fallback and
  a guard against combining it with a local index), threaded through
  `_gather_items` → `_search_source(local, db_path, token)` to the commercial
  backend. This is the funnel made literal: the tool learned on the free data
  *is* the tool used on the paid archive, with the identical flags. No new
  dependency and no model call — the token is only ever sent to the Canopy
  endpoint, and the whole path is offline-tested against a `responses`-mocked STAC
  API (no credentials, no network), covering the dispatch, the token→archive flow,
  the per-command wiring, the `$UMBRA_CANOPY_TOKEN` fallback and the
  mutual-exclusion guard.
- **Auto-fetch a global geoid grid for vertical-datum correction —
  `umbra convert --geoid auto` / `umbra_py.geoid` (`docs/STRATEGY.md` 5.5).**
  Vertical-datum correction shipped as `--geoid PATH`, but that still made the
  user find, download, and point at the right EGM undulation grid — the same
  "same 500 lines of glue" `--dem auto` removed for DEMs, still present for the
  geoid. `--geoid auto` / `sicd_to_geocoded_cog(geoid="auto")` closes it, the
  vertical sibling of `--dem auto`: the new `umbra_py.geoid` module fetches a
  global geoid-undulation grid (the compact ~4 MB EGM96 15′ model PROJ
  distributes on [`cdn.proj.org`](https://cdn.proj.org/), `us_nga_egm96_15.tif`)
  once, caches it under the same XDG cache dir the index and DEM tiles use, and
  hands it into the shipped `--geoid PATH` correction unchanged — so
  `--dem auto --geoid auto` gives a terrain-corrected *and* vertically-referenced
  scene over relief with zero data hunting. Unlike a DEM the EGM grid is a single
  global file (nothing to tile — one file covers every scene); the fetch reuses
  the resume-safe `download_url` and is injectable (`fetch_geoid_grid`,
  `geoid_grid_url`, `default_geoid_cache_dir`), so the whole download-and-cache
  path is offline-tested with a stub downloader, with no new dependency and no
  packaged EGM data. `us_nga_egm08_25.tif` (EGM2008 2.5′) is a higher-resolution
  alternative on the same CDN, selectable via `fetch_geoid_grid(name=…)`.
- **Vertical-datum / geoid correction for terrain orthorectification —
  `umbra convert --geoid PATH` / `sicd_to_geocoded_cog(geoid=…)`
  (`docs/STRATEGY.md` 5.5).** Terrain orthorectification walks each control point
  onto the DEM surface, but global DEMs (Copernicus GLO-30, SRTM) quote height
  above the **EGM geoid** while SICD projects against the **ellipsoid**; feeding
  the orthometric height in as-is mislocated relief by roughly `N·tan(look_angle)`
  (the geoid undulation `N` reaches ~±100 m worldwide). `--geoid` takes any
  rasterio-readable undulation grid (e.g. an EGM96/EGM2008 GeoTIFF) and adds `N`
  to each sampled DEM height (`hae = orthometric + N`) before projecting, for
  survey-grade geolocation over relief. The correction is a pure composition of
  two injectable `(lons, lats) -> heights` samplers (`_geoid_corrected_sampler`) —
  the geoid grid is read with the same `_dem_height_sampler` the DEM uses — so the
  whole path is offline-tested with a hand-written grid, with no new dependency
  and no packaged EGM data. It requires `--dem` (it corrects DEM heights, a hard
  error without one), degrades gracefully to the uncorrected height off the grid,
  and without it the output is unchanged (correct to the local geoid–ellipsoid
  separation, ample for map placement).
- **Auto-fetch the covering Copernicus DEM for terrain orthorectification —
  `umbra convert --dem auto` / `umbra_py.dem` (`docs/STRATEGY.md` 5.5).** DEM
  terrain orthorectification shipped as `--dem PATH`, but that still made the
  user find, download, and mosaic the right elevation tiles for the scene — the
  last convert-side "same 500 lines of glue" named in `TODO.md`. `--dem auto` /
  `sicd_to_geocoded_cog(dem="auto")` closes it: it projects the scene's image
  corners to a geographic bbox, resolves the 1°×1°
  [Copernicus GLO-30](https://registry.opendata.aws/copernicus-dem/) tiles
  covering it, pulls them from the public AWS Open Data bucket (skipping the
  all-ocean gaps Copernicus
  omits with a 404, merging several into a mosaic), and terrain-orthorectifies
  against the result — one flag, correctly geolocated over relief. The new
  `umbra_py.dem` module keeps the tile math (`copernicus_tile_id`,
  `tiles_covering_bbox`, `tile_url`, `tile_ids_for_bbox`) pure standard library
  and offline-tested, and the fetch (`fetch_dem_for_bbox`) reuses the resume-safe
  `download_url` behind an injectable `download` callable, so the skip/merge/raise
  behaviour is covered with a stub downloader — only the multi-tile
  `rasterio.merge` mosaic touches the `[convert]` extra. Tiles are cached under
  the same XDG cache dir the index uses (`default_dem_cache_dir`,
  `$UMBRA_DEM_DIR`), so a second conversion over the same area re-downloads
  nothing. `fetch_dem_for_bbox`, `copernicus_tile_id`, `tile_ids_for_bbox`,
  `default_dem_cache_dir` and `DemUnavailableError` are exported from the package
  root; the `--dem` CLI option now accepts a path *or* `auto` and validates a
  given path exists.
- **DEM terrain orthorectification for SICD geocoding — `umbra convert --dem`
  / `sicd_to_geocoded_cog(dem=...)` (`docs/STRATEGY.md` 5.5).** The single named
  remaining strategic code gap: every path to the open data assumed a flat
  height plane, which mislocates relief (a pixel is placed where the radar ray
  meets the plane, not where it meets the ground). `--dem PATH` — any
  rasterio-readable elevation model, e.g. a Copernicus/SRTM COG — now walks each
  ground-control point onto the terrain surface via the standard ortho
  fixed-point iteration (`_refine_gcps_with_dem`: project at a height → sample
  the DEM there → reproject, until the height it lands on stops moving), so
  hilltops and valley floors land in their true ground position. `--dem`
  supersedes `--projection`; where the DEM has no coverage a point falls back to
  the scene reference height rather than snapping to zero. Both the iteration and
  the DEM lookup are injectable (`project`/`sample_height` callables), so the
  whole path is exercised offline with plain callables and a hand-written DEM
  raster — no sarpy DEM plumbing, and the sarpy-facing HAE projector batches
  points that share a (binned) height into one call. Stdlib/rasterio-only tests
  cover convergence to a closed-form terrain fixed point, the flat-DEM and
  off-DEM fallbacks, the DEM sampler (ramp read, out-of-bounds/nodata masking,
  CRS reprojection), and the end-to-end + CLI paths.
- **Published + fetchable whole-catalog PMTiles basemap — `umbra tiles --fetch`
  (`docs/STRATEGY.md` 5.2, `docs/DEMO_APP_GAPS.md` Path A step 3).** `umbra
  tiles` shipped the stdlib-only PMTiles *encoder*; this ships the built
  *artifact*. The weekly `publish-index.yml` workflow now tiles the freshly
  built index (`umbra tiles --local`, no second crawl) into a single-file
  `catalog.pmtiles` and writes a `catalog.html` MapLibre GL viewer pointed at
  the published archive, uploading both to the rolling `catalog-index` release
  beside `catalog.db` / `umbra-open-data.parquet`. The consume side mirrors
  `CatalogIndex.from_release()`: `pmtiles.fetch_prebuilt_pmtiles()` downloads the
  release asset via the resume-safe `download_url` to `default_pmtiles_path()`
  (`catalog.pmtiles` beside the cached `catalog.db`, honouring `$UMBRA_PMTILES`),
  and a new `umbra tiles --fetch` mode (`--out` optional, `--url` override,
  `--viewer` writes a local viewer) gives a fresh install a fast, zoom-anywhere
  map of the *entire* archive with no crawl and no index — the visual sibling of
  `umbra index fetch`. Stdlib-only and fully offline-tested against a mocked
  release download and a round-tripped archive; the existing build path is
  unchanged.
- **Read-through catalog search — `CatalogIndex.search_live()` and
  `umbra search --local --live` (`docs/CODEBASE_ANALYSIS.md` §4.4 / P3 #21).**
  The transparent middle between the instant-but-stale local index and the
  always-current-but-slow live walk, the "make the index the default path" gap
  the analysis doc names. `search_live()` answers the whole query from the local
  index *and* walks only acquisitions at or after the index's freshness horizon
  (its newest indexed `acq_date` minus `overlap_days`), merging the two streams
  in the usual `(task, acq_date)` order and de-duplicating by sidecar href — so a
  repeat search stays near-instant but still catches anything published since the
  index was built. With `refresh=True` (the default) each genuinely new
  acquisition the delta discovers is upserted into the index as it is yielded
  (the read-through cache warms, so the next call walks even less; a read-only
  index disables warming automatically rather than failing). `umbra search
  --local --live` exposes it on the CLI; `--live` without `--local` is rejected.
  The bound reuses the same recent-only sidecar pruning `umbra index update`
  relies on, and the whole path is offline-tested with an injected catalog.
- **Keyed single-item lookup on the catalog index — `CatalogIndex.get(item_id)`
  (`docs/CODEBASE_ANALYSIS.md` §4.5).** The retrieval complement to
  `search()`'s listing: `get()` returns the indexed `UmbraItem` with a given
  STAC id (or `None`), backed by a new `idx_items_id` index so it stays fast as
  the published `catalog.db` snapshot grows, rather than scanning an
  id-filtered `search`. `umbra serve`'s `GET /collections/{id}/items/{item_id}`
  now resolves through this keyed lookup when it is backed by an index (via a
  new `serve.get_one` helper), falling back to the id-filtered search for the
  live-catalog source that only lists. The index is additive — existing
  databases gain it on the next open with no schema-version bump — so a
  deployed or fetched snapshot needs no rebuild.
- **Structured `--json` success output on the remaining commands
  (`docs/AI_INTEGRATION_IDEAS.md` §A1).** The machine-readable *error* contract
  already shipped; this completes the *success* side, so every command that
  produces a result now has a stable, machine-readable stdout shape:
  - `umbra download --json` emits a `[{asset, path, bytes, sha256}, …]` array,
    hashing each written file with a streaming SHA-256 so a caller can verify
    what it fetched without re-reading it
    (`docs/schemas/download.schema.json`).
  - `umbra index info --json` emits the index summary — `path`, `size_bytes`,
    `items`, `start`, `end`, `tasks`, `built_at`
    (`docs/schemas/index-info.schema.json`).
  - The render commands `change`, `timescan`, `swipe`, `gallery` and `map`
    accept `--json` and emit a `{output, items_used, parameters}` manifest
    naming the artifact written, the acquisition ids it was built from, and the
    settings used; a command that also writes an auxiliary file (e.g.
    `umbra change --narrate`'s narration JSON) lists it under an optional
    `sidecars` map (`docs/schemas/render-manifest.schema.json`).

  Human progress lines, warnings, and the `--place` "Resolved …" status line go
  to stderr under `--json`, so stdout carries the JSON alone. The three new
  schemas are published as public API alongside the error contract
  (`docs/schemas/README.md`), under the same backwards-compatibility rules as
  `umbra_py.__all__`.
- **Machine-readable errors (`docs/AI_INTEGRATION_IDEAS.md` §A1).** Every
  `UmbraError` now carries an optional `hint` — a single actionable recovery
  step — and serializes to a stable `{"error", "message", "hint"}` dict via
  `UmbraError.to_dict()`. When a command fails and JSON output is active (the
  invocation passed `--json`, or `UMBRA_JSON` is set to a truthy value) the CLI
  prints that object to stderr instead of a prose line, so an agent can branch
  on `error` and act on `hint` without parsing a traceback; otherwise it prints
  the usual `error: …` line plus a `hint: …` line when one applies. The wire
  shape is published as public API in `docs/schemas/error.schema.json`
  (`docs/schemas/README.md`). Every optional-dependency and API-key error now
  populates `hint` with the exact `pip install` command or the environment
  variable to set (e.g. `pip install "umbra-py[viz]"`,
  `Set ANTHROPIC_API_KEY (or OPENAI_API_KEY)`), and geocoding's no-match error
  points at `--bbox`.

### Fixed
- **Catalog index is now safe for concurrent, multi-process access
  (`docs/CODEBASE_ANALYSIS.md` §4.5).** The published `catalog.db` snapshot
  (`umbra index fetch`) is a *shared* artifact — read by `umbra serve`, `umbra
  demo` and the MCP server while a CLI writer (`umbra index update` / `build` /
  `bake-*`) may be refreshing it in another process — but `CatalogIndex` opened
  its connection with SQLite's single-process defaults (rollback journal, no busy
  timeout), so a reader that arrived while a writer held a transaction could fail
  with `database is locked`. `CatalogIndex._configure_connection` now sets a
  `busy_timeout` (5 s — a contended access waits rather than erroring at once) and
  switches the file to WAL journal mode (best-effort, swallowed on a read-only
  medium), under which a reader never blocks on the writer and a single writer
  never blocks readers. WAL needs only the writable file and directory the index
  already required (it ensures the schema on every open), so it tightens nothing;
  `check_same_thread` is left at its default because `umbra serve` already opens a
  fresh backend per request. No model call, no new dependency (two stdlib
  `PRAGMA`s); offline-tested in `tests/test_index.py` (the PRAGMAs, WAL
  persistence across reopen, and a second connection reading during an open write
  transaction).
- **Asset classifier now recognises a plain `image/tiff` GeoTIFF
  (`docs/CODEBASE_ANALYSIS.md` P1 #8).** `_classify_asset` tested `"tif" in
  name`, but `name` is upper-cased (`f"{key} {href}".upper()`), so the lowercase
  substring could never match — dead code. Umbra's own COGs were still caught by
  the parallel `"geotiff" in media` check, but an asset that declares a plain
  `image/tiff` media type (no `geotiff` profile substring) with a `.tif` key
  slipped through and was dropped from `asset_map` / `available_assets` — i.e.
  its GEC product became invisible to `info`, `download`, and every consumer of
  the item. The check now matches `"TIF"` against the upper-cased `name`; added a
  regression test (`tests/test_models.py`) covering the plain-`image/tiff` case.

### Security
- **Generated HTML now escapes all remote metadata and validates link schemes
  (`docs/CODEBASE_ANALYSIS.md` §3.1).** The map/gallery/swipe/change artifacts
  and the `umbra view` / `umbra demo` pages interpolate strings that come from
  remote STAC JSON, and the CLI accepts arbitrary item URLs — so a hostile STAC
  document could previously inject markup (a `<script>` in an `id`/`platform`
  field) or a `javascript:` link into an HTML file a user then opens locally.
  `viz._popup_html` now `html.escape()`s every remote-derived value (`id`,
  `datetime`, `platform`, `instrument_mode`, `product_type`, `polarizations`,
  `available_assets`) and routes the STAC link through a new shared
  `_html.safe_href()` gate — a scheme allowlist (`http(s)` only) plus
  attribute-escaping — which drops the link rather than emitting an unsafe
  scheme. The same `safe_href` gate now covers `_html.py`'s card/gallery links,
  `viewer._viewer_html`'s panel/title/link, and `demo.py`'s client-side STAC
  link (scheme-guarded at build time). `_lazy_imagery.popup_button_html` already
  escaped its inputs and was unchanged.

### Added
- **The local catalog index is now schema-versioned (`docs/CODEBASE_ANALYSIS.md`
  §4.5 / P1 #10).** `CatalogIndex` records its on-disk layout with
  `PRAGMA user_version` (`_SCHEMA_VERSION = 1`) and checks it on open. This
  matters because the index is no longer a private cache — the weekly `catalog.db`
  snapshot users pull with `umbra index fetch` is a *distributed* artifact that
  `--local` search, the MCP server, `umbra serve`, `umbra demo` and `umbra tiles`
  all consume — so the next schema change (the demo denormalizations in
  `docs/DEMO_APP_GAPS.md` G2, an R\*Tree upgrade) needs to be a migration, not a
  confusing break. A fresh or pre-versioning database (`user_version 0`, which
  every current snapshot reads) is adopted in place and stamped; a database
  written by a *newer* umbra-py — or a lower versioned schema with no migration
  path — now raises the new `IndexSchemaError` (surfaced by the CLI as a clean
  `error: …`) instead of being silently misread. No new dependency, no behaviour
  change for a matching index; mirrors the `PRAGMA user_version` discipline the
  `catalog.embed.db` sidecar already used. `IndexSchemaError` is exported from the
  top-level package.
- **STAC Query extension on `umbra serve` — filter `/search` by product type and
  place, not just bbox/date (`docs/AI_INTEGRATION_IDEAS.md` §B2 / `docs/DEMO_APP_GAPS.md`
  Path B).** The read-only STAC API answered only the STAC *core* filters (bbox,
  datetime, ids), even though the `CatalogIndex` it wraps already filters by
  product type and free-text task/site `area`. `/search` and
  `/collections/{id}/items` now accept `product_types` (comma-separated, e.g.
  `GEC,SICD`), `area` (a task/site substring) and a `fuzzy` toggle — as GET
  query params, plain top-level `POST` body fields, or a standards-compliant
  STAC **Query extension** object (`{"query": {"product_types": {"in": ["GEC"]},
  "area": {"like": "Beet Piler"}}}`, with bare-value shorthands). The filters
  are pushed straight down to the backend `search` both `CatalogIndex` and the
  live `UmbraCatalog` already implement, so the same query works against either,
  and GET pagination carries them into the `next` link. Two new pure parsers
  keep it honest: `parse_product_types` rejects an unknown product type with a
  `400` (never a silent empty result), and `parse_query` rejects an unsupported
  query property or operator with a `400` so a client's filter is never quietly
  dropped. The `item-search#query` conformance class is now advertised. Wired
  entirely behind the deterministic document/parse boundary, so it is
  offline-tested through the in-process `TestClient` with no network and no
  `viz` extra.
- **Visual similarity search over MCP — `find_similar` / `find_similar_text`
  tools on `umbra-mcp` (`docs/AI_INTEGRATION_IDEAS.md` §C5).** The flagship
  scene-embedding capability (`umbra embed`) is now conversational: the
  `umbra-mcp` server exposes two tools (plus a `find-similar-scenes` prompt) that
  wrap the shipped `SceneEmbeddingIndex` unchanged. `find_similar(url)` renders and
  embeds one acquisition's quicklook and ranks the pre-embedded archive by cosine
  similarity — "find scenes that *look like* this flooded field", the search that
  lives in the pixels rather than the metadata, with the query item excluded from
  its own results; `find_similar_text(query)` ranks the stored image vectors against
  a plain-language query ("ships at a berth") given a joint CLIP-family model. Both
  require a scene index built ahead of time with `umbra embed build` (a sidecar
  `catalog.embed.db`; a missing one raises a self-describing error pointing at that
  command) and the `[ai]` embedding key, and return `SceneMatch` records as compact
  cards — each carrying the acquisition's STAC `href`, so a match hands straight to
  `get_item` / `quicklook` / `change_composite`, closing the discover-then-view loop
  in one conversation. Like the rest of the server they hold the determinism
  boundary: the only model call is turning the query image/text into a vector (the
  injectable `default_image_embedder` / `default_text_embedder`), while rendering,
  storage and cosine ranking stay deterministic — so the whole path is
  offline-tested with a stand-in embedder and renderer.
- **Incremental index refresh — `umbra index update` / `CatalogIndex.update`
  (`docs/CODEBASE_ANALYSIS.md` §4.4, `docs/STRATEGY.md` §6).** A full `umbra
  index build` fetches a `*.stac.v2.json` sidecar for *every* acquisition in
  scope — the N+1 round trips that dominate a crawl — so on an index only days
  old almost all of that work re-reads unchanged data. `update` instead reads
  the newest acquisition date already indexed and passes it (minus
  `--overlap-days`, default 1) as the `start` bound to the live walk, which
  prunes older acquisitions' sidecar fetches, so a weekly refresh reads only the
  new passes and upserts them exactly as `build` does. It is the incremental
  companion to the shipped `umbra index fetch` / `CatalogIndex.from_release`:
  bootstrap from the weekly snapshot once, then `update` to catch acquisitions
  published since — the "walk only prefixes newer than the index" improvement the
  analysis doc named and the "keep the crawl incremental" guardrail in the
  strategy doc. The bound is on *acquisition* date, not publish date, so
  `--overlap-days` re-scans a little past the newest indexed date to catch
  near-real-time lag, and the docstring is explicit that completeness over
  back-dated late arrivals still wants a widened window or a full `build`. An
  empty index falls back to a full build; `--since` forces a specific lower
  bound; `--bbox`/`--place`/`--area`/`--limit` scope the refresh exactly as
  `build` does. `CatalogIndex.update()` returns an `UpdateResult`
  (`scanned`/`added`/`refreshed`/`start`, exported from the package root), and
  the whole path is offline-tested with a recording fake catalog (derived-bound,
  overlap widening, new-vs-refreshed tally, empty-index fallback, `since`
  override, scope pass-through, and the `start=`-rejection guard), plus a CLI
  test. The published weekly snapshot is deliberately left as a full rebuild so
  it stays authoritative.
- **Whole-catalog PMTiles tiling — `umbra tiles` / `build_pmtiles`
  (`docs/DEMO_APP_GAPS.md` Path A step 3).** Every other map surface embeds its
  features in the page (Folium polygons in `umbra map`, an inline JSON blob in
  `umbra demo`) — great up to a few thousand acquisitions, but the *whole*
  acquisition set was the last open demo-app gap. `umbra tiles` pre-cuts the
  catalog's acquisition centroids into a vector-tile pyramid and packages it as a
  single [PMTiles v3](https://github.com/protomaps/PMTiles) file, so a map fetches
  only the tiles in view and stays fast at whole-archive scale. The output drops
  straight onto GitHub Pages or into a bucket — no tile server, and **no
  tippecanoe**: because the geometry is points, the entire encoder (the Mapbox
  Vector Tile protobuf and the PMTiles container) is pure standard library, so it
  runs in a core install and is fully offline-tested by decoding its own output
  (verified against the reference `pmtiles` / `mapbox-vector-tile` readers).
  `--viewer` also writes a self-contained MapLibre GL page that renders the
  archive as a scalable circle layer with a click popup, the same OpenStreetMap
  basemap the Leaflet demo uses, and the mandatory CC-BY attribution. Reads a
  prebuilt index with `--local` for a near-instant build. `build_pmtiles` /
  `write_pmtiles` / `build_viewer` / `save_viewer` are exported from the package
  root.
- **SICD → geocoded COG — `umbra convert` / `sicd_to_geocoded_cog`
  (`docs/STRATEGY.md` 5.5).** Umbra's `GEC` asset is already a geocoded COG, but
  the complex `SICD` product lives in the radar slant plane and does not open on
  a map, in QGIS, or in the xarray/rioxarray stack without hand-rolled
  geocoding. The new `convert` extra function detects amplitude from the complex
  product and warps it onto a north-up EPSG:4326 cloud-optimized GeoTIFF using
  SICD's own image-projection model — a lattice of ground control points from
  `project_image_to_ground_geo`, so the sensor geometry (not a naive
  corner-stretch) places the pixels. `umbra convert SRC DST` geocodes by default
  (with `--gcp-grid`, `--resolution`, `--resampling`, `--projection`, and
  `--linear` for magnitude instead of dB); `--slant-plane` keeps the prior
  ungeoreferenced amplitude image for quick inspection. The geocoding is an
  honest flat-earth first slice (pixels on the scene's height-above-ellipsoid
  plane): exact over flat terrain, adequate for map placement elsewhere; full
  terrain orthorectification (a DEM, MultiRTC interop) is the follow-on. The
  geocoding core (`_warp_gcps_to_cog`) is free of any sarpy dependency, so it is
  offline-tested with a plain array and hand-built GCPs against real `rasterio`,
  and the read → amplitude → GCP → warp path is exercised end to end with a
  faked reader — `convert.py` gains its first test suite (the `[convert]` extra
  CI job). `sicd_to_amplitude_geotiff` / `sicd_to_geocoded_cog` are exported
  from the package root.
- **Async job semantics for long `umbra serve` renders — `202 Accepted` + poll
  (`docs/DEMO_APP_GAPS.md` Path B step 2).** The composite render endpoints
  (`POST /artifacts/change` / `timescan` / `swipe`) accept an opt-in
  `"async": true` in the request body. Instead of holding the request for the
  whole render, the server queues the work on a small background pool and returns
  `202 Accepted` with a job document (and a `Location` header). Two new endpoints
  drive the poll loop: `GET /jobs/{id}` reports status
  (`queued` → `running` → `succeeded` | `failed`, with a `result` link once done)
  and `GET /jobs/{id}/result` serves the finished artifact. There is **no
  separate result store** — the render writes the same content-addressed disk
  cache the synchronous path uses, so a completed job's result *is* a cache entry,
  and an async request whose key is already cached returns an already-`succeeded`
  job with no work. Frame resolution and validation stay synchronous, so a bad
  request (too few acquisitions, malformed bbox) is still a fast `400` and never a
  doomed background job; a failed render becomes a `failed` job whose result
  endpoint mirrors the synchronous status (`501` for a missing `viz` extra, `500`
  otherwise). Default behavior is unchanged when `"async"` is absent. The queue's
  executor is injectable (`build_app(..., job_executor=...)`) and a new pure
  `job_to_dict` builder keeps the whole path offline-testable with no wall-clock
  timing.
- **`POST /artifacts/swipe` on `umbra serve`, and `umbra demo --server-url` that
  calls the render endpoints — closing the self-serve demo loop
  (`docs/DEMO_APP_GAPS.md` R4 / Path B step 3).** `umbra serve` gained a fourth
  artifact endpoint that renders `viz.swipe_map` (an interactive before/after
  HTML page) alongside the three PNG composites; because it returns HTML rather
  than a PNG it is served from its own disk-cache entry, and the render
  functions stay injectable (offline-testable) via a new `swipe` field on
  `Renderers`. The server now also sets a permissive **read-only CORS** policy
  so a browser page on another origin can call `/search` and the artifact
  endpoints. On the front end, `build_demo(..., server_url=...)` /
  `umbra demo --server-url <serve URL>` add an "Analyze this view" sidebar panel
  whose Change / Timescan / Swipe buttons POST the currently-filtered
  acquisitions (chronological, sampled to a bounded cap) to the matching
  endpoint and render the returned artifact in place (swipe opens its map in a
  new tab) — the R4 "run this analysis here" affordance over *any* site. With no
  `server_url` the page stays a fully static single file, unchanged.

### Changed
- **Live search now fetches acquisition sidecars concurrently
  (`docs/CODEBASE_ANALYSIS.md` §4.2 / P1 #9).** Discovery is the library's core
  value — searching a catalog that has no search API — and the walk's one
  remaining per-acquisition round trip was the `*.stac.v2.json` sidecar GET,
  issued serially, so a 50-item search paid ~50 latencies back to back.
  `UmbraCatalog._walk_task` now resolves those sidecars through a small thread
  pool (`_SIDECAR_WORKERS = 8`, mirroring the gallery's proven pattern) and
  yields them strictly in acquisition-date order, so a task's wall time collapses
  from N serial fetches toward N/workers with the output order unchanged.
  Fetching in windows keeps the pool bounded and caps wasted work at one window
  when an early `limit` / `max_per_task` stops the search. The shared
  `_http.default_session()` connection pool was sized up (`pool_maxsize=16`) so
  the fan-out reuses connections instead of churning them. No behavior change
  beyond speed — same items, same order, still fully offline-testable.
- **The shared HTTP session now retries transient failures, and downloads verify
  their integrity (`docs/CODEBASE_ANALYSIS.md` P1 #5/#6, §3.2/§4.3).** The
  library's core job is fetching data from a public bucket; these harden that
  path from alpha-fragile to dependable, and every caller inherits them because
  everything routes through `_http.default_session()`.
  - **Retry/backoff on the shared session.** `default_session()` now mounts an
    `HTTPAdapter` with `urllib3` `Retry(total=3, backoff_factor=0.5,
    status_forcelist=(429, 500, 502, 503, 504))` on idempotent `GET`/`HEAD`
    requests. A single transient S3 hiccup no longer fails an entire
    multi-minute index build, catalog walk, or download.
  - **Download integrity is verified before finalizing.** `download_url` now
    compares the received byte count against `Content-Length` and raises
    `DownloadError` on a short read (a cleanly-closed truncated body that
    previously renamed a silently-incomplete `.part` into place), and converts a
    mid-stream connection break into `DownloadError` — in both cases leaving the
    `.part` on disk so a later call resumes rather than discarding progress.
  - **Resume is validated with `If-Range`.** A resumed download stores the
    object's `ETag` next to its `.part` and sends it as `If-Range` on the next
    `Range` request, so if the remote object changed the server returns the whole
    new object (a clean restart) instead of splicing bytes from two different
    objects into a corrupt file.

### Added
- **On-demand render artifacts on `umbra serve` — quicklook / change / timescan
  over any site (`docs/DEMO_APP_GAPS.md` R4 / Path B step 2).** The STAC API
  façade shipped *discovery* (search/collections/items); the demo-gap analysis's
  last self-serve requirement (R4) was *triggering the visual products from the
  UI over any site*, not just a curated set baked at build time. The server now
  renders them on demand, wrapping the existing `umbra_py.viz` functions
  unchanged:
  - **Three endpoints.** `GET /artifacts/quicklook/{item_id}.png` renders one
    acquisition's SAR quicklook; `POST /artifacts/change` renders a 2–3 date
    change composite over a query (`ids`, or `bbox` + `datetime`); `POST
    /artifacts/timescan` renders a temporal-statistics composite over a series.
    A query resolving to more frames than a composite takes is subsampled
    deterministically (change → first/middle/last three dates; timescan → an
    evenly-spaced cap of 60), and too few is a `400`.
  - **Disk-cached by inputs.** Every artifact is cached to disk keyed by a
    content hash of its kind, ordered frame ids and render options, so a repeat
    request is a file read (`X-Umbra-Cache: hit`) — closing the "no artifact
    caching" gap for these endpoints. Frame order is part of the key (a change
    composite is not the same picture with its passes reversed).
  - **Injectable renderers, offline-testable.** `build_app(..., renderers=...)`
    overrides the render functions, so the routes are unit-tested in the core
    install with no network and no `viz` extra; the default renderers lazily
    import `viz` at request time and a missing extra surfaces as HTTP `501`.
  - **Opt-out for public instances.** `umbra serve --no-artifacts` mounts only
    the read-only STAC surface (bounding COG-streaming egress); `--cache-dir`
    overrides where PNGs are cached. Rendering is synchronous for now — an async
    job queue for long renders is the ledgered follow-on (`TODO.md`).
- **`umbra demo` — a self-serve interactive catalog explorer in one HTML page
  (`docs/DEMO_APP_GAPS.md` G3/G4, Path A front end).** Every other visual command
  emits a *one-shot* artifact — change a filter and you re-run the CLI and open a
  new file. `umbra_py.demo` (`umbra demo`, `[viz]` extra) produces the missing
  *application*: a single self-contained page (no extra required — the page is
  pure HTML and the map runs browser-side) over a whole slice of the catalog
  with the interactive controls the gap analysis names as absent today —
  client-side **faceted filters** (free-text site/id search, a date-range slider
  bounded to the data, product-type chips), **marker clustering** so it scales
  past a Folium map's few-hundred-polygon ceiling, and a click-to-quicklook SAR
  overlay streamed on demand.
  - **Static, single file, no server.** Leaflet + Leaflet.markercluster from
    pinned CDNs, the catalog embedded as JSON, all filtering in the browser — it
    opens from `file://` or any static host (GitHub Pages), exactly like
    `umbra swipe` / `umbra gallery` output. This is Path A's front end delivered
    as an artifact; the productized FastAPI server app remains Path B.
  - **Reads the fast index.** Like the other visual commands it routes through
    `_gather_items`, so `--local` builds the page from a prebuilt index
    (`umbra index fetch` / `umbra index build`) in milliseconds instead of
    re-walking S3 — the "no multi-minute walk in the user's critical path"
    requirement a demo needs. `--max-per-task 1` gives a one-marker-per-site
    whole-archive overview.
  - **Reuses the proven COG driver.** The per-item "Get SAR image" button drives
    the same browser-side geotiff.js fetcher as `umbra map --lazy-imagery`; the
    only addition is a `window.umbraLazyMap` fallback in `_lazy_imagery` so the
    shared driver resolves a plain Leaflet map on this non-Folium page (the
    Folium DOM-walk path is untouched). Pass `--no-lazy-imagery` for a
    metadata-only explorer with no CDN dependency at click time.
  - **Safe by construction.** The catalog arrives as a JSON global
    (`window.UMBRA_DEMO`, with `</` neutralised against a `</script>` break-out)
    and the application JavaScript is a *static* string that reads it — remote
    metadata is placed into the DOM with `textContent` / `setAttribute`, never
    parsed as HTML. The generator is stdlib-only, so it runs in a core install
    and is fully offline-testable.
- **Archive scene embeddings — visual similarity search (`docs/AI_INTEGRATION_IDEAS.md`
  C5, the last open AI item).** Every other search matches *metadata* (a date, a
  bbox, a task name); this matches *appearance*. `umbra_py.embed`
  (`umbra embed`, `[ai]` + `[viz]` extras) embeds each acquisition's rendered
  quicklook into a vector once and then ranks scenes by cosine similarity, so
  "find scenes that look like this one" becomes plain offline arithmetic over the
  stored vectors — a capability nothing in the Umbra ecosystem offers.
  - **`umbra embed build`** renders each item's quicklook once (only downsampled
    overviews stream over HTTP — no full download) and embeds it, keyed by item
    id so a rebuild only embeds what is new. It takes the same search-vs-URLs
    interface as `umbra change` (plus `--local`/`--index-db`), and skips a scene
    whose asset won't render rather than aborting the batch.
  - **`umbra embed similar <item-url>`** renders and embeds the query item, then
    returns the archived scenes that look most like it (the query is excluded from
    its own results) — image-to-image search.
  - **`umbra embed search "a flooded field"`** ranks the stored *image* vectors
    against a text query — text-to-scene search, given a joint CLIP-family model
    whose text and image encoders share a space.
  - **`umbra embed info`** reports the scene-vector count, model and dimension.

  It holds the library's determinism boundary (`docs/AI_INTEGRATION_IDEAS.md` §A4,
  §6.1): the *only* model calls are turning an image or a text query into a
  vector (injectable `ImageEmbedder` / text `Embedder`, default an
  OpenAI-compatible multimodal `/embeddings` endpoint via the already-core
  `requests`, user-supplied key, never implicit). Rendering, storage, cosine
  ranking and thresholding are stdlib-only (no `numpy`, no `sqlite-vec`), so the
  whole feature is offline-testable with a deterministic stand-in embedder and
  renderer. The vectors live in a schema-versioned sidecar `catalog.embed.db`
  beside the catalog index — never inside `catalog.db` — so the deterministic
  index and its published snapshot never carry model-derived data a core install
  can't use (the same boundary `umbra semantic` uses). A `SceneMatch` is a pointer
  back to a real acquisition (id, task, datetime, STAC href), never a
  model-authored fact.
- **Example notebook gallery (`docs/STRATEGY.md` 5.4 / `docs/AI_INTEGRATION_IDEAS.md`
  B3) — the demo notebooks DevRel links first.** Three self-contained, self-checking
  Jupyter notebooks under `examples/`, each driven by a small deterministic search
  and ending its code cells with `assert`s, so running one top-to-bottom is both a
  tutorial and a live smoke test:
  - `01_hello_umbra.ipynb` — search → summarize → quicklook, plus the zero-glue
    geopandas (`__geo_interface__`) and model-ready (`to_llm_context`) paths.
  - `02_download_and_open_gec.ipynb` — stream a GEC into an analysis-ready
    `xarray.DataArray` (no full download), analyze it, and round-trip the CRS with
    `rioxarray`.
  - `03_change_detection.ipynb` — find a repeat-imaged site, pick two passes, and
    composite the change into one color image.

  The notebooks ship with cleared outputs. `tests/test_examples.py` guards them
  **offline on every CI run** using only the stdlib (`json` + `ast`): each notebook
  must be well-formed, its code cells must parse, every `umbra_py` symbol it
  references must be public (drift protection — a renamed export turns the build
  red), and the CC-BY attribution line must be present. The same test executes the
  notebooks end-to-end under `pytest -m network` when `nbclient` and the render
  extras are available, so the weekly canary can prove the documented flows still
  run against the live bucket.
- **PyPI release readiness — the single highest-leverage adoption gap
  (`docs/CODEBASE_ANALYSIS.md` P0 #2/#3, P2 #11/#15).** The whole funnel is
  built (free-bucket search → paid Canopy archive, all in one library), but the
  README's first instruction, `pip install umbra-py`, still fails because the
  package isn't on PyPI. This lands the release plumbing so a maintainer can
  claim the name and ship:
  - **`release.yml` workflow** publishing to PyPI via **Trusted Publishing**
    (OIDC) on a published GitHub Release — no long-lived token stored in the
    repo. It builds the sdist + wheel, runs `twine check`, and refuses to
    publish if the `vX.Y.Z` release tag disagrees with the package version.
    `workflow_dispatch` runs a build-and-verify dry run without publishing.
  - **Single-sourced version.** `pyproject.toml` now derives the version from
    `umbra_py.__version__` via hatchling's dynamic version, so the two can no
    longer drift (`docs/CODEBASE_ANALYSIS.md` §2.2).
  - **PEP 561 `py.typed` marker** shipped in the wheel and sdist, so downstream
    type checkers finally consume the library's inline types.
  - **Repository-identity fix.** The `pyproject.toml` project URLs, the
    `CHANGELOG` compare/tag links, and the `CONTRIBUTING` clone command now all
    point at the canonical `reesehammer/umbra-py` instead of the stale
    `theminiverse` org (`docs/CODEBASE_ANALYSIS.md` P0 #3).
- **Canopy commercial-archive backend behind the same `search()` interface
  (`docs/STRATEGY.md` 5.1 — the single highest-value strategic move).** Umbra's
  open data is a static STAC catalog with no search API (which is why this
  library crawls S3); its *commercial* product, Canopy, exposes a real,
  authenticated STAC API over the full archive. `UmbraCatalog` now accepts a
  Canopy `token` (plus optional `archive_url` / `collections`), and when one is
  given the **same `search()` call** queries
  `api.canopy.umbra.space/archive/search` instead of walking the open bucket —
  *the same filters, the same `UmbraItem` results*, so every downstream verb
  (download, quicklook, change, chips, …) works unchanged against either
  archive. This is the funnel made literal: a user onboarded on the free data is
  already holding the exact tool they'd use as a paying customer. `bbox` and the
  date bounds are pushed down to the STAC API; `product_types` and
  `area`/`fuzzy` are applied to the returned items exactly as on the open path,
  so the interface is identical across both. The client speaks the STAC API
  standard — a POST item-search body plus `rel="next"` pagination (POST-merge or
  GET token links) — and the bearer token is only ever sent to the Canopy
  endpoint, never the open bucket; a 401/403 surfaces as a clear "token
  rejected" `CatalogError`. The CLI exposes it as `umbra search --token …`
  (falling back to `$UMBRA_CANOPY_TOKEN`), mutually exclusive with
  `--local`/`--db`. No model is involved and no credentials are needed to test
  it: the whole path is offline-testable against a mocked STAC API
  (`tests/test_canopy.py`).
- **`watch_site` MCP tool + `watch-site` prompt: the standing-analyst delta,
  now conversational (`docs/AI_INTEGRATION_IDEAS.md` C3 — the last open C3
  piece).** The `umbra watch` idempotent delta is now surfaced over the flagship
  `umbra-mcp` server, reusing `umbra_py.watch.watch()` unchanged. `watch_site`
  takes the same filters as `search_catalog` (`place`/`area`/`bbox`,
  `products`, `start`/`end`, `fuzzy`) and returns only the acquisitions **new**
  since the last check of that site — all of them on the first run, and just the
  delta on every re-check. State persists in the local catalog index's `meta`
  table (`MetaWatchStore`, created on first use), so a watch survives across MCP
  sessions with no extra setup; a stable `name` is derived from the query (pass
  an explicit `name` for several independent watches over one site), and
  `reset=True` re-establishes the baseline. The returned `new_items` are context
  cards ready to hand straight to `change_composite` / `timescan`, closing the
  standing-analyst loop (new pass → composite → describe) inside one
  conversation. The companion `watch-site` prompt packages that workflow.
  **No model is called** — this is pure set arithmetic over the deterministic
  search — so the whole surface stays offline-testable (the search source is an
  injectable live/index backend and the store an injectable index).
- **`umbra chips`: turn SAR scenes into georeferenced ML training tiles
  (`docs/AI_INTEGRATION_IDEAS.md` C4 / `docs/STRATEGY.md` 5.5 — the ML
  dataset-preparation layer).** For the model-*training* audience, the missing
  verb is *chipping*. The new `umbra_py.chips` module (`[load]` extra, mirroring
  `umbra_py.load`) walks a search result and cuts each acquisition's geocoded
  GeoTIFF into fixed-size, georeferenced tiles with a manifest that carries the
  per-chip metadata a training pipeline needs. `chip_item()` reads band 1 of the
  item's COG one window at a time through GDAL's `/vsicurl/` driver — so only the
  bytes for each tile stream over HTTP range requests (no multi-gigabyte
  download, memory bounded to one chip) — and emits full `chip_size` × `chip_size`
  tiles as GeoTIFF (georeferenced) or `.npy` (bare `float32`); partial edge tiles
  are dropped so every chip has the exact shape a loader expects, `stride`
  controls overlap for dense inference / augmentation, and `min_valid` drops the
  mostly-nodata corners of a rotated footprint. `write_chips()` chips a whole
  search into a dataset and writes a manifest — `.jsonl` (one `ChipRecord` per
  line, the standard ML format) or `.geojson` (a `FeatureCollection` of chip
  footprints for QGIS / geopandas), both stdlib-only — where every record carries
  the chip's geographic bbox, CRS, affine transform, grid position and source
  pixel window plus the acquisition's datetime, place, platform, polarization,
  incidence angle and resolution, stamped with the CC-BY attribution (the same
  license discipline the library applies to GeoTIFF tags and xarray attrs). The
  `umbra chips` command mirrors `umbra change`'s search-vs-URLs interface (pass
  STAC URLs directly, or `--area`/`--bbox` with `--start`/`--end`, plus
  `--local`/`--index-db` to gather from a prebuilt index) with `--chip-size`,
  `--stride`, `--format`, `--db`, `--min-valid`, `--manifest` and a `--json`
  dataset summary. No model is called — chipping is pure raster iteration +
  manifest logic in the deterministic core — so the whole feature is
  offline-testable with a real on-disk GeoTIFF and no network.
- **`umbra watch`: idempotent delta detection for standing site monitoring
  (`docs/AI_INTEGRATION_IDEAS.md` C3 — the first "agent as a standing analyst"
  primitive).** SAR re-images a site pass after pass, so the natural way to
  monitor one is to run the same search on a schedule and act only on what is
  *new*. The new `umbra_py.watch` module packages the delta, not the schedule:
  `watch()` searches an injected source (a live `UmbraCatalog` or a
  `CatalogIndex`), compares the results against the set of acquisition keys
  previous runs already reported, returns only the new ones, and folds them back
  into a small state store. It is idempotent — an immediate re-run with no newly
  published data reports zero — because the delta is an exact set difference over
  sidecar hrefs, not a date watermark (which would miss a late upload dated
  earlier than acquisitions already seen). State persists in a `CatalogIndex`'s
  existing `meta` table (`MetaWatchStore`, no schema change, so a fetched
  snapshot is a valid store); `InMemoryWatchStore` is the offline-testable
  stand-in. The `umbra watch` command mirrors `umbra search`'s query flags plus
  `--name` (stable watch identity, auto-derived from the query via `watch_key`
  when omitted), `--state-db`, `--reset` (re-baseline), `--json` (a machine
  readable delta whose `new_items` are `to_llm_context` cards, carrying the CC-BY
  attribution), and `--exit-code` (exit 10 when there are new acquisitions, so a
  scheduler's shell `if` can branch without parsing output). Cron, a GitHub
  Action, or an agent loop supplies the schedule; this supplies the delta — pair
  it with `umbra change --narrate` / `umbra describe` for the full standing
  analyst (new pass lands → composite against the previous pass → narration). The
  search source and state store are both injectable, so the whole feature is
  deterministic and offline-testable with no network and no model call.
- **`umbra change --narrate`: a vision model narrates *what changed* between two
  SAR passes, grounded in a deterministic per-block decibel-change grid
  (`docs/AI_INTEGRATION_IDEAS.md` C2 — the second Tier C VLM-in-the-loop
  capability, completing C2).** `umbra describe` reads one scene; this reads the
  *change* between two. The new `umbra_py.narrate` module (`[ai]` + `[viz]`
  extras) computes `compute_change_stats` — a coarse grid of the mean *signed*
  backscatter change in dB (`20·log10(later) − 20·log10(earlier)`: positive =
  brightened/appeared, the composite's green; negative = dimmed/vanished, its
  magenta) plus per-block change fractions — and hands the model both the change
  composite PNG and that grid, so the narration cites numbers rather than
  hallucinating change the pixels don't support. Add `--narrate` to `umbra change`
  (composite output only): it renders the composite once, writes it, prints a
  structured `ChangeNarration` (`{summary, changes[], confidence, caveats[]}`),
  and writes the machine-readable grid alongside as `<out>.narration.json` so
  every statement is auditable against a number a test can recompute. The model
  **only interprets**: the picture and the dB grid are produced deterministically,
  the reply passes the `parse_narration` boundary and never becomes a filter, a
  URL, or a measurement, and every narration carries the CC-BY attribution and the
  `AI_PROVENANCE` note. Like `umbra describe`, the model call is an injectable
  `Narrator` (and the render an injectable `ChangeRenderer`) reusing the same
  `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` provider plumbing (`OPENAI_BASE_URL` /
  `UMBRA_NARRATE_MODEL`, `--model`), `requests` only — no heavy SDK — so the whole
  feature is offline-testable with no network. It stays behind the `[ai]` extra
  and never runs implicitly. `narrate`, `parse_narration`, `compute_change_stats`,
  `ChangeNarration`, `ChangeStats` and `NarrateError` are exported at the top
  level.
- **`umbra describe "…"`: a vision model reads a SAR scene in plain language
  (`docs/AI_INTEGRATION_IDEAS.md` C2 — the first Tier C VLM-in-the-loop
  capability).** Searching gets you the scene; *reading* SAR is a separate skill
  (why is water dark? is that black patch shadow or an empty field?). The new
  `umbra_py.describe` module (`[ai]` + `[viz]` extras) renders an item's quicklook
  and sends that PNG plus the `UmbraItem.to_llm_context()` metadata card to a
  configured vision model, returning a structured `SceneDescription`:
  `{summary, observed_features[], confidence, caveats[]}`. `umbra describe
  <item-url>` prints the reading (`--json` for the object; `--asset` / `--no-db` /
  `--max-size` control the render; `--model` picks the model). The SAR literacy a
  general vision model lacks — backscatter ≠ brightness, speckle, layover/shadow,
  one-frame ≠ change — is encoded once in the packaged prompt. The model **only
  interprets**: the picture and metadata are produced deterministically, its
  reply passes the `parse_description` boundary and never becomes a filter, a URL,
  or a coordinate, and every description carries the CC-BY attribution plus a new
  `AI_PROVENANCE` note so a model's reading of radar is never mistaken for a
  measurement. Like `umbra ask`, the model call is an injectable `Describer` (and
  the render an injectable `Renderer`) chosen from `ANTHROPIC_API_KEY` /
  `OPENAI_API_KEY` (`OPENAI_BASE_URL` / `UMBRA_DESCRIBE_MODEL`), `requests` only —
  no heavy SDK — so the whole feature is offline-testable with no network. It
  stays behind the `[ai]` extra and never runs implicitly. `describe`,
  `SceneDescription`, `parse_description`, `DescribeError` and `AI_PROVENANCE` are
  exported at the top level.
- **Semantic task-name aliasing: the embedding layer of natural-language search
  (`docs/AI_INTEGRATION_IDEAS.md` C1 — the last open C1 piece, completing Phase
  3's natural-language-search line).** `--fuzzy` matches by the *words* in a task
  label; some queries share no word with the label they mean (Umbra's
  North-Dakota grain-storage site is named *"Beet Piler - ND"*), which only a
  model that has read about the world can bridge. The new `umbra_py.semantic`
  module (`[ai]` extra) embeds the catalog index's task names once and ranks them
  by meaning: `umbra semantic build` stores one vector per distinct task name in
  a small SQLite file beside `catalog.db` (schema-versioned with `PRAGMA
  user_version`; idempotent — a rebuild only embeds new names), and `umbra
  semantic search "grain storage north dakota"` embeds the query, ranks the
  stored vectors by cosine similarity, and prints the closest task names plus the
  exact `umbra search --area …` command for the best match (`--run` executes it,
  `--json` emits the ranking, `--top-k` / `--min-score` tune it) — the same
  "model proposes, library executes, user audits" boundary as `umbra ask`. The
  **only** model call is turning text into a vector: an injectable `Embedder`
  callable (default: an OpenAI-compatible `/embeddings` endpoint via the
  already-core `requests`, `OPENAI_API_KEY` / `OPENAI_BASE_URL` /
  `UMBRA_EMBED_MODEL`, no heavy SDK). Storage, cosine ranking and thresholding
  are stdlib-only — no `numpy`, no `sqlite-vec` binary dependency — so the whole
  feature is offline-testable with a deterministic stand-in embedder. It stays
  behind the `[ai]` extra and never runs implicitly; the deterministic `--area` /
  `--fuzzy` matchers remain the default search path. `SemanticTaskIndex`,
  `SemanticMatch`, `SemanticError`, `cosine_similarity` and `default_embedder`
  are exported at the top level.
- **`umbra ask "…"`: model-planned, deterministically executed natural-language
  search (`docs/AI_INTEGRATION_IDEAS.md` C1 — the capstone of the
  natural-language-search direction, and the first feature that calls a model).**
  A configured model reads the user's sentence plus the `llm_context()` domain
  document and returns the search *parameters* it maps to; the new
  `umbra_py.planner` module then re-validates every one of them deterministically
  (`parse_plan`) — dates through `parse_date_bound`, product types against
  `PRODUCT_ASSETS`, the bounding box range-checked, `place`/`bbox` enforced
  mutually exclusive — and prints the exact `umbra search` command it resolves
  to. **Nothing the model emits becomes a filter without passing that
  deterministic layer**, and the command is shown before it runs: the LLM plans,
  the library executes, the user audits. By default `umbra ask` only prints the
  plan; `--run` executes it (against a live walk or `--local` index), `--json`
  emits the resolved plan, and `--limit` overrides the model's cap. The feature
  lives behind a new `[ai]` extra and **never runs implicitly** — only `umbra
  ask` reaches a model, and only with a user-supplied key: `ANTHROPIC_API_KEY`,
  or `OPENAI_API_KEY` (with optional `OPENAI_BASE_URL` for any OpenAI-compatible
  endpoint), with `UMBRA_ASK_MODEL` / `--model` to pick the model. The provider
  call uses only the already-core `requests` (no heavy SDK). The planning step is
  an injectable callable (`ask(question, planner=…)`), so the whole feature —
  prompt building, plan validation, command rendering, provider selection, and
  the CLI — is fully offline-testable with no network. `ask`, `parse_plan`,
  `SearchPlan` and `AskError` are exported at the top level. Semantic task
  aliasing (`"grain storage north dakota"` → `"Beet Piler - ND"`) is the
  persistent, offline embedding-index answer to the same aliasing — see the
  `umbra semantic` entry above, which closes out C1.
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

[Unreleased]: https://github.com/reesehammer/umbra-py/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/reesehammer/umbra-py/releases/tag/v0.1.0
