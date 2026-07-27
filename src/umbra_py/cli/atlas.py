"""Where the archive has imagery: ``map`` and ``gallery``.

The two survey verbs -- a Leaflet footprint/timeline map and a contact sheet
of quicklooks -- over whatever a search or an explicit list of URLs yields.
"""

from __future__ import annotations

import click

from .._spinner import OrbitSpinner
from ..constants import PRODUCT_ASSETS
from ..viz import (
    save_footprint_map,
    save_gallery,
    save_timeline_map,
    write_geojson,
)
from . import _shared
from ._root import cli


@cli.command()
@click.option("--bbox", help="Footprint filter: 'min_lon,min_lat,max_lon,max_lat'.")
@click.option(
    "--place",
    default=None,
    help="Geocode a place name (e.g. 'California', 'Tokyo') to a bounding box "
    "and gather tiles within it, via OpenStreetMap Nominatim. Mutually "
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
    help="Keep items exposing this asset (repeatable). Defaults to --asset so "
    "every tile is renderable.",
)
@click.option("--limit", type=int, default=24, show_default=True, help="Max tiles.")
@click.option(
    "--max-per-task",
    type=int,
    default=None,
    help="Cap items per Umbra task directory. '--max-per-task 1' gives one "
    "tile per distinct site rather than every revisit -- a quick overview of "
    "where the archive has imagery.",
)
@click.option("--out", "out_path", required=True, help="Output HTML file (e.g. gallery.html).")
@click.option(
    "--asset",
    default="GEC",
    show_default=True,
    type=click.Choice(PRODUCT_ASSETS, case_sensitive=False),
    help="Which product to render in each thumbnail. GEC (the detected "
    "GeoTIFF) is the sensible default; CSI also works. The complex SICD/CPHD "
    "products aren't amplitude rasters.",
)
@click.option(
    "--max-size",
    type=int,
    default=512,
    show_default=True,
    help="Max pixel dimension of each thumbnail. Larger is sharper but fetches "
    "more bytes per tile (~quadratic).",
)
@click.option(
    "--db",
    is_flag=True,
    help="Use a decibel (log-amplitude) stretch -- the radiometrically-correct "
    "SAR look that reveals texture the default linear stretch crushes toward "
    "black.",
)
@click.option(
    "--colormap",
    default=None,
    help="Matplotlib colormap for pseudo-colored thumbnails (e.g. viridis, "
    "magma). Default is grayscale.",
)
@click.option(
    "--percentile",
    default="2,98",
    show_default=True,
    help="Low,high percentile cut for each thumbnail's contrast stretch.",
)
@click.option(
    "--workers",
    type=int,
    default=8,
    show_default=True,
    help="How many thumbnails to stream in parallel.",
)
@_shared._local_index_options
@_shared._token_option
@_shared._fuzzy_option
@_shared._manifest_option
@_shared._acquisition_filter_options
def gallery(
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
    max_size,
    db,
    colormap,
    percentile,
    workers,
    polarizations,
    min_incidence,
    max_incidence,
    max_resolution,
    local,
    db_path,
    as_json,
    token,
) -> None:
    """Render search results as a browseable HTML SAR thumbnail gallery.

    Searches the catalog, streams a small SAR quicklook for each match (only
    downsampled overviews via HTTP range requests -- no full downloads), and
    writes a single self-contained HTML contact sheet: a grid of thumbnails,
    each tile linking to its STAC item with a footprint sketch. The missing
    "browse the catalog visually" primitive. Requires the viz extra
    (``pip install "umbra-py[viz]"``).

    With --local (or --index-db), any thumbnail already baked into the index by
    'umbra index bake-thumbnails' is embedded straight from local bytes -- no S3
    stream, and no viz extra needed when every tile is baked -- so a baked index
    renders the gallery instantly and offline.
    """
    if not out_path.lower().endswith((".html", ".htm")):
        raise click.ClickException("Gallery output must be an .html file.")

    _shared._check_token_not_local(token, local, db_path)
    search_bbox, search_geometry = _shared._resolve_geography(bbox, place, intersects)
    items = _shared._gather_items(
        local=local,
        db_path=db_path,
        token=token,
        bbox=search_bbox,
        intersects=search_geometry,
        start=start,
        end=end,
        area=area,
        fuzzy=fuzzy,
        product_types=list(products) or [asset],
        limit=limit,
        max_per_task=max_per_task,
        **_shared._acquisition_filter_kwargs(
            polarizations, min_incidence, max_incidence, max_resolution
        ),
    )
    if not items:
        raise click.ClickException("No items matched the search.")

    # A --local gallery can serve any thumbnail already baked into the index
    # (umbra index bake-thumbnails) straight from local bytes -- instant, offline,
    # and skipping the S3 overview stream entirely. Only the rest are streamed.
    baked = _shared._baked_thumbnails(items, db_path) if (local or db_path is not None) else {}
    n_baked = sum(1 for it in items if it.id in baked)
    n_stream = len(items) - n_baked
    if n_stream:
        label = f"Streaming {n_stream} SAR thumbnail(s)"
        if n_baked:
            label += f" ({n_baked} from the baked index)"
    else:
        label = f"Loading {n_baked} baked SAR thumbnail(s)"

    with OrbitSpinner(label):
        path = save_gallery(
            items,
            out_path,
            asset=asset,
            max_size=max_size,
            db=db,
            colormap=colormap or None,
            percentile=_shared._parse_percentile(percentile),
            max_workers=workers,
            subtitle=_shared._search_subtitle(place or area, bbox, start, end),
            baked=baked,
        )
    if as_json:
        _shared._emit_render_manifest(
            path,
            items,
            {
                "asset": asset,
                "products": list(products) or [asset],
                "max_size": max_size,
                "db": db,
                "percentile": percentile,
                **_shared._acquisition_filter_manifest(
                    polarizations, min_incidence, max_incidence, max_resolution
                ),
            },
        )
    else:
        note = f" ({n_baked} from baked thumbnails)" if n_baked else ""
        click.echo(f"Wrote gallery of {len(items)} acquisition(s){note} to {path}")


@cli.command(name="map")
@click.option("--bbox", help="Footprint filter: 'min_lon,min_lat,max_lon,max_lat'.")
@click.option(
    "--place",
    default=None,
    help="Geocode a place name (e.g. 'California', 'Tokyo') to a bounding box "
    "and plot items within it, via OpenStreetMap Nominatim. Mutually exclusive "
    "with --bbox. (Distinct from --geocode, which labels each plotted "
    "footprint with its place name.)",
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
    help="Keep items exposing this asset (repeatable).",
)
@click.option("--limit", type=int, default=100, show_default=True, help="Max results to plot.")
@click.option(
    "--out",
    "out_path",
    required=True,
    help="Output file. '.html' writes an interactive Folium map (requires the "
    "viz extra); '.geojson' / '.json' writes a GeoJSON FeatureCollection.",
)
@click.option(
    "--imagery",
    is_flag=True,
    help="Overlay each item's GEC SAR image on the map (HTML output only; "
    "needs the viz extra including rasterio).",
)
@click.option(
    "--imagery-max-size",
    type=int,
    default=None,
    help="Max pixel dimension of each SAR overlay. Default is 1024 -- bump "
    "to 2048 or 4096 for sharper imagery at the cost of larger HTML output "
    "(quadratic in size). SAR data is inherently grainy (speckle); higher "
    "values reveal more detail but also more speckle noise.",
)
@click.option(
    "--max-per-task",
    type=int,
    default=None,
    help="Cap items per Umbra task directory. Each task is repeated imaging "
    "of the same area, so '--max-per-task 1' returns one item per distinct "
    "site rather than every revisit.",
)
@click.option(
    "--geocode/--no-geocode",
    default=True,
    show_default=True,
    help="Reverse-geocode each footprint's centroid via OpenStreetMap "
    "Nominatim and include the resulting place name in the popup. "
    "Adds one HTTP request per item (throttled to ~1/sec to honor "
    "Nominatim's usage policy); pass --no-geocode to skip the network "
    "calls or when running offline.",
)
@click.option(
    "--timeline",
    is_flag=True,
    help="Render an animated timeline map instead of the static footprint "
    "map. Footprints appear at their acquisition timestamps and the page "
    "ships a play button + scrubber, so you can watch Umbra's coverage "
    "accumulate over the requested window. HTML output only; --imagery "
    "is not yet supported on this view.",
)
@click.option(
    "--timeline-period",
    default="P1D",
    show_default=True,
    help="ISO 8601 step for the timeline slider (e.g. PT1H, P1D, P7D). "
    "Pick a period matching the cadence of your search: PT1H for one "
    "day of acquisitions, P1D for a month, P7D for a year. Ignored "
    "without --timeline.",
)
@click.option(
    "--lazy-imagery",
    is_flag=True,
    help="Add a 'Get SAR image' button to each popup. On click, the browser "
    "streams that item's GEC cloud-optimized GeoTIFF directly from the "
    "Umbra bucket via HTTP range requests (using georaster-layer-for-leaflet "
    "+ geotiff.js from a CDN) and overlays it on the map. Unlike --imagery, "
    "the HTML stays ~30 KB regardless of how many items it carries -- you "
    "only pay the fetch cost for items you click. Works with --timeline. "
    "HTML output only; mutually exclusive with --imagery.",
)
@_shared._local_index_options
@_shared._token_option
@_shared._manifest_option
@_shared._acquisition_filter_options
@_shared._fuzzy_option
def map_cmd(
    bbox,
    place,
    intersects,
    start,
    end,
    area,
    fuzzy,
    products,
    limit,
    out_path,
    imagery,
    imagery_max_size,
    max_per_task,
    geocode,
    timeline,
    timeline_period,
    lazy_imagery,
    polarizations,
    min_incidence,
    max_incidence,
    max_resolution,
    local,
    db_path,
    as_json,
    token,
) -> None:
    """Render search results as an interactive map or GeoJSON file."""
    _shared._check_token_not_local(token, local, db_path)
    search_bbox, search_geometry = _shared._resolve_geography(bbox, place, intersects)
    imagery_kwargs: dict | None = None
    if imagery_max_size is not None:
        imagery_kwargs = {"max_size": imagery_max_size}

    items = _shared._gather_items(
        local=local,
        db_path=db_path,
        token=token,
        bbox=search_bbox,
        intersects=search_geometry,
        start=start,
        end=end,
        area=area,
        fuzzy=fuzzy,
        product_types=list(products) or None,
        limit=limit,
        max_per_task=max_per_task,
        **_shared._acquisition_filter_kwargs(
            polarizations, min_incidence, max_incidence, max_resolution
        ),
    )
    if not items:
        raise click.ClickException("No items matched the search.")

    lower = out_path.lower()
    if lower.endswith((".geojson", ".json")):
        if imagery:
            raise click.ClickException("--imagery only applies to HTML map output.")
        if timeline:
            raise click.ClickException("--timeline only applies to HTML map output.")
        if lazy_imagery:
            raise click.ClickException("--lazy-imagery only applies to HTML map output.")
        path = write_geojson(items, out_path)
    elif lower.endswith(".html") or lower.endswith(".htm"):
        if timeline and imagery:
            raise click.ClickException(
                "--timeline and --imagery can't be combined yet; animating SAR "
                "rasters across the slider isn't supported. Use --lazy-imagery "
                "for on-demand SAR overlays on the timeline."
            )
        if imagery and lazy_imagery:
            raise click.ClickException(
                "--imagery (pre-baked PNG overlays) and --lazy-imagery "
                "(browser-side COG fetch on click) are mutually exclusive. "
                "Pick one."
            )
        if timeline:
            extras = []
            if geocode:
                extras.append(f"geocoding ~{len(items)}s")
            if lazy_imagery:
                extras.append("lazy SAR overlays")
            suffix = (" with " + ", ".join(extras)) if extras else ""
            with OrbitSpinner(f"Rendering {len(items)} acquisition(s) on timeline{suffix}"):
                path = save_timeline_map(
                    items,
                    out_path,
                    period=timeline_period,
                    geocode=geocode,
                    lazy_imagery=lazy_imagery,
                )
        else:
            extras = []
            if imagery:
                extras.append("imagery")
            if lazy_imagery:
                extras.append("lazy SAR overlays")
            if geocode:
                # Geocoding is the slow part (1 req/sec), so call it out so
                # users aren't surprised when --geocode + a 100-item search
                # spends a minute on Nominatim before the file appears.
                extras.append(f"geocoding ~{len(items)}s")
            suffix = (" with " + ", ".join(extras)) if extras else ""
            with OrbitSpinner(f"Rendering {len(items)} footprint(s){suffix}"):
                path = save_footprint_map(
                    items,
                    out_path,
                    imagery=imagery,
                    imagery_kwargs=imagery_kwargs,
                    geocode=geocode,
                    lazy_imagery=lazy_imagery,
                )
    else:
        raise click.ClickException(
            "Unrecognized output extension. Use .html for a map or .geojson for data."
        )
    if as_json:
        _shared._emit_render_manifest(
            path,
            items,
            {
                "format": "geojson" if lower.endswith((".geojson", ".json")) else "html",
                "products": list(products) or None,
                "imagery": imagery,
                "lazy_imagery": lazy_imagery,
                "timeline": timeline,
                "geocode": geocode,
                **_shared._acquisition_filter_manifest(
                    polarizations, min_incidence, max_incidence, max_resolution
                ),
            },
        )
    else:
        click.echo(f"Wrote {len(items)} footprint(s) to {path}")
