"""Generate the ``llms.txt`` context bundle for umbra-py.

*Context is a product surface.* The `llms.txt convention <https://llmstxt.org/>`_
gives a project one well-known, LLM-ready description of itself: a concise
``/llms.txt`` index and an expanded ``/llms-full.txt`` that an agent can pull
into context in a single fetch. Where :func:`umbra_py.llm_context` is the
*machine-readable* domain document (a JSON dict for programmatic use), this
module renders the *prose* guide — "how to drive this library" — for a model
reading Markdown.

Two documents are produced, both from facts already in the package:

- :func:`llms_txt` — the concise index (title, one-paragraph orientation, and
  link sections), the convention's ``/llms.txt``.
- :func:`llms_full_txt` — the self-contained bundle: the determinism boundary,
  the domain knowledge from :func:`umbra_py.llm_context`, the full CLI command
  reference (introspected from the live ``umbra`` command tree), the AI-native
  interfaces, and each core module's explanatory docstring. This is
  ``/llms-full.txt``.

Design notes, in keeping with the library's determinism boundary (``AGENTS.md``):
this module is **deterministic and stdlib-only** — it describes the library, it
never calls a model, and it imports no heavy extra. Module docstrings are read
from source with :mod:`ast` rather than by importing the modules, so the
generator runs in the bare ``requests`` + ``click`` core install without pulling
in ``fastapi``, ``mcp``, ``matplotlib`` or the rest. The committed
``llms.txt`` / ``llms-full.txt`` at the repo root are the rendered output of
these functions; a golden test keeps them from drifting. Regenerate with::

    umbra llms-txt > llms.txt
    umbra llms-txt --full > llms-full.txt
"""

from __future__ import annotations

import ast
from pathlib import Path

from .constants import (
    ATTRIBUTION,
    DATA_LICENSE,
    GITHUB_REPO,
    POLARIZATION_CAVEAT,
    PRODUCT_ASSETS,
    PRODUCT_TYPE_EXPLANATIONS,
)
from .context import llm_context

#: Raw-content base for the canonical repository. Agents fetch Markdown, so the
#: index links to ``raw.githubusercontent.com`` (which returns the file body)
#: rather than the rendered GitHub blob page.
_RAW_BASE = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main"

#: Core modules whose docstrings are written as explanatory preambles, in the
#: dependency order a reader would walk the library: discovery -> indexing ->
#: representation -> retrieval -> presentation -> AI-native interfaces. Each is
#: read from source with :mod:`ast`, so listing a module here never imports it.
_MODULE_GUIDE: tuple[tuple[str, str], ...] = (
    ("catalog.py", "UmbraCatalog"),
    ("index.py", "CatalogIndex"),
    ("models.py", "UmbraItem"),
    ("download.py", "download_asset"),
    ("load.py", "to_xarray / to_geotiff"),
    ("viz.py", "quicklook / maps / change / timescan / gallery"),
    ("context.py", "llm_context"),
    ("mcp_server.py", "umbra-mcp"),
    ("serve.py", "umbra serve"),
)


def _module_docstring(filename: str) -> str | None:
    """Return the module-level docstring of a package source file, or ``None``.

    The file is parsed with :mod:`ast`, never imported, so a module that pulls a
    heavy extra at import time (``serve``, ``mcp_server``) still contributes its
    docstring in a core-only environment.
    """
    path = Path(__file__).with_name(filename)
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        return None
    doc = ast.get_docstring(ast.parse(source))
    return doc.strip() if doc else None


def _first_paragraph(text: str) -> str:
    """The first blank-line-delimited paragraph, whitespace-collapsed to a line."""
    para = text.split("\n\n", 1)[0]
    return " ".join(para.split())


def _command_reference() -> list[tuple[str, str]]:
    """Introspect the ``umbra`` CLI into ``(command, one-line help)`` rows.

    ``cli`` is imported lazily to avoid an import cycle (``cli`` imports this
    module for the ``llms-txt`` command) and because the command tree is the
    single source of truth for what the library exposes — regenerating the
    bundle after adding a command keeps this section correct for free.
    """
    import click

    from .cli import cli

    rows: list[tuple[str, str]] = []

    def walk(group: click.Group, prefix: str) -> None:
        for name, cmd in sorted(group.commands.items()):
            full = f"{prefix} {name}"
            rows.append((full, (cmd.get_short_help_str(limit=300) or "").strip()))
            if isinstance(cmd, click.Group):
                walk(cmd, full)

    walk(cli, "umbra")
    return rows


_SUMMARY = (
    "A Python-first toolkit for Umbra's open SAR (synthetic aperture radar) "
    "data. It searches Umbra's static STAC catalog -- which publishes no upstream "
    "search API -- streams cloud-optimized products, and renders quicklooks, "
    "footprint maps, change composites and timescans. Because there is no Umbra "
    "STAC API, this library is the de-facto programmatic front door to a public, "
    "multi-terabyte SAR archive."
)

_ORIENTATION = (
    "umbra-py collapses two kinds of friction. The *mechanical* friction -- "
    "searching a catalog with no search endpoint, resuming multi-GB downloads, "
    "streaming cloud-optimized GeoTIFFs -- is handled by the deterministic core "
    "(`requests` + `click`, no heavy dependency in the base install). The "
    "*interpretive* friction -- which product type to ask for, why two "
    "polarizations must not be differenced, what a decibel stretch means -- is "
    "handled by making the library AI-legible: the CLI emits JSON, items expose "
    "`to_llm_context()` context cards, `umbra context` prints the domain "
    "document machine-readably, and the archive is reachable through two "
    "AI-native front doors -- an MCP server (`umbra mcp`) and a read-only STAC "
    "API (`umbra serve`). Heavy geospatial dependencies (rasterio, matplotlib, "
    "folium, xarray, sarpy) live behind extras and load lazily, so an agent can "
    "search and reason over metadata with nothing extra installed."
)

_DETERMINISM_NOTE = (
    "Determinism boundary: the core library is deterministic and never calls a "
    "model. Anything AI-facing (this document, the context cards, the MCP "
    "server, the STAC API) either describes the library or exposes it as tools "
    "-- models plan, describe and narrate; the library searches, downloads and "
    "renders. A model output never becomes a coordinate, a URL or a filter "
    "without passing through the deterministic layer."
)


def _license_block() -> str:
    lic = llm_context()["license"]
    return (
        f"All Umbra open data is licensed {DATA_LICENSE}. Attribution is "
        f"mandatory and must survive every derived product -- including "
        f"model-generated text describing the data. Use the string: "
        f'"{lic["attribution"]}"'
    )


def llms_txt() -> str:
    """Return the concise ``llms.txt`` index as a Markdown string.

    The `llms.txt convention <https://llmstxt.org/>`_ document: an H1 title, a
    one-line blockquote summary, a short orientation, and link sections pointing
    an agent at the fuller resources (chief among them ``llms-full.txt``). It is
    the map; :func:`llms_full_txt` is the territory.
    """
    lines: list[str] = []
    lines.append("# umbra-py")
    lines.append("")
    lines.append(f"> {_SUMMARY}")
    lines.append("")
    lines.append(_ORIENTATION)
    lines.append("")
    lines.append(_DETERMINISM_NOTE)
    lines.append("")

    lines.append("## Start here")
    lines.append("")
    lines.append(
        f"- [llms-full.txt]({_RAW_BASE}/llms-full.txt): the complete, "
        "self-contained guide to driving umbra-py -- domain knowledge, the full "
        "CLI command reference, the AI-native interfaces, and a per-module map. "
        "Fetch this first."
    )
    lines.append(
        f"- [README]({_RAW_BASE}/README.md): human-facing install instructions "
        "and a quick-start tour of every command."
    )
    lines.append(
        "- `umbra context` / `umbra_py.llm_context()`: the same domain knowledge "
        "as a machine-readable JSON document, for programmatic use."
    )
    lines.append("")

    lines.append("## AI-native interfaces")
    lines.append("")
    lines.append(
        "- MCP server (`umbra mcp` / `uvx umbra-mcp`, the `[mcp]` extra): exposes "
        "`search_catalog`, `get_item`, `geocode_place`, `index_stats`, "
        "`quicklook`, `change_composite`, `timescan`, `download_asset` and "
        "`watch_site` (report only passes new since the last check) as MCP "
        "tools; the imagery tools return the rendered PNG as an image block, so "
        "the agent *sees* the scene. Ships a `umbra://context` resource and "
        "`monitor-site` / `watch-site` / `survey-region` prompts."
    )
    lines.append(
        "- STAC API (`umbra serve`, the `[serve]` extra): a read-only STAC API "
        "over the catalog index -- landing page, `/conformance`, `/collections`, "
        "`/collections/{id}/items`, and item search over `GET`/`POST /search` -- "
        "with an OpenAPI doc at `/docs`. Speaks the protocol `pystac-client`, "
        "the QGIS STAC plugin, `stac-browser`, leafmap and OpenAPI-driven agents "
        "already understand."
    )
    lines.append("")

    lines.append("## More docs")
    lines.append("")
    lines.append(
        f"- [AGENTS.md]({_RAW_BASE}/AGENTS.md): the contributor-agent guide -- "
        "how to *modify* the library (repo map, conventions, testing rules)."
    )
    lines.append(
        f"- [CONTRIBUTING.md]({_RAW_BASE}/CONTRIBUTING.md): development setup, "
        "linting and the test workflow."
    )
    lines.append(f"- [Changelog]({_RAW_BASE}/CHANGELOG.md): what has shipped, most recent first.")
    lines.append("")

    lines.append("## Optional")
    lines.append("")
    lines.append(
        f"- [Strategy]({_RAW_BASE}/docs/STRATEGY.md): where the project sits in "
        "the SAR ecosystem and why."
    )
    lines.append(
        f"- [AI integration ideas]({_RAW_BASE}/docs/AI_INTEGRATION_IDEAS.md): the "
        "MCP / STAC-API / AI-capability roadmap this bundle is part of."
    )
    lines.append("")

    return "\n".join(lines)


def llms_full_txt() -> str:
    """Return the expanded ``llms-full.txt`` bundle as a Markdown string.

    Everything an agent needs to drive umbra-py, in one document and in reading
    order: the determinism boundary, the domain knowledge (product types,
    product order, search-parameter semantics, the polarization caveat, the
    license), the full CLI command reference introspected from the live command
    tree, the two AI-native interfaces, and each core module's explanatory
    docstring. Assembled entirely from facts already in the package, so it never
    drifts from the code it describes.
    """
    ctx = llm_context()
    lines: list[str] = []

    lines.append("# umbra-py — full LLM context")
    lines.append("")
    lines.append(f"> {_SUMMARY}")
    lines.append("")
    lines.append(_ORIENTATION)
    lines.append("")
    lines.append(_DETERMINISM_NOTE)
    lines.append("")

    # --- Product types -------------------------------------------------------
    lines.append("## Product types")
    lines.append("")
    lines.append(
        "Ordered easiest-to-use first (GEC) to rawest (CPHD). Prefer GEC unless "
        "the task needs complex or phase data."
    )
    lines.append("")
    for name in PRODUCT_ASSETS:
        explanation = PRODUCT_TYPE_EXPLANATIONS.get(name, "")
        lines.append(f"- **{name}** — {explanation}")
    lines.append("")
    lines.append(f"Polarization caveat: {POLARIZATION_CAVEAT}")
    lines.append("")

    # --- Search parameters ---------------------------------------------------
    lines.append("## Search parameters")
    lines.append("")
    lines.append(
        "Filters shared by the `umbra search` CLI, `UmbraCatalog.search` and "
        "`CatalogIndex.search`. Build a valid query in one shot:"
    )
    lines.append("")
    for param, description in ctx["search_parameters"].items():
        lines.append(f"- **{param}** — {description}")
    lines.append("")

    # --- License -------------------------------------------------------------
    lines.append("## License and attribution")
    lines.append("")
    lines.append(_license_block())
    lines.append("")

    # --- CLI reference -------------------------------------------------------
    lines.append("## CLI command reference")
    lines.append("")
    lines.append(
        "The CLI subcommands map 1:1 to library functions. Run any command with "
        "`--help` for its full options; `--json` is available where a structured "
        "result makes sense (`search`, `info`)."
    )
    lines.append("")
    for command, short_help in _command_reference():
        lines.append(f"- `{command}` — {short_help}")
    lines.append("")

    # --- AI-native interfaces ------------------------------------------------
    lines.append("## AI-native interfaces")
    lines.append("")
    lines.append(
        "- **MCP server** (`umbra mcp` / `uvx umbra-mcp`, `[mcp]` extra) — "
        "exposes `search_catalog`, `get_item`, `geocode_place`, `index_stats`, "
        "`quicklook`, `change_composite`, `timescan`, `download_asset` and "
        "`watch_site` (report only passes new since the last check) as MCP "
        "tools. The imagery tools return the rendered PNG as an MCP image block. "
        "Also serves a `umbra://context` resource and the `monitor-site` / "
        "`watch-site` / `survey-region` prompts."
    )
    lines.append(
        "- **STAC API** (`umbra serve`, `[serve]` extra) — a read-only STAC API "
        "over the same `CatalogIndex`: landing page, `/conformance`, "
        "`/collections`, `/collections/{id}`, `/collections/{id}/items`, "
        "`/collections/{id}/items/{item_id}`, and item search over `GET /search` "
        "and `POST /search` (bbox, datetime interval, ids, limit, token "
        "pagination), with an OpenAPI doc at `/docs`."
    )
    lines.append("")

    # --- Module guide --------------------------------------------------------
    lines.append("## Module guide")
    lines.append("")
    lines.append(
        "The library's layers, each with the opening of its source docstring. "
        "Read top-to-bottom to follow a scene from discovery to presentation."
    )
    lines.append("")
    for filename, exports in _MODULE_GUIDE:
        doc = _module_docstring(filename)
        if not doc:
            continue
        lines.append(f"### `umbra_py/{filename}` ({exports})")
        lines.append("")
        lines.append(_first_paragraph(doc))
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(ATTRIBUTION)
    lines.append("")

    return "\n".join(lines)
