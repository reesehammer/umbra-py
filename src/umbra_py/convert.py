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
undulation grid, or ``"auto"`` to fetch a global EGM grid — to add that
separation to the sampled DEM height before projecting, for survey-grade
placement. Without it the DEM height is used as-is,
correct to within the local geoid–ellipsoid separation and ample for map
placement.

Terrain orthorectification fixes where each pixel lands; it does nothing to how
bright it is. Radar backscatter is strongly modulated by the local incidence
angle, so on relief a slope tilted toward the radar looks bright and one tilted
away looks dark from geometry alone. Pass ``rtc=True`` (with a ``dem=``) to
**radiometrically terrain-flatten** the geocoded output: each pixel is scaled by
the geometric cosine correction ``cos(reference) / cos(local_incidence)``,
derived from the DEM slope and the scene look geometry, so those geometric
brightness swings are removed. It is a normalisation of *detected amplitude*, not
a calibrated gamma-nought RTC product; the pure-numpy core (terrain normals, look
vector, correction factor) is exercised offline with hand-built arrays.

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


# --------------------------------------------------------------------------- #
# Radiometric terrain flattening (RTC).
#
# Terrain orthorectification above places every pixel in its true *ground*
# position; it does nothing to the pixel's *brightness*. But radar backscatter
# is strongly modulated by the local incidence angle (LIA) — the angle between
# the radar line of sight and the terrain surface normal — so on relief a slope
# tilted toward the sensor looks bright and one tilted away looks dark, purely
# from geometry rather than from any real difference on the ground. Radiometric
# terrain correction removes that geometric modulation so the imagery can be
# compared and analysed across terrain.
#
# What ships here is the standard geometric *cosine correction* to a reference
# incidence angle (the honest first slice, matching the flat-earth-then-DEM
# cadence of the geocoding above): each pixel's value is scaled in the power
# domain by ``cos(theta_ref) / cos(theta_lia)``, computed from the DEM's local
# slope and the scene's look geometry. On flat ground the LIA equals the scene
# incidence angle, so with the default reference (the scene incidence) flat
# terrain is left unchanged and only slopes are flattened. This is a geometric
# normalisation of *detected amplitude*, not a calibrated gamma-nought RTC
# product (Umbra's open products are not radiometrically calibrated) — it is
# documented as exactly that.
#
# The whole computation is a pure-numpy core (terrain normals from a DEM patch,
# the look vector, their dot product, and the correction factor) with closed-form
# behaviour over a planar slope, so it is exercised offline with hand-built
# arrays; only resampling the DEM onto the output grid touches rasterio.
# --------------------------------------------------------------------------- #

#: Metres per degree of latitude / longitude at the equator, for turning a
#: degree-spaced geographic grid into the ground distances a slope needs. The
#: east-west figure is scaled by ``cos(latitude)`` per row.
_M_PER_DEG_LAT = 111320.0
_M_PER_DEG_LON = 111320.0

#: Clamp on the power-domain correction factor (``+/- 10 dB``), so a near-shadow
#: slope where ``cos(theta_lia)`` approaches zero cannot amplify noise without
#: bound.
_RTC_FACTOR_MIN = 0.1
_RTC_FACTOR_MAX = 10.0


def _terrain_normals(dem: np.ndarray, *, x_res_deg: float, y_res_deg: float, top_lat: float):
    """Per-pixel unit surface normals ``(east, north, up)`` from a north-up DEM.

    ``dem`` is a 2-D height array on a north-up EPSG:4326 grid (row 0 is the
    northmost row). ``x_res_deg`` / ``y_res_deg`` are the pixel size in degrees
    (positive), and ``top_lat`` is the latitude of the top row's centre, so the
    east-west ground spacing can shrink with ``cos(latitude)`` away from the
    equator. Flat ground yields ``(0, 0, 1)``.

    NaNs (DEM gaps) are filled with the scene mean height before differencing so
    a gap reads as locally flat (normal straight up) rather than tearing the
    gradient; callers suppress the correction over gaps separately.
    """
    np = _require("numpy")

    dem = np.asarray(dem, dtype="float64")
    finite = np.isfinite(dem)
    fill = float(np.mean(dem[finite])) if finite.any() else 0.0
    dem = np.where(finite, dem, fill)

    h, _w = dem.shape
    rows = np.arange(h, dtype="float64")
    lat = np.clip(top_lat - rows * y_res_deg, -89.9, 89.9)  # north-up: row -> south
    dy = y_res_deg * _M_PER_DEG_LAT  # north spacing (metres), constant per row
    dx = np.maximum(x_res_deg * _M_PER_DEG_LON * np.cos(np.radians(lat)), 1e-6)

    dz_drow, dz_dcol = np.gradient(dem)
    dz_deast = dz_dcol / dx[:, None]
    dz_dnorth = -dz_drow / dy  # north is the -row direction on a north-up grid
    # Upward normal of the surface z = f(east, north): (-dz/deast, -dz/dnorth, 1).
    nx = -dz_deast
    ny = -dz_dnorth
    nz = np.ones_like(dem)
    norm = np.sqrt(nx * nx + ny * ny + nz * nz)
    return nx / norm, ny / norm, nz / norm


def _look_unit_vector(incidence_deg: float, azimuth_deg: float):
    """Unit vector from a ground point toward the sensor, in local ENU.

    ``incidence_deg`` is the incidence angle (from vertical) and ``azimuth_deg``
    is the azimuth (clockwise from north) of the horizontal ground-to-sensor
    direction — SICD's ``SCPCOA.AzimAng``. Over flat ground (up-normal) the dot
    product with this vector is ``cos(incidence)``, i.e. the local incidence
    angle reduces to the scene incidence angle.
    """
    np = _require("numpy")

    theta = np.radians(float(incidence_deg))
    az = np.radians(float(azimuth_deg))
    sin_t = np.sin(theta)
    return (sin_t * np.sin(az), sin_t * np.cos(az), np.cos(theta))


def _cos_local_incidence(normals, look):
    """Cosine of the local incidence angle: the normal-look dot product."""
    nx, ny, nz = normals
    lx, ly, lz = look
    return nx * lx + ny * ly + nz * lz


def _terrain_flatten_factor(cos_lia, *, cos_ref: float):
    """Power-domain correction factor ``cos_ref / cos_lia``, clamped and gap-safe.

    A slope facing the radar has a smaller local incidence angle (larger
    ``cos_lia``) and looks artificially bright, so its factor is below one
    (darkened); a slope facing away is brightened. Non-finite ``cos_lia`` (a DEM
    gap) takes ``cos_ref`` so the factor is exactly one (no change), and
    ``cos_lia`` at or below zero (radar shadow / steep back-slope, where the
    cosine correction is undefined) is floored before dividing. The result is
    clamped to :data:`_RTC_FACTOR_MIN` .. :data:`_RTC_FACTOR_MAX`.
    """
    np = _require("numpy")

    cos_lia = np.asarray(cos_lia, dtype="float64")
    cos_lia = np.where(np.isfinite(cos_lia), cos_lia, cos_ref)
    cos_lia = np.clip(cos_lia, 1e-3, 1.0)
    factor = cos_ref / cos_lia
    return np.clip(factor, _RTC_FACTOR_MIN, _RTC_FACTOR_MAX)


def _apply_terrain_flattening(amplitude: np.ndarray, factor, *, decibels: bool):
    """Apply a power-domain correction ``factor`` to a detected-amplitude raster.

    ``amplitude`` is the geocoded output: ``20*log10(|z|)`` (which equals
    ``10*log10(power)``) when ``decibels`` is true, else linear magnitude. So in
    decibels the correction adds ``10*log10(factor)``; in linear magnitude it
    multiplies by ``sqrt(factor)``. NaN nodata is preserved either way.
    """
    np = _require("numpy")

    amplitude = np.asarray(amplitude, dtype="float32")
    factor = np.asarray(factor, dtype="float64")
    if decibels:
        return (amplitude + 10.0 * np.log10(factor)).astype("float32")
    return (amplitude * np.sqrt(factor)).astype("float32")


def _scene_look_geometry(sicd: Any) -> tuple[float, float]:
    """``(incidence_deg, azimuth_deg)`` at the scene centre from SICD ``SCPCOA``.

    Reads ``SCPCOA.IncidenceAng`` (angle from vertical) and ``SCPCOA.AzimAng``
    (azimuth clockwise from north of the ground-to-sensor line of sight), the
    scene-centre geometry every SICD carries. A scene-constant look vector is the
    standard approximation for a scene-scale flattening. Raises if the fields are
    absent, since the correction has no meaning without them.
    """
    scpcoa = getattr(sicd, "SCPCOA", None)
    inc = getattr(scpcoa, "IncidenceAng", None)
    az = getattr(scpcoa, "AzimAng", None)
    if inc is None or az is None:
        raise ValueError(
            "SICD is missing SCPCOA.IncidenceAng / SCPCOA.AzimAng, which "
            "radiometric terrain flattening (--rtc) needs for the look geometry."
        )
    return float(inc), float(az)


def _terrain_flatten_on_grid(
    warped: np.ndarray,
    dst_transform: Any,
    width: int,
    height: int,
    *,
    dem: str | os.PathLike,
    incidence_deg: float,
    azimuth_deg: float,
    reference_deg: float,
    decibels: bool,
) -> np.ndarray:
    """Radiometrically flatten a warped amplitude raster against a DEM.

    Resamples ``dem`` onto the output grid (``dst_transform`` / ``width`` /
    ``height``), derives the per-pixel local incidence angle from the DEM slope
    and the scene look geometry, and scales the amplitude by the cosine
    correction to ``reference_deg``. The DEM resample is the only rasterio touch;
    the physics is the pure-numpy core above. Over DEM gaps the factor is one, so
    those pixels pass through unchanged.
    """
    np = _require("numpy")
    import rasterio  # noqa: PLC0415
    from rasterio.warp import Resampling, reproject  # noqa: PLC0415

    dem_on_grid = np.full((height, width), np.nan, dtype="float64")
    with rasterio.open(str(dem)) as dem_ds:
        reproject(
            source=rasterio.band(dem_ds, 1),
            destination=dem_on_grid,
            dst_transform=dst_transform,
            dst_crs="EPSG:4326",
            src_nodata=dem_ds.nodata,
            dst_nodata=float("nan"),
            resampling=Resampling.bilinear,
        )

    x_res = abs(dst_transform.a)
    y_res = abs(dst_transform.e)
    top_lat = dst_transform.f + dst_transform.e / 2.0  # centre of the top row
    normals = _terrain_normals(dem_on_grid, x_res_deg=x_res, y_res_deg=y_res, top_lat=top_lat)
    look = _look_unit_vector(incidence_deg, azimuth_deg)
    cos_ref = float(np.cos(np.radians(reference_deg)))
    cos_lia = _cos_local_incidence(normals, look)
    # Suppress the correction where the DEM had no coverage on the output grid.
    cos_lia = np.where(np.isfinite(dem_on_grid), cos_lia, cos_ref)
    factor = _terrain_flatten_factor(cos_lia, cos_ref=cos_ref)
    return _apply_terrain_flattening(warped, factor, decibels=decibels)


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
    post_warp: Any = None,
) -> Path:
    """Warp a GCP-tagged amplitude array onto a north-up EPSG:4326 COG.

    This is the geocoding core, deliberately free of any SICD/sarpy dependency
    so it is exercised offline with a plain array and hand-built GCPs. The
    output bounds come from the GCP lon/lat extent; ``resolution`` (degrees)
    defaults to the finer of the two per-axis ground sample distances so the
    warp does not throw away resolution.

    ``post_warp``, if given, is a callable
    ``(warped, dst_transform, width, height) -> warped`` applied to the geocoded
    array before it is written — the hook radiometric terrain flattening uses to
    adjust pixel values in the output geometry, kept out of the sarpy-free core.
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

    if post_warp is not None:
        warped = np.ascontiguousarray(
            post_warp(warped, dst_transform, width, height), dtype="float32"
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
    rtc: bool = False,
    rtc_reference_deg: float | None = None,
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
        EGM96/EGM2008 undulation GeoTIFF), or the literal ``"auto"`` to fetch a
        global EGM geoid grid for the scene automatically
        (:func:`umbra_py.geoid.fetch_geoid_grid`). Global DEMs quote *orthometric*
        height above the geoid, but SICD projects against the *ellipsoid*, so with
        a DEM this adds ``N`` at each point to convert the sampled height to HAE
        before projecting — survey-grade geolocation over relief. Requires ``dem``
        (it corrects DEM heights); passing it without ``dem`` is an error. Where
        the grid has no coverage the undulation is taken as ``0`` (the DEM height
        is used uncorrected). ``None`` reads DEM heights as-is (correct to within
        the local geoid–ellipsoid separation, ample for map placement).
    rtc:
        If true, **radiometrically terrain-flatten** the output: after geocoding,
        scale each pixel by the geometric cosine correction
        ``cos(reference) / cos(local_incidence)``, computed from the DEM slope and
        the scene look geometry, so slopes tilted toward or away from the radar no
        longer look artificially bright or dark. Requires ``dem`` (the correction
        needs terrain); passing it without ``dem`` is an error. This is a
        geometric normalisation of detected amplitude, not a calibrated
        gamma-nought product.
    rtc_reference_deg:
        Reference incidence angle (degrees) the flattening normalises to. ``None``
        uses the scene incidence angle, so flat terrain is left unchanged and only
        slopes are corrected.
    """
    np = _require("numpy")
    _require("rasterio")
    _require("sarpy")
    import rasterio  # noqa: PLC0415
    from sarpy.io.complex.converter import open_complex  # noqa: PLC0415

    if rtc and dem is None:
        raise ValueError(
            "rtc= requires dem=: radiometric terrain flattening derives the local "
            "incidence angle from a DEM, so pass a DEM to flatten against."
        )

    reader = open_complex(str(src))
    sicd = reader.get_sicds_as_tuple()[0]
    amplitude = _amplitude(reader[:, :], decibels=decibels)
    if isinstance(dem, str) and dem.lower() == "auto":
        from . import dem as dem_mod  # noqa: PLC0415

        dem = dem_mod.fetch_dem_for_bbox(_scene_geo_bbox(sicd, amplitude.shape))
    if dem is not None:
        import contextlib  # noqa: PLC0415

        if isinstance(geoid, str) and geoid.lower() == "auto":
            from . import geoid as geoid_mod  # noqa: PLC0415

            geoid = geoid_mod.fetch_geoid_grid()
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

    post_warp = None
    if rtc:
        incidence_deg, azimuth_deg = _scene_look_geometry(sicd)
        reference_deg = incidence_deg if rtc_reference_deg is None else float(rtc_reference_deg)

        def post_warp(warped, dst_transform, width, height):
            return _terrain_flatten_on_grid(
                warped,
                dst_transform,
                width,
                height,
                dem=dem,
                incidence_deg=incidence_deg,
                azimuth_deg=azimuth_deg,
                reference_deg=reference_deg,
                decibels=decibels,
            )

    return _warp_gcps_to_cog(
        amplitude,
        gcps,
        dst,
        resolution=resolution,
        resampling=resampling,
        nodata=float(np.nan),
        post_warp=post_warp,
    )
