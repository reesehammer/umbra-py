"""``umbra-mcp`` — a Model Context Protocol server over :mod:`umbra_py`.

*Agents are the new first-time users.* Umbra publishes no STAC API, so this
library **is** the query layer for a 17+ TB public SAR archive. This module
exposes that layer over MCP, turning any MCP-enabled client (Claude Desktop /
Code and a growing list of others) into a zero-install, natural-language front
door to the archive: *"show me what changed at Centerfield, Utah this spring"*
becomes a first-run experience instead of a tutorial chapter.

The tools are thin wrappers over the existing public API — the CLI subcommands
already map 1:1 to library functions, so the tool inventory was already
designed. Two design commitments carry over from the rest of the package:

- **Deterministic core, AI at the edges.** Almost nothing here calls a model:
  the server searches, geocodes, downloads and renders; the *client's* model
  plans and narrates. The one deliberate, opt-in exception is ``describe_scene``
  (the C2 VLM reading), which consults a vision model only when an ``[ai]`` key
  is configured — and even it holds the boundary: a model output never becomes a
  coordinate, a URL, or a filter without passing through this deterministic
  layer, and every reading is validated and stamped as an AI interpretation.
- **Images are the API.** ``quicklook``/``change_composite``/``timescan``
  return the rendered PNG as an MCP image content block, so the model *sees*
  the radar scene — the differentiator over geo servers that return only JSON.
  Compact context cards (:meth:`UmbraItem.to_llm_context`), not full STAC JSON,
  are returned from search to protect the client's context window.

Run it with ``uvx umbra-mcp`` / ``umbra mcp`` (stdio transport) or
``python -m umbra_py.mcp_server``. Requires the ``mcp`` extra
(``pip install 'umbra-py[mcp]'``).
"""

from __future__ import annotations

import io
import json
from typing import TYPE_CHECKING, Any

from .catalog import UmbraCatalog
from .constants import ATTRIBUTION, DATA_LICENSE
from .context import llm_context
from .exceptions import MissingDependencyError
from .geocode import geocode_place as _geocode_place
from .index import CatalogIndex, default_index_path
from .models import UmbraItem
from .watch import MetaWatchStore, watch, watch_key

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mcp.server.fastmcp import FastMCP


def _require_mcp():
    """Import the MCP SDK, or raise a helpful install hint."""
    try:
        from mcp.server.fastmcp import FastMCP, Image
    except ModuleNotFoundError as exc:  # pragma: no cover - exercised via CLI
        raise MissingDependencyError(
            "The MCP server needs the 'mcp' extra. Install it with:\n"
            "    pip install 'umbra-py[mcp]'",
            hint="pip install 'umbra-py[mcp]'",
        ) from exc
    return FastMCP, Image


# --------------------------------------------------------------------------
# Shared helpers (deterministic; no MCP dependency so they are unit-testable
# without the SDK installed).
# --------------------------------------------------------------------------


def _fetch_item(url: str) -> UmbraItem:
    """Fetch and parse a single STAC item from its JSON URL."""
    from ._http import get_json

    return UmbraItem.from_dict(get_json(url), href=url)


def _png_bytes(image: Any) -> bytes:
    """Encode a ``PIL.Image`` as PNG bytes for an MCP image block."""
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def _search_source(local: bool | None) -> tuple[object, bool]:
    """Pick the search backend.

    ``local=True`` forces the on-disk index (error if absent); ``local=False``
    forces a live S3 walk; ``local=None`` (the default) uses the index when one
    exists at the default path and falls back to a live walk otherwise — the
    fast path for an agent that has run ``umbra index fetch``.
    """
    path = default_index_path()
    if local is True:
        if not path.exists():
            raise FileNotFoundError(
                f"No local index at {path}. Fetch the published snapshot with "
                "'umbra index fetch', or build one with 'umbra index build'."
            )
        return CatalogIndex(path), True
    if local is None and path.exists():
        return CatalogIndex(path), True
    return UmbraCatalog(), False


def _require_same_polarization(items: list[UmbraItem]) -> None:
    """Guard change detection against the documented polarization pitfall.

    An HH scene and a VV scene of the same place measure different scattering
    and must not be differenced. The deterministic layer refuses the mix rather
    than emit a misleading composite; the model never gets to override this.
    """
    pols = {p for item in items for p in (item.polarizations or [])}
    if len(pols) > 1:
        raise ValueError(
            f"Refusing to composite mixed polarizations {sorted(pols)}: HH and "
            "VV measure different scattering and cannot be compared for change. "
            "Filter to a single polarization first."
        )


# --------------------------------------------------------------------------
# Tool implementations (plain functions; registered on the server below).
# --------------------------------------------------------------------------


def search_catalog(
    bbox: list[float] | None = None,
    place: str | None = None,
    area: str | None = None,
    fuzzy: bool = False,
    start: str | None = None,
    end: str | None = None,
    products: list[str] | None = None,
    limit: int = 20,
    max_per_task: int | None = None,
    local: bool | None = None,
) -> dict[str, Any]:
    """Search Umbra's catalog and return compact context cards.

    Filters (all optional, combine freely): ``bbox`` as
    ``[min_lon, min_lat, max_lon, max_lat]`` in WGS84 degrees; ``place`` as a
    free-text name geocoded to a bbox; ``area`` as a substring of the Umbra
    task (site) name (set ``fuzzy`` to match it loosely -- word-order- and
    punctuation-independent and typo-tolerant, resolved deterministically with
    no model call, so ``"utah centerfield"`` still reaches ``"Centerfield,
    Utah"``); ``start``/``end`` as acquisition-date bounds -- an ISO
    ``YYYY-MM-DD`` date, a bare year/month (``2024``, ``2024-03``), or a
    relative expression (``today``, ``yesterday``, ``3 months ago``,
    ``last month``), resolved deterministically with no model call;
    ``products`` to restrict to product types
    (any of GEC, SICD, SIDD, CPHD); ``limit`` to cap results; ``max_per_task``
    to cap items per site (use 1 for a one-pin-per-site overview).

    ``local`` selects the backend: leave it unset to use the on-disk index when
    present (instant) and fall back to a live S3 walk otherwise. Returns the
    per-item context cards, not full STAC JSON, to protect the context window;
    call ``get_item`` for one item's full metadata.
    """
    resolved_bbox = tuple(bbox) if bbox else None
    resolved_place = None
    if place and resolved_bbox is None:
        resolved_bbox, resolved_place = _geocode_place(place)

    source, is_index = _search_source(local)
    try:
        results = source.search(
            bbox=resolved_bbox,
            start=start,
            end=end,
            product_types=list(products) if products else None,
            area=area,
            fuzzy=fuzzy,
            limit=limit,
            max_per_task=max_per_task,
        )
        cards = [item.to_llm_context() for item in results]
    finally:
        if is_index:
            source.close()

    return {
        "count": len(cards),
        "source": "local-index" if is_index else "live-catalog",
        "resolved_place": resolved_place,
        "resolved_bbox": list(resolved_bbox) if resolved_bbox else None,
        "items": cards,
        "attribution": ATTRIBUTION,
    }


def get_item(url: str) -> dict[str, Any]:
    """Return the full context card for one STAC item, given its JSON URL.

    The card (:meth:`UmbraItem.to_llm_context`) carries the id, ISO datetime,
    place, bbox, resolution, polarization *with the change-detection caveat*,
    the per-product-type explanations and asset URLs, and the mandatory
    attribution line — everything a model needs to reason about the scene.
    """
    return _fetch_item(url).to_llm_context()


def geocode_place(query: str) -> dict[str, Any]:
    """Resolve a free-text place name to a bounding box via Nominatim.

    Returns ``{"bbox": [min_lon, min_lat, max_lon, max_lat], "display_name":
    ...}`` so the agent can inspect the box before searching, or pass it
    straight to ``search_catalog``'s ``bbox``.
    """
    box, display = _geocode_place(query)
    return {"bbox": list(box), "display_name": display}


def index_stats() -> dict[str, Any]:
    """Report on the local catalog index, if one has been built or fetched.

    Lets a long-running agent decide whether searches will be instant (index
    present) or pay a live S3 walk, and how stale the snapshot is.
    """
    path = default_index_path()
    if not path.exists():
        return {
            "available": False,
            "path": str(path),
            "hint": (
                "No local index. Fetch the published weekly snapshot with "
                "'umbra index fetch' for instant whole-catalog search."
            ),
        }
    index = CatalogIndex(path)
    try:
        stats = dict(index.stats())
    finally:
        index.close()
    stats["available"] = True
    stats["path"] = str(path)
    return stats


def quicklook(url: str, asset: str = "GEC", db: bool = False, max_size: int = 1024) -> list[Any]:
    """Render a scene's quicklook PNG and return it as an image block.

    Streams the cloud-optimized product at ``url`` (default the analysis-ready
    ``GEC``), applies a SAR-appropriate stretch (set ``db=True`` for a decibel
    stretch), and returns the image so the model *sees* the radar scene, plus a
    text block with the scene id and attribution.
    """
    _, Image = _require_mcp()
    from . import viz

    item = _fetch_item(url)
    image = viz.quicklook(item, asset=asset, db=db, max_size=max_size)
    caption = f"Quicklook of {item.id} ({asset}). {ATTRIBUTION}"
    return [Image(data=_png_bytes(image), format="png"), caption]


def change_composite(
    urls: list[str], asset: str = "GEC", db: bool = False, max_size: int = 1024
) -> list[Any]:
    """Composite 2-3 passes of one site into a change image.

    Pass the STAC URLs of two or three acquisitions of the *same* site in time
    order. Colors encode change: bright green = signal appeared after the first
    date, magenta = signal vanished, grey = unchanged. Refuses to mix
    polarizations (HH vs VV are not comparable). Returns the image block plus a
    caption naming the color semantics and attribution.
    """
    _, Image = _require_mcp()
    from . import viz

    items = [_fetch_item(u) for u in urls]
    if len(items) < 2:
        raise ValueError("change_composite needs at least two item URLs.")
    _require_same_polarization(items)
    image = viz.change_composite(items, asset=asset, db=db, max_size=max_size)
    caption = (
        "Change composite (green = appeared, magenta = vanished, grey = "
        f"unchanged) over {len(items)} passes. {ATTRIBUTION}"
    )
    return [Image(data=_png_bytes(image), format="png"), caption]


def timescan(
    urls: list[str], asset: str = "GEC", db: bool = False, max_size: int = 1024
) -> list[Any]:
    """Summarize a whole time series into one activity image.

    Pass the STAC URLs of a site's passes; the timescan encodes per-pixel
    temporal statistics so bright/colored areas are *where activity happened*
    over the series. Returns the image block plus attribution.
    """
    _, Image = _require_mcp()
    from . import viz

    items = [_fetch_item(u) for u in urls]
    if len(items) < 2:
        raise ValueError("timescan needs at least two item URLs.")
    image = viz.timescan_composite(items, asset=asset, db=db, max_size=max_size)
    caption = f"Timescan over {len(items)} passes (color = temporal activity). {ATTRIBUTION}"
    return [Image(data=_png_bytes(image), format="png"), caption]


def download_asset(
    url: str, asset: str = "GEC", dest_dir: str = ".", confirm: bool = False
) -> dict[str, Any]:
    """Download one asset of an item to local disk, gated by a size check.

    SAR products can be multiple GB, so this is a two-step handshake: call it
    first with ``confirm=False`` (the default) to get the byte size without
    downloading, then again with ``confirm=True`` to actually fetch. Returns
    ``{asset, path, bytes}`` on download, or a size-and-hint object when
    confirmation is still required.
    """
    from ._http import DEFAULT_TIMEOUT, default_session
    from .download import download_asset as _download_asset

    item = _fetch_item(url)
    href = item.asset_href(asset)

    if not confirm:
        session = default_session()
        head = session.head(href, allow_redirects=True, timeout=DEFAULT_TIMEOUT)
        head.raise_for_status()
        length = head.headers.get("Content-Length")
        size = int(length) if length is not None else None
        return {
            "confirm_required": True,
            "asset": asset,
            "url": href,
            "bytes": size,
            "hint": (
                f"This asset is {size / 1e6:.1f} MB. " if size is not None else "Size unknown. "
            )
            + "Call again with confirm=true to download it.",
        }

    path = _download_asset(item, asset, dest_dir)
    return {"asset": asset, "path": str(path), "bytes": path.stat().st_size}


def watch_site(
    place: str | None = None,
    area: str | None = None,
    bbox: list[float] | None = None,
    fuzzy: bool = False,
    start: str | None = None,
    end: str | None = None,
    products: list[str] | None = None,
    name: str | None = None,
    reset: bool = False,
    local: bool | None = None,
) -> dict[str, Any]:
    """Report only the acquisitions **new** since the last check of this site.

    SAR's value for monitoring is its cadence, so the natural workflow is
    *standing*: run the same search each time and act only on what changed. This
    tool packages that idempotent delta — call it in one turn, come back a day
    (or a session) later and call it again, and it returns just the passes
    published in between, not the whole list again. The scheduler is you: ask the
    agent to check the site, and it reports the delta.

    Define the site with the same filters as ``search_catalog`` — ``place``
    (geocoded to a bbox), ``area`` (task/site-name substring, loosened by
    ``fuzzy``), ``bbox``, ``products``, and ``start``/``end`` date bounds. The
    watch is identified by a stable ``name`` derived from those filters (pass an
    explicit ``name`` to run several independent watches over the same site);
    ``reset=True`` re-establishes the baseline, reporting everything as new.

    State persists in the local catalog index's ``meta`` table (created on first
    use), so a watch survives across sessions with no extra setup. ``local``
    selects the search backend exactly as in ``search_catalog`` — leave it unset
    to use the on-disk index when present and a live S3 walk otherwise; for true
    monitoring a live walk (``local=false``) catches freshly published passes.
    **No model is called** — this is pure set arithmetic over the deterministic
    search — and the returned ``new_items`` are context cards ready to hand
    straight to ``change_composite`` / ``timescan``, closing the standing-analyst
    loop (new pass → composite → describe) without leaving the conversation.
    """
    resolved_bbox = tuple(bbox) if bbox else None
    resolved_place = None
    if place and resolved_bbox is None:
        resolved_bbox, resolved_place = _geocode_place(place)

    source, is_index = _search_source(local)
    # Watch state always lives in the local index's meta table (MetaWatchStore),
    # so a watch persists across MCP sessions. Reuse the index connection when
    # the search already opened one; otherwise open the index just for the store
    # (CatalogIndex creates the DB + meta table on first use).
    store_index = source if is_index else CatalogIndex(default_index_path())

    resolved_products = list(products) if products else None
    watch_name = name or watch_key(
        area=area,
        place=place,
        bbox=resolved_bbox,
        product_types=resolved_products,
        start=start,
        end=end,
        fuzzy=fuzzy,
    )
    try:
        result = watch(
            source,
            name=watch_name,
            store=MetaWatchStore(store_index),
            reset=reset,
            bbox=resolved_bbox,
            area=area,
            fuzzy=fuzzy,
            product_types=resolved_products,
            start=start,
            end=end,
        )
        payload = result.to_dict()
    finally:
        store_index.close()

    payload["source"] = "local-index" if is_index else "live-catalog"
    payload["resolved_place"] = resolved_place
    payload["resolved_bbox"] = list(resolved_bbox) if resolved_bbox else None
    return payload


def _scene_matches_payload(
    matches: list[Any], stored_model: str | None, query: dict[str, Any]
) -> dict[str, Any]:
    """Shape a list of :class:`~umbra_py.embed.SceneMatch` into a tool result.

    Each match is a pointer back to a real acquisition (id, task, datetime, STAC
    ``href``), never a model-authored fact — so the agent can hand a match's
    ``href`` straight to ``get_item`` / ``quicklook`` / ``change_composite`` to
    keep working. ``model`` records which embedding model the index was built
    with, and the attribution line rides along as with every other tool.
    """
    return {
        "query": query,
        "count": len(matches),
        "model": stored_model,
        "matches": [m.to_dict() for m in matches],
        "attribution": ATTRIBUTION,
    }


def find_similar(
    url: str,
    asset: str = "GEC",
    top_k: int = 10,
    min_score: float = 0.0,
    model: str | None = None,
) -> dict[str, Any]:
    """Find archived scenes that **look like** the acquisition at ``url``.

    This is the one search that lives in the pixels, not the metadata: where
    ``search_catalog`` filters by date/bbox/task-name, this renders the query
    item's quicklook, embeds it, and ranks the pre-embedded scene index by visual
    (cosine) similarity — "find scenes that look like this flooded field / crowded
    berth / runway". The query item is excluded from its own results.

    Requires a scene-embedding index built ahead of time with ``umbra embed
    build`` (a sidecar ``catalog.embed.db`` beside the catalog index) and a
    user-supplied multimodal embedding key (``OPENAI_API_KEY``, optionally
    ``OPENAI_BASE_URL`` / ``UMBRA_SCENE_EMBED_MODEL``) — the only model call is
    turning the query image into a vector; ranking is deterministic. ``asset``
    picks which product's quicklook to embed (default ``GEC``; match how the index
    was built); ``model`` must match the index's embedding model. Returns the
    ranked matches as compact records (each with the acquisition's ``href``, so
    hand it to ``get_item`` / ``quicklook`` / ``change_composite`` next) plus the
    attribution line.
    """
    from . import embed as emb

    path = emb.default_scene_embed_path()
    if not path.exists():
        raise FileNotFoundError(
            f"No scene-embedding index at {path}. Build one first with "
            "'umbra embed build' (image-to-image similarity search needs it)."
        )
    item = _fetch_item(url)
    embedder = emb.default_image_embedder(model=model)

    def render(it: UmbraItem) -> bytes:
        return emb._render_quicklook_asset(it, asset=asset)

    with emb.SceneEmbeddingIndex(path) as index:
        matches = index.similar_to_item(
            item, embedder=embedder, render=render, top_k=top_k, min_score=min_score
        )
        stored_model = index.stored_model()
    return _scene_matches_payload(
        matches, stored_model, {"kind": "image", "item_id": item.id, "asset": asset}
    )


def find_similar_text(
    query: str,
    top_k: int = 10,
    min_score: float = 0.0,
    model: str | None = None,
) -> dict[str, Any]:
    """Find archived scenes matching a plain-language ``query`` over their imagery.

    Text-to-scene search: embeds ``query`` ("ships at a berth", "a flooded field")
    and ranks the stored *image* vectors by cosine similarity. It only works when
    the index was built and this query is embedded with the **same** joint
    CLIP-family model whose text and image encoders share a vector space (a
    dimension mismatch is reported as an error; ``model`` selects it).

    Requires the same prebuilt ``catalog.embed.db`` (``umbra embed build``) and
    embedding key as ``find_similar``; the only model call is turning the text
    query into a vector. Returns the ranked matches as compact records (each with
    the acquisition's ``href`` for ``get_item`` / ``quicklook``) plus attribution.
    """
    from . import embed as emb

    path = emb.default_scene_embed_path()
    if not path.exists():
        raise FileNotFoundError(
            f"No scene-embedding index at {path}. Build one first with "
            "'umbra embed build' (text-to-scene search needs it)."
        )
    text_embedder = emb.default_text_embedder(model=model)
    with emb.SceneEmbeddingIndex(path) as index:
        matches = index.similar_to_text(query, text_embedder, top_k=top_k, min_score=min_score)
        stored_model = index.stored_model()
    return _scene_matches_payload(matches, stored_model, {"kind": "text", "text": query})


def describe_scene(
    url: str,
    asset: str = "GEC",
    db: bool = True,
    max_size: int = 1024,
    model: str | None = None,
) -> dict[str, Any]:
    """Return a grounded, plain-language reading of the SAR scene at ``url``.

    This is the C2 "VLM-in-the-loop" capability (``umbra describe``) on the MCP
    surface: it renders the acquisition's quicklook, sends that picture plus the
    item's :meth:`~umbra_py.UmbraItem.to_llm_context` metadata card to a vision
    model behind the packaged SAR-literacy prompt, and returns a validated
    ``{summary, observed_features[], confidence, caveats[]}`` reading. The value
    over the client simply looking at a ``quicklook`` image itself is the packaged
    prompt: it encodes the SAR reading rules a general model lacks (brightness is
    backscatter not sunlight, speckle is not structure, a dark patch may be radar
    shadow not water, one frame is not change), so the radar is read correctly.

    **This is the one tool on the server that consults a model** — every other
    tool is deterministic. It is a deliberate, opt-in exception that preserves the
    boundary the rest of the package holds (``docs/AI_INTEGRATION_IDEAS.md`` §A4):
    the picture and the metadata are produced deterministically, the model **only
    interprets** (its reply passes the deterministic ``parse_description``
    boundary — it never becomes a coordinate, a URL, or a filter), and every
    reading is stamped with the CC-BY attribution and an ``AI_PROVENANCE`` note so
    a model's reading of radar is never mistaken for a measurement.

    It requires the ``[ai]`` extra plus a user-supplied vision key
    (``ANTHROPIC_API_KEY`` or ``OPENAI_API_KEY``, optionally ``OPENAI_BASE_URL`` /
    ``UMBRA_DESCRIBE_MODEL``), and the ``[viz]`` extra for the render; it raises a
    helpful setup error when no key is configured, so it never runs implicitly.
    ``db=True`` (the default) reads the decibel stretch — the radiometrically
    correct SAR look; ``asset`` picks which product's quicklook to read (default
    ``GEC``); ``model`` overrides the configured model. To *see* the same scene the
    reading is of, call :func:`quicklook` on the same ``url``.
    """
    # Import the function directly: ``umbra_py.describe`` the attribute is the
    # re-exported function, not the submodule, so ``from . import describe`` would
    # bind the function. Its body still resolves ``default_describer`` / the render
    # from its own module globals, so the [ai]-key gating and offline stubbing work.
    from .describe import describe as _describe_scene

    item = _fetch_item(url)
    description = _describe_scene(item, model=model, asset=asset, max_size=max_size, db=db)
    return description.to_dict()


def main() -> None:
    """Entry point for the ``umbra-mcp`` console script / ``umbra mcp``."""
    build_server().run()


def build_server() -> FastMCP:
    """Construct and return the configured ``FastMCP`` server.

    Kept as a factory (rather than a module-level singleton) so tests can build
    and introspect it without side effects, and so the ``mcp`` import stays
    lazy — importing this module does not require the extra until you build.
    """
    FastMCP, _Image = _require_mcp()

    server = FastMCP(
        "umbra",
        instructions=(
            "Search and visualize Umbra's open SAR archive. Start by reading "
            "the 'umbra://context' resource for product-type and search "
            "semantics. Use search_catalog to find acquisitions (compact cards), "
            "get_item for one item's full metadata, and quicklook / "
            "change_composite / timescan to see the radar imagery. describe_scene "
            "returns a SAR-literate model reading of a scene (the one tool that "
            "consults a model, and only when an [ai] key is configured). All data "
            f"is {DATA_LICENSE}; keep the attribution line with any derived product."
        ),
    )

    for fn in (
        search_catalog,
        get_item,
        geocode_place,
        index_stats,
        quicklook,
        change_composite,
        timescan,
        download_asset,
        watch_site,
        find_similar,
        find_similar_text,
        describe_scene,
    ):
        server.add_tool(fn)

    @server.resource(
        "umbra://context",
        name="umbra-llm-context",
        description="Product-type table, search semantics and license rules for driving umbra-py.",
        mime_type="application/json",
    )
    def _context_resource() -> str:
        return json.dumps(llm_context(), indent=2)

    @server.resource(
        "umbra://index/stats",
        name="umbra-index-stats",
        description="Status of the local catalog index (built/fetched, item count, staleness).",
        mime_type="application/json",
    )
    def _index_resource() -> str:
        return json.dumps(index_stats(), indent=2)

    @server.prompt(
        name="monitor-site",
        description="Workflow: find a site's passes and composite the latest change.",
    )
    def monitor_site(place: str, start: str | None = None, end: str | None = None) -> str:
        window = f" between {start} and {end}" if start or end else ""
        return (
            f"Monitor '{place}' for change{window} using the umbra tools:\n"
            f"1. search_catalog(place='{place}'"
            f"{f', start={start!r}' if start else ''}"
            f"{f', end={end!r}' if end else ''}) to list the site's passes.\n"
            "2. Pick the two or three most recent passes of the same "
            "polarization (see each card's polarization_caveat).\n"
            "3. change_composite(urls=[...]) on their stac_href URLs.\n"
            "4. Describe what the green/magenta regions imply about activity, "
            "citing the acquisition dates and keeping the attribution line."
        )

    @server.prompt(
        name="watch-site",
        description="Standing workflow: report only passes new since last check, then composite.",
    )
    def watch_site_prompt(place: str, start: str | None = None, end: str | None = None) -> str:
        window = f" between {start} and {end}" if start or end else ""
        args = f"place='{place}', local=False"
        if start:
            args += f", start={start!r}"
        if end:
            args += f", end={end!r}"
        return (
            f"Check '{place}' for newly published passes{window} using the umbra tools:\n"
            f"1. watch_site({args}) — it returns only the acquisitions new since the "
            "last check (all of them on the first run), persisting state so a later "
            "re-check reports just the delta.\n"
            "2. If new_count is 0, report that nothing new has arrived and stop.\n"
            "3. Otherwise pick two or three recent passes of the same polarization "
            "(see each new item's polarization_caveat) and change_composite(urls=[...]) "
            "on their stac_href URLs.\n"
            "4. Describe what the green/magenta regions imply about activity since the "
            "last check, citing the acquisition dates and keeping the attribution line."
        )

    @server.prompt(
        name="find-similar-scenes",
        description="Workflow: find archived scenes that look like an acquisition, then view them.",
    )
    def find_similar_scenes(url: str) -> str:
        return (
            f"Find archived scenes that look like the acquisition at {url} using the "
            "umbra tools (needs a scene index built with 'umbra embed build'):\n"
            f"1. find_similar(url='{url}') — ranks the pre-embedded archive by visual "
            "similarity to this scene's quicklook and returns the closest matches.\n"
            "2. If the result's count is 0, report that nothing cleared the similarity "
            "threshold (or that the index has not been built) and stop.\n"
            "3. Otherwise quicklook(url=<match.href>) on the top one or two matches so "
            "you can see them, and summarize what the matched scenes have in common, "
            "citing their acquisition dates and keeping the attribution line."
        )

    @server.prompt(
        name="describe-scene",
        description="Workflow: view a scene's quicklook and read it with SAR literacy.",
    )
    def describe_scene_prompt(url: str) -> str:
        return (
            f"Read the SAR acquisition at {url} using the umbra tools (the reading "
            "needs an [ai] vision key configured on the server):\n"
            f"1. quicklook(url='{url}') so you can see the radar scene yourself.\n"
            f"2. describe_scene(url='{url}') — a vision model reads it behind the "
            "packaged SAR-literacy prompt and returns a grounded "
            "{summary, observed_features, confidence, caveats} object stamped as an "
            "AI interpretation with the CC-BY attribution.\n"
            "3. Present the reading, noting it is an AI interpretation of radar (not "
            "a measurement) and keeping the attribution line. Remember SAR rules: "
            "bright is backscatter not sunlight, a dark patch may be shadow not "
            "water, and one frame shows no change over time."
        )

    @server.prompt(
        name="survey-region",
        description="Workflow: survey what Umbra has imaged over a region.",
    )
    def survey_region(place: str) -> str:
        return (
            f"Survey Umbra's coverage of '{place}':\n"
            f"1. geocode_place('{place}') to get the bounding box.\n"
            "2. search_catalog(bbox=..., max_per_task=1) for a one-pin-per-site "
            "overview, then again without max_per_task for a promising site's "
            "full time series.\n"
            "3. quicklook(url=...) on a representative scene and summarize the "
            "products, dates and resolutions available, keeping attribution."
        )

    return server


if __name__ == "__main__":  # pragma: no cover
    main()
