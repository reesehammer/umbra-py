"""``umbra-llamaindex`` — the umbra_py catalog as native LlamaIndex tools.

*Agents are the new first-time users* (see :mod:`umbra_py.mcp_server`). The MCP
server puts the catalog in front of MCP-native clients (Claude Desktop / Code and
a growing list of others) and :mod:`umbra_py.langchain` puts the same surface in
front of LangChain / LangGraph builders; this module completes the reach to the
third large population of agent builders — anyone assembling an agent or a
RAG-style query engine with **LlamaIndex**. Umbra publishes no STAC API, so this
library is the query layer for a 17+ TB public SAR archive; :func:`umbra_tools`
hands that layer to a LlamaIndex agent as a ready-to-use list of
:class:`~llama_index.core.tools.FunctionTool`.

The commitments carry straight over from the other two surfaces, because there is
**no new business logic here** — every JSON tool is the exact same deterministic
callable the MCP server exposes (:mod:`umbra_py.mcp_server`), and the render tools
are thin native reimplementations over the same shared helpers, so all three front
doors present one inventory and can never drift:

- **Deterministic core, AI at the edges.** Almost nothing here calls a model:
  the tools search, geocode, download and render; the *agent's* model plans and
  narrates. The one deliberate, opt-in exception is ``describe_scene`` (the C2
  VLM reading), which consults a vision model only when an ``[ai]`` key is
  configured — and even then a model output never becomes a coordinate, a URL, or
  a filter without passing through the deterministic layer first.
- **Images are the API.** The ``quicklook`` / ``change_composite`` / ``timescan``
  tools return a :class:`RenderResult`: its string form is the caption a
  text-only agent reads, and the raw PNG rides on ``.png`` (surfaced as the
  ``ToolOutput.raw_output`` for a multimodal pipeline to *see* the radar scene) —
  the differentiator over geo servers that return only JSON. The JSON tools return
  compact context cards (:meth:`UmbraItem.to_llm_context`), not full STAC JSON, to
  protect the agent's context window.

Usage::

    from umbra_py.llamaindex import umbra_tools

    tools = umbra_tools()                       # ready for any LlamaIndex agent
    from llama_index.core.agent import ReActAgent
    agent = ReActAgent.from_tools(tools, llm=my_llm)

Requires the ``llamaindex`` extra (``pip install 'umbra-py[llamaindex]'``); it
pulls in ``llama-index-core`` (the lightweight tool/abstraction package, not the
full framework) and the ``viz`` extra the render tools need. It mirrors the
``mcp`` / ``langchain`` extras: a third front door for agent builders over the
identical deterministic callables.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import mcp_server as _mcp
from .constants import ATTRIBUTION
from .exceptions import MissingDependencyError

# The deterministic, JSON-returning tool callables are the single source of
# truth, shared verbatim with the MCP server (and the LangChain surface). They
# are defined without any MCP SDK dependency (only the render tools in mcp_server
# touch the SDK, and those are re-implemented natively below), so importing them
# here is free.
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
    from llama_index.core.tools import FunctionTool


def _require_llamaindex():
    """Import the LlamaIndex tool factory, or raise a helpful install hint."""
    try:
        from llama_index.core.tools import FunctionTool
    except ModuleNotFoundError as exc:  # pragma: no cover - exercised via tests
        raise MissingDependencyError(
            "The LlamaIndex tools need the 'llamaindex' extra. Install it with:\n"
            "    pip install 'umbra-py[llamaindex]'",
            hint="pip install 'umbra-py[llamaindex]'",
        ) from exc
    return FunctionTool


@dataclass
class RenderResult:
    """A rendered radar scene returned by the image tools.

    LlamaIndex has no ``content_and_artifact`` split, so the two channels ride on
    one value: ``str(result)`` is the ``caption`` (the human/agent-readable text
    a text-only model reads and the ``ToolOutput.content``), while ``result.png``
    is the raw PNG bytes (the ``ToolOutput.raw_output`` a multimodal pipeline
    hands to a vision model). *Images are the API.*
    """

    caption: str
    png: bytes

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.caption


# --------------------------------------------------------------------------
# Render tools. "Images are the API": these return a ``RenderResult`` whose
# string form is the caption and whose ``.png`` carries the raw PNG. They are
# reimplemented here (rather than reusing the MCP ``quicklook`` etc., which wrap
# the bytes in an ``mcp.Image``) over the same shared ``mcp_server`` helpers, so
# the LlamaIndex surface never pulls in the MCP SDK — only ``viz`` — exactly as
# the LangChain surface does. The names match the MCP / LangChain tools so an
# agent author sees one consistent inventory across all three front doors.
# --------------------------------------------------------------------------


def quicklook(url: str, asset: str = "GEC", db: bool = False, max_size: int = 1024) -> RenderResult:
    """Render a scene's quicklook PNG and return it as a RenderResult.

    Streams the cloud-optimized product at ``url`` (default the analysis-ready
    ``GEC``), applies a SAR-appropriate stretch (set ``db=True`` for a decibel
    stretch), and returns a :class:`RenderResult` — its ``.png`` carries the raw
    bytes so a multimodal model downstream *sees* the radar scene, and its caption
    carries the scene id and the mandatory attribution.
    """
    from . import viz

    item = _mcp._fetch_item(url)
    image = viz.quicklook(item, asset=asset, db=db, max_size=max_size)
    caption = f"Quicklook of {item.id} ({asset}). {ATTRIBUTION}"
    return RenderResult(caption, _mcp._png_bytes(image))


def change_composite(
    urls: list[str], asset: str = "GEC", db: bool = False, max_size: int = 1024
) -> RenderResult:
    """Composite 2-3 passes of one site into a change image (RenderResult).

    Pass the STAC URLs of two or three acquisitions of the *same* site in time
    order. Colors encode change: bright green = signal appeared after the first
    date, magenta = signal vanished, grey = unchanged. Refuses to mix
    polarizations (HH vs VV are not comparable). Returns a :class:`RenderResult`;
    its ``.png`` carries the raw bytes and its caption names the color semantics
    and the attribution.
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
    return RenderResult(caption, _mcp._png_bytes(image))


def timescan(
    urls: list[str], asset: str = "GEC", db: bool = False, max_size: int = 1024
) -> RenderResult:
    """Summarize a whole time series into one activity image (RenderResult).

    Pass the STAC URLs of a site's passes; the timescan encodes per-pixel
    temporal statistics so bright/colored areas are *where activity happened*
    over the series. Returns a :class:`RenderResult` — its ``.png`` carries the
    raw bytes and its caption carries the attribution.
    """
    from . import viz

    items = [_mcp._fetch_item(u) for u in urls]
    if len(items) < 2:
        raise ValueError("timescan needs at least two item URLs.")
    image = viz.timescan_composite(items, asset=asset, db=db, max_size=max_size)
    caption = f"Timescan over {len(items)} passes (color = temporal activity). {ATTRIBUTION}"
    return RenderResult(caption, _mcp._png_bytes(image))


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

# The "images are the API" render tools, returning a RenderResult (caption + PNG).
_RENDER_TOOLS = (quicklook, change_composite, timescan)


def umbra_tools(*, include_render: bool = True) -> list[FunctionTool]:
    """Build the umbra_py catalog tools as LlamaIndex :class:`FunctionTool` objects.

    Returns a list ready to hand to a LlamaIndex agent (``ReActAgent.from_tools``,
    ``FunctionCallingAgent``, or a tool-calling ``QueryEngineTool`` pipeline). Each
    tool's name and description are inferred from the underlying function
    (docstring + signature) and its argument schema from the signature, so the
    inventory stays in lockstep with the MCP and LangChain surfaces (same
    callables, same semantics).

    :param include_render: when ``True`` (the default) the ``quicklook`` /
        ``change_composite`` / ``timescan`` image tools are included; each returns
        a :class:`RenderResult` whose ``.png`` (the ``ToolOutput.raw_output``)
        carries the PNG for a multimodal model. Set ``False`` for a JSON-only
        surface — e.g. a text-only model, or an install without the ``viz`` extra.

    Raises :class:`~umbra_py.MissingDependencyError` if the ``llamaindex`` extra
    is not installed.
    """
    FunctionTool = _require_llamaindex()

    fns = list(_JSON_TOOLS)
    if include_render:
        fns += list(_RENDER_TOOLS)
    return [FunctionTool.from_defaults(fn=fn) for fn in fns]


__all__ = [
    "umbra_tools",
    "RenderResult",
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
