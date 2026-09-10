# docs/

This is the published user manual (mkdocs → [umbra-py.space](https://umbra-py.space/)).

| Path | Role |
|------|------|
| `docs/*.md`, `guides/`, `reference/` | Mkdocs pages (`docs_dir: docs`) |
| [`schemas/`](schemas/) | Public JSON contracts (also packaged in the wheel as `umbra_py/_schemas`) |
| [`.github/TODO.md`](../.github/TODO.md) | Maintainer ledger of scoped-out follow-ups (internal; not part of the site) |
| [`deploy/`](../deploy/) | Dockerfiles, compose, entrypoint |
| [`railway.toml`](../railway.toml) | Railway Config-as-Code (root; `dockerfilePath` → `deploy/Dockerfile.mcp`) |

`README.md` and `schemas/` are excluded from the mkdocs build. See also the **Repo layout** section in [`README.md`](../README.md).
