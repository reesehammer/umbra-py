"""Command-line interface: ``umbra search | info | download | map``."""

from __future__ import annotations

import json
import sys

import click

from . import __version__
from ._http import get_json
from ._spinner import OrbitSpinner
from .catalog import UmbraCatalog
from .constants import DATA_LICENSE, PRODUCT_ASSETS
from .download import download_item
from .exceptions import UmbraError
from .models import UmbraItem
from .viz import save_footprint_map, save_quicklook, save_timeline_map, write_geojson


def _parse_bbox(value: str | None) -> tuple[float, float, float, float] | None:
    if not value:
        return None
    parts = [float(p) for p in value.split(",")]
    if len(parts) != 4:
        raise click.BadParameter("bbox must be 'min_lon,min_lat,max_lon,max_lat'")
    return (parts[0], parts[1], parts[2], parts[3])


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
@click.option("--start", help="Earliest acquisition date (YYYY-MM-DD).")
@click.option("--end", help="Latest acquisition date (YYYY-MM-DD).")
@click.option(
    "--product",
    "products",
    multiple=True,
    type=click.Choice(PRODUCT_ASSETS, case_sensitive=False),
    help="Keep items exposing this asset (repeatable).",
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
def search(bbox, start, end, products, limit, max_per_task, as_json) -> None:
    """Search the catalog by area, date and product type."""
    catalog = UmbraCatalog()
    results = catalog.search(
        bbox=_parse_bbox(bbox),
        start=start,
        end=end,
        product_types=list(products) or None,
        limit=limit,
        max_per_task=max_per_task,
    )
    found = 0
    spinner = OrbitSpinner("Searching Umbra archive")
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


@cli.command(name="map")
@click.option("--bbox", help="Footprint filter: 'min_lon,min_lat,max_lon,max_lat'.")
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
    catalog = UmbraCatalog()
    imagery_kwargs: dict | None = None
    if imagery_max_size is not None:
        imagery_kwargs = {"max_size": imagery_max_size}

    with OrbitSpinner("Searching Umbra archive"):
        items = list(
            catalog.search(
                bbox=_parse_bbox(bbox),
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
