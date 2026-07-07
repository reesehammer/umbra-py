"""Command-line interface: ``umbra search | info | download | map``."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from . import __version__
from ._http import get_json
from ._spinner import OrbitSpinner
from .catalog import UmbraCatalog
from .ccd import _sicd_shape, save_ccd
from .constants import DATA_LICENSE, PRODUCT_ASSETS
from .download import download_asset, download_item
from .exceptions import GeocodeError, UmbraError
from .geocode import geocode_place
from .index import CatalogIndex, default_index_path
from .models import UmbraItem
from .viz import (
    save_change_animation,
    save_change_composite,
    save_footprint_map,
    save_gallery,
    save_quicklook,
    save_swipe_map,
    save_timeline_map,
    save_timescan_composite,
    select_change_frames,
    write_geojson,
)


def _parse_bbox(value: str | None) -> tuple[float, float, float, float] | None:
    if not value:
        return None
    parts = [float(p) for p in value.split(",")]
    if len(parts) != 4:
        raise click.BadParameter("bbox must be 'min_lon,min_lat,max_lon,max_lat'")
    return (parts[0], parts[1], parts[2], parts[3])


def _resolve_search_bbox(
    bbox: str | None, place: str | None
) -> tuple[float, float, float, float] | None:
    """Resolve ``--bbox`` / ``--place`` into a single bounding box (or None).

    ``--place`` is geocoded to a bounding box via Nominatim, and the resolved
    place is echoed so the user can confirm the match before the search runs.
    The two options are mutually exclusive.
    """
    if place and bbox:
        raise click.UsageError("Pass --place or --bbox, not both.")
    if place:
        try:
            resolved, label = geocode_place(place)
        except GeocodeError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(f"Resolved '{place}' to {label}.")
        return resolved
    return _parse_bbox(bbox)


def _index_path(db_path: str | None) -> Path:
    """Resolve the index database path from an explicit ``--db`` or the default."""
    return Path(db_path) if db_path else default_index_path()


def _search_source(local: bool, db_path: str | None) -> tuple[object, bool]:
    """Pick the search backend: the local index (when ``--local``/``--db`` is
    given) or a live :class:`UmbraCatalog`. Returns ``(source, is_index)``."""
    if local or db_path is not None:
        path = _index_path(db_path)
        if not path.exists():
            raise click.ClickException(
                f"No index at {path}. Build one first with 'umbra index build'."
            )
        return CatalogIndex(path), True
    return UmbraCatalog(), False


def _progress_printer(label: str):
    def cb(done: int, total: int | None) -> None:
        if total:
            pct = 100 * done / total
            click.echo(
                f"\r  {label}: {done / 1e6:.1f}/{total / 1e6:.1f} MB ({pct:4.1f}%)", nl=False
            )
        else:
            click.echo(f"\r  {label}: {done / 1e6:.1f} MB", nl=False)

    return cb


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="umbra-py")
def cli() -> None:
    """umbra-py: discover, download and work with Umbra open SAR data."""


@cli.command()
@click.option("--bbox", help="Footprint filter: 'min_lon,min_lat,max_lon,max_lat'.")
@click.option(
    "--place",
    default=None,
    help="Geocode a place name (e.g. 'California', 'Tokyo') to a bounding box "
    "and search within it, via OpenStreetMap Nominatim. Mutually exclusive "
    "with --bbox; the match is rectangular, so it can include nearby areas "
    "outside the named place.",
)
@click.option("--start", help="Earliest acquisition date (YYYY-MM-DD).")
@click.option("--end", help="Latest acquisition date (YYYY-MM-DD).")
@click.option(
    "--product",
    "products",
    multiple=True,
    type=click.Choice(PRODUCT_ASSETS, case_sensitive=False),
    help="Keep items exposing this asset (repeatable).",
)
@click.option(
    "--area",
    default=None,
    help="Case-insensitive name of an Umbra task/site to search (e.g. "
    "'Centerfield'). Umbra files every pass of a site under one named "
    "directory, so this returns just that area's acquisitions -- and skips "
    "listing the rest, so it's much faster. The easy way to gather the "
    "co-located passes that 'umbra change' needs.",
)
@click.option("--limit", type=int, default=20, show_default=True, help="Max results.")
@click.option(
    "--max-per-task",
    type=int,
    default=None,
    help="Cap items per Umbra task directory. Each task is repeated imaging "
    "of the same area, so '--max-per-task 1' returns one item per distinct "
    "site rather than every revisit.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit full STAC item JSON.")
@click.option(
    "--local",
    is_flag=True,
    help="Search a local SQLite index built with 'umbra index build' instead "
    "of walking S3 live -- near-instant for repeat searches. Only returns "
    "acquisitions already present in the index.",
)
@click.option(
    "--db",
    "db_path",
    default=None,
    help="Path to the local index database (default: $UMBRA_INDEX_DB or "
    "~/.cache/umbra-py/catalog.db). Implies --local.",
)
def search(bbox, place, start, end, products, area, limit, max_per_task, as_json, local, db_path):
    """Search the catalog by area, date and product type."""
    search_bbox = _resolve_search_bbox(bbox, place)
    source, index = _search_source(local, db_path)
    try:
        results = source.search(
            bbox=search_bbox,
            start=start,
            end=end,
            product_types=list(products) or None,
            area=area,
            limit=limit,
            max_per_task=max_per_task,
        )
        found = 0
        spinner = OrbitSpinner("Searching local index" if index else "Searching Umbra archive")
        spinner.__enter__()
        try:
            for item in results:
                # Stop the spinner the moment we have something to print so the
                # streaming output isn't fighting the animation's cursor moves.
                spinner.stop()
                found += 1
                if as_json:
                    click.echo(json.dumps(item.raw))
                else:
                    click.echo(item.summary())
                    if item.href:
                        click.echo(f"  url      : {item.href}")
                    click.echo("")
        finally:
            spinner.stop()
        if not as_json:
            click.echo(f"{found} item(s).")
    finally:
        if index:
            source.close()


@cli.command()
@click.argument("item_url")
def info(item_url) -> None:
    """Show a readable summary of a STAC item given its JSON URL."""
    item = UmbraItem.from_dict(get_json(item_url), href=item_url)
    click.echo(item.summary())
    click.echo(f"\nData license: {DATA_LICENSE} (attribution required).")


@cli.command()
@click.argument("item_url")
@click.option(
    "--asset",
    "assets",
    multiple=True,
    type=click.Choice(PRODUCT_ASSETS, case_sensitive=False),
    help="Asset(s) to download (repeatable). Defaults to all present.",
)
@click.option("--dest", default=".", show_default=True, help="Output directory.")
@click.option("--overwrite", is_flag=True, help="Re-download if the file exists.")
def download(item_url, assets, dest, overwrite) -> None:
    """Download asset(s) of an item given its STAC JSON URL."""
    item = UmbraItem.from_dict(get_json(item_url), href=item_url)
    names = list(assets) or item.available_assets
    if not names:
        raise click.ClickException("No downloadable assets found on this item.")
    for name in names:
        click.echo(f"Downloading {name} of {item.id} ...")
        path = download_item(
            item, dest, assets=[name], overwrite=overwrite, progress=_progress_printer(name)
        )[0]
        click.echo(f"\n  -> {path}")


def _parse_percentile(value: str) -> tuple[float, float]:
    parts = value.split(",")
    if len(parts) != 2:
        raise click.BadParameter("percentile must be 'low,high' (e.g. '2,98')")
    try:
        lo, hi = float(parts[0]), float(parts[1])
    except ValueError as exc:
        raise click.BadParameter("percentile values must be numbers") from exc
    return (lo, hi)


@cli.command()
@click.argument("item_url")
@click.option(
    "--out",
    "out_path",
    required=True,
    help="Output image file (extension picks the format, e.g. scene.png).",
)
@click.option(
    "--asset",
    default="GEC",
    show_default=True,
    type=click.Choice(PRODUCT_ASSETS, case_sensitive=False),
    help="Which product to render. GEC (the detected GeoTIFF) is the sensible "
    "default; CSI also works. The complex SICD/CPHD products aren't amplitude "
    "rasters.",
)
@click.option(
    "--max-size",
    type=int,
    default=2048,
    show_default=True,
    help="Max pixel dimension of the quicklook. Larger is sharper but reveals "
    "more SAR speckle and fetches more bytes (roughly quadratic).",
)
@click.option(
    "--db",
    is_flag=True,
    help="Use a decibel (log-amplitude) stretch -- the radiometrically-correct "
    "SAR look. Reveals terrain texture and structure that the default linear "
    "stretch crushes toward black.",
)
@click.option(
    "--colormap",
    default=None,
    help="Matplotlib colormap for a pseudo-colored quicklook (e.g. viridis, "
    "magma, inferno). Default is grayscale.",
)
@click.option(
    "--percentile",
    default="2,98",
    show_default=True,
    help="Low,high percentile cut for the contrast stretch.",
)
def quicklook(item_url, out_path, asset, max_size, db, colormap, percentile) -> None:
    """Render a standalone SAR quicklook image from a STAC item URL.

    Streams a downsampled preview of the item's cloud-optimized GeoTIFF via
    HTTP range requests and writes it as an image -- no full download, no
    map. Requires the viz extra (``pip install "umbra-py[viz]"``).
    """
    item = UmbraItem.from_dict(get_json(item_url), href=item_url)
    with OrbitSpinner(f"Rendering quicklook of {item.id}"):
        path = save_quicklook(
            item,
            out_path,
            asset=asset,
            max_size=max_size,
            db=db,
            colormap=colormap or None,
            percentile=_parse_percentile(percentile),
        )
    click.echo(f"Wrote quicklook to {path}")


@cli.command(name="load")
@click.argument("item_url")
@click.option(
    "--out",
    "out_path",
    required=True,
    help="Output GeoTIFF path (e.g. scene.tif).",
)
@click.option(
    "--asset",
    default="GEC",
    show_default=True,
    type=click.Choice(PRODUCT_ASSETS, case_sensitive=False),
    help="Which product to load. GEC (the geocoded GeoTIFF) is the sensible "
    "default; CSI also works. The complex SICD/CPHD products aren't amplitude "
    "rasters.",
)
@click.option("--bbox", help="Clip to a lon/lat window: 'min_lon,min_lat,max_lon,max_lat'.")
@click.option(
    "--max-size",
    type=int,
    default=None,
    help="Cap the longest output side in pixels (decimates via COG overviews). "
    "Omit to write full resolution -- pair that with --bbox for a large scene.",
)
@click.option(
    "--db",
    is_flag=True,
    help="Write the decibel (log-amplitude) scale instead of linear amplitude.",
)
def load_cmd(item_url, out_path, asset, bbox, max_size, db) -> None:
    """Load a clipped/decimated SAR scene from a STAC item URL to a GeoTIFF.

    Streams only the requested window/resolution of the item's cloud-optimized
    GeoTIFF via HTTP range requests and writes an analysis-ready, single-band
    float32 GeoTIFF in the source CRS -- no full download. For an in-memory
    array instead, use ``umbra_py.to_xarray``. Requires the load extra
    (``pip install "umbra-py[load]"``).
    """
    from .load import to_geotiff  # noqa: PLC0415

    item = UmbraItem.from_dict(get_json(item_url), href=item_url)
    with OrbitSpinner(f"Loading {asset} of {item.id}"):
        path = to_geotiff(
            item,
            out_path,
            asset=asset,
            bbox=_parse_bbox(bbox),
            max_size=max_size,
            db=db,
        )
    click.echo(f"Wrote GeoTIFF to {path}")


@cli.command()
@click.argument("item_urls", nargs=-1)
@click.option(
    "--out",
    "out_path",
    required=True,
    help="Output file. An image extension (.png/.jpg) writes a 2-3 date color "
    "composite; '.gif' writes an animated time-lapse across all the "
    "acquisitions.",
)
@click.option(
    "--area",
    default=None,
    help="Search mode: name of an Umbra site (e.g. 'Centerfield') to gather "
    "automatically instead of passing URLs. Combine with --start/--end to "
    "bound the time range.",
)
@click.option("--bbox", help="Search mode: footprint filter 'min_lon,min_lat,max_lon,max_lat'.")
@click.option("--start", help="Search mode: earliest acquisition date (YYYY-MM-DD).")
@click.option("--end", help="Search mode: latest acquisition date (YYYY-MM-DD).")
@click.option(
    "--frames",
    type=click.IntRange(2, 3),
    default=2,
    show_default=True,
    help="Composite (image) output only: how many dates to composite (2 or 3), "
    "spread evenly across the matched time range. A .gif time-lapse uses every "
    "matched acquisition.",
)
@click.option(
    "--max-search",
    type=int,
    default=50,
    show_default=True,
    help="Search mode: cap how many acquisitions the search pulls.",
)
@click.option(
    "--asset",
    default="GEC",
    show_default=True,
    type=click.Choice(PRODUCT_ASSETS, case_sensitive=False),
    help="Which product to compare. GEC (the detected GeoTIFF) is the sensible "
    "default; CSI also works. The complex SICD/CPHD products aren't amplitude "
    "rasters.",
)
@click.option(
    "--max-size",
    type=int,
    default=None,
    help="Max pixel dimension of the shared grid. Default 2048 for a composite, "
    "1024 for a .gif (a time-lapse stacks many frames, so smaller keeps the "
    "file sane). Larger is sharper but fetches more bytes (~quadratic).",
)
@click.option(
    "--db",
    is_flag=True,
    help="Use a decibel (log-amplitude) stretch -- the radiometrically-correct "
    "SAR look. Reveals texture and structure the default linear stretch "
    "crushes toward black.",
)
@click.option(
    "--colormap",
    default=None,
    help="Time-lapse (.gif) only: matplotlib colormap for pseudo-colored frames "
    "(e.g. viridis, magma). Default is grayscale.",
)
@click.option(
    "--fps",
    type=float,
    default=2.0,
    show_default=True,
    help="Time-lapse (.gif) only: playback speed in frames per second.",
)
@click.option(
    "--percentile",
    default="2,98",
    show_default=True,
    help="Low,high percentile cut for each frame's contrast stretch.",
)
def change(
    item_urls,
    out_path,
    area,
    bbox,
    start,
    end,
    frames,
    max_search,
    asset,
    max_size,
    db,
    colormap,
    fps,
    percentile,
) -> None:
    """Render multi-temporal SAR change: a color composite or a time-lapse.

    Two outputs, picked by the --out extension:

    \b
    - An image (.png/.jpg) is a 2-3 date color composite: unchanged ground
      stays gray, backscatter that appeared shows green and backscatter that
      vanished shows magenta (two dates), or a red/green/blue trail (three).
    - A .gif is an animated time-lapse over every matched acquisition, all
      co-registered so the site stays put and only the scene evolves.

    Two ways to choose what to render:

    \b
    - Pass STAC JSON URLs directly, in chronological order (2-3 for a
      composite, 2+ for a .gif).
    - Or search: give --area (or --bbox) with --start/--end and the command
      gathers a site's acquisitions automatically (preferring a single
      polarization).

    Only downsampled overviews are streamed via HTTP range requests -- no full
    download. Requires the viz extra (``pip install "umbra-py[viz]"``).
    """
    animate = out_path.lower().endswith(".gif")
    if colormap and not animate:
        raise click.UsageError("--colormap only applies to animated (.gif) output.")

    search_mode = any(v for v in (area, bbox, start, end))
    if item_urls and search_mode:
        raise click.UsageError(
            "Pass item URLs OR search criteria (--area/--bbox/--start/--end), not both."
        )

    if item_urls:
        if animate:
            if len(item_urls) < 2:
                raise click.BadParameter("a time-lapse needs 2 or more item URLs.")
        elif not 2 <= len(item_urls) <= 3:
            raise click.BadParameter("a composite needs 2 or 3 item URLs, in chronological order.")
        items = [UmbraItem.from_dict(get_json(url), href=url) for url in item_urls]
    else:
        if not (area or bbox):
            raise click.UsageError(
                "Give --area or --bbox (optionally with --start/--end) to search, "
                "or pass item URLs directly."
            )
        with OrbitSpinner("Searching Umbra archive"):
            found = list(
                UmbraCatalog().search(
                    bbox=_parse_bbox(bbox),
                    start=start,
                    end=end,
                    area=area,
                    product_types=[asset],
                    limit=max_search,
                )
            )
        if len(found) < 2:
            raise click.ClickException(
                f"Need at least 2 {asset} acquisitions to compare; the search "
                f"found {len(found)}. Widen the date range or area."
            )
        # A .gif uses the whole series; a composite picks 2-3 spanning frames.
        items = select_change_frames(found, frames=None if animate else frames)
        if len({tuple(i.polarizations) for i in items}) > 1:
            click.echo(
                "warning: selected acquisitions have mixed polarizations; some "
                "apparent change may be a polarization difference, not real change.",
                err=True,
            )
        if animate:
            span = f"{items[0].datetime:%Y-%m-%d} → {items[-1].datetime:%Y-%m-%d}"
            click.echo(f"Selected {len(items)} of {len(found)} acquisition(s) ({span}).")
        else:
            click.echo(f"Selected {len(items)} of {len(found)} acquisition(s):")
            for it in items:
                when = it.datetime.isoformat() if it.datetime else "unknown time"
                click.echo(f"  {when}  {it.id}")

    grid = max_size if max_size is not None else (1024 if animate else 2048)
    if animate:
        with OrbitSpinner(f"Rendering {len(items)}-frame time-lapse"):
            path = save_change_animation(
                items,
                out_path,
                asset=asset,
                max_size=grid,
                db=db,
                colormap=colormap or None,
                percentile=_parse_percentile(percentile),
                fps=fps,
            )
        click.echo(f"Wrote time-lapse to {path}")
    else:
        with OrbitSpinner(f"Rendering change composite of {len(items)} acquisitions"):
            path = save_change_composite(
                items,
                out_path,
                asset=asset,
                max_size=grid,
                db=db,
                percentile=_parse_percentile(percentile),
            )
        click.echo(f"Wrote change composite to {path}")


@cli.command()
@click.argument("item_urls", nargs=-1)
@click.option(
    "--out",
    "out_path",
    required=True,
    help="Output image file (.png/.jpg) for the temporal-statistics composite.",
)
@click.option(
    "--area",
    default=None,
    help="Search mode: name of an Umbra site (e.g. 'Centerfield') to gather "
    "automatically instead of passing URLs. Combine with --start/--end to "
    "bound the time range.",
)
@click.option("--bbox", help="Search mode: footprint filter 'min_lon,min_lat,max_lon,max_lat'.")
@click.option(
    "--place",
    default=None,
    help="Search mode: geocode a place name (e.g. 'California', 'Tokyo') to a "
    "bounding box and summarise within it, via OpenStreetMap Nominatim. "
    "Mutually exclusive with --bbox; the match is rectangular, so it can "
    "include nearby areas outside the named place.",
)
@click.option("--start", help="Search mode: earliest acquisition date (YYYY-MM-DD).")
@click.option("--end", help="Search mode: latest acquisition date (YYYY-MM-DD).")
@click.option(
    "--max-search",
    type=int,
    default=50,
    show_default=True,
    help="Search mode: cap how many acquisitions the search pulls into the stack.",
)
@click.option(
    "--asset",
    default="GEC",
    show_default=True,
    type=click.Choice(PRODUCT_ASSETS, case_sensitive=False),
    help="Which product to summarise. GEC (the detected GeoTIFF) is the sensible "
    "default; CSI also works. The complex SICD/CPHD products aren't amplitude "
    "rasters.",
)
@click.option(
    "--max-size",
    type=int,
    default=2048,
    show_default=True,
    help="Max pixel dimension of the shared grid. Larger is sharper but fetches "
    "more bytes (~quadratic).",
)
@click.option(
    "--db",
    is_flag=True,
    help="Summarise in the decibel (log-amplitude) domain -- the "
    "radiometrically-correct SAR look, measuring variability in log space.",
)
@click.option(
    "--percentile",
    default="2,98",
    show_default=True,
    help="Low,high percentile cut for each statistic's contrast stretch.",
)
def timescan(
    item_urls,
    out_path,
    area,
    bbox,
    place,
    start,
    end,
    max_search,
    asset,
    max_size,
    db,
    percentile,
) -> None:
    """Collapse a whole SAR time series into one temporal-statistics image.

    Where `umbra change` compares 2-3 dates, this summarises the *entire*
    stack of a site's acquisitions per pixel and maps the statistics to color:

    \b
    - red   = average backscatter
    - green = peak backscatter
    - blue  = temporal variability (standard deviation)

    Stable terrain renders gray/yellow; anything that came and went across the
    series -- ships cycling through a berth, vehicles in a lot, a field
    flooding -- glows blue/cyan. The whole archive of a site becomes one
    glanceable "where did activity happen" picture.

    Two ways to choose what to summarise:

    \b
    - Pass 3+ STAC JSON URLs directly (order doesn't matter).
    - Or search: give --area (or --bbox / --place) with --start/--end and the
      command gathers a site's acquisitions automatically (preferring a single
      polarization).

    Only downsampled overviews are streamed via HTTP range requests -- no full
    download. Requires the viz extra (``pip install "umbra-py[viz]"``).
    """
    search_mode = any(v for v in (area, bbox, place, start, end))
    if item_urls and search_mode:
        raise click.UsageError(
            "Pass item URLs OR search criteria (--area/--bbox/--place/--start/--end), not both."
        )

    if item_urls:
        if len(item_urls) < 3:
            raise click.BadParameter("a timescan needs 3 or more item URLs of the same site.")
        items = [UmbraItem.from_dict(get_json(url), href=url) for url in item_urls]
    else:
        if not (area or bbox or place):
            raise click.UsageError(
                "Give --area, --bbox or --place (optionally with --start/--end) to "
                "search, or pass item URLs directly."
            )
        search_bbox = _resolve_search_bbox(bbox, place)
        with OrbitSpinner("Searching Umbra archive"):
            found = list(
                UmbraCatalog().search(
                    bbox=search_bbox,
                    start=start,
                    end=end,
                    area=area,
                    product_types=[asset],
                    limit=max_search,
                )
            )
        if len(found) < 3:
            raise click.ClickException(
                f"Need at least 3 {asset} acquisitions to summarise; the search "
                f"found {len(found)}. Widen the date range or area."
            )
        # The whole series (single-polarization where possible), oldest-first.
        items = select_change_frames(found, frames=None)
        if len({tuple(i.polarizations) for i in items}) > 1:
            click.echo(
                "warning: selected acquisitions have mixed polarizations; some "
                "apparent variability may be a polarization difference, not real change.",
                err=True,
            )
        span = f"{items[0].datetime:%Y-%m-%d} → {items[-1].datetime:%Y-%m-%d}"
        click.echo(f"Selected {len(items)} of {len(found)} acquisition(s) ({span}).")

    with OrbitSpinner(f"Rendering timescan of {len(items)} acquisitions"):
        path = save_timescan_composite(
            items,
            out_path,
            asset=asset,
            max_size=max_size,
            db=db,
            percentile=_parse_percentile(percentile),
        )
    click.echo(f"Wrote timescan composite to {path}")


def _parse_crop(ctx, param, value):
    """Parse ``--crop`` into an int (centered SIZExSIZE) or a 4-tuple window."""
    if not value:
        return None
    try:
        nums = [int(p) for p in value.split(",")]
    except ValueError as exc:
        raise click.BadParameter("crop must be integers: SIZE or COL,ROW,WIDTH,HEIGHT") from exc
    if len(nums) == 1:
        return nums[0]
    if len(nums) == 4:
        return tuple(nums)
    raise click.BadParameter("crop is either SIZE or COL,ROW,WIDTH,HEIGHT")


def _resolve_sicd_arg(arg: str, dest: str) -> str:
    """Turn a ``ccd`` positional into a local SICD path.

    A STAC item URL is resolved and its ``SICD`` asset downloaded into
    ``dest`` (sarpy needs a local file -- the complex NITF can't be streamed
    like the GEC overviews). Anything else is treated as a local path.
    """
    if arg.startswith(("http://", "https://")):
        item = UmbraItem.from_dict(get_json(arg), href=arg)
        click.echo(f"Downloading SICD for {item.id} ...")
        return str(download_asset(item, "SICD", dest))
    return arg


@cli.command()
@click.argument("reference")
@click.argument("secondary")
@click.option(
    "--out",
    "out_path",
    required=True,
    help="Output image file (.png/.jpg) for the coherence map.",
)
@click.option(
    "--window",
    type=int,
    default=5,
    show_default=True,
    help="Coherence estimation window (odd). Larger smooths noise but blurs fine change.",
)
@click.option(
    "--upsample",
    type=int,
    default=10,
    show_default=True,
    help="Sub-pixel coregistration precision (1/N of a pixel). Coherence is "
    "very sensitive to misregistration, so don't drop this much.",
)
@click.option(
    "--colormap",
    default=None,
    help="Matplotlib colormap (e.g. viridis, magma) for the coherence map. Default is grayscale.",
)
@click.option(
    "--invert",
    is_flag=True,
    help="Show change bright instead of stable bright (display 1 - coherence).",
)
@click.option(
    "--max-size",
    type=int,
    default=2048,
    show_default=True,
    help="Max pixel dimension of the written image. Coherence is still "
    "estimated at full resolution, then resized for display.",
)
@click.option(
    "--crop",
    callback=_parse_crop,
    help="Process only a sub-window, to bound memory on large (multi-GB) "
    "scenes: a single SIZE for a centered SIZExSIZE box, or "
    "COL,ROW,WIDTH,HEIGHT for an explicit window. In pixels. The whole NITF is "
    "still downloaded; this bounds memory and compute, not bytes fetched.",
)
@click.option(
    "--dest",
    default=".",
    show_default=True,
    help="Directory to save SICDs auto-downloaded when item URLs are passed.",
)
def ccd(
    reference, secondary, out_path, window, upsample, colormap, invert, max_size, crop, dest
) -> None:
    """Coherent change detection from a pair of complex SICD acquisitions.

    Where `umbra change` compares how *bright* a scene is, this compares
    whether the ground itself was physically disturbed between two passes.
    Two complex SICD images of the same site share a speckle phase pattern
    unless something at the surface moved; their normalised correlation, the
    coherence in [0, 1], maps that:

    \b
    - bright / high coherence = unchanged ground
    - dark   / low coherence  = disturbed ground (tire tracks, dug earth,
      moved foliage/water) -- or a weak, incoherent return (shadow, smooth
      water). Use --invert to make change the bright signal.

    This reveals sub-resolution change that leaves no amplitude signature at
    all -- the one product a general GIS pipeline can't reproduce, because it
    needs the preserved phase only SICD carries.

    REFERENCE and SECONDARY are each either a local SICD (NITF) file or a STAC
    item JSON URL (its SICD asset is downloaded into --dest first).

    Coregistration is a single global sub-pixel translation, which only aligns a
    pair that already shares a pixel grid -- a coherent collect on near-
    identical geometry. Two independently-focused SICDs of the same site
    generally do NOT share a grid, so they decorrelate everywhere and the map is
    just noise; the command warns when it detects that. Full sensor-model
    coregistration for arbitrary repeat-pass pairs is not implemented.

    Port and other large scenes run to several GB and don't fit in memory whole
    -- pass --crop to process a sub-window (e.g. a set of berths) at full
    resolution. The printed scene dimensions tell you the pixel coordinate
    space for an explicit COL,ROW,WIDTH,HEIGHT crop.

    Requires the convert + viz extras
    (``pip install "umbra-py[convert,viz]"``).
    """
    ref_path = _resolve_sicd_arg(reference, dest)
    sec_path = _resolve_sicd_arg(secondary, dest)
    rows, cols = _sicd_shape(ref_path)
    click.echo(f"Reference scene: {rows} x {cols} px (rows x cols).")
    with OrbitSpinner("Estimating coherence"):
        path = save_ccd(
            ref_path,
            sec_path,
            out_path,
            window=window,
            upsample=upsample,
            colormap=colormap or None,
            invert=invert,
            max_size=max_size,
            crop=crop,
        )
    click.echo(f"Wrote coherent change map to {path}")


@cli.command()
@click.argument("item_urls", nargs=-1)
@click.option(
    "--out",
    "out_path",
    required=True,
    help="Output HTML file for the interactive swipe map.",
)
@click.option(
    "--area",
    default=None,
    help="Search mode: name of an Umbra site (e.g. 'Centerfield') to gather "
    "automatically instead of passing two URLs. Combine with --start/--end to "
    "bound the time range; the earliest and latest passes are compared.",
)
@click.option("--bbox", help="Search mode: footprint filter 'min_lon,min_lat,max_lon,max_lat'.")
@click.option("--start", help="Search mode: earliest acquisition date (YYYY-MM-DD).")
@click.option("--end", help="Search mode: latest acquisition date (YYYY-MM-DD).")
@click.option(
    "--max-search",
    type=int,
    default=50,
    show_default=True,
    help="Search mode: cap how many acquisitions the search pulls.",
)
@click.option(
    "--asset",
    default="GEC",
    show_default=True,
    type=click.Choice(PRODUCT_ASSETS, case_sensitive=False),
    help="Which product to compare. GEC (the detected GeoTIFF) is the sensible "
    "default; CSI also works. The complex SICD/CPHD products aren't amplitude "
    "rasters.",
)
@click.option(
    "--max-size",
    type=int,
    default=1024,
    show_default=True,
    help="Max pixel dimension of each overlay. Larger is sharper but fetches "
    "more bytes (~quadratic).",
)
@click.option(
    "--db",
    is_flag=True,
    help="Use a decibel (log-amplitude) stretch -- the radiometrically-correct "
    "SAR look. Reveals texture and structure the default linear stretch "
    "crushes toward black.",
)
@click.option(
    "--percentile",
    default="2,98",
    show_default=True,
    help="Low,high percentile cut for each overlay's contrast stretch.",
)
def swipe(
    item_urls,
    out_path,
    area,
    bbox,
    start,
    end,
    max_search,
    asset,
    max_size,
    db,
    percentile,
) -> None:
    """Render an interactive before/after swipe map of two SAR passes.

    Drag the divider to wipe one acquisition over the other across the same
    ground: SAR backscatter is stable between passes, so anything that
    changed -- a ship that docked, a field that flooded, a building that
    rose -- snaps in and out as you sweep the seam. The output is a single
    self-contained HTML file.

    Two ways to choose what to compare:

    \b
    - Pass exactly two STAC JSON URLs, in chronological order (before after).
    - Or search: give --area (or --bbox) with --start/--end and the command
      gathers a site's acquisitions and compares the earliest with the latest
      (preferring a single polarization).

    Only downsampled overviews are streamed via HTTP range requests -- no full
    download. Requires the viz extra (``pip install "umbra-py[viz]"``).
    """
    search_mode = any(v for v in (area, bbox, start, end))
    if item_urls and search_mode:
        raise click.UsageError(
            "Pass two item URLs OR search criteria (--area/--bbox/--start/--end), not both."
        )

    if item_urls:
        if len(item_urls) != 2:
            raise click.BadParameter("swipe needs exactly 2 item URLs (before after).")
        before, after = (UmbraItem.from_dict(get_json(url), href=url) for url in item_urls)
    else:
        if not (area or bbox):
            raise click.UsageError(
                "Give --area or --bbox (optionally with --start/--end) to search, "
                "or pass two item URLs directly."
            )
        with OrbitSpinner("Searching Umbra archive"):
            found = list(
                UmbraCatalog().search(
                    bbox=_parse_bbox(bbox),
                    start=start,
                    end=end,
                    area=area,
                    product_types=[asset],
                    limit=max_search,
                )
            )
        if len(found) < 2:
            raise click.ClickException(
                f"Need at least 2 {asset} acquisitions to compare; the search "
                f"found {len(found)}. Widen the date range or area."
            )
        before, after = select_change_frames(found, frames=2)
        if tuple(before.polarizations) != tuple(after.polarizations):
            click.echo(
                "warning: the two acquisitions have different polarizations; some "
                "apparent change may be a polarization difference, not real change.",
                err=True,
            )
        click.echo(f"Comparing {len(found)} found acquisition(s):")
        for it in (before, after):
            when = it.datetime.isoformat() if it.datetime else "unknown time"
            click.echo(f"  {when}  {it.id}")

    with OrbitSpinner("Rendering swipe map"):
        path = save_swipe_map(
            before,
            after,
            out_path,
            asset=asset,
            max_size=max_size,
            db=db,
            percentile=_parse_percentile(percentile),
        )
    click.echo(f"Wrote swipe map to {path}")


def _search_subtitle(area, bbox, start, end) -> str | None:
    """A short, human-readable description of a search, for a page header."""
    parts: list[str] = []
    if area:
        parts.append(area)
    elif bbox:
        parts.append(f"bbox {bbox}")
    if start or end:
        parts.append(f"{start or '…'} → {end or '…'}")
    return " · ".join(parts) or None


@cli.command()
@click.option("--bbox", help="Footprint filter: 'min_lon,min_lat,max_lon,max_lat'.")
@click.option(
    "--place",
    default=None,
    help="Geocode a place name (e.g. 'California', 'Tokyo') to a bounding box "
    "and gather tiles within it, via OpenStreetMap Nominatim. Mutually "
    "exclusive with --bbox.",
)
@click.option("--start", help="Earliest acquisition date (YYYY-MM-DD).")
@click.option("--end", help="Latest acquisition date (YYYY-MM-DD).")
@click.option(
    "--area",
    default=None,
    help="Case-insensitive name of an Umbra task/site to gather (e.g. "
    "'Centerfield'). Faster than a broad scan -- it lists just that area's "
    "directory.",
)
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
def gallery(
    bbox,
    place,
    start,
    end,
    area,
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
) -> None:
    """Render search results as a browseable HTML SAR thumbnail gallery.

    Searches the catalog, streams a small SAR quicklook for each match (only
    downsampled overviews via HTTP range requests -- no full downloads), and
    writes a single self-contained HTML contact sheet: a grid of thumbnails,
    each tile linking to its STAC item with a footprint sketch. The missing
    "browse the catalog visually" primitive. Requires the viz extra
    (``pip install "umbra-py[viz]"``).
    """
    if not out_path.lower().endswith((".html", ".htm")):
        raise click.ClickException("Gallery output must be an .html file.")

    search_bbox = _resolve_search_bbox(bbox, place)
    catalog = UmbraCatalog()
    with OrbitSpinner("Searching Umbra archive"):
        items = list(
            catalog.search(
                bbox=search_bbox,
                start=start,
                end=end,
                area=area,
                product_types=list(products) or [asset],
                limit=limit,
                max_per_task=max_per_task,
            )
        )
    if not items:
        raise click.ClickException("No items matched the search.")

    with OrbitSpinner(f"Streaming {len(items)} SAR thumbnail(s)"):
        path = save_gallery(
            items,
            out_path,
            asset=asset,
            max_size=max_size,
            db=db,
            colormap=colormap or None,
            percentile=_parse_percentile(percentile),
            max_workers=workers,
            subtitle=_search_subtitle(place or area, bbox, start, end),
        )
    click.echo(f"Wrote gallery of {len(items)} acquisition(s) to {path}")


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
@click.option("--start", help="Earliest acquisition date (YYYY-MM-DD).")
@click.option("--end", help="Latest acquisition date (YYYY-MM-DD).")
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
def map_cmd(
    bbox,
    place,
    start,
    end,
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
) -> None:
    """Render search results as an interactive map or GeoJSON file."""
    search_bbox = _resolve_search_bbox(bbox, place)
    catalog = UmbraCatalog()
    imagery_kwargs: dict | None = None
    if imagery_max_size is not None:
        imagery_kwargs = {"max_size": imagery_max_size}

    with OrbitSpinner("Searching Umbra archive"):
        items = list(
            catalog.search(
                bbox=search_bbox,
                start=start,
                end=end,
                product_types=list(products) or None,
                limit=limit,
                max_per_task=max_per_task,
            )
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
    click.echo(f"Wrote {len(items)} footprint(s) to {path}")


@cli.group()
def index() -> None:
    """Build and inspect a local SQLite catalog index for fast offline search.

    Umbra has no STAC API, so a live search re-walks S3 every time. Index the
    archive once into a local database, then run 'umbra search --local' for
    near-instant repeat searches over the same data.
    """


@index.command("build")
@click.option(
    "--db",
    "db_path",
    default=None,
    help="Output index database (default: $UMBRA_INDEX_DB or "
    "~/.cache/umbra-py/catalog.db). Created if missing; existing rows are "
    "refreshed and new ones added (incremental).",
)
@click.option("--bbox", help="Scope the build to a footprint: 'min_lon,min_lat,max_lon,max_lat'.")
@click.option(
    "--place",
    default=None,
    help="Scope the build to a geocoded place name (mutually exclusive with --bbox).",
)
@click.option("--start", help="Scope to acquisitions on/after this date (YYYY-MM-DD).")
@click.option("--end", help="Scope to acquisitions on/before this date (YYYY-MM-DD).")
@click.option(
    "--area",
    default=None,
    help="Scope to one Umbra task/site by name (e.g. 'Centerfield'). Much "
    "faster than a full walk -- it lists just that task.",
)
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Cap how many acquisitions to index this run (default: no cap -- index "
    "everything in scope).",
)
def index_build(db_path, bbox, place, start, end, area, limit) -> None:
    """Walk Umbra's archive and persist matching acquisitions into the index.

    With no scope flags this indexes the whole bucket, which lists every task
    and takes a while; pass --area/--bbox/--place/--start/--end to index just
    the slice you care about.
    """
    search_bbox = _resolve_search_bbox(bbox, place)
    path = _index_path(db_path)
    scope = "Umbra archive" if not any((search_bbox, start, end, area)) else "matching acquisitions"
    with OrbitSpinner(f"Indexing {scope}") as spinner:
        # A full-bucket build runs for a while, so show a live tally instead of
        # an inscrutable spinner. The spinner repaints its label each frame.
        def tally(n: int) -> None:
            spinner.label = f"Indexing {scope} ({n} so far)"

        with CatalogIndex(path) as idx:
            written = idx.build(
                progress=tally,
                bbox=search_bbox,
                start=start,
                end=end,
                area=area,
                limit=limit,
            )
            total = len(idx)
    click.echo(f"Indexed {written} acquisition(s); index now holds {total}. ({path})")


@index.command("info")
@click.option(
    "--db",
    "db_path",
    default=None,
    help="Index database to inspect (default: $UMBRA_INDEX_DB or ~/.cache/umbra-py/catalog.db).",
)
def index_info(db_path) -> None:
    """Show what a local index holds: item count, date span and task count."""
    path = _index_path(db_path)
    if not path.exists():
        raise click.ClickException(f"No index at {path}. Build one with 'umbra index build'.")
    with CatalogIndex(path) as idx:
        s = idx.stats()
    size_mb = path.stat().st_size / 1e6
    click.echo(f"Index: {path}")
    click.echo(f"  items : {s['items']}")
    click.echo(f"  dates : {s['start'] or '?'} -> {s['end'] or '?'}")
    click.echo(f"  tasks : {s['tasks']}")
    click.echo(f"  size  : {size_mb:.1f} MB")


def main() -> None:
    """Console entry point with friendly error reporting."""
    try:
        cli.main(standalone_mode=False)
    except click.ClickException as exc:
        exc.show()
        sys.exit(exc.exit_code)
    except UmbraError as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)
    except click.exceptions.Abort:
        sys.exit(130)


if __name__ == "__main__":
    main()
