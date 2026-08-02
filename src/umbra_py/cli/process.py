"""Turning scenes into analysis-ready products: ``stack``, ``convert``,
``chips``.

The three commands that write data rather than pictures -- the co-registered
time-series datacube, the SICD -> geocoded COG pipeline (DEM, RTC flattening,
radiometric calibration), and the ML chip set with its manifest.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import click

from .._spinner import OrbitSpinner
from ..chips import CHIPPABLE_ASSETS, ChipDataset
from ..constants import PRODUCT_ASSETS
from ..convert import (
    CALIBRATION_TYPES,
    NOISE_MODELS,
    RESAMPLING_METHODS,
    RTC_MODELS,
    SPECKLE_FILTERS,
    SPECKLE_WINDOW_DEFAULT,
)
from ..load import STACK_EXTENTS
from ..preflight import DEFAULT_PREFLIGHT_WORKERS, PREFLIGHT_ASSET
from ..viz import (
    select_change_frames,
)
from . import _shared
from ._root import cli

#: What each ``--noise-model`` calls the product it writes. The models are named
#: apart everywhere else -- in the provenance tags, in the refusal ``umbra
#: stack`` raises on a mixed series -- so the line that says what was written
#: names them apart too. A measured floor, a constant guess and a fitted profile
#: are not the same claim about the pixels.
_NOISE_LABELS = {
    "measured": "noise-subtracted",
    "estimated": "noise-estimated",
    "estimated-range": "noise-profiled",
}


def _noise_label(noise_model: str) -> str:
    """How a noise-subtracted product describes itself on the way out."""
    return _NOISE_LABELS.get(noise_model.lower(), "noise-estimated")


def _echo_noise_report(path: Path) -> None:
    """Say what the noise subtraction did to the raster just written.

    The subtraction's two documented limits -- it floors whatever sits at the
    sensor's sensitivity limit, and the estimated model assumes the scene
    contained dark ground to read -- were true of every conversion and visible in
    none of them. The numbers that answer both are recorded in the output's own
    ``UMBRA_*`` tags, so they are read back from there rather than returned
    through the conversion functions: what gets printed is then exactly what the
    file will still say tomorrow, and ``sicd_to_geocoded_cog`` keeps returning a
    path.

    The margin note is an advisory, never an error. A scene that is genuinely
    uniform is not a mistake, and the fix where one matters is a *measured*
    floor, not a differently-tuned guess -- so this says what happened and leaves
    the call to the person making it.
    """
    from ..convert import NOISE_MARGIN_WARN_DB, read_conversion_tags  # noqa: PLC0415

    tags = read_conversion_tags(path)
    floored = tags.get("noise_floored_fraction")
    if floored is not None:
        click.echo(
            f"  {float(floored):.1%} of the image is at the sensor's limit after the "
            "subtraction (UMBRA_NOISE_FLOORED_FRACTION)"
        )
    spread = tags.get("noise_floor_spread_db")
    if spread is not None:
        click.echo(
            f"  The fitted floor swings {float(spread):.1f} dB across the swath "
            "(UMBRA_NOISE_FLOOR_SPREAD_DB) -- that is what one constant floor would "
            "have left behind as a gradient"
        )
    margin = tags.get("noise_floor_margin_db")
    if margin is None:
        return
    floor_db = tags.get("noise_floor_db")
    kind = "Fitted floor" if spread is not None else "Estimated floor"
    # The range profile has no single level, so what is quoted is its median --
    # say which, rather than let a reader take it for the number subtracted.
    qualifier = "median " if spread is not None else ""
    level = f" of {qualifier}{float(floor_db):.1f} dB" if floor_db is not None else ""
    click.echo(f"  {kind}{level} sits {float(margin):.1f} dB below the scene median")
    if float(margin) < NOISE_MARGIN_WARN_DB:
        click.echo(
            f"  Note: that is under {NOISE_MARGIN_WARN_DB:g} dB, so this scene had little "
            "dark ground for the estimate to read -- the fifth percentile it subtracted "
            "is likely real backscatter rather than the receiver. Use --noise-model "
            "measured where the product states its own floor."
        )


def _echo_speckle_report(path: Path) -> None:
    """Say what the speckle filter actually achieved on the raster just written.

    A filter's window says how many *pixels* were averaged; the equivalent number
    of looks says how many independent *measurements* that was, which is the
    smaller number on any product sampled finer than it resolves -- as Umbra's
    are. Both are recorded in the output's own tags (the same reason
    :func:`_echo_noise_report` reads them back from there rather than through the
    conversion's return value), so what is printed is what the file will still say
    tomorrow.

    The note below :data:`umbra_py.convert.SPECKLE_ENL_GAIN_WARN` is an advisory,
    never an error: on imagery that is textured everywhere -- dense city, forest --
    ``lee`` is *supposed* to leave most pixels alone, so a small gain there is the
    filter working rather than failing.
    """
    from ..convert import SPECKLE_ENL_GAIN_WARN, read_conversion_tags  # noqa: PLC0415

    tags = read_conversion_tags(path)
    before, after = tags.get("speckle_enl_before"), tags.get("speckle_enl_after")
    window = tags.get("speckle_window")
    looks = tags.get("speckle_looks")
    if looks is not None:
        click.echo(
            f"  Speckle taken as {float(looks):.1f}-look for the filter's own "
            "statistics (UMBRA_SPECKLE_LOOKS)"
        )
    if before is None or after is None:
        # No block of the image was homogeneous enough to read an ENL from --
        # honest, and worth saying, since the filter still ran.
        click.echo(
            "  No homogeneous block to measure looks in, so the ENL gain is unreported "
            "(the filter still ran; UMBRA_SPECKLE_FILTER records it)"
        )
        return
    ceiling = f", of {int(window) ** 2} pixels averaged" if window is not None else ""
    click.echo(
        f"  Equivalent looks {float(before):.1f} -> {float(after):.1f}{ceiling} "
        "(UMBRA_SPECKLE_ENL_BEFORE/AFTER)"
    )
    if float(before) > 0 and float(after) / float(before) < SPECKLE_ENL_GAIN_WARN:
        click.echo(
            f"  Note: that is under {SPECKLE_ENL_GAIN_WARN:g}x, so this window bought "
            "little -- either the scene is textured almost everywhere (which is what "
            "'lee' is meant to leave alone) or the product's pixels are correlated, in "
            "which case a wider --speckle-window is what buys independent looks."
        )


def _echo_chip_skipped_report(dataset: ChipDataset) -> None:
    """Say which acquisitions a run left out, and in whose words.

    Unlike the noise and speckle roll-ups below, this one prints a line *per*
    acquisition rather than a count: a skipped scene is not a diagnostic about a
    scene that is in the dataset, it is a scene that is not, and the reason
    differs per product (one carries no ``Radiometric`` block at all, another
    carries scale factors but no noise level). Silent when nothing was skipped,
    which is every run that did not ask for the flag.

    The last line names the sidecar those same acquisitions were written to,
    because the console is the one place this report reaches somebody who is
    watching and the file is the only place it reaches somebody who is not.
    """
    if not dataset.skipped:
        return
    click.echo(
        f"  Skipped {len(dataset.skipped)} acquisition(s) whose metadata cannot "
        "support the request:"
    )
    for skip in dataset.skipped:
        # The stage is named only where it changes what the line means: a
        # preflighted pass was never downloaded, which is the whole saving.
        where = " [preflight]" if skip.stage == "preflight" else ""
        click.echo(f"    {skip.item_id}{where}: {skip.reason}")
        if skip.hint:
            click.echo(f"      hint: {skip.hint}")
    if dataset.skipped_path:
        click.echo(f"    skipped -> {dataset.skipped_path}")


def _echo_chip_preflight_report(dataset: ChipDataset) -> None:
    """Say what asking the archive first cost, and what it saved.

    The dropped acquisitions themselves are :func:`_echo_chip_skipped_report`'s
    job -- they are holes in the dataset however they were found. This line is
    the other half, and the one that says whether the flag earned its place: the
    headers read against the products not downloaded. Silent when no preflight
    ran, which is every run that did not ask for it.
    """
    summary = dataset.preflight
    if summary is None:
        return
    if not summary.skipped:
        tail = ": every one can support the request."
    elif summary.product_bytes_skipped:
        tail = (
            f" and dropped {summary.skipped}, saving "
            f"{_human_bytes(summary.product_bytes_skipped)} of download."
        )
    else:
        # A source that states no size (a local path with none, a server with no
        # Content-Range) still saved the download; only the number is unknown.
        tail = f" and dropped {summary.skipped} before downloading them."
    click.echo(
        f"  Preflight read {_human_bytes(summary.bytes_read)} of product headers "
        f"from {summary.checked} acquisition(s)" + tail
    )
    if summary.unreadable:
        # Kept in the run rather than dropped, so say so: the batch will find out
        # the expensive way, which is the right response to a failed read.
        click.echo(
            f"    {summary.unreadable} acquisition(s) could not be read ahead of time "
            "and were chipped anyway."
        )


def _echo_chip_noise_report(dataset: ChipDataset) -> None:
    """Say what the noise subtraction did across a chip run's scenes.

    :func:`_echo_noise_report` is the same job for the one raster ``umbra
    convert`` writes. A chip run converts many, so a line per scene would bury
    the only question a dataset builder has -- *were any of these scenes ones the
    estimate should not have been trusted on?* -- under twenty lines saying it
    was fine. What is printed is therefore a count, and the count is silent when
    it is zero: an estimate that held on every scene needs no comment.

    The advisory stays advisory here for the same reason it is one per scene: a
    uniformly bright scene is legitimate imagery, and the honest fix where the
    margin matters is a measured floor, not a differently-tuned guess. The
    numbers are in every manifest record, so a loader can act on them per scene
    rather than take the batch's word for it.
    """
    noise = dataset.noise
    if noise is None:
        return
    models = "/".join(noise.models)
    click.echo(f"  noise floor: {models}, subtracted on {noise.scenes} scene(s)")
    if noise.max_floored_fraction is not None:
        click.echo(
            f"  up to {noise.max_floored_fraction:.1%} of a scene is at the sensor's "
            "limit after the subtraction"
        )
    if noise.low_margin_scenes:
        click.echo(
            f"  Note: {noise.low_margin_scenes} of {noise.margin_scenes} scene(s) had "
            f"under {noise.margin_warn_db:g} dB of margin above the estimated floor "
            f"(narrowest {noise.min_margin_db:.1f} dB) -- those scenes had little dark "
            "ground for the estimate to read, so what came off them is likely real "
            "backscatter. Filter the manifest on noise_floor_margin_db, or use "
            "--noise-model measured where the products state their own floor."
        )


def _echo_chip_speckle_report(dataset: ChipDataset) -> None:
    """Say what the speckle filter achieved across a chip run's scenes.

    :func:`_echo_speckle_report` is the same job for the one raster ``umbra
    convert`` writes, and this is its batch form for the reason
    :func:`_echo_chip_noise_report` is the noise report's: the question is about
    the *set*, not about each scene, so what is printed is the typical gain and a
    count of the scenes the window bought little on. Silent when no filter ran.

    The gain rather than the level, because both levels read low on a textured
    scene -- the ratio is what says the window did something. And an advisory
    rather than an error, because on imagery textured everywhere ``lee`` leaving
    most pixels alone is the filter working.
    """
    speckle = dataset.speckle
    if speckle is None:
        return
    filters = "/".join(speckle.filters)
    windows = "/".join(f"{w}x{w}" for w in speckle.windows)
    click.echo(f"  speckle: {filters} over {windows}, on {speckle.scenes} scene(s)")
    if speckle.median_gain is None:
        # No block of any sampled window was homogeneous enough to read an ENL
        # from -- honest, and worth saying, since the filter still ran.
        click.echo(
            "  no homogeneous block to measure looks in, so the ENL gain is unreported "
            "(the filter still ran; every record's speckle_filter says so)"
        )
        return
    click.echo(
        f"  equivalent looks up by {speckle.median_gain:.1f}x on the median scene "
        f"(speckle_enl_before/after in every record)"
    )
    if speckle.low_gain_scenes:
        click.echo(
            f"  Note: {speckle.low_gain_scenes} of {speckle.gain_scenes} scene(s) gained "
            f"under {speckle.gain_warn:g}x (worst {speckle.min_gain:.1f}x) -- those scenes "
            "are textured almost everywhere (which is what 'lee' is meant to leave alone) "
            "or their pixels are correlated, in which case a wider --speckle-window is "
            "what buys independent looks."
        )


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
    "--speckle-filter",
    type=click.Choice(list(SPECKLE_FILTERS), case_sensitive=False),
    default=None,
    help="Average speckle down in every pass, on the shared grid, before the "
    "cube is assembled. Speckle is the interference pattern coherent "
    "illumination makes on a rough surface, so a single look's power scatters "
    "about the surface's true backscatter as widely as its own mean -- which "
    "makes it the dominant uncertainty in every number --stats reports, and a "
    "cell-by-cell difference between two passes mostly interference rather than "
    "change. 'boxcar' averages the window unconditionally (the multilook); 'lee' "
    "averages only where the window is no more variable than speckle alone "
    "explains, so edges and points survive. Not a default: what it spends is "
    "resolution. The cube records the filter and its window, so the statistics "
    "state the trade and a later stack refuses to difference it against an "
    "unfiltered cube. (Filters the published GEC rasters too, which 'umbra "
    "convert --speckle-filter' cannot reach.)",
)
@click.option(
    "--speckle-window",
    type=int,
    default=SPECKLE_WINDOW_DEFAULT,
    show_default=True,
    metavar="CELLS",
    help="Edge of the odd, centred window --speckle-filter averages over, in "
    "cells of the shared grid (so --max-size decides what it covers on the "
    "ground). Wider removes more speckle and more detail; it costs no more to "
    "compute.",
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
    "decent fraction of --max-size (e.g. 1024). Same output -- including under "
    "--speckle-filter, where each window is read with a half-window halo so the "
    "filter never sees a truncated window at a chunk edge.",
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
    speckle_filter,
    speckle_window,
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

    --speckle-filter averages down the one uncertainty every number above
    otherwise carries. A single-look SAR pixel's power scatters about its
    surface's true backscatter as widely as its own mean, so an unfiltered
    cell-to-cell difference is mostly interference; averaging is the only
    correction, and this is the only place it reaches the published GEC
    rasters ('umbra convert --speckle-filter' filters complex products before
    geocoding, so it never sees them). Each pass is filtered on the shared
    grid, and the cube records the filter and its window -- so --stats states
    both halves of the trade (less noisy cells, coarser resolution) and a later
    stack refuses to difference this cube against an unfiltered one.

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
            speckle_filter=speckle_filter,
            speckle_window=speckle_window,
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
                # Only when it ran: a key that said "none" on every unfiltered
                # cube would be noise in the manifest of the common case.
                **(
                    {"speckle_filter": speckle_filter, "speckle_window": speckle_window}
                    if speckle_filter
                    else {}
                ),
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
    "--noise-check",
    is_flag=True,
    help="Don't convert: score the inferred noise floors (--noise-model "
    "estimated / estimated-range) against the floor SRC's own metadata states, "
    "and print the comparison as JSON -- how far each estimate reads low, and "
    "how well it follows the real floor across the swath once that offset is "
    "granted. Needs a product declaring an ABSOLUTE noise level. Takes no DST.",
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
@click.option(
    "--subtract-noise",
    is_flag=True,
    help="Subtract the receiver's own thermal-noise floor (the SICD's "
    "Radiometric.NoiseLevel polynomial) from pixel power before anything scales "
    "it, so low-backscatter surfaces -- calm water, radar shadow, dry sand -- "
    "report the ground instead of the sensor's sensitivity limit. Applied first, "
    "because noise adds where calibration and --rtc multiply. Where the floor "
    "comes from is --noise-model.",
)
@click.option(
    "--noise-model",
    type=click.Choice(list(NOISE_MODELS), case_sensitive=False),
    default="measured",
    show_default=True,
    help="Where --subtract-noise gets the floor. 'measured' reads the product's "
    "own Radiometric.NoiseLevel polynomial, so the floor follows the across-swath "
    "variation the sensor states -- but it needs an ABSOLUTE noise level and "
    "fails clearly without one, which is most of Umbra's open archive. "
    "'estimated' infers one constant floor from the scene's own darkest pixels "
    "(a SAR image's water, shadow and smooth ground return essentially nothing, "
    "so the low tail of its power distribution is the receiver), needs no "
    "metadata, and is recorded as an inference. 'estimated-range' takes that same "
    "read per range line and fits it against range, so an inferred floor follows "
    "the swath instead of leaving a gradient behind, and reports the swing it "
    "found in UMBRA_NOISE_FLOOR_SPREAD_DB. All three record themselves apart in "
    "UMBRA_NOISE_SUBTRACTION, and 'umbra stack' refuses to difference a series "
    "that mixes any two of them.",
)
@click.option(
    "--speckle-filter",
    type=click.Choice(list(SPECKLE_FILTERS), case_sensitive=False),
    default=None,
    help="Speckle-filter the detected power before geocoding. Speckle is not "
    "sensor noise and no floor subtraction removes it: coherent illumination of a "
    "rough surface interferes with itself, so a single-look pixel's power scatters "
    "about the surface's true backscatter with a standard deviation equal to its "
    "mean -- which is why a pixel-by-pixel difference between two passes is mostly "
    "speckle. Averaging is the only correction. 'boxcar' averages the window "
    "unconditionally (the multilook: most variance removed, blind to edges); 'lee' "
    "averages only where the window is no more variable than speckle alone "
    "explains, so edges and points survive. Not a default, because what it spends "
    "is resolution -- a window that averages N pixels reports ground N pixels "
    "across. The raster records the filter, the window and the equivalent looks it "
    "reached, and 'umbra stack' refuses to difference a series that mixes two.",
)
@click.option(
    "--speckle-window",
    type=int,
    default=SPECKLE_WINDOW_DEFAULT,
    show_default=True,
    metavar="PIXELS",
    help="Edge of the odd, centred window --speckle-filter averages over. Wider "
    "removes more speckle and more detail; it costs no more to compute.",
)
@click.option(
    "--clip-bbox",
    default=None,
    help="Convert only a lon/lat window 'min_lon,min_lat,max_lon,max_lat' of the "
    "scene. Only the image rows and columns covering that ground are read from "
    "the product and warped, and the output is cropped to the window, so a small "
    "area of interest costs a small conversion instead of a whole-scene one. The "
    "download is whole-product either way -- a slant-plane NITF has no map grid "
    "to range-read.",
)
def convert(
    src,
    dst,
    provenance,
    noise_check,
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
    subtract_noise,
    noise_model,
    speckle_filter,
    speckle_window,
    clip_bbox,
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

    A scene is tens of square kilometres at 16-25 cm, and all of the above is
    proportional to it. Pass --clip-bbox to make it proportional to the area you
    care about instead: only the image window covering that ground is read and
    warped, and the output is cropped to it.

    Geocoding and flattening both leave the pixel values *relative* -- an image
    comparable with itself and nothing else. Add --calibrate to make them
    physical: the SICD's own radiometric scale factors turn detected power into
    a backscatter coefficient (sigma0 / beta0 / gamma0) or an absolute radar
    cross-section, so the decibels mean the same thing across scenes and dates.
    It only works where the product supplies those scale factors.

    A measured pixel is the ground's echo plus the receiver's own thermal noise,
    and over a dark surface the second term is most of it -- so a calibrated
    value there can be precise, physical and still be a report of the sensor
    rather than the scene. Add --subtract-noise to take that floor off first,
    where noise actually adds. By default the floor is the product's own stated
    one; --noise-model estimated infers it from the scene's own darkest pixels
    instead, which is what works on Umbra's open products, since they generally
    carry no noise metadata to read, and --noise-model estimated-range infers one
    per range line and fits it against range, so an inferred floor follows the
    swath rather than leaving a gradient the constant one could not. Whichever
    floor ran, the conversion then reports what the subtraction did to this
    scene: how much of the image it drove to the sensor's sensitivity limit, and
    -- for an inferred floor, which assumes the scene contained dark ground to
    read -- how far the scene's median sat above it, plus the swing a fitted
    profile found. A narrow margin says that assumption did not hold here. Where
    a product states its own floor there is a truth to check those inferences
    against: --noise-check converts nothing and scores them against it instead,
    reporting how far each estimate reads low and how well it follows the real
    floor across the swath once that offset is granted.

    What is left after all of that is speckle, which is not an error at all: a
    coherently illuminated rough surface interferes with itself, so one look's
    power scatters about the surface's true backscatter as widely as its own mean.
    It is the dominant uncertainty in every number above, and averaging is the only
    correction. --speckle-filter does it -- 'boxcar' unconditionally, 'lee' only
    where the window is more uniform than an edge would be -- and reports the
    equivalent looks the scene reached, which on imagery sampled finer than it
    resolves is well under the pixels averaged. It is opt-in because what it
    spends is resolution, which is the reason to use this archive.

    Every raster written here records how it was made -- the calibration, the
    terrain model and its reference angle, the DEM/geoid, the projection and the
    scale -- in the file's own metadata, so a converted scene can say what its
    pixel values mean. Read it back with --provenance (or gdalinfo).

    SICD/CPHD are the complex products; the ``GEC`` asset is already a geocoded
    COG and needs no conversion. Requires the convert extra
    (``pip install "umbra-py[convert]"``).
    """
    from ..convert import (  # noqa: PLC0415
        compare_noise_models,
        read_conversion_tags,
        sicd_to_amplitude_geotiff,
        sicd_to_geocoded_cog,
    )

    if provenance and noise_check:
        raise click.BadParameter(
            "--provenance reads an already-converted raster's tags and "
            "--noise-check reads a SICD's pixels; pick one.",
            param_hint="--noise-check",
        )
    if noise_check:
        if dst is not None:
            raise click.BadParameter(
                "--noise-check reads SRC and writes nothing; drop the DST argument.",
                param_hint="DST",
            )
        try:
            comparison = compare_noise_models(src, bbox=_shared._parse_bbox(clip_bbox))
        except ValueError as exc:  # e.g. the product states no absolute floor
            raise click.ClickException(str(exc)) from exc
        click.echo(json.dumps(asdict(comparison), indent=2))
        return
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
    speckle = speckle_filter.lower() if speckle_filter else None
    if speckle:
        from ..convert import _check_speckle_window  # noqa: PLC0415

        try:
            # One rule, checked in the library and reported as a parameter error
            # here -- a bad window is a typo, not a conversion failure.
            _check_speckle_window(speckle_window)
        except ValueError as exc:
            raise click.BadParameter(str(exc), param_hint="--speckle-window") from exc
    clip = _shared._parse_bbox(clip_bbox)
    if slant_plane and clip is not None:
        raise click.BadParameter(
            "--clip-bbox needs the geocoded path: the slant-plane image has no map "
            "grid, so there is no ground rectangle to cut from it. Drop "
            "--slant-plane to convert an area of interest.",
            param_hint="--clip-bbox",
        )
    if slant_plane:
        with OrbitSpinner(f"Reading amplitude from {Path(src).name}"):
            try:
                path = sicd_to_amplitude_geotiff(
                    src,
                    dst,
                    decibels=decibels,
                    calibration=calibration,
                    noise_subtract=subtract_noise,
                    noise_model=noise_model.lower(),
                    speckle_filter=speckle,
                    speckle_window=speckle_window,
                )
            except ValueError as exc:  # e.g. the product carries no scale factor
                raise click.ClickException(str(exc)) from exc
        label = f"{calibration}-calibrated " if calibration else ""
        if speckle:
            label = f"{speckle}-filtered {label}"
        if subtract_noise:
            label = f"{_noise_label(noise_model)} {label}"
        click.echo(f"Wrote slant-plane {label}amplitude GeoTIFF to {path}")
        if subtract_noise:
            _echo_noise_report(path)
        if speckle:
            _echo_speckle_report(path)
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
                noise_subtract=subtract_noise,
                noise_model=noise_model.lower(),
                speckle_filter=speckle,
                speckle_window=speckle_window,
                bbox=clip,
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
    if speckle:
        kind = f"{speckle}-filtered {kind}"
    if subtract_noise:
        kind = f"{_noise_label(noise_model)} {kind}"
    click.echo(f"Wrote {kind} to {path}")
    if subtract_noise:
        _echo_noise_report(path)
    if speckle:
        _echo_speckle_report(path)


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
    "--subtract-noise",
    is_flag=True,
    help="SICD only: subtract the receiver's own thermal-noise floor (the SICD's "
    "Radiometric.NoiseLevel polynomial) from pixel power before anything scales "
    "it, so a chip over water or shadow teaches a model the ground rather than "
    "the sensor's sensitivity limit. Where the floor comes from is --noise-model.",
)
@click.option(
    "--noise-model",
    type=click.Choice(list(NOISE_MODELS), case_sensitive=False),
    default="measured",
    show_default=True,
    help="SICD only: where --subtract-noise gets the floor. 'measured' reads the "
    "product's own Radiometric.NoiseLevel and fails clearly without an ABSOLUTE "
    "level -- which is most of Umbra's open archive; 'estimated' infers one "
    "constant floor per scene from its own darkest pixels and needs no metadata; "
    "'estimated-range' infers one per range line and fits it against range, so "
    "chips cut from opposite edges of a swath are not offset by the floor the "
    "constant model left behind. Each chip's manifest entry records which ran, so "
    "a training set never mixes two floors without saying so.",
)
@click.option(
    "--speckle-filter",
    type=click.Choice(list(SPECKLE_FILTERS), case_sensitive=False),
    default=None,
    help="Speckle-filter every scene, so a tile teaches a model the surface "
    "rather than the interference pattern coherent illumination made on it (a "
    "single look's power scatters as widely as its own mean). 'boxcar' averages "
    "the window unconditionally; 'lee' averages only where the window is no more "
    "variable than speckle alone explains, keeping edges. It runs wherever it is "
    "most correct for the asset: on GEC/CSI the tiles themselves are averaged; "
    "with --asset SICD the scene is, in the radar's own image space before it is "
    "geocoded. What it spends is resolution -- a window that averages N pixels "
    "resolves ground N pixels across -- so every chip's manifest entry records "
    "the filter, its window, and the equivalent looks either side of it.",
)
@click.option(
    "--speckle-window",
    type=int,
    default=SPECKLE_WINDOW_DEFAULT,
    show_default=True,
    metavar="PIXELS",
    help="Edge of the odd, centred window --speckle-filter averages over. Wider "
    "removes more speckle and more detail; it costs no more to compute.",
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
    "--clip-bbox",
    default=None,
    help="Chip only a lon/lat window 'min_lon,min_lat,max_lon,max_lat' of each "
    "acquisition, numbering rows/columns from its corner. With --asset SICD it "
    "is also the conversion's clip, so each scene is geocoded over the area of "
    "interest rather than whole -- the expensive step then costs what the site "
    "costs, not what the scene does. (Distinct from --bbox, which filters which "
    "acquisitions the search returns.)",
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
    "--skip-unsupported",
    is_flag=True,
    help="Carry on past an acquisition whose own metadata cannot support the "
    "measurement asked of it (no Radiometric block for --calibrate, no stated "
    "noise floor for --noise-model measured, no stated collection geometry for "
    "--rtc) instead of ending the run on it, "
    "and report which ones were left out. Without it the first such product "
    "costs the whole batch; with it the dataset says where its holes are.",
)
@click.option(
    "--preflight",
    is_flag=True,
    help="With --asset SICD, read each product's metadata over the wire first "
    "(two HTTP range requests, tens of kilobytes) and drop the acquisitions that "
    "cannot support the request before downloading any of them -- so discovering "
    "that a pass carries no Radiometric block (or, under --rtc, no collection "
    "geometry) costs its header rather than the "
    "whole multi-gigabyte product. The dropped passes are reported exactly as "
    "--skip-unsupported reports them. Worth passing both: this asks only what "
    "the metadata answers.",
)
@click.option(
    "--preflight-workers",
    type=int,
    default=DEFAULT_PREFLIGHT_WORKERS,
    show_default=True,
    help="How many product headers --preflight reads in parallel (1 to read them one at a time).",
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
    subtract_noise,
    noise_model,
    speckle_filter,
    speckle_window,
    convert_resolution,
    resampling,
    work_dir,
    chip_size,
    stride,
    fmt,
    db,
    min_valid,
    clip_bbox,
    manifest,
    skip_unsupported,
    preflight,
    preflight_workers,
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
    --speckle-filter applies to any asset. It averages down the one uncertainty
    a SAR pixel carries that is not the sensor's fault and is larger than any
    that is: on a single look, power scatters about the surface's true
    backscatter as widely as its own mean, so an unfiltered tile teaches a model
    the interference pattern as much as the ground. On GEC/CSI the tiles
    themselves are averaged (each read with a halo, so overlapping tiles agree);
    with --asset SICD the scene is, before geocoding. What it costs is
    resolution, which is why it is opt-in and in every manifest record.

    \b
    --asset SICD chips the complex archive instead: each scene is downloaded
    whole and geocoded before its tiles are cut, so --dem, --rtc,
    --calibrate and --subtract-noise apply too and the chips can
    carry a physical backscatter coefficient with the sensor's own noise floor
    taken off. That
    path needs the convert extra
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
        "--subtract-noise": subtract_noise,
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
            noise_subtract=subtract_noise,
            noise_model=noise_model.lower(),
            resolution=convert_resolution,
            resampling=resampling,
        )
    if preflight and asset.upper() not in COMPLEX_ASSETS:
        # Checked before anything is fetched, for the reason the speckle window
        # below is: a flag that cannot apply is a parameter error, not a
        # discovery to make after a search has already run.
        raise click.UsageError(
            f"--preflight applies to the complex products ({', '.join(COMPLEX_ASSETS)}); "
            f"--asset {asset} is an amplitude raster, which carries no SICD metadata to "
            "ask and is streamed tile by tile rather than downloaded."
        )
    if preflight_workers < 1:
        raise click.BadParameter(
            "must be 1 or more (1 reads the product headers one at a time).",
            param_hint="--preflight-workers",
        )
    speckle = speckle_filter.lower() if speckle_filter else None
    if speckle:
        # Checked here so an even window is a parameter error naming the flag,
        # rather than a ValueError raised part-way through a run that has
        # already streamed scenes.
        from ..convert import _check_speckle_window  # noqa: PLC0415

        try:
            _check_speckle_window(speckle_window)
        except ValueError as exc:
            raise click.BadParameter(str(exc), param_hint="--speckle-window") from exc
    clip = _shared._parse_bbox(clip_bbox)
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

    def _report_preflight(index: int, total: int, item, result) -> None:
        click.echo(_preflight_line(result))

    if preflight and not as_json:
        click.echo(f"Preflighting {len(items)} product header(s) ...")

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
            bbox=clip,
            speckle_filter=speckle,
            speckle_window=speckle_window,
            manifest=manifest,
            progress=None if as_json else _report,
            conversion=conversion,
            work_dir=work_dir,
            skip_unsupported=skip_unsupported,
            preflight=preflight,
            preflight_progress=None if as_json else _report_preflight,
            preflight_workers=preflight_workers,
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
    _echo_chip_preflight_report(dataset)
    _echo_chip_skipped_report(dataset)
    _echo_chip_noise_report(dataset)
    _echo_chip_speckle_report(dataset)


def _human_bytes(count: int | None) -> str:
    """A byte count as the largest unit it reads cleanly in."""
    if count is None:
        return "unknown"
    size = float(count)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"  # pragma: no cover - unreachable, the loop returns


def _preflight_line(result) -> str:
    """One acquisition's verdict, as the report prints it."""
    caps = result.capabilities
    if caps is None:
        return f"  {result.item_id}: could not be read -- {result.error}"
    cals = ", ".join(caps.calibrations) if caps.calibrations else "none"
    noise = caps.noise_level or "none"
    geometry = "none" if caps.look_geometry is None else f"{caps.look_geometry[0]:.1f} deg"
    verdict = "yes" if result.supported else "no"
    return (
        f"  {result.item_id}: calibrations {cals}; noise level {noise}; "
        f"look geometry {geometry} -> {verdict}"
    )


@cli.command()
@click.argument("item_urls", nargs=-1)
@click.option(
    "--calibrate",
    type=click.Choice(list(CALIBRATION_TYPES), case_sensitive=False),
    default=None,
    help="Ask whether each product could be radiometrically calibrated this way "
    "(the same choice --calibrate takes on convert/chips).",
)
@click.option(
    "--subtract-noise",
    is_flag=True,
    help="Ask whether each product's noise floor could be subtracted. Only "
    "--noise-model measured depends on the metadata; the inferred models read the "
    "scene's own pixels and so need nothing from a preflight.",
)
@click.option(
    "--noise-model",
    type=click.Choice(list(NOISE_MODELS), case_sensitive=False),
    default="measured",
    show_default=True,
    help="Which floor --subtract-noise would use (see convert --noise-model).",
)
@click.option(
    "--rtc",
    is_flag=True,
    help="Ask whether each product states the collection geometry radiometric "
    "terrain flattening needs (SCPCOA). Most do; the ones that do not refuse a "
    "--rtc run only after the download, the DEM fetch and the warp.",
)
@click.option(
    "--workers",
    type=int,
    default=DEFAULT_PREFLIGHT_WORKERS,
    show_default=True,
    help="How many product headers to read in parallel (1 to read them one at a "
    "time). The verdicts and their order are the same at any width.",
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
@click.option("--json", "as_json", is_flag=True, help="Emit the report as JSON.")
@_shared._fuzzy_option
@_shared._acquisition_filter_options
@_shared._local_index_options
@_shared._token_option
def preflight(
    item_urls,
    calibrate,
    subtract_noise,
    noise_model,
    rtc,
    workers,
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
    """Ask which complex acquisitions can support a measurement, before downloading any.

    Radiometric calibration and a measured noise floor both read polynomials out
    of the SICD's own Radiometric metadata, which Umbra's open products generally
    do not carry -- so `umbra convert --calibrate` and `umbra chips --calibrate`
    refuse on them, by design. Terrain flattening (--rtc) reads the collection
    geometry out of the same file's SCPCOA block. Finding out which passes can
    answer used to cost one whole-product download each: a SICD's metadata lives
    inside the NITF.

    This reads it over the wire instead. A NITF states its own layout, so the SICD
    XML is located and fetched with two HTTP range requests -- tens of kilobytes of
    a multi-gigabyte product -- and the verdict is the conversion's own support
    check applied to it. Over a site's twenty passes that is the difference between
    a few hundred kilobytes and tens of gigabytes.

    \b
    Two ways to choose what to ask about:
    - Pass STAC JSON URLs directly.
    - Or search: give --area (or --bbox / --place / --intersects) with
      --start/--end.

    Needs no extra: the parse is stdlib, so "can this archive answer my question?"
    is answerable from a core install.
    """
    from ..preflight import preflight_items

    _shared._check_token_not_local(token, local, db_path)
    if workers < 1:
        raise click.BadParameter(
            "must be 1 or more (1 reads the product headers one at a time).",
            param_hint="--workers",
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
            product_types=[PREFLIGHT_ASSET],
            limit=max_search,
            **_shared._acquisition_filter_kwargs(
                polarizations, min_incidence, max_incidence, max_resolution
            ),
        )
        if not items:
            raise click.ClickException(
                f"Search found no {PREFLIGHT_ASSET} acquisitions. Widen the date range or area."
            )

    def _report(index: int, total: int, item, result) -> None:
        click.echo(_preflight_line(result))

    with OrbitSpinner(f"Reading {len(items)} product header(s)"):
        report = preflight_items(
            items,
            calibration=calibrate.lower() if calibrate else None,
            noise_subtract=subtract_noise,
            noise_model=noise_model.lower(),
            rtc=rtc,
            progress=None if as_json else _report,
            workers=workers,
        )

    if as_json:
        click.echo(json.dumps(report.to_dict(), indent=2))
        return

    asked = []
    if calibrate:
        asked.append(f"--calibrate {calibrate.lower()}")
    if subtract_noise:
        asked.append(f"--subtract-noise --noise-model {noise_model.lower()}")
    if rtc:
        asked.append("--rtc")
    what = " ".join(asked) if asked else "conversion"
    click.echo(f"{len(report.supported)} of {len(report.results)} acquisition(s) support {what}.")
    click.echo(
        f"  Read {_human_bytes(report.bytes_read)} of product headers"
        + (
            f" instead of {_human_bytes(report.product_bytes)} of product."
            if report.product_bytes
            else "."
        )
    )
    hints = {r.hint for r in report.unsupported if r.hint}
    for hint in sorted(hints):
        click.echo(f"  hint: {hint}")
