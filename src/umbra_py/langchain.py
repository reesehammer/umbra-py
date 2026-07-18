"""``umbra-langchain`` — the umbra_py catalog as native LangChain tools.

*Agents are the new first-time users* (see :mod:`umbra_py.mcp_server`). The MCP
server puts the catalog in front of MCP-native clients (Claude Desktop / Code and
a growing list of others); this module puts the **same** tool surface in front of
the other large population of agent builders — anyone assembling a graph or agent
with LangChain / LangGraph. Umbra publishes no STAC API, so this library is the
query layer for a 17+ TB public SAR archive; :func:`umbra_tools` hands that layer
to a LangChain agent as a ready-to-bind list of :class:`~langchain_core.tools.BaseTool`.

Two commitments carry straight over from the MCP surface, because there is **no
new business logic here** — every tool is a thin adapter over the exact same
deterministic callables the MCP server exposes (:mod:`umbra_py.mcp_server`), so
the two front doors can never drift:

- **Deterministic core, AI at the edges.** Almost nothing here calls a model:
  the tools search, geocode, download and render; the *agent's* model plans and
  narrates. The one deliberate, opt-in exception is ``describe_scene`` (the C2
  VLM reading), which consults a vision model only when an ``[ai]`` key is
  configured — and even then a model output never becomes a coordinate, a URL, or
  a filter without passing through the deterministic layer first.
- **Images are the API.** The ``quicklook`` / ``change_composite`` / ``timescan``
  tools return the rendered PNG as a LangChain *tool artifact*
  (``response_format="content_and_artifact"``): the ``ToolMessage`` carries a text
  caption as its content and the raw PNG bytes on ``.artifact``, so a multimodal
  model downstream can *see* the radar scene — the differentiator over geo servers
  that return only JSON. The JSON tools return compact context cards
  (:meth:`UmbraItem.to_llm_context`), not full STAC JSON, to protect the agent's
  context window.

Usage::

    from umbra_py.langchain import umbra_tools

    tools = umbra_tools()                      # ready to bind to any LangChain agent
    llm_with_tools = my_chat_model.bind_tools(tools)
    # or, with LangGraph's prebuilt agent:
    #   from langgraph.prebuilt import create_react_agent
    #   agent = create_react_agent(my_chat_model, umbra_tools())

Requires the ``langchain`` extra (``pip install 'umbra-py[langchain]'``); it pulls
in ``langchain-core`` (the lightweight tool/abstraction package, not the full
framework) and the ``viz`` extra the render tools need.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import mcp_server as _mcp
from .constants import ATTRIBUTION
from .exceptions import MissingDependencyError

# The deterministic, JSON-returning tool callables are the single source of
# truth, shared verbatim with the MCP server. They are defined without any MCP
# SDK dependency (only the render tools in mcp_server touch the SDK, and those
# are re-implemented natively below), so importing them here is free.
from .mcp_server import (  # noqa: F401 - re-exported for direct use
    describe_scene,
    download_asset,
    find_similar,
    find_similar_text,
    geocode_place,
    get_item,
    index_stats,
    search_catalog,
    watch_site,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from langchain_core.tools import BaseTool


def _require_langchain():
    """Import the LangChain tool factory, or raise a helpful install hint."""
    try:
        from langchain_core.tools import StructuredTool
    except ModuleNotFoundError as exc:  # pragma: no cover - exercised via tests
        raise MissingDependencyError(
            "The LangChain tools need the 'langchain' extra. Install it with:\n"
            "    pip install 'umbra-py[langchain]'",
            hint="pip install 'umbra-py[langchain]'",
        ) from exc
    return StructuredTool


# --------------------------------------------------------------------------
# Render tools. "Images are the API": these return ``(caption, png_bytes)`` so
# LangChain's ``content_and_artifact`` response format puts the caption on the
# ToolMessage content and the raw PNG on ``.artifact`` for a downstream
# multimodal model. They are re-implemented here (rather than reusing the MCP
# ``quicklook`` etc., which wrap the bytes in an ``mcp.Image``) so the LangChain
# surface never pulls in the MCP SDK — only ``viz``. The names match the MCP
# tools so an agent author sees one consistent inventory across both front doors.
# --------------------------------------------------------------------------


def quicklook(url: str, asset: str = "GEC", db: bool = False, max_size: int = 1024):
    """Render a scene's quicklook PNG and return it as a tool artifact.

    Streams the cloud-optimized product at ``url`` (default the analysis-ready
    ``GEC``), applies a SAR-appropriate stretch (set ``db=True`` for a decibel
    stretch), and returns ``(caption, png_bytes)`` — the PNG rides on the
    ToolMessage's ``.artifact`` so a multimodal model downstream *sees* the radar
    scene, and the caption carries the scene id and the mandatory attribution.
    """
    from . import viz

    item = _mcp._fetch_item(url)
    image = viz.quicklook(item, asset=asset, db=db, max_size=max_size)
    caption = f"Quicklook of {item.id} ({asset}). {ATTRIBUTION}"
    return caption, _mcp._png_bytes(image)


def change_composite(urls: list[str], asset: str = "GEC", db: bool = False, max_size: int = 1024):
    """Composite 2-3 passes of one site into a change image (tool artifact).

    Pass the STAC URLs of two or three acquisitions of the *same* site in time
    order. Colors encode change: bright green = signal appeared after the first
    date, magenta = signal vanished, grey = unchanged. Refuses to mix
    polarizations (HH vs VV are not comparable). Returns ``(caption, png_bytes)``;
    the PNG rides on the ToolMessage's ``.artifact`` and the caption names the
    color semantics and the attribution.
    """
    from . import viz

    items = [_mcp._fetch_item(u) for u in urls]
    if len(items) < 2:
        raise ValueError("change_composite needs at least two item URLs.")
    _mcp._require_same_polarization(items)
    image = viz.change_composite(items, asset=asset, db=db, max_size=max_size)
    caption = (
        "Change composite (green = appeared, magenta = vanished, grey = "
        f"unchanged) over {len(items)} passes. {ATTRIBUTION}"
    )
    return caption, _mcp._png_bytes(image)


def timescan(urls: list[str], asset: str = "GEC", db: bool = False, max_size: int = 1024):
    """Summarize a whole time series into one activity image (tool artifact).

    Pass the STAC URLs of a site's passes; the timescan encodes per-pixel
    temporal statistics so bright/colored areas are *where activity happened*
    over the series. Returns ``(caption, png_bytes)`` — the PNG rides on the
    ToolMessage's ``.artifact`` and the caption carries the attribution.
    """
    from . import viz

    items = [_mcp._fetch_item(u) for u in urls]
    if len(items) < 2:
        raise ValueError("timescan needs at least two item URLs.")
    image = viz.timescan_composite(items, asset=asset, db=db, max_size=max_size)
    caption = f"Timescan over {len(items)} passes (color = temporal activity). {ATTRIBUTION}"
    return caption, _mcp._png_bytes(image)


# The JSON-returning tools, shared verbatim with the MCP server (compact context
# cards, geocoding, index status, downloads, standing watch, similarity search,
# and the one opt-in VLM reading). Each gates its own optional key/index at call
# time with a helpful error, so all are safe to register unconditionally.
_JSON_TOOLS = (
    search_catalog,
    get_item,
    geocode_place,
    index_stats,
    download_asset,
    watch_site,
    find_similar,
    find_similar_text,
    describe_scene,
)

# The "images are the API" render tools, returned via content_and_artifact.
_RENDER_TOOLS = (quicklook, change_composite, timescan)


def umbra_tools(*, include_render: bool = True) -> list[BaseTool]:
    """Build the umbra_py catalog tools as LangChain :class:`BaseTool` objects.

    Returns a list ready to hand to ``model.bind_tools(...)`` or a LangGraph
    ``create_react_agent``. Each tool's schema is inferred from the underlying
    function signature and its description from the docstring, so the inventory
    stays in lockstep with the MCP server (same callables, same semantics).

    :param include_render: when ``True`` (the default) the ``quicklook`` /
        ``change_composite`` / ``timescan`` image tools are included with the
        ``content_and_artifact`` response format (the PNG rides on the
        ``ToolMessage.artifact``). Set ``False`` for a JSON-only surface — e.g. a
        text-only model, or an install without the ``viz`` extra.

    Raises :class:`~umbra_py.MissingDependencyError` if the ``langchain`` extra
    is not installed.
    """
    StructuredTool = _require_langchain()

    tools: list[BaseTool] = [StructuredTool.from_function(fn) for fn in _JSON_TOOLS]
    if include_render:
        tools += [
            StructuredTool.from_function(fn, response_format="content_and_artifact")
            for fn in _RENDER_TOOLS
        ]
    return tools


__all__ = [
    "umbra_tools",
    "search_catalog",
    "get_item",
    "geocode_place",
    "index_stats",
    "download_asset",
    "watch_site",
    "find_similar",
    "find_similar_text",
    "describe_scene",
    "quicklook",
    "change_composite",
    "timescan",
]
