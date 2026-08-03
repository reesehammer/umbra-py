"""One acquisition at a time: ``describe``, ``download``, ``quicklook``,
``view``, ``load``.

Each takes a single item URL and produces a file, a picture, or a description
of it. The heavy readers (``rasterio``, ``xarray``) stay behind the ``[load]``
/ ``[viz]`` extras and are imported by the functions these commands call.
"""

from __future__ import annotations

import json

import click

from .._spinner import OrbitSpinner
from ..constants import PRODUCT_ASSETS
from ..describe import PREVIEW_SOURCES
from ..download import download_item
from ..exceptions import UmbraError
from ..viz import (
    save_quicklook,
)
from . import _shared
from ._root import cli


@cli.command()
@click.argument("item_url")
@click.option(
    "--asset",
    default="GEC",
    show_default=True,
    type=click.Choice(PRODUCT_ASSETS, case_sensitive=False),
    help="Which product to read. GEC (the geocoded GeoTIFF) is the sensible "
    "default; CSI also works. The complex SICD/CPHD products aren't amplitude "
    "rasters.",
)
@click.option(
    "--model",
    default=None,
    help="Override the vision model (default: $UMBRA_DESCRIBE_MODEL, else the "
    "provider default). The provider is chosen by which API key is set — "
    "ANTHROPIC_API_KEY or OPENAI_API_KEY (with optional OPENAI_BASE_URL).",
)
@click.option(
    "--max-size",
    type=int,
    default=1024,
    show_default=True,
    help="Max pixel dimension of the quicklook sent to the model. Larger is "
    "sharper but fetches more bytes and costs more tokens.",
)
@click.option(
    "--db/--no-db",
    "db",
    default=True,
    show_default=True,
    help="Use a decibel (log-amplitude) stretch — the radiometrically-correct "
    "SAR look the model reads best. --no-db uses a linear stretch.",
)
@click.option(
    "--preview",
    type=click.Choice(PREVIEW_SOURCES, case_sensitive=False),
    default="render",
    show_default=True,
    help="Where the picture the model reads comes from. 'render' streams a fresh "
    "quicklook from S3; 'baked' reads the preview already cached in the local "
    "index ('umbra index bake-thumbnails' / 'fetch-thumbnails') — no range read, "
    "no viz extra — and fails if there is none; 'auto' prefers the cached one and "
    "renders when it is missing. A cached preview is smaller than --max-size, "
    "which the description records and caveats.",
)
@click.option(
    "--index-db",
    "db_path",
    default=None,
    help="Path to the local index database holding the baked previews (default: "
    "$UMBRA_INDEX_DB or ~/.cache/umbra-py/catalog.db). Only read when --preview "
    "is 'baked' or 'auto'. Named --index-db because --db means the decibel stretch.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit the structured description as JSON "
    "(see docs/schemas/scene-description.schema.json).",
)
def describe(item_url, asset, model, max_size, db, preview, db_path, as_json) -> None:
    """Describe a SAR scene in plain language with a vision model.

    Renders the item's quicklook, sends that picture plus the library's metadata
    context card to a configured vision model, and returns a structured reading:
    a summary, observed features, the model's confidence, and SAR-specific
    caveats. The model *only* interprets the imagery — every description is
    stamped as an AI interpretation and carries the mandatory CC-BY attribution,
    and nothing the model says becomes a filter, a URL, or a coordinate.

    With --preview baked (or auto) the picture comes from the quicklook already
    cached in the local index instead of a fresh S3 overview stream, so a
    description costs no range read and needs no viz extra at all. It is a
    smaller picture than --max-size asks for, so the reading says which it read
    and carries a caveat about the detail it could not have seen.

    Requires the ``ai`` extra for the model call and ``viz`` for the render
    (``pip install 'umbra-py[ai,viz]'``) plus a vision model API key: set
    ANTHROPIC_API_KEY, or OPENAI_API_KEY (optionally with OPENAI_BASE_URL for a
    compatible endpoint). Example::

        umbra describe https://.../<item>/<id>.json
    """
    from ..describe import DescribeError
    from ..describe import describe as describe_scene

    item = _shared._item_from_url(item_url)
    previews = _shared._baked_previews(db_path) if preview != "render" else None
    try:
        with OrbitSpinner(f"Describing {item.id}"):
            description = describe_scene(
                item,
                model=model,
                asset=asset,
                max_size=max_size,
                db=db,
                preview=preview,
                previews=previews,
            )
    except (DescribeError, UmbraError) as exc:
        raise click.ClickException(str(exc)) from exc

    if as_json:
        click.echo(json.dumps(description.to_dict(), indent=2))
    else:
        click.echo(description.to_text())


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
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit one {asset, path, bytes, sha256} record per downloaded asset as a "
    "JSON array on stdout (see docs/schemas/download.schema.json), instead of the "
    "human progress lines. Progress stays on stderr.",
)
def download(item_url, assets, dest, overwrite, as_json) -> None:
    """Download asset(s) of an item given its STAC JSON URL.

    ``--json`` emits a machine-readable ``[{asset, path, bytes, sha256}, ...]``
    array (``docs/schemas/download.schema.json``) so an agent can verify each
    file it just fetched without re-hashing it.
    """
    item = _shared._item_from_url(item_url)
    names = list(assets) or item.available_assets
    if not names:
        raise click.ClickException("No downloadable assets found on this item.")
    records: list[dict] = []
    for name in names:
        if not as_json:
            click.echo(f"Downloading {name} of {item.id} ...")
        path = download_item(
            item,
            dest,
            assets=[name],
            overwrite=overwrite,
            progress=None if as_json else _shared._progress_printer(name),
        )[0]
        if as_json:
            records.append(
                {
                    "asset": name,
                    "path": str(path),
                    "bytes": path.stat().st_size,
                    "sha256": _shared._sha256_file(path),
                }
            )
        else:
            click.echo(f"\n  -> {path}")
    if as_json:
        click.echo(json.dumps(records, indent=2))


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
    item = _shared._item_from_url(item_url)
    with OrbitSpinner(f"Rendering quicklook of {item.id}"):
        path = save_quicklook(
            item,
            out_path,
            asset=asset,
            max_size=max_size,
            db=db,
            colormap=colormap or None,
            percentile=_shared._parse_percentile(percentile),
        )
    click.echo(f"Wrote quicklook to {path}")


@cli.command()
@click.argument("item_url")
@click.option(
    "--asset",
    default="GEC",
    show_default=True,
    type=click.Choice(PRODUCT_ASSETS, case_sensitive=False),
    help="Which product to view. GEC (the geocoded GeoTIFF) is the sensible "
    "default; CSI also works. The complex SICD/CPHD products aren't amplitude "
    "rasters.",
)
@click.option("--host", default="127.0.0.1", show_default=True, help="Host to bind the server to.")
@click.option(
    "--port",
    type=int,
    default=0,
    show_default=True,
    help="Port to bind (0 picks a free one).",
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
    help="Matplotlib colormap for a pseudo-colored view (e.g. viridis, magma, "
    "inferno). Default is grayscale.",
)
@click.option(
    "--percentile",
    default="2,98",
    show_default=True,
    help="Low,high percentile cut for the global contrast stretch.",
)
@click.option("--no-browser", is_flag=True, help="Don't open the viewer in a browser.")
def view(item_url, asset, host, port, db, colormap, percentile, no_browser) -> None:
    """Explore one SAR scene at full resolution in an interactive web viewer.

    Starts a local tile server and opens a Leaflet map in the browser. Pan and
    zoom to roam the acquisition's cloud-optimized GeoTIFF at native resolution
    -- only the tiles in view are streamed via HTTP range requests and warped
    onto the web map, so there's no full download. Where ``umbra quicklook``
    collapses the scene to one downsampled PNG, this keeps every pixel a zoom
    away. Runs until you press Ctrl-C. Requires the viz extra
    (``pip install "umbra-py[viz]"``).
    """
    from ..viewer import make_viewer_server  # noqa: PLC0415

    item = _shared._item_from_url(item_url)
    with OrbitSpinner(f"Opening {asset} of {item.id}"):
        httpd, url = make_viewer_server(
            item,
            asset=asset,
            host=host,
            port=port,
            db=db,
            colormap=colormap or None,
            percentile=_shared._parse_percentile(percentile),
        )
    click.echo(f"Serving SAR viewer for {item.id} at {url}")
    click.echo("Press Ctrl-C to stop.")
    if not no_browser:
        import webbrowser  # noqa: PLC0415

        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        click.echo("\nStopping viewer.")
    finally:
        # serve_forever has already returned, so close the socket directly --
        # shutdown() is for stopping the loop from another thread and would
        # deadlock here.
        httpd.server_close()


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
    from ..load import to_geotiff  # noqa: PLC0415

    item = _shared._item_from_url(item_url)
    with OrbitSpinner(f"Loading {asset} of {item.id}"):
        path = to_geotiff(
            item,
            out_path,
            asset=asset,
            bbox=_shared._parse_bbox(bbox),
            max_size=max_size,
            db=db,
        )
    click.echo(f"Wrote GeoTIFF to {path}")
