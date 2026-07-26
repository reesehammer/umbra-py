"""Multi-temporal composites: change, timescan and animation.

The comparison half of ``viz`` (requires the ``viz`` extra plus rasterio).
Several passes over one site arrive in whatever grid each acquisition used, so
everything here starts by co-registering them onto a shared grid
(:func:`_coregister_bands`) and then reduces the stack to one picture: a
date-colored change composite (unchanged ground stays gray, anything that
appeared or vanished lights up), a whole-series timescan (mean / peak /
variability as RGB), or a labelled animated GIF.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from ..exceptions import AssetNotFoundError
from ..models import UmbraItem
from ._deps import _require
from .raster import _normalize_band, _stretch_to_rgba


def _compose_change_rgba(
    bands: list[Any],
    *,
    percentile: tuple[float, float] = (2.0, 98.0),
    db: bool = False,
) -> Any:
    """Stack 2-3 co-registered SAR bands into an RGBA change composite.

    Each band is percentile-stretched independently, then assigned to a
    color channel by acquisition order:

    - **Two dates** map to ``R = t1, G = t2, B = t1``. An unchanged pixel
      (``t1 == t2``) lands on the gray diagonal; a pixel that brightened
      only in the later pass shows **green**; one that dimmed shows
      **magenta**. This is the classic two-date SAR change product.
    - **Three dates** map straight to ``R/G/B`` -- a temporal-RGB where
      stationary scene stays gray and anything that changed between passes
      is tinted by *when* it was bright.

    All bands must already share a pixel grid (use :func:`_coregister_bands`).
    A pixel invalid in *any* band is made transparent, so the composite
    only colors ground seen on every pass.
    """
    np = _require("numpy")
    n = len(bands)
    if n not in (2, 3):
        raise ValueError(f"change composite needs 2 or 3 bands, got {n}.")
    shape = np.asarray(bands[0]).shape
    if any(np.asarray(b).shape != shape for b in bands):
        raise ValueError("all bands must share the same shape; co-register first.")

    norms: list[Any] = []
    invalid = np.zeros(shape, dtype=bool)
    for band in bands:
        norm, inv = _normalize_band(band, percentile=percentile, db=db)
        norms.append(norm)
        invalid |= inv

    order = (0, 1, 0) if n == 2 else (0, 1, 2)
    rgb = np.stack([(norms[i] * 255.0).astype("uint8") for i in order], axis=-1)
    alpha = np.where(invalid, 0, 255).astype("uint8")
    return np.dstack([rgb, alpha])


def _stretch_stat(stat: Any, valid: Any, percentile: tuple[float, float]) -> Any:
    """Percentile-stretch a 2D statistic map to ``[0, 1]`` using an explicit mask.

    Unlike :func:`_normalize_band`, which treats non-positive pixels as
    nodata, the temporal statistics fed here have meaningful zeros and
    negatives -- a perfectly stable pixel has ``std == 0`` (and should read
    dark, not transparent), and a dB mean is routinely negative. So validity
    is passed in explicitly rather than re-derived from the sign of the data.
    """
    np = _require("numpy")
    vals = stat[valid]
    lo, hi = np.percentile(vals, percentile)
    if hi <= lo:
        hi = lo + 1.0
    safe = np.where(valid, stat, lo)
    return np.clip((safe - lo) / (hi - lo), 0.0, 1.0)


def _compose_timescan_rgba(
    bands: list[Any],
    *,
    percentile: tuple[float, float] = (2.0, 98.0),
    db: bool = False,
) -> Any:
    """Collapse N co-registered SAR bands into a temporal-statistics RGBA.

    Where :func:`_compose_change_rgba` assigns 2-3 individual dates to color
    channels, this summarises an arbitrarily long time series per pixel and
    maps the *statistics* to color:

    - **R = temporal mean** -- the pixel's average backscatter level.
    - **G = temporal max** -- the brightest it ever got.
    - **B = temporal standard deviation** -- how much it varied over time.

    A scene that never changes has ``mean ≈ max`` and ``std ≈ 0`` -> it reads
    gray/yellow (no blue). A pixel that flickers bright and dark -- a berth
    that ships cycle through, a lot that fills and empties, a field that
    floods -- has high std and lights up **blue/cyan**. So the composite turns
    "where did *activity* happen across the whole series" into a single
    glanceable image, which no individual date or 2-date change product shows.

    Each statistic is percentile-stretched independently (mean and max share
    amplitude units; std is its own quantity). With ``db`` the per-pixel stack
    is converted to decibels *before* the statistics, so variability is
    measured in the radiometrically-meaningful log domain.

    All bands must share a pixel grid (use :func:`_coregister_bands`). A pixel
    invalid in *any* pass is transparent, so every statistic is computed over
    the same number of samples everywhere it's colored. Needs >= 3 bands; for
    two dates use :func:`_compose_change_rgba`.
    """
    np = _require("numpy")
    n = len(bands)
    if n < 3:
        raise ValueError(
            f"timescan composite needs at least 3 bands, got {n}; "
            "for two dates use the change composite."
        )
    shape = np.asarray(bands[0]).shape
    if any(np.asarray(b).shape != shape for b in bands):
        raise ValueError("all bands must share the same shape; co-register first.")

    stack = np.stack([np.asarray(b, dtype="float64") for b in bands], axis=0)
    invalid_each = ~np.isfinite(stack) | (stack <= 0)
    invalid = invalid_each.any(axis=0)
    if invalid.all():
        raise ValueError("Time series has no pixel valid on every pass to summarise.")

    if db:
        with np.errstate(divide="ignore", invalid="ignore"):
            stack = np.where(invalid_each, np.nan, 20.0 * np.log10(stack))
    else:
        stack = np.where(invalid_each, np.nan, stack)

    valid = ~invalid
    # nan-aware so fully-invalid columns don't poison the stats; those pixels
    # are masked out by `valid` before the stretch anyway.
    with np.errstate(invalid="ignore"):
        mean = np.nanmean(stack, axis=0)
        mx = np.nanmax(stack, axis=0)
        std = np.nanstd(stack, axis=0)

    channels = [
        _stretch_stat(mean, valid, percentile),
        _stretch_stat(mx, valid, percentile),
        _stretch_stat(std, valid, percentile),
    ]
    rgb = np.stack([(c * 255.0).astype("uint8") for c in channels], axis=-1)
    alpha = np.where(invalid, 0, 255).astype("uint8")
    return np.dstack([rgb, alpha])


def _coregister_bands(
    items: list[UmbraItem],
    asset: str,
    max_size: int,
) -> tuple[list[Any], tuple[float, float, float, float]]:
    """Read each item's SAR band onto one shared EPSG:4326 grid.

    Returns ``(bands, bounds)`` where ``bands`` is a list of 2D arrays --
    one per item, all the same shape and pixel-aligned -- and ``bounds`` is
    the geographic intersection ``(left, bottom, right, top)`` they cover.
    Each source cloud-optimized GeoTIFF is read at a downsampled resolution
    via range requests and warped to lon/lat so the same output pixel
    refers to the same ground location across dates -- the prerequisite for
    an honest change comparison.

    Raises ``ValueError`` when the footprints don't overlap (nothing to
    compare).
    """
    rasterio = _require("rasterio")
    _require("numpy")
    from rasterio.enums import Resampling  # noqa: PLC0415
    from rasterio.vrt import WarpedVRT  # noqa: PLC0415

    datasets: list[Any] = []
    vrts: list[Any] = []
    try:
        for item in items:
            url = item.asset_href(asset)
            if not url:
                raise AssetNotFoundError(
                    f"Item {item.id!r} has no resolvable URL for asset {asset!r}."
                )
            ds = rasterio.open(f"/vsicurl/{url}")
            datasets.append(ds)
            # A full-resolution warp to lon/lat. Cheap to construct -- nothing
            # is read until we do a *decimated* windowed read below, which
            # lets GDAL pull the matching cloud-optimized GeoTIFF overview
            # instead of every full-res tile. (Reading a coarse WarpedVRT
            # whole, by contrast, forces a full-res source read and thousands
            # of range requests -- effectively a hang over the network.)
            vrts.append(WarpedVRT(ds, crs="EPSG:4326", resampling=Resampling.average))

        # Intersection of the (already lon/lat) warped footprints.
        left = max(v.bounds.left for v in vrts)
        bottom = max(v.bounds.bottom for v in vrts)
        right = min(v.bounds.right for v in vrts)
        top = min(v.bounds.top for v in vrts)
        if left >= right or bottom >= top:
            raise ValueError(
                "Footprints do not overlap, so there's nothing to compare. "
                "Change detection needs acquisitions of the same area "
                "(e.g. items from one Umbra task)."
            )

        # Output grid: max_size on the longer side, aspect from the
        # intersection's lon/lat extent. Same lat/lon-stretch quick-look
        # approximation image_overlay uses -- fine at the scene scale,
        # mildly distorted toward the poles.
        w_deg, h_deg = right - left, top - bottom
        if w_deg >= h_deg:
            out_w = max_size
            out_h = max(int(round(max_size * h_deg / w_deg)), 1)
        else:
            out_h = max_size
            out_w = max(int(round(max_size * w_deg / h_deg)), 1)

        # Each read targets the identical geographic window and output shape,
        # so the returned arrays are pixel-aligned across dates.
        bands: list[Any] = []
        for v in vrts:
            window = v.window(left, bottom, right, top)
            # List index + 3-D out_shape, dropping the band axis here. Rasterio's
            # scalar-index path squeezes in place with an ndarray.shape
            # assignment, deprecated in NumPy 2.5.
            bands.append(
                v.read(
                    [1], window=window, out_shape=(1, out_h, out_w), resampling=Resampling.average
                )[0]
            )
    finally:
        for v in vrts:
            v.close()
        for ds in datasets:
            ds.close()
    return bands, (left, bottom, right, top)


def select_change_frames(
    items: Iterable[UmbraItem],
    *,
    frames: int | None = 2,
) -> list[UmbraItem]:
    """Pick acquisitions of a site for a change composite or time-lapse.

    Given the acquisitions of a site (e.g. the result of
    ``catalog.search(area=...)``), choose ``frames`` of them, evenly spaced
    in time from the earliest to the latest. ``frames=2`` or ``3`` feeds the
    RGB :func:`change_composite`; ``frames=None`` returns the *whole* series
    (oldest-first) for an animated time-lapse. ``frames`` is clamped to
    what's available.

    To keep the comparison apples-to-apples, acquisitions are first grouped
    by polarization and the largest single-polarization group is used --
    mixing HH and VV would show the polarization difference as fake "change"
    (and would make a time-lapse flicker between brightness regimes). If
    every acquisition is a different polarization (so no same-polarization
    pair exists), all are used as a fallback; the caller can warn. Items
    without a datetime are dropped (they can't be ordered).

    Raises ``ValueError`` if fewer than two dated acquisitions are available.
    Returns the selection oldest-first.
    """
    if frames not in (2, 3, None):
        raise ValueError(f"frames must be 2, 3, or None, got {frames}.")
    dated = [i for i in items if i.datetime is not None]
    if len(dated) < 2:
        raise ValueError(f"need at least 2 dated acquisitions to compare, got {len(dated)}.")

    groups: dict[tuple[str, ...], list[UmbraItem]] = {}
    for item in dated:
        groups.setdefault(tuple(item.polarizations), []).append(item)
    # Largest single-polarization group, deterministic on ties.
    pool = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))[0][1]
    if len(pool) < 2:
        pool = dated  # no same-pol pair exists; compare across pols instead.

    # Every item in `pool` came from `dated` (datetime is not None); the
    # ``or datetime.min`` fallback is unreachable but keeps the sort key typed
    # as a plain ``datetime`` for the type checker.
    pool = sorted(pool, key=lambda i: i.datetime or datetime.min)
    if frames is None:
        return pool  # whole series, for a time-lapse
    n = min(frames, len(pool))
    # Evenly spaced indices including both endpoints.
    indices = [round(k * (len(pool) - 1) / (n - 1)) for k in range(n)]
    chosen: list[UmbraItem] = []
    seen: set[int] = set()
    for j in indices:
        if j not in seen:
            seen.add(j)
            chosen.append(pool[j])
    return chosen


def change_composite(
    items: Iterable[UmbraItem],
    *,
    asset: str = "GEC",
    max_size: int = 2048,
    percentile: tuple[float, float] = (2.0, 98.0),
    db: bool = False,
):
    """Render a multi-temporal SAR change composite of 2-3 acquisitions.

    SAR's killer app is change detection: the radar backscatter of a fixed
    scene is remarkably stable between passes, so anything that *did* change
    -- a ship that arrived, a field that flooded, a building that went up --
    jumps out against the static background. This function turns 2 or 3
    acquisitions of the same site into a single color image where unchanged
    ground stays gray and change is tinted by *when* it happened.

    Pass the items in **chronological order**. The bands are co-registered
    onto a shared lon/lat grid (so the same pixel is the same place on every
    date), each is percentile-stretched, and they're assigned to color
    channels:

    - **Two dates:** **green** = backscatter that appeared in the later pass
      (new/brighter), **magenta** = backscatter that vanished (gone/dimmer),
      gray/white = unchanged.
    - **Three dates:** a temporal-RGB (earliest=red, middle=green,
      latest=blue); a moving bright target leaves a red→green→blue trail.

    Only the area imaged on *every* pass is colored; pixels missing from any
    acquisition are transparent. ``db`` switches to a decibel stretch (the
    radiometrically-correct SAR view). ``asset`` defaults to ``"GEC"`` (the
    detected amplitude GeoTIFF); ``"CSI"`` also works. Only a downsampled
    overview of each cloud-optimized GeoTIFF is fetched (range requests, no
    full download). Returns a ``PIL.Image``. Requires the ``viz`` extra.
    """
    _require("PIL")
    from PIL import Image  # noqa: PLC0415

    items = list(items)
    if len(items) not in (2, 3):
        raise ValueError(f"change_composite needs 2 or 3 acquisitions, got {len(items)}.")

    bands, _ = _coregister_bands(items, asset, max_size)
    rgba = _compose_change_rgba(bands, percentile=percentile, db=db)
    return Image.fromarray(rgba, mode="RGBA")


def save_change_composite(
    items: Iterable[UmbraItem],
    dest: str | os.PathLike,
    **kwargs,
) -> Path:
    """Render a SAR change composite and write it to ``dest`` as an image.

    The output format follows ``dest``'s extension (``.png``, ``.jpg``,
    ...), per Pillow. See :func:`change_composite` for the rendering
    options and color semantics.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    image = change_composite(items, **kwargs)
    if dest.suffix.lower() in (".jpg", ".jpeg"):
        # JPEG has no alpha channel; flatten transparent (un-imaged) pixels
        # onto black so the save doesn't error.
        image = image.convert("RGB")
    image.save(str(dest))
    return dest


def timescan_composite(
    items: Iterable[UmbraItem],
    *,
    asset: str = "GEC",
    max_size: int = 2048,
    percentile: tuple[float, float] = (2.0, 98.0),
    db: bool = False,
):
    """Summarise a full SAR time series into one temporal-statistics image.

    Umbra revisits a site many times; this collapses that whole stack into a
    single picture of *where the scene was active over time*. The
    acquisitions are co-registered onto a shared lon/lat grid (so each pixel
    is the same ground location on every date), then summarised per pixel:

    - **red** = average backscatter, **green** = peak backscatter, **blue** =
      temporal variability (standard deviation).

    Stable terrain (``std ≈ 0``) renders gray/yellow; anything that came and
    went across the series -- ships through a berth, vehicles in a lot, a
    field flooding -- has high variability and glows **blue/cyan**. This is
    the multi-date complement to :func:`change_composite`, which is limited to
    2-3 dates: here you can throw the entire archive of a site at it.

    Pass at least three acquisitions (order doesn't matter -- the statistics
    are order-independent). ``db`` summarises in the decibel domain;
    ``asset`` defaults to ``"GEC"`` (the detected amplitude GeoTIFF), ``"CSI"``
    also works. Only downsampled overviews are streamed via range requests --
    no full download. Returns a ``PIL.Image``. Requires the ``viz`` extra.
    """
    _require("PIL")
    from PIL import Image  # noqa: PLC0415

    items = list(items)
    if len(items) < 3:
        raise ValueError(
            f"timescan_composite needs at least 3 acquisitions, got {len(items)}; "
            "for two dates use change_composite."
        )

    bands, _ = _coregister_bands(items, asset, max_size)
    rgba = _compose_timescan_rgba(bands, percentile=percentile, db=db)
    return Image.fromarray(rgba, mode="RGBA")


def save_timescan_composite(
    items: Iterable[UmbraItem],
    dest: str | os.PathLike,
    **kwargs,
) -> Path:
    """Render a SAR timescan composite and write it to ``dest`` as an image.

    The output format follows ``dest``'s extension (``.png``, ``.jpg``, ...),
    per Pillow. See :func:`timescan_composite` for the rendering options and
    color semantics.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    image = timescan_composite(items, **kwargs)
    if dest.suffix.lower() in (".jpg", ".jpeg"):
        # JPEG has no alpha channel; flatten transparent (un-imaged) pixels
        # onto black so the save doesn't error.
        image = image.convert("RGB")
    image.save(str(dest))
    return dest


def _label_font(px: int):
    """Best-available bitmap font at roughly ``px`` height.

    Pillow's built-in default font takes a ``size`` only on 10.1+; fall back
    to the fixed-size default on older Pillow so we never need a font file.
    """
    from PIL import ImageFont  # noqa: PLC0415

    try:
        return ImageFont.load_default(size=px)
    except TypeError:  # pragma: no cover - very old Pillow
        return ImageFont.load_default()


def _stamp_label(img: Any, text: str) -> None:
    """Draw ``text`` in the top-left of ``img`` over a dark plate for contrast."""
    from PIL import ImageDraw  # noqa: PLC0415

    draw = ImageDraw.Draw(img)
    font = _label_font(max(14, img.height // 36))
    x, y = 6, 6
    box = draw.textbbox((x, y), text, font=font)
    draw.rectangle([box[0] - 3, box[1] - 2, box[2] + 3, box[3] + 2], fill=(0, 0, 0))
    draw.text((x, y), text, fill=(255, 255, 255), font=font)


def change_animation(
    items: Iterable[UmbraItem],
    *,
    asset: str = "GEC",
    max_size: int = 1024,
    percentile: tuple[float, float] = (2.0, 98.0),
    db: bool = False,
    colormap: str | None = None,
    label: bool = True,
) -> list[Any]:
    """Render a co-registered SAR time-lapse: one frame per acquisition.

    Where :func:`change_composite` collapses 2-3 dates into a single colored
    image, this tracks change across *any* number of acquisitions by turning
    the series into an animation. All frames are co-registered onto the
    shared footprint intersection (see :func:`_coregister_bands`), so the
    site stays put and only the scene content moves between frames -- the
    point of a time-lapse. Each frame is a SAR quicklook (same percentile /
    ``db`` / ``colormap`` controls as :func:`quicklook`); with ``label`` the
    acquisition date is stamped in the corner so time is legible.

    Items are ordered oldest-first by acquisition time. Returns a list of
    ``PIL.Image`` frames (RGB); :func:`save_change_animation` writes them to
    an animated GIF. Needs at least two acquisitions. Requires the ``viz``
    extra.
    """
    _require("PIL")
    from PIL import Image  # noqa: PLC0415

    # Oldest-first so the animation plays forward in time; undated items
    # (which can't be placed on the timeline) sort to the end.
    items = sorted(items, key=lambda i: (i.datetime is None, i.datetime or datetime.min))
    if len(items) < 2:
        raise ValueError(f"animation needs at least 2 acquisitions, got {len(items)}.")

    bands, _ = _coregister_bands(items, asset, max_size)
    frames: list[Any] = []
    for item, band in zip(items, bands, strict=True):
        rgba = _stretch_to_rgba(band, percentile=percentile, db=db, colormap=colormap)
        # Flatten onto black: GIF handles per-frame transparency poorly, and
        # invalid pixels are already dark after the stretch.
        frame = Image.fromarray(rgba, mode="RGBA").convert("RGB")
        if label:
            dt = item.datetime
            _stamp_label(frame, dt.strftime("%Y-%m-%d") if dt else item.id[:12])
        frames.append(frame)
    return frames


def save_change_animation(
    items: Iterable[UmbraItem],
    dest: str | os.PathLike,
    *,
    fps: float = 2.0,
    loop: int = 0,
    **kwargs,
) -> Path:
    """Render a SAR time-lapse and write it to ``dest`` as an animated GIF.

    ``fps`` sets the playback speed (frames per second); ``loop=0`` (the
    default) loops forever, any other value plays that many times. See
    :func:`change_animation` for the per-frame rendering options.
    """
    from PIL import Image  # noqa: PLC0415

    frames = change_animation(items, **kwargs)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    duration_ms = max(int(round(1000.0 / fps)), 1)
    # Quantize to a palette first: Pillow's multi-frame GIF writer silently
    # collapses RGB ``append_images`` to a single frame, but writes every
    # palette-mode frame.
    paletted = [f.convert("P", palette=Image.ADAPTIVE, colors=256) for f in frames]
    paletted[0].save(
        str(dest),
        save_all=True,
        append_images=paletted[1:],
        duration=duration_ms,
        loop=loop,
        disposal=2,
    )
    return dest
