"""Format conversion helpers (optional, requires the ``convert`` extra).

Umbra's ``GEC`` asset is already a geocoded cloud-optimized GeoTIFF and needs no
conversion. The complex products (``SICD``/``CPHD``) live in the radar slant
plane; getting them onto a map is the format gap that generates the most
support burden (`STRATEGY.md` 5.5). This module provides two well-defined steps:

* :func:`sicd_to_amplitude_geotiff` — a quick, *ungeoreferenced* detected
  amplitude image in the slant plane, for inspection.
* :func:`sicd_to_geocoded_cog` — a north-up, EPSG:4326 **geocoded** cloud-
  optimized GeoTIFF. It projects a grid of image points to ground with SICD's
  own image-projection model, tags them as ground control points, and warps the
  amplitude onto a regular geographic grid — so the scene drops straight onto a
  map or into ``rioxarray`` / QGIS with no hand-rolled geocoding.

The geocoding is a *flat-earth* first slice: the projection places pixels on the
scene's height-above-ellipsoid plane (the SICD ``HAE`` projection), which is
exact over flat terrain and good enough for map placement everywhere. Full
terrain orthorectification (a DEM, MultiRTC interop) is the follow-on.

Install with: ``pip install "umbra-py[convert]"``
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .exceptions import MissingDependencyError

if TYPE_CHECKING:  # pragma: no cover - import only for type checkers
    import numpy as np
    from rasterio.control import GroundControlPoint

#: Resampling kernels accepted by :func:`sicd_to_geocoded_cog`, mapped to the
#: matching ``rasterio.warp.Resampling`` member at call time.
RESAMPLING_METHODS = ("nearest", "bilinear", "cubic", "average", "lanczos")


def _require(module: str):
    try:
        return __import__(module)
    except ImportError as exc:  # pragma: no cover - exercised only without extra
        raise MissingDependencyError(
            f"'{module}' is required for conversion. "
            'Install the extra with: pip install "umbra-py[convert]"',
            hint='pip install "umbra-py[convert]"',
        ) from exc


def _amplitude(complex_data: Any, *, decibels: bool):
    """Detected amplitude of complex SAR data, optionally in decibels."""
    np = _require("numpy")
    amplitude = np.abs(complex_data).astype("float32")
    if decibels:
        amplitude = 20.0 * np.log10(np.clip(amplitude, 1e-6, None))
    return amplitude


def sicd_to_amplitude_geotiff(
    src: str | os.PathLike,
    dst: str | os.PathLike,
    *,
    decibels: bool = True,
) -> Path:
    """Read a SICD (complex) image and write its detected amplitude as a GeoTIFF.

    This is an inspection-quality product in the slant plane: the output is
    *not* geocoded. For a map-ready raster use :func:`sicd_to_geocoded_cog`, or
    use the item's ``GEC`` asset directly when one exists.

    Parameters
    ----------
    src:
        Path to a SICD NITF file.
    dst:
        Output GeoTIFF path.
    decibels:
        If true, scale amplitude to dB (``20*log10``); otherwise raw magnitude.
    """
    _require("sarpy")
    _require("rasterio")
    import rasterio  # noqa: PLC0415
    from rasterio.transform import from_origin  # noqa: PLC0415
    from sarpy.io.complex.converter import open_complex  # noqa: PLC0415

    reader = open_complex(str(src))
    amplitude = _amplitude(reader[:, :], decibels=decibels)

    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    rows, cols = amplitude.shape
    profile = {
        "driver": "GTiff",
        "height": rows,
        "width": cols,
        "count": 1,
        "dtype": "float32",
        "compress": "deflate",
        "tiled": True,
        # No geolocation: identity transform in pixel space.
        "transform": from_origin(0, 0, 1, 1),
    }
    with rasterio.open(dst, "w", **profile) as out:
        out.write(amplitude, 1)
    return dst


def _grid_indices(n: int, count: int) -> list[int]:
    """``count`` evenly-spaced integer indices spanning ``0 .. n-1`` inclusive.

    Always includes both endpoints so the ground-control grid pins the image
    corners (where slant-plane geolocation error is largest). ``count`` is
    clamped to ``[2, n]``.
    """
    count = max(2, min(count, n))
    if n <= 1:
        return [0]
    step = (n - 1) / (count - 1)
    idx = sorted({int(round(i * step)) for i in range(count)})
    return idx


def _build_gcps(
    sicd: Any,
    shape: tuple[int, int],
    *,
    grid: int,
    projection_type: str,
) -> list[GroundControlPoint]:
    """Ground control points mapping image (row, col) to lon/lat via the SICD model.

    Projects a ``grid``×``grid`` lattice of image coordinates to WGS-84 ground
    coordinates using SICD's own image-projection algorithm, so the warp in
    :func:`_warp_gcps_to_cog` reproduces the sensor geometry rather than a naive
    corner-stretch. ``projection_type`` is passed to
    :meth:`SICDType.project_image_to_ground_geo` (``"HAE"`` flat-earth,
    ``"PLANE"``, or ``"DEM"``).
    """
    np = _require("numpy")
    from rasterio.control import GroundControlPoint  # noqa: PLC0415

    rows, cols = shape
    row_idx = _grid_indices(rows, grid)
    col_idx = _grid_indices(cols, grid)
    im_points = np.array([[r, c] for r in row_idx for c in col_idx], dtype="float64")
    # ordering="latlong" -> columns are [lat, lon, hae]; project on the scene's
    # height plane so a whole flat scene lands in the right place.
    ground = sicd.project_image_to_ground_geo(
        im_points, ordering="latlong", projection_type=projection_type
    )
    gcps = []
    for (row, col), (lat, lon, hae) in zip(im_points, ground, strict=True):
        gcps.append(
            GroundControlPoint(
                row=float(row), col=float(col), x=float(lon), y=float(lat), z=float(hae)
            )
        )
    return gcps


def _warp_gcps_to_cog(
    amplitude: np.ndarray,
    gcps: list[GroundControlPoint],
    dst: str | os.PathLike,
    *,
    resolution: float | None,
    resampling: str,
    nodata: float,
) -> Path:
    """Warp a GCP-tagged amplitude array onto a north-up EPSG:4326 COG.

    This is the geocoding core, deliberately free of any SICD/sarpy dependency
    so it is exercised offline with a plain array and hand-built GCPs. The
    output bounds come from the GCP lon/lat extent; ``resolution`` (degrees)
    defaults to the finer of the two per-axis ground sample distances so the
    warp does not throw away resolution.
    """
    np = _require("numpy")
    from rasterio.crs import CRS  # noqa: PLC0415
    from rasterio.io import MemoryFile  # noqa: PLC0415
    from rasterio.shutil import copy as rio_copy  # noqa: PLC0415
    from rasterio.transform import from_origin  # noqa: PLC0415
    from rasterio.warp import Resampling, reproject  # noqa: PLC0415

    if resampling not in RESAMPLING_METHODS:
        raise ValueError(
            f"Unknown resampling {resampling!r}; choose one of {', '.join(RESAMPLING_METHODS)}."
        )

    rows, cols = amplitude.shape
    xs = [g.x for g in gcps]
    ys = [g.y for g in gcps]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    span_x, span_y = maxx - minx, maxy - miny
    if span_x <= 0 or span_y <= 0:
        raise ValueError("Ground control points are degenerate (zero geographic extent).")

    if resolution is None:
        resolution = min(span_x / cols, span_y / rows)
    if resolution <= 0:
        raise ValueError("resolution must be positive.")

    width = max(1, int(np.ceil(span_x / resolution)))
    height = max(1, int(np.ceil(span_y / resolution)))
    dst_transform = from_origin(minx, maxy, resolution, resolution)
    crs = CRS.from_epsg(4326)

    warped = np.full((height, width), nodata, dtype="float32")
    reproject(
        source=np.ascontiguousarray(amplitude, dtype="float32"),
        destination=warped,
        gcps=gcps,
        src_crs=crs,
        dst_crs=crs,
        dst_transform=dst_transform,
        dst_nodata=nodata,
        resampling=Resampling[resampling],
    )

    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    profile = {
        "driver": "GTiff",
        "height": height,
        "width": width,
        "count": 1,
        "dtype": "float32",
        "crs": crs,
        "transform": dst_transform,
        "nodata": nodata,
    }
    # Write into memory, then emit a true COG (tiled, internal overviews) via
    # the COG driver -- the same "cloud-optimized" shape the GEC assets ship in.
    with MemoryFile() as mem:
        with mem.open(**profile) as tmp:
            tmp.write(warped, 1)
            rio_copy(
                tmp,
                str(dst),
                driver="COG",
                compress="DEFLATE",
                overview_resampling="average",
            )
    return dst


def sicd_to_geocoded_cog(
    src: str | os.PathLike,
    dst: str | os.PathLike,
    *,
    decibels: bool = True,
    gcp_grid: int = 15,
    resolution: float | None = None,
    resampling: str = "bilinear",
    projection_type: str = "HAE",
) -> Path:
    """Geocode a SICD to a north-up EPSG:4326 cloud-optimized GeoTIFF.

    Reads the complex SICD, detects amplitude, and warps it onto a regular
    geographic grid using SICD's own image-projection model (a ``gcp_grid`` ×
    ``gcp_grid`` lattice of ground control points). The result opens on a web
    map, in QGIS, or as a georeferenced :class:`xarray.DataArray` (via
    :func:`umbra_py.to_xarray`) with no further work.

    The geocoding is *flat-earth*: pixels are placed on the scene's
    height-above-ellipsoid plane (``projection_type="HAE"``), which is exact
    over flat terrain and adequate for map placement elsewhere. Full terrain
    orthorectification is out of scope for this first slice.

    Parameters
    ----------
    src:
        Path to a SICD NITF file.
    dst:
        Output GeoTIFF path (written as a COG).
    decibels:
        If true, write the decibel (log-amplitude) scale; otherwise raw
        magnitude.
    gcp_grid:
        Edge of the square lattice of ground control points sampled across the
        image (clamped to the image size). More points track the sensor
        geometry more faithfully at a small projection cost.
    resolution:
        Output pixel size in degrees. ``None`` picks the finer of the two
        per-axis ground sample distances so no resolution is thrown away.
    resampling:
        Warp kernel, one of :data:`RESAMPLING_METHODS`.
    projection_type:
        SICD image-projection type: ``"HAE"`` (flat-earth, the default),
        ``"PLANE"``, or ``"DEM"``.
    """
    np = _require("numpy")
    _require("rasterio")
    _require("sarpy")
    from sarpy.io.complex.converter import open_complex  # noqa: PLC0415

    reader = open_complex(str(src))
    sicd = reader.get_sicds_as_tuple()[0]
    amplitude = _amplitude(reader[:, :], decibels=decibels)
    gcps = _build_gcps(sicd, amplitude.shape, grid=gcp_grid, projection_type=projection_type)
    return _warp_gcps_to_cog(
        amplitude,
        gcps,
        dst,
        resolution=resolution,
        resampling=resampling,
        nodata=float(np.nan),
    )
