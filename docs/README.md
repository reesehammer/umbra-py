# docs/

This directory is **not** the published user manual. Layout for newcomers:

| Path | Role |
|------|------|
| [`docs_src/`](../docs_src/) | Published mkdocs site → [umbra-py.space](https://umbra-py.space/) |
| [`docs/schemas/`](schemas/) | Public JSON contracts (also packaged in the wheel as `umbra_py/_schemas`) |
| [`docs/TODO.md`](TODO.md), [`docs/STRATEGY.md`](STRATEGY.md) | Maintainer ledger / strategy (internal) |
| [`deploy/`](../deploy/) | Dockerfiles, compose, entrypoint |
| [`railway.toml`](../railway.toml) | Railway Config-as-Code (root; `dockerfilePath` → `deploy/Dockerfile.mcp`) |

See also the **Repo layout** section in [`README.md`](../README.md).
