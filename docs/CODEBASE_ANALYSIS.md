# umbra-py — Codebase Analysis (consolidated)

> **This document has been consolidated.** It was a point-in-time codebase
> review (originally dated 2026-07-02, at commit `a89b5e9`, v0.1.0). Its
> recommendations — the critical S3 pagination fix, download integrity,
> HTTP retries, the parallel sidecar fetch, the security hardening
> (HTML escaping, SRI, `defusedxml`, `SECURITY.md`, `pip-audit`), the mypy
> type gate, the docs site, and the index/schema work — have almost all
> shipped.
>
> To avoid keeping the same status notes in several places, this file no
> longer carries the full analysis. Instead:
>
> - **What shipped** → [`CHANGELOG.md`](../CHANGELOG.md) (authoritative history).
> - **What's still open** → [`STRATEGY.md` §8](STRATEGY.md#8-current-status--remaining-critical-path)
>   (the remaining critical path) and [`TODO.md`](../TODO.md) (per-PR
>   follow-ons). The still-open structural items from this review — the
>   `cli.py` shared-gathering extraction (P3 #18), the `viz.py` package split
>   (P3 #19), `pytest --cov` + Codecov (P2 #16), and the R\*Tree upgrade — are
>   listed there.
>
> The original item IDs (`P0`–`P3`, `§3.1`–`§3.5`, `§4.1`–`§4.6`) are still
> cited from source docstrings and commit messages; the detail behind each is
> in this file's git history.
