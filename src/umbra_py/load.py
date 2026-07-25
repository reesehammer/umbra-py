"""Analysis-ready loading of Umbra SAR imagery into ``xarray`` (the *load* step).

The project tagline is "discover, **load**, download, and analyze". Discovery,
download and pretty-picture visualization already exist; this module fills the
missing verb: turning a geocoded Umbra GeoTIFF into a georeferenced
:class:`xarray.DataArray` so the data drops straight into the scientific Python
stack (``xarray``/``dask``/``matplotlib``/``scikit-image``/``rioxarray`` ...).

Why this matters for adoption: every Sentinel-1 / Landsat workflow starts by
loading a scene into a labelled, georeferenced array. Until now an Umbra user
had to hand-roll ``rasterio`` + windowing + coordinate construction to get
there. :func:`to_xarray` makes it one call -- and because the source is a
cloud-optimized GeoTIFF read through GDAL's ``/vsicurl/`` driver, only the bytes
for the requested window and resolution are streamed over HTTP range requests
(no multi-gigabyte download).

:func:`to_stack` is the *time series* half of the same verb. Elsewhere in the
STAC ecosystem that step is ``stackstac`` / ``odc-stac``: search returns N
scenes, and one call co-registers them onto a shared grid and hands back a
``(time, y, x)`` datacube. Neither works against Umbra, because both assume a
STAC *API* and a common projected grid, and Umbra's passes over one site land
in whatever UTM zone (and at whatever extent) each acquisition happened to use.
:func:`to_stack` does the warp-to-a-common-grid step itself, so a search result
becomes a labelled cube you can take ``.mean("time")``, ``.diff("time")`` or
``.std("time")`` of -- the primitive every multi-date SAR analysis starts from,
and the one thing the library previously only produced as a *picture*
(``umbra change`` / ``umbra timescan``) rather than as numbers.

Install with: ``pip install "umbra-py[load]"``
"""

from __future__ import annotations

import os
from datetime import timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .constants import ATTRIBUTION, DATA_LICENSE
from .exceptions import AssetNotFoundError, MissingDependencyError
from .models import UmbraItem

if TYPE_CHECKING:  # pragma: no cover - import only for type checkers
    from collections.abc import Iterable

    import xarray as xr

#: Geographic bbox: ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326.
BBox = tuple[float, float, float, float]

#: How :func:`to_stack` picks the datacube's footprint from the scenes it stacks.
#: ``"intersection"`` keeps only the ground every acquisition covers (every cell
#: has a full time series -- what change detection wants); ``"union"`` keeps all
#: ground any acquisition covers, filling each slice outside its own footprint
#: with ``NaN``.
STACK_EXTENTS = ("intersection", "union")


def _require(module: str):
    try:
        return __import__(module)
    except ImportError as exc:  # pragma: no cover - exercised only without extra
        raise MissingDependencyError(
            f"'{module}' is required for analysis-ready loading. "
            'Install the extra with: pip install "umbra-py[load]"',
            hint='pip install "umbra-py[load]"',
        ) from exc


def _open_path(url: str) -> str:
    """Path to hand ``rasterio.open``: stream remote COGs, open local files directly.

    Umbra's public assets are ``https`` cloud-optimized GeoTIFFs, which GDAL
    reads with range requests via the ``/vsicurl/`` driver. A plain local path
    (used in tests, or for an already-downloaded file) is opened as-is.
    """
    if url.startswith(("http://", "https://")):
        return f"/vsicurl/{url}"
    return url


def to_xarray(
    item: UmbraItem,
    *,
    asset: str = "GEC",
    bbox: BBox | None = None,
    max_size: int | None = None,
    db: bool = False,
    masked: bool = True,
) -> xr.DataArray:
    """Load an Umbra SAR image as a georeferenced :class:`xarray.DataArray`.

    Reads band 1 of the item's geocoded GeoTIFF (the ``GEC`` cloud-optimized
    GeoTIFF by default) and returns it as a 2D ``DataArray`` with ``y`` / ``x``
    coordinate axes in the raster's native CRS, ready for the scientific Python
    stack. Only the requested window and resolution are streamed via HTTP range
    requests -- no full download.

    Parameters
    ----------
    item:
        The acquisition to load.
    asset:
        Which product to read. Defaults to ``"GEC"``; ``"CSI"`` (the
        single-band color-sub-aperture GeoTIFF) also works. The complex
        ``SICD`` / ``CPHD`` products are not amplitude rasters and aren't
        supported here.
    bbox:
        Optional ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326. When
        given, only that geographic window is read (reprojected to the
        raster's CRS first). Useful for pulling a small area out of a large
        scene without reading the whole thing.
    max_size:
        Optional cap on the longest output side in pixels. The raster is
        decimated to fit (GDAL pulls the matching cloud-optimized GeoTIFF
        overview, so this is cheap). ``None`` reads full resolution -- which
        for a multi-GB scene can be a lot of data; pair large reads with a
        ``bbox`` or a ``max_size``.
    db:
        Convert linear amplitude to decibels (``20*log10(amplitude)``), the
        radiometrically-meaningful SAR scale. Implies ``masked=True`` for the
        non-positive pixels ``log10`` can't represent.
    masked:
        Replace nodata and non-positive amplitudes with ``NaN`` so they don't
        contaminate statistics. The array is always returned as ``float32``.

    Returns
    -------
    xarray.DataArray
        Dimensions ``("y", "x")`` with descending ``y`` (north-up) and
        ascending ``x`` cell-center coordinates. ``attrs`` carry the CRS
        (``crs``, a WKT/PROJ string), the affine ``transform`` (a 6-tuple),
        the geographic ``bounds``, ``units``, and acquisition metadata
        (``item_id``, ``datetime``, ``platform``, ``product_type``), plus the
        Umbra ``license`` and ``attribution`` you must carry with derived
        products. The CRS string round-trips through ``rasterio.crs.CRS`` /
        ``pyproj`` and ``rioxarray`` (``da.rio.write_crs(da.attrs["crs"])``).
    """
    rasterio = _require("rasterio")
    np = _require("numpy")
    xr = _require("xarray")
    from affine import Affine  # noqa: PLC0415
    from rasterio.enums import Resampling  # noqa: PLC0415
    from rasterio.windows import Window, from_bounds  # noqa: PLC0415

    url = item.asset_href(asset)
    if not url:
        raise AssetNotFoundError(
            f"Item {item.id!r} has no resolvable URL for asset {asset!r} "
            "(asset href is empty and no umbra:task_id available to derive one)."
        )

    with rasterio.open(_open_path(url)) as src:
        # Restrict to the requested geographic window (in the source CRS).
        if bbox is not None:
            from rasterio.errors import WindowError  # noqa: PLC0415
            from rasterio.warp import transform_bounds  # noqa: PLC0415

            left, bottom, right, top = transform_bounds("EPSG:4326", src.crs, *bbox)
            requested = from_bounds(left, bottom, right, top, transform=src.transform)
            try:
                window = requested.intersection(Window(0, 0, src.width, src.height))
            except WindowError:
                window = None
            if window is None or window.width < 1 or window.height < 1:
                raise ValueError(
                    f"bbox {bbox} does not overlap item {item.id!r} "
                    f"(bounds {tuple(src.bounds)} in {src.crs})."
                )
        else:
            window = Window(0, 0, src.width, src.height)

        # Decimate to fit max_size (GDAL serves the matching COG overview).
        scale = 1.0
        if max_size is not None:
            scale = max(max(window.width, window.height) / max_size, 1.0)
        out_w = max(round(window.width / scale), 1)
        out_h = max(round(window.height / scale), 1)

        # Read band 1 via a list index into a 3-D out_shape and drop the band
        # axis ourselves. Rasterio's scalar-index + 2-D out_shape path squeezes
        # the result in place with an ndarray.shape assignment, which NumPy 2.5
        # deprecates; a list index returns a 3-D array with no in-place reshape.
        data = src.read(
            [1],
            window=window,
            out_shape=(1, out_h, out_w),
            resampling=Resampling.average,
        )[0].astype("float32")

        nodata = src.nodata
        crs = src.crs
        # Output transform: window origin scaled to the decimated grid. GEC is
        # north-up (no rotation), so a/e fully describe pixel size and y runs
        # top-to-bottom (negative e).
        win_transform = src.window_transform(window)
        transform = win_transform * Affine.scale(window.width / out_w, window.height / out_h)

    invalid = ~np.isfinite(data)
    if nodata is not None:
        invalid |= data == nodata
    if db:
        masked = True
        invalid |= data <= 0
        with np.errstate(divide="ignore", invalid="ignore"):
            data = np.where(invalid, np.nan, 20.0 * np.log10(data)).astype("float32")
    elif masked:
        invalid |= data <= 0
        data = np.where(invalid, np.nan, data).astype("float32")

    # Cell-center coordinates from the affine transform (b == d == 0, north-up).
    xs = transform.c + transform.a * (np.arange(out_w) + 0.5)
    ys = transform.f + transform.e * (np.arange(out_h) + 0.5)
    left, top = transform.c, transform.f
    right, bottom = transform * (out_w, out_h)

    dt = item.datetime
    attrs: dict[str, Any] = {
        "crs": crs.to_string() if crs else None,
        "transform": tuple(transform)[:6],
        "bounds": (left, bottom, right, top),
        "units": "dB" if db else "amplitude",
        "long_name": "SAR backscatter (dB)" if db else "SAR amplitude",
        "item_id": item.id,
        "datetime": dt.isoformat() if dt else None,
        "platform": item.platform,
        "product_type": asset,
        "license": DATA_LICENSE,
        "attribution": ATTRIBUTION,
    }

    return xr.DataArray(
        data,
        dims=("y", "x"),
        coords={"y": ys, "x": xs},
        name="backscatter_db" if db else "amplitude",
        attrs={k: v for k, v in attrs.items() if v is not None},
    )


def to_geotiff(
    item: UmbraItem,
    dest: str | os.PathLike,
    *,
    asset: str = "GEC",
    bbox: BBox | None = None,
    max_size: int | None = None,
    db: bool = False,
) -> Path:
    """Load an Umbra SAR image and write it to ``dest`` as a GeoTIFF.

    A file-producing companion to :func:`to_xarray` for users who want a
    clipped / decimated raster on disk (for QGIS, GDAL, or any GIS) rather
    than an in-memory array. Same windowing and resolution options: ``bbox``
    clips to a lon/lat area, ``max_size`` decimates via the cloud-optimized
    GeoTIFF overviews, ``db`` writes the decibel scale. Only the requested
    window/resolution is streamed (no full download).

    The output is a single-band ``float32`` GeoTIFF in the source raster's
    native CRS, with nodata / non-positive pixels written as ``NaN``
    (``nodata=NaN``) so masking survives the round-trip. Deflate-compressed
    and tiled.
    """
    rasterio = _require("rasterio")
    _require("numpy")
    from affine import Affine  # noqa: PLC0415

    da = to_xarray(item, asset=asset, bbox=bbox, max_size=max_size, db=db, masked=True)
    data = da.values

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    profile = {
        "driver": "GTiff",
        "height": data.shape[0],
        "width": data.shape[1],
        "count": 1,
        "dtype": "float32",
        "crs": da.attrs.get("crs"),
        "transform": Affine(*da.attrs["transform"]),
        "nodata": float("nan"),
        "compress": "deflate",
        "tiled": True,
    }
    with rasterio.open(dest, "w", **profile) as dst:
        dst.write(data, 1)
        dst.update_tags(
            item_id=item.id,
            units=da.attrs["units"],
            license=DATA_LICENSE,
            attribution=ATTRIBUTION,
        )
    return dest


def _stack_items(items: Iterable[UmbraItem]) -> list[UmbraItem]:
    """Order the stack's acquisitions oldest-first, rejecting undated ones.

    The time axis *is* the point of a datacube, so an acquisition with no
    ``datetime`` can't take a position on it. Fail by name rather than dropping
    it silently or parking it at ``NaT``.
    """
    ordered = list(items)
    if not ordered:
        raise ValueError("to_stack() needs at least one acquisition.")
    undated = [i.id for i in ordered if i.datetime is None]
    if undated:
        raise ValueError(
            "Cannot stack acquisitions with no datetime (the time axis needs one): "
            + ", ".join(undated)
        )
    return sorted(ordered, key=lambda i: i.datetime)  # type: ignore[arg-type,return-value]


def _stack_bounds(
    vrt_bounds: list[Any], extent: str, bbox: BBox | None
) -> tuple[float, float, float, float]:
    """Geographic window the datacube covers, in EPSG:4326.

    ``extent`` picks intersection (every cell has a full series) or union (no
    ground is dropped); ``bbox``, when given, clips whichever was chosen.
    """
    if extent not in STACK_EXTENTS:
        raise ValueError(f"extent must be one of {STACK_EXTENTS}, got {extent!r}.")
    if extent == "intersection":
        left = max(b.left for b in vrt_bounds)
        bottom = max(b.bottom for b in vrt_bounds)
        right = min(b.right for b in vrt_bounds)
        top = min(b.top for b in vrt_bounds)
        if left >= right or bottom >= top:
            raise ValueError(
                "Footprints do not all overlap, so the intersection is empty. "
                "Stack acquisitions of the same site (e.g. items from one Umbra "
                'task), or pass extent="union" to keep every scene\'s own ground '
                "and fill the rest with NaN."
            )
    else:
        left = min(b.left for b in vrt_bounds)
        bottom = min(b.bottom for b in vrt_bounds)
        right = max(b.right for b in vrt_bounds)
        top = max(b.top for b in vrt_bounds)

    if bbox is not None:
        left, bottom = max(left, bbox[0]), max(bottom, bbox[1])
        right, top = min(right, bbox[2]), min(top, bbox[3])
        if left >= right or bottom >= top:
            raise ValueError(f"bbox {bbox} does not overlap the stacked acquisitions.")
    return left, bottom, right, top


def _mask_slice(np: Any, data: Any, nodata: float | None, *, db: bool) -> Any:
    """Nodata / non-positive pixels to ``NaN``, optionally on the decibel scale.

    A stack is always masked: cross-date statistics (``mean``/``std``/``diff``)
    would otherwise be poisoned by fill values, and with ``extent="union"`` the
    ground a slice doesn't cover has to read as "no observation", not as zero.
    """
    invalid = ~np.isfinite(data)
    if nodata is not None:
        invalid |= data == nodata
    invalid |= data <= 0
    if db:
        with np.errstate(divide="ignore", invalid="ignore"):
            return np.where(invalid, np.nan, 20.0 * np.log10(data)).astype("float32")
    return np.where(invalid, np.nan, data).astype("float32")


def to_stack(
    items: Iterable[UmbraItem],
    *,
    asset: str = "GEC",
    bbox: BBox | None = None,
    max_size: int = 1024,
    db: bool = False,
    extent: str = "intersection",
) -> xr.DataArray:
    """Co-register several acquisitions into one ``(time, y, x)`` datacube.

    The multi-date companion to :func:`to_xarray`, and the missing primitive
    between *search* and *analysis*: hand it a search result and get back a
    labelled :class:`xarray.DataArray` whose slices are pixel-aligned, so
    ``cube.mean("time")``, ``cube.std("time")`` and ``cube.diff("time")`` are
    honest per-ground-cell statistics rather than per-scene ones.

    Alignment is real work, not a reshape: Umbra's passes over a site are
    delivered in whatever UTM zone each acquisition used and at whatever extent
    it happened to cover, so every scene is warped to a shared **EPSG:4326**
    (lon/lat) grid derived from the requested ``extent``. Only decimated
    overviews are streamed via HTTP range requests -- no full download.

    Parameters
    ----------
    items:
        The acquisitions to stack. Order doesn't matter (the result is sorted
        oldest-first); each must carry a ``datetime``.
    asset:
        Which product to read, as in :func:`to_xarray`. Stack **one**
        polarization: mixing VV and VH puts a polarization difference on the
        time axis where you'll read it as change. ``UmbraItem.polarizations``
        and ``search(polarizations=...)`` are how you keep them apart.
    bbox:
        Optional ``(min_lon, min_lat, max_lon, max_lat)`` in EPSG:4326 clipping
        the cube to a sub-area of whatever ``extent`` selected.
    max_size:
        Longest side of the output grid in pixels. The grid is shared by every
        slice, so this caps the whole cube (bytes fetched grow ~quadratically).
    db:
        Return the decibel scale (``20*log10(amplitude)``) instead of linear
        amplitude -- the radiometrically meaningful scale for differencing,
        where a ratio of backscatter becomes a subtraction.
    extent:
        One of :data:`STACK_EXTENTS`. ``"intersection"`` (default) keeps only
        the ground *every* acquisition covers, so no cell has a gap; it raises
        if the footprints don't all overlap. ``"union"`` keeps all ground *any*
        acquisition covers and fills each slice outside its own footprint with
        ``NaN``.

    Returns
    -------
    xarray.DataArray
        Dimensions ``("time", "y", "x")``: ascending ``time``, descending ``y``
        (north-up) and ascending ``x`` cell-center coordinates in degrees, plus
        an ``item_id`` coordinate along ``time`` so every slice keeps its
        provenance. Nodata and non-positive pixels are ``NaN`` and the dtype is
        always ``float32``. ``attrs`` mirror :func:`to_xarray`'s (``crs``,
        ``transform``, ``bounds``, ``units``, ``license``, ``attribution``).

    Notes
    -----
    The lon/lat grid stretches with latitude (cells are not equal-area), the
    same quick-look approximation ``umbra change`` / ``umbra timescan`` make.
    It is fine at scene scale and for comparing a cell to *itself* across dates,
    which is what a time series does; reproject the result (e.g. with
    ``rioxarray``) before measuring areas or distances.
    """
    rasterio = _require("rasterio")
    np = _require("numpy")
    xr = _require("xarray")
    from affine import Affine  # noqa: PLC0415
    from rasterio.enums import Resampling  # noqa: PLC0415
    from rasterio.vrt import WarpedVRT  # noqa: PLC0415

    ordered = _stack_items(items)

    datasets: list[Any] = []
    vrts: list[Any] = []
    try:
        for item in ordered:
            url = item.asset_href(asset)
            if not url:
                raise AssetNotFoundError(
                    f"Item {item.id!r} has no resolvable URL for asset {asset!r}."
                )
            ds = rasterio.open(_open_path(url))
            datasets.append(ds)
            # Full-resolution warp to lon/lat: cheap to construct, and nothing
            # is read until the *decimated* windowed reads below, which let
            # GDAL serve the matching cloud-optimized GeoTIFF overview instead
            # of every full-res tile (reading a coarse VRT whole would force a
            # full-res source read -- thousands of range requests).
            vrts.append(WarpedVRT(ds, crs="EPSG:4326", resampling=Resampling.average))

        left, bottom, right, top = _stack_bounds([v.bounds for v in vrts], extent=extent, bbox=bbox)

        # Output grid: max_size on the longer side, aspect from the lon/lat extent.
        w_deg, h_deg = right - left, top - bottom
        if w_deg >= h_deg:
            out_w = max(int(max_size), 1)
            out_h = max(round(out_w * h_deg / w_deg), 1)
        else:
            out_h = max(int(max_size), 1)
            out_w = max(round(out_h * w_deg / h_deg), 1)
        xres, yres = w_deg / out_w, h_deg / out_h

        slices = []
        for vrt in vrts:
            slab = np.full((out_h, out_w), np.nan, dtype="float32")
            # The part of the output grid this scene actually covers. Under
            # "intersection" that is the whole grid, so every read targets the
            # identical window and shape and the slices are exactly aligned.
            ol, ob = max(left, vrt.bounds.left), max(bottom, vrt.bounds.bottom)
            orr, ot = min(right, vrt.bounds.right), min(top, vrt.bounds.top)
            if ol < orr and ob < ot:
                col0 = max(round((ol - left) / xres), 0)
                col1 = min(round((orr - left) / xres), out_w)
                row0 = max(round((top - ot) / yres), 0)
                row1 = min(round((top - ob) / yres), out_h)
                if col1 > col0 and row1 > row0:
                    # Snap back to those pixel edges so the read is grid-aligned,
                    # clamped inside the VRT so the window is always readable.
                    win = vrt.window(
                        max(left + col0 * xres, vrt.bounds.left),
                        max(top - row1 * yres, vrt.bounds.bottom),
                        min(left + col1 * xres, vrt.bounds.right),
                        min(top - row0 * yres, vrt.bounds.top),
                    )
                    # List index + 3-D out_shape, dropping the band axis here:
                    # rasterio's scalar-index path squeezes in place with an
                    # ndarray.shape assignment, deprecated in NumPy 2.5.
                    data = vrt.read(
                        [1],
                        window=win,
                        out_shape=(1, row1 - row0, col1 - col0),
                        resampling=Resampling.average,
                    )[0].astype("float32")
                    slab[row0:row1, col0:col1] = _mask_slice(np, data, vrt.nodata, db=db)
            slices.append(slab)
    finally:
        for v in vrts:
            v.close()
        for ds in datasets:
            ds.close()

    transform = Affine(xres, 0.0, left, 0.0, -yres, top)
    xs = left + xres * (np.arange(out_w) + 0.5)
    ys = top - yres * (np.arange(out_h) + 0.5)
    times = np.array(
        [i.datetime.astimezone(timezone.utc).replace(tzinfo=None) for i in ordered],  # type: ignore[union-attr]
        dtype="datetime64[ns]",
    )

    return xr.DataArray(
        np.stack(slices),
        dims=("time", "y", "x"),
        coords={
            "time": times,
            "y": ys,
            "x": xs,
            "item_id": ("time", [i.id for i in ordered]),
        },
        name="backscatter_db" if db else "amplitude",
        attrs={
            "crs": "EPSG:4326",
            "transform": tuple(transform)[:6],
            "bounds": (left, bottom, right, top),
            "extent": extent,
            "units": "dB" if db else "amplitude",
            "long_name": "SAR backscatter (dB)" if db else "SAR amplitude",
            "product_type": asset,
            "license": DATA_LICENSE,
            "attribution": ATTRIBUTION,
        },
    )


def stack_to_geotiff(
    items: Iterable[UmbraItem],
    dest: str | os.PathLike,
    *,
    asset: str = "GEC",
    bbox: BBox | None = None,
    max_size: int = 1024,
    db: bool = False,
    extent: str = "intersection",
) -> Path:
    """Co-register several acquisitions and write the cube to a GeoTIFF.

    The file-producing companion to :func:`to_stack`, mirroring what
    :func:`to_geotiff` is to :func:`to_xarray`. The output is a multi-band
    ``float32`` GeoTIFF in EPSG:4326 -- **one band per acquisition, oldest
    first** -- with each band described by its acquisition timestamp and the
    item ids carried in the file tags, so the time axis survives the trip into
    QGIS, GDAL or any GIS. Nodata is ``NaN``; deflate-compressed and tiled.
    """
    rasterio = _require("rasterio")
    _require("numpy")
    from affine import Affine  # noqa: PLC0415

    cube = to_stack(items, asset=asset, bbox=bbox, max_size=max_size, db=db, extent=extent)
    data = cube.values
    stamps = [str(t)[:19] for t in cube["time"].values]
    ids = [str(v) for v in cube["item_id"].values]

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    profile = {
        "driver": "GTiff",
        "height": data.shape[1],
        "width": data.shape[2],
        "count": data.shape[0],
        "dtype": "float32",
        "crs": cube.attrs["crs"],
        "transform": Affine(*cube.attrs["transform"]),
        "nodata": float("nan"),
        "compress": "deflate",
        "tiled": True,
    }
    with rasterio.open(dest, "w", **profile) as dst:
        for band, (stamp, item_id) in enumerate(zip(stamps, ids, strict=True), start=1):
            dst.write(data[band - 1], band)
            dst.set_band_description(band, f"{stamp} {item_id}")
        dst.update_tags(
            item_ids=",".join(ids),
            datetimes=",".join(stamps),
            extent=extent,
            units=cube.attrs["units"],
            license=DATA_LICENSE,
            attribution=ATTRIBUTION,
        )
    return dest
