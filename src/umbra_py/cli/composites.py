"""Multi-pass pictures of one site: ``change``, ``timescan``, ``swipe``.

All three gather a series with the shared options in :mod:`._shared`, pick
frames from it, and hand the result to :mod:`umbra_py.viz`.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from .._spinner import OrbitSpinner
from ..constants import PRODUCT_ASSETS
from ..exceptions import UmbraError
from ..viz import (
    save_change_animation,
    save_change_composite,
    save_swipe_map,
    save_timescan_composite,
    select_change_frames,
)
from . import _shared
from ._root import cli


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
@_shared._place_option
@_shared._geometry_option
@click.option(
    "--start",
    help="Search mode: earliest acquisition date. YYYY-MM-DD, a year/month "
    "(2024, 2024-03), or relative ('3 months ago', 'last month').",
)
@click.option(
    "--end",
    help="Search mode: latest acquisition date (same formats as --start).",
)
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
@click.option(
    "--narrate",
    is_flag=True,
    help="Composite (image) output only: after rendering, have a vision model "
    "narrate WHAT changed, grounded in a per-block decibel-change grid. Writes a "
    "machine-readable '<out>.narration.json' sidecar and prints the reading. "
    "Needs the 'ai' extra and a model API key (ANTHROPIC_API_KEY / OPENAI_API_KEY).",
)
@click.option(
    "--model",
    default=None,
    help="--narrate only: override the vision model (default: $UMBRA_NARRATE_MODEL, "
    "else the provider default). The provider is chosen by which API key is set.",
)
@_shared._local_index_options
@_shared._token_option
@_shared._fuzzy_option
@_shared._manifest_option
@_shared._acquisition_filter_options
def change(
    item_urls,
    out_path,
    area,
    fuzzy,
    bbox,
    place,
    intersects,
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
    narrate,
    model,
    polarizations,
    min_incidence,
    max_incidence,
    max_resolution,
    local,
    db_path,
    as_json,
    token,
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
    - Or search: give --area (or --bbox / --place / --intersects) with
      --start/--end and the command gathers a site's acquisitions
      automatically (preferring a single polarization).

    Add --narrate (composite output only) to have a vision model describe *what*
    changed, grounded in a per-block decibel-change grid written alongside the
    image as '<out>.narration.json' (needs the ai extra and a model API key).

    Only downsampled overviews are streamed via HTTP range requests -- no full
    download. Requires the viz extra (``pip install "umbra-py[viz]"``).
    """
    animate = out_path.lower().endswith(".gif")
    if colormap and not animate:
        raise click.UsageError("--colormap only applies to animated (.gif) output.")
    if narrate and animate:
        raise click.UsageError(
            "--narrate applies to a change composite (.png/.jpg), not a .gif time-lapse."
        )
    if model and not narrate:
        raise click.UsageError("--model only applies together with --narrate.")

    _shared._check_token_not_local(token, local, db_path)
    search_mode = any(v for v in (area, bbox, place, intersects, start, end))
    if item_urls and search_mode:
        raise click.UsageError(
            "Pass item URLs OR search criteria "
            "(--area/--bbox/--place/--intersects/--start/--end), not both."
        )

    if item_urls:
        if animate:
            if len(item_urls) < 2:
                raise click.BadParameter("a time-lapse needs 2 or more item URLs.")
        elif not 2 <= len(item_urls) <= 3:
            raise click.BadParameter("a composite needs 2 or 3 item URLs, in chronological order.")
        items = [_shared._item_from_url(url) for url in item_urls]
    else:
        if not (area or bbox or place or intersects):
            raise click.UsageError(
                "Give --area, --bbox, --place or --intersects (optionally with "
                "--start/--end) to search, or pass item URLs directly."
            )
        search_bbox, search_geometry = _shared._resolve_geography(bbox, place, intersects)
        found = _shared._gather_items(
            local=local,
            db_path=db_path,
            token=token,
            bbox=search_bbox,
            intersects=search_geometry,
            start=start,
            end=end,
            area=area,
            fuzzy=fuzzy,
            product_types=[asset],
            limit=max_search,
            **_shared._acquisition_filter_kwargs(
                polarizations, min_incidence, max_incidence, max_resolution
            ),
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
        if not as_json:
            if animate:
                span = f"{items[0].datetime:%Y-%m-%d} → {items[-1].datetime:%Y-%m-%d}"
                click.echo(f"Selected {len(items)} of {len(found)} acquisition(s) ({span}).")
            else:
                click.echo(f"Selected {len(items)} of {len(found)} acquisition(s):")
                for it in items:
                    when = it.datetime.isoformat() if it.datetime else "unknown time"
                    click.echo(f"  {when}  {it.id}")

    grid = max_size if max_size is not None else (1024 if animate else 2048)
    sidecars: dict = {}
    if animate:
        with OrbitSpinner(f"Rendering {len(items)}-frame time-lapse"):
            path = save_change_animation(
                items,
                out_path,
                asset=asset,
                max_size=grid,
                db=db,
                colormap=colormap or None,
                percentile=_shared._parse_percentile(percentile),
                fps=fps,
            )
        if not as_json:
            click.echo(f"Wrote time-lapse to {path}")
    elif narrate:
        # Render the composite ONCE (the expensive co-registration) and reuse the
        # exact bytes for both the written file and the model, so the narration is
        # grounded in the picture the user keeps -- no second S3 walk.
        from ..narrate import NarrateError, render_change_png, save_change_scene
        from ..narrate import narrate as narrate_change

        try:
            with OrbitSpinner(f"Rendering and narrating change of {len(items)} acquisitions"):
                image_png, stats = render_change_png(
                    items,
                    asset=asset,
                    max_size=grid,
                    db=db,
                    percentile=_shared._parse_percentile(percentile),
                )
                path = save_change_scene(image_png, out_path)
                narration = narrate_change(
                    items,
                    render=lambda _its: (image_png, stats),
                    model=model,
                    asset=asset,
                )
        except (NarrateError, UmbraError) as exc:
            raise click.ClickException(str(exc)) from exc

        sidecar = Path(out_path).with_suffix(".narration.json")
        sidecar.write_text(json.dumps(narration.to_dict(), indent=2))
        sidecars["narration"] = sidecar
        if not as_json:
            click.echo(f"Wrote change composite to {path}")
            click.echo(f"Wrote change narration to {sidecar}")
            click.echo("")
            click.echo(narration.to_text())
    else:
        with OrbitSpinner(f"Rendering change composite of {len(items)} acquisitions"):
            path = save_change_composite(
                items,
                out_path,
                asset=asset,
                max_size=grid,
                db=db,
                percentile=_shared._parse_percentile(percentile),
            )
        if not as_json:
            click.echo(f"Wrote change composite to {path}")

    if as_json:
        parameters: dict = {
            "asset": asset,
            "max_size": grid,
            "db": db,
            "percentile": percentile,
            "mode": "animation" if animate else "composite",
        }
        if animate:
            parameters["fps"] = fps
            if colormap:
                parameters["colormap"] = colormap
        else:
            parameters["frames"] = frames
            if narrate:
                parameters["narrate"] = True
        parameters.update(
            _shared._acquisition_filter_manifest(
                polarizations, min_incidence, max_incidence, max_resolution
            )
        )
        _shared._emit_render_manifest(path, items, parameters, sidecars or None)


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
@_shared._geometry_option
@click.option(
    "--start",
    help="Search mode: earliest acquisition date. YYYY-MM-DD, a year/month "
    "(2024, 2024-03), or relative ('3 months ago', 'last month').",
)
@click.option(
    "--end",
    help="Search mode: latest acquisition date (same formats as --start).",
)
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
@_shared._local_index_options
@_shared._token_option
@_shared._fuzzy_option
@_shared._manifest_option
@_shared._acquisition_filter_options
def timescan(
    item_urls,
    out_path,
    area,
    fuzzy,
    bbox,
    place,
    intersects,
    start,
    end,
    max_search,
    asset,
    max_size,
    db,
    percentile,
    polarizations,
    min_incidence,
    max_incidence,
    max_resolution,
    local,
    db_path,
    as_json,
    token,
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
    _shared._check_token_not_local(token, local, db_path)
    search_mode = any(v for v in (area, bbox, place, intersects, start, end))
    if item_urls and search_mode:
        raise click.UsageError(
            "Pass item URLs OR search criteria "
            "(--area/--bbox/--place/--intersects/--start/--end), not both."
        )

    if item_urls:
        if len(item_urls) < 3:
            raise click.BadParameter("a timescan needs 3 or more item URLs of the same site.")
        items = [_shared._item_from_url(url) for url in item_urls]
    else:
        if not (area or bbox or place or intersects):
            raise click.UsageError(
                "Give --area, --bbox, --place or --intersects (optionally with "
                "--start/--end) to search, or pass item URLs directly."
            )
        search_bbox, search_geometry = _shared._resolve_geography(bbox, place, intersects)
        found = _shared._gather_items(
            local=local,
            db_path=db_path,
            token=token,
            bbox=search_bbox,
            intersects=search_geometry,
            start=start,
            end=end,
            area=area,
            fuzzy=fuzzy,
            product_types=[asset],
            limit=max_search,
            **_shared._acquisition_filter_kwargs(
                polarizations, min_incidence, max_incidence, max_resolution
            ),
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
        if not as_json:
            span = f"{items[0].datetime:%Y-%m-%d} → {items[-1].datetime:%Y-%m-%d}"
            click.echo(f"Selected {len(items)} of {len(found)} acquisition(s) ({span}).")

    with OrbitSpinner(f"Rendering timescan of {len(items)} acquisitions"):
        path = save_timescan_composite(
            items,
            out_path,
            asset=asset,
            max_size=max_size,
            db=db,
            percentile=_shared._parse_percentile(percentile),
        )
    if as_json:
        _shared._emit_render_manifest(
            path,
            items,
            {
                "asset": asset,
                "max_size": max_size,
                "db": db,
                "percentile": percentile,
                **_shared._acquisition_filter_manifest(
                    polarizations, min_incidence, max_incidence, max_resolution
                ),
            },
        )
    else:
        click.echo(f"Wrote timescan composite to {path}")


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
@_shared._place_option
@_shared._geometry_option
@click.option(
    "--start",
    help="Search mode: earliest acquisition date. YYYY-MM-DD, a year/month "
    "(2024, 2024-03), or relative ('3 months ago', 'last month').",
)
@click.option(
    "--end",
    help="Search mode: latest acquisition date (same formats as --start).",
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
@_shared._local_index_options
@_shared._token_option
@_shared._fuzzy_option
@_shared._manifest_option
@_shared._acquisition_filter_options
def swipe(
    item_urls,
    out_path,
    area,
    fuzzy,
    bbox,
    place,
    intersects,
    start,
    end,
    max_search,
    asset,
    max_size,
    db,
    percentile,
    polarizations,
    min_incidence,
    max_incidence,
    max_resolution,
    local,
    db_path,
    as_json,
    token,
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
    - Or search: give --area (or --bbox / --place / --intersects) with
      --start/--end and the command gathers a site's acquisitions and
      compares the earliest with the latest (preferring a single
      polarization).

    Only downsampled overviews are streamed via HTTP range requests -- no full
    download. Requires the viz extra (``pip install "umbra-py[viz]"``).
    """
    _shared._check_token_not_local(token, local, db_path)
    search_mode = any(v for v in (area, bbox, place, intersects, start, end))
    if item_urls and search_mode:
        raise click.UsageError(
            "Pass two item URLs OR search criteria "
            "(--area/--bbox/--place/--intersects/--start/--end), not both."
        )

    if item_urls:
        if len(item_urls) != 2:
            raise click.BadParameter("swipe needs exactly 2 item URLs (before after).")
        before, after = (_shared._item_from_url(url) for url in item_urls)
    else:
        if not (area or bbox or place or intersects):
            raise click.UsageError(
                "Give --area, --bbox, --place or --intersects (optionally with "
                "--start/--end) to search, or pass two item URLs directly."
            )
        search_bbox, search_geometry = _shared._resolve_geography(bbox, place, intersects)
        found = _shared._gather_items(
            local=local,
            db_path=db_path,
            token=token,
            bbox=search_bbox,
            intersects=search_geometry,
            start=start,
            end=end,
            area=area,
            fuzzy=fuzzy,
            product_types=[asset],
            limit=max_search,
            **_shared._acquisition_filter_kwargs(
                polarizations, min_incidence, max_incidence, max_resolution
            ),
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
        if not as_json:
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
            percentile=_shared._parse_percentile(percentile),
        )
    if as_json:
        _shared._emit_render_manifest(
            path,
            [before, after],
            {
                "asset": asset,
                "max_size": max_size,
                "db": db,
                "percentile": percentile,
                **_shared._acquisition_filter_manifest(
                    polarizations, min_incidence, max_incidence, max_resolution
                ),
            },
        )
    else:
        click.echo(f"Wrote swipe map to {path}")
