# Contributing to umbra-py

Thanks for your interest in improving `umbra-py`! This project aims to be the
friendly, batteries-included entry point to Umbra's open SAR data, and
contributions of all kinds — code, docs, examples, bug reports — are welcome.

## Development setup

We use [`uv`](https://github.com/astral-sh/uv) (or plain `pip`) and
[`ruff`](https://github.com/astral-sh/ruff).

```bash
git clone https://github.com/reesehammer/umbra-py
cd umbra-py
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"
```

## Running the checks

```bash
ruff check .          # lint
ruff format .         # format
pytest                # unit tests (no network)
pytest -m network     # live tests against Umbra's public catalog
```

The default `pytest` run excludes tests marked `network` so the suite is fast
and offline. Please keep unit tests offline by mocking HTTP (see
`tests/test_download.py` for the pattern using `responses`).

## Guidelines

- **Keep the core install light.** Heavy/optional dependencies (sarpy, rasterio,
  matplotlib, …) belong behind extras and should be imported lazily inside the
  function that needs them (see `umbra_py/convert.py`).
- **Match existing style.** `ruff` enforces formatting and import order; run it
  before pushing.
- **Add a test** for new behavior, and a `CHANGELOG.md` entry under "Unreleased".
- **Be correct about SAR.** This is a domain where silent errors are easy. If a
  transform or parameter choice matters, say so in a docstring.

## Good first issues

Look for the `good first issue` and `help wanted` labels on the issue tracker.
The original starter list (metadata accessors, Folium helpers, notebooks) has
shipped. Useful remaining work is tracked in [`docs/TODO.md`](docs/TODO.md).
Good entry points today:

- A docs snippet or notebook that drifted from the public API
  (`tests/test_docs_snippets.py` / `tests/test_examples.py` catch this).
- A `network`-marked smoke test of the Canopy backend, once a
  `UMBRA_CANOPY_TOKEN` is available.
- Running `umbra convert --noise-check` against a real product that carries
  an `ABSOLUTE` `NoisePoly` (open products generally do not).

## Cutting a release

Version is single-sourced from `umbra_py.__version__`. The tag must be
`vX.Y.Z` matching that value — `.github/workflows/release.yml` refuses a
mismatch, then publishes to PyPI (Trusted Publisher) and submits
`server.json` to the MCP registry.

1. Register the PyPI Trusted Publisher *before* the first tag (GitHub repo
   `reesehammer/umbra-py`, workflow `release.yml`, environment `pypi`).
2. Move `CHANGELOG.md`'s `[Unreleased]` notes under `## [X.Y.Z] — YYYY-MM-DD`
   with a short first-screen summary above the detailed bullets. Leave
   Unreleased empty.
3. Set `date-released:` in `CITATION.cff` (and `doi:` after Zenodo mints one).
4. `ruff check . && ruff format --check . && pytest -q` is green.
5. Create a GitHub Release on tag `vX.Y.Z`. The Release body is the short
   CHANGELOG narrative, not the full file.
6. Confirm `pip install umbra-py` from a clean venv, then `umbra --version`.

Do not retag if PyPI publish fails — fix Trusted Publisher and re-run the
job.

## Reporting bugs

Open an issue using the bug report template and include the Umbra item URL (or
search parameters) needed to reproduce.

By contributing you agree that your contributions are licensed under the
project's Apache 2.0 license.
