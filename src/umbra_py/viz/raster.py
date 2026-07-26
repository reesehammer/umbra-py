"""Streaming SAR raster reads, stretches and single-scene renders.

The pixel half of ``viz`` (requires the ``viz`` extra plus rasterio): read a
downsampled overview of an acquisition's cloud-optimized GeoTIFF over HTTP
range requests -- never a full download -- and turn it into something you can
look at. That covers the amplitude stretch (linear or the radiometrically
correct decibel scale, with optional matplotlib pseudo-color), the RGBA
overlays maps composite onto a basemap, standalone ``quicklook`` images, and
the small PNG thumbnails the gallery and the baked index embed.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..exceptions import AssetNotFoundError
from ..models import UmbraItem
from ._deps import _require


def _normalize_band(
    data: Any,
    *,
    percentile: tuple[float, float] = (2.0, 98.0),
    db: bool = False,
    cuts: tuple[float, float] | None = None,
) -> tuple[Any, Any]:
    """Percentile-stretch a 2D SAR amplitude band to ``[0, 1]`` + a mask.

    Returns ``(norm, invalid)``: ``norm`` is a float64 array in ``[0, 1]``
    (invalid pixels clamped to 0) and ``invalid`` is a boolean mask of the
    pixels that were NaN / nodata / non-positive.

    SAR data has enormous dynamic range; a straight 0-255 scaling looks
    almost black. We compute the low/high cut on positive, finite values
    only, clip the rest to that range, and rescale. When ``db`` is True the
    amplitudes are first converted to decibels (``20*log10(amplitude)``)
    before the percentile stretch -- the radiometrically-meaningful view:
    the log compresses the huge dynamic range so terrain texture and urban
    structure that a linear amplitude stretch crushes into near-black
    become visible.

    ``cuts`` supplies an explicit ``(lo, hi)`` stretch range (in the chosen
    domain -- amplitude, or dB when ``db`` is True) instead of computing the
    percentiles from this band. The tile viewer uses it to apply *one* global
    stretch -- derived once from a whole-scene overview via
    :func:`_amplitude_cuts` -- to every tile, so neighbouring tiles share
    contrast and don't seam.

    Shared by the grayscale/pseudo-color quicklook path
    (:func:`_stretch_to_rgba`) and the multi-temporal change composite
    (:func:`_compose_change_rgba`).
    """
    np = _require("numpy")
    # float64 so the log and the rescale don't lose precision on integer
    # amplitude rasters.
    arr = np.asarray(data, dtype="float64")
    invalid = ~np.isfinite(arr) | (arr <= 0)
    if invalid.all():
        # With an explicit global stretch a fully-invalid tile is normal (a
        # tile off the edge of the scene), not an error -- return an all-zero
        # band that callers render fully transparent via the mask.
        if cuts is not None:
            return np.zeros(arr.shape, dtype="float64"), invalid
        raise ValueError("Image has no valid pixels to stretch.")
    if db:
        # amplitude -> decibels; only defined for the positive pixels we
        # already flagged as valid. Invalid pixels become NaN and are
        # masked out of the percentile below.
        with np.errstate(divide="ignore", invalid="ignore"):
            arr = np.where(invalid, np.nan, 20.0 * np.log10(arr))
    if cuts is None:
        valid = arr[~invalid]
        lo, hi = np.percentile(valid, percentile)
    else:
        lo, hi = cuts
    if hi <= lo:
        hi = lo + 1.0
    # Replace invalid pixels with lo before scaling so NaN values don't
    # trigger numpy warnings; they're set fully transparent by callers.
    safe = np.where(invalid, lo, arr)
    norm = np.clip((safe - lo) / (hi - lo), 0.0, 1.0)
    return norm, invalid


def _amplitude_cuts(
    data: Any,
    *,
    percentile: tuple[float, float] = (2.0, 98.0),
    db: bool = False,
) -> tuple[float, float]:
    """Percentile stretch cuts ``(lo, hi)`` for a SAR amplitude band.

    Returns the low/high bounds of the contrast stretch in the chosen domain
    (amplitude, or dB when ``db`` is True), computed over the finite, positive
    pixels only. Feed the result back as the ``cuts`` argument of
    :func:`_normalize_band` / :func:`_stretch_to_rgba` to apply the *same*
    stretch to many bands -- the tile viewer computes it once from a
    whole-scene overview so every tile shares contrast.
    """
    np = _require("numpy")
    arr = np.asarray(data, dtype="float64")
    invalid = ~np.isfinite(arr) | (arr <= 0)
    if invalid.all():
        raise ValueError("Image has no valid pixels to stretch.")
    if db:
        with np.errstate(divide="ignore", invalid="ignore"):
            arr = np.where(invalid, np.nan, 20.0 * np.log10(arr))
    lo, hi = np.percentile(arr[~invalid], percentile)
    if hi <= lo:
        hi = lo + 1.0
    return float(lo), float(hi)


def _stretch_to_rgba(
    data: Any,
    *,
    percentile: tuple[float, float] = (2.0, 98.0),
    db: bool = False,
    colormap: str | None = None,
    cuts: tuple[float, float] | None = None,
) -> Any:
    """Convert a 2D array of SAR amplitudes to an RGBA uint8 image.

    Pixels that were invalid (NaN / nodata / non-positive) become fully
    transparent so the basemap shows through scene edges. See
    :func:`_normalize_band` for the percentile-stretch and optional dB
    rationale.

    When ``colormap`` names a matplotlib colormap (e.g. ``"viridis"``,
    ``"magma"``) the stretched values are mapped through it for a
    pseudo-colored quicklook instead of grayscale; this needs matplotlib
    (already in the ``viz`` extra).

    ``cuts`` supplies an explicit ``(lo, hi)`` stretch range (see
    :func:`_normalize_band`) so the tile viewer can apply one global stretch
    across every tile.
    """
    np = _require("numpy")
    norm, invalid = _normalize_band(data, percentile=percentile, db=db, cuts=cuts)
    alpha = np.where(invalid, 0, 255).astype("uint8")

    if colormap:
        rgb = _apply_colormap(norm, colormap)
    else:
        gray = (norm * 255.0).astype("uint8")
        rgb = np.stack([gray, gray, gray], axis=-1)
    return np.dstack([rgb, alpha])


def _apply_colormap(norm: Any, name: str) -> Any:
    """Map a [0,1]-normalised 2D array through a matplotlib colormap.

    Returns an ``(H, W, 3)`` uint8 RGB array. Raised separately from
    ``_stretch_to_rgba`` so the numpy-only grayscale path doesn't import
    matplotlib.
    """
    _require("matplotlib")
    from matplotlib import colormaps  # noqa: PLC0415

    cmap = colormaps[name]
    rgb = cmap(norm)[..., :3]  # drop the colormap's own alpha channel
    return (rgb * 255.0).astype("uint8")


def _read_sar_band(
    item: UmbraItem,
    asset: str,
    max_size: int,
    *,
    reproject_to_4326: bool = False,
) -> tuple[Any, Any]:
    """Read a downsampled band 1 of an item's SAR GeoTIFF via range requests.

    Returns ``(data, bounds)`` where ``data`` is a 2D numpy array and
    ``bounds`` is the dataset's geographic bounds. Only the bytes for the
    requested resolution are fetched (the asset is a cloud-optimized
    GeoTIFF read through GDAL's ``/vsicurl/`` driver). When
    ``reproject_to_4326`` is True the raster is warped to lon/lat so it
    can be placed on a web map; for a standalone quicklook the native
    projection is read as-is (no warp distortion).
    """
    rasterio = _require("rasterio")
    _require("numpy")
    from rasterio.enums import Resampling  # noqa: PLC0415
    from rasterio.vrt import WarpedVRT  # noqa: PLC0415

    url = item.asset_href(asset)
    if not url:
        raise AssetNotFoundError(
            f"Item {item.id!r} has no resolvable URL for asset {asset!r} "
            "(asset href is empty and no umbra:task_id available to derive one)."
        )
    with rasterio.open(f"/vsicurl/{url}") as src:
        if reproject_to_4326:
            epsg = src.crs.to_epsg() if src.crs else None
            wrap = WarpedVRT(src, crs="EPSG:4326") if epsg != 4326 else None
        else:
            wrap = None
        ds = wrap if wrap is not None else src
        try:
            scale = max(max(ds.width, ds.height) / max_size, 1.0)
            out_w = max(int(ds.width / scale), 1)
            out_h = max(int(ds.height / scale), 1)
            # List index + 3-D out_shape, dropping the band axis here. Rasterio's
            # scalar-index + 2-D out_shape path squeezes in place with an
            # ndarray.shape assignment, deprecated in NumPy 2.5.
            data = ds.read([1], out_shape=(1, out_h, out_w), resampling=Resampling.average)[0]
            bounds = ds.bounds
        finally:
            if wrap is not None:
                wrap.close()
    return data, bounds


def _rgba_overlay(
    rgba: Any,
    bounds: tuple[float, float, float, float],
    *,
    opacity: float = 1.0,
    pane: str | None = None,
):
    """Encode an RGBA array as a base64-PNG Folium ``ImageOverlay``.

    ``bounds`` is ``(left, bottom, right, top)`` in EPSG:4326. Embedding the
    PNG inline keeps the resulting map a single self-contained HTML file.
    ``pane`` places the overlay in a named Leaflet pane (used by the swipe
    map so each layer can be clipped independently).
    """
    folium = _require("folium")
    _require("PIL")

    import base64  # noqa: PLC0415
    import io  # noqa: PLC0415

    from PIL import Image  # noqa: PLC0415

    left, bottom, right, top = bounds
    buf = io.BytesIO()
    Image.fromarray(rgba, mode="RGBA").save(buf, format="PNG")
    data_uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

    extra = {"pane": pane} if pane is not None else {}
    return folium.raster_layers.ImageOverlay(
        image=data_uri,
        bounds=[[bottom, left], [top, right]],
        opacity=opacity,
        **extra,
    )


def image_overlay(
    item: UmbraItem,
    *,
    asset: str = "GEC",
    max_size: int = 1024,
    percentile: tuple[float, float] = (2.0, 98.0),
    opacity: float = 1.0,
    db: bool = False,
):
    """Build a Folium ``ImageOverlay`` of an item's SAR image.

    Reads a downsampled preview of the cloud-optimized GeoTIFF via HTTP
    range requests (only the bytes for the requested resolution are
    fetched), applies a percentile contrast stretch for SAR amplitude,
    reprojects to lat/lon if necessary, and embeds the result as a base64
    PNG so the resulting map stays a single self-contained HTML file.

    ``db`` switches to a decibel (log-amplitude) stretch -- the
    radiometrically-correct SAR view that reveals texture the default
    linear stretch crushes toward black.

    Requires the ``viz`` extra (which pulls in rasterio + numpy; Pillow
    comes transitively via matplotlib).
    """
    data, bounds = _read_sar_band(item, asset, max_size, reproject_to_4326=True)
    rgba = _stretch_to_rgba(data, percentile=percentile, db=db)
    return _rgba_overlay(
        rgba, (bounds.left, bounds.bottom, bounds.right, bounds.top), opacity=opacity
    )


def quicklook(
    item: UmbraItem,
    *,
    asset: str = "GEC",
    max_size: int = 2048,
    percentile: tuple[float, float] = (2.0, 98.0),
    db: bool = False,
    colormap: str | None = None,
):
    """Render a standalone SAR quicklook image of an item.

    Reads a downsampled preview of the cloud-optimized GeoTIFF via HTTP
    range requests (only the bytes for the requested resolution are
    fetched — no multi-gigabyte download), applies a percentile contrast
    stretch tuned for SAR's dynamic range, and returns a ``PIL.Image`` you
    can ``.save("scene.png")`` or display in a notebook.

    This is the lowest-friction way to *see* an Umbra acquisition: no map,
    no GIS, no full download. Unlike :func:`image_overlay`, the raster is
    read in its native (already geocoded, north-up) projection rather than
    warped to lon/lat — a faithful look at the pixels rather than a
    map-placeable overlay.

    ``db`` switches to a decibel (log-amplitude) stretch, the
    radiometrically-correct way to view SAR — it reveals terrain texture
    and urban structure that the default linear stretch crushes toward
    black. ``colormap`` names a matplotlib colormap (``"viridis"``,
    ``"magma"``, ...) for a pseudo-colored quicklook instead of grayscale.

    ``asset`` defaults to ``"GEC"``, the detected single-band image; that
    and ``"CSI"`` are the sensible targets (the complex SICD/CPHD products
    aren't amplitude rasters). Requires the ``viz`` extra.
    """
    _require("PIL")
    from PIL import Image  # noqa: PLC0415

    data, _ = _read_sar_band(item, asset, max_size, reproject_to_4326=False)
    rgba = _stretch_to_rgba(data, percentile=percentile, db=db, colormap=colormap)
    return Image.fromarray(rgba, mode="RGBA")


def save_quicklook(
    item: UmbraItem,
    dest: str | os.PathLike,
    **kwargs,
) -> Path:
    """Render an item's SAR quicklook and write it to ``dest`` as an image.

    The output format follows ``dest``'s extension (``.png``, ``.jpg``,
    ...), per Pillow. See :func:`quicklook` for the rendering options.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    image = quicklook(item, **kwargs)
    if dest.suffix.lower() in (".jpg", ".jpeg"):
        # JPEG has no alpha channel; flatten transparent (invalid) pixels
        # onto black so the save doesn't error.
        image = image.convert("RGB")
    image.save(str(dest))
    return dest


def _thumbnail_png(
    item: UmbraItem,
    *,
    asset: str = "GEC",
    max_size: int = 256,
    db: bool = True,
    percentile: tuple[float, float] = (2.0, 98.0),
    colormap: str | None = None,
) -> bytes:
    """Render a small SAR quicklook and return the raw PNG bytes.

    The byte-level primitive under :func:`_thumbnail_data_uri` (which base64s
    these bytes into a data URI) and the default renderer for
    :meth:`umbra_py.index.CatalogIndex.bake_thumbnails`, which stores the bytes
    directly in the index. ``db=True`` gives the radiometrically-correct decibel
    stretch, which reads better at thumbnail size than the linear default. Only
    the bytes for ``max_size`` are streamed from the cloud-optimized GeoTIFF.
    Requires the ``viz`` extra.
    """
    import io  # noqa: PLC0415

    image = quicklook(
        item, asset=asset, max_size=max_size, db=db, percentile=percentile, colormap=colormap
    )
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def _png_data_uri(png: bytes) -> str:
    """Wrap raw PNG bytes as a base64 ``data:`` URI for inline HTML embedding.

    The byte-level primitive shared by :func:`_thumbnail_data_uri` (which renders
    the bytes from the cloud-optimized GeoTIFF) and the gallery's baked-thumbnail
    path (which reads them straight from the index's ``thumbnail`` column via
    :meth:`umbra_py.index.CatalogIndex.get_thumbnail`), so a pre-baked preview and
    a freshly-streamed one reach the page in exactly the same shape. Pure standard
    library -- no ``viz`` extra -- so a fully-baked gallery needs no ``rasterio``.
    """
    import base64  # noqa: PLC0415

    return "data:image/png;base64," + base64.b64encode(png).decode()


def _thumbnail_data_uri(
    item: UmbraItem,
    *,
    asset: str = "GEC",
    max_size: int = 256,
    db: bool = True,
    percentile: tuple[float, float] = (2.0, 98.0),
    colormap: str | None = None,
) -> str:
    """Render a small SAR quicklook and return it as a base64 PNG data URI.

    Used by :class:`umbra_py.ItemCollection` and :func:`gallery` to embed
    thumbnails inline. ``db=True`` (the default here) gives the
    radiometrically-correct decibel stretch, which reads better at thumbnail
    size than the linear default. Only the bytes for ``max_size`` are streamed
    from the cloud-optimized GeoTIFF. Requires the ``viz`` extra.
    """
    png = _thumbnail_png(
        item, asset=asset, max_size=max_size, db=db, percentile=percentile, colormap=colormap
    )
    return _png_data_uri(png)
