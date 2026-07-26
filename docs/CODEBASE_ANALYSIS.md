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
>   follow-ons). Of this review's structural items, the `viz.py` package split
>   (P3 #19) and `pytest --cov` + Codecov (P2 #16) have since **shipped**, as has
>   the `cli.py` shared-option extraction (P3 #18) — its geography group first,
>   then its task-name group (`--area` / `--fuzzy`, which gave `umbra map` the
>   site filter it was the only gather command to lack) plus the parity suite that
>   keeps a new gather command from missing either. What is still open — the date
>   and limit options, deliberately left per-command, and the R\*Tree upgrade — is
>   listed there.
>
> The original item IDs (`P0`–`P3`, `§3.1`–`§3.5`, `§4.1`–`§4.6`) are still
> cited from source docstrings and commit messages; the detail behind each is
> in this file's git history.
