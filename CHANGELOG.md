# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Entries are user-facing: what a consumer can do now, or what broke. Design
rationale lives in the PR; pre-0.1.0 development history lives in git.

## [Unreleased]

### Added
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

### Changed
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

[Unreleased]: https://github.com/reesehammer/umbra-py/compare/v0.1.2...HEAD
[0.1.2]: https://github.com/reesehammer/umbra-py/releases/tag/v0.1.2
[0.1.1]: https://github.com/reesehammer/umbra-py/releases/tag/v0.1.1
[0.1.0]: https://github.com/reesehammer/umbra-py/releases/tag/v0.1.0
