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
Great starter areas:

- More metadata accessors / nicer summaries on `UmbraItem`.
- Footprint visualization helpers (Folium/Leaflet) behind the `viz` extra.
- Example notebooks (`examples/`).
- Expanding live-catalog test coverage.

## Reporting bugs

Open an issue using the bug report template and include the Umbra item URL (or
search parameters) needed to reproduce.

By contributing you agree that your contributions are licensed under the
project's Apache 2.0 license.
