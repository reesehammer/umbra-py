# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Entries are user-facing: what a consumer can do now, or what broke. Design
rationale lives in the PR; pre-0.1.0 development history lives in git.

## [Unreleased]

### Security
- **`umbra index fetch` checks the published snapshot.** The weekly workflow
  uploads `catalog.sha256` and attests those digests. A fetch verifies
  `catalog.db` when the manifest is present, and warns and continues when an
  older release has none. Index URLs (`--url`, including `UMBRA_INDEX_URL`)
  must be https unless `--insecure-url` or `UMBRA_INSECURE_INDEX_URL=1`.
  A downloaded snapshot is opened read-only with SQLite's defensive settings;
  triggers and unexpected objects are rejected.
- **Downloads no longer trust the filename in an asset URL.** The basename is
  percent-decoded once; a leading dot, a control character, or a backslash is
  refused, and the file must stay inside the destination directory.
  `umbra download --max-bytes` caps a transfer. Local MCP
  `download_asset(confirm=true)` keeps `dest_dir`, and refuses a path outside
  `UMBRA_MCP_DOWNLOAD_ROOT` (default: the working directory).

### Changed
- **Citation metadata uses the Zenodo concept DOI**
  `10.5281/zenodo.23676715`. Cite all versions with that DOI; it resolves to
  the latest archived release.

## [0.1.4] — 2026-10-26

Hosted MCP is in the registry, and the six core tools keep their names
through 0.x. Republished collects stay indexed and can collapse to one
canonical copy. Baked previews move to `item_thumbnails` (index schema v8).

### Upgrading

The published `catalog.db` snapshot is index schema v8. v7 added
`items.canonical`. v8 moved baked previews into the `item_thumbnails`
table. umbra-py 0.1.3 reads only up to schema v6, so `umbra index fetch`
on 0.1.3 fails against current snapshots. Upgrade to 0.1.4.

0.1.4 migrates a local index from schema v0 through v7 to v8 in place.
The migration is one-way: it stamps schema v8, and 0.1.3 cannot read the
file afterward.

Relocating baked thumbnails in a large self-built index (more than 64 MiB
of inline previews) does not run during open. `umbra serve` finishes that
move on a background thread after it is listening. The copy commits eight
rows at a time and checkpoints the write-ahead log so freed pages are
reused. It still needs temporary extra disk for about one batch, not a
second copy of the whole preview payload.

### Added
- **The MCP registry lists the hosted server.** `server.json` keeps the stdio
  PyPI package and adds a `streamable-http` remote at
  `https://api.umbra-py.space/mcp`. The host needs no auth, so the remote
  has no headers. Registry clients that only keep remotes can discover it.
- **MCP stable core.** `search_catalog`, `get_item`, `quicklook`,
  `download_asset`, `geocode_place`, and `index_stats` keep their names and
  existing arguments through 0.x ([Stable tools](docs/mcp.md#stable-tools)).
  The server reports its package version, and read-only tools set
  `readOnlyHint`. `tools/list` is checked for those six names after the
  weekly index redeploy and on the 15-minute hosted health probe.
- **Republished captures stay in the index and are linked.** The same collect
  under `sar-data/task-data/` and `sar-data/tasks/<campaign>/` is one group
  when the datetimes are within 60 seconds. Every item gains
  `umbra-py:canonical`; grouped items also gain `umbra-py:canonical_id`,
  `umbra-py:duplicate_ids`, `umbra-py:duplicate_confidence`, and
  `rel: canonical` / `rel: related` links (sidecar URLs in the index and
  parquet; the API rewrites them to its item route). `GET`/`POST /search` and
  `search_catalog(canonical=True)` can keep the canonical copy. Item counts
  are unchanged; `unique_captures` counts canonical rows.
- **Optional agent plugin for the hosted umbra MCP.**
  `integrations/agent-plugin/umbra-sar/` is a portable plugin (MCP config plus
  a short skill) that searches, previews, and opens Umbra open SAR (CC BY 4.0)
  through `https://api.umbra-py.space/mcp`, limited to the core tools. Claude
  Code installs it with `/plugin marketplace add reesehammer/umbra-py`.

### Fixed
- **A catalog-read deadline is a 503 on Python 3.10.** `asyncio.wait_for`
  raises `asyncio.TimeoutError` there, a different class from the builtin
  `TimeoutError`. `/healthz` and search now answer 503 when that deadline
  fires, instead of a 500.
- **Stateless MCP no longer logs `Terminating session: None` on every
  request.** The line comes from the `mcp` Streamable HTTP transport, which
  tears down a session-less transport after each call. The line is kept only
  when that logger is at DEBUG; otherwise it is dropped, including when the
  handler level is NOTSET. A real session id, and errors on that logger, keep
  their level.
- **`/healthz` keeps a single cached readiness document.** A new snapshot or
  UTC day replaces the previous entry.
- **Hosted `/healthz` and `/search` no longer read every preview image.**
  Baked quicklooks now live in `item_thumbnails`, not in the `items` row ahead
  of `datetime_suspect`, `created`, `published`, and `canonical`. Opening an
  existing index moves them in place (no catalog rebuild). `/search` stops
  after `LIMIT` rows using an index on that sort, and `/healthz` reuses one
  result per snapshot instead of calling `COUNT(thumbnail)` on every probe.
  A catalog read that overruns its deadline, or that cannot start because
  other reads are still running, returns 503. `GET /livez` does no catalog
  I/O; Railway's deploy check uses it. If the event loop stays silent past
  a grace period, the process exits so the platform can restart it.
- **The external health probe waits 60s and names a timeout.** The GitHub
  Actions check of `https://api.umbra-py.space/healthz` no longer treats a
  ~25s cold read as "no status". A failure issue is assigned to and mentions
  @reesehammer, and a client timeout says it is a timeout.
- **CI image builds survive ECR Public pull limits.** The Docker workflow
  retries `public.ecr.aws/docker/library/python:3.12-slim` when the registry
  returns 429 `Data limit exceeded`. If those retries still fail, that job
  builds from `mirror.gcr.io/library/python:3.12-slim`. Dockerfiles unchanged;
  Railway builds `deploy/Dockerfile.mcp`, so production is unaffected.
- **`download_asset(confirm=true)` is refused on the public host.** When COG
  streaming is off (`umbra serve --public`), confirming a download no longer
  writes the file onto the server. The tool error includes the asset URL, its
  size in bytes, and tells you to download it locally or run
  `uvx --from 'umbra-py[mcp]' umbra-mcp` and call `download_asset` there.
  If that size cannot be read, the same refusal says the size is unknown.
  `confirm=false` still returns the size.

## [0.1.3] — 2026-10-15

The catalog index covers the open bucket (named tasks, UUID `task-data/`,
legacy collects, and `open-data/`) and the weekly snapshot validates what it
keeps. STAC search and the geoparquet export use asset schema v2. The public
host states its limits, probes `/healthz`, and serves baked previews. This
release was not archived. Zenodo's GitHub integration is enabled for 0.1.4,
which will be the first archived release. No concept DOI had been minted.

### Added
- **Cite umbra-py.** `.zenodo.json` is the metadata Zenodo copies from a
  GitHub release. The README and [Citing](docs/citing.md) give a BibTeX
  entry and a DOI badge using a placeholder DOI until
  that release mints a concept DOI, and they quote Umbra's required
  notice: `SAR-derived data © [YEAR] Umbra Lab, Inc.`
- **Replace your crawler.** [docs/replace-crawler.md](docs/replace-crawler.md)
  is three short programs — plain `GET /search`, `pystac-client`, and DuckDB
  over the weekly `umbra-open-data.parquet` — that list GEC hrefs for a bbox.
  A daily Replace-crawler canary runs those same files against
  https://api.umbra-py.space. The API already serves asset schema v2
  (`gec`, `sicd`, …, filename on `umbra-py:filename`). The DuckDB check waits
  until the published parquet carries that schema.
- **Hosted API limits and an external uptime probe.** [Hosted API limits](docs/hosted-limits.md)
  states the best-effort 99% monthly uptime (no SLA), the 120 requests per
  60-second sliding window, the 10,000 `limit` ceiling, the CORS policy, and
  the `catalog-index` release as the bulk or outage fallback. A GitHub Actions
  workflow probes `https://api.umbra-py.space/healthz` every 15 minutes (HTTP
  200, `degraded: false`, and latency) and appends each result to
  `healthz.jsonl` on the `monitoring` branch. Three consecutive failures open
  or update an issue. An uptime percentage is withheld until that file covers
  7 days.
- **Stale index alert.** `/healthz` sets `stale: true` when `built_at` is
  missing or more than 9 days old. The same probe opens or updates an issue.
  The STAC collection and the geoparquet file metadata carry
  `umbra-py:built_at` and `umbra-py:snapshot` next to `umbra-py:asset_schema`.
  The collection `updated` field is that build date at midnight UTC.
- **Coverage report states the 99% bar.** `coverage.json` / `coverage.md`
  already break indexed vs bucket acquisitions down by prefix and product, and
  list quarantined rows with reasons plus the duplicate count. The report now
  also records whether `pct_indexable` meets the 99% publication bar.
- **Look up an acquisition by filename or stem.** `umbra info`, `umbra download`,
  and the MCP `get_item` tool accept a GEC/SICD filename
  (`2025-05-17-17-43-46_UMBRA-08_GEC.tif`) or an acquisition stem
  (`2025-05-17-17-43-46_UMBRA-08`) in addition to a STAC JSON URL. The name is
  resolved through the local catalog index, matching the stem against asset
  hrefs under `sar-data/tasks/`, `sar-data/task-data/`, and `open-data/`. An
  unknown name is an error; more than one match lists the candidate STAC URLs.
- **`datetime_suspect` on index build.** A kept acquisition whose datetime is
  after `properties.created` (or after `built_at` when `created` is missing)
  is flagged and left out of `umbra index info`'s `end`, `rank_by=recency`,
  and other latest-pass selections. The datetime is not rewritten. `end` stays
  the newest plausible acquisition. `datetime_suspect_count` is how many rows
  are flagged, and `quarantined_future_count` is how many source rows the build
  removed for a datetime more than a day ahead (the upstream 2035–2036 items),
  so those drops stay visible.
- **Index `created` and `published` times.** Catalog index schema version 6
  adds nullable `items.created` (STAC `properties.created` when the sidecar
  has it; legacy `_METADATA.json` collects leave it empty) and
  `items.published` (the sidecar's S3 `LastModified`, taken from the
  `ListObjectsV2` page the crawl already fetches). `umbra serve` `/search`
  and item responses pass `created` through as `properties.created` and
  `published` as `properties["umbra-py:published"]`. The geoparquet export
  carries the same fields. `created` minus `datetime` is the lag until
  Umbra generated the STAC record, not delivery latency; `published` is when
  the object appeared in the public bucket. Open data is a curated,
  batch-published subset.
- **Index validation on `umbra index build`.** The weekly snapshot drops
  duplicate ids (latest `updated`, otherwise datetime, then the most assets)
  and quarantines items dated before 2018-01-01 or more than a day ahead,
  items with a missing or invalid footprint, and items with no public HTTPS
  asset href or any `prod-prod-processed` href. Those rows are listed in the
  coverage report and in `index-validation.json` on the `catalog-index`
  release. `umbra index validate` re-checks the index and parquet and exits
  non-zero if a violation remains.
- **One JSON log line per STAC `/search`.** `umbra serve` (plain, `--mcp`,
  and `--public`) logs each `GET` or `POST /search` to stdout: client IP,
  User-Agent, HTTP method, path, status, duration, and the `bbox`,
  `datetime`, `limit`, `collections`, and `sortby` query parameters when
  the client sent them. A `POST` also logs the JSON body's key names
  (`body_keys`) and never the values. The same bare-JSON logger as
  `POST /mcp` is now configured for every `umbra serve`, not only when
  MCP is mounted.
- **Legacy and `open-data/` collects in the open-bucket walk.**
  `umbra index build` (and the weekly snapshot) now indexes 2023 to 2024
  collects that ship only `<stem>_METADATA.json`, flat `tasks/ad hoc/<site>/`
  files, sidecars misplaced in a `<stem>.stac.v2/` folder, and the
  `open-data/` root. Legacy items get a stable minted id (uuid5 of
  `umbra:collect_id`), not an Umbra id; every walked item carries
  `umbra-py:products`, `umbra-py:cphd_sicd_pair` and `umbra-py:layout`.
- **`umbra index coverage`.** Measures what share of the bucket's
  acquisitions an index covers, by prefix, sidecar layout and product;
  `--json` / `--markdown` write the report and `--min-pct` exits non-zero
  below a threshold.
- **`umbra index fetch --if-changed`.** Refreshes a local index only when the
  published snapshot's ETag changed (one `HEAD` otherwise), validates the
  download, refuses one under half the current size unless `--force`, and
  swaps it in atomically; failures raise `IndexRefreshError`. `/healthz` adds
  `ids` (distinct item ids), `built_at` and `snapshot`.
- **`sar-data/task-data/` in the open-bucket walk.** `UmbraCatalog.search`
  (and therefore `umbra index build` / the weekly snapshot) lists UUID
  collects under `sar-data/task-data/` after the named `sar-data/tasks/`
  tree. Place / bbox search can now find CPHD that was only published in
  that tree. `umbra download <stac-url> --asset CPHD` reconstructs the
  public filename from the sidecar stem, so a STAC key whose processing
  timestamp differs from the on-disk `*_CPHD.cphd` still hits the object.
- **CPHD for GPU backprojection.**
  [`docs/guides/complex-downstream.md`](docs/guides/complex-downstream.md)
  adds a MatX `sarbp`-shaped recipe: `--product CPHD` by place/bbox,
  HEAD the size (often 10–30+ GiB), download, stop. umbra-py does not
  form the image. Limitations and the CPHD product blurb say so.
- **CPHD + SICD pair for image formers.**
  [`docs/guides/complex-downstream.md`](docs/guides/complex-downstream.md)
  shows a place or bbox search, a size check, and
  `umbra download <item> --asset CPHD --asset SICD` from one collect,
  then handing both files to a former such as FastSAR
  `read_cphd(..., sicd=)`. umbra-py finds and downloads the pair.
- **Complex SICD/CPHD handoff guide.** New
  [`docs/guides/complex-downstream.md`](docs/guides/complex-downstream.md)
  for phase-preserving downstream processors: open Umbra SICD is
  RGAZIM/PFA (not Capella RGZERO); fetch the index, search
  `--product SICD` / `CPHD`, size-check with HTTP `HEAD`, download, and
  stop. `umbra convert` writes amplitude only. Limitations and product-type
  blurbs say so.
- **ISR / research cookbook.** New `examples/09_isr_training_set.ipynb` walks
  CatalogIndex search (VV / incidence filters) → size-check → chips for an
  Umbra open-data training set, with a [Used in research](https://umbra-py.space/guides/research/)
  guide citing ProSR. README chips callout points at SR / ATR-style benchmarks.
- **Connect Claude (MCP) docs.** Copy-paste recipes for Claude Desktop and
  Claude Code against `https://api.umbra-py.space/mcp`, plus local `uvx`
  stdio. Claude Code needs `"type": "http"` (or `claude mcp add --transport
  http`); Desktop uses the `url` JSON. Linked from Home, README, and Deploy.
- **MCP reading kits, no server vision key.** On a host without
  `ANTHROPIC_API_KEY` / `OPENROUTER_API_KEY` / `OPENAI_API_KEY` (including
  the public community instance), `describe_scene` returns the quicklook PNG
  plus the packaged SAR-literacy prompt and JSON schema so the *client's*
  already-running model does the reading. `narrate_change` does the same
  with the change composite and the per-block dB grid, on a host that is
  allowed to stream COGs. A local stdio server with a user-supplied key
  still calls a vision model itself. The public host never holds a key.
- **`stamp_description` / `stamp_narration` MCP tools.** After a reading kit,
  these validate the JSON, stamp CC-BY and AI provenance, and (for a baked
  preview) append a deterministic size caveat. Unstamped prose is not
  pipeline output.
- **One JSON log line per `POST /mcp`.** `umbra serve --mcp` / `--public`
  logs the client IP, User-Agent, `mcp-protocol-version`, JSON-RPC method,
  status and duration to stdout, plus `clientInfo` on `initialize` and the
  tool name and argument key names (never values) on `tools/call`.

### Changed
- **Breaking: STAC asset schema v2.** `umbra serve` (`/search`,
  `/collections/{id}/items`) and `umbra index export` key assets as `gec`,
  `csi`, `sidd`, `csi_sidd`, `sicd`, `cphd`, `metadata`, `stac_metadata`, and
  `di_gif` instead of the product filename. A second file of the same product
  is `gec_2`, `gec_3`, … (filename order; the canonical `*_GEC.tif` /
  `*_CPHD.cphd` name keeps the bare key). The filename is `title` when the
  asset had none, and always `umbra-py:filename`. `umbra-py:products` stays
  the uppercase list (`GEC`, …) lined up with those keys. The collection and
  the parquet metadata record `umbra-py:asset_schema: 2`. Migrate by reading
  the stable key, or pass `GEC` / `gec` / the filename to `asset_href`; v1
  filename keys are not served alongside v2.
- **Container builds pull official Python from ECR Public.** `deploy/Dockerfile`
  and `deploy/Dockerfile.mcp` default to
  `public.ecr.aws/docker/library/python:3.12-slim` (the same official
  `python:3.12-slim` tag) so CI and Railway are not blocked by anonymous
  Docker Hub pulls. Override the registry with `--build-arg PYTHON_IMAGE=...`.
- **Published user manual lives in `docs/`.** `docs_src/` is gone; mkdocs
  `docs_dir` is `docs/`. Maintainer `STRATEGY.md` and `TODO.md` moved to
  `.github/`. JSON contracts stay at `docs/schemas/` (unchanged path, still
  packaged in the wheel).
- **Docker files live under `deploy/`.** `Dockerfile`, `Dockerfile.mcp`,
  `docker-entrypoint.sh`, and `docker-compose.yml` moved out of the repo root.
  `railway.toml` stays at the root (default Railway discovery) with
  `dockerfilePath = "deploy/Dockerfile.mcp"`. Image build context stays the
  repository root (`docker build -f deploy/Dockerfile … .` /
  `docker compose -f deploy/docker-compose.yml up`).
- **Published catalog thumbnails are 512 px.** The weekly `catalog.thumbs.db`
  bake (`umbra index bake-thumbnails --size 512`) is large enough to read a
  scene in an MCP client. `bake_thumbnails` upgrades a smaller recorded
  bake, so the bump from 128 px actually replaces the old sidecar. MCP
  captions still say when `max_size` was ignored and that a local
  `uvx --from 'umbra-py[mcp]' umbra-mcp` server can stream a higher-
  resolution GeoTIFF (typically 1024 px), or you can download the GEC
  href from `get_item`.
- **Baked MCP previews say when `max_size` was ignored.** A catalog
  thumbnail is no longer captioned as a quiet "baked" substitute: the
  caption leads with `BAKED PREVIEW`, names that `max_size` was ignored
  when the cache is smaller than requested, and includes a JSON `image`
  record (`source`, `width`, `height`, `requested_max_size`, `substituted`).
- **`get_item` / `quicklook` / `describe_scene` prefer the catalog index.**
  Re-fetching the live STAC sidecar made those tools disagree with
  `search_catalog` (different GEC filenames, extra product types, `s3://`
  hrefs). When the index has the href, every tool uses that snapshot.
- **Public MCP `quicklook` serves baked catalog previews.** `preview="auto"`
  (the default) reads `catalog.thumbs.db` so the community host can show a
  scene without proxying Umbra GeoTIFFs. Tools that must stream COGs
  (`change_composite`, `timescan`, `stack_stats`, `stack_provenance`,
  `pick_change_interval`, `narrate_change`, `find_similar`) refuse on
  `umbra serve --public` with a local-server hint. The image entrypoint
  fetches the published thumbnail sidecar on first boot next to the index.
- **MCP search prefers fuzzy / `find_repeat_sites` over `semantic=True`.**
  A query that shares words with a task name (`"beet piler"`) is a
  deterministic `fuzzy=True` match; `semantic=True` is only for
  descriptions that share no tokens, and says so when the embedding index
  or key is missing.
- **Document the public community STAC/MCP host.** Deploy, README, and the
  docs homepage now use `https://api.umbra-py.space/` instead of Railway
  placeholders. Self-host / operator Railway instructions are unchanged.
- **Changelog is a user-facing delta, not a development diary.** The 0.1.0
  section keeps the public-release summary; pre-release per-PR history is
  in git. New entries are 1–3 sentences naming the public surface.

### Fixed
- **Download selection accepts schema v2 keys.** `asset_href`,
  `download_asset` / `download_item`, `umbra download --asset`, and the MCP
  `download_asset` tool accept a v2 key (`gec`, `gec_2`, `csi_sidd`,
  `file_tif`), a product type in any case, or the original filename,
  including when the stored item still has filename or uppercase keys. A
  missing name lists those v2 keys. The MCP tool's `url` also accepts a
  filename or acquisition stem, resolved like `get_item`.
- **Coverage no longer counts intentional drops as gaps.** `umbra index coverage` and the weekly 99% gate omit acquisitions named as deduped duplicates or quarantined rows in `index-validation.json` (or the validation record stored on the index). `totals.pct_indexable` is indexed acquisitions over that expected set. A sidecar the report does not name still fails the gate.
- **Weekly thumbnail bakes accumulate, and the preview floor is the merged count.** The publish workflow restores a cached thumbnail seed, bakes up to 2400 new previews, and saves the seed for the next run. The guard compares previews whose id or href is in the current index with `.github/thumbnail-preview-floor.json` (3355), `/healthz`, and any published sidecar, and may publish a seed when the release has no sidecar. The server still will not replace a larger local `catalog.thumbs.db`.
- **`umbra index validate` no longer loads the whole catalog into memory.**
  The weekly publish was killed a couple of minutes into that step (runner
  shutdown, "The operation was canceled") because the check held every index
  row and the entire geoparquet table. Duplicate ids are counted with SQL
  and an `id`-only parquet read, and rows are checked a batch at a time.
  Quarantine rules and the `index-validation.json` / `.md` reports are unchanged.
- **Published thumbnails fit GitHub's 2 GiB release-asset limit.** The weekly
  sidecar passed that limit and `gh release upload --clobber` deleted the
  previous `catalog.thumbs.db` before GitHub rejected the replacement
  (`size must be less than 2147483648`), so the asset 404'd. The workflow now
  uploads `catalog.thumbs.manifest.json` and `catalog.thumbs.db.partNNN.<sha>`
  parts under 1.5 GiB, checks that each asset is on the release, and fails
  the job if that upload's assets are missing. `umbra index fetch-thumbnails`
  (and the container entrypoint) reads the manifest and reassembles
  `catalog.thumbs.db`. A release that still has only the single file keeps
  working.
- **A smaller thumbnail sidecar is not published.** The weekly bake warns
  when the `catalog-index` release has no sidecar to re-import, and the upload
  compares the new preview count with the manifest's `previews` field (or the
  legacy asset), the live `/healthz` `thumbnails.count`, and
  `.github/thumbnail-preview-floor.json`. A lower count skips the thumbnail
  upload and leaves the previous assets in place while the catalog index still
  publishes. `umbra index fetch-thumbnails --if-changed` refuses to replace a
  local `catalog.thumbs.db` with one that has fewer previews.
- **`POST /search` page links keep the request.** `next` and `prev` are
  `POST /search` with the original JSON body and `token` set once (replaced,
  not added again). The previous links were `GET /search?token=N` with no
  body, so the next page dropped `limit`, `collections`, and the other
  filters and used the default limit of 10. Search order is task, acquisition
  date, datetime, then id, so two passes that share a timestamp do not swap
  at a page boundary.
- **GET search page links keep one `token` and the rest of the query.**
  `next` and `prev` on `GET /search` and `GET /collections/{collection}/items`
  replace any `token` already in the query instead of appending another, and
  keep `bbox`, `datetime`, `collections`, `limit`, `ids`, `intersects`, and
  the other parameters. A `prev` link is included when the page is not the
  first.
- **`GET /collections` no longer scans baked thumbnails for its time span.**
  The extent comes from `CatalogIndex.temporal_extent()`, the same start and
  end as `stats()` without reading thumbnail blobs, and is reused until the
  index file changes.
- **`umbra download` accepts a legacy `_METADATA.json` URL.** Commands that
  take a sidecar URL (`download`, `info`, `quicklook`, and the rest) used to
  parse a 2023 to 2024 `<stem>_METADATA.json` as STAC and fail with `Item ''
  has no asset 'SICD'`. Those URLs now use the same synthesis as `umbra
  index build`, so the collect's CPHD, SICD, and GEC siblings are assets.
- **A full volume no longer breaks the boot refresh or the index.**
  `umbra index fetch --if-changed` and `fetch-thumbnails --if-changed` remove
  stale `.next` / `.next.part` / `.merge` leftovers and check free space before
  each download and merge. A step that does not fit is skipped with an
  `InsufficientSpaceError` line, and the current files are kept. The thumbnail
  merge writes a copy that replaces `catalog.db` only after it passes
  `quick_check`, and an unreadable index is re-fetched even when its snapshot is
  current. `/healthz` adds `thumbnails` and `degraded` (still `200`) and returns
  `503` only when the index cannot be opened; see `docs/deploy.md`, "Volume
  sizing".
- **The hosted API is redeployed onto the whole weekly snapshot.** The
  publish workflow now redeploys Railway after both `catalog.db` and
  `catalog.thumbs.db` are released, confirms `/healthz` `snapshot` matches the
  released `catalog.db` ETag, and warns in the job summary when `RAILWAY_TOKEN`
  is unset instead of silently skipping.
- **The Docker / Railway image no longer serves its first index forever.** The
  entrypoint fetched the snapshot only when `catalog.db` was missing, so a
  persistent `/data` volume pinned the hosted API to its first boot. It now
  runs `umbra index fetch --if-changed` on every boot and keeps serving the
  existing index if the refresh fails.
- **The image's baked previews refresh too.** `catalog.thumbs.db` was also
  fetched only when missing, and a refreshed `catalog.db` lost its merged
  previews. The entrypoint now runs the new
  `umbra index fetch-thumbnails --if-changed` on every boot: it downloads a
  changed sidecar and re-merges it whenever the sidecar or the index changed.
  If the check fails, the existing sidecar is kept and merged.
- **`/healthz` `snapshot` is the bare ETag.** It read `"\"0x...\""` (the raw
  header's quotes); quotes and a weak `W/` prefix are now stripped, and
  existing recorded state still compares as the same snapshot.
- **Public MCP COG refusals reach the client.** Tools that refuse to proxy
  Umbra GeoTIFFs on `umbra serve --public` (`change_composite`, `timescan`,
  `stack_stats`, `narrate_change`) used to arrive as
  `Error executing tool <name>` with the local-server hint left on the
  server. Anticipated `ValueError` / `UmbraError` now surface as MCP
  `ToolError`, so the client sees why the tool refused.
- **`umbra serve --public` accepts MCP requests addressed to a public
  hostname.** The MCP SDK auto-installs a localhost-only DNS-rebinding Host
  allowlist when `streamable_http_app()` is left at `host=127.0.0.1`, so
  `POST https://api.umbra-py.space/mcp` returned `421 Invalid Host header`
  while STAC on the same process answered 200. Public mode now disables that
  check (the reverse proxy already owns Host). Local `umbra serve --mcp`
  keeps the localhost allowlist.
- **`download_asset` with an empty item id says what to do.** The MCP tool
  used to answer `Item '' has no asset 'SICD'`; an empty or blank id now
  fails up front with "An item id is required ...; call search_catalog first."

## [0.1.2] — 2026-09-02

Hosted community STAC API next to MCP. Umbra still ships no search API;
`umbra serve --public` is that layer on one URL (STAC at `/search`, MCP at
`/mcp`). Railway start, volume, and extras bugs that kept the public
instance from booting are fixed. Listings name the gap.

### Added
- **Hosted community STAC API on the same Railway URL as MCP
  (`umbra serve --public`).** One process serves STAC search
  (`/search`, `/collections`, `/sites`, `/docs`) and Streamable HTTP MCP
  (`POST /mcp`). Public-instance guardrails: artifacts off (clients stream
  asset hrefs from Umbra S3 themselves), a per-client rate limit (120/min,
  `429` + `Retry-After`), CC-BY license headers on every response, uvicorn
  proxy headers so the cap sees real clients, and a refuse of `--live`,
  `--artifacts`, `--narrate`, `UMBRA_CANOPY_TOKEN`, and model API keys.
  `railway.toml` / `Dockerfile.mcp` start `serve --public`. Pair with the
  static showcase at `https://umbra-py.space/showcase/`.
- **`umbra mcp --http`: Streamable HTTP transport for a hosted MCP server.**
  stdio stays the default (`umbra-mcp` / Claude Desktop / `uvx`). `--http`
  serves the same tools at `POST /mcp` with `GET /healthz` for orchestrators.
  `$PORT` / `$UMBRA_PORT` / `$UMBRA_HOST` bind a container; Railway's
  `railway.toml` + `Dockerfile.mcp` fetch the catalog index then listen
  on `$PORT`. Stateless HTTP is on (no sticky sessions).
  The `umbra-mcp` console script still takes no argv, so a client that
  appends the package identifier keeps working.

### Changed
- **Public one-liners name the gap, not just the toolkit.** PyPI / docs /
  GitHub / MCP-registry descriptions now lead with "Umbra ships no search
  API" and the community STAC API + MCP, instead of "a local STAC API via
  `umbra serve`". The MCP registry blurb is still ≤100 characters.
- **Dev extra now requires ruff 0.16.** Ruff 0.16 formats fenced code
  blocks inside Markdown, which the previous `<0.16` cap was there to
  keep off CI. The pin is now `ruff>=0.16,<0.17` and the
  `examples/*.md` snippets are reformatted against that formatter.
  Replaces Dependabot #240, which lifted the cap without the reformat
  and failed `ruff format --check`.

### Fixed
- **Railway `umbra serve --public` no longer dies for want of FastAPI.**
  `Dockerfile.mcp` installed extras from `ARG UMBRA_EXTRAS`, so a dashboard
  build-arg left over from the MCP-only host (`mcp,viz`) overrode the new
  default and shipped an image without `[serve]`. The public Dockerfile now
  hardcodes `pip install ".[mcp,viz,serve]"`.
- **Railway Volume at `/data` is writable by the image user.** A Railway
  Volume is root-owned and hides the image's `chown`, so first boot failed
  with `PermissionError: '/data/umbra-py'` and fell back to a live S3 walk.
  The entrypoint now `chown`s `/data` as root and drops to `umbra` (uid
  10001) before `umbra index fetch`.
- **Railway Metal builder: drop the Dockerfile `VOLUME` instruction.** The
  builder rejects `VOLUME ["/data"]` (`use Railway Volumes`) even when a
  Railway Volume is already mounted at `/data`. `/data` stays a normal
  directory the image user owns; mount it at run time.
- **Railway first deploy no longer execs a missing `mcp` binary.** Railway's
  start command replaces the image `ENTRYPOINT` in exec form, so
  `startCommand = "mcp"` never reached the entrypoint. `railway.toml` now
  wraps `/usr/local/bin/docker-entrypoint.sh mcp`, and `Dockerfile.mcp`
  bakes `UMBRA_EXTRAS=mcp,viz` so a first boot needs no UI build-arg and
  no `/data` volume (the published index is ~17 MB). Generate a public
  `*.up.railway.app` domain — `.railway.internal` is private mesh DNS.

## [0.1.1] — 2026-08-21

PyPI long-description refresh. No runtime change.

### Changed
- Dropped the README **What's next** section (maintainer launch notes: Umbra
  outreach, registry listings, Zenodo). Contributing stays under Community.
  GitHub already had this via #242; this tag is what PyPI reads.

## [0.1.0] — 2026-08-21

First public release. Umbra publishes a 17+ TB open SAR archive with no search
API; this package is the search, preview, load, and convert layer over it.

**Discover.** `UmbraCatalog.search` walks the public S3 bucket with date
pruning, task-name (`area=` / `--fuzzy`) matching, bbox / polygon /
polarization / incidence / resolution filters, and a Canopy token path that
reuses the same interface. `CatalogIndex` + `umbra index fetch` serve a
weekly SQLite / stac-geoparquet / PMTiles snapshot so repeat search is local.
`umbra sites` ranks the most repeat-imaged tasks.

**Preview.** Range-request quicklooks, a full-res tile viewer, HTML galleries,
Folium footprint / timeline / swipe maps, change and timescan composites, a
stdlib `umbra demo` explorer, and a static Pages showcase.

**Load.** `to_xarray` / `to_geotiff` stream a GEC window into xarray or a
file. `to_stack` / `stack_stats` / `umbra stack` co-register a series onto
one grid (eager or lazy/chunked) and reduce it to JSON, with provenance
refusal when conversions disagree and an exact speckle detection floor.

**Convert / chips.** `sicd_to_geocoded_cog` / `umbra convert` geocode a SICD
(flat-earth or DEM, optional RTC, calibration, noise subtraction, speckle
filter, `--clip-bbox`). `umbra preflight` reads SICD XML over HTTP before a
download. `umbra chips` cuts georeferenced ML tiles with a manifest.

**Serve / agents.** `umbra serve` is a read-only STAC API over the index.
`umbra-mcp` (plus LangChain / LlamaIndex wrappers) exposes the same
deterministic callables. `umbra ask` / `describe` / `embed` / `change
--narrate` call a model only when asked. Every `--json` shape has a schema
in `docs/schemas/`.

What this is not: an InSAR / coherence toolbox; a hosted public API; a
live-verified Canopy client; a MultiRTC replacement. See
`docs_src/guides/limitations.md`.

This tag is the first public cut. Pre-release commits are in git.

[Unreleased]: https://github.com/reesehammer/umbra-py/compare/v0.1.4...HEAD
[0.1.4]: https://github.com/reesehammer/umbra-py/releases/tag/v0.1.4
[0.1.3]: https://github.com/reesehammer/umbra-py/releases/tag/v0.1.3
[0.1.2]: https://github.com/reesehammer/umbra-py/releases/tag/v0.1.2
[0.1.1]: https://github.com/reesehammer/umbra-py/releases/tag/v0.1.1
[0.1.0]: https://github.com/reesehammer/umbra-py/releases/tag/v0.1.0
