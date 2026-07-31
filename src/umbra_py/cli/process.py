"""Turning scenes into analysis-ready products: ``stack``, ``convert``,
``chips``.

The three commands that write data rather than pictures -- the co-registered
time-series datacube, the SICD -> geocoded COG pipeline (DEM, RTC flattening,
radiometric calibration), and the ML chip set with its manifest.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from .._spinner import OrbitSpinner
from ..chips import CHIPPABLE_ASSETS
from ..constants import PRODUCT_ASSETS
from ..convert import CALIBRATION_TYPES, RESAMPLING_METHODS, RTC_MODELS
from ..load import STACK_EXTENTS
from ..viz import (
    select_change_frames,
)
from . import _shared
from ._root import cli


@cli.command()
@click.argument("item_urls", nargs=-1)
@click.option(
    "--out",
    "out_path",
    default=None,
    help="Output multi-band GeoTIFF path (one band per acquisition, oldest "
    "first). Required unless --stats asks for the statistics alone.",
)
@click.option(
    "--stats",
    is_flag=True,
    help="Also print the cube's time-series statistics as JSON: per-pass "
    "distribution, the decibel change between consecutive passes, and the net "
    "first-to-last change (with the changed area in km2 under --crs utm). Pass "
    "it without --out to measure without writing a file.",
)
@click.option(
    "--blocks",
    type=int,
    default=0,
    help="Break the statistics down over a N x N grid of the scene: each block "
    "reports its own net change and the pair of passes it moved most between. "
    "Implies --stats; 6 is a good starting grid.",
)
@click.option(
    "--block-series",
    is_flag=True,
    help="With --blocks: report each block's whole pass-to-pass sequence, not "
    "only the interval it moved most in -- so a block that drifted every pass "
    "reads differently from one that jumped once and held.",
)
@click.option(
    "--stats-windowed",
    is_flag=True,
    help="Implies --stats: measure the cube one window at a time (the windows "
    "--chunk-size cut it into) instead of one whole pass at a time, so a cube "
    "too big to hold a slice of can still be measured. Every count, mean and "
    "change number stays exact; each pass's median/p5/p95 become histogram "
    "estimates, and the output says so.",
)
@click.option(
    "--change-threshold-db",
    type=float,
    default=3.0,
    show_default=True,
    help="With --stats: how many decibels a cell must move between two passes "
    "to count as changed (3 dB is a doubling of backscatter power).",
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
    "bounding box and stack within it, via OpenStreetMap Nominatim. Mutually "
    "exclusive with --bbox; the match is rectangular, so it can include nearby "
    "areas outside the named place.",
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
    help="Which product to stack. GEC (the geocoded GeoTIFF) is the sensible "
    "default; CSI also works. The complex SICD/CPHD products aren't amplitude "
    "rasters.",
)
@click.option(
    "--clip-bbox",
    default=None,
    help="Clip the cube to a lon/lat window 'min_lon,min_lat,max_lon,max_lat' "
    "inside whatever --extent selected. (Distinct from --bbox, which filters "
    "which acquisitions the search returns.)",
)
@click.option(
    "--max-size",
    type=int,
    default=1024,
    show_default=True,
    help="Longest side of the shared grid in pixels. Larger is sharper but "
    "fetches more bytes (~quadratic), and the grid is shared by every band.",
)
@click.option(
    "--extent",
    type=click.Choice(STACK_EXTENTS, case_sensitive=False),
    default="intersection",
    show_default=True,
    help="intersection: only ground every acquisition covers, so no cell has a "
    "gap. union: all ground any acquisition covers, NaN outside each scene.",
)
@click.option(
    "--crs",
    default=None,
    help="CRS of the shared output grid. Default is lon/lat (EPSG:4326), whose "
    "cells are not equal-area; 'utm' picks the UTM zone the site falls in so "
    "every cell covers the same ground (what area measurements need). Any CRS "
    "string also works (e.g. EPSG:32633). --clip-bbox stays lon/lat either way.",
)
@click.option(
    "--db",
    is_flag=True,
    help="Stack the decibel (log-amplitude) scale -- the radiometrically "
    "meaningful scale for differencing, where a backscatter ratio becomes a "
    "subtraction.",
)
@click.option(
    "--lazy",
    is_flag=True,
    help="Read each pass on demand (one dask chunk per acquisition) instead of "
    "holding the whole cube in memory, and write/measure it a slice at a time. "
    "Same output; peak memory is set by --max-size rather than by how many "
    "acquisitions the series has, so a long series can be stacked sharp. "
    'Needs the dask extra: pip install "umbra-py[dask]".',
)
@click.option(
    "--chunk-size",
    type=int,
    default=None,
    help="With --lazy, cut each pass into CHUNK_SIZE-square windows read (and "
    "written) independently, so a single pass no longer has to fit in memory "
    "either. Costs one read per window instead of one per pass, so keep it a "
    "decent fraction of --max-size (e.g. 1024). Same output.",
)
@_shared._local_index_options
@_shared._token_option
@_shared._fuzzy_option
@_shared._manifest_option
@_shared._acquisition_filter_options
def stack(
    item_urls,
    out_path,
    stats,
    blocks,
    block_series,
    stats_windowed,
    change_threshold_db,
    area,
    fuzzy,
    bbox,
    place,
    intersects,
    start,
    end,
    max_search,
    asset,
    clip_bbox,
    max_size,
    extent,
    crs,
    db,
    lazy,
    chunk_size,
    polarizations,
    min_incidence,
    max_incidence,
    max_resolution,
    local,
    db_path,
    as_json,
    token,
) -> None:
    """Co-register a site's acquisitions into one analysis-ready datacube.

    The time-series half of `umbra load`, and the step between *search* and
    *analysis*: several passes over one site are warped onto one shared grid
    and written as a multi-band float32 GeoTIFF -- one band per acquisition,
    oldest first, each described by its timestamp. Because the bands are
    pixel-aligned, band arithmetic is an honest per-ground-cell comparison, so
    the file drops straight into QGIS, GDAL, rioxarray or a notebook.

    The grid is lon/lat (EPSG:4326) by default, whose cells stretch with
    latitude; pass --crs utm (or any CRS) when cells have to be equal-area,
    e.g. to turn a count of changed cells into an area.

    Where `umbra change` and `umbra timescan` render this comparison as a
    *picture*, this writes the *numbers*. In Python, ``umbra_py.to_stack``
    returns the same cube as an xarray DataArray with a real ``time``
    dimension (``cube.mean("time")``, ``cube.diff("time")``, ...).

    --stats reduces the cube to a JSON summary instead of making you open it:
    each pass's distribution, the signed decibel change against the pass
    before it, and the net first-to-last change -- including how much ground
    moved, in km2, when --crs makes the cells equal-area. Give it without
    --out to measure a site without writing the file.

    --blocks N adds the spatial half of that answer: the scene is cut into an
    N x N grid and each block reports its own net change, a compass label and
    lon/lat centre to find it by, and the consecutive pair of passes it moved
    most between -- so a change confined to one corner, which a scene-wide
    mean hides, reads as "the northeast brightened, between these two passes".
    --block-series keeps each block's whole sequence rather than just that
    peak, which is what distinguishes a steady drift from a single step.

    --lazy lifts the ceiling on how much series fits: passes are read on
    demand (one dask chunk each) and written or measured a slice at a time, so
    peak memory follows --max-size instead of the number of acquisitions. Reach
    for it when a long series would otherwise have to be stacked coarse; the
    cube and the statistics are the same either way. --chunk-size N lifts what
    is left of that ceiling: each pass is cut into N-square windows read and
    written independently, so --max-size stops being bounded by how much of
    one scene fits in memory (at one read per window rather than per pass).
    --stats-windowed gives the *measurement* the same lift: the reduction walks
    those windows instead of whole passes, so a cube sharper than a slice you
    can hold is measurable and not only writable. The trade is stated in the
    output -- every count, mean and change number stays exact, while each
    pass's median/p5/p95 become histogram estimates, since a percentile is the
    one statistic that needs the whole pass at once.

    Two ways to choose what to stack:

    \b
    - Pass 2+ STAC JSON URLs directly (order doesn't matter).
    - Or search: give --area (or --bbox / --place) with --start/--end and the
      command gathers a site's acquisitions automatically.

    Stack one polarization: mixing VV and VH puts a polarization difference on
    the time axis where you'll read it as change (--pol filters the search).
    Only downsampled overviews are streamed via HTTP range requests -- no full
    download. Requires the load extra (``pip install "umbra-py[load]"``).
    """
    # The private writer, not stack_to_geotiff: --stats needs the cube itself,
    # and stacking a series twice would double the bytes streamed.
    from ..load import _write_stack_geotiff, stack_stats, to_stack  # noqa: PLC0415

    _shared._check_token_not_local(token, local, db_path)
    if blocks < 0:
        raise click.BadParameter("--blocks must be 0 (off) or a positive grid size.")
    if block_series and not blocks:
        raise click.UsageError("--block-series needs --blocks N (the series lives on a block).")
    if chunk_size is not None:
        if not lazy:
            raise click.UsageError(
                "--chunk-size needs --lazy (an eager cube is read a slab at a time)."
            )
        if chunk_size < 1:
            raise click.BadParameter("--chunk-size must be a positive pixel count.")
    stats = stats or bool(blocks) or stats_windowed
    if not (out_path or stats):
        raise click.UsageError("Give --out to write the datacube, --stats to measure it, or both.")
    search_mode = any(v for v in (area, bbox, place, intersects, start, end))
    if item_urls and search_mode:
        raise click.UsageError(
            "Pass item URLs OR search criteria "
            "(--area/--bbox/--place/--intersects/--start/--end), not both."
        )

    if item_urls:
        if len(item_urls) < 2:
            raise click.BadParameter("a stack needs 2 or more item URLs of the same site.")
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
                f"Need at least 2 {asset} acquisitions to stack; the search found "
                f"{len(found)}. Widen the date range or area."
            )
        # The whole series (single-polarization where possible), oldest-first.
        items = select_change_frames(found, frames=None)
        if len({tuple(i.polarizations) for i in items}) > 1:
            click.echo(
                "warning: selected acquisitions have mixed polarizations; a "
                "polarization difference will appear on the time axis as if it "
                "were change. Re-run with --pol to stack one polarization.",
                err=True,
            )
        if not as_json:
            span = f"{items[0].datetime:%Y-%m-%d} → {items[-1].datetime:%Y-%m-%d}"
            # To stderr when --stats will print JSON, so stdout stays one object.
            click.echo(f"Selected {len(items)} of {len(found)} acquisition(s) ({span}).", err=stats)

    with OrbitSpinner(f"Stacking {len(items)} acquisitions"):
        cube = to_stack(
            items,
            asset=asset,
            bbox=_shared._parse_bbox(clip_bbox),
            max_size=max_size,
            db=db,
            extent=extent,
            crs=crs,
            lazy=lazy,
            chunk_size=chunk_size,
        )
        path = _write_stack_geotiff(cube, out_path) if out_path else None
        summary = (
            stack_stats(
                cube,
                change_threshold_db=change_threshold_db,
                blocks=blocks,
                block_series=block_series,
                windowed=stats_windowed,
            )
            if stats
            else None
        )

    if path and as_json:
        _shared._emit_render_manifest(
            path,
            items,
            {
                "asset": asset,
                "max_size": max_size,
                "extent": extent,
                "crs": crs or "EPSG:4326",
                "db": db,
                **_shared._acquisition_filter_manifest(
                    polarizations, min_incidence, max_incidence, max_resolution
                ),
            },
            stats=summary,
        )
        return
    if path:
        # When the statistics follow, the note goes to stderr so stdout carries
        # the JSON object alone and pipes into jq.
        click.echo(f"Wrote {len(items)}-band datacube to {path}", err=summary is not None)
    if summary is not None:
        click.echo(json.dumps(summary, indent=2))


@cli.command()
@click.argument("src", type=click.Path(exists=True, dir_okay=False))
@click.argument("dst", type=click.Path(dir_okay=False), required=False)
@click.option(
    "--provenance",
    is_flag=True,
    help="Don't convert: read the conversion provenance umbra-py recorded in "
    "SRC (an already-converted raster) and print it as JSON -- which "
    "calibration, terrain-flattening model, DEM and scale produced those pixel "
    "values. Takes no DST.",
)
@click.option(
    "--slant-plane",
    is_flag=True,
    help="Skip geocoding: write the raw slant-plane amplitude with no "
    "geolocation (inspection only). Default is a north-up EPSG:4326 COG.",
)
@click.option(
    "--linear",
    is_flag=True,
    help="Write linear magnitude instead of the decibel (log-amplitude) scale.",
)
@click.option(
    "--gcp-grid",
    type=int,
    default=15,
    show_default=True,
    help="Edge of the square lattice of ground control points sampled across "
    "the image to model the sensor geometry (geocoded output only).",
)
@click.option(
    "--resolution",
    type=float,
    default=None,
    help="Output pixel size in degrees (geocoded output only). Omit to pick "
    "the finer of the two per-axis ground sample distances.",
)
@click.option(
    "--resampling",
    type=click.Choice(list(RESAMPLING_METHODS), case_sensitive=False),
    default="bilinear",
    show_default=True,
    help="Warp kernel for geocoding.",
)
@click.option(
    "--projection",
    type=click.Choice(["HAE", "PLANE", "DEM"], case_sensitive=False),
    default="HAE",
    show_default=True,
    help="SICD image-projection type. HAE is the flat-earth default (exact "
    "over flat terrain, adequate for map placement elsewhere).",
)
@click.option(
    "--dem",
    type=str,
    default=None,
    metavar="PATH|auto",
    help="Terrain-orthorectify against a digital elevation model instead of the "
    "flat-earth projection. Pass a path to any raster rasterio can open (e.g. a "
    "Copernicus/SRTM COG), or 'auto' to fetch the covering Copernicus GLO-30 "
    "tiles for the scene automatically. Supersedes --projection.",
)
@click.option(
    "--geoid",
    type=str,
    default=None,
    metavar="PATH|auto",
    help="Geoid-undulation grid giving ellipsoid-minus-geoid separation in metres. "
    "Pass a path to any raster rasterio can open (e.g. an EGM96/EGM2008 GeoTIFF), "
    "or 'auto' to fetch a global EGM geoid grid for the scene automatically. "
    "Global DEMs quote height above the geoid but SICD projects against the "
    "ellipsoid, so this converts sampled DEM heights to HAE for survey-grade "
    "placement over relief. Requires --dem.",
)
@click.option(
    "--rtc",
    is_flag=True,
    help="Radiometrically terrain-flatten the geocoded output: scale each pixel "
    "by the cosine correction cos(reference)/cos(local_incidence) from the DEM "
    "slope and scene look geometry, so slopes facing toward or away from the "
    "radar no longer look artificially bright or dark. Requires --dem. A "
    "geometric normalisation of detected amplitude, not a calibrated product.",
)
@click.option(
    "--rtc-ref-angle",
    type=float,
    default=None,
    metavar="DEGREES",
    help="Reference incidence angle (degrees) the --rtc flattening normalises to. "
    "Omit to use the scene incidence angle, which leaves flat terrain unchanged.",
)
@click.option(
    "--rtc-model",
    type=click.Choice(list(RTC_MODELS), case_sensitive=False),
    default="cosine",
    show_default=True,
    help="Terrain-flattening model for --rtc. 'cosine' scales by "
    "cos(reference)/cos(local_incidence) (the 3-D local incidence angle); 'area' "
    "scales by sin(local_range_incidence)/sin(reference), the projected-area / "
    "foreshortening correction in the range plane, which targets range "
    "foreshortening and layover; 'gamma' scales by cos(reference)*nz/"
    "cos(local_incidence), the per-pixel facet-area (gamma-nought) normalisation "
    "that adds the true tilted-facet-area term the other two omit; 'facet' "
    "integrates the illuminated area in the radar's own (slant range, azimuth) "
    "geometry and normalises each pixel by the total accumulated in its cell -- "
    "the only model that measures LAYOVER, where terrain folds several facets "
    "into one cell and their returns sum. On their own all four normalise "
    "detected amplitude; pair one with --calibrate to get a physical product.",
)
@click.option(
    "--calibrate",
    type=click.Choice(list(CALIBRATION_TYPES), case_sensitive=False),
    default=None,
    help="Radiometrically calibrate the output using the SICD's own Radiometric "
    "scale factors, so pixel values are a physical quantity instead of relative "
    "brightness: 'sigma0'/'beta0'/'gamma0' are the backscatter coefficients "
    "referenced to unit ground, slant-plane and perpendicular-to-look area; "
    "'rcs' is the absolute radar cross-section in m2. In the default decibel "
    "scale the output is that coefficient in dB. Composes with --rtc "
    "(--rtc-model facet --calibrate gamma0 is terrain-flattened gamma-nought). "
    "Fails clearly when the product carries no such scale factor -- Umbra's open "
    "products usually don't.",
)
def convert(
    src,
    dst,
    provenance,
    slant_plane,
    linear,
    gcp_grid,
    resolution,
    resampling,
    projection,
    dem,
    geoid,
    rtc,
    rtc_ref_angle,
    rtc_model,
    calibrate,
) -> None:
    """Convert a downloaded SICD (complex) product to a map-ready GeoTIFF.

    By default this geocodes the scene: it detects amplitude and warps it onto
    a north-up EPSG:4326 cloud-optimized GeoTIFF using SICD's own image-
    projection model, so the result opens straight onto a map, in QGIS, or as a
    georeferenced array via ``umbra_py.to_xarray`` -- no hand-rolled geocoding.

    The geocoding is flat-earth (pixels on the scene's height plane): exact over
    flat terrain, adequate for map placement elsewhere. Pass --dem PATH to
    terrain-orthorectify against a digital elevation model instead, so relief is
    placed in its true ground position. Add --rtc (with --dem) to also
    radiometrically terrain-flatten the output, removing the geometric brightness
    swings that slopes cause. Pass --slant-plane for a quick, ungeoreferenced
    amplitude image instead.

    Geocoding and flattening both leave the pixel values *relative* -- an image
    comparable with itself and nothing else. Add --calibrate to make them
    physical: the SICD's own radiometric scale factors turn detected power into
    a backscatter coefficient (sigma0 / beta0 / gamma0) or an absolute radar
    cross-section, so the decibels mean the same thing across scenes and dates.
    It only works where the product supplies those scale factors.

    Every raster written here records how it was made -- the calibration, the
    terrain model and its reference angle, the DEM/geoid, the projection and the
    scale -- in the file's own metadata, so a converted scene can say what its
    pixel values mean. Read it back with --provenance (or gdalinfo).

    SICD/CPHD are the complex products; the ``GEC`` asset is already a geocoded
    COG and needs no conversion. Requires the convert extra
    (``pip install "umbra-py[convert]"``).
    """
    from ..convert import (  # noqa: PLC0415
        read_conversion_tags,
        sicd_to_amplitude_geotiff,
        sicd_to_geocoded_cog,
    )

    if provenance:
        if dst is not None:
            raise click.BadParameter(
                "--provenance reads SRC and writes nothing; drop the DST argument.",
                param_hint="DST",
            )
        tags = read_conversion_tags(src)
        if not tags:
            raise click.ClickException(
                f"{Path(src).name} carries no umbra-py conversion provenance "
                "(it was not written by 'umbra convert')."
            )
        click.echo(json.dumps(tags, indent=2))
        return
    if dst is None:
        raise click.BadParameter("Missing argument 'DST'.", param_hint="DST")

    decibels = not linear
    calibration = calibrate.lower() if calibrate else None
    if slant_plane:
        with OrbitSpinner(f"Reading amplitude from {Path(src).name}"):
            try:
                path = sicd_to_amplitude_geotiff(
                    src, dst, decibels=decibels, calibration=calibration
                )
            except ValueError as exc:  # e.g. the product carries no scale factor
                raise click.ClickException(str(exc)) from exc
        label = f"{calibration}-calibrated " if calibration else ""
        click.echo(f"Wrote slant-plane {label}amplitude GeoTIFF to {path}")
        return

    auto_dem = bool(dem) and dem.lower() == "auto"
    if dem and not auto_dem and not Path(dem).exists():
        raise click.BadParameter(f"DEM path does not exist: {dem}", param_hint="--dem")
    if geoid and not dem:
        raise click.BadParameter(
            "--geoid requires --dem: the geoid correction adjusts DEM heights to "
            "ellipsoidal (HAE).",
            param_hint="--geoid",
        )
    auto_geoid = bool(geoid) and geoid.lower() == "auto"
    if geoid and not auto_geoid and not Path(geoid).exists():
        raise click.BadParameter(f"Geoid path does not exist: {geoid}", param_hint="--geoid")
    if rtc and not dem:
        raise click.BadParameter(
            "--rtc requires --dem: radiometric terrain flattening derives the "
            "local incidence angle from a DEM.",
            param_hint="--rtc",
        )

    label = "Terrain-geocoding" if dem else "Geocoding"
    with OrbitSpinner(f"{label} {Path(src).name}"):
        try:
            path = sicd_to_geocoded_cog(
                src,
                dst,
                decibels=decibels,
                gcp_grid=gcp_grid,
                resolution=resolution,
                resampling=resampling.lower(),
                projection_type=projection.upper(),
                dem=dem,
                geoid=geoid,
                rtc=rtc,
                rtc_reference_deg=rtc_ref_angle,
                rtc_model=rtc_model.lower(),
                calibration=calibration,
            )
        except ValueError as exc:  # e.g. the product carries no scale factor
            raise click.ClickException(str(exc)) from exc
    if rtc:
        kind = "radiometrically terrain-flattened COG"
    elif dem:
        kind = "terrain-orthorectified COG"
    else:
        kind = "geocoded COG"
    if calibration:
        kind = f"{calibration}-calibrated {kind}"
    click.echo(f"Wrote {kind} to {path}")


@cli.command()
@click.argument("item_urls", nargs=-1)
@click.option(
    "--out",
    "out_dir",
    required=True,
    help="Output directory for the chips and manifest (created if needed).",
)
@click.option(
    "--asset",
    default="GEC",
    show_default=True,
    type=click.Choice(CHIPPABLE_ASSETS, case_sensitive=False),
    help="Which product to chip. GEC (the geocoded GeoTIFF) is the sensible "
    "default and streams tile by tile; CSI also works. SICD is the complex "
    "slant-plane product: it has no map grid, so each scene is downloaded whole "
    "and geocoded first (the [convert] extra) before the same tiles are cut -- "
    "which is how a training set reaches the full-resolution archive, and where "
    "--calibrate / --rtc / --dem apply. CPHD is phase history, not an image.",
)
@click.option(
    "--dem",
    type=str,
    default=None,
    metavar="PATH|auto",
    help="SICD only: terrain-orthorectify each scene against a digital elevation "
    "model instead of the flat-earth projection. A path to any raster rasterio "
    "can open, or 'auto' to fetch the covering Copernicus GLO-30 tiles.",
)
@click.option(
    "--geoid",
    type=str,
    default=None,
    metavar="PATH|auto",
    help="SICD only: geoid-undulation grid converting sampled DEM heights to "
    "height-above-ellipsoid, or 'auto' to fetch one. Requires --dem.",
)
@click.option(
    "--rtc",
    is_flag=True,
    help="SICD only: radiometrically terrain-flatten each scene before chipping, "
    "so slopes facing toward or away from the radar don't teach a model their "
    "brightness. Requires --dem.",
)
@click.option(
    "--rtc-model",
    type=click.Choice(list(RTC_MODELS), case_sensitive=False),
    default="cosine",
    show_default=True,
    help="SICD only: terrain-flattening model for --rtc (see 'umbra convert "
    "--help' for what each one corrects).",
)
@click.option(
    "--rtc-ref-angle",
    type=float,
    default=None,
    metavar="DEGREES",
    help="SICD only: reference incidence angle the --rtc flattening normalises "
    "to. Omit to use each scene's own incidence angle.",
)
@click.option(
    "--calibrate",
    type=click.Choice(list(CALIBRATION_TYPES), case_sensitive=False),
    default=None,
    help="SICD only: radiometrically calibrate the pixels using the SICD's own "
    "Radiometric scale factors, so chips carry a physical backscatter "
    "coefficient rather than relative brightness -- the difference between a "
    "model that transfers across scenes and one that doesn't. Composes with "
    "--rtc. Fails clearly when the product carries no such scale factor.",
)
@click.option(
    "--convert-resolution",
    type=float,
    default=None,
    metavar="DEGREES",
    help="SICD only: geocoded pixel size in degrees. Omit to keep the finer of "
    "the two per-axis ground sample distances (throw no resolution away).",
)
@click.option(
    "--resampling",
    type=click.Choice(list(RESAMPLING_METHODS), case_sensitive=False),
    default="bilinear",
    show_default=True,
    help="SICD only: warp kernel used to geocode each scene.",
)
@click.option(
    "--work-dir",
    type=click.Path(file_okay=False),
    default=None,
    help="SICD only: keep the downloaded product and the geocoded COG here "
    "instead of a temporary directory. A re-run then reuses a scene already "
    "geocoded with the same settings rather than fetching and warping it again.",
)
@click.option(
    "--chip-size",
    type=int,
    default=512,
    show_default=True,
    help="Tile edge in pixels. Only full tiles are written; a partial edge "
    "strip is dropped, so every chip has this exact shape.",
)
@click.option(
    "--stride",
    type=int,
    default=None,
    help="Step between tile origins in pixels (default: --chip-size, "
    "non-overlapping). A smaller stride overlaps tiles for dense inference "
    "or augmentation.",
)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["geotiff", "npy"], case_sensitive=False),
    default="geotiff",
    show_default=True,
    help="Chip file format: georeferenced GeoTIFF, or a bare float32 .npy "
    "array (geo metadata then lives only in the manifest).",
)
@click.option(
    "--db",
    is_flag=True,
    help="Write the decibel (log-amplitude) scale instead of linear amplitude.",
)
@click.option(
    "--min-valid",
    type=float,
    default=0.0,
    show_default=True,
    help="Drop a tile whose fraction of valid (finite, positive) pixels is "
    "below this. 0.0 keeps every full tile; e.g. 0.5 drops the mostly-nodata "
    "corners of a rotated footprint.",
)
@click.option(
    "--manifest",
    default="manifest.jsonl",
    show_default=True,
    help="Manifest filename inside --out. A .jsonl writes one chip record per "
    "line (the ML default); a .geojson writes a FeatureCollection of chip "
    "footprints for QGIS / geopandas; a .parquet writes a stac-geoparquet table "
    "DuckDB / geopandas can query at scale (needs the [export] extra).",
)
@click.option(
    "--area", default=None, help="Search an Umbra task/site by name (e.g. 'Centerfield')."
)
@click.option("--bbox", help="Footprint filter: 'min_lon,min_lat,max_lon,max_lat'.")
@_shared._place_option
@_shared._geometry_option
@click.option(
    "--start",
    help="Earliest acquisition date (YYYY-MM-DD or a relative expression).",
)
@click.option("--end", help="Latest acquisition date (same formats as --start).")
@click.option(
    "--max-search",
    type=int,
    default=20,
    show_default=True,
    help="Max acquisitions to gather when searching (ignored with item URLs).",
)
@click.option("--json", "as_json", is_flag=True, help="Emit the dataset summary as JSON.")
@_shared._fuzzy_option
@_shared._acquisition_filter_options
@_shared._local_index_options
@_shared._token_option
def chips(
    item_urls,
    out_dir,
    asset,
    dem,
    geoid,
    rtc,
    rtc_model,
    rtc_ref_angle,
    calibrate,
    convert_resolution,
    resampling,
    work_dir,
    chip_size,
    stride,
    fmt,
    db,
    min_valid,
    manifest,
    area,
    bbox,
    place,
    intersects,
    start,
    end,
    max_search,
    as_json,
    fuzzy,
    polarizations,
    min_incidence,
    max_incidence,
    max_resolution,
    local,
    db_path,
    token,
) -> None:
    """Cut SAR scenes into fixed-size, georeferenced ML training tiles.

    Walks the chosen acquisitions and writes each one's geocoded GeoTIFF as a
    grid of full chip-size tiles (GeoTIFF or .npy), plus a manifest carrying
    per-chip geo + acquisition metadata (bbox, CRS, transform, datetime,
    polarization, incidence angle, resolution, license) -- the data-loading
    layer for SAR foundation-model and change-detection research.

    Two ways to choose what to chip:

    \b
    - Pass STAC JSON URLs directly.
    - Or search: give --area (or --bbox / --place / --intersects) with
      --start/--end and the command gathers a site's acquisitions
      automatically.

    For the amplitude products (GEC, CSI) only the bytes for each tile are
    streamed via HTTP range requests -- no full download, and memory stays
    bounded to one chip. Requires the load extra
    (``pip install "umbra-py[load]"``).

    \b
    --asset SICD chips the complex archive instead: each scene is downloaded
    whole and geocoded before its tiles are cut, so --dem, --rtc and
    --calibrate apply and the chips can carry a physical backscatter
    coefficient. That path needs the convert extra
    (``pip install "umbra-py[convert]"``) and real bytes per scene, so give
    --work-dir to keep the geocoded scenes and make a re-run cheap.
    """
    from ..chips import COMPLEX_ASSETS, SicdConversion, write_chips

    _shared._check_token_not_local(token, local, db_path)
    conversion_flags = {
        "--dem": dem,
        "--geoid": geoid,
        "--rtc": rtc,
        "--rtc-ref-angle": rtc_ref_angle,
        "--calibrate": calibrate,
        "--convert-resolution": convert_resolution,
        "--work-dir": work_dir,
    }
    if asset.upper() not in COMPLEX_ASSETS:
        used = sorted(name for name, value in conversion_flags.items() if value)
        if used:
            raise click.UsageError(
                f"{', '.join(used)} only appl{'ies' if len(used) == 1 else 'y'} to "
                f"--asset SICD; {asset} is already a geocoded amplitude raster."
            )
        conversion = None
    else:
        conversion = SicdConversion(
            dem=dem,
            geoid=geoid,
            rtc=rtc,
            rtc_model=rtc_model,
            rtc_reference_deg=rtc_ref_angle,
            calibration=calibrate,
            resolution=convert_resolution,
            resampling=resampling,
        )
    search_mode = any(v for v in (area, bbox, place, intersects, start, end))
    if item_urls and search_mode:
        raise click.UsageError(
            "Pass item URLs OR search criteria "
            "(--area/--bbox/--place/--intersects/--start/--end), not both."
        )

    if item_urls:
        items = [_shared._item_from_url(url) for url in item_urls]
    else:
        if not (area or bbox or place or intersects):
            raise click.UsageError(
                "Give --area, --bbox, --place or --intersects (optionally with "
                "--start/--end) to search, or pass item URLs directly."
            )
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
            product_types=[asset],
            limit=max_search,
            **_shared._acquisition_filter_kwargs(
                polarizations, min_incidence, max_incidence, max_resolution
            ),
        )
        if not items:
            raise click.ClickException(
                f"Search found no {asset} acquisitions. Widen the date range or area."
            )
        click.echo(f"Chipping {len(items)} acquisition(s) into {out_dir} ...")

    def _report(index: int, total: int, item, written: int) -> None:
        click.echo(f"  [{index}/{total}] {item.id}: {written} chip(s)")

    with OrbitSpinner(f"Chipping into {out_dir}"):
        dataset = write_chips(
            items,
            out_dir,
            asset=asset,
            chip_size=chip_size,
            stride=stride,
            db=db,
            fmt=fmt,
            min_valid=min_valid,
            manifest=manifest,
            progress=None if as_json else _report,
            conversion=conversion,
            work_dir=work_dir,
        )

    if as_json:
        click.echo(json.dumps(dataset.to_dict(), indent=2))
        return
    click.echo(
        f"Wrote {dataset.chip_count} chip(s) from {len({r.item_id for r in dataset.records})} "
        f"acquisition(s) to {dataset.out_dir}"
    )
    if dataset.manifest_path:
        click.echo(f"  manifest -> {dataset.manifest_path}")
