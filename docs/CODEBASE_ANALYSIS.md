# umbra-py — Codebase Analysis & Prioritized Recommendations

*Analysis date: 2026-07-02 · Codebase at commit `a89b5e9` (v0.1.0, ~5,000 lines of
source, ~3,600 lines of tests). All claims below were verified against the code,
the live Umbra bucket, PyPI, and a clean local run of the project's own checks
(`ruff check`, `ruff format --check`, `pytest -q` — all green).*

---

## 1. Executive summary

`umbra-py` is an unusually healthy early-alpha codebase. The architecture is
coherent (a thin, dependency-light core with heavy geospatial deps isolated
behind extras and lazy imports), the documentation culture is exceptional for a
v0.1 (module docstrings explain *why*, `AGENTS.md` is a model agent-onboarding
file, `TODO.md` is an honest ledger of known debt), and the offline test suite
is fast and well-patterned.

The findings that matter most are not style issues — they are:

1. **A confirmed, live correctness bug** *(✅ fixed in PR #29)*: the S3 listing
   code sent a ListObjects **V2** continuation parameter against what was
   effectively a **V1** API call (it never sent `list-type=2`), so any task
   directory with more than 1,000 keys was **silently truncated**. This was
   reproduced against the live bucket during this analysis (`Centerfield, Utah`
   returns `IsTruncated=true` with 1,000 keys and no continuation token the old
   code could read). Both listers now send `list-type=2`; search results are
   complete again.
2. **The package is not on PyPI** even though the README's first instruction is
   `pip install umbra-py` (verified: PyPI returns 404 for `umbra-py`). For an
   open-source adoption strategy this is the single highest-leverage gap — and
   an unclaimed name is a squatting risk.
3. **CI never exercises ~a third of the test suite**: 72 of 212 tests skip
   because the `viz`/`load` extras are never installed in CI, so the largest
   module (`viz.py`, 1,827 lines) ships effectively untested by CI.

Everything else is incremental hardening: download integrity, HTML-output
escaping, retry/backoff, repo metadata hygiene, and open-source scaffolding
(releases, security policy, docs site).

---

## 2. Code quality assessment

### 2.1 Strengths (worth preserving deliberately)

- **Layering is clean and honest.** `catalog.py` (discovery) → `models.py`
  (representation) → `download.py`/`load.py` (retrieval) → `viz.py`
  (presentation), with `cli.py` as a thin-ish shell. The CLI subcommands map
  1:1 to library functions, as `AGENTS.md` promises.
- **The lazy-import discipline is real, not aspirational.** `rasterio`,
  `numpy`, `matplotlib`, `folium`, `sarpy`, `xarray` are all imported inside
  the functions that need them via the `_require()` pattern; the core install
  is genuinely `requests` + `click` only. This was verified by the passing
  core-only test run.
- **Domain correctness is documented where it bites.** Docstrings explain dB
  vs. linear stretch, slant vs. ground plane, polarization-mixing warnings in
  change products, and why co-registration must precede comparison. This is
  rare and valuable in a SAR library.
- **Test patterns are sound.** HTTP is mocked with `responses`; catalog walks
  are tested against an in-memory fake tree; network tests are marked and
  excluded by default; pruning behavior (the key performance property) has a
  dedicated regression test (`test_search_prunes_out_of_range_acquisitions`).
- **`AGENTS.md` + `TODO.md` + Keep-a-Changelog discipline** make the repo
  highly navigable for both humans and coding agents.

### 2.2 Weaknesses

- **`cli.py` is a 1,309-line monolith and business logic is leaking into it.**
  The "search mode vs. URL mode" resolution, frame selection, polarization
  warnings, and output-extension dispatch in `change`, `timescan`, `swipe`,
  `gallery`, and `map` are duplicated across subcommands (the
  `item_urls XOR search flags` validation block appears four times, nearly
  verbatim). This contradicts the project's own rule ("don't put business
  logic in the CLI") and makes each new command more expensive. Extract a
  shared `_gather_items(urls, area, bbox, place, start, end, ...)` helper and a
  shared set of reusable click option groups (click supports composing options
  via decorators).
- **`viz.py` is 1,827 lines and carries five distinct concerns**: GeoJSON
  export, Folium maps, raster reading/stretching, change/timescan compositing,
  and gallery/animation rendering. It is still readable, but it is the next
  module that will become `cli.py`. A `viz/` package split
  (`geojson.py`, `maps.py`, `raster.py`, `composites.py`, `gallery.py`) with
  re-exports preserved in `viz/__init__.py` would be behavior-neutral.
- **Manual context-manager plumbing in `cli.search`**: the spinner is driven
  with `spinner.__enter__()` and paired `finally: spinner.stop()` instead of a
  `with` block (cli.py:170-185). It works, but it's the kind of code that
  breaks silently under refactoring.
- ~~**Version is duplicated** in `pyproject.toml` and
  `src/umbra_py/__init__.py`.~~ ✅ **Fixed:** `pyproject.toml` now uses
  hatchling's dynamic version (`[tool.hatch.version] path =
  "src/umbra_py/__init__.py"`), so `__version__` is the single source and the
  two cannot drift.
- ~~**No `py.typed` marker.**~~ ✅ **Fixed:** `src/umbra_py/py.typed` now ships
  in the wheel and sdist, so downstream type checkers consume the library's
  inline types.
- **No type-checking in CI.** ruff catches lint, but nothing runs mypy or
  pyright. The `# type: ignore[arg-type]` in `index.build` (index.py:198) and
  the loosely-typed `**kwargs` pass-throughs in `download.py`/`viz.py` save
  functions would benefit from a checker keeping them honest.
- ~~**Known dead-code bug, already ledgered**: `_classify_asset`'s
  `"tif" in name` check can never match an uppercased string.~~ ✅ **Fixed**
  (P1 #8): the check now matches `"TIF"` against the upper-cased `name`, so an
  item declaring a plain `image/tiff` media type no longer silently loses its
  GEC classification. Regression test in `tests/test_models.py`; the `TODO.md`
  entry is deleted.
- **`ItemCollection` subclasses `list` but its constructor options are lost on
  slicing/`+`** (a slice returns a plain `list`, dropping `thumbnails`
  state). Minor, but worth a docstring note or a `__getitem__` override before
  users depend on it.

### 2.3 Test-coverage observations

- Offline suite: 140 passed, 5 network-deselected, **72 skipped** in a
  core-only environment. The skips are the entire `viz`/`load`/`lazy_imagery`
  /`html` surface — i.e. the majority of feature code added since v0.1's core.
- CI (`.github/workflows/ci.yml`) installs only `.[dev]`, so **CI runs exactly
  this reduced suite**. The 1,600-line `test_viz.py` has never gated a merge.
- `pytest-cov` is declared as a dev dependency but coverage is never measured
  in CI, so there is no signal when coverage regresses.

---

## 3. Security review

Context matters: this library reads from a public bucket with anonymous HTTPS,
writes local files, and generates static HTML. There is no auth surface, no
server, and no secret handling — the attack surface is (a) remote content it
parses, (b) files it writes, and (c) HTML/JS it emits. Findings in rough order
of severity:

### 3.1 Unescaped remote metadata is interpolated into generated HTML (moderate) — **fixed**

> **Status:** ✅ Fixed. A shared, dependency-free `_html.safe_href()` (scheme
> allowlist + attribute-escaping) is now the single gate for every clickable
> link built from a remote href, and every remote-derived string is
> `html.escape()`d before it reaches generated HTML. `viz._popup_html` now
> escapes `id`, `datetime`, `platform`, `instrument_mode`, `product_type`,
> `polarizations` and `available_assets`, and routes `item.href` through
> `safe_href` (so a `javascript:`/`data:` scheme or an attribute-breakout value
> drops the link instead of emitting it). The same discipline was extended to
> the other surfaces that interpolate remote metadata: `viewer._viewer_html`
> (the `umbra view` single-scene page — escapes the panel/title metadata and
> validates the STAC link), and `_html.py`'s card/gallery links and `demo.py`'s
> client-side STAC link (scheme-guarded at build time, since it is assigned to
> an anchor's `href` DOM property). `_lazy_imagery.popup_button_html` already
> escaped its `item_id`/`asset_url`, so it was unchanged. Regression tests cover
> the escaping and the `javascript:` scheme rejection across `viz`, `_html`,
> `viewer`, and `demo`. Remaining follow-on (unrelated to this class): SRI hashes
> on the third-party CDN `<script>` tags (§3.4). Original finding retained below.

`viz._popup_html` escapes `location` and `description` but interpolates
`item.id`, `platform`, `product_type`, `instrument_mode`, and `item.href` into
popup HTML **without escaping** (viz.py:159-208 — `rows` values pass through
`fmt()` which does no escaping, and `item.href` is placed raw inside
`<a href='{...}'>`). These values originate from STAC JSON fetched from the
bucket. The trust chain today is "Umbra's bucket," but the same code paths
accept **arbitrary item URLs** (`umbra info <url>`, `umbra map`, `swipe`,
`change` all take user-supplied STAC URLs), so a malicious STAC document can
inject script into an HTML artifact the user then opens locally (a `file://`
origin, where exfiltration constraints are weak). `_html.py` gets this right
(everything funnels through `escape()`); `viz.py` should match it.
**Fix:** `html.escape()` every remote-derived string in `_popup_html` and
validate `item.href` scheme (`http(s)`) before emitting it as a link.

### 3.2 Download integrity is not verified (moderate) — **fixed**

> **Status:** ✅ Fixed. `download_url` now compares the received byte count to
> `Content-Length` before renaming the `.part` (raising `DownloadError` on a
> clean short read, and converting a mid-stream `RequestException` into one too,
> leaving the `.part` for resume), and sends `If-Range` with a stored ETag on
> resume so a changed remote object restarts cleanly instead of splicing. ✅
> **Content verification now ships too** (the former follow-on): when the server
> exposes a single-part S3 `ETag` (the object's hex MD5) and `verify=True` (the
> default), the finished file is streamed through MD5 and compared, so
> on-the-wire corruption a correct length can't catch fails loudly with a
> `Checksum mismatch` — and the full-but-corrupt `.part` is discarded so a retry
> re-downloads cleanly. Multipart ETags (`"<hash>-<n>"`) aren't a plain MD5 and
> are skipped; `verify=False` opts out. Offline-tested in
> `tests/test_download.py` with a known body + its MD5. Original finding retained
> below.

`download_url` (download.py:24-79):

- After streaming, the `.part` file is renamed to the final name **without
  checking received bytes against `Content-Length`**. A dropped connection
  mid-body raises inside `iter_content` in the common case, but a proxy or
  server that closes cleanly early yields a silently truncated "complete"
  file. For multi-GB SAR products this is painful and hard to diagnose.
- Resume uses a `Range` header **without `If-Range`/ETag validation**. If the
  remote object changed between attempts, the resumed file is a corrupt splice
  of two different objects. S3 provides ETags; send `If-Range` with the stored
  ETag (persist it next to the `.part`), and fall back to a restart on
  mismatch.
- ~~No checksum verification. S3 exposes ETag (MD5 for single-part uploads) via
  HEAD; verifying when available is cheap insurance.~~ ✅ **Fixed** — the
  finished file is hashed and compared against a single-part ETag's MD5 when the
  server exposes one (`verify=True` default; multipart ETags skipped).

### 3.3 XML parsing with stdlib `ElementTree` (low, defense-in-depth)

`catalog._list_prefix`/`_stream_keys` parse bucket-listing XML with
`xml.etree.ElementTree.fromstring` (catalog.py:129, 163). Python 3.8+ disables
external entity resolution by default, so classic XXE doesn't apply, but
entity-expansion (billion-laughs) style inputs are still a hazard if `bucket`
is ever pointed at a hostile endpoint (the constructor accepts arbitrary
`bucket`/`region`). Using `defusedxml.ElementTree` is a one-line hardening
with no dependency weight concerns (it's pure Python), or document the
trust assumption explicitly.

### 3.4 Third-party CDN scripts without Subresource Integrity (low-moderate)

Generated lazy-imagery maps load `geotiff.js` from unpkg pinned to a version
(`_lazy_imagery.py:50`) — good — but **without an SRI hash**, and Folium's own
CDN assets (leaflet, jquery, bootstrap) also ship hashless. A compromised CDN
or package release could execute script in every map a user has generated.
Pinning + `integrity=`/`crossorigin=` attributes on the injected `<script>`
tag closes this for the code the project controls.

### 3.5 No security policy or dependency monitoring (process gap) — **partly addressed**

- ~~No `SECURITY.md` / disclosure channel.~~ ✅ **Fixed:** `SECURITY.md` now
  documents private vulnerability reporting via GitHub Security Advisories, the
  supported-version policy, and the library's security posture (anonymous HTTPS,
  no auth surface; remote content + generated HTML as the trust boundary).
- ~~No Dependabot/Renovate config~~ ✅ **Fixed** (`.github/dependabot.yml`
  already ships grouped Actions + pip updates).
- No `pip-audit` (or similar) step in CI — still open.

### 3.6 Non-findings worth recording

- SQL in `index.py` is fully parameterized, and `LIKE` wildcards are escaped
  (`_escape_like`) — done correctly.
- `_filename_from_url` cannot path-traverse (it takes the final `/`-segment
  and never URL-decodes), so a hostile URL can't escape `dest_dir`.
- Nominatim usage honors the 1 req/s policy with a descriptive User-Agent, and
  geocoding is opt-in on library paths (no surprise network calls).
- No credentials anywhere by design ("anonymous HTTPS only") — this is a
  genuine security *feature* of the architecture.

---

## 4. Scalability & robustness

### 4.1 **Confirmed bug: S3 pagination silently truncates large tasks** (critical) — **fixed** (PR #29)

> **Status:** ✅ Fixed. Both `_list_prefix` and `_stream_keys` now send
> `&list-type=2`, so ListObjectsV2 pagination works as the code expects.
> Offline regression tests drive both listers across two truncated pages, and a
> `network`-marked test confirms a >1,000-key task (*Centerfield, Utah*) now
> streams past its first page against the live bucket. The description below is
> retained as the record of the original defect.

`_list_prefix` and `_stream_keys` build URLs like
`https://s3.<region>.amazonaws.com/<bucket>/?prefix=...&continuation-token=...`
(catalog.py:121, 155) — but **never send `list-type=2`**. Without that
parameter S3 serves the **ListObjects V1** API, which ignores
`continuation-token` and responds with `<Marker>`/`<NextMarker>` semantics.
The code then looks for `<NextContinuationToken>`, finds none, and breaks out
of the pagination loop after the first 1,000 keys.

Verified live during this analysis:

```
GET /?prefix=sar-data/tasks/Centerfield,%20Utah/
→ 1000 <Key> elements, <IsTruncated>true</IsTruncated>, no NextContinuationToken
→ (response is V1: contains <Marker>, not <KeyCount>)
```

Consequences today:

- Any task with >1,000 objects (Centerfield, Utah already is one) has
  acquisitions **silently missing from every search, index build, gallery,
  timescan, and change detection**.
- The top-level `sar-data/tasks/` listing currently fits in one page, but the
  moment Umbra publishes its 1,001st task, **whole tasks disappear** from
  discovery with no error.

**Fix:** append `&list-type=2` to both listing URLs (then
`NextContinuationToken` works exactly as the code expects). Add an offline
regression test that serves two fake truncated pages and asserts both are
consumed, and a `network`-marked test asserting a >1,000-key task yields more
than 1,000 keys. This is a two-line production fix.

### 4.2 The N+1 sidecar fetch is serial (high impact on UX) — **fixed**

> **Status:** ✅ Fixed. `_walk_task` now collects each task's in-range
> acquisitions in date order and resolves their `*.stac.v2.json` sidecars
> through a bounded `ThreadPoolExecutor` (`_SIDECAR_WORKERS = 8`, mirroring the
> gallery pool) in `UmbraCatalog._items_from_sidecars`, yielding strictly in the
> sorted order — so live-search wall time drops from N serial fetches toward
> N/workers with the deterministic ordering preserved. Fetches run in windows so
> an early `limit`/`max_per_task` wastes at most one window, and the shared
> session's connection pool was sized up (`pool_maxsize=16`) to hold the
> fan-out. Regression tests cover order-under-out-of-order-completion, genuine
> parallelism, the bounded over-fetch, and the single-acquisition fast path.
> Original finding retained below.

`_walk_task` fetches one `*.stac.v2.json` per in-range acquisition,
sequentially (catalog.py:283-291). A 50-item search pays ~50 round trips at
full latency each. The gallery module already demonstrates the fix in-repo
(`_render_gallery_thumbnails` uses a small `ThreadPoolExecutor`); applying the
same bounded pool (6–8 workers) to sidecar GETs would cut live-search wall
time by roughly the worker count — with the caveat that `search()` is a
generator and per-task batching keeps ordering deterministic.

### 4.3 No retries or backoff anywhere on the HTTP path — **fixed**

> **Status:** ✅ Fixed. `_http.default_session()` now mounts an `HTTPAdapter`
> with `Retry(total=3, backoff_factor=0.5, status_forcelist=(429, 500, 502, 503,
> 504))` on idempotent `GET`/`HEAD` requests for both `http://` and `https://`.
> Because everything routes through `default_session()`, every caller — catalog
> walk, sidecar fetch, geocode, download — inherits the retry/backoff at once.
> Original finding retained below.

`_http.default_session()` mounts no `HTTPAdapter` with retries; one transient
S3 503/connection-reset fails an entire search, index build, or download.
`urllib3.util.retry.Retry(total=3, backoff_factor=0.5,
status_forcelist=(429, 500, 502, 503, 504))` on the shared session is the
standard, low-risk fix and lands in one place because everything already goes
through `default_session()` (a deliberate and correct design choice).

### 4.4 Whole-bucket operations need the index to be the default path

The live walk lists every task to answer unscoped searches; the README rightly
warns a full index build "takes a while." Two structural improvements:

- **Publish a prebuilt index.** The `index.py` docstring already names this
  ("walk once, ship the `.db`"). A scheduled GitHub Action that runs
  `umbra index build` nightly and attaches `catalog.db` (a few MB compressed)
  to a rolling release turns every user's first search from minutes to
  milliseconds — and is the substrate for the STAC-API/MCP ideas in the
  companion document.
- **Incremental refresh — ✅ shipped** (`umbra index update` /
  `CatalogIndex.update`). Keeping a snapshot fresh no longer means re-crawling
  the whole bucket: `update` reads the newest indexed `acq_date` and re-walks
  only from there (minus a small `--overlap-days` window for publish lag), so the
  walk prunes older acquisitions' sidecar fetches — a weekly refresh reads just
  the new passes and upserts them. This is the "only walk prefixes newer than the
  index's max `acq_date`" half of the idea below, delivered as an explicit
  command; the bound is on acquisition date (not publish date), so completeness
  over back-dated late arrivals still wants a widened window or a full `build`.
- **Read-through caching — ✅ shipped** (`CatalogIndex.search_live` / `umbra
  search --local --live`). The remaining half is now delivered: `search_live`
  answers the whole query from the index *and* walks only acquisitions at or
  after the index's freshness horizon (its newest `acq_date` minus
  `overlap_days`), merging the two streams in the usual `(task, acq_date)` order
  and de-duplicating by sidecar href — so a repeat search stays near-instant but
  also catches anything published since the index was built. With `refresh=True`
  (the default) each new acquisition the delta discovers is upserted as it is
  yielded, so the cache warms and the next call walks even less; a read-only
  index disables the write-back rather than failing. It reuses the same
  recent-only sidecar pruning `umbra index update` already relies on, and is
  offline-tested with an injected catalog. This is the "fold the update into
  search transparently" step (P3 #21), delivered as an explicit read-through
  method + `--live` flag rather than an implicit mode change to `search`, so the
  fast path can also be fresh without changing what a plain `search` means.

### 4.5 SQLite index details

- `CatalogIndex` uses a single connection with default settings: no WAL mode,
  no `check_same_thread=False` handling, no busy timeout. Fine single-process;
  document that, or set `PRAGMA journal_mode=WAL` for concurrent readers
  (which the prebuilt-index use case will invite).
- ~~**No schema version marker.**~~ ✅ **Fixed.** `CatalogIndex` now stamps
  `PRAGMA user_version = 1` on create and checks it on open (`index.py`,
  `_SCHEMA_VERSION` + `_init_schema`). A fresh or pre-versioning database
  (`user_version 0`, which every deployed `catalog.db` and fetched snapshot
  currently reads) is adopted in place and stamped; a database written by a
  *newer* umbra-py — or a lower versioned schema with no migration — raises
  `IndexSchemaError` (surfaced by the CLI as a clean `error: …`) instead of
  being silently misread. This is what makes the next schema change (the
  demo-oriented denormalizations in `DEMO_APP_GAPS.md` G2, an R\*Tree upgrade)
  a migration rather than a confusing break — landed while every deployed DB
  still shares one layout, as this recommendation urged. Two additive migrations
  have since exercised the path: `user_version` 1 → 2 added the baked `place`
  column (`bake_places`, G2) and 2 → 3 added the baked `thumbnail` BLOB
  (`bake_thumbnails`, G6), each applied in place by adding the nullable column so
  an existing or fetched snapshot gains it on the next open.
- bbox queries do a full table scan with range predicates. At the current
  catalog scale (thousands of items) this is irrelevant; if the index grows to
  hundreds of thousands, SQLite's built-in R*Tree module is the natural
  upgrade and the schema-version marker makes that migration possible.
- ✅ **Keyed single-item lookup shipped.** `CatalogIndex.get(item_id)` returns
  one item by STAC id via a new `idx_items_id` index (the retrieval complement
  to `search`'s listing), and `umbra serve`'s `/collections/{id}/items/{item_id}`
  resolves through it (`serve.get_one`) instead of scanning an id-filtered
  search. The index was added additively (`CREATE INDEX IF NOT EXISTS` in
  `_SCHEMA`, no `user_version` bump), so existing/fetched snapshots gain it on
  the next open — the first exercise of the additive-schema path the version
  marker above was landed to enable.

### 4.6 Miscellaneous robustness

- `geocode` module-level state (`_GEOCODE_CACHE` unbounded, `_LAST_GEOCODE_AT`
  global, not thread-safe) is acceptable for CLI usage; note it or guard it if
  geocoding ever moves onto the gallery's thread pool.
- `download_url` returns early if `dest` exists (`overwrite=False`) without
  any completeness check — combined with §3.2, a truncated previous download
  is never repaired. Verifying size against a HEAD request would fix both.

---

## 5. Open-source strategy review

### 5.1 Where the project already leads

- Apache-2.0 code / CC-BY-4.0 data distinction is explained clearly, and the
  attribution string is propagated into `xarray` attrs and GeoTIFF tags —
  license hygiene most geo libraries get wrong.
- CONTRIBUTING.md, issue templates, PR template, pre-commit config, and a
  three-version CI matrix all exist at v0.1. The "good first issues" section
  names genuinely approachable areas.
- `AGENTS.md` as the canonical agent guide (with CLAUDE.md pointing to it) is
  ahead of most of the ecosystem.

### 5.2 Gaps, in order of strategic cost

1. **Not on PyPI (verified 404).** The README's install command fails for
   every prospective user — the single biggest adoption blocker, and the name
   is claimable by anyone. Even a `0.1.0a1` pre-release claims the name,
   enables `pip install`, and unlocks conda-forge later. ⏳ **The
   `release.yml` using PyPI Trusted Publishing (OIDC — no long-lived token
   secret) triggered on GitHub Releases now exists**; the remaining step is a
   maintainer action — register the PyPI Trusted Publisher for
   `reesehammer/umbra-py` and cut the `v0.1.0` GitHub Release, which fires the
   workflow and claims the name.
2. ~~**Repository metadata points to the wrong org.**~~ ✅ **Fixed:** the
   `pyproject.toml` project URLs, `CHANGELOG` compare/tag links, and
   `CONTRIBUTING` clone command now all point at the canonical
   `reesehammer/umbra-py`.
3. **No releases, no tags.** The changelog is all "Unreleased" and there are
   no git tags, so there is no way to pin, bisect, or communicate stability.
   Cutting `v0.1.0` (matching the existing version string) costs minutes.
4. **CI doesn't test what users use** (§2.3): add a second CI job installing
   `.[all,dev]` so the viz/load suite actually runs; add Python 3.13 to the
   matrix (3.13 has been stable for ~20 months); wire `pytest --cov` +
   Codecov and put the badge in the README.
5. ~~**Missing community/security scaffolding:**~~ ✅ **Largely done.**
   `SECURITY.md` (§3.5), `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1), and
   `CITATION.cff` now ship, and Dependabot config was already present — so
   GitHub's community profile is complete and research users can cite the tool.
   A `pip-audit` CI step is the one remaining item under this heading.
6. **No rendered documentation site.** The README is excellent but is now 328
   lines doing the job of a docs site. mkdocs-material + mkdocstrings can
   generate API docs from the existing (high-quality) docstrings nearly for
   free, published via GitHub Pages. This also creates the anchor for the
   `llms.txt` idea in the companion document.
7. **Ecosystem listing:** once on PyPI, register in the STAC ecosystem tools
   list, the AWS Open Data registry's usage-examples section for the Umbra
   dataset, and pyOpenSci — each is a durable discovery channel for exactly
   the audience this library serves.

---

## 6. Recommendations, ordered by priority

**P0 — correctness & existence (do first, all are small)**

| # | Recommendation | Where | Effort |
|---|---|---|---|
| 1 | ✅ **Done (PR #29).** Added `list-type=2` to both S3 listing URLs; added truncated-pagination regression tests (offline two-page fakes + `network` test) | `catalog.py:121,155` | ~2 lines + tests |
| 2 | ⏳ **Release plumbing done (this PR); the publish itself is a maintainer action.** Added `.github/workflows/release.yml` — Trusted-Publishing (OIDC) release workflow that builds sdist+wheel, `twine check`s them, and refuses to publish if the `vX.Y.Z` tag disagrees with the version. Remaining: a maintainer cuts the `v0.1.0` GitHub Release (which fires the workflow) after registering the PyPI Trusted Publisher | `pyproject.toml`, `.github/workflows/release.yml` | half a day |
| 3 | ✅ **Done (this PR).** Fixed `pyproject.toml` project URLs, `CHANGELOG` compare/tag links, and `CONTRIBUTING` clone command to the canonical `reesehammer` org | `pyproject.toml`, `CHANGELOG.md`, `CONTRIBUTING.md` | minutes |
| 4 | Add a CI job with `.[all,dev]` so the 72 skipped viz/load tests run; add Python 3.13 | `.github/workflows/ci.yml` | ~10 lines |

**P1 — user-facing robustness**

| # | Recommendation | Where | Effort |
|---|---|---|---|
| 5 | ✅ **Done.** `download_url` verifies received bytes against `Content-Length` (raising `DownloadError` on a short read and on a mid-stream break, keeping the `.part` for resume), sends `If-Range` + a stored ETag on resume so a changed object restarts cleanly instead of splicing, and (default `verify=True`) hashes the finished file against a single-part ETag's MD5 to catch on-the-wire corruption, discarding the corrupt `.part` on mismatch | `download.py` | small |
| 6 | ✅ **Done.** `default_session()` mounts an `HTTPAdapter` with `Retry(total=3, backoff_factor=0.5, status_forcelist=(429,500,502,503,504))` on `GET`/`HEAD`; every caller inherits it | `_http.py` | ~5 lines |
| 7 | Escape all remote-derived strings in `viz._popup_html`; validate href scheme | `viz.py:159-208` | small |
| 8 | ✅ **Done.** `_classify_asset` now matches `"TIF"` against the already-upper-cased `name` (the lowercase `"tif"` was dead code), so a GeoTIFF that declares a plain `image/tiff` media type is classified as GEC instead of being dropped; added a regression test and deleted the TODO entry | `models.py`, `tests/test_models.py`, `TODO.md` | ~5 lines |
| 9 | ✅ **Done.** `_walk_task` collects in-range acquisitions in date order and fetches their sidecars through a bounded `ThreadPoolExecutor` (`_items_from_sidecars`, `_SIDECAR_WORKERS=8`), yielding in sorted order; windowed so `limit` caps over-fetch, session `pool_maxsize` raised to 16 | `catalog.py:_walk_task`, `_http.py` | medium |
| 10 | ✅ **Done.** `CatalogIndex` stamps `PRAGMA user_version = 1` on create and checks it on open (`_SCHEMA_VERSION` + `_init_schema`): a fresh/pre-versioning DB is adopted and stamped, a newer or un-migratable version raises `IndexSchemaError`. Landed while every deployed DB still shares one layout, so the next schema change is a migration, not a break | `index.py`, `exceptions.py` | small |

**P2 — hardening & hygiene**

| # | Recommendation | Where | Effort |
|---|---|---|---|
| 11 | ⏳ **`py.typed` shipped (this PR)** — the marker is now in the wheel + sdist, so downstream type checkers consume the inline types; adding mypy/pyright to CI is still open | package + CI | small |
| 12 | Add SRI hashes to the injected geotiff.js `<script>` tag | `_lazy_imagery.py` | small |
| 13 | Parse listing XML with `defusedxml` (or document the trust boundary) | `catalog.py` | small |
| 14 | ⏳ **Mostly done.** `SECURITY.md`, `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1) now ship; Dependabot config was already present. Remaining: a `pip-audit` CI step | `.github/`, repo root | small |
| 15 | ✅ **Done (this PR).** Version single-sourced from `umbra_py.__version__` via hatchling's dynamic version, so `pyproject.toml` and `__init__.py` can no longer drift | `pyproject.toml`, `__init__.py` | small |
| 16 | Wire `pytest --cov` + Codecov badge into CI | CI | small |
| 17 | Publish a nightly prebuilt `catalog.db` as a rolling release artifact (scheduled Action) | new workflow | medium |

**P3 — structural investments (schedule, don't rush)**

| # | Recommendation | Where | Effort |
|---|---|---|---|
| 18 | Extract shared search-vs-URLs gathering + common option groups from the five CLI commands that duplicate them | `cli.py` | medium |
| 19 | Split `viz.py` into a `viz/` package (geojson / maps / raster / composites / gallery) with re-exports preserved | `viz.py` | medium |
| 20 | Stand up mkdocs-material + mkdocstrings docs site on GitHub Pages | new `docs/` config | medium |
| 21 | ✅ **Done.** Incremental refresh (`umbra index update` / `CatalogIndex.update`) *and* the read-through consult now both ship: `CatalogIndex.search_live` / `umbra search --local --live` answer from the index and walk only acquisitions newer than its max `acq_date`, merging + de-duplicating the two streams and warming the cache with the delta (§4.4) | `index.py`/`cli.py` | larger; design first |
| 22 | ⏳ **`CITATION.cff` added (this PR)** — machine-readable citation metadata (CFF 1.2.0), version-synced to `__version__` by an offline test, so GitHub shows "Cite this repository". Remaining (maintainer actions): register with the STAC ecosystem list, AWS Open Data registry examples, pyOpenSci; mint a Zenodo DOI | repo root | small each |

---

## 7. Closing note

The project's stated philosophy — "a small, well-documented layer," surgical
changes, offline-first tests — is visible in the code, which is the best
predictor of long-term maintainability. The P0 list is deliberately tiny:
one two-line protocol fix, one publishing task, one metadata correction, and
one CI job. Landing those four converts this from a well-built repository
into a dependable, installable, discoverable open-source project.
