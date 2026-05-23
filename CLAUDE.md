# CLAUDE.md

This file is the entry point for Claude Code (and other AI coding agents) in
this repository.

**👉 Read [`AGENTS.md`](AGENTS.md) for the full, canonical agent guide.** It is
the single source of truth for project context, conventions, setup commands,
testing rules, and the operating principles you should follow. Everything below
is a short pointer; do not duplicate guidance here.

## TL;DR for any coding task

1. **Open `AGENTS.md`.** It has the repo map, setup, test, and conventions.
2. **State your assumptions.** Surface ambiguity *before* writing code. If
   multiple interpretations exist, ask — don't pick silently.
3. **Smallest change that solves the task.** No speculative abstractions, no
   "improvements" to adjacent code, no error handling for impossible cases.
4. **Turn the task into a verifiable goal.** "Fix the bug" → "Write a test
   that reproduces it, then make it pass." Loop until the check passes.
5. **Every changed line must trace to the user's request.** Match existing
   style. Don't refactor things that aren't broken.

## Quick commands

```bash
uv pip install -e ".[dev]"          # install
ruff check . && ruff format --check .  # lint + format check
pytest -q                           # offline tests (CI default)
pytest -m network                   # live tests against Umbra's public catalog
umbra --help                        # CLI
```

## Where things live (one-liner)

`src/umbra_py/` is the package; `catalog.py` searches Umbra's static STAC
tree, `models.py` is the `UmbraItem` dataclass, `download.py` handles
resume-safe HTTPS downloads, `cli.py` exposes `umbra search|info|download`.
Heavy deps (`sarpy`, `rasterio`, `matplotlib`, `folium`) live behind extras
and are imported lazily.

For anything beyond this, see [`AGENTS.md`](AGENTS.md).
