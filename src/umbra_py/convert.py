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

By default the geocoding is *flat-earth*: the projection places pixels on the
scene's height-above-ellipsoid plane (the SICD ``HAE`` projection), which is
exact over flat terrain and good enough for map placement everywhere. Over
relief a single height plane mislocates hilltops and valley floors (the pixel is
placed where the radar ray meets the plane, not where it meets the ground), so
:func:`sicd_to_geocoded_cog` also accepts ``dem=`` — any rasterio-readable
digital elevation model. When given, each ground-control point is walked onto
the terrain surface (project at a height → look up the DEM there → reproject,
until the height it lands on stops moving), so the scene is *terrain-
orthorectified* rather than flat-projected. The iteration and the DEM lookup are
both injectable, so the whole path is exercised offline with plain callables and
a hand-written DEM raster — no sarpy DEM plumbing, and any Copernicus/SRTM COG
works as the elevation source.

Global DEMs quote height above the *geoid* (EGM96/EGM2008), but SICD projects
against the *ellipsoid*; the difference (the geoid undulation, up to ~±100 m) is
a systematic geolocation error over relief. Pass ``geoid=`` — a path to an
undulation grid — to add that separation to the sampled DEM height before
projecting, for survey-grade placement. Without it the DEM height is used as-is,
correct to within the local geoid–ellipsoid separation and ample for map
placement.

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


def _sicd_projector(sicd: Any, *, height_bin: float = 1.0):
    """A ``project(im_points, haes) -> (lats, lons)`` over the SICD ``HAE`` model.

    SICD's ``project_image_to_ground_geo(..., projection_type="HAE", hae0=h)``
    projects every point onto a single height plane ``h``; terrain
    orthorectification needs a *different* height per point. This adapter accepts
    a per-point height array and batches points that share a (binned) height into
    one projection call, so the common early iterations — where all points sit on
    the same plane — stay a single call, and the whole thing is still just the
    stock HAE projection. ``height_bin`` (metres) is the grouping granularity;
    its residual is well below the placement accuracy the flat-earth path already
    accepts, and the loop re-refines regardless.
    """
    np = _require("numpy")

    def project(im_points, haes):
        im_points = np.asarray(im_points, dtype="float64")
        n = im_points.shape[0]
        haes = np.broadcast_to(np.asarray(haes, dtype="float64"), (n,))
        lats = np.empty(n, dtype="float64")
        lons = np.empty(n, dtype="float64")
        keys = np.round(haes / height_bin).astype("int64")
        for key in np.unique(keys):
            mask = keys == key
            h = float(np.mean(haes[mask]))
            ground = sicd.project_image_to_ground_geo(
                im_points[mask], ordering="latlong", projection_type="HAE", hae0=h
            )
            ground = np.asarray(ground, dtype="float64")
            lats[mask] = ground[:, 0]
            lons[mask] = ground[:, 1]
        return lats, lons

    return project


def _refine_gcps_with_dem(
    im_points: np.ndarray,
    project: Any,
    sample_height: Any,
    *,
    h0: float,
    max_iter: int = 8,
    tol: float = 1.0,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Walk image points onto the terrain surface sampled from a DEM.

    Flat-earth GCPs put every pixel on one height plane; over relief that
    mislocates a point by roughly ``(terrain_height - h0) * tan(look_angle)`` on
    the ground. Starting from the scene reference height ``h0``, this projects
    each point to ground, looks up the DEM height there, reprojects at that
    height, and repeats until the height a point lands on stops moving (within
    ``tol`` metres) or ``max_iter`` is reached — the standard ortho fixed-point
    iteration.

    Both dependencies are injected so the loop is exercised offline with plain
    callables and no sarpy/rasterio::

        project(im_points, haes)  -> (lats, lons)   # SICD image->ground per height
        sample_height(lons, lats) -> heights        # DEM lookup (NaN where no data)

    Where the DEM has no coverage (``sample_height`` returns NaN) the point keeps
    its last good height rather than snapping to zero, so a scene straddling the
    DEM edge degrades to flat-earth there instead of tearing.
    """
    np = _require("numpy")

    pts = np.asarray(im_points, dtype="float64")
    n = pts.shape[0]
    haes = np.full(n, float(h0), dtype="float64")
    lats, lons = project(pts, haes)
    for _ in range(max_iter):
        sampled = np.asarray(sample_height(lons, lats), dtype="float64")
        new_h = np.where(np.isfinite(sampled), sampled, haes)
        lats, lons = project(pts, new_h)
        moved = float(np.max(np.abs(new_h - haes))) if n else 0.0
        haes = new_h
        if moved <= tol:
            break
    return lats, lons, haes


def _dem_height_sampler(dem_ds: Any):
    """A ``sample_height(lons, lats) -> heights`` reading from an open DEM dataset.

    Reprojects the query lon/lat (EPSG:4326) into the DEM's CRS when they differ,
    samples the first band, and returns NaN outside coverage or at the DEM's
    nodata value — so :func:`_refine_gcps_with_dem` can fall back to the scene
    height there. Sarpy-free and rasterio-only, so it is tested against a
    hand-written DEM GeoTIFF.
    """
    np = _require("numpy")
    from rasterio.warp import transform as warp_transform  # noqa: PLC0415

    nodata = dem_ds.nodata
    left, bottom, right, top = dem_ds.bounds
    to_epsg = dem_ds.crs.to_epsg() if dem_ds.crs else None

    def sample_height(lons, lats):
        lons = np.atleast_1d(np.asarray(lons, dtype="float64"))
        lats = np.atleast_1d(np.asarray(lats, dtype="float64"))
        xs, ys = lons, lats
        if dem_ds.crs is not None and to_epsg != 4326:
            xs, ys = warp_transform("EPSG:4326", dem_ds.crs, lons.tolist(), lats.tolist())
            xs = np.asarray(xs, dtype="float64")
            ys = np.asarray(ys, dtype="float64")
        inside = (xs >= left) & (xs <= right) & (ys >= bottom) & (ys <= top)
        out = np.full(lons.shape, np.nan, dtype="float64")
        coords = list(zip(xs.tolist(), ys.tolist(), strict=True))
        vals = np.array([v[0] for v in dem_ds.sample(coords, indexes=1)], dtype="float64")
        if nodata is not None:
            vals = np.where(vals == nodata, np.nan, vals)
        out[inside] = vals[inside]
        return out

    return sample_height


def _geoid_corrected_sampler(dem_sample: Any, geoid_sample: Any):
    """Compose an orthometric DEM sampler with a geoid grid into an *ellipsoidal*-height sampler.

    Global DEMs (Copernicus GLO-30, SRTM) quote height above the **geoid**
    (EGM96/EGM2008), but SICD's image-projection model wants height above the
    **ellipsoid** (HAE). The two differ by the geoid undulation ``N`` — up to
    ~±100 m worldwide — and feeding an orthometric height in as if it were
    ellipsoidal mislocates a point by roughly ``N * tan(look_angle)`` on the
    ground, the same failure mode terrain orthorectification exists to fix. This
    adapter adds ``N`` at each query point so the refinement loop projects a true
    HAE::

        hae = dem_orthometric_height + geoid_undulation

    Both dependencies are injected, so the correction is exercised offline with
    plain callables — ``dem_sample`` and ``geoid_sample`` share the
    ``(lons, lats) -> heights`` shape of :func:`_dem_height_sampler`, so a geoid
    undulation grid is read with the very same sampler. Where the geoid grid has
    no coverage (``geoid_sample`` returns NaN) the undulation is taken as ``0`` —
    i.e. the DEM height is used uncorrected — so a scene straddling the grid edge
    degrades gracefully rather than tearing. A DEM NaN (no terrain coverage) is
    preserved as NaN, so :func:`_refine_gcps_with_dem` still falls back to the
    scene height there.
    """
    np = _require("numpy")

    def sample_height(lons, lats):
        heights = np.asarray(dem_sample(lons, lats), dtype="float64")
        undulation = np.asarray(geoid_sample(lons, lats), dtype="float64")
        undulation = np.where(np.isfinite(undulation), undulation, 0.0)
        return heights + undulation

    return sample_height


def _scene_reference_hae(sicd: Any) -> float:
    """Scene reference height (SCP HAE, metres) to seed the DEM iteration.

    Falls back to ``0.0`` when the SICD lacks a populated ``GeoData.SCP`` (e.g. a
    test fake), which the fixed-point iteration recovers from in a few extra
    steps.
    """
    try:
        return float(sicd.GeoData.SCP.LLH.HAE)
    except Exception:  # pragma: no cover - defensive; exercised via the 0.0 path
        return 0.0


def _scene_geo_bbox(sicd: Any, shape: tuple[int, int]) -> tuple[float, float, float, float]:
    """Geographic bbox ``(west, south, east, north)`` of the scene's image corners.

    Projects the four image corners onto the scene height plane with SICD's own
    model, so :func:`umbra_py.dem.fetch_dem_for_bbox` knows which Copernicus DEM
    tiles to pull for ``dem="auto"``. A coarse footprint is all the tile resolver
    needs (tiles are 1° cells), so four corners suffice.
    """
    np = _require("numpy")

    rows, cols = shape
    corners = np.array(
        [[0, 0], [0, cols - 1], [rows - 1, 0], [rows - 1, cols - 1]], dtype="float64"
    )
    ground = np.asarray(
        sicd.project_image_to_ground_geo(corners, ordering="latlong", projection_type="HAE"),
        dtype="float64",
    )
    lats, lons = ground[:, 0], ground[:, 1]
    return float(lons.min()), float(lats.min()), float(lons.max()), float(lats.max())


def _build_gcps_dem(
    sicd: Any,
    shape: tuple[int, int],
    *,
    grid: int,
    sample_height: Any,
    h0: float,
) -> list[GroundControlPoint]:
    """Terrain-orthorectified ground control points for :func:`_warp_gcps_to_cog`.

    Like :func:`_build_gcps`, but each lattice point is walked onto the DEM
    surface by :func:`_refine_gcps_with_dem` (via the injectable
    ``sample_height``) instead of projected onto a single flat height plane, so
    the warp reproduces the true ground position over relief. The refined terrain
    height is carried as the GCP ``z``.
    """
    np = _require("numpy")
    from rasterio.control import GroundControlPoint  # noqa: PLC0415

    rows, cols = shape
    row_idx = _grid_indices(rows, grid)
    col_idx = _grid_indices(cols, grid)
    im_points = np.array([[r, c] for r in row_idx for c in col_idx], dtype="float64")
    project = _sicd_projector(sicd)
    lats, lons, haes = _refine_gcps_with_dem(im_points, project, sample_height, h0=h0)
    gcps = []
    for (row, col), lat, lon, hae in zip(im_points, lats, lons, haes, strict=True):
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
    dem: str | os.PathLike | None = None,
    geoid: str | os.PathLike | None = None,
) -> Path:
    """Geocode a SICD to a north-up EPSG:4326 cloud-optimized GeoTIFF.

    Reads the complex SICD, detects amplitude, and warps it onto a regular
    geographic grid using SICD's own image-projection model (a ``gcp_grid`` ×
    ``gcp_grid`` lattice of ground control points). The result opens on a web
    map, in QGIS, or as a georeferenced :class:`xarray.DataArray` (via
    :func:`umbra_py.to_xarray`) with no further work.

    By default the geocoding is *flat-earth*: pixels are placed on the scene's
    height-above-ellipsoid plane (``projection_type="HAE"``), which is exact
    over flat terrain and adequate for map placement elsewhere. Pass ``dem`` — a
    path to any rasterio-readable digital elevation model — to **terrain-
    orthorectify** instead: each control point is walked onto the DEM surface
    (project → sample the DEM → reproject, until it converges), so hilltops and
    valley floors land in their true ground position. ``dem`` supersedes
    ``projection_type``. Pass ``dem="auto"`` to fetch the covering Copernicus
    GLO-30 DEM tiles for the scene automatically (see :mod:`umbra_py.dem`).

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
        SICD image-projection type when ``dem`` is not given: ``"HAE"``
        (flat-earth, the default), ``"PLANE"``, or ``"DEM"``.
    dem:
        Optional path to a digital elevation model (any raster rasterio can
        open, e.g. a Copernicus/SRTM COG), or the literal ``"auto"`` to
        auto-fetch the covering Copernicus GLO-30 tiles for the scene
        (:func:`umbra_py.dem.fetch_dem_for_bbox`). When given, the scene is
        terrain-orthorectified against it and ``projection_type`` is ignored;
        heights are read in the DEM's own vertical datum. ``None`` keeps the
        flat-earth projection.
    geoid:
        Optional path to a geoid-undulation grid (any raster rasterio can open,
        giving the ellipsoid-minus-geoid separation ``N`` in metres, e.g. an
        EGM96/EGM2008 undulation GeoTIFF). Global DEMs quote *orthometric* height
        above the geoid, but SICD projects against the *ellipsoid*, so with a DEM
        this adds ``N`` at each point to convert the sampled height to HAE before
        projecting — survey-grade geolocation over relief. Requires ``dem`` (it
        corrects DEM heights); passing it without ``dem`` is an error. Where the
        grid has no coverage the undulation is taken as ``0`` (the DEM height is
        used uncorrected). ``None`` reads DEM heights as-is (correct to within the
        local geoid–ellipsoid separation, ample for map placement).
    """
    np = _require("numpy")
    _require("rasterio")
    _require("sarpy")
    import rasterio  # noqa: PLC0415
    from sarpy.io.complex.converter import open_complex  # noqa: PLC0415

    reader = open_complex(str(src))
    sicd = reader.get_sicds_as_tuple()[0]
    amplitude = _amplitude(reader[:, :], decibels=decibels)
    if isinstance(dem, str) and dem.lower() == "auto":
        from . import dem as dem_mod  # noqa: PLC0415

        dem = dem_mod.fetch_dem_for_bbox(_scene_geo_bbox(sicd, amplitude.shape))
    if dem is not None:
        import contextlib  # noqa: PLC0415

        with contextlib.ExitStack() as stack:
            dem_ds = stack.enter_context(rasterio.open(str(dem)))
            sample_height = _dem_height_sampler(dem_ds)
            if geoid is not None:
                geoid_ds = stack.enter_context(rasterio.open(str(geoid)))
                sample_height = _geoid_corrected_sampler(
                    sample_height, _dem_height_sampler(geoid_ds)
                )
            gcps = _build_gcps_dem(
                sicd,
                amplitude.shape,
                grid=gcp_grid,
                sample_height=sample_height,
                h0=_scene_reference_hae(sicd),
            )
    elif geoid is not None:
        raise ValueError(
            "geoid= requires dem=: the geoid correction adjusts DEM heights to "
            "ellipsoidal (HAE), so pass a DEM to terrain-orthorectify against."
        )
    else:
        gcps = _build_gcps(sicd, amplitude.shape, grid=gcp_grid, projection_type=projection_type)
    return _warp_gcps_to_cog(
        amplitude,
        gcps,
        dst,
        resolution=resolution,
        resampling=resampling,
        nodata=float(np.nan),
    )
