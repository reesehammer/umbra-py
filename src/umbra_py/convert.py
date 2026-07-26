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
**radiometrically terrain-flatten** the geocoded output, derived from the DEM
slope and the scene look geometry so those geometric brightness swings are
removed. Four models are available (``rtc_model=``): the default ``"cosine"``
geometric correction ``cos(reference) / cos(local_incidence)`` using the full 3-D
local incidence angle; ``"area"``, the projected-area / foreshortening correction
``sin(local_range_incidence) / sin(reference)`` that works in the range–vertical
plane so it targets the range-direction foreshortening and layover which dominate
radiometric terrain distortion; ``"gamma"``, the per-pixel facet-area
(gamma-nought) normalisation ``cos(reference) * nz / cos(local_incidence)``, which
adds the true tilted-facet-area term ``nz`` — the ground-referenced cosine and
range-plane area models both omit it — using the full 3-D facet normal; and
``"facet"``, the **image-space illuminated-area integration** (Small 2011), which
projects every terrain facet into the radar's own ``(slant_range, azimuth)``
geometry, accumulates the illuminated area landing in each radar cell, and
normalises each pixel by that total. The first three correct a pixel from its own
slope, so none of them can see terrain *folding*; the fourth is the one that
measures layover, because several facets imaging into one cell sum there. The
pure-numpy core (terrain normals, look vector, radar coordinates, area
accumulation, correction factors) is exercised offline with hand-built arrays.

Flattening removes the terrain's *geometric* brightness swings but leaves the
result in whatever arbitrary units the product's pixels carry — a relative
image, comparable within itself and with nothing else. ``calibration=`` closes
that last gap: it scales pixel power by the SICD's own ``Radiometric``
scale-factor polynomial, so the output is a physical quantity — the ``sigma0`` /
``beta0`` / ``gamma0`` backscatter coefficients (referenced to unit ground,
slant-plane and perpendicular-to-look area) or ``rcs``, the absolute radar cross
section in m². Both are power-domain factors, so they compose:
``rtc_model="facet"`` with ``calibration="gamma0"`` is a terrain-flattened
gamma-nought product whose decibels mean the same thing across scenes and dates.
The calibration is only ever as real as the metadata behind it — Umbra's open
products generally ship *without* a ``Radiometric`` block, and asking for a
calibration a product cannot support raises rather than returning a
plausible-looking number. :func:`sicd_calibration_types` reports what a given
file supports before you ask for it.

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

#: Radiometric terrain-flattening models accepted by
#: :func:`sicd_to_geocoded_cog` (``rtc_model=``). ``"cosine"`` is the geometric
#: cosine correction; ``"area"`` is the projected-area / foreshortening model;
#: ``"gamma"`` is the per-pixel facet-area (gamma-nought) normalisation;
#: ``"facet"`` is the image-space illuminated-area integration, the only one that
#: accumulates several terrain facets into one radar cell (layover).
RTC_MODELS = ("cosine", "area", "gamma", "facet")

#: Radiometric calibrations accepted by :func:`sicd_to_geocoded_cog` and
#: :func:`sicd_to_amplitude_geotiff` (``calibration=``). Each names a SICD
#: ``Radiometric`` scale-factor polynomial that converts detected *power* into a
#: physical quantity: the three backscatter coefficients ``sigma0`` (per unit
#: **ground** area), ``beta0`` (per unit **slant-plane** area) and ``gamma0``
#: (per unit area **perpendicular to the look direction**), plus ``rcs``, the
#: absolute radar cross-section in m² rather than a per-area coefficient.
CALIBRATION_TYPES = ("sigma0", "beta0", "gamma0", "rcs")

#: Maps each :data:`CALIBRATION_TYPES` member to the SICD ``Radiometric``
#: polynomial that defines it. A product only supports the calibrations whose
#: polynomial its own metadata carries.
_CALIBRATION_POLYS = {
    "sigma0": "SigmaZeroSFPoly",
    "beta0": "BetaZeroSFPoly",
    "gamma0": "GammaZeroSFPoly",
    "rcs": "RCSSFPoly",
}


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


# --------------------------------------------------------------------------- #
# Radiometric calibration (SICD ``Radiometric`` scale-factor polynomials).
# --------------------------------------------------------------------------- #


def _available_calibrations(sicd: Any) -> tuple[str, ...]:
    """Which :data:`CALIBRATION_TYPES` this SICD's own metadata supports.

    Empty when the product carries no ``Radiometric`` block at all — which is
    the honest answer for an uncalibrated product, and the one that says the
    scale factors are missing rather than assumed.
    """
    radiometric = getattr(sicd, "Radiometric", None)
    if radiometric is None:
        return ()
    return tuple(
        kind
        for kind in CALIBRATION_TYPES
        if getattr(radiometric, _CALIBRATION_POLYS[kind], None) is not None
    )


def _calibration_coefficients(sicd: Any, kind: str):
    """The 2-D scale-factor polynomial coefficients for ``kind``, off a SICD.

    Reads ``Radiometric.<Kind>SFPoly`` and returns its coefficient array (the
    ``Coefs`` of a sarpy ``Poly2DType``, or a bare array/scalar so the pure core
    is testable without sarpy). Raises a self-describing :class:`ValueError`
    when the product cannot support the requested calibration — the whole point
    of the feature is that an uncalibrated product says so rather than emitting
    a number that looks calibrated.

    Every metadata check here runs before ``numpy`` is required, so "this
    product cannot be calibrated" is answerable without the ``convert`` extra.
    """
    if kind not in CALIBRATION_TYPES:
        raise ValueError(
            f"Unknown calibration {kind!r}; choose one of {', '.join(CALIBRATION_TYPES)}."
        )
    radiometric = getattr(sicd, "Radiometric", None)
    if radiometric is None:
        raise ValueError(
            "SICD carries no Radiometric metadata, so it cannot be radiometrically "
            "calibrated: the scale factors that turn detected power into a "
            "backscatter coefficient have to come from the product. Umbra's open "
            "products are typically uncalibrated -- convert without calibration for "
            "the usual (relative) amplitude image."
        )
    poly = getattr(radiometric, _CALIBRATION_POLYS[kind], None)
    if poly is None:
        available = _available_calibrations(sicd)
        offer = ", ".join(available) if available else "none"
        raise ValueError(
            f"SICD Radiometric metadata carries no {_CALIBRATION_POLYS[kind]}, so "
            f"{kind} calibration is unavailable for this product "
            f"(available: {offer})."
        )
    np = _require("numpy")
    coefs = np.atleast_2d(np.asarray(getattr(poly, "Coefs", poly), dtype="float64"))
    if coefs.size == 0:
        raise ValueError(
            f"SICD {_CALIBRATION_POLYS[kind]} has no coefficients, so the "
            f"{kind} scale factor is undefined."
        )
    return coefs


def _image_grid_geometry(sicd: Any) -> dict[str, float]:
    """The SICD image-grid geometry the radiometric polynomials are written in.

    The scale-factor polynomials are functions of image coordinates measured in
    **metres from the scene centre point** (SCP), so evaluating one needs the
    per-axis pixel spacings (``Grid.Row.SS`` / ``Grid.Col.SS``) and the SCP's
    pixel address (``ImageData.SCPPixel``). ``ImageData.FirstRow`` /
    ``FirstCol`` place a chipped image inside the full grid the SCP is quoted
    against and are ``0`` for a full product.
    """
    grid = getattr(sicd, "Grid", None)
    image_data = getattr(sicd, "ImageData", None)
    row_ss = getattr(getattr(grid, "Row", None), "SS", None)
    col_ss = getattr(getattr(grid, "Col", None), "SS", None)
    scp = getattr(image_data, "SCPPixel", None)
    scp_row = getattr(scp, "Row", None)
    scp_col = getattr(scp, "Col", None)
    if row_ss is None or col_ss is None or scp_row is None or scp_col is None:
        raise ValueError(
            "SICD is missing Grid.Row.SS / Grid.Col.SS / ImageData.SCPPixel, which "
            "radiometric calibration needs: the scale-factor polynomials are "
            "functions of image coordinates in metres from the scene centre point."
        )
    return {
        "row_ss": float(row_ss),
        "col_ss": float(col_ss),
        "scp_row": float(scp_row),
        "scp_col": float(scp_col),
        "first_row": float(getattr(image_data, "FirstRow", 0) or 0),
        "first_col": float(getattr(image_data, "FirstCol", 0) or 0),
    }


def _calibration_scale(
    coefs,
    shape: tuple[int, int],
    *,
    row_ss: float,
    col_ss: float,
    scp_row: float,
    scp_col: float,
    first_row: float = 0.0,
    first_col: float = 0.0,
):
    """Per-pixel power-domain scale factor from a SICD SF polynomial.

    Evaluates the 2-D polynomial over the image grid in SICD's own coordinates
    — pixel ``(row, col)`` sits at ``((row + first_row - scp_row) * row_ss,
    (col + first_col - scp_col) * col_ss)`` metres from the SCP — so a constant
    polynomial (the common case) gives a flat scale and a higher-order one
    tracks the across-swath variation the product describes.

    A scale factor is a positive power ratio by construction; a non-positive or
    non-finite value means the metadata cannot be evaluated on this grid, which
    is raised rather than clamped, because a silently repaired calibration is
    worse than none.
    """
    np = _require("numpy")
    from numpy.polynomial import polynomial as npoly  # noqa: PLC0415

    rows, cols = shape
    x = (np.arange(rows, dtype="float64") + first_row - scp_row) * row_ss
    y = (np.arange(cols, dtype="float64") + first_col - scp_col) * col_ss
    xx, yy = np.meshgrid(x, y, indexing="ij")
    scale = np.asarray(
        npoly.polyval2d(xx, yy, np.atleast_2d(np.asarray(coefs, dtype="float64"))),
        dtype="float64",
    )
    bad = ~np.isfinite(scale) | (scale <= 0.0)
    if bool(bad.any()):
        raise ValueError(
            f"The SICD radiometric scale factor is non-positive or non-finite over "
            f"{int(bad.sum())} of {bad.size} pixels, so the calibration polynomial "
            "cannot be evaluated on this image grid. The product's Radiometric "
            "metadata is inconsistent with its image geometry."
        )
    return scale


def _apply_calibration(amplitude: np.ndarray, scale, *, decibels: bool):
    """Apply a radiometric scale factor to a detected-amplitude raster.

    SICD's scale factors multiply *power* (``|z|**2``), which is exactly the
    convention the terrain-flattening factors already use, so this is the same
    power-domain scaling of the same raster: in decibels it adds
    ``10*log10(scale)``, giving the calibrated coefficient in dB directly; in
    linear magnitude it multiplies by ``sqrt(scale)``, giving *calibrated
    amplitude* (square it for the linear coefficient). Sharing one
    implementation is what keeps calibration and ``--rtc`` composable: applied
    together the factors simply multiply in the power domain.
    """
    return _apply_terrain_flattening(amplitude, scale, decibels=decibels)


def _calibrate_amplitude(sicd: Any, amplitude: np.ndarray, *, kind: str, decibels: bool):
    """Calibrate a detected-amplitude raster against the SICD's own metadata."""
    coefs = _calibration_coefficients(sicd, kind)
    scale = _calibration_scale(coefs, amplitude.shape, **_image_grid_geometry(sicd))
    return _apply_calibration(amplitude, scale, decibels=decibels)


def sicd_calibration_types(src: str | os.PathLike) -> tuple[str, ...]:
    """Which radiometric calibrations a SICD product's metadata supports.

    Returns the subset of :data:`CALIBRATION_TYPES` whose scale-factor
    polynomial the file's ``Radiometric`` metadata actually carries — empty for
    an uncalibrated product. Ask this before passing ``calibration=`` to
    :func:`sicd_to_geocoded_cog` when you want to *check* rather than handle the
    error, e.g. when deciding whether a scene can enter a calibrated stack.

    Parameters
    ----------
    src:
        Path to a SICD NITF file.
    """
    _require("sarpy")
    from sarpy.io.complex.converter import open_complex  # noqa: PLC0415

    reader = open_complex(str(src))
    return _available_calibrations(reader.get_sicds_as_tuple()[0])


def sicd_to_amplitude_geotiff(
    src: str | os.PathLike,
    dst: str | os.PathLike,
    *,
    decibels: bool = True,
    calibration: str | None = None,
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
    calibration:
        Optional radiometric calibration, one of :data:`CALIBRATION_TYPES`,
        applied from the SICD's own ``Radiometric`` scale-factor polynomial (see
        :func:`sicd_to_geocoded_cog` for what the calibrated values mean).
        ``None`` writes the uncalibrated amplitude. Raises when the product
        carries no scale factor for the requested calibration — ask
        :func:`sicd_calibration_types` first to check.
    """
    _require("sarpy")
    _require("rasterio")
    import rasterio  # noqa: PLC0415
    from rasterio.transform import from_origin  # noqa: PLC0415
    from sarpy.io.complex.converter import open_complex  # noqa: PLC0415

    if calibration is not None and calibration not in CALIBRATION_TYPES:
        raise ValueError(
            f"Unknown calibration {calibration!r}; choose one of {', '.join(CALIBRATION_TYPES)}."
        )

    reader = open_complex(str(src))
    amplitude = _amplitude(reader[:, :], decibels=decibels)
    if calibration is not None:
        amplitude = _calibrate_amplitude(
            reader.get_sicds_as_tuple()[0],
            amplitude,
            kind=calibration,
            decibels=decibels,
        )

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
# Three geometric models ship here, all an honest first slice matching the
# flat-earth-then-DEM cadence of the geocoding above, and all a normalisation of
# *detected amplitude*, not a calibrated gamma-nought product (Umbra's open
# products are not radiometrically calibrated) — documented as exactly that:
#
#  * The "cosine" model (the default) scales each pixel in the power domain by
#    ``cos(theta_ref) / cos(theta_lia)`` using the full 3-D local incidence angle
#    from the DEM's local slope and the scene's look geometry.
#  * The "area" model scales by ``sin(theta_ref_range... )`` — precisely
#    ``sin(theta_local) / sin(theta_ref)`` — measured in the *range–vertical*
#    plane. Only the range-direction terrain tilt foreshortens the SAR image, so
#    this targets the range foreshortening and layover that dominate radiometric
#    terrain distortion, separating them from the azimuth-direction tilt the
#    per-pixel cosine model folds in.
#  * The "gamma" model scales by ``cos(theta_ref) * nz / cos(theta_lia)`` — the
#    per-pixel facet-area (gamma-nought) normalisation. It normalises power by the
#    local illuminated facet area projected into the plane perpendicular to the
#    look direction: a terrain facet whose ground-projected area is one pixel has
#    true (tilted) area ``1 / nz`` (``nz`` = cosine of the slope from horizontal),
#    and its projection onto the look-perpendicular plane is ``(1 / nz) *
#    cos(theta_lia)``, so the illuminated area per pixel scales as
#    ``cos(theta_lia) / nz`` and normalising to the reference geometry gives the
#    factor above. It uses the full 3-D facet normal (like "cosine", unlike the
#    range-plane "area") and adds the true-facet-area term ``nz`` both other
#    models omit — the gamma-nought convention on a per-pixel facet.
#
# All three are honest first slices, not the full image-space illuminated-area
# facet integration (Small 2011 — integrating the projected local illuminated area
# per pixel over the DEM in slant/azimuth image space, accumulating layover) or
# MultiRTC interop, which remain deferred.
#
# On flat ground all three reduce to the scene incidence angle, so with the
# default reference (the scene incidence) flat terrain is left unchanged and only
# slopes are flattened. Each is a pure-numpy core (terrain normals from a DEM
# patch, the look/range geometry, and the correction factor) with closed-form
# behaviour over a planar slope, so all are exercised offline with hand-built
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


def _range_local_incidence(normals, *, incidence_deg: float, azimuth_deg: float):
    """Local incidence angle (radians) measured in the range–vertical plane.

    Only the terrain tilt in the *ground-range* direction foreshortens the SAR
    image; a slope tilted purely in the azimuth direction (perpendicular to the
    look) does not compress the range sampling and so does not change the
    illuminated area to first order. The projected-area model therefore works in
    the plane spanned by the horizontal ground-range direction ``r`` (the
    horizontal projection of the ground-to-sensor look, at ``azimuth_deg``
    clockwise from north) and the vertical, ignoring the azimuth-direction tilt
    the pure-cosine local-incidence angle folds in.

    Returns ``theta_local = theta_scene - alpha``, where ``alpha`` is the range
    tilt of the surface toward the sensor: ``alpha > 0`` when the slope faces the
    radar (foreshortened, smaller local incidence), ``alpha < 0`` on a back-slope.
    Flat ground yields ``alpha = 0`` so ``theta_local == theta_scene``.
    """
    np = _require("numpy")

    nx, ny, nz = normals
    az = np.radians(float(azimuth_deg))
    # Component of the up-normal along the horizontal ground-range direction:
    # positive when the surface leans toward the sensor.
    n_r = nx * np.sin(az) + ny * np.cos(az)
    alpha = np.arctan2(n_r, nz)
    return np.radians(float(incidence_deg)) - alpha


def _foreshortening_factor(theta_local, *, sin_ref: float):
    """Power-domain area factor ``sin(theta_local) / sin(theta_ref)``, gap-safe.

    The radar-illuminated ground area per resolution cell scales as
    ``1 / sin(theta_local)`` in the range direction, so a foreshortened
    radar-facing slope (small ``theta_local``) integrates a larger area and looks
    artificially bright; scaling its power by ``sin(theta_local) / sin(theta_ref)``
    normalises it back to the reference geometry (factor below one → darkened). A
    back-slope (larger ``theta_local``) is brightened. Non-finite inputs (a DEM
    gap propagated through) take ``sin_ref`` so the factor is exactly one, and
    layover / radar-shadow where ``sin(theta_local)`` collapses toward zero is
    floored before dividing, then the whole factor is clamped to
    :data:`_RTC_FACTOR_MIN` .. :data:`_RTC_FACTOR_MAX` so it cannot run away.
    """
    np = _require("numpy")

    sin_local = np.sin(np.asarray(theta_local, dtype="float64"))
    sin_local = np.where(np.isfinite(sin_local), sin_local, sin_ref)
    # Layover / back-projection (theta_local <= 0) cannot be inverted; floor it.
    sin_local = np.clip(sin_local, 1e-3, None)
    factor = sin_local / sin_ref
    return np.clip(factor, _RTC_FACTOR_MIN, _RTC_FACTOR_MAX)


def _facet_area_factor(cos_lia, nz, *, cos_ref: float):
    """Gamma-nought facet-area correction ``cos_ref * nz / cos_lia``, gap-safe.

    Normalises detected power by the local *illuminated facet area* projected
    into the plane perpendicular to the look direction — the gamma-nought
    convention. A terrain facet whose ground-projected area is one pixel has true
    (tilted) surface area ``1 / nz`` (``nz`` = cosine of the slope from horizontal,
    the up-component of the unit normal), and its projection onto the
    look-perpendicular plane is ``(1 / nz) * cos_lia``, so the illuminated area per
    pixel scales as ``cos_lia / nz``. Normalising that to the flat reference
    geometry (area ``cos_ref``, ``nz = 1``) gives ``cos_ref * nz / cos_lia``.

    This is exactly the per-pixel cosine factor scaled by the true-facet-area term
    ``nz`` — using the full 3-D facet normal, and adding the area term the
    ground-referenced :func:`_terrain_flatten_factor` and the range-plane
    :func:`_foreshortening_factor` both omit. It is still a per-pixel
    normalisation, not the full image-space area integration over the DEM (layover
    accumulation), which stays deferred.

    Gap-safe and clamped exactly like the other models: a non-finite ``cos_lia``
    (a DEM gap) takes ``cos_ref`` and a non-finite ``nz`` takes one, so the factor
    is exactly one (no change); ``cos_lia`` at or below zero (radar shadow / steep
    back-slope) is floored before dividing; and the result is clamped to
    :data:`_RTC_FACTOR_MIN` .. :data:`_RTC_FACTOR_MAX`.
    """
    np = _require("numpy")

    cos_lia = np.asarray(cos_lia, dtype="float64")
    nz = np.asarray(nz, dtype="float64")
    cos_lia = np.where(np.isfinite(cos_lia), cos_lia, cos_ref)
    nz = np.where(np.isfinite(nz), nz, 1.0)
    cos_lia = np.clip(cos_lia, 1e-3, 1.0)
    factor = cos_ref * nz / cos_lia
    return np.clip(factor, _RTC_FACTOR_MIN, _RTC_FACTOR_MAX)


#: Cap on the radar accumulation grid, as a multiple of the output pixel count.
#: The grid is sized from the pixel spacing, so it tracks the image; extreme
#: relief stretches the slant-range axis, and past this the bins are coarsened
#: rather than the allocation growing without bound.
_RTC_MAX_RADAR_CELLS = 64


def _radar_coordinates(
    dem: np.ndarray,
    *,
    x_res_deg: float,
    y_res_deg: float,
    top_lat: float,
    incidence_deg: float,
    azimuth_deg: float,
):
    """Per-pixel ``(slant_range, azimuth)`` radar coordinates, in metres.

    Maps each cell of a north-up EPSG:4326 DEM grid into the radar's own
    geometry: the slant-range coordinate is the position projected onto the
    ground-to-sensor look direction (so a hill leans *toward* the sensor and
    lands at a shorter range than the ground beneath it — the shift that causes
    foreshortening and layover), and the azimuth coordinate is the position
    along the horizontal direction perpendicular to it.

    A scene-constant look vector is the same plane-wave approximation
    :func:`_scene_look_geometry` makes for the other models; over a single scene
    the range/azimuth axes do not rotate meaningfully. Both coordinates are
    origin-free (only differences matter to the caller), and DEM gaps take the
    scene mean height so a gap reads as locally flat rather than displacing the
    pixel to an arbitrary range.
    """
    np = _require("numpy")

    dem = np.asarray(dem, dtype="float64")
    finite = np.isfinite(dem)
    fill = float(np.mean(dem[finite])) if finite.any() else 0.0
    up = np.where(finite, dem, fill)

    h, w = dem.shape
    rows = np.arange(h, dtype="float64")[:, None]
    cols = np.arange(w, dtype="float64")[None, :]
    lat = np.clip(top_lat - rows * y_res_deg, -89.9, 89.9)
    east = cols * x_res_deg * _M_PER_DEG_LON * np.cos(np.radians(lat))
    north = -rows * y_res_deg * _M_PER_DEG_LAT  # north-up: row -> south

    lx, ly, lz = _look_unit_vector(incidence_deg, azimuth_deg)
    slant = -(east * lx + north * ly + up * lz)
    az = np.radians(float(azimuth_deg))
    # Along-track: the horizontal unit vector perpendicular to the ground-range
    # direction (sin(az), cos(az)).
    along = east * np.cos(az) - north * np.sin(az)
    return slant, np.broadcast_to(along, dem.shape).copy()


def _accumulate_radar_area(slant, along, area, *, slant_bin: float, along_bin: float):
    """Bin per-pixel illuminated areas into radar cells, and read the total back.

    ``area`` is each ground facet's illuminated area projected into the plane
    perpendicular to the look direction; ``slant`` / ``along`` are its radar
    coordinates. Each facet's area is spread bilinearly over the four radar cells
    it falls between (so the accumulation is a smooth partition of the total
    rather than a nearest-cell histogram), and every pixel then reads back the
    **total** area accumulated in the cell it landed in.

    That read-back is the whole point of working in image space: where terrain
    folds several ground facets into one radar cell — layover — each of them
    reads the *summed* area of all of them, which is exactly the over-brightening
    a per-pixel correction cannot see. Returns an array shaped like ``area``.
    """
    np = _require("numpy")

    slant = np.asarray(slant, dtype="float64")
    along = np.asarray(along, dtype="float64")
    area = np.asarray(area, dtype="float64")

    span_s = float(np.ptp(slant))
    span_a = float(np.ptp(along))
    cells = ((span_s / slant_bin) + 2.0) * ((span_a / along_bin) + 2.0)
    budget = _RTC_MAX_RADAR_CELLS * area.size
    if cells > budget:
        coarsen = float(np.sqrt(cells / budget))
        slant_bin *= coarsen
        along_bin *= coarsen

    si = (slant - slant.min()) / slant_bin
    ai = (along - along.min()) / along_bin
    ns = int(si.max()) + 2
    na = int(ai.max()) + 2

    s0 = np.floor(si).astype("int64")
    a0 = np.floor(ai).astype("int64")
    fs = si - s0
    fa = ai - a0

    grid = np.zeros((ns + 1, na + 1), dtype="float64")
    for ds, ws in ((0, 1.0 - fs), (1, fs)):
        for da, wa in ((0, 1.0 - fa), (1, fa)):
            np.add.at(grid, (s0 + ds, a0 + da), area * ws * wa)

    total = np.zeros_like(area)
    for ds, ws in ((0, 1.0 - fs), (1, fs)):
        for da, wa in ((0, 1.0 - fa), (1, fa)):
            total += grid[s0 + ds, a0 + da] * ws * wa
    return total


def _image_space_area_factor(
    dem: np.ndarray,
    normals,
    *,
    x_res_deg: float,
    y_res_deg: float,
    top_lat: float,
    incidence_deg: float,
    azimuth_deg: float,
    reference_deg: float,
):
    """Gamma-nought correction from an image-space illuminated-area integration.

    The other three models correct each pixel from its own slope alone, so none
    of them can see terrain *folding*: where a slope is steeper than the look
    direction, several ground facets backscatter into one radar cell and their
    returns sum, which is why layover is bright well beyond what any per-pixel
    cosine or area term predicts. This model integrates instead (Small 2011): it
    projects every facet into the radar's ``(slant_range, azimuth)`` geometry via
    :func:`_radar_coordinates`, accumulates each facet's illuminated area —
    its true tilted area ``cell / nz`` projected onto the look-perpendicular
    plane, ``* cos(local_incidence)`` — into radar cells, and normalises each
    pixel's power by the **total** area accumulated in its cell.

    The reference is the same integration run over *flat* ground in the same
    geometry, so the discretisation (and the scene edges, where a cell has fewer
    contributing facets) cancels exactly and flat terrain comes back unchanged.
    ``reference_deg`` re-references that flat value from the scene incidence by
    ``tan(scene) / tan(reference)`` — the composition of the range-compression
    and facet-area reference ratios the ``"area"`` and ``"gamma"`` models each
    apply on their own, which is what this integration reduces to over a planar
    slope.

    Gap- and shadow-safe like the other models: a facet tilted away from the
    radar (``cos_lia <= 0``, shadow) contributes no area, an empty cell is
    floored before dividing, and the factor is clamped to
    :data:`_RTC_FACTOR_MIN` .. :data:`_RTC_FACTOR_MAX`. Still a normalisation of
    *detected* amplitude — Umbra's open products are not radiometrically
    calibrated — but it is the area-integrating form rather than a per-pixel
    approximation of it.
    """
    np = _require("numpy")

    dem = np.asarray(dem, dtype="float64")
    h, _w = dem.shape
    rows = np.arange(h, dtype="float64")[:, None]
    lat = np.clip(top_lat - rows * y_res_deg, -89.9, 89.9)
    dy = y_res_deg * _M_PER_DEG_LAT
    dx = np.maximum(x_res_deg * _M_PER_DEG_LON * np.cos(np.radians(lat)), 1e-6)
    cell_area = np.broadcast_to(dx * dy, dem.shape)

    look = _look_unit_vector(incidence_deg, azimuth_deg)
    cos_lia = _cos_local_incidence(normals, look)
    nz = np.clip(np.asarray(normals[2], dtype="float64"), 1e-3, None)
    # Illuminated area of the tilted facet, projected perpendicular to the look
    # direction. A facet turned away from the radar (negative cosine) is in
    # shadow and contributes nothing.
    projected = cell_area / nz * np.clip(cos_lia, 0.0, None)

    spacing = float(np.sqrt(np.mean(cell_area)))
    sin_ref = max(float(np.sin(np.radians(reference_deg))), 1e-3)
    # One ground cell per radar cell on flat reference terrain: the ground->radar
    # map compresses area by sin(incidence), independent of the look azimuth.
    bins = {"slant_bin": spacing * sin_ref, "along_bin": spacing}

    slant, along = _radar_coordinates(
        dem,
        x_res_deg=x_res_deg,
        y_res_deg=y_res_deg,
        top_lat=top_lat,
        incidence_deg=incidence_deg,
        azimuth_deg=azimuth_deg,
    )
    accumulated = _accumulate_radar_area(slant, along, projected, **bins)

    flat = np.zeros_like(dem)
    flat_slant, flat_along = _radar_coordinates(
        flat,
        x_res_deg=x_res_deg,
        y_res_deg=y_res_deg,
        top_lat=top_lat,
        incidence_deg=incidence_deg,
        azimuth_deg=azimuth_deg,
    )
    flat_area = cell_area * float(np.cos(np.radians(incidence_deg)))
    reference = _accumulate_radar_area(flat_slant, flat_along, flat_area, **bins)

    tan_scene = np.tan(np.radians(incidence_deg))
    tan_ref = max(float(np.tan(np.radians(reference_deg))), 1e-6)
    factor = (reference / np.clip(accumulated, 1e-9, None)) * (tan_scene / tan_ref)
    factor = np.where(np.isfinite(factor), factor, 1.0)
    return np.clip(factor, _RTC_FACTOR_MIN, _RTC_FACTOR_MAX)


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
    model: str = "cosine",
) -> np.ndarray:
    """Radiometrically flatten a warped amplitude raster against a DEM.

    Resamples ``dem`` onto the output grid (``dst_transform`` / ``width`` /
    ``height``), derives the per-pixel terrain geometry from the DEM slope and the
    scene look geometry, and scales the amplitude to ``reference_deg``. Two models
    are available, both a normalisation of *detected amplitude* (not a calibrated
    gamma-nought product) and both pure-numpy at the core:

    * ``"cosine"`` (the default) — the geometric cosine correction
      ``cos(reference) / cos(local_incidence)``, using the full 3-D local
      incidence angle.
    * ``"area"`` — the projected-area / foreshortening correction
      ``sin(local_range_incidence) / sin(reference)``, using the range-plane
      incidence, so it targets the range-direction foreshortening/layover the
      per-pixel cosine model conflates with azimuth tilt.
    * ``"gamma"`` — the per-pixel facet-area (gamma-nought) normalisation
      ``cos(reference) * nz / cos(local_incidence)``, using the full 3-D facet
      normal and the true-facet-area term ``nz`` the other two omit.
    * ``"facet"`` — the image-space illuminated-area integration: every facet's
      area is accumulated into the radar cell it images into, so terrain folded
      into one cell (layover) is normalised by the *summed* area of all of it,
      which no per-pixel model can see.

    The DEM resample is the only rasterio touch; the physics is the pure-numpy
    core above. Over DEM gaps the factor is one, so those pixels pass through
    unchanged.
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
    covered = np.isfinite(dem_on_grid)  # DEM coverage on the output grid
    if model == "area":
        theta_local = _range_local_incidence(
            normals, incidence_deg=incidence_deg, azimuth_deg=azimuth_deg
        )
        sin_ref = float(np.sin(np.radians(reference_deg)))
        factor = _foreshortening_factor(theta_local, sin_ref=sin_ref)
        # Suppress the correction where the DEM had no coverage on the output grid.
        factor = np.where(covered, factor, 1.0)
    elif model == "gamma":
        look = _look_unit_vector(incidence_deg, azimuth_deg)
        cos_ref = float(np.cos(np.radians(reference_deg)))
        cos_lia = _cos_local_incidence(normals, look)
        nz = normals[2]  # up-component of the unit facet normal (cos of the slope)
        # Off-DEM pixels get a unit factor: cos_lia -> cos_ref and nz -> 1.
        cos_lia = np.where(covered, cos_lia, cos_ref)
        nz = np.where(covered, nz, 1.0)
        factor = _facet_area_factor(cos_lia, nz, cos_ref=cos_ref)
    elif model == "facet":
        factor = _image_space_area_factor(
            dem_on_grid,
            normals,
            x_res_deg=x_res,
            y_res_deg=y_res,
            top_lat=top_lat,
            incidence_deg=incidence_deg,
            azimuth_deg=azimuth_deg,
            reference_deg=reference_deg,
        )
        # A pixel the DEM did not cover has no measured facet, so it passes through.
        factor = np.where(covered, factor, 1.0)
    else:
        look = _look_unit_vector(incidence_deg, azimuth_deg)
        cos_ref = float(np.cos(np.radians(reference_deg)))
        cos_lia = _cos_local_incidence(normals, look)
        cos_lia = np.where(covered, cos_lia, cos_ref)
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
    rtc_model: str = "cosine",
    calibration: str | None = None,
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
        scale each pixel by a terrain-geometry correction (see ``rtc_model``)
        computed from the DEM slope and the scene look geometry, so slopes tilted
        toward or away from the radar no longer look artificially bright or dark.
        Requires ``dem`` (the correction needs terrain); passing it without
        ``dem`` is an error. This is a geometric normalisation of detected
        amplitude, not a calibrated gamma-nought product.
    rtc_reference_deg:
        Reference incidence angle (degrees) the flattening normalises to. ``None``
        uses the scene incidence angle, so flat terrain is left unchanged and only
        slopes are corrected.
    rtc_model:
        Which terrain-flattening model to apply when ``rtc`` is true, one of
        :data:`RTC_MODELS`:

        * ``"cosine"`` (the default) scales power by
          ``cos(reference) / cos(local_incidence)`` using the full 3-D local
          incidence angle — the standard geometric cosine correction.
        * ``"area"`` scales power by
          ``sin(local_range_incidence) / sin(reference)``, the projected-area /
          foreshortening correction. It measures incidence in the range–vertical
          plane, so it targets the range-direction foreshortening and layover that
          dominate radiometric terrain distortion — separating them from the
          azimuth-direction tilt the per-pixel cosine model folds in. It is an
          honest first-order step toward area-based gamma-nought normalisation.
        * ``"gamma"`` scales power by ``cos(reference) * nz / cos(local_incidence)``,
          the per-pixel facet-area (gamma-nought) normalisation. It normalises by
          the local illuminated facet area projected into the plane perpendicular
          to the look direction, using the full 3-D facet normal and adding the
          true tilted-facet-area term ``nz`` (cosine of the slope from horizontal)
          that the ground-referenced ``"cosine"`` and range-plane ``"area"``
          models both omit.
        * ``"facet"`` normalises each pixel by the illuminated area accumulated
          in the radar cell it images into — the image-space illuminated-area
          integration (Small 2011). Every facet is projected into the scene's
          ``(slant_range, azimuth)`` geometry and its true tilted area,
          projected perpendicular to the look direction, is binned there; a
          pixel's factor is the flat-terrain reference over the **total** area
          in its cell. The other three correct each pixel from its own slope, so
          only this one measures **layover** — where terrain folds several
          facets into one cell, their areas sum and all of them are suppressed
          together. Over a planar slope it reduces to the product of the
          ``"area"`` and ``"gamma"`` factors.

        On their own all four are a normalisation of *detected amplitude*, in
        whatever arbitrary units the product's pixels carry. Pair them with
        ``calibration=`` to make the result a physical backscatter coefficient:
        ``calibration="gamma0"`` with ``rtc_model="facet"`` is the terrain-
        flattened gamma-nought product.
    calibration:
        Optional radiometric calibration, one of :data:`CALIBRATION_TYPES`,
        applied to the detected amplitude **before** geocoding — the SICD scale
        factors are polynomials in image coordinates, so image space is where
        they are defined. It multiplies pixel *power* by the scale factor the
        product's own ``Radiometric`` metadata supplies, which is what turns a
        relative brightness into a physical quantity: ``"sigma0"`` /
        ``"beta0"`` / ``"gamma0"`` are the backscatter coefficients referenced
        to unit ground, slant-plane and perpendicular-to-look area respectively,
        and ``"rcs"`` is the absolute radar cross-section in m². With
        ``decibels=True`` the output is that coefficient in dB directly
        (``10*log10``); in linear magnitude it is the *calibrated amplitude*,
        whose square is the coefficient. Composes with ``rtc``: both are
        power-domain factors, so a calibrated *and* terrain-flattened scene is
        the two applied together.

        ``None`` (the default) leaves the output uncalibrated, which is what
        Umbra's open products generally require — they usually ship without a
        ``Radiometric`` block, and asking for a calibration the metadata cannot
        support is a self-describing error rather than a plausible-looking
        number. :func:`sicd_calibration_types` reports what a given file
        supports. MultiRTC interop remains deferred (`STRATEGY.md` 5.5).
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
    if rtc and rtc_model not in RTC_MODELS:
        raise ValueError(f"Unknown rtc_model {rtc_model!r}; choose one of {', '.join(RTC_MODELS)}.")
    if calibration is not None and calibration not in CALIBRATION_TYPES:
        raise ValueError(
            f"Unknown calibration {calibration!r}; choose one of {', '.join(CALIBRATION_TYPES)}."
        )

    reader = open_complex(str(src))
    sicd = reader.get_sicds_as_tuple()[0]
    amplitude = _amplitude(reader[:, :], decibels=decibels)
    if calibration is not None:
        # In image space: the SF polynomials are functions of image coordinates,
        # so calibrate before the warp resamples the grid away.
        amplitude = _calibrate_amplitude(sicd, amplitude, kind=calibration, decibels=decibels)
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
                model=rtc_model,
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
