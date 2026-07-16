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
- **Status:** v0.1 / early alpha. Discovery + download core is shipped;
  processing helpers are intentionally minimal.
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
  models.py          # UmbraItem dataclass + asset classification
  download.py        # download_url / download_asset / download_item (resume support)
  cli.py             # `umbra search | info | download`
  constants.py       # bucket, STAC root URL, canonical product types
  convert.py         # optional SICD -> slant-plane amplitude + geocoded COG (behind [convert] extra)
  chips.py           # umbra chips: cut scenes into fixed-size georeferenced ML training tiles + manifest ([load], no model call)
  viewer.py          # local XYZ tile server + Leaflet page for `umbra view` (full-res scene explorer, [viz])
  demo.py            # umbra demo: one self-contained interactive catalog explorer (Leaflet + markercluster, client-side facets, lazy SAR overlays); stdlib-only generator
  _lazy_imagery.py   # browser-side geotiff.js COG-fetch driver shared by `umbra map --lazy-imagery` and `umbra demo`
  mcp_server.py      # umbra-mcp: MCP server exposing search/geocode/quicklook/change/timescan tools ([mcp])
  serve.py           # umbra serve: read-only STAC API façade over CatalogIndex (FastAPI, [serve])
  context.py         # llm_context(): domain knowledge as a machine-readable JSON dict (`umbra context`)
  llms_txt.py        # llms_txt()/llms_full_txt(): llms.txt-convention agent guide (`umbra llms-txt`); stdlib-only
  planner.py         # umbra ask: model plans a search, library re-validates + executes it ([ai])
  semantic.py        # umbra semantic: embedding index over task names for meaning-based --area aliasing ([ai])
  describe.py        # umbra describe: vision model reads a rendered quicklook -> structured, provenance-stamped scene description ([ai]+[viz])
  narrate.py         # umbra change --narrate: vision model narrates change, grounded in a deterministic per-block dB-delta grid ([ai]+[viz])
  watch.py           # umbra watch: idempotent delta detection for standing site monitoring (state in the index meta table; no model call)
  exceptions.py      # UmbraError hierarchy
  _http.py           # tiny requests wrapper, default session, timeouts
tests/
  test_catalog.py    # offline tests using an in-memory fake catalog tree
  test_models.py     # parsing/accessor tests against tests/data/sample_item.json
  test_download.py   # uses `responses` to mock HTTP
  test_live.py       # marked `network`, skipped by default
  data/sample_item.json
examples/            # planned notebooks (v0.2); see examples/README.md
.github/workflows/ci.yml  # lint + format check + offline pytest, matrix 3.10/3.11/3.12
pyproject.toml       # deps, extras, ruff + pytest config
TODO.md              # ledger of follow-ups intentionally scoped out of merged PRs
```

**Discovery tips for agents:**
- `grep -rn "<symbol>" src/ tests/` is reliable — the tree is small (~10 modules).
- Public API is whatever `src/umbra_py/__init__.py` re-exports. If you add a
  public name, add it to `__all__`.
- The CLI subcommands are defined in `cli.py`; each maps 1:1 to a library
  function.

---

## 3. Setup, run, test (copy-paste)

```bash
# Install in editable mode with dev tools
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"          # or: pip install -e ".[dev]"

# Lint, format, test (what CI runs)
ruff check .
ruff format --check .                # use `ruff format .` to apply
pytest -q                            # offline only; default excludes network tests

# Optional: run the live integration tests against Umbra's public catalog
pytest -m network

# Try the CLI
umbra --help
umbra search --start 2024-02-08 --end 2024-02-08 --limit 3
```

If any of the above fails on a clean checkout, that's a real bug — surface it
before working around it.

---

## 4. How to think before coding

These principles are non-negotiable here. They map to the project's "small,
well-documented layer" philosophy.

### 4.1 Think before coding
- **State assumptions explicitly.** If the task says "add validation",
  enumerate what counts as invalid before writing any code.
- **Surface ambiguity.** If a task has multiple sensible interpretations, list
  them and ask. Do not pick silently.
- **Push back on complexity.** If the simplest viable approach is 20 lines and
  the requested approach is 200, say so. Recommend the simpler one with the
  tradeoff named.
- **Stop on confusion.** Name what's confusing. Ask. Don't paper over it.

### 4.2 Simplicity first
- Minimum code that solves the stated problem. Nothing speculative.
- **No** features beyond what was asked.
- **No** abstractions for single-use code (no base classes for one
  implementation, no `Protocol` for one caller).
- **No** "flexibility" or configurability that wasn't requested.
- **No** error handling for impossible scenarios. Validate at boundaries
  (HTTP responses, user input, STAC JSON), trust internals.
- If you wrote 200 lines and 50 would do, rewrite.

Senior-engineer test: would a senior reviewer call this overcomplicated?
If yes, simplify.

### 4.3 Surgical changes
- Touch only what the task requires.
- Don't "improve" adjacent code, comments, formatting, or naming — even when
  you'd do it differently. Match existing style.
- If you spot unrelated dead code or a latent bug, **mention it** in your
  reply and add an entry to [`TODO.md`](TODO.md) (link to the PR that
  surfaced it, point at the code, sketch the fix). Don't delete or fix it
  inline.
- **Clean up your own orphans only:** if your edit removes the last use of
  an import / variable / helper, delete it. Don't sweep pre-existing dead
  code on the side.

Per-line test: every changed line should trace directly to the user's request.

### 4.4 Goal-driven execution
Turn vague tasks into verifiable goals **before** coding:

| Vague task             | Verifiable goal                                                          |
| ---------------------- | ------------------------------------------------------------------------ |
| "Add validation"       | "Write tests for invalid inputs, then make them pass."                   |
| "Fix the bug"          | "Write a test that reproduces it, then make it pass."                    |
| "Refactor X"           | "Ensure tests pass before and after; no behavior change."                |
| "Speed up search"      | "Add a benchmark; show before/after; assert pruning still correct."      |

For multi-step tasks, write the plan inline:

```
1. <step>  → verify: <check>
2. <step>  → verify: <check>
3. <step>  → verify: <check>
```

Strong success criteria let you loop independently without check-ins.

---

## 5. Domain context an agent needs (and won't guess right)

This is a SAR / geospatial project. A few facts that matter when writing code:

- **No STAC API; we list S3 directly.** Acquisitions live under
  `sar-data/tasks/<task>/[<uuid>/]<acquisition>/`, each with a
  `*.stac.v2.json` sidecar. `UmbraCatalog._walk` paginates S3 listings
  level by level, pruning acquisition directories whose date prefix
  (`YYYY-MM-DD-HH-MM-SS_PLATFORM`) falls outside the requested
  `start` / `end` range. **Do not** flatten this into "fetch
  everything" — without date pruning the walk takes minutes.
- **Product types** (canonical, ordered easiest → rawest):
  `GEC, CSI, SIDD, SICD, CPHD`. See `constants.py:PRODUCT_ASSETS` and
  the README table. `GEC` is a cloud-optimized GeoTIFF and is the default
  starting point for users.
- **Asset key heuristics.** Different catalog generations name assets
  differently (`"GEC"` vs `..._MM.tif`). Classification lives in
  `models._classify_asset` — extend that, don't sprinkle string matching
  elsewhere.
- **Anonymous HTTPS only.** No AWS SDK, no signed requests, no creds. If
  you find yourself reaching for `boto3`, stop and re-check the task.
- **Resume-safe downloads.** `download_url` writes to `<dest>.part` and uses
  HTTP `Range` headers. Preserve this when changing download behavior.
- **Heavy deps are optional.** `sarpy`, `rasterio`, `numpy`, `matplotlib`,
  `folium` belong behind extras (`[convert]`, `[viz]`) and must be imported
  **inside** the function that needs them (see `convert.py:_require`). The
  core install stays small.
- **SAR correctness matters.** Silent errors are easy in this domain. If a
  transform or parameter choice has consequences (units, slant vs ground
  plane, dB scaling), say so in a docstring.
- **Deterministic core, AI at the edges.** The library searches, downloads and
  renders deterministically and offline-testably; it must never call a language
  model implicitly. Anything that *invokes* a model (describe/narrate/NL-search)
  belongs behind a future `[ai]` extra and runs only when the user asks. The
  AI-*legible* surface — `UmbraItem.to_llm_context()`, `llm_context()`,
  `__geo_interface__`, `--json` output — is pure data with no model call, so it
  stays in the core. See `docs/AI_INTEGRATION_IDEAS.md` §A4/§6.

---

## 6. Coding conventions

- **Style:** ruff (line length 100, target `py310`). Rule set in
  `pyproject.toml`: `E, F, I, UP, B, W`. Run `ruff format .` before committing.
- **Typing:** modern style — `list[str]`, `X | None`, `from __future__ import
  annotations` at the top of every module.
- **Errors:** raise from `UmbraError` subclasses in
  `exceptions.py`. Don't introduce a new top-level exception type without a
  reason.
- **HTTP:** go through `_http.default_session()` / `get_json()` so the user
  agent and timeouts stay consistent.
- **Public API:** anything in `src/umbra_py/__init__.py`'s `__all__` is public
  and must keep backwards compatibility within a minor version. Internal
  helpers start with `_`.
- **Docstrings:** module-level docstring explaining *why*, plus short
  function/class docstrings. Don't restate what well-named code already says.
- **Comments:** only when the *why* is non-obvious (a constraint, a workaround,
  a surprising behavior). Don't narrate the code.

---

## 7. Testing rules

- **Default `pytest` runs offline.** `pyproject.toml` sets
  `addopts = "-m 'not network'"`. Keep new unit tests offline.
- **Mock HTTP with `responses`.** See `tests/test_download.py` for the pattern.
- **For catalog tests:** monkey-patch `UmbraCatalog._get` with an in-memory
  tree (`tests/test_catalog.py` has the canonical example).
- **Live tests** belong in `test_live.py` (or any file) under
  `pytestmark = pytest.mark.network`. They only run on `pytest -m network`.
- **Every new behavior gets a test.** Every bug fix gets a regression test
  first (red), then the fix (green).
- **Don't pin to live data IDs** in offline tests — they can disappear from
  the public catalog.

---

## 8. Common task recipes

### Add a new metadata accessor on `UmbraItem`
1. Add a `@property` on `UmbraItem` in `models.py` reading from
   `self.properties`. → verify: `pytest tests/test_models.py`.
2. If user-facing, include it in `metadata_summary()` / `summary()`. → verify:
   summary string contains the new value.
3. CHANGELOG entry under **Unreleased**.

### Add a new CLI flag
1. Add the `@click.option` in `cli.py` next to the existing options on that
   subcommand.
2. Wire it through to the library function (don't put business logic in the
   CLI). → verify: `umbra <cmd> --help` shows it; add a click runner test if
   the behavior is non-trivial.

### Add a new optional dependency
1. Put it under the right extra in `pyproject.toml`
   (`[project.optional-dependencies]`).
2. Import it **inside** the function that needs it, via the
   `_require("modname")` pattern from `convert.py`. → verify: `pip install -e .`
   (without the extra) still imports `umbra_py` cleanly.

### Touching catalog traversal
- Re-run `tests/test_catalog.py::test_search_prunes_out_of_range_acquisitions` —
  pruning is a feature, regressing it makes the search orders of magnitude
  slower.

---

## 9. Git / PR workflow

- **Branch:** work on whatever feature branch you were told to use. Don't push
  to `main`.
- **Commits:** descriptive, present tense, focus on *why*. Match recent
  history (see `git log --oneline`).
- **Before pushing:**
  ```bash
  ruff check . && ruff format --check . && pytest -q
  ```
- **PR description should include:**
  - What changed and why.
  - Any new public API (functions, CLI flags, env vars).
  - Test plan (what you ran; what a reviewer should run).
  - A `CHANGELOG.md` entry under **Unreleased** for any user-visible change.
- **Scoping out follow-ups:** if you defer something to keep the PR small
  (latent bug, missing test, adjacent refactor), add an entry to
  [`TODO.md`](TODO.md) in the same PR. The PR body alone is too easy to lose.
  When a follow-up PR closes one out, delete the entry.
- Pre-commit hooks (`.pre-commit-config.yaml`) run ruff + a few sanity checks.
  Don't bypass with `--no-verify` — fix the root cause.

---

## 10. If you get stuck

- **Can't reproduce a bug:** ask for the Umbra item URL or the exact search
  parameters. Without that, you're guessing.
- **STAC item looks weird:** check `tests/data/sample_item.json` for the
  shape we already handle, then read the actual item JSON from the URL.
- **Network test fails in CI:** it shouldn't run — CI uses `pytest -q` which
  excludes `network`. If a "network" test runs by default, the marker is
  wrong.
- **Don't know which product type to use:** `GEC` for almost anything
  pixel-based; `SICD`/`CPHD` for phase-preserving work. See the README table.
- **Considering a destructive operation** (force push, hard reset, deleting
  a file you didn't create, dropping a dependency): stop and confirm with
  the user first.

When in doubt, ask. A 30-second clarifying question beats a 30-minute wrong
implementation.
