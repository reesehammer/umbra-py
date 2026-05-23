"""Command-line interface: ``umbra search | info | download | map``."""

from __future__ import annotations

import json
import sys

import click

from . import __version__
from ._http import get_json
from .catalog import UmbraCatalog
from .constants import DATA_LICENSE, PRODUCT_ASSETS
from .download import download_item
from .exceptions import UmbraError
from .models import UmbraItem
from .viz import save_footprint_map, write_geojson


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
@click.option("--json", "as_json", is_flag=True, help="Emit full STAC item JSON.")
def search(bbox, start, end, products, limit, as_json) -> None:
    """Search the catalog by area, date and product type."""
    catalog = UmbraCatalog()
    results = catalog.search(
        bbox=_parse_bbox(bbox),
        start=start,
        end=end,
        product_types=list(products) or None,
        limit=limit,
    )
    found = 0
    for item in results:
        found += 1
        if as_json:
            click.echo(json.dumps(item.raw))
        else:
            click.echo(item.summary())
            if item.href:
                click.echo(f"  url      : {item.href}")
            click.echo("")
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
def map_cmd(bbox, start, end, products, limit, out_path) -> None:
    """Render search results as an interactive map or GeoJSON file."""
    catalog = UmbraCatalog()
    items = list(
        catalog.search(
            bbox=_parse_bbox(bbox),
            start=start,
            end=end,
            product_types=list(products) or None,
            limit=limit,
        )
    )
    if not items:
        raise click.ClickException("No items matched the search.")

    lower = out_path.lower()
    if lower.endswith((".geojson", ".json")):
        path = write_geojson(items, out_path)
    elif lower.endswith(".html") or lower.endswith(".htm"):
        path = save_footprint_map(items, out_path)
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
