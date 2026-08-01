"""Serving and publishing the archive: ``mcp``, ``serve``, ``demo``,
``tiles``, ``showcase``.

The commands that stand something up rather than write a single artifact: the
MCP server, the read-only STAC API, the self-serve explorer, the whole-catalog
PMTiles archive, and the static showcase that composes them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import click

from .._spinner import OrbitSpinner
from ..constants import PRODUCT_ASSETS
from ..models import UmbraItem
from ..pmtiles import DEFAULT_COG_ASSET, FOOTPRINT_MIN_ZOOM
from ..serve import STACK_SCHEDULERS
from ..showcase import DEFAULT_FEATURED_VIEW, FEATURED_VIEW_NAMES, FEATURED_VIEWS
from . import _shared
from ._root import cli


@cli.command()
def mcp() -> None:
    """Run the umbra Model Context Protocol server (stdio transport).

    Exposes search / geocode / quicklook / change / timescan as MCP tools so an
    MCP client (Claude Desktop / Code and others) can drive the archive in
    natural language. Requires the ``mcp`` extra (``pip install 'umbra-py[mcp]'``);
    also runnable as ``umbra-mcp`` or ``uvx umbra-mcp``.
    """
    from ..exceptions import MissingDependencyError
    from ..mcp_server import main as run_server

    try:
        run_server()
    except MissingDependencyError as exc:
        raise click.ClickException(str(exc)) from exc


@cli.command()
@click.option("--host", default="127.0.0.1", show_default=True, help="Interface to bind.")
@click.option("--port", default=8000, show_default=True, type=int, help="Port to listen on.")
@click.option(
    "--db",
    "index_path",
    default=None,
    help="Catalog index to serve (default: the shared index path). "
    "Fetch one first with 'umbra index fetch'.",
)
@click.option(
    "--live",
    is_flag=True,
    help="Serve from a live S3 walk per request instead of a local index "
    "(correct but slow; for a quick try without building an index).",
)
@click.option(
    "--artifacts/--no-artifacts",
    default=True,
    show_default=True,
    help="Mount the on-demand artifact endpoints (/artifacts/quicklook, /change, "
    "/timescan, /swipe, /stats). Use --no-artifacts for a public instance that "
    "wants to bound COG-streaming egress.",
)
@click.option(
    "--stack-lazy",
    is_flag=True,
    help="Build POST /artifacts/stats' datacube lazily (one dask task per pass) "
    "so a long series is measured a slice at a time instead of held whole. "
    "Needs the 'dask' extra on the server; the numbers are identical either way.",
)
@click.option(
    "--stack-chunk-size",
    type=int,
    default=None,
    help="With --stack-lazy, also cut each pass into N-square windows read "
    "independently, so one scene need not fit in memory either. Costs one range "
    "read per window instead of one per pass, and is what lets a stats request "
    'ask for "windowed": true (measured window by window, estimated percentiles).',
)
@click.option(
    "--stack-scheduler",
    type=click.Choice(STACK_SCHEDULERS),
    default="synchronous",
    show_default=True,
    help="With --stack-lazy, which dask scheduler evaluates the chunks: "
    "'synchronous' on the request's own worker, or 'threads' for dask's thread "
    "pool (faster per request, multiplies under concurrent ones).",
)
@click.option(
    "--cache-dir",
    default=None,
    help="Directory for cached render artifacts (default: alongside the index).",
)
def serve(
    host,
    port,
    index_path,
    live,
    artifacts,
    stack_lazy,
    stack_chunk_size,
    stack_scheduler,
    cache_dir,
) -> None:
    """Run a read-only STAC API over the catalog index (HTTP server).

    Umbra publishes a static STAC catalog and no search API, so the standard
    STAC tooling (pystac-client, the QGIS STAC plugin, stac-browser, leafmap)
    has nothing to query. This serves ``/search``, ``/collections`` and
    ``/collections/{id}/items`` -- plus an OpenAPI doc at ``/docs`` -- over the
    local index, turning umbra-py into the STAC API bridge for the open archive.

    It also renders artifacts on demand over any site: a quicklook
    (``GET /artifacts/quicklook/{id}.png``), a change composite
    (``POST /artifacts/change``) or a timescan (``POST /artifacts/timescan``),
    each cached to disk by its inputs -- and answers the same change question in
    *numbers* at ``POST /artifacts/stats``, the ``umbra stack --stats``
    reduction (per-pass decibel statistics, changed area in km², and with
    ``"blocks": N`` which part of the site moved) over HTTP. That last one is
    the only endpoint whose cost grows with the *number* of acquisitions, so
    ``--stack-lazy`` (plus ``--stack-chunk-size``) gives it the same memory
    ceiling-lift ``umbra stack --lazy`` has -- an instance-wide setting, since
    it needs the ``dask`` extra here on the server. On a chunked instance a
    request may also send ``"windowed": true`` to be *measured* in those windows
    (``umbra stack --stats-windowed``), which is a request field rather than a
    policy because it estimates the percentiles it no longer holds a pass for.
    An *unchunked* instance honours the complementary field: ``"speckle_filter":
    "boxcar" | "lee"`` (``umbra stack --speckle-filter``) averages speckle down
    on the shared grid before anything is measured, which needs each pass whole.
    Requires the ``serve`` extra (``pip install 'umbra-py[serve]'``).
    """
    from ..exceptions import MissingDependencyError
    from ..serve import StackExecution
    from ..serve import serve as run_stac_server

    try:
        execution = StackExecution(
            lazy=stack_lazy, chunk_size=stack_chunk_size, scheduler=stack_scheduler
        )
    except ValueError as exc:
        raise click.BadParameter(str(exc), param_hint="--stack-chunk-size") from exc

    click.echo(f"Serving Umbra STAC API on http://{host}:{port}  (docs at /docs)")
    if artifacts:
        click.echo(f"  /artifacts/stats datacube: {execution.describe()}")
        # The client-visible consequence of the policy, and it cuts both ways:
        # with windows to walk a request may ask to be measured in them, and
        # without them it may ask for the filter that needs a pass whole.
        if execution.chunk_size:
            click.echo('    requests may send "windowed": true (estimated percentiles)')
        else:
            click.echo('    requests may send "speckle_filter": "boxcar" | "lee"')
        # And the client learns the same thing without asking: the landing
        # page's "stats" link reports both options and the reason for the one
        # this instance cannot honour.
        click.echo('    advertised on / as the "stats" link\'s "umbra:options"')
    try:
        run_stac_server(
            host=host,
            port=port,
            index_path=index_path,
            live=live,
            artifacts=artifacts,
            stack_execution=execution,
            cache_dir=cache_dir,
        )
    except MissingDependencyError as exc:
        raise click.ClickException(str(exc)) from exc


@cli.command()
@click.option("--bbox", help="Footprint filter: 'min_lon,min_lat,max_lon,max_lat'.")
@click.option(
    "--place",
    default=None,
    help="Geocode a place name (e.g. 'California', 'Tokyo') to a bounding box "
    "and gather items within it, via OpenStreetMap Nominatim. Mutually "
    "exclusive with --bbox.",
)
@_shared._geometry_option
@click.option(
    "--start",
    help="Earliest acquisition date. Accepts YYYY-MM-DD, a year or month "
    "(2024, 2024-03), or a relative expression ('today', 'yesterday', "
    "'3 months ago', 'last month').",
)
@click.option(
    "--end",
    help="Latest acquisition date (same formats as --start; a bare year, month "
    "or period like 'last month' snaps to that span's last day).",
)
@_shared._area_option
@click.option(
    "--product",
    "products",
    multiple=True,
    type=click.Choice(PRODUCT_ASSETS, case_sensitive=False),
    help="Keep items exposing this asset (repeatable). The explorer also lets "
    "you toggle product types client-side once the page is open.",
)
@click.option("--limit", type=int, default=500, show_default=True, help="Max acquisitions to load.")
@click.option(
    "--max-per-task",
    type=int,
    default=None,
    help="Cap items per Umbra task directory. '--max-per-task 1' gives one "
    "marker per distinct site -- a fast whole-archive overview.",
)
@click.option("--out", "out_path", required=True, help="Output HTML file (e.g. demo.html).")
@click.option(
    "--asset",
    default="GEC",
    show_default=True,
    type=click.Choice(PRODUCT_ASSETS, case_sensitive=False),
    help="Product the on-click 'Get SAR image' button streams. GEC (the "
    "detected GeoTIFF) is the sensible default; CSI also works.",
)
@click.option(
    "--no-lazy-imagery",
    "lazy_imagery",
    is_flag=True,
    default=True,
    flag_value=False,
    help="Build a metadata-only explorer without the on-click SAR overlay "
    "button (no geotiff.js CDN dependency at click time).",
)
@click.option(
    "--percentile",
    default="2,98",
    show_default=True,
    help="Low,high percentile cut for the on-click SAR overlay's contrast stretch.",
)
@click.option(
    "--server-url",
    default=None,
    help="Base URL of a running 'umbra serve' instance (e.g. "
    "http://localhost:8000). When set, the explorer gains an 'Analyze this "
    "view' panel whose buttons render change/timescan/swipe products over the "
    "currently-filtered acquisitions on demand, plus a Quantify button that "
    "measures the same view in numbers. Omit for a fully static page.",
)
@click.option(
    "--pmtiles",
    "pmtiles_url",
    default=None,
    help="URL or page-relative path of a whole-catalog .pmtiles archive (from "
    "'umbra tiles' / 'umbra tiles --fetch'). The explorer then draws EVERY "
    "acquisition in that archive from vector tiles read on demand instead of an "
    "embedded search slice -- the whole-archive explorer, in a page that stays a "
    "few KB. Footprint outlines come from the archive's footprint polygons where "
    "it carries them, and the on-click 'Get SAR image' overlay works for any "
    "acquisition the archive references a COG for. The search options don't "
    "apply in this mode -- filter in the page instead.",
)
@_shared._local_index_options
@_shared._fuzzy_option
def demo(
    bbox,
    place,
    intersects,
    start,
    end,
    area,
    fuzzy,
    products,
    limit,
    max_per_task,
    out_path,
    asset,
    lazy_imagery,
    percentile,
    server_url,
    pmtiles_url,
    local,
    db_path,
) -> None:
    """Build a self-serve interactive catalog explorer as one HTML page.

    Unlike the one-shot artifacts the other visual commands emit, this is an
    *application*: a single self-contained page over the whole gathered slice of
    the catalog with client-side filters (search box, date range, product-type
    and polarization chips), clustered markers that scale past a plain map's polygon ceiling, and
    a click-to-quicklook SAR overlay streamed on demand. Reads a prebuilt index
    with --local for a near-instant, offline build. Needs no extra: the page is
    pure HTML, and Leaflet + the on-click COG decode run browser-side from
    pinned CDNs.

    Pass --server-url pointing at a running 'umbra serve' to add an "Analyze
    this view" panel that renders change/timescan/swipe products over the
    currently-filtered acquisitions on demand (the server does the raster work
    and caches results), and a Quantify button that measures them instead:
    how many decibels the site moved first-to-last, how much ground crossed
    the change threshold in km2, and which block moved most, when. Without
    --server-url the page stays fully static.

    Pass --pmtiles PATH-OR-URL to explore the WHOLE archive instead of a
    gathered slice: the page draws every acquisition in a '.pmtiles' catalog
    (from 'umbra tiles') as a MapLibre vector layer read by range request, so
    the same sidebar filters cover the entire catalog from a page that stays a
    few KB. Nothing is searched or embedded in that mode.
    """
    if not out_path.lower().endswith((".html", ".htm")):
        raise click.ClickException("Explorer output must be an .html file.")

    from ..demo import save_demo  # noqa: PLC0415

    if pmtiles_url:
        # The archive *is* the data source, so a search would be gathered and
        # thrown away. Refuse rather than silently ignore the flags -- a user who
        # asked for a filtered explorer must not get an unfiltered one.
        ignored = {
            "--bbox": bbox,
            "--place": place,
            "--intersects": intersects,
            "--start": start,
            "--end": end,
            "--area": area,
            "--product": products,
            "--max-per-task": max_per_task,
            "--local": local,
            "--index-db": db_path,
        }
        named = [flag for flag, value in ignored.items() if value]
        if named:
            raise click.ClickException(
                f"--pmtiles draws the whole archive from vector tiles, so "
                f"{', '.join(named)} would have no effect. Drop them and filter "
                f"in the page, or drop --pmtiles to explore a searched slice."
            )
        path = save_demo(
            [],
            out_path,
            subtitle="Every acquisition in the tiled open-data catalog.",
            server_url=server_url,
            pmtiles_url=pmtiles_url,
            lazy_imagery=lazy_imagery,
            percentile=_shared._parse_percentile(percentile),
        )
        click.echo(f"Wrote whole-archive explorer over {pmtiles_url} to {path}")
        return

    search_bbox, search_geometry = _shared._resolve_geography(bbox, place, intersects)
    items = _shared._gather_items(
        local=local,
        db_path=db_path,
        bbox=search_bbox,
        intersects=search_geometry,
        start=start,
        end=end,
        area=area,
        fuzzy=fuzzy,
        product_types=list(products) or None,
        limit=limit,
        max_per_task=max_per_task,
    )
    if not items:
        raise click.ClickException("No items matched the search.")

    with OrbitSpinner(f"Building explorer over {len(items)} acquisition(s)"):
        path = save_demo(
            items,
            out_path,
            asset=asset,
            lazy_imagery=lazy_imagery,
            percentile=_shared._parse_percentile(percentile),
            subtitle=_shared._search_subtitle(place or area, bbox, start, end),
            server_url=server_url,
        )
    click.echo(f"Wrote interactive explorer over {len(items)} acquisition(s) to {path}")


def _tiles_fetch(out_path, viewer_path, fetch_url, default_pmtiles_path, fetch_prebuilt_pmtiles):
    """Download the published whole-catalog PMTiles basemap (``umbra tiles --fetch``)."""
    if out_path is not None and not out_path.lower().endswith(".pmtiles"):
        raise click.ClickException("Tiles output must be a .pmtiles file.")
    dest = Path(out_path) if out_path else default_pmtiles_path()

    with OrbitSpinner("Fetching prebuilt catalog basemap") as spinner:

        def tally(done: int, total: int | None) -> None:
            if total:
                spinner.label = f"Fetching prebuilt catalog basemap ({done / total:.0%})"
            else:
                spinner.label = f"Fetching prebuilt catalog basemap ({done / 1e6:.0f} MB)"

        path = fetch_prebuilt_pmtiles(dest, url=fetch_url, progress=tally)
    size_mb = path.stat().st_size / 1e6
    click.echo(f"Fetched prebuilt PMTiles basemap to {path} ({size_mb:.2f} MB)")

    if viewer_path is not None:
        from ..pmtiles import save_viewer  # noqa: PLC0415

        vpath = save_viewer(path.name, viewer_path)
        click.echo(f"Wrote MapLibre viewer to {vpath}")


@cli.command()
@click.option("--bbox", help="Footprint filter: 'min_lon,min_lat,max_lon,max_lat'.")
@click.option(
    "--place",
    default=None,
    help="Geocode a place name to a bounding box and tile items within it, via "
    "OpenStreetMap Nominatim. Mutually exclusive with --bbox.",
)
@_shared._geometry_option
@click.option(
    "--start",
    help="Earliest acquisition date (YYYY-MM-DD, a year/month, or a relative "
    "expression like '3 months ago').",
)
@click.option(
    "--end",
    help="Latest acquisition date (same formats as --start; a bare year/month "
    "snaps to that span's last day).",
)
@click.option(
    "--area",
    default=None,
    help="Case-insensitive name of an Umbra task/site to tile (e.g. "
    "'Centerfield'). Faster than a broad scan.",
)
@click.option(
    "--product",
    "products",
    multiple=True,
    type=click.Choice(PRODUCT_ASSETS, case_sensitive=False),
    help="Keep items exposing this asset (repeatable).",
)
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Max acquisitions to tile (default: all). The whole catalog is the "
    "point of tiling, so leave unset with --local for the full archive.",
)
@click.option(
    "--max-per-task",
    type=int,
    default=None,
    help="Cap items per Umbra task directory ('--max-per-task 1' tiles one point "
    "per distinct site).",
)
@click.option(
    "--out",
    "out_path",
    default=None,
    help="Output PMTiles archive (e.g. catalog.pmtiles). Required unless "
    "--fetch is given, where it is the download destination "
    "(default: the cached basemap path beside the index).",
)
@click.option(
    "--fetch",
    is_flag=True,
    help="Skip tiling: download the prebuilt whole-catalog basemap published "
    "weekly on the catalog-index release (no crawl, no index needed). Writes to "
    "--out if given, else the default cache path.",
)
@click.option(
    "--url",
    "fetch_url",
    default=None,
    help="With --fetch, override the release asset URL (advanced -- e.g. to pull from a fork).",
)
@click.option(
    "--min-zoom",
    type=int,
    default=0,
    show_default=True,
    help="Lowest zoom level to generate (world view).",
)
@click.option(
    "--max-zoom",
    type=int,
    default=9,
    show_default=True,
    help="Highest zoom level to generate. 9 reaches city scale, where SAR sites "
    "read individually; raise it for denser sites at the cost of a larger file.",
)
@click.option(
    "--footprints/--no-footprints",
    default=True,
    show_default=True,
    help="Also tile each acquisition's footprint polygon (clipped per tile) so a "
    "zoomed-in map shows coverage shape, not just a marker. --no-footprints "
    "writes a smaller centroids-only archive.",
)
@click.option(
    "--footprint-min-zoom",
    type=int,
    default=FOOTPRINT_MIN_ZOOM,
    show_default=True,
    help="Lowest zoom carrying footprint polygons. Below it a footprint is "
    "sub-pixel, so tiling it only inflates the tiles a viewer loads first.",
)
@click.option(
    "--cog-asset",
    default=DEFAULT_COG_ASSET,
    show_default=True,
    type=click.Choice(PRODUCT_ASSETS, case_sensitive=False),
    help="Product whose cloud-optimized GeoTIFF each tiled acquisition "
    "references, so a viewer ('umbra demo --pmtiles') can stream the picture on "
    "click. GEC is the detected, map-projected GeoTIFF; CSI also works.",
)
@click.option(
    "--no-cog",
    is_flag=True,
    default=False,
    help="Tile metadata only, with no image reference (a smaller archive whose "
    "viewers show no 'Get SAR image' button).",
)
@click.option(
    "--viewer",
    "viewer_path",
    default=None,
    help="Also write a self-contained MapLibre GL viewer HTML that renders the "
    "archive (points the page at the .pmtiles by its filename, so host them "
    "side by side).",
)
@_shared._local_index_options
@_shared._fuzzy_option
def tiles(
    bbox,
    place,
    intersects,
    start,
    end,
    area,
    fuzzy,
    products,
    limit,
    max_per_task,
    out_path,
    fetch,
    fetch_url,
    min_zoom,
    max_zoom,
    footprints,
    footprint_min_zoom,
    cog_asset,
    no_cog,
    viewer_path,
    local,
    db_path,
) -> None:
    """Tile the whole catalog into a single-file PMTiles vector archive.

    Where 'umbra map' and 'umbra demo' embed every footprint in the page (great
    up to a few thousand items), this pre-cuts the catalog into a vector tile
    pyramid so a map fetches only the tiles in view -- the fast, zoom-anywhere
    whole-archive answer. Each acquisition is tiled as a centroid at every zoom
    and (unless --no-footprints) as its clipped footprint polygon from
    --footprint-min-zoom down, so zooming in shows coverage shape. Each feature
    also references its --cog-asset cloud-optimized GeoTIFF, so a viewer over the
    archive ('umbra demo --pmtiles') can stream the actual radar picture on
    click. The output is one .pmtiles file: drop it on GitHub Pages or in a
    bucket, no tile server. With --viewer it also writes a MapLibre GL page that
    renders it.

    Skip the tiling entirely with --fetch: the weekly index workflow publishes a
    ready-made whole-catalog 'catalog.pmtiles' on the catalog-index release, so a
    fresh install gets the same basemap with no crawl and no index -- the visual
    sibling of 'umbra index fetch'.

    Needs no extra: the encoder is pure standard library, and the viewer's map
    runs browser-side from pinned CDNs. Use --local for a near-instant build
    from a prebuilt index.
    """
    if viewer_path is not None and not viewer_path.lower().endswith((".html", ".htm")):
        raise click.ClickException("Viewer output must be an .html file.")

    from ..pmtiles import (  # noqa: PLC0415
        default_pmtiles_path,
        fetch_prebuilt_pmtiles,
        save_viewer,
        write_pmtiles,
    )

    if fetch:
        _tiles_fetch(out_path, viewer_path, fetch_url, default_pmtiles_path, fetch_prebuilt_pmtiles)
        return

    if fetch_url is not None:
        raise click.ClickException("--url only applies with --fetch.")
    if not out_path:
        raise click.ClickException("--out is required unless --fetch is given.")
    if not out_path.lower().endswith(".pmtiles"):
        raise click.ClickException("Tiles output must be a .pmtiles file.")

    search_bbox, search_geometry = _shared._resolve_geography(bbox, place, intersects)
    items = _shared._gather_items(
        local=local,
        db_path=db_path,
        bbox=search_bbox,
        intersects=search_geometry,
        start=start,
        end=end,
        area=area,
        fuzzy=fuzzy,
        product_types=list(products) or None,
        limit=limit,
        max_per_task=max_per_task,
    )
    if not items:
        raise click.ClickException("No items matched the search.")

    with OrbitSpinner(f"Tiling {len(items)} acquisition(s) (z{min_zoom}-z{max_zoom})"):
        path = write_pmtiles(
            items,
            out_path,
            min_zoom=min_zoom,
            max_zoom=max_zoom,
            footprints=footprints,
            footprint_min_zoom=footprint_min_zoom,
            cog_asset=None if no_cog else cog_asset,
        )
    size_mb = path.stat().st_size / 1e6
    click.echo(f"Wrote PMTiles archive of {len(items)} acquisition(s) to {path} ({size_mb:.2f} MB)")

    if viewer_path is not None:
        vpath = save_viewer(Path(out_path).name, viewer_path)
        click.echo(f"Wrote MapLibre viewer to {vpath}")


@cli.command()
@click.option("--bbox", help="Footprint filter: 'min_lon,min_lat,max_lon,max_lat'.")
@click.option(
    "--place",
    default=None,
    help="Geocode a place name to a bounding box and gather the explorer's "
    "items within it, via OpenStreetMap Nominatim. Mutually exclusive with --bbox.",
)
@_shared._geometry_option
@click.option("--start", help="Earliest acquisition date for the explorer (see 'umbra demo').")
@click.option("--end", help="Latest acquisition date for the explorer (see 'umbra demo').")
@click.option(
    "--area",
    default=None,
    help="Case-insensitive Umbra task/site name to gather for the explorer.",
)
@click.option(
    "--product",
    "products",
    multiple=True,
    type=click.Choice(PRODUCT_ASSETS, case_sensitive=False),
    help="Keep explorer items exposing this asset (repeatable).",
)
@click.option(
    "--limit", type=int, default=2000, show_default=True, help="Max acquisitions to load."
)
@click.option(
    "--max-per-task",
    type=int,
    default=1,
    show_default=True,
    help="Cap items per Umbra task directory. The default '1' gives one marker "
    "per distinct site -- a fast whole-archive overview fit for a landing page.",
)
@click.option(
    "--out",
    "out_dir",
    required=True,
    help="Output directory for the showcase site (index.html + map/explore pages).",
)
@click.option(
    "--pmtiles",
    "pmtiles_path",
    default=None,
    help="Local whole-catalog .pmtiles basemap to include (copied in beside a "
    "MapLibre viewer). Mutually exclusive with --fetch-pmtiles.",
)
@click.option(
    "--fetch-pmtiles",
    is_flag=True,
    help="Download the published whole-catalog 'catalog.pmtiles' basemap into "
    "the showcase (the same artifact 'umbra tiles --fetch' pulls) instead of "
    "supplying one with --pmtiles.",
)
@click.option("--pmtiles-url", default=None, help="Override the --fetch-pmtiles asset URL.")
@click.option(
    "--unified",
    is_flag=True,
    help="Build ONE page instead of two: the explorer reads the .pmtiles "
    "archive directly, so it covers every acquisition (with the filters) and "
    "the separate map.html is dropped. Needs a basemap (--pmtiles / "
    "--fetch-pmtiles); the explorer's search options don't apply.",
)
@click.option(
    "--no-explore",
    "explore",
    is_flag=True,
    default=True,
    flag_value=False,
    help="Skip building the interactive 'umbra demo' explorer page (a "
    "map-only showcase). By default an explorer is built from the gathered slice.",
)
@click.option(
    "--asset",
    default="GEC",
    show_default=True,
    type=click.Choice(PRODUCT_ASSETS, case_sensitive=False),
    help="Product the explorer's on-click 'Get SAR image' button streams.",
)
@click.option(
    "--no-lazy-imagery",
    "lazy_imagery",
    is_flag=True,
    default=True,
    flag_value=False,
    help="Build the explorer metadata-only (no on-click SAR overlay button).",
)
@click.option(
    "--featured",
    type=int,
    default=0,
    show_default=True,
    help="Precompute an artifact for this many repeat-imaged sites and show "
    "them as a gallery on the landing page. Needs the 'viz' extra and streams "
    "each scene's overview; 0 (the default) skips the gallery.",
)
@click.option(
    "--featured-view",
    type=click.Choice(FEATURED_VIEW_NAMES),
    default=DEFAULT_FEATURED_VIEW,
    show_default=True,
    help="What to precompute per featured site: 'change' (a 2/3-date "
    "composite), 'timescan' (the whole series collapsed to temporal "
    "statistics, needs 3+ passes) or 'swipe' (an interactive before/after "
    "page linked from the gallery).",
)
@click.option(
    "--featured-area",
    "featured_areas",
    multiple=True,
    help="Curate a featured site by name instead of auto-selecting (repeatable, "
    "matched like --area). Implies --featured for the sites named.",
)
@click.option(
    "--featured-frames",
    type=click.Choice(["2", "3"]),
    default="2",
    show_default=True,
    help="Passes per featured composite: 2 (green=new, magenta=gone) or 3 "
    "(temporal RGB). Applies to --featured-view change only.",
)
@click.option(
    "--featured-limit",
    type=int,
    default=1500,
    show_default=True,
    help="Size of the candidate pool the auto-selected featured sites are "
    "chosen from (tasks are scanned in name order).",
)
@click.option("--title", default=None, help="Override the landing-page title.")
@click.option("--tagline", default=None, help="Override the landing-page one-line pitch.")
@click.option(
    "--updated",
    default=None,
    help="Freshness stamp shown on the landing page (e.g. the index build date).",
)
@_shared._local_index_options
@_shared._fuzzy_option
def showcase(
    bbox,
    place,
    intersects,
    start,
    end,
    area,
    fuzzy,
    products,
    limit,
    max_per_task,
    out_dir,
    pmtiles_path,
    fetch_pmtiles,
    pmtiles_url,
    unified,
    explore,
    asset,
    lazy_imagery,
    featured,
    featured_view,
    featured_areas,
    featured_frames,
    featured_limit,
    title,
    tagline,
    updated,
    local,
    db_path,
) -> None:
    """Assemble a static, hostable showcase site into a directory.

    This composes the pieces the other visual commands already produce into one
    self-contained folder you drop on any static host (GitHub Pages, a bucket):

    \b
      index.html    a landing page linking the pieces below + install/docs/source
      map.html      a MapLibre viewer over the whole-catalog PMTiles basemap
      explore.html  the interactive 'umbra demo' catalog explorer
      featured/     precomputed artifacts (with --featured / --featured-area)

    Give the basemap with --pmtiles PATH, or --fetch-pmtiles to pull the
    published 'catalog.pmtiles' (the same artifact 'umbra tiles --fetch' fetches).
    The explorer is built from a gathered slice of the catalog (--local answers
    from a prebuilt index in milliseconds; --max-per-task 1, the default, gives a
    one-pin-per-site overview); pass --no-explore for a map-only showcase.

    --unified collapses the two map pages into one: the explorer reads the
    .pmtiles archive itself, so a visitor gets every acquisition in the catalog
    *and* the live filters on a single page, and map.html is not written. That
    needs a basemap and ignores the explorer's search options -- nothing is
    gathered, because the archive is the data source.

    --featured N precomputes an artifact for the N most repeat-imaged sites in
    the catalog (or name them yourself with repeated --featured-area) and puts
    them on the landing page, so a first-time visitor sees what SAR change looks
    like with no render round-trip. --featured-view picks which artifact: a
    'change' composite (the default), a whole-series 'timescan' composite, or an
    interactive before/after 'swipe' page the gallery links to. That step alone
    needs the 'viz' extra and streams each scene's overview; without it every
    page is self-contained HTML, so this runs in a core install and is the front
    end the '.github/workflows/docs.yml' Pages deploy publishes beside the docs.
    """
    if pmtiles_path and fetch_pmtiles:
        raise click.ClickException("Pass either --pmtiles or --fetch-pmtiles, not both.")
    if pmtiles_url is not None and not fetch_pmtiles:
        raise click.ClickException("--pmtiles-url only applies with --fetch-pmtiles.")
    if featured < 0:
        raise click.ClickException("--featured must be zero or more.")
    if unified and not (pmtiles_path or fetch_pmtiles):
        raise click.ClickException(
            "--unified builds the explorer over the tiled archive, so it needs a "
            "basemap: pass --pmtiles PATH or --fetch-pmtiles."
        )
    if unified and not explore:
        raise click.ClickException("--unified and --no-explore ask for opposite things.")

    from ..showcase import assemble_showcase  # noqa: PLC0415

    dest = Path(out_dir)
    dest.mkdir(parents=True, exist_ok=True)

    # Resolve the basemap: a supplied file, a fetched published snapshot, or none.
    basemap: Path | None = None
    if pmtiles_path:
        basemap = Path(pmtiles_path)
        if not basemap.exists():
            raise click.ClickException(f"PMTiles file not found: {basemap}")
    elif fetch_pmtiles:
        from ..pmtiles import fetch_prebuilt_pmtiles  # noqa: PLC0415

        basemap = dest / "catalog.pmtiles"
        with OrbitSpinner("Fetching prebuilt catalog basemap") as spinner:

            def tally(done: int, total: int | None) -> None:
                spinner.label = (
                    f"Fetching prebuilt catalog basemap ({done / total:.0%})"
                    if total
                    else f"Fetching prebuilt catalog basemap ({done / 1e6:.0f} MB)"
                )

            fetch_prebuilt_pmtiles(basemap, url=pmtiles_url, progress=tally)

    # Gather the explorer's items unless a map-only showcase was requested, or
    # --unified made the tiled archive itself the explorer's data source.
    # Resolved once and shared by both gathers below: the explorer's slice and
    # the marquee selection search the same ground, and a --place would
    # otherwise be geocoded (and echoed) twice.
    search_bbox, search_geometry = _shared._resolve_geography(bbox, place, intersects)

    items: list[UmbraItem] = []
    if explore and not unified:
        items = _shared._gather_items(
            local=local,
            db_path=db_path,
            bbox=search_bbox,
            intersects=search_geometry,
            start=start,
            end=end,
            area=area,
            fuzzy=fuzzy,
            product_types=list(products) or None,
            limit=limit,
            max_per_task=max_per_task,
        )
        if not items:
            raise click.ClickException("No items matched the search for the explorer.")

    # Resolve the marquee sites for the precomputed change gallery. This is a
    # second, separate gather: the explorer's slice is capped at one item per
    # task (a whole-archive overview), while a change composite needs several
    # passes of the *same* site.
    featured_sites = _shared._gather_featured_sites(
        count=featured,
        areas=featured_areas,
        pool_limit=featured_limit,
        min_passes=FEATURED_VIEWS[featured_view].min_passes_for(int(featured_frames)),
        local=local,
        db_path=db_path,
        search_kwargs={
            "bbox": search_bbox,
            "intersects": search_geometry,
            "start": start,
            "end": end,
            "fuzzy": fuzzy,
            "product_types": [asset],
        },
    )

    if basemap is None and not items and not featured_sites and not unified:
        raise click.ClickException(
            "Nothing to show: supply a basemap (--pmtiles / --fetch-pmtiles) "
            "and/or build the explorer (drop --no-explore)."
        )

    showcase_kwargs: dict[str, Any] = {}
    if title:
        showcase_kwargs["title"] = title
    if tagline:
        showcase_kwargs["tagline"] = tagline
    if updated:
        showcase_kwargs["updated"] = updated

    label = "Assembling showcase site"
    if featured_sites:
        label = f"Rendering {len(featured_sites)} featured composite(s) + showcase site"
    with OrbitSpinner(label):
        index = assemble_showcase(
            dest,
            items=items or None,
            pmtiles_path=basemap,
            unified=unified,
            demo_kwargs=(
                {"subtitle": "Every acquisition in the tiled open-data catalog."}
                if unified
                else {
                    "asset": asset,
                    "lazy_imagery": lazy_imagery,
                    "subtitle": _shared._search_subtitle(place or area, bbox, start, end),
                }
            ),
            featured_sites=featured_sites,
            featured_frames=int(featured_frames),
            featured_view=featured_view,
            **showcase_kwargs,
        )

    pages = ["index.html"]
    if basemap is not None and not unified:
        pages.append("map.html")
    if items or unified:
        pages.append("explore.html")
    suffix = FEATURED_VIEWS[featured_view].suffix
    rendered = sorted((dest / "featured").glob(f"*{suffix}")) if featured_sites else []
    if rendered:
        pages.append(f"featured/ ({len(rendered)} {featured_view} artifacts)")
    click.echo(f"Wrote showcase site ({', '.join(pages)}) to {index.parent}")
