# AGENTS.md

Guidance for AI coding agents (Claude Code, Cursor, Aider, Copilot, etc.) working
in this repository. Humans should read [`README.md`](README.md) and
[`CONTRIBUTING.md`](CONTRIBUTING.md) first; this file exists so an agent can pick
up a task without re-deriving project context from scratch.

If you are Claude Code reading [`CLAUDE.md`](CLAUDE.md), that file points here.
Treat **this** file as the source of truth.

---

## 1. Project in 30 seconds

- **What it is:** `umbra-py` — a Python toolkit for discovering, downloading
  and working with [Umbra](https://umbra.space/open-data/) open SAR data.
- **Status:** v0.1.2. Discovery, download, load,
  convert, viz, chips, serve, and the agent front doors all ship. Not an
  InSAR toolbox; see `docs_src/guides/limitations.md`.
- **Language / Python:** Python 3.10+ (also tested on 3.11, 3.12).
- **License:** Apache-2.0 (code); Umbra data is CC-BY-4.0.
- **Package layout:** `src/umbra_py/` (importable as `umbra_py`).
- **Console entry points:** `umbra` and `umbra-py` → `umbra_py.cli:main`.

The data lives in a public S3 bucket under
`sar-data/tasks/<task>/[<uuid>/]<acquisition>/`, with a `*.stac.v2.json`
sidecar next to each acquisition's binary products. There is no STAC API
or search endpoint — this library is the search layer, enumerating
acquisitions via paginated S3 listings.

---

## 2. Repo map (where to look first)

```
src/umbra_py/
  __init__.py        # public API surface; update __all__ when adding exports
  catalog.py         # UmbraCatalog: walks sar-data/tasks/ via S3 listings, prunes by date
  index.py           # CatalogIndex: local SQLite index of items for fast offline/repeat search
  models.py          # UmbraItem dataclass + asset classification + intersects_bbox / intersects_polygon
  _geometry.py       # stdlib-only GeoJSON polygon parsing + intersection primitives (the `intersects` search filter, no shapely)
  download.py        # download_url / download_asset / download_item (resume support)
  cli/               # the `umbra` command group; every name re-exported from `umbra_py.cli` (see its __init__ docstring)
    _root.py         #   the Click group itself, the UMBRA_JSON_ERRORS envelope, `main()`
    _shared.py       #   shared option groups (geography / task name / acquisition properties / token / manifest) + how a command gets its items (`_gather_items`, `_item_from_url`)
    discover.py      #   `search | watch | info | context | llms-txt | ask`: which acquisitions exist
    scenes.py        #   `describe | download | quicklook | view | load`: one acquisition at a time
    process.py       #   `stack | convert | chips | preflight`: data products rather than pictures
    composites.py    #   `change | timescan | swipe`: multi-pass pictures of one site
    atlas.py         #   `map | gallery`: where the archive has imagery
    explore.py       #   `mcp | serve | demo | tiles | showcase`: the commands that stand something up
    indexes.py       #   `index | semantic | embed`: the local SQLite sidecars
  constants.py       # bucket, STAC root URL, canonical product types
  load.py            # to_xarray / to_stack / stack_stats: analysis-ready arrays and datacubes ([load], [dask])
  convert.py         # optional SICD -> slant-plane amplitude + (flat-earth or DEM terrain-orthorectified) geocoded COG, optionally RTC-flattened, radiometrically calibrated, noise-floor-subtracted, speckle-filtered and clipped to an area of interest (behind [convert] extra)
  coverage.py        # SiteCoverage / umbra sites: rank the most repeat-imaged tasks
  embed.py           # umbra embed: visual similarity sidecar ([ai]+[viz])
  showcase.py        # umbra showcase: static Pages landing + featured composites
  pmtiles.py         # umbra tiles: stdlib PMTiles v3 writer + MapLibre viewer
  export.py          # stac-geoparquet export of a CatalogIndex ([export])
  geocode.py         # Nominatim place-name geocoder (rate-limited)
  dates.py           # parse_date_bound: ISO + relative date grammar
  fuzzy.py           # deterministic task-name fuzzy match (no model)
  dem.py / geoid.py  # Copernicus DEM tiles + EGM undulation fetch for convert --dem / --geoid
  preflight.py       # umbra preflight: read a SICD's XML metadata out of the NITF by HTTP range request (stdlib NITF header walk, no sarpy) and answer whether a product can support --calibrate / --noise-model measured / --rtc's SCPCOA geometry, before downloading it; a selection is read several products at a time (workers=, default 8) but consumed in selection order, since the chip run pairs verdicts against its items positionally
  chips.py           # umbra chips: cut scenes into fixed-size georeferenced ML training tiles + manifest ([load], no model call); --asset SICD geocodes each complex product via convert.py first ([convert]); --clip-bbox tiles (and converts) one area of interest; --preflight drops the passes whose metadata cannot support the request before downloading any of them (preflight.py); a run that left anything out writes a skipped.jsonl sidecar beside the manifest, so the dataset states its hole and not only the run that built it
  viz/               # rendering package; every name re-exported from `umbra_py.viz` (see its __init__ docstring)
    geojson.py       #   items -> GeoJSON features / FeatureCollections (no dependencies)
    raster.py        #   range-request COG reads, amplitude stretches, quicklooks, thumbnails ([viz])
    composites.py    #   co-registration + change / timescan composites and animations ([viz])
    contact_sheet.py #   `umbra gallery`: many acquisitions as one standalone HTML page ([viz])
    maps.py          #   Folium footprint / timeline / swipe maps + the rate-limited Nominatim geocoder ([viz])
    _deps.py         #   _require(): the single optional-dependency gate for the whole package
  viewer.py          # local XYZ tile server + Leaflet page for `umbra view` (full-res scene explorer, [viz])
  demo.py            # umbra demo: one self-contained interactive catalog explorer (Leaflet + markercluster, client-side facets, lazy SAR overlays); stdlib-only generator
  _lazy_imagery.py   # browser-side geotiff.js COG-fetch driver shared by `umbra map --lazy-imagery` and `umbra demo`
  mcp_server.py      # umbra-mcp: MCP server exposing search/geocode/quicklook/change/timescan tools ([mcp])
  langchain.py       # umbra_tools(): the same catalog tools as native LangChain/LangGraph StructuredTools; reuses mcp_server's deterministic callables ([langchain])
  llamaindex.py      # umbra_tools(): the same catalog tools as native LlamaIndex FunctionTools; reuses mcp_server's deterministic callables ([llamaindex])
  serve.py           # umbra serve: read-only STAC API façade over CatalogIndex (FastAPI, [serve]); GET /sites ranks the most repeat-imaged sites (discovery before analysis); --public hosts it next to MCP at /mcp (Railway); its routes carry the committed docs/schemas/ contracts into the generated OpenAPI document as components
  context.py         # llm_context(): domain knowledge as a machine-readable JSON dict (`umbra context`)
  llms_txt.py        # llms_txt()/llms_full_txt(): llms.txt-convention agent guide (`umbra llms-txt`); stdlib-only
  planner.py         # umbra ask: model plans a search, library re-validates + executes it ([ai])
  semantic.py        # umbra semantic: embedding index over task names for meaning-based --area aliasing ([ai])
  describe.py        # umbra describe: vision model reads a rendered quicklook -> structured, provenance-stamped scene description ([ai]+[viz])
  narrate.py         # umbra change --narrate: vision model narrates change, grounded in a deterministic per-block dB-delta grid ([ai]+[viz])
  watch.py           # umbra watch: idempotent delta detection for standing site monitoring (state in the index meta table; no model call)
  schemas.py         # load_schema()/schema_names(): read the published docs/schemas/ contracts from an installed umbra-py (stdlib only; the wheel carries a copy of the directory as package data)
  exceptions.py      # UmbraError hierarchy
  _http.py           # tiny requests wrapper, default session, timeouts
  _specfun.py        # trigamma + regularized incomplete beta in stdlib math, so stack_stats' speckle detection floor needs no SciPy
tests/
  test_catalog.py    # offline tests using an in-memory fake catalog tree
  test_models.py     # parsing/accessor tests against tests/data/sample_item.json
  test_download.py   # uses `responses` to mock HTTP
  test_live.py       # marked `network`, skipped by default
  test_workflows.py  # every `umbra ...` call in .github/workflows/ must parse
  test_schemas.py    # every docs/schemas/*.json validated against a real payload from the surface that emits it
  test_mcp_registry.py # server.json and every documented `uvx ...` must be the command that actually starts umbra-mcp
  data/sample_item.json
examples/            # notebooks 01–08 + Markdown guides; see examples/README.md
docs_src/            # published user manual (mkdocs); not the internal docs/ ledger
.github/workflows/ci.yml  # lint + format check + offline pytest (matrix 3.10/3.11/3.12) + mypy + all-extras coverage gate
pyproject.toml       # deps, extras, ruff + pytest config
server.json          # MCP registry manifest for umbra-mcp; submitted by release.yml's publish-mcp job
docs/schemas/        # the published JSON contracts for every `--json` surface (public API); shipped in the wheel too, read with `umbra_py.schemas`
docs/TODO.md         # ledger of follow-ups intentionally scoped out of merged PRs
docs/README.md       # clarifies docs_src vs docs/schemas vs TODO/STRATEGY vs deploy/
deploy/              # Dockerfiles, docker-compose.yml, docker-entrypoint.sh, railway.toml (build context = repo root)
```

PLACEHOLDER_REST_OF_AGENTS
