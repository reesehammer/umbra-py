# umbra-py — Making the Project AI-Native (consolidated)

> **This document has been consolidated.** It laid out the roadmap for making
> the project AI-legible and AI-native across three tiers: Tier A (AI-legible
> surface — `info --json`, `llm_context()`, context cards, the determinism
> policy, `__geo_interface__`), Tier B (AI-native interfaces — the `umbra-mcp`
> MCP server, the `umbra serve` STAC API, notebooks, `llms.txt`), and Tier C
> (AI-infused capabilities — natural-language search / `umbra ask`, `umbra
> describe`, `change --narrate`, `umbra watch`, `umbra chips`, and the `umbra
> embed` similarity index). **Every idea in this document has shipped.**
>
> To keep status in one place, this file no longer carries the full plan.
> Instead:
>
> - **What shipped** → [`CHANGELOG.md`](../CHANGELOG.md).
> - **The durable design principles** (deterministic core / AI at the edges,
>   images-are-the-API, context-as-a-product-surface, license propagation,
>   agents-are-users) → [`STRATEGY.md` §7](STRATEGY.md#7-design-principles-to-hold-onto).
> - **Open follow-ons** (e.g. registering `umbra-mcp` in the MCP registries) →
>   [`TODO.md`](../TODO.md), with the remaining critical path in
>   [`STRATEGY.md` §8](STRATEGY.md#8-current-status--remaining-critical-path).
>
> The original item IDs (`A1`–`A4`, `B1`–`B3`, `C1`–`C5`, `§6.1`) are still
> cited from source docstrings (`planner.py`, `describe.py`, `narrate.py`,
> `watch.py`, `mcp_server.py`, `chips.py`, `embed.py`, …); the detail behind
> each is in this file's git history.
