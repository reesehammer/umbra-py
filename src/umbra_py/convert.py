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

A calibrated pixel is still the sum of the ground's echo *and* the receiver's
own thermal noise, and over a dark surface — calm water, radar shadow, dry sand
— the second term is most of it. Scaling that sum by a calibration factor gives
a number that is precise, physical and wrong: it reports the noise floor as
backscatter, and because the floor varies across the swath it puts a gradient in
the answer that has nothing to do with the scene. ``noise_subtract=True``
(``umbra convert --subtract-noise``) removes it, in the power domain where noise
adds, using the product's own ``Radiometric.NoiseLevel`` polynomial — so a low-
backscatter surface reads as the low number it is rather than as the sensor's
sensitivity. It is subtractive where calibration and flattening are
multiplicative, so it goes *first*, on the raw detected power, before either
scales it. Only an ``ABSOLUTE`` noise level can be subtracted: a ``RELATIVE``
one describes how the floor varies without saying what it *is*, so asking to
subtract it is a self-describing error rather than an arbitrary offset.
:func:`sicd_noise_level` reports which kind (if any) a file carries.

That measured floor is the better number, and most Umbra open products do not
carry it — they generally ship without a ``Radiometric`` block at all, so the
correction refused on exactly the archive this library exists for.
``noise_model="estimated"`` (``umbra convert --noise-model estimated``) infers
the floor from the scene instead: a SAR image's darkest surfaces return
essentially nothing, so the low tail of its own power distribution *is* the
receiver. It needs no metadata and so works on the open archive; in exchange it
is one constant rather than a polynomial across the swath, and it assumes the
scene contains dark ground to read at all. Because it is an inference and not a
measurement it says so — ``UMBRA_NOISE_SUBTRACTION`` records ``"estimated"``
rather than ``"absolute"``, with the inferred level in
``UMBRA_NOISE_FLOOR_DB`` — and :func:`umbra_py.load.to_stack` refuses to
difference a series that mixes the two.

The first of those exchanges — one constant where the measured floor is a
polynomial — is the one that shows up *in the picture*: a receiver's sensitivity
varies with range, so a scalar taken off the whole scene under-subtracts at one
edge of the swath and over-subtracts at the other, leaving exactly the gradient
the correction exists to remove. ``noise_model="estimated-range"`` (``umbra
convert --noise-model estimated-range``) infers a floor that *follows* range
instead: SICD stores range along the image rows, so it reads the low tail of
each range line separately and fits those per-line floors against range
(:func:`_estimate_noise_profile`). The fit is what makes it usable — it
interpolates across the lines that had no dark ground to read, and it discards
the lines whose tail sits far *above* it, since ground contamination can only
push a line's low tail up. It is still an inference from the pixels, so it is
recorded as its own third thing (``"estimated-range"``) rather than quietly
changing what ``"estimated"`` means, and it reports the swing it found in
``UMBRA_NOISE_FLOOR_SPREAD_DB`` — the number that says whether there was any
across-swath variation for the constant model to have missed.

Those two exchanges were documented; they were not *reported*, so on any given
scene there was no way to tell whether they had bitten. Every subtraction now
also records what it did to the image (:class:`NoiseSubtraction`):
``UMBRA_NOISE_FLOORED_FRACTION``, how much of the raster the floor drove to the
sensor's sensitivity limit, and — for the inferred floors —
``UMBRA_NOISE_FLOOR_MARGIN_DB``, how far the scene's own median power sat above
the inferred floor. The second is the estimator's assumption made checkable: it
works because a SAR scene's dark surfaces are a *different population* from its
ordinary backscatter, so a wide margin is the evidence that they were, and a
narrow one says this scene was bright everywhere and the fifth percentile it
subtracted was ground. ``umbra convert`` prints both and says so below
:data:`NOISE_MARGIN_WARN_DB`. It stays an advisory rather than a refusal — a
uniform scene is legitimate, and the honest answer there is a measured floor,
not a different guess.

Every correction above targets something the *sensor* added — a geometric
brightness swing, an arbitrary scale, a thermal floor. What is left after all of
them is not an error at all: coherent illumination of a rough surface interferes
with itself, so a single-look pixel's power is exponentially distributed about
the surface's true backscatter, with a standard deviation equal to its mean. That
is speckle, it is the dominant uncertainty in every one of those calibrated
numbers, and averaging is the only thing that reduces it.
``speckle_filter=`` (``umbra convert --speckle-filter``) does the averaging, in
the power domain, last in image space: ``"boxcar"`` averages the window
unconditionally — the multilook — and ``"lee"`` averages only where the window is
no more variable than speckle alone would explain, keeping edges and points
(Lee 1980). The trade is explicit and is why no filter is the default: what is
bought is measurement precision, and what is spent is resolution — a window that
averages ``N`` pixels reports ground ``N`` pixels across, and 25 cm resolution is
the reason to use this archive. So the filter and its window are recorded
(``UMBRA_SPECKLE_FILTER`` / ``UMBRA_SPECKLE_WINDOW``, both refused-on-mix by
:func:`umbra_py.load.to_stack`, since averaging one pass and not another shows up
as change), and so is what the filter actually *achieved*: the equivalent number
of looks before and after (:func:`_estimate_enl`), which on an oversampled
product is well below the window's pixel count and is the number that says so.

All of that work is proportional to the scene, and a scene is tens of square
kilometres at 16–25 cm. ``bbox=`` on :func:`sicd_to_geocoded_cog` (``umbra
convert --clip-bbox``) makes it proportional to the *area of interest* instead:
the ground rectangle is turned back into the image window that covers it, only
that window is read from the product, and the geocoded output is cropped to the
rectangle. Nothing about the result changes — the pixels are the same pixels the
whole-scene conversion would have produced there — only how much of the scene
had to be detected, calibrated, projected, warped and written.

Every raster written here records *how* it was made — the calibration, the
terrain-flattening model and its reference angle, the DEM/geoid, the projection,
the scale, and the data licence — as namespaced GeoTIFF metadata
(:func:`conversion_tags`, read back with :func:`read_conversion_tags`, ``umbra
convert --provenance``, or plain ``gdalinfo``). Without it two scenes converted
with different settings are pixel-for-pixel indistinguishable after the fact,
and a physical measurement nobody can attribute to a calibration is not one.

Install with: ``pip install "umbra-py[convert]"``
"""

from __future__ import annotations

import math
import os
import warnings
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict

from . import __version__
from .constants import ATTRIBUTION, DATA_LICENSE
from .exceptions import MissingDependencyError, UnsupportedMeasurementError

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
    is testable without sarpy). Raises a self-describing
    :class:`~umbra_py.exceptions.UnsupportedMeasurementError` when the product
    cannot support the requested calibration — the whole point of the feature is
    that an uncalibrated product says so rather than emitting a number that
    looks calibrated. An unknown *calibration name* stays a bare
    :class:`ValueError`: that is a fact about the request, not about the file.

    Every metadata check here runs before ``numpy`` is required, so "this
    product cannot be calibrated" is answerable without the ``convert`` extra.
    """
    if kind not in CALIBRATION_TYPES:
        raise ValueError(
            f"Unknown calibration {kind!r}; choose one of {', '.join(CALIBRATION_TYPES)}."
        )
    radiometric = getattr(sicd, "Radiometric", None)
    if radiometric is None:
        raise UnsupportedMeasurementError(
            "SICD carries no Radiometric metadata, so it cannot be radiometrically "
            "calibrated: the scale factors that turn detected power into a "
            "backscatter coefficient have to come from the product. Umbra's open "
            "products are typically uncalibrated -- convert without calibration for "
            "the usual (relative) amplitude image.",
            hint="Convert without --calibrate, or use a product whose Radiometric "
            "block states its scale factors.",
        )
    poly = getattr(radiometric, _CALIBRATION_POLYS[kind], None)
    if poly is None:
        available = _available_calibrations(sicd)
        offer = ", ".join(available) if available else "none"
        raise UnsupportedMeasurementError(
            f"SICD Radiometric metadata carries no {_CALIBRATION_POLYS[kind]}, so "
            f"{kind} calibration is unavailable for this product "
            f"(available: {offer}).",
            hint=(
                f"Ask for one of: {offer}."
                if available
                else "Convert without --calibrate; this product states no scale factors."
            ),
        )
    np = _require("numpy")
    coefs = np.atleast_2d(np.asarray(getattr(poly, "Coefs", poly), dtype="float64"))
    if coefs.size == 0:
        raise UnsupportedMeasurementError(
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
        raise UnsupportedMeasurementError(
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


def _image_poly_values(
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
    """Evaluate a SICD image-coordinate polynomial over a pixel grid.

    The ``Radiometric`` polynomials — the scale factors *and* the noise level —
    are all functions of image coordinates measured in **metres from the scene
    centre point**: pixel ``(row, col)`` sits at ``((row + first_row - scp_row)
    * row_ss, (col + first_col - scp_col) * col_ss)``. This is that shared
    evaluation; what the numbers *mean* (a power ratio, a noise power in dB) and
    which values are admissible is the caller's business.
    """
    np = _require("numpy")
    from numpy.polynomial import polynomial as npoly  # noqa: PLC0415

    rows, cols = shape
    x = (np.arange(rows, dtype="float64") + first_row - scp_row) * row_ss
    y = (np.arange(cols, dtype="float64") + first_col - scp_col) * col_ss
    xx, yy = np.meshgrid(x, y, indexing="ij")
    return np.asarray(
        npoly.polyval2d(xx, yy, np.atleast_2d(np.asarray(coefs, dtype="float64"))),
        dtype="float64",
    )


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
    (:func:`_image_poly_values`), so a constant polynomial (the common case)
    gives a flat scale and a higher-order one tracks the across-swath variation
    the product describes.

    A scale factor is a positive power ratio by construction; a non-positive or
    non-finite value means the metadata cannot be evaluated on this grid, which
    is raised rather than clamped, because a silently repaired calibration is
    worse than none.
    """
    np = _require("numpy")

    scale = _image_poly_values(
        coefs,
        shape,
        row_ss=row_ss,
        col_ss=col_ss,
        scp_row=scp_row,
        scp_col=scp_col,
        first_row=first_row,
        first_col=first_col,
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


def _calibrate_amplitude(
    sicd: Any,
    amplitude: np.ndarray,
    *,
    kind: str,
    decibels: bool,
    origin: tuple[int, int] = (0, 0),
):
    """Calibrate a detected-amplitude raster against the SICD's own metadata.

    ``origin`` is the ``(row, col)`` position of ``amplitude`` inside the full
    image. The scale-factor polynomials are functions of image coordinates, so a
    clipped read has to be evaluated at the coordinates it actually came from —
    the same correction ``ImageData.FirstRow`` / ``FirstCol`` already make for a
    chipped *product*, applied to a chip this library cut itself.
    """
    coefs = _calibration_coefficients(sicd, kind)
    geometry = _image_grid_geometry(sicd)
    row0, col0 = origin
    geometry["first_row"] += float(row0)
    geometry["first_col"] += float(col0)
    scale = _calibration_scale(coefs, amplitude.shape, **geometry)
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


# --------------------------------------------------------------------------- #
# Noise-floor subtraction (SICD ``Radiometric.NoiseLevel``).
# --------------------------------------------------------------------------- #

#: The only SICD ``NoiseLevelType`` that can be subtracted. ``"RELATIVE"``
#: describes how the noise floor *varies* across the image without stating its
#: absolute level, so subtracting it would mean inventing the offset.
_SUBTRACTABLE_NOISE_LEVEL = "ABSOLUTE"

#: Where the noise floor subtracted by ``noise_subtract=True`` comes from, for
#: ``noise_model=`` on :func:`sicd_to_geocoded_cog` /
#: :func:`sicd_to_amplitude_geotiff` (``umbra convert --noise-model``):
#:
#: * ``"measured"`` (the default, and the only behaviour before it had a name)
#:   reads the product's own ``Radiometric.NoiseLevel.NoisePoly`` — the floor as
#:   the sensor's own metadata states it, per pixel, tracking the across-swath
#:   variation. It is a measurement, and it raises when the product has none.
#: * ``"estimated"`` derives one constant floor from the scene's own detected
#:   power (see :func:`_estimate_noise_power`). It needs no metadata, which is
#:   the point: Umbra's open products generally ship without a ``Radiometric``
#:   block at all, so ``"measured"`` refuses on exactly the archive this library
#:   exists for. It is an *inference* from the image and is recorded as one.
#: * ``"estimated-range"`` infers a floor that varies across the swath, by
#:   reading the same low tail *per range line* and fitting those floors against
#:   range (see :func:`_estimate_noise_profile`). Same input as ``"estimated"``
#:   — the pixels, no metadata — and the same assumption, applied line by line
#:   rather than once; what it buys is the across-swath variation a single
#:   scalar cannot represent, which is the one difference from the measured
#:   floor that shows up as a gradient in the output.
NOISE_MODELS = ("measured", "estimated", "estimated-range")

#: Percentile of the scene's own detected power taken as the noise floor by
#: ``noise_model="estimated"`` (and, per range line, by ``"estimated-range"``).
#: Low enough that ordinary land backscatter sits well above it, high enough not
#: to land in the speckle tail of the darkest pixels — the floor wanted is the
#: *mean* power of the noise-dominated population, not its minimum.
NOISE_ESTIMATE_PERCENTILE = 5.0

#: Degree of the polynomial ``noise_model="estimated-range"`` fits to the
#: per-range-line floors. Quadratic because that is the shape a receiver's
#: sensitivity actually has across a swath — a smooth roll-off from the antenna
#: elevation pattern and range spreading, not a step — and because a low degree
#: is what makes the fit an *interpolator* over the lines with no dark ground
#: rather than a curve that chases each line's speckle.
NOISE_PROFILE_DEGREE = 2

#: Finite pixels a range line needs before its percentile is believed. A line
#: mostly outside the collect (or mostly nodata after a clip) has too few samples
#: for a low-tail percentile to mean anything; it is dropped and the fit covers
#: it, which is the whole reason the profile is a fit rather than a lookup.
_NOISE_PROFILE_MIN_SAMPLES = 16

#: Decibels above the fitted profile beyond which a range line is treated as
#: contaminated and dropped before the fit is redone. The trim is deliberately
#: **one-sided**: bright ground can only push a line's low tail *up* (a line with
#: no dark surfaces reports its dimmest backscatter, not the receiver), so a line
#: far below the fit is noise-only and informative while one far above it is a
#: line the estimator could not read.
_NOISE_PROFILE_TRIM_DB = 3.0

#: The :func:`conversion_tags` ``NOISE_SUBTRACTION`` value each model records.
#: ``"measured"`` keeps the ``"absolute"`` it has always written (the SICD level
#: type it read), so rasters converted before ``noise_model=`` existed still
#: compare equal to ones converted after it. ``"estimated"`` is deliberately a
#: *different* value: it is in :data:`umbra_py.load.MEASUREMENT_PROVENANCE_KEYS`,
#: so a series that mixes a measured floor with an inferred one is refused by
#: :func:`umbra_py.load.to_stack` rather than differenced.
_NOISE_PROVENANCE = {
    "measured": _SUBTRACTABLE_NOISE_LEVEL.lower(),
    "estimated": "estimated",
    "estimated-range": "estimated-range",
}

#: Power floor left where the estimated noise meets or exceeds the measured
#: power. Matches the ``1e-6`` magnitude floor :func:`_amplitude` already clamps
#: the log scale at (power is magnitude squared), so a pixel driven to the floor
#: by noise subtraction reads as the same "as dark as this raster goes" value a
#: zero-amplitude pixel does, rather than as ``-inf``.
_NOISE_RESIDUAL_FLOOR = 1e-12

#: Below this separation between the *estimated* floor and the scene's own median
#: power, ``umbra convert`` says the scene had little dark ground to read (see
#: :attr:`NoiseSubtraction.margin_db`). It is an advisory threshold, never a
#: refusal: "how bimodal is this scene?" is a heuristic, and a scene can be
#: legitimately uniform. 6 dB is a factor of four in power — enough that the low
#: tail is a different population from typical backscatter rather than its lower
#: shoulder, and low enough that ordinary single-surface scenes clear it.
NOISE_MARGIN_WARN_DB = 6.0


@dataclass(frozen=True)
class NoiseSubtraction:
    """What one noise-floor subtraction did, beyond changing the pixels.

    :func:`_denoise_amplitude` computes all three of these on its way through the
    array and, before this existed, threw them away — which left the correction's
    two documented failure modes invisible in the one place they matter, the
    output. They are diagnostics of *this* raster, not statements about what a
    pixel value means, so they are recorded (see :func:`conversion_tags`) but are
    deliberately **not** in :data:`umbra_py.load.MEASUREMENT_PROVENANCE_KEYS`: a
    stack of passes that were floored to different degrees is still a stack of
    comparable measurements.

    Attributes
    ----------
    floored_fraction:
        Fraction of the raster's finite pixels the subtraction drove to
        :data:`_NOISE_RESIDUAL_FLOOR` — where the floor met or exceeded the
        measured power. That is exactly "how much of this image is at the
        sensor's sensitivity limit", a fact about the radar rather than the
        ground, and a large value is the tell that a scene is being reported
        mostly as its own noise. Counted in image space over the window actually
        read, so a ``bbox=`` clip reports its own window rather than the scene.
    floor_db:
        The floor subtracted, in decibels, for the inferred models — the single
        constant for ``"estimated"``, and the *median* of the fitted profile for
        ``"estimated-range"``, which is the one level that stands for a floor
        varying across the swath (its swing is ``floor_spread_db``). ``None`` for
        the measured model, which is a polynomial the product states rather than
        a number this module inferred, and so is reproducible from the file.
    margin_db:
        How far the scene's own *median* power sits above that estimated floor.
        The estimator assumes the scene contains a noise-dominated population to
        read; when it does, the median is far above the fifth percentile, and
        when it doesn't — uniformly bright imagery, where the fifth percentile is
        ground — the two collapse together and the subtraction removes real
        backscatter. This number is that distance, so the assumption is reported
        rather than merely documented (:data:`NOISE_MARGIN_WARN_DB`). ``None``
        for the measured model, which assumes nothing about the scene.
    floor_spread_db:
        Peak-to-peak swing of the fitted floor across range, for
        ``"estimated-range"`` only. It is the answer to "was there anything here
        for the constant model to have missed?" — near zero and the two inferred
        models agree, wide and the scalar was leaving a gradient behind. ``None``
        for the constant estimate (whose spread is zero by construction, so
        recording it would say nothing) and for the measured floor (whose
        variation is the product's own metadata, readable from the SICD).
    """

    floored_fraction: float
    floor_db: float | None = None
    margin_db: float | None = None
    floor_spread_db: float | None = None


def _noise_level_type(sicd: Any) -> str | None:
    """The SICD's ``Radiometric.NoiseLevel.NoiseLevelType``, upper-cased.

    ``None`` when the product carries no noise-level metadata at all — the
    honest answer for a product whose floor is undescribed, and the one
    :func:`sicd_noise_level` hands back.
    """
    radiometric = getattr(sicd, "Radiometric", None)
    level = getattr(radiometric, "NoiseLevel", None) if radiometric is not None else None
    kind = getattr(level, "NoiseLevelType", None) if level is not None else None
    return str(kind).upper() if kind else None


def _noise_coefficients(sicd: Any):
    """The 2-D ``NoisePoly`` coefficients (noise power in dB), off a SICD.

    Raises a self-describing
    :class:`~umbra_py.exceptions.UnsupportedMeasurementError` for each way a
    product can fail to support the subtraction — no ``Radiometric`` block, no
    ``NoiseLevel``, a ``RELATIVE`` level, or an empty polynomial. The point of the feature is that
    a noise floor is either measured or absent, never assumed: a subtraction of
    a guessed floor is indistinguishable from a real one in the output and would
    make dark surfaces confidently wrong.

    Every metadata check runs before ``numpy`` is required, so "this product's
    noise floor cannot be subtracted" is answerable without the ``convert``
    extra.
    """
    radiometric = getattr(sicd, "Radiometric", None)
    if radiometric is None:
        raise UnsupportedMeasurementError(
            "SICD carries no Radiometric metadata, so its noise floor cannot be "
            "subtracted: the noise level has to come from the product. Umbra's open "
            "products are typically uncalibrated -- convert without --subtract-noise "
            "for the usual (noise-inclusive) amplitude image.",
            hint="Use --noise-model estimated (or estimated-range) to infer the floor "
            "from the scene's own dark ground, which needs no metadata.",
        )
    level = getattr(radiometric, "NoiseLevel", None)
    if level is None:
        raise UnsupportedMeasurementError(
            "SICD Radiometric metadata carries no NoiseLevel block, so there is no "
            "noise floor to subtract for this product.",
            hint="Use --noise-model estimated (or estimated-range) to infer the floor "
            "from the scene itself.",
        )
    kind = _noise_level_type(sicd)
    if kind != _SUBTRACTABLE_NOISE_LEVEL:
        raise UnsupportedMeasurementError(
            f"SICD NoiseLevelType is {kind or 'unset'!r}, not "
            f"{_SUBTRACTABLE_NOISE_LEVEL!r}: a relative noise level describes how the "
            "floor varies across the image without stating what it is, so it cannot "
            "be subtracted from pixel power without inventing the absolute offset.",
            hint="Use --noise-model estimated-range, which infers a floor that follows "
            "the swath from the scene's own pixels.",
        )
    poly = getattr(level, "NoisePoly", None)
    if poly is None:
        raise UnsupportedMeasurementError(
            "SICD NoiseLevel carries no NoisePoly, so the noise power is undefined "
            "even though the level is declared absolute."
        )
    np = _require("numpy")
    coefs = np.atleast_2d(np.asarray(getattr(poly, "Coefs", poly), dtype="float64"))
    if coefs.size == 0:
        raise UnsupportedMeasurementError(
            "SICD NoisePoly has no coefficients, so the noise power is undefined."
        )
    return coefs


def _noise_power(
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
    """Per-pixel noise power from a SICD ``NoisePoly``.

    ``NoisePoly`` yields the thermal-noise power **in decibels** as a function
    of the same image coordinates the scale factors use, so this evaluates it
    there (:func:`_image_poly_values`) and converts to the linear power domain
    where noise and signal add. A constant polynomial (the common case) gives a
    flat floor; a higher-order one tracks the across-swath variation that is
    exactly what a single scalar floor would smear into the result.

    A non-finite value means the metadata cannot be evaluated on this grid,
    which is raised rather than clamped — the same rule as the scale factors.
    """
    np = _require("numpy")

    noise_db = _image_poly_values(
        coefs,
        shape,
        row_ss=row_ss,
        col_ss=col_ss,
        scp_row=scp_row,
        scp_col=scp_col,
        first_row=first_row,
        first_col=first_col,
    )
    bad = ~np.isfinite(noise_db)
    if bool(bad.any()):
        raise UnsupportedMeasurementError(
            f"The SICD noise polynomial is non-finite over {int(bad.sum())} of "
            f"{bad.size} pixels, so it cannot be evaluated on this image grid. The "
            "product's NoiseLevel metadata is inconsistent with its image geometry."
        )
    return np.power(10.0, noise_db / 10.0)


def _check_percentile(percentile: float) -> None:
    """Reject a noise-estimate percentile that is not inside the distribution.

    Shared by the two estimators so "which tail?" fails the same way whether the
    caller asked for one floor or one per range line.
    """
    if not 0.0 < float(percentile) < 100.0:
        raise ValueError(
            f"Noise-estimate percentile must be between 0 and 100 (exclusive), got "
            f"{percentile!r}. It selects the low tail of the scene's own power "
            "distribution, so 0 is the darkest single pixel and 100 the brightest."
        )


def _detected_power(amplitude: np.ndarray, *, decibels: bool):
    """A detected-amplitude raster as linear power, whichever scale it arrived in.

    The module carries amplitude in two conventions -- decibel power
    (``20*log10`` of magnitude, which *is* ``10*log10`` of power) or linear
    magnitude -- and every noise operation happens in the power domain, because
    that is the domain noise adds in. One place for the conversion so the three
    functions that need it cannot drift on which log it undoes.
    """
    np = _require("numpy")

    values = np.asarray(amplitude, dtype="float64")
    return np.power(10.0, values / 10.0) if decibels else np.square(values)


def _subtract_noise(amplitude: np.ndarray, noise: Any, *, decibels: bool):
    """Remove an additive noise power from a detected-amplitude raster.

    Noise adds to signal in *power*, which is the one place in this module the
    correction is not a multiplicative factor: the flattening and the
    calibration both scale power, so they commute with each other and with the
    warp, while this one has to happen on the raw detected power before either
    scales it.

    ``amplitude`` is the module's usual pair of conventions — decibel power
    (``20*log10`` of magnitude, which *is* ``10*log10`` of power) or linear
    magnitude — and comes back in whichever it arrived in. Where the estimated
    noise meets or exceeds the measured power the residual is floored at
    :data:`_NOISE_RESIDUAL_FLOOR` rather than driven to zero or negative: those
    pixels are at the sensor's sensitivity limit, which is a statement about the
    radar and not a measurement of the ground. Non-finite pixels (the warp's
    nodata) stay non-finite.

    Returns the corrected raster and the fraction of its finite pixels that
    landed on that floor — the "how much of this image is the sensor rather than
    the scene?" number, which is free here (the comparison is already being made)
    and unrecoverable afterwards, since a floored pixel and a genuinely
    floor-valued one are the same value in the output.
    """
    np = _require("numpy")

    power = _detected_power(amplitude, decibels=decibels)
    residual = power - np.asarray(noise, dtype="float64")
    finite = np.isfinite(residual)
    finite_count = int(finite.sum())
    floored = int(np.logical_and(finite, residual <= _NOISE_RESIDUAL_FLOOR).sum())
    residual = np.clip(residual, _NOISE_RESIDUAL_FLOOR, None)
    with np.errstate(divide="ignore", invalid="ignore"):
        corrected = 10.0 * np.log10(residual) if decibels else np.sqrt(residual)
    return (
        np.asarray(corrected, dtype="float32"),
        floored / finite_count if finite_count else 0.0,
    )


def _estimate_noise_power(
    amplitude: np.ndarray,
    *,
    decibels: bool,
    percentile: float = NOISE_ESTIMATE_PERCENTILE,
) -> tuple[float, float]:
    """Infer a constant noise floor from a detected-amplitude raster itself.

    A SAR scene almost always contains surfaces that return essentially nothing
    — calm water, radar shadow, a smooth road — and what the radar records there
    is its own receiver noise. So the low tail of the scene's power distribution
    *is* the noise floor, and its ``percentile``-th percentile is a robust read
    of it: robust to a scene with only a little dark ground (raise the
    percentile and it starts eating real backscatter; lower it and speckle in
    the noise-only population drags it under the true level).

    This is the one number in this module that is inferred from the pixels
    rather than read from the metadata, and the difference is not cosmetic. The
    measured floor (:func:`_noise_power`) is a polynomial in image coordinates,
    so it follows the across-swath variation; this is a single scalar, so it
    cannot. And it assumes the scene *has* a noise-dominated population: over
    imagery that is bright everywhere — dense city, forest at high incidence —
    the fifth percentile is ground, and subtracting it removes real signal.
    Both facts are why it records itself as ``"estimated"`` rather than sharing
    a provenance value with the measured floor (see :data:`_NOISE_PROVENANCE`).

    Returns the floor **and the scene's median power**, both as linear power in
    the same arbitrary units the product's pixels carry: the first for
    :func:`_subtract_noise` to take off, the second because the distance between
    them is the only evidence the estimator leaves about whether its assumption
    held (see :attr:`NoiseSubtraction.margin_db`). The median comes from the same
    already-computed, already-filtered array, so asking for it costs one more
    pass over the finite values rather than a second conversion of the scene.
    """
    np = _require("numpy")

    _check_percentile(percentile)
    power = _detected_power(amplitude, decibels=decibels)
    finite = power[np.isfinite(power)]
    if finite.size == 0:
        raise ValueError(
            "Cannot estimate a noise floor from this image: no pixel carries a "
            "finite value, so the scene's power distribution is empty."
        )
    return float(np.percentile(finite, float(percentile))), float(np.median(finite))


def _estimate_noise_profile(
    amplitude: np.ndarray,
    *,
    decibels: bool,
    percentile: float = NOISE_ESTIMATE_PERCENTILE,
    degree: int = NOISE_PROFILE_DEGREE,
):
    """Infer a *range-varying* noise floor from a detected-amplitude raster.

    :func:`_estimate_noise_power` reads the low tail of the whole scene and gets
    one number for it. A receiver's sensitivity is not one number: it varies with
    range, which is why the measured floor (:func:`_noise_power`) is a polynomial
    and why subtracting a scalar leaves an across-swath gradient behind — the
    very artefact the correction exists to remove. This reads the same low tail
    **per range line** instead. SICD stores range along the image rows
    (``Grid.Row`` is the range direction, ``Grid.Col`` azimuth), so a row is a
    set of azimuth samples at one range and its own percentile is the receiver at
    that range.

    A per-line percentile alone would be unusable: a line crossing nothing but
    city has no dark ground, so its low tail is dim *backscatter* and sits above
    the floor, and a line that is mostly nodata has too few samples to take a
    percentile of at all. Both are handled by making the profile a **fit** rather
    than a lookup — a degree-``degree`` polynomial in the row coordinate:

    * lines with fewer than :data:`_NOISE_PROFILE_MIN_SAMPLES` finite pixels are
      dropped, and the fit covers them by interpolation;
    * the fit is then redone without the lines sitting more than
      :data:`_NOISE_PROFILE_TRIM_DB` **above** it. That trim is one-sided on
      purpose: contamination can only raise a line's low tail, never lower it, so
      a line far above the curve is one the estimator could not read while a line
      far below it is noise-only and is exactly what should be believed.

    The fit runs in decibels, where the measured ``NoisePoly`` is also written
    and where the roll-off is close to polynomial, and the result is converted to
    linear power for :func:`_subtract_noise` — which broadcasts a column vector
    across the azimuth samples of each line.

    What this adds over :func:`_estimate_noise_power` is the *shape*, not the
    level. A percentile of a speckled noise-only population sits below that
    population's mean power by an amount set by the percentile, so both
    estimators read a floor that is conservatively low — and because that bias is
    very nearly the same decibel offset on every line, it moves the whole fitted
    curve down without bending it. Under-subtraction is the safe direction (it
    leaves a little of the receiver in rather than taking real backscatter out),
    and it is the *gradient*, not the offset, that a constant floor puts into a
    scene and that this removes.

    Returns the profile as an ``(rows, 1)`` power array, the scene's median power
    (for :attr:`NoiseSubtraction.margin_db`, as above), the profile's median
    level in decibels (the one number that stands for it) and its peak-to-peak
    swing in decibels — the last being the evidence for whether this model was
    worth using over the constant one at all.

    Raises when too few range lines qualify to fit a curve through, naming
    ``"estimated"`` as the model that needs only one.
    """
    np = _require("numpy")

    _check_percentile(percentile)
    degree = int(degree)
    if degree < 0:
        raise ValueError(f"Noise-profile degree must be non-negative, got {degree!r}.")
    power = _detected_power(amplitude, decibels=decibels)
    if power.ndim != 2:
        raise ValueError(
            f"A range-varying noise floor needs a 2-D image to fit across, got shape "
            f"{power.shape!r}."
        )
    finite = np.isfinite(power)
    if not bool(finite.any()):
        raise ValueError(
            "Cannot estimate a noise floor from this image: no pixel carries a "
            "finite value, so the scene's power distribution is empty."
        )
    rows = power.shape[0]

    # One low-tail read per range line, over that line's finite samples only.
    masked = np.where(finite, power, np.nan)
    with warnings.catch_warnings():
        # An all-nodata line is expected here, not exceptional -- it is dropped
        # below and the fit covers it.
        warnings.simplefilter("ignore", category=RuntimeWarning)
        line_floor = np.nanpercentile(masked, float(percentile), axis=1)
    usable = (
        np.isfinite(line_floor)
        & (line_floor > 0.0)
        & (finite.sum(axis=1) >= _NOISE_PROFILE_MIN_SAMPLES)
    )
    if int(usable.sum()) < degree + 1:
        raise ValueError(
            f"Only {int(usable.sum())} of {rows} range lines carry enough dark ground to "
            f"read a noise floor from, which is too few to fit a degree-{degree} profile "
            f"through (it needs {degree + 1}). Use noise_model='estimated' for one "
            "constant floor over the whole scene, or noise_model='measured' where the "
            "product states its own."
        )

    # Row index normalised to [-1, 1] so the fit is conditioned the same way for
    # a 500-line clip as for a 50 000-line collect.
    x = np.linspace(-1.0, 1.0, rows) if rows > 1 else np.zeros(1)
    y = 10.0 * np.log10(line_floor[usable])
    coefs = np.polyfit(x[usable], y, degree)
    keep = (y - np.polyval(coefs, x[usable])) <= _NOISE_PROFILE_TRIM_DB
    if not bool(keep.all()) and int(keep.sum()) >= degree + 1:
        coefs = np.polyfit(x[usable][keep], y[keep], degree)

    profile_db = np.polyval(coefs, x)
    profile = np.power(10.0, profile_db / 10.0).reshape(rows, 1)
    return (
        profile,
        float(np.median(power[finite])),
        float(np.median(profile_db)),
        float(profile_db.max() - profile_db.min()),
    )


def _denoise_amplitude(
    sicd: Any,
    amplitude: np.ndarray,
    *,
    decibels: bool,
    origin: tuple[int, int] = (0, 0),
    model: str = "measured",
    percentile: float = NOISE_ESTIMATE_PERCENTILE,
) -> tuple[np.ndarray, NoiseSubtraction]:
    """Subtract a noise floor from a detected-amplitude raster.

    ``model`` picks where the floor comes from, one of :data:`NOISE_MODELS`:
    ``"measured"`` reads the SICD's own ``Radiometric.NoiseLevel`` polynomial,
    ``"estimated"`` infers a constant one from the image
    (:func:`_estimate_noise_power`), and ``"estimated-range"`` infers one that
    varies across the swath (:func:`_estimate_noise_profile`). The subtraction
    itself is the same in every case — :func:`_subtract_noise`, in the power
    domain, before anything scales it — because what differs is the provenance of
    the number, not the physics.

    ``origin`` is the ``(row, col)`` position of ``amplitude`` inside the full
    image, for the same reason :func:`_calibrate_amplitude` takes one: the noise
    polynomial is a function of image coordinates, so a clipped read has to be
    evaluated at the coordinates it actually came from. The estimated models have
    no image-coordinate dependence and so ignore it — a clip's floor is inferred
    from the clip, which is the only ground it can see, and the range profile is
    fitted over the clip's own rows.

    Returns the corrected raster and a :class:`NoiseSubtraction` describing what
    the correction did to it: always how much of the image it drove to the
    sensor's limit, and for the inferred floors the level plus how far the
    scene's median sat above it (and, for the range profile, its swing).
    """
    if model not in NOISE_MODELS:
        raise ValueError(f"Unknown noise_model {model!r}; choose one of {', '.join(NOISE_MODELS)}.")
    if model == "estimated-range":
        profile, median, level_db, spread_db = _estimate_noise_profile(
            amplitude, decibels=decibels, percentile=percentile
        )
        corrected, floored = _subtract_noise(amplitude, profile, decibels=decibels)
        return corrected, NoiseSubtraction(
            floored_fraction=floored,
            floor_db=level_db,
            margin_db=_margin_db(median, 10.0 ** (level_db / 10.0)),
            floor_spread_db=spread_db,
        )
    if model == "estimated":
        floor, median = _estimate_noise_power(amplitude, decibels=decibels, percentile=percentile)
        corrected, floored = _subtract_noise(amplitude, floor, decibels=decibels)
        return corrected, NoiseSubtraction(
            floored_fraction=floored,
            floor_db=10.0 * math.log10(floor) if floor > 0.0 else float("-inf"),
            margin_db=_margin_db(median, floor),
        )

    coefs = _noise_coefficients(sicd)
    geometry = _image_grid_geometry(sicd)
    row0, col0 = origin
    geometry["first_row"] += float(row0)
    geometry["first_col"] += float(col0)
    noise = _noise_power(coefs, amplitude.shape, **geometry)
    corrected, floored = _subtract_noise(amplitude, noise, decibels=decibels)
    return corrected, NoiseSubtraction(floored_fraction=floored)


class _NoiseTagValues(TypedDict):
    """The :func:`conversion_tags` keywords a :class:`NoiseSubtraction` fills in."""

    noise_floor_db: float | None
    noise_floored_fraction: float | None
    noise_floor_margin_db: float | None
    noise_floor_spread_db: float | None


def _noise_tag_values(noise: NoiseSubtraction | None) -> _NoiseTagValues:
    """A :class:`NoiseSubtraction` as :func:`conversion_tags` keyword arguments.

    One place for the mapping so both writers (slant-plane and geocoded) record
    the same numbers, and so a raster written without the subtraction passes
    ``None`` for all of them rather than omitting different keys.
    """
    if noise is None:
        return {
            "noise_floor_db": None,
            "noise_floored_fraction": None,
            "noise_floor_margin_db": None,
            "noise_floor_spread_db": None,
        }
    return {
        "noise_floor_db": noise.floor_db,
        "noise_floored_fraction": noise.floored_fraction,
        "noise_floor_margin_db": noise.margin_db,
        "noise_floor_spread_db": noise.floor_spread_db,
    }


def _margin_db(median: float, floor: float) -> float | None:
    """How far a scene's median power sits above an inferred floor, in decibels.

    ``None`` where the ratio is undefined — a non-positive floor or median, which
    a scene of pure zeros produces — because an absent number is a smaller lie
    than an infinite one in a tag a reader will parse as a float.
    """
    if floor <= 0.0 or median <= 0.0:
        return None
    return 10.0 * math.log10(median / floor)


def sicd_noise_level(src: str | os.PathLike) -> str | None:
    """Which noise level a SICD product's metadata declares, if any.

    Returns ``"ABSOLUTE"`` (the floor is stated, so ``noise_subtract=True``
    works), ``"RELATIVE"`` (the floor's *variation* is described but not its
    level, so it cannot be subtracted), or ``None`` for a product with no noise
    metadata at all. Ask this before passing ``noise_subtract=True`` when you
    want to *check* rather than handle the error — the same role
    :func:`sicd_calibration_types` plays for the scale factors.

    Only the ``"measured"`` noise model depends on this: ``noise_model=
    "estimated"`` infers the floor from the scene's own pixels, so it works on
    the products this returns ``None`` for — which is most of Umbra's open
    archive.

    Parameters
    ----------
    src:
        Path to a SICD NITF file.
    """
    _require("sarpy")
    from sarpy.io.complex.converter import open_complex  # noqa: PLC0415

    reader = open_complex(str(src))
    return _noise_level_type(reader.get_sicds_as_tuple()[0])


def _check_measurement_support(
    sicd: Any,
    *,
    calibration: str | None,
    noise_subtract: bool,
    noise_model: str,
    rtc: bool = False,
) -> None:
    """Raise if this product's own metadata cannot support what was asked of it.

    The corrections that depend on a product describing itself —
    ``calibration=`` and a ``"measured"`` noise floor, which read polynomials out
    of the SICD's ``Radiometric`` block, and ``rtc=``, which reads the collection
    geometry out of its ``SCPCOA`` block — are asked about here, off the metadata
    alone. That is what makes the refusal cost the *header* rather than the
    scene: the checks used to happen where the values are first used, which is
    after a multi-gigabyte complex product has been read and detected (and, for
    the flattening, after it has been warped), so a conversion that could never
    have succeeded still spent the whole read finding out. It is the ordering
    :func:`_check_speckle_window` already has, applied to every setting whose
    answer is in the file rather than in the request.

    The evaluations downstream stay exactly where they are — that is where the
    image coordinates they are functions of exist — so what is duplicated is a
    handful of metadata lookups, and the checks themselves are the same
    functions, not a second opinion about them.
    """
    if calibration is not None:
        _calibration_coefficients(sicd, calibration)
        _image_grid_geometry(sicd)
    if noise_subtract and noise_model == "measured":
        _noise_coefficients(sicd)
        _image_grid_geometry(sicd)
    if rtc:
        _scene_look_geometry(sicd)


# --------------------------------------------------------------------------- #
# Scoring an inferred noise floor against a measured one.
# --------------------------------------------------------------------------- #

#: The noise models :func:`compare_noise_models` can score — :data:`NOISE_MODELS`
#: without ``"measured"``, which is the reference the others are scored against
#: rather than a candidate.
INFERRED_NOISE_MODELS = tuple(model for model in NOISE_MODELS if model != "measured")


@dataclass(frozen=True)
class NoiseModelAgreement:
    """How closely one inferred noise floor matches the product's measured one.

    The two inferred models (:func:`_estimate_noise_power`,
    :func:`_estimate_noise_profile`) make a documented pair of claims: that the
    floor they read is *biased low but consistently so*, and that the range
    profile adds the across-swath **shape** a constant cannot. Both claims are
    about the difference between the inferred floor and the true one, which is
    unobservable on a product that states no floor — so this splits the measured
    difference into exactly those two parts.

    Attributes
    ----------
    model:
        The model scored, one of :data:`INFERRED_NOISE_MODELS`.
    floor_db:
        The level it inferred (the profile's median for ``"estimated-range"``),
        the same number the conversion records in ``UMBRA_NOISE_FLOOR_DB``.
    spread_db:
        Peak-to-peak swing of the inferred floor — ``0.0`` for the constant
        estimate by construction, and ``UMBRA_NOISE_FLOOR_SPREAD_DB`` for the
        profile. Compare against :attr:`NoiseModelComparison.measured_spread_db`,
        which is how much swing was actually there to find.
    bias_db:
        Median of ``inferred − measured`` in decibels: the *offset*. Expected
        negative, because a percentile of a speckled noise-only population sits
        below that population's mean — the estimators read low, which
        under-subtracts, which is the safe direction. A positive bias is the
        interesting failure: the scene had too little dark ground and the low
        tail read was backscatter.
    shape_error_db:
        RMS of ``inferred − measured`` **after removing the bias**: what is left
        once the offset above is granted, i.e. how well the inferred floor
        follows the true one across the image. This is the number the range
        profile exists to reduce, and on a scene whose floor genuinely varies a
        constant estimate cannot score better here than the measured floor's own
        RMS deviation about its mean.
    residual_db:
        RMS of ``inferred − measured`` with the bias included — how wrong the
        subtracted floor was in absolute terms, which is what the pixels
        actually got.
    """

    model: str
    floor_db: float
    spread_db: float
    bias_db: float
    shape_error_db: float
    residual_db: float


@dataclass(frozen=True)
class NoiseModelComparison:
    """The measured noise floor of one product, and how the estimators did on it.

    Attributes
    ----------
    source:
        File name of the product compared on (the name only, for the same reason
        :func:`conversion_tags` records only that).
    shape:
        ``(rows, cols)`` of the image window the comparison ran over — the whole
        scene, or the ``bbox=`` window when one was given.
    measured_floor_db:
        Median of the product's own ``NoisePoly`` over that window: the truth
        the estimates are scored against.
    measured_spread_db:
        Peak-to-peak swing of that measured floor across the window. This is the
        premise of the whole range-profile model: where it is near zero, a
        constant estimate had nothing to miss and the two inferred models should
        score alike; where it is wide, it is the artefact a scalar floor leaves
        behind.
    models:
        One :class:`NoiseModelAgreement` per model scored, in the order asked
        for.
    """

    source: str
    shape: tuple[int, int]
    measured_floor_db: float
    measured_spread_db: float
    models: tuple[NoiseModelAgreement, ...]


def _floor_agreement(model: str, inferred_db, measured_db, *, floor_db: float, spread_db: float):
    """Score one inferred floor against the measured one, both in decibels.

    Split rather than summed: the offset (:attr:`NoiseModelAgreement.bias_db`)
    and what is left after granting it (``shape_error_db``) answer two different
    questions, and the estimators are only ever claimed to get the second right.
    The median is used for the offset and the RMS for the errors, so one
    contaminated corner of a scene moves the offset a little and the error a lot
    — which is the ordering that makes the two numbers readable together.

    ``inferred_db`` broadcasts against ``measured_db``: a scalar for the constant
    estimate, an ``(rows, 1)`` column for the fitted profile.
    """
    np = _require("numpy")

    inferred = np.broadcast_to(np.asarray(inferred_db, dtype="float64"), measured_db.shape)
    diff = inferred - measured_db
    bias = float(np.median(diff))
    return NoiseModelAgreement(
        model=model,
        floor_db=float(floor_db),
        spread_db=float(spread_db),
        bias_db=bias,
        shape_error_db=float(np.sqrt(np.mean(np.square(diff - bias)))),
        residual_db=float(np.sqrt(np.mean(np.square(diff)))),
    )


def compare_noise_models(
    src: str | os.PathLike,
    *,
    models: tuple[str, ...] = INFERRED_NOISE_MODELS,
    percentile: float = NOISE_ESTIMATE_PERCENTILE,
    bbox: tuple[float, float, float, float] | None = None,
) -> NoiseModelComparison:
    """Score the inferred noise floors against the one a product measures.

    ``noise_model="estimated"`` and ``"estimated-range"`` infer the receiver's
    floor from the scene's own darkest pixels, which is what lets the correction
    run on Umbra's open archive at all — those products carry no ``Radiometric``
    block to read a floor from. That is also why the inference was, until this,
    only ever *argued*: the archive it was built for has no truth to check it
    against.

    A product that does state an ``ABSOLUTE`` noise level has both. This runs the
    inferred models over such a product's pixels and differences each result
    against its own ``NoisePoly``, which turns "does the estimator work?" into
    two numbers per model: the offset it reads low by, and — after granting that
    offset — how well it follows the real floor across the image
    (:class:`NoiseModelAgreement`). Nothing is written and no conversion is
    performed; this is the measurement, not a correction.

    What to expect, and what each departure means:

    * ``bias_db`` negative on both models, by roughly the same amount. A
      percentile of a speckled noise-only population sits below that
      population's mean, so both estimators read low and under-subtract, which
      leaves a little of the receiver in rather than taking real backscatter
      out. Positive is the failure the margin diagnostic warns about: too little
      dark ground, so the low tail was ground.
    * ``shape_error_db`` much smaller for ``"estimated-range"`` than for
      ``"estimated"`` **when** ``measured_spread_db`` is wide. That is the whole
      claim of the fitted profile, and where the floor is genuinely flat there is
      nothing to separate the two models and they should score alike.

    Parameters
    ----------
    src:
        Path to a SICD NITF file that declares an ``ABSOLUTE`` noise level. One
        that does not raises, naming what it carries — the same refusal
        ``noise_model="measured"`` makes, for the same reason: there is no truth
        here to score against. :func:`sicd_noise_level` answers it ahead of time.
    models:
        Which inferred models to score, a subset of
        :data:`INFERRED_NOISE_MODELS`.
    percentile:
        The low-tail percentile the estimators read, defaulting to the one they
        use in a conversion (:data:`NOISE_ESTIMATE_PERCENTILE`). Exposed here and
        nowhere else on purpose: this is the surface where the number is being
        *measured* rather than trusted, so sweeping it is the point.
    bbox:
        Optional ``(min_lon, min_lat, max_lon, max_lat)`` window to compare over
        instead of the whole scene, resolved exactly as ``bbox=`` on
        :func:`sicd_to_geocoded_cog` — including evaluating the ``NoisePoly`` at
        the image coordinates the window actually occupies, without which the
        measured floor would be read off the wrong part of the swath.
    """
    np = _require("numpy")
    _require("sarpy")
    from sarpy.io.complex.converter import open_complex  # noqa: PLC0415

    unknown = [model for model in models if model not in INFERRED_NOISE_MODELS]
    if unknown:
        raise ValueError(
            f"Cannot compare noise model(s) {', '.join(repr(m) for m in unknown)}; "
            f"choose from {', '.join(INFERRED_NOISE_MODELS)}. 'measured' is the "
            "reference every model here is scored against, not a candidate."
        )
    if not models:
        raise ValueError("No noise models to compare; pass at least one of INFERRED_NOISE_MODELS.")

    reader = open_complex(str(src))
    sicd = reader.get_sicds_as_tuple()[0]
    # Read the measured floor's metadata *first*: on a product that cannot supply
    # one there is nothing to compare against, and finding that out before
    # reading a multi-gigabyte scene is free.
    coefs = _noise_coefficients(sicd)

    origin = (0, 0)
    if bbox is not None:
        row0, row1, col0, col1 = _clip_window(sicd, _reader_shape(reader, sicd), bbox)
        data = reader[row0:row1, col0:col1]
        origin = (row0, col0)
    else:
        data = reader[:, :]
    amplitude = _amplitude(data, decibels=True)

    geometry = _image_grid_geometry(sicd)
    geometry["first_row"] += float(origin[0])
    geometry["first_col"] += float(origin[1])
    measured_db = 10.0 * np.log10(_noise_power(coefs, amplitude.shape, **geometry))

    scored: list[NoiseModelAgreement] = []
    for model in models:
        if model == "estimated-range":
            profile, _median, level_db, spread_db = _estimate_noise_profile(
                amplitude, decibels=True, percentile=percentile
            )
            inferred_db = 10.0 * np.log10(profile)
        else:
            floor, _median = _estimate_noise_power(amplitude, decibels=True, percentile=percentile)
            level_db = 10.0 * math.log10(floor) if floor > 0.0 else float("-inf")
            inferred_db = level_db
            spread_db = 0.0
        scored.append(
            _floor_agreement(
                model, inferred_db, measured_db, floor_db=level_db, spread_db=spread_db
            )
        )

    return NoiseModelComparison(
        source=Path(src).name,
        shape=(int(amplitude.shape[0]), int(amplitude.shape[1])),
        measured_floor_db=float(np.median(measured_db)),
        measured_spread_db=float(measured_db.max() - measured_db.min()),
        models=tuple(scored),
    )


# --------------------------------------------------------------------------- #
# Speckle filtering (the granular texture coherent imaging has instead of noise).
# --------------------------------------------------------------------------- #

#: Speckle filters accepted by :func:`sicd_to_geocoded_cog` /
#: :func:`sicd_to_amplitude_geotiff` (``speckle_filter=``), applied to detected
#: **power** in image space:
#:
#: * ``"boxcar"`` averages the window unconditionally — the multilook every SAR
#:   workflow starts with. It is the strongest variance reduction available for a
#:   given window and the bluntest: an edge inside the window is averaged across
#:   just as happily as a homogeneous field is.
#: * ``"lee"`` is the local-statistics minimum-mean-square-error filter (Lee
#:   1980): it averages where the window's variability is what speckle alone
#:   would produce and leaves the pixel alone where it is more variable than
#:   that, so edges, points and textured ground survive. It smooths less than
#:   ``"boxcar"`` on purpose.
SPECKLE_FILTERS = ("boxcar", "lee")

#: Default window edge, in pixels, for :data:`SPECKLE_FILTERS`. Odd so the
#: window is centred on the pixel it replaces; 5 rather than 3 because Umbra's
#: products are sampled finer than their resolution (neighbouring pixels are
#: partly the same measurement), so a 3-pixel window averages fewer independent
#: looks than its size suggests — see :attr:`SpeckleFiltering.enl_after`.
SPECKLE_WINDOW_DEFAULT = 5

#: Smallest edge of the non-overlapping blocks :func:`_estimate_enl` reads its
#: per-block mean and variance from. Large enough that a block's variance is a
#: usable estimate, small enough that many blocks land inside one surface — which
#: is what the estimator needs, since a block spanning two surfaces measures the
#: contrast between them rather than the speckle within either.
_ENL_BLOCK = 16

#: How many filter windows wide a block must be, when the raster being measured
#: has been through a window of its own. A block's variance is only a usable
#: estimate of the field's variance if the block holds enough *independent*
#: samples, and a filter makes neighbouring pixels dependent out to its window —
#: so a 16-pixel block after a 9×9 filter holds about three independent samples
#: and its variance is far too noisy to divide by. Six windows keeps the estimate
#: within a few percent of the truth on synthetic single-look imagery for every
#: window this module accepts (measured in ``tests/test_convert.py``).
_ENL_BLOCK_WINDOWS = 6

#: Which percentile of the per-block ENL distribution :func:`_estimate_enl`
#: reports: the **median**, i.e. the typical block rather than the most uniform
#: one. Picking the upper tail instead would look like the better idea — texture
#: can only inflate a block's variance, so only push its ENL down — but the ENL
#: of a block is a ratio of noisy estimates, and its upper tail is dominated by
#: the blocks whose variance happened to come out low rather than by the blocks
#: that were genuinely uniform. The median is the estimate that texture biases
#: *down*, which is the safe direction for a number that says how much speckle
#: was removed.
_ENL_PERCENTILE = 50.0

#: Fraction of a block's pixels that must be finite before its ENL is believed.
#: A block mostly outside the collect (or mostly nodata after a clip) has too few
#: samples for a variance to mean anything.
_ENL_MIN_VALID = 0.5

#: Below this ratio of :attr:`SpeckleFiltering.enl_after` to ``enl_before``,
#: ``umbra convert`` says the window bought little. An advisory, never a refusal:
#: on a scene that is textured everywhere — or a product whose pixels are heavily
#: oversampled — a small gain is the honest outcome rather than a fault.
SPECKLE_ENL_GAIN_WARN = 1.5


@dataclass(frozen=True)
class SpeckleFiltering:
    """What one speckle filter did, beyond changing the pixels.

    Speckle is not sensor noise: it is the interference pattern coherent
    illumination produces on a rough surface, so it is *multiplicative*, it is
    the same physics on every SAR image, and averaging is the only thing that
    removes it. Which means a filtered raster differs from an unfiltered one in
    two ways that both matter downstream — its variance and its effective
    resolution — and neither is visible in the pixel values afterwards. Hence
    the record.

    Attributes
    ----------
    filter:
        Which of :data:`SPECKLE_FILTERS` ran.
    window:
        Edge of the (odd, centred) window it ran over, in pixels.
    enl_before, enl_after:
        The scene's **equivalent number of looks** before and after, as
        :func:`_estimate_enl` reads it: the median block's ``mean² / variance``
        of detected power, which is the standard measure of how much speckle is
        left. Single-look imagery sits at about 1.0 and every filter's job is to
        raise it, so the pair is the filter's own effect measured on the scene it
        ran on rather than claimed from its window size. Both read low on a
        textured scene, so the *ratio* is the number to trust rather than either
        level. ``None`` where the raster was smaller than one measuring block or
        no block held enough finite pixels (:data:`_ENL_MIN_VALID`).

        The gain is worth reading rather than assuming: a ``window²``-pixel
        boxcar averages ``window²`` *pixels* but only as many independent
        *looks* as the product's sampling provides, and Umbra samples finer than
        its resolution, so the achieved ENL lands below the window's pixel count.
        That gap is a fact about the product, and this is where it shows up.
    looks:
        The ENL ``"lee"`` assumed for the speckle it was separating from scene
        structure — :attr:`enl_before`, i.e. read off the scene rather than
        assumed, clamped at single-look (no product has fewer looks than one, so a
        lower read is the estimator meeting texture) and falling back to 1.0 where
        the scene gave no block to read. ``None`` for ``"boxcar"``, which needs no
        such parameter. It is recorded because the filter's output depends on it,
        so a pixel value is not reproducible without it.

    Notes
    -----
    Like :class:`NoiseSubtraction`'s diagnostics, ``enl_before`` / ``enl_after``
    / ``looks`` describe *this scene* rather than what a pixel value means, so
    they are recorded (see :func:`conversion_tags`) but stay out of
    :data:`umbra_py.load.MEASUREMENT_PROVENANCE_KEYS`: two passes of one site
    legitimately differ on them, and refusing a stack over that would end every
    series. ``filter`` and ``window`` are the opposite case and *are* in that key
    set — a 5×5-averaged pass differenced against an unfiltered one reports the
    filter as change.
    """

    filter: str
    window: int
    enl_before: float | None = None
    enl_after: float | None = None
    looks: float | None = None


def _check_speckle_window(window: int) -> int:
    """Reject a speckle window that cannot be centred on the pixel it replaces.

    Odd and at least 3: an even window has no centre pixel (the output would be
    shifted half a pixel against its own geolocation), and a 1-pixel window is a
    filter that does nothing, which is better spelled by not asking for one.
    """
    size = int(window)
    if size < 3 or size % 2 == 0:
        raise ValueError(
            f"speckle_window must be an odd integer >= 3, got {window!r}. The window "
            "is centred on the pixel it replaces, so an even edge would shift the "
            "output half a pixel against its own geolocation, and 1 would filter "
            "nothing."
        )
    return size


def _box_sum(values: np.ndarray, window: int):
    """Sum of ``values`` over the ``window``-square neighbourhood of each pixel.

    A summed-area table (two cumulative sums) rather than a convolution, so the
    cost is independent of the window size — a 9×9 filter costs what a 3×3 one
    does, which is what makes the window a free parameter rather than a budget.
    Windows are **clipped** at the image edge rather than padded, so an edge
    pixel averages the neighbours it has; :func:`_local_moments` divides by the
    matching count, which is why the count is computed the same way.
    """
    np = _require("numpy")

    rows, cols = values.shape
    pad = window // 2
    table = np.zeros((rows + 1, cols + 1), dtype="float64")
    np.cumsum(values, axis=0, dtype="float64", out=table[1:, 1:])
    np.cumsum(table[1:, 1:], axis=1, dtype="float64", out=table[1:, 1:])

    r = np.arange(rows)
    c = np.arange(cols)
    r0, r1 = np.clip(r - pad, 0, rows), np.clip(r + pad + 1, 0, rows)
    c0, c1 = np.clip(c - pad, 0, cols), np.clip(c + pad + 1, 0, cols)
    total = table[np.ix_(r1, c1)]
    total -= table[np.ix_(r0, c1)]
    total -= table[np.ix_(r1, c0)]
    total += table[np.ix_(r0, c0)]
    return total


def _local_moments(power: np.ndarray, window: int, *, squares: bool):
    """Windowed mean (and optionally mean-square) of a power raster, plus counts.

    Non-finite pixels — the nodata a clip or a previous warp leaves — are
    *excluded* from their neighbours' windows rather than treated as zero, which
    is the difference between an edge pixel that averages its real neighbours and
    one dragged toward nothing. Returns ``(mean, mean_sq, count)`` with
    ``mean_sq`` ``None`` when ``squares`` is false, and ``NaN`` means where a
    window held no finite pixel at all.
    """
    np = _require("numpy")

    finite = np.isfinite(power)
    filled = np.where(finite, power, 0.0)
    count = _box_sum(finite.astype("float64"), window)
    with np.errstate(invalid="ignore", divide="ignore"):
        mean = np.where(count > 0, _box_sum(filled, window) / count, np.nan)
        mean_sq = (
            np.where(count > 0, _box_sum(np.square(filled), window) / count, np.nan)
            if squares
            else None
        )
    return mean, mean_sq, count


def _block_enl_ratios(power: np.ndarray, *, block: int = _ENL_BLOCK):
    """The per-block ``mean² / variance`` ratios :func:`_estimate_enl` reduces.

    Split out because the reduction and the blocks are separable: a caller that
    reads a scene in pieces -- :func:`umbra_py.chips._scene_speckle`, which
    samples windows of a raster it never holds whole -- needs to *pool* the
    blocks of several arrays before taking the percentile, and pooling
    percentiles is not the same thing as the percentile of the pooled blocks.

    Returns a flat array, empty where the raster is smaller than one block, no
    block held enough finite pixels (:data:`_ENL_MIN_VALID`), or every
    qualifying block had zero variance.
    """
    np = _require("numpy")

    rows = (power.shape[0] // block) * block
    cols = (power.shape[1] // block) * block
    if rows == 0 or cols == 0:
        return np.empty(0, dtype="float64")
    tiles = power[:rows, :cols].reshape(rows // block, block, cols // block, block)
    valid = np.isfinite(tiles).sum(axis=(1, 3))
    with warnings.catch_warnings():
        # An all-nodata block is expected (a clip's corners); its mean is NaN and
        # it is dropped below rather than warned about.
        warnings.simplefilter("ignore", RuntimeWarning)
        mean = np.nanmean(tiles, axis=(1, 3))
        # ddof=1: the block's variance is being *estimated* from its pixels, and
        # the population form biases it low, which would bias the ratio -- the
        # number reported -- high.
        var = np.nanvar(tiles, axis=(1, 3), ddof=1)
    keep = (valid >= max(4, int(_ENL_MIN_VALID * block * block))) & (var > 0) & (mean > 0)
    if not keep.any():
        return np.empty(0, dtype="float64")
    return np.square(mean[keep]) / var[keep]


def _estimate_enl(
    power: np.ndarray,
    *,
    block: int = _ENL_BLOCK,
    percentile: float = _ENL_PERCENTILE,
) -> float | None:
    """Read a raster's equivalent number of looks off its own blocks.

    ENL is ``mean² / variance`` of detected power, and over a homogeneous surface
    that ratio *is* the number of independent looks averaged into each pixel —
    1.0 for single-look imagery, higher the more speckle has been averaged away.
    Measured over a whole scene it is meaningless, because the variance would be
    the scene's own contrast; so it is measured per block and the median block is
    reported (:data:`_ENL_PERCENTILE`).

    Structure inside a block inflates its variance and so deflates its ENL, which
    means this reads *low* on a textured scene — it is a floor on the looks
    present, not a claim about them, and that is the direction to be wrong in for
    a number quoted as "this is how much speckle was removed". It is a diagnostic:
    nothing in the conversion depends on it except ``"lee"``'s own speckle
    parameter, which is clamped at single-look for exactly this reason.

    ``block`` should span several of any filter window the raster has been through
    (:data:`_ENL_BLOCK_WINDOWS`): a block only a window or two across holds a
    handful of *independent* samples, and dividing by a variance estimated from a
    handful of samples biases the ratio high.

    ``None`` when the raster is smaller than one block, when no block held enough
    finite pixels (:data:`_ENL_MIN_VALID`), or when every qualifying block had
    zero variance — a synthetic constant raster, where there is no speckle to
    count the looks of.
    """
    np = _require("numpy")

    ratios = _block_enl_ratios(power, block=block)
    if ratios.size == 0:
        return None
    return float(np.percentile(ratios, float(percentile)))


def _boxcar_power(power: np.ndarray, window: int):
    """Unconditional windowed mean of a power raster — the multilook."""
    np = _require("numpy")

    mean, _mean_sq, count = _local_moments(power, window, squares=False)
    return np.where(count > 0, mean, power)


def _lee_power(power: np.ndarray, window: int, *, looks: float):
    """Lee's local-statistics minimum-MSE speckle filter, in the power domain.

    Speckle multiplies, so over a homogeneous surface a window's coefficient of
    variation is a known constant — ``1/sqrt(looks)`` — and any *excess*
    variability is scene structure. The filter is that comparison: it returns
    ``mean + b·(pixel − mean)`` with

    ``b = (var − mean²/looks) / (var · (1 + 1/looks))``

    clipped to ``[0, 1]``. Where the window is no more variable than speckle
    alone explains, ``b`` is 0 and the pixel becomes the local mean (full
    smoothing); across an edge or on a bright point ``b`` approaches 1 and the
    pixel is kept. That is the whole difference from :func:`_boxcar_power`: the
    same window, applied only where averaging is defensible.

    ``looks`` is the ENL the speckle is assumed to have, read off the scene by
    :func:`_estimate_enl` rather than assumed — a filter told the imagery is
    single-look when it has already been averaged over-smooths, because it reads
    real structure as speckle it is allowed to remove.
    """
    np = _require("numpy")

    mean, mean_sq, count = _local_moments(power, window, squares=True)
    var = np.maximum(mean_sq - np.square(mean), 0.0)
    speckle_var = np.square(mean) / float(looks)
    with np.errstate(invalid="ignore", divide="ignore"):
        weight = np.clip((var - speckle_var) / (var * (1.0 + 1.0 / float(looks))), 0.0, 1.0)
    weight = np.where(np.isfinite(weight), weight, 0.0)
    return np.where(count > 0, mean + weight * (power - mean), power)


def _filter_speckle(
    amplitude: np.ndarray,
    *,
    decibels: bool,
    name: str,
    window: int = SPECKLE_WINDOW_DEFAULT,
    looks: float | None = None,
) -> tuple[np.ndarray, SpeckleFiltering]:
    """Speckle-filter a detected-amplitude raster, in the power domain.

    Power, not amplitude or decibels, for the same reason the noise subtraction
    works there: speckle is multiplicative in amplitude and its statistics — the
    coefficient of variation :func:`_lee_power` tests against, and the ENL both
    filters are measured by — are defined on intensity. A mean of decibels is the
    geometric mean of the powers, which is biased low by about 2.5 dB for
    single-look speckle; a mean of powers is the estimate of the surface's
    backscatter. So the raster is converted to power, filtered, and converted
    back to whichever scale it arrived in.

    Non-finite pixels stay non-finite — a filter changes values, not the mask —
    and they are excluded from their neighbours' windows rather than counted as
    zero (:func:`_local_moments`).

    Returns the filtered raster and a :class:`SpeckleFiltering` describing what
    the filter did to it — the ENL it started from, the ENL it reached, and (for
    ``"lee"``) the looks it assumed.

    ``looks`` supplies that speckle parameter from a *wider scope* than this
    array, and is the reason a caller that filters a scene in pieces gets the
    same answer as one that filters it whole. When it is given, this array's own
    ENL is not read at all: ``"lee"`` filters by the supplied value, and the
    returned record carries no ``enl_before`` / ``enl_after``, because measuring
    them here would describe the piece rather than the scene the looks came from
    (``"boxcar"`` needs no such parameter, so for it only the skipped
    measurement applies). :func:`umbra_py.chips._scene_speckle` is the caller
    this exists for — it establishes both numbers once per acquisition and then
    cuts every tile through the identical filter.
    """
    np = _require("numpy")

    if name not in SPECKLE_FILTERS:
        raise ValueError(
            f"Unknown speckle_filter {name!r}; choose one of {', '.join(SPECKLE_FILTERS)}."
        )
    size = _check_speckle_window(window)
    power = _detected_power(amplitude, decibels=decibels)
    # One block size for both reads, sized to the window the *filtered* raster
    # will carry, so the pair is a before/after of one measurement rather than two
    # differently-biased ones.
    block = max(_ENL_BLOCK, _ENL_BLOCK_WINDOWS * size)
    measure = looks is None
    enl_before = _estimate_enl(power, block=block) if measure else None

    if name == "lee":
        # Read off the scene and clamped at single-look. No SAR product carries
        # fewer than one look, so a read below 1.0 is the estimator meeting a
        # textured scene rather than physics -- and believing it would tell the
        # filter that speckle is worse than it is, which is licence to smooth
        # structure away. Under-smoothing is the safe direction.
        if looks is None:
            looks = max(enl_before, 1.0) if enl_before is not None else 1.0
        filtered = _lee_power(power, size, looks=looks)
    else:
        looks = None
        filtered = _boxcar_power(power, size)

    enl_after = _estimate_enl(filtered, block=block) if measure else None
    # A filter changes values, not the mask: a nodata pixel had no measurement to
    # improve, and filling it from its neighbours would invent ground where the
    # scene has none (the same rule the noise subtraction follows).
    filtered = np.where(np.isfinite(power), filtered, np.nan)
    # The same "as dark as this raster goes" clamp `_amplitude` applies to
    # magnitude, so a window that averaged only zeros reads as the darkest value
    # the raster can hold rather than as -inf.
    filtered = np.clip(filtered, _NOISE_RESIDUAL_FLOOR, None)
    with np.errstate(divide="ignore", invalid="ignore"):
        corrected = 10.0 * np.log10(filtered) if decibels else np.sqrt(filtered)
    return (
        np.asarray(corrected, dtype="float32"),
        SpeckleFiltering(
            filter=name,
            window=size,
            enl_before=enl_before,
            enl_after=enl_after,
            looks=looks,
        ),
    )


class _SpeckleTagValues(TypedDict):
    """The :func:`conversion_tags` keywords a :class:`SpeckleFiltering` fills in."""

    speckle_filter: str | None
    speckle_window: int | None
    speckle_enl_before: float | None
    speckle_enl_after: float | None
    speckle_looks: float | None


def _speckle_tag_values(speckle: SpeckleFiltering | None) -> _SpeckleTagValues:
    """A :class:`SpeckleFiltering` as :func:`conversion_tags` keyword arguments.

    One place for the mapping so both writers (slant-plane and geocoded) record
    the same numbers, the same way :func:`_noise_tag_values` does for the noise
    subtraction.
    """
    if speckle is None:
        return {
            "speckle_filter": None,
            "speckle_window": None,
            "speckle_enl_before": None,
            "speckle_enl_after": None,
            "speckle_looks": None,
        }
    return {
        "speckle_filter": speckle.filter,
        "speckle_window": speckle.window,
        "speckle_enl_before": speckle.enl_before,
        "speckle_enl_after": speckle.enl_after,
        "speckle_looks": speckle.looks,
    }


# --------------------------------------------------------------------------- #
# Conversion provenance (what the pixel values mean, recorded in the raster).
# --------------------------------------------------------------------------- #

#: Prefix on the GeoTIFF metadata keys this module writes into every raster it
#: converts (see :func:`conversion_tags`). Namespaced so umbra-py's provenance
#: never collides with the product's own tags, and so
#: :func:`read_conversion_tags` can pick it back out of a mixed tag set.
PROVENANCE_TAG_PREFIX = "UMBRA_"


def _speckle_detail_tags(
    *,
    window: int | None,
    enl_before: float | None,
    enl_after: float | None,
    looks: float | None,
) -> dict[str, str]:
    """The unprefixed tags describing a speckle filter that ran, minus its name.

    One formatter, because :mod:`umbra_py.chips` writes the same four keys onto a
    tile it filtered itself: a chip cut from a filtered GEC and one cut from a
    filtered SICD have to read identically, down to the number of significant
    figures, or a manifest built from both would sort them into two products.
    A value that was never measured is omitted rather than written as a sentinel
    -- unlike a processing *step*, an absent diagnostic has only one meaning.
    """
    tags: dict[str, str] = {}
    if window is not None:
        tags["SPECKLE_WINDOW"] = str(int(window))
    if enl_before is not None:
        tags["SPECKLE_ENL_BEFORE"] = f"{float(enl_before):.4g}"
    if enl_after is not None:
        tags["SPECKLE_ENL_AFTER"] = f"{float(enl_after):.4g}"
    if looks is not None:
        tags["SPECKLE_LOOKS"] = f"{float(looks):.4g}"
    return tags


def _pixel_units(*, calibration: str | None, decibels: bool) -> str:
    """A one-line statement of what a pixel value *is*, for the units tag.

    Uncalibrated output is relative brightness in the product's own arbitrary
    units; a calibrated one is a physical quantity, and in the linear scale it
    is the *amplitude* whose square is that quantity (see ``calibration=`` on
    :func:`sicd_to_geocoded_cog`).
    """
    if calibration is None:
        return "dB (relative amplitude)" if decibels else "relative amplitude"
    if decibels:
        return f"dB ({calibration})"
    return f"amplitude (sqrt {calibration})"


def conversion_tags(
    *,
    source: str | os.PathLike,
    geocoded: bool,
    decibels: bool = True,
    calibration: str | None = None,
    noise_subtraction: str | None = None,
    noise_floor_db: float | None = None,
    noise_floored_fraction: float | None = None,
    noise_floor_margin_db: float | None = None,
    noise_floor_spread_db: float | None = None,
    speckle_filter: str | None = None,
    speckle_window: int | None = None,
    speckle_enl_before: float | None = None,
    speckle_enl_after: float | None = None,
    speckle_looks: float | None = None,
    rtc_model: str | None = None,
    rtc_reference_deg: float | None = None,
    projection_type: str | None = None,
    dem: str | os.PathLike | None = None,
    geoid: str | os.PathLike | None = None,
    resampling: str | None = None,
) -> dict[str, str]:
    """The provenance tags describing one conversion, as GeoTIFF metadata.

    A converted raster carries no trace of *how* it was made unless one is
    written into it: two scenes converted with different ``calibration=`` or
    ``rtc_model=`` settings are pixel-for-pixel indistinguishable after the
    fact, and a calibrated scene whose calibration is unrecorded is a physical
    measurement nobody can quote. These tags are the same "provenance travels
    with the artifact" rule the render manifests follow, applied to the file
    itself, so ``gdalinfo`` (or :func:`read_conversion_tags`) answers the
    question that would otherwise need the shell history.

    Every processing step is reported, including the ones that did *not* run —
    ``"none"`` rather than a missing key — so a tag's absence never has to be
    interpreted. Values are strings because GeoTIFF metadata is.

    Parameters
    ----------
    source:
        The input product. Only its file *name* is recorded: the local
        directory it happened to sit in is not provenance, and travels with the
        artifact to places it does not belong.
    geocoded:
        Whether the output is the map-ready geocoded raster
        (:func:`sicd_to_geocoded_cog`) or the ungeoreferenced slant-plane
        amplitude (:func:`sicd_to_amplitude_geotiff`). The geocoding parameters
        below are recorded only for the former.
    noise_subtraction:
        Which noise floor was subtracted — ``"absolute"`` for the SICD's own
        stated level, ``"estimated"`` for one constant inferred from the scene,
        ``"estimated-range"`` for one inferred per range line — or ``None`` for a
        raster that still carries its floor. Recorded because it is the
        difference between "this surface backscatters at −25 dB" and "this sensor
        cannot hear below −25 dB", which the pixel values themselves cannot
        distinguish after the fact; and the values are distinct because a
        measured floor, a constant guess and a fitted profile are not the same
        claim about the pixels.
    noise_floor_db:
        The estimated floor actually subtracted, in decibels, for the inferred
        models (the profile's median level for ``"estimated-range"``, whose swing
        is ``noise_floor_spread_db``). Recorded for the same reason
        ``rtc_reference_deg`` is: an inferred number that nobody can read back
        is not reproducible. Omitted for the measured floor, which is a
        polynomial the product itself states rather than one this module chose.
    noise_floored_fraction:
        How much of the image the subtraction drove to the sensor's sensitivity
        limit, ``noise_floor_margin_db`` how far the scene's median power sat
        above an *estimated* floor, and ``noise_floor_spread_db`` how far a fitted
        range profile swung from one edge of the swath to the other — the
        diagnostics of :class:`NoiseSubtraction`. They describe how well the
        correction was supported by this scene rather than what a pixel value
        means, which is why they are recorded here but stay out of
        :data:`umbra_py.load.MEASUREMENT_PROVENANCE_KEYS`: they legitimately
        differ between passes of one series, so a stack must not be refused over
        them. All are omitted when no floor was subtracted.
    speckle_filter:
        Which speckle filter ran (:data:`SPECKLE_FILTERS`) and ``speckle_window``
        the window edge it ran over, or ``None`` for a raster that still carries
        its full speckle. Both are in
        :data:`umbra_py.load.MEASUREMENT_PROVENANCE_KEYS`, because a filtered
        pixel is an average over ground the unfiltered one resolved separately:
        differencing a 5×5-averaged pass against an unfiltered pass — or against a
        7×7-averaged one — reports the filter as change.
    speckle_enl_before:
        Along with ``speckle_enl_after`` and ``speckle_looks``, the filter's
        *diagnostics* (see :class:`SpeckleFiltering`): the equivalent number of
        looks the scene carried before and after, and the looks a ``"lee"`` filter
        assumed. Recorded because the achieved ENL is what says whether the window
        bought what its size suggests, and left out of the measurement keys for
        the same reason the noise diagnostics are — they legitimately differ
        between passes of one series. All are omitted when no filter ran.
    rtc_reference_deg:
        The *resolved* reference incidence angle the flattening normalised to
        (the scene incidence angle when the caller passed none), so the tag
        records the number actually used rather than the request.
    """
    tags = {
        "SOFTWARE": f"umbra-py {__version__}",
        "SOURCE": Path(source).name,
        "CONVERSION": "geocoded" if geocoded else "slant-plane",
        "SCALE": "decibels" if decibels else "linear",
        "UNITS": _pixel_units(calibration=calibration, decibels=decibels),
        "CALIBRATION": calibration or "none",
        "NOISE_SUBTRACTION": noise_subtraction or "none",
        "SPECKLE_FILTER": speckle_filter or "none",
        "RTC_MODEL": rtc_model or "none",
    }
    if noise_subtraction is not None:
        if noise_floor_db is not None:
            tags["NOISE_FLOOR_DB"] = f"{float(noise_floor_db):.6g}"
        if noise_floored_fraction is not None:
            tags["NOISE_FLOORED_FRACTION"] = f"{float(noise_floored_fraction):.4g}"
        if noise_floor_margin_db is not None:
            tags["NOISE_FLOOR_MARGIN_DB"] = f"{float(noise_floor_margin_db):.6g}"
        if noise_floor_spread_db is not None:
            tags["NOISE_FLOOR_SPREAD_DB"] = f"{float(noise_floor_spread_db):.6g}"
    if speckle_filter is not None:
        tags.update(
            _speckle_detail_tags(
                window=speckle_window,
                enl_before=speckle_enl_before,
                enl_after=speckle_enl_after,
                looks=speckle_looks,
            )
        )
    if rtc_model is not None and rtc_reference_deg is not None:
        tags["RTC_REFERENCE_DEG"] = f"{float(rtc_reference_deg):.6g}"
    if geocoded:
        tags["PROJECTION"] = "DEM" if dem is not None else (projection_type or "HAE")
        tags["DEM"] = Path(dem).name if dem is not None else "none"
        tags["GEOID"] = Path(geoid).name if geoid is not None else "none"
        if resampling is not None:
            tags["RESAMPLING"] = resampling
    # The data licence survives every transformation, including this one.
    tags["LICENSE"] = DATA_LICENSE
    tags["ATTRIBUTION"] = ATTRIBUTION
    return {f"{PROVENANCE_TAG_PREFIX}{key}": value for key, value in tags.items()}


def conversion_provenance(tags: Mapping[str, str]) -> dict[str, str]:
    """Pick umbra-py's conversion provenance out of an already-read tag set.

    The parsing half of :func:`read_conversion_tags`, split out because the
    consumers of this provenance mostly hold the dataset already:
    :func:`umbra_py.load.to_stack` reads every source's record while it is
    resolving the shared grid, and re-opening each raster to ask would cost a
    second round of range requests against a remote COG.

    Returns the :func:`conversion_tags` entries with the
    :data:`PROVENANCE_TAG_PREFIX` stripped and lower-cased, and an empty dict
    for a tag set with none of them (a raster umbra-py did not convert).
    """
    return {
        key[len(PROVENANCE_TAG_PREFIX) :].lower(): value
        for key, value in tags.items()
        if key.startswith(PROVENANCE_TAG_PREFIX)
    }


def read_conversion_tags(src: str | os.PathLike) -> dict[str, str]:
    """Read back the conversion provenance recorded in a converted raster.

    Returns the :func:`conversion_tags` entries with the
    :data:`PROVENANCE_TAG_PREFIX` stripped and lower-cased, so
    ``read_conversion_tags(path)["calibration"]`` answers "what do these pixel
    values mean?" — and an empty dict for a raster umbra-py did not convert.

    Parameters
    ----------
    src:
        Path to a raster (any format rasterio can open).
    """
    _require("rasterio")
    import rasterio  # noqa: PLC0415

    with rasterio.open(str(src)) as ds:
        tags = ds.tags()
    return conversion_provenance(tags)


def sicd_to_amplitude_geotiff(
    src: str | os.PathLike,
    dst: str | os.PathLike,
    *,
    decibels: bool = True,
    calibration: str | None = None,
    noise_subtract: bool = False,
    noise_model: str = "measured",
    speckle_filter: str | None = None,
    speckle_window: int = SPECKLE_WINDOW_DEFAULT,
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
    noise_subtract:
        If true, subtract the receiver's own thermal-noise floor from pixel
        power before any calibration (see :func:`sicd_to_geocoded_cog`).
    noise_model:
        Where that floor comes from, one of :data:`NOISE_MODELS`:
        ``"measured"`` (the default) reads the SICD's own
        ``Radiometric.NoiseLevel`` and raises when the product declares no
        absolute level — ask :func:`sicd_noise_level` first to check;
        ``"estimated"`` infers one constant floor from the scene's own pixels
        and needs no metadata; ``"estimated-range"`` infers one per range line
        and fits it against range. Ignored when ``noise_subtract`` is false.
    speckle_filter:
        Optional speckle filter, one of :data:`SPECKLE_FILTERS`, applied to
        detected power after any calibration (see
        :func:`sicd_to_geocoded_cog` for what each one trades). ``None`` leaves
        the scene's full speckle in.
    speckle_window:
        Edge of the odd, centred window that filter averages over.
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
    if noise_model not in NOISE_MODELS:
        raise ValueError(
            f"Unknown noise_model {noise_model!r}; choose one of {', '.join(NOISE_MODELS)}."
        )
    if speckle_filter is not None and speckle_filter not in SPECKLE_FILTERS:
        raise ValueError(
            f"Unknown speckle_filter {speckle_filter!r}; choose one of "
            f"{', '.join(SPECKLE_FILTERS)}."
        )

    reader = open_complex(str(src))
    sicd = reader.get_sicds_as_tuple()[0]
    # Before the read: whether this product can be calibrated, or state the floor
    # it is asked to have subtracted, is answerable from its metadata alone.
    _check_measurement_support(
        sicd,
        calibration=calibration,
        noise_subtract=noise_subtract,
        noise_model=noise_model,
    )
    amplitude = _amplitude(reader[:, :], decibels=decibels)
    noise: NoiseSubtraction | None = None
    if noise_subtract:
        # Before the calibration: noise adds to raw power, so it comes off there.
        amplitude, noise = _denoise_amplitude(sicd, amplitude, decibels=decibels, model=noise_model)
    if calibration is not None:
        amplitude = _calibrate_amplitude(
            sicd,
            amplitude,
            kind=calibration,
            decibels=decibels,
        )
    speckle: SpeckleFiltering | None = None
    if speckle_filter is not None:
        amplitude, speckle = _filter_speckle(
            amplitude, decibels=decibels, name=speckle_filter, window=speckle_window
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
        out.update_tags(
            **conversion_tags(
                source=src,
                geocoded=False,
                decibels=decibels,
                calibration=calibration,
                noise_subtraction=_NOISE_PROVENANCE[noise_model] if noise_subtract else None,
                **_noise_tag_values(noise),
                **_speckle_tag_values(speckle),
            )
        )
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
    origin: tuple[int, int] = (0, 0),
) -> list[GroundControlPoint]:
    """Ground control points mapping image (row, col) to lon/lat via the SICD model.

    Projects a ``grid``×``grid`` lattice of image coordinates to WGS-84 ground
    coordinates using SICD's own image-projection algorithm, so the warp in
    :func:`_warp_gcps_to_cog` reproduces the sensor geometry rather than a naive
    corner-stretch. ``projection_type`` is passed to
    :meth:`SICDType.project_image_to_ground_geo` (``"HAE"`` flat-earth,
    ``"PLANE"``, or ``"DEM"``).

    ``shape`` is the shape of the array being warped and ``origin`` its
    ``(row, col)`` position in the full image, so a clipped read
    (:func:`_clip_window`) is projected with the *scene's* image coordinates
    while the GCP rows/cols stay relative to the array itself — which is what
    the warp indexes.
    """
    np = _require("numpy")
    from rasterio.control import GroundControlPoint  # noqa: PLC0415

    rows, cols = shape
    row0, col0 = origin
    row_idx = _grid_indices(rows, grid)
    col_idx = _grid_indices(cols, grid)
    im_points = np.array([[r, c] for r in row_idx for c in col_idx], dtype="float64")
    # ordering="latlong" -> columns are [lat, lon, hae]; project on the scene's
    # height plane so a whole flat scene lands in the right place.
    ground = sicd.project_image_to_ground_geo(
        im_points + np.array([row0, col0], dtype="float64"),
        ordering="latlong",
        projection_type=projection_type,
    )
    gcps = []
    for (row, col), (lat, lon, hae) in zip(im_points, ground, strict=True):
        gcps.append(
            GroundControlPoint(
                row=float(row), col=float(col), x=float(lon), y=float(lat), z=float(hae)
            )
        )
    return gcps


@dataclass(frozen=True)
class ClipSavings:
    """What a ``bbox`` clip read instead of the whole scene.

    :func:`sicd_to_geocoded_cog` with a ``bbox`` turns a ground rectangle into the
    image window covering it (:func:`_clip_window`) and reads only that window, so
    the scene-sized amplitude array, the warp over it and the scene-sized output on
    disk never exist. This prices that saving: ``window`` is the pixels actually
    read, ``scene`` the pixels the whole product holds, and :attr:`fraction` the
    ratio -- the number that says whether pointing the conversion at an area of
    interest was worth it, at the moment someone is deciding whether to.

    The *download* is whole-product either way (a slant-plane NITF has no map grid
    to range-read), so this is the processing saving, not the bytes fetched -- the
    same distinction :func:`sicd_to_geocoded_cog`'s docstring draws. It is reported
    (``umbra convert``'s ``clipped`` line, via the ``clip_report`` callback) rather
    than recorded in the ``UMBRA_*`` tags: the output's geotransform already states
    which ground it covers, and a tag would make a clipped and an unclipped
    conversion of one site disagree on a provenance key for no measurement reason.
    """

    window_rows: int
    window_cols: int
    scene_rows: int
    scene_cols: int

    @property
    def window_pixels(self) -> int:
        """Pixels read from the product (the clipped image window)."""
        return self.window_rows * self.window_cols

    @property
    def scene_pixels(self) -> int:
        """Pixels the whole product holds (what an unclipped run would read)."""
        return self.scene_rows * self.scene_cols

    @property
    def fraction(self) -> float:
        """The window as a fraction of the scene in ``[0, 1]`` (``0`` if empty)."""
        return self.window_pixels / self.scene_pixels if self.scene_pixels else 0.0

    def to_dict(self) -> dict[str, Any]:
        """A JSON-ready mapping of the two figures and their ratio."""
        return {
            "window_pixels": self.window_pixels,
            "scene_pixels": self.scene_pixels,
            "window_shape": [self.window_rows, self.window_cols],
            "scene_shape": [self.scene_rows, self.scene_cols],
            "fraction": self.fraction,
        }


def _clip_window(
    sicd: Any,
    shape: tuple[int, int],
    bbox: tuple[float, float, float, float],
    *,
    projection_type: str = "HAE",
    grid: int = 33,
) -> tuple[int, int, int, int]:
    """The image-space window ``(row0, row1, col0, col1)`` covering a lon/lat bbox.

    A SICD is stored in the radar's slant plane, so an area of interest on the
    ground is a *rotated, sheared* region of the image — there is no bbox to
    read directly. This finds the smallest axis-aligned image window that
    contains it, by projecting a ``grid``×``grid`` lattice to ground (the same
    projection :func:`_build_gcps` uses) and keeping every lattice **cell**
    whose four corners' extent touches the requested bbox. Cells rather than
    points, so an area of interest smaller than one lattice step is still found.

    The window is a deliberate superset: it is padded by one lattice step on
    every side, and the geocoded output is cropped to the requested bbox
    afterwards (``bounds=`` on :func:`_warp_gcps_to_cog`). The search runs on
    the flat-earth projection even when a DEM is given — terrain moves a point
    on the ground by far less than the padding, and being generous here costs
    a few extra image columns, while being tight would silently clip the edge
    of the area someone asked for.

    Returns a half-open window (``row1`` / ``col1`` are exclusive), and raises
    when the bbox misses the scene entirely.
    """
    np = _require("numpy")

    west, south, east, north = (float(v) for v in bbox)
    if east <= west or north <= south:
        raise ValueError(
            f"bbox must be (min_lon, min_lat, max_lon, max_lat) with a positive "
            f"extent, got {tuple(bbox)!r}."
        )

    rows, cols = shape
    row_idx = _grid_indices(rows, grid)
    col_idx = _grid_indices(cols, grid)
    im_points = np.array([[r, c] for r in row_idx for c in col_idx], dtype="float64")
    ground = np.asarray(
        sicd.project_image_to_ground_geo(
            im_points, ordering="latlong", projection_type=projection_type
        ),
        dtype="float64",
    )
    lat = ground[:, 0].reshape(len(row_idx), len(col_idx))
    lon = ground[:, 1].reshape(len(row_idx), len(col_idx))

    def _cells(idx: list[int]) -> list[tuple[int, int]]:
        """Consecutive lattice pairs along one axis (a 1-wide axis is one cell)."""
        if len(idx) == 1:
            return [(0, 0)]
        return [(i, i + 1) for i in range(len(idx) - 1)]

    hits: list[tuple[int, int, int, int]] = []
    for i0, i1 in _cells(row_idx):
        for j0, j1 in _cells(col_idx):
            corner_lon = lon[[i0, i0, i1, i1], [j0, j1, j0, j1]]
            corner_lat = lat[[i0, i0, i1, i1], [j0, j1, j0, j1]]
            if (
                corner_lon.max() >= west
                and corner_lon.min() <= east
                and corner_lat.max() >= south
                and corner_lat.min() <= north
            ):
                hits.append((i0, i1, j0, j1))
    if not hits:
        scene = _scene_geo_bbox(sicd, shape)
        raise ValueError(
            f"The requested bbox {(west, south, east, north)} does not overlap the "
            f"scene, whose footprint is about {tuple(round(v, 6) for v in scene)}. "
            "Nothing would be left to convert."
        )

    row_step = max(1, int(np.ceil((rows - 1) / max(1, len(row_idx) - 1))))
    col_step = max(1, int(np.ceil((cols - 1) / max(1, len(col_idx) - 1))))
    row0 = max(0, min(row_idx[i0] for i0, _, _, _ in hits) - row_step)
    row1 = min(rows, max(row_idx[i1] for _, i1, _, _ in hits) + 1 + row_step)
    col0 = max(0, min(col_idx[j0] for _, _, j0, _ in hits) - col_step)
    col1 = min(cols, max(col_idx[j1] for _, _, _, j1 in hits) + 1 + col_step)
    return row0, row1, col0, col1


def _reader_shape(reader: Any, sicd: Any) -> tuple[int, int]:
    """The full image shape ``(rows, cols)`` of an open complex reader.

    Clipping has to know the image size *before* reading it — the whole point
    is not to read all of it. sarpy exposes ``data_size``; the SICD's own
    ``ImageData`` is the fallback, and either one being absent is an error
    rather than a guess, because guessing wrong would silently convert the
    wrong part of the scene.
    """
    size = getattr(reader, "data_size", None)
    if size is not None and len(size) and isinstance(size[0], (tuple, list)):
        size = size[0]  # a multi-image reader reports one shape per image
    if size is not None and len(size) >= 2:
        return int(size[0]), int(size[1])
    image_data = getattr(sicd, "ImageData", None)
    rows = getattr(image_data, "NumRows", None)
    cols = getattr(image_data, "NumCols", None)
    if rows is None or cols is None:
        raise ValueError(
            "Cannot determine the SICD image size, which clipping needs in order to "
            "read only part of it (no reader data_size and no ImageData.NumRows / "
            "NumCols). Convert without bbox= to read the whole scene."
        )
    return int(rows), int(cols)


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
    standard approximation for a scene-scale flattening.

    A product that does not state it raises
    :class:`~umbra_py.exceptions.UnsupportedMeasurementError` — the same family
    as the ``Radiometric`` refusals, because it is the same kind of fact: what
    the flattening needs is in the file or it is not, and the honest responses
    are a different setting or a different scene rather than a fix the caller
    can make to the request. Naming it is what lets ``umbra chips
    --skip-unsupported`` carry a batch past it and ``umbra preflight --rtc`` find
    it out over the wire.
    """
    scpcoa = getattr(sicd, "SCPCOA", None)
    inc = getattr(scpcoa, "IncidenceAng", None)
    az = getattr(scpcoa, "AzimAng", None)
    if inc is None or az is None:
        raise UnsupportedMeasurementError(
            "SICD is missing SCPCOA.IncidenceAng / SCPCOA.AzimAng, which "
            "radiometric terrain flattening (--rtc) needs for the look geometry: "
            "the local incidence angle is the scene-centre geometry tilted by the "
            "DEM's own slope, so without the first there is nothing to tilt.",
            hint="Convert without --rtc for the unflattened amplitude image, or "
            "pick a product whose metadata states its collection geometry.",
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


def _scene_geo_bbox(
    sicd: Any, shape: tuple[int, int], *, origin: tuple[int, int] = (0, 0)
) -> tuple[float, float, float, float]:
    """Geographic bbox ``(west, south, east, north)`` of the scene's image corners.

    Projects the four image corners onto the scene height plane with SICD's own
    model, so :func:`umbra_py.dem.fetch_dem_for_bbox` knows which Copernicus DEM
    tiles to pull for ``dem="auto"``. A coarse footprint is all the tile resolver
    needs (tiles are 1° cells), so four corners suffice.

    ``shape`` and ``origin`` describe the region actually being converted, so a
    clipped conversion fetches the tiles covering the *clip* rather than the
    whole scene.
    """
    np = _require("numpy")

    rows, cols = shape
    row0, col0 = origin
    corners = np.array(
        [[0, 0], [0, cols - 1], [rows - 1, 0], [rows - 1, cols - 1]], dtype="float64"
    ) + np.array([row0, col0], dtype="float64")
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
    origin: tuple[int, int] = (0, 0),
) -> list[GroundControlPoint]:
    """Terrain-orthorectified ground control points for :func:`_warp_gcps_to_cog`.

    Like :func:`_build_gcps`, but each lattice point is walked onto the DEM
    surface by :func:`_refine_gcps_with_dem` (via the injectable
    ``sample_height``) instead of projected onto a single flat height plane, so
    the warp reproduces the true ground position over relief. The refined terrain
    height is carried as the GCP ``z``. ``origin`` places a clipped read inside
    the full image, exactly as in :func:`_build_gcps`.
    """
    np = _require("numpy")
    from rasterio.control import GroundControlPoint  # noqa: PLC0415

    rows, cols = shape
    row0, col0 = origin
    row_idx = _grid_indices(rows, grid)
    col_idx = _grid_indices(cols, grid)
    im_points = np.array([[r, c] for r in row_idx for c in col_idx], dtype="float64")
    project = _sicd_projector(sicd)
    lats, lons, haes = _refine_gcps_with_dem(
        im_points + np.array([row0, col0], dtype="float64"), project, sample_height, h0=h0
    )
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
    tags: dict[str, str] | None = None,
    bounds: tuple[float, float, float, float] | None = None,
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

    ``tags``, if given, are written as dataset metadata (see
    :func:`conversion_tags`) before the COG copy, so the provenance is carried
    by the emitted file rather than only by the caller.

    ``bounds``, if given, is a ``(west, south, east, north)`` window the output
    grid is restricted to — intersected with the GCP extent, so asking for more
    ground than the scene covers yields the overlap rather than a margin of
    nodata. The pixel size is still derived from the *whole* input, so clipping
    changes which ground is written and not how finely it is sampled.
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

    if bounds is not None:
        west, south, east, north = (float(v) for v in bounds)
        minx, miny = max(minx, west), max(miny, south)
        maxx, maxy = min(maxx, east), min(maxy, north)
        span_x, span_y = maxx - minx, maxy - miny
        if span_x <= 0 or span_y <= 0:
            raise ValueError(
                f"The requested clip {(west, south, east, north)} does not overlap the "
                "ground the control points cover, so there is nothing to write."
            )

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
            if tags:
                tmp.update_tags(**tags)
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
    noise_subtract: bool = False,
    noise_model: str = "measured",
    speckle_filter: str | None = None,
    speckle_window: int = SPECKLE_WINDOW_DEFAULT,
    bbox: tuple[float, float, float, float] | None = None,
    clip_report: Callable[[ClipSavings], None] | None = None,
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

    Pass ``bbox`` to convert only an **area of interest**. A SICD scene is tens
    of square kilometres at 16–25 cm, so converting all of it to keep a corner
    of it costs the whole warp, a scene-sized float raster in memory, and a
    scene-sized COG on disk. With ``bbox`` only the image window covering that
    ground is read from the product, everything downstream is sized to it, and
    the output is cropped to the requested rectangle — so the cost of a
    conversion follows the area someone asked for rather than the area the
    satellite happened to collect. The download is whole-product either way (a
    slant-plane NITF has no map grid to range-read), which is why the saving is
    in the processing rather than in the bytes fetched. Pass ``clip_report`` to
    learn how large that saving was: it is called once, before the read, with a
    :class:`ClipSavings` pricing the window read against the whole scene (what
    ``umbra convert``'s ``clipped`` line prints).

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
    noise_subtract:
        If true, subtract the receiver's own thermal-noise floor — the SICD's
        ``Radiometric.NoiseLevel.NoisePoly``, evaluated per pixel — from the
        detected power before anything scales it. A measured pixel is the
        ground's echo *plus* that floor, and over a low-backscatter surface
        (calm water, radar shadow, dry sand) the floor is most of it, so an
        uncorrected value there reports the sensor's sensitivity as if it were
        the scene's brightness — and, because the floor varies across the swath,
        varies with the geometry rather than the ground. Subtraction is the one
        correction here that is not a multiplicative factor, so it is applied
        first, in image space, on raw power: ``calibration`` and ``rtc`` then
        scale what is left.

        ``False`` (the default) leaves the noise floor in, which is what an
        uncalibrated relative image already assumes. Where the floor comes from
        is ``noise_model``.
    noise_model:
        Where the subtracted floor comes from, one of :data:`NOISE_MODELS`.

        ``"measured"`` (the default) reads the product's own
        ``Radiometric.NoiseLevel.NoisePoly``, evaluated per pixel, so the floor
        tracks the across-swath variation the sensor's metadata describes. Only
        an ``ABSOLUTE`` noise level qualifies: a ``RELATIVE`` one describes the
        floor's shape without its level, and a product may declare no noise
        level at all; both raise a self-describing error rather than subtracting
        a guess, and :func:`sicd_noise_level` reports which case a file is in
        before you ask.

        ``"estimated"`` infers one constant floor from the scene's own detected
        power — its :data:`NOISE_ESTIMATE_PERCENTILE`-th percentile, on the
        argument that a SAR scene's darkest surfaces (calm water, radar shadow,
        smooth roads) return essentially nothing, so what is recorded there is
        the receiver. It needs no metadata, which is the whole point: Umbra's
        open products generally ship without a ``Radiometric`` block, so
        ``"measured"`` refuses on most of the archive this library exists for.
        The trade is real and is why the models are named separately — the
        estimate is one scalar, so it cannot follow the swath, and it assumes the
        scene contains dark ground at all (over imagery that is bright everywhere
        it removes signal).

        ``"estimated-range"`` answers the first half of that trade. It takes the
        same low-tail read *per range line* — SICD stores range along the image
        rows — and fits those per-line floors against range
        (:func:`_estimate_noise_profile`), so the subtracted floor follows the
        swath the way the measured one does while still needing no metadata. The
        fit is what makes it work on real imagery: it interpolates over the lines
        that had no dark ground to read, and it drops the lines whose tail sits
        far above it, since bright ground can only push a line's low tail up. It
        remains an inference and assumes the scene has dark ground *somewhere*
        along range, so it is recorded as its own value rather than as a better
        ``"estimated"``, and it reports the swing it found in
        ``UMBRA_NOISE_FLOOR_SPREAD_DB`` — near zero means there was nothing here
        the constant model was missing.

        The raster records which one ran (``UMBRA_NOISE_SUBTRACTION`` of
        ``"absolute"``, ``"estimated"`` or ``"estimated-range"``, plus
        ``UMBRA_NOISE_FLOOR_DB`` for the inferred ones), and
        :func:`umbra_py.load.to_stack` refuses to difference a series that mixes
        any two of them.
    speckle_filter:
        Optional speckle filter, one of :data:`SPECKLE_FILTERS`, applied to
        detected power in image space. Speckle is not sensor noise and no floor
        subtraction touches it: it is the interference pattern coherent
        illumination produces on a rough surface, so a single-look pixel's power
        is exponentially distributed about the surface's true backscatter — its
        standard deviation *equals* its mean. That is why a single Umbra pixel is
        a poor measurement of a surface even after calibration, why a
        pixel-by-pixel difference between two passes is dominated by speckle
        rather than by change, and why every SAR workflow averages before it
        measures. Averaging is also the only correction available, because
        speckle is a property of the illumination rather than an additive error to
        remove.

        ``"boxcar"`` averages the window unconditionally — the multilook, maximum
        variance reduction, blind to edges. ``"lee"`` averages only where the
        window's variability is what speckle alone would produce and keeps the
        pixel where it is more variable than that (Lee 1980), so edges and points
        survive at the cost of smoothing less. Both run in the power domain, after
        the noise subtraction and the calibration: the noise estimators read the
        scene's own low tail, whose *distribution* a filter would narrow, and a
        smooth multiplicative scale factor commutes with a local average anyway,
        so the filter goes last in image space — before the warp, which is where
        the pixel grid stops being the radar's.

        What it costs is resolution, and that cost is the reason it is opt-in: a
        window that averages ``N`` pixels reports ground ``N`` pixels across. The
        raster records the filter and its window (``UMBRA_SPECKLE_FILTER`` /
        ``UMBRA_SPECKLE_WINDOW``) and :func:`umbra_py.load.to_stack` refuses to
        difference a series that mixes two of them, since the smoothing would
        otherwise be read as change. It also records what the filter *achieved* on
        this scene — the equivalent number of looks before and after
        (``UMBRA_SPECKLE_ENL_BEFORE`` / ``_AFTER``), which is the honest answer to
        "how much speckle did that remove?" and is generally well below the
        window's pixel count, because Umbra samples finer than it resolves.
    speckle_window:
        Edge of the odd, centred window ``speckle_filter`` averages over
        (:data:`SPECKLE_WINDOW_DEFAULT`). Larger windows remove more speckle and
        more detail; the cost is independent of the size
        (:func:`_box_sum`), so the choice is about the imagery rather than the
        runtime. Ignored when ``speckle_filter`` is ``None``.
    bbox:
        Optional area of interest ``(min_lon, min_lat, max_lon, max_lat)`` in
        WGS-84 degrees. Only the image window covering that ground is read,
        amplitude-detected, calibrated, projected and warped, and the output is
        cropped to the rectangle (intersected with the scene, so asking for more
        ground than the scene holds returns the overlap rather than a nodata
        margin). Because a SICD lies in the slant plane, the window found is a
        deliberate superset — the smallest axis-aligned image rectangle that
        contains the rotated ground region, padded by one search step — so the
        pixels read exceed the area kept, and both are a small fraction of the
        scene for a small area of interest. ``None`` (the default) converts the
        whole scene. The clip is *not* recorded in the provenance tags: the
        output's own geotransform states exactly which ground it covers, and the
        tags record what a pixel value means rather than where it is.
    clip_report:
        Optional callback, invoked once with a :class:`ClipSavings` when a
        ``bbox`` clip is applied (never otherwise), pricing the image window read
        against the whole scene. It is how ``umbra convert`` prints its
        ``clipped`` line; a caller who does not pass it sees no behaviour change.
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
    if noise_model not in NOISE_MODELS:
        raise ValueError(
            f"Unknown noise_model {noise_model!r}; choose one of {', '.join(NOISE_MODELS)}."
        )
    if speckle_filter is not None and speckle_filter not in SPECKLE_FILTERS:
        raise ValueError(
            f"Unknown speckle_filter {speckle_filter!r}; choose one of "
            f"{', '.join(SPECKLE_FILTERS)}."
        )
    if speckle_filter is not None:
        # Before the read: an unusable window is worth finding out about without
        # first pulling a multi-gigabyte scene through the amplitude detection.
        _check_speckle_window(speckle_window)

    reader = open_complex(str(src))
    sicd = reader.get_sicds_as_tuple()[0]
    # Before the read, for the same reason the speckle window is checked before
    # it: a product that cannot support the measurement should say so without
    # first being pulled through the amplitude detection whole. `rtc` is asked
    # here rather than at the warp for the stronger version of the same reason --
    # the flattening runs after the read *and* after the reprojection.
    _check_measurement_support(
        sicd,
        calibration=calibration,
        noise_subtract=noise_subtract,
        noise_model=noise_model,
        rtc=rtc,
    )
    if bbox is None:
        origin = (0, 0)
        amplitude = _amplitude(reader[:, :], decibels=decibels)
    else:
        # Ask which image window covers the ground first, then read only that --
        # the point of a clip is that the scene-sized array never exists.
        scene_shape = _reader_shape(reader, sicd)
        row0, row1, col0, col1 = _clip_window(
            sicd,
            scene_shape,
            bbox,
            # Flat-earth for the *search* even with a DEM: the padding covers the
            # terrain shift, and the refinement loop below still places the pixels.
            projection_type="HAE" if dem is not None else projection_type,
        )
        if clip_report is not None:
            # Priced before the read, off the window just computed and the scene
            # shape already in hand -- so a caller learns what the clip saved
            # whether or not the (whole-product) download or the warp then run.
            clip_report(
                ClipSavings(
                    window_rows=row1 - row0,
                    window_cols=col1 - col0,
                    scene_rows=scene_shape[0],
                    scene_cols=scene_shape[1],
                )
            )
        origin = (row0, col0)
        amplitude = _amplitude(reader[row0:row1, col0:col1], decibels=decibels)
    noise: NoiseSubtraction | None = None
    if noise_subtract:
        # First, and in image space: the noise floor is additive in power and is
        # a polynomial in image coordinates, so it comes off the raw detected
        # power before the multiplicative corrections scale what is left. Its
        # diagnostics are therefore counted over the image window read, which is
        # the population the correction actually saw.
        amplitude, noise = _denoise_amplitude(
            sicd, amplitude, decibels=decibels, origin=origin, model=noise_model
        )
    if calibration is not None:
        # In image space: the SF polynomials are functions of image coordinates,
        # so calibrate before the warp resamples the grid away.
        amplitude = _calibrate_amplitude(
            sicd, amplitude, kind=calibration, decibels=decibels, origin=origin
        )
    speckle: SpeckleFiltering | None = None
    if speckle_filter is not None:
        # Last in image space, and before the warp: the window has to be square
        # in the *radar's* grid (where speckle is one sample per pixel and
        # independent of its neighbours), not in the resampled ground grid, where
        # neighbouring pixels can be interpolations of the same measurement.
        amplitude, speckle = _filter_speckle(
            amplitude, decibels=decibels, name=speckle_filter, window=speckle_window
        )
    if isinstance(dem, str) and dem.lower() == "auto":
        from . import dem as dem_mod  # noqa: PLC0415

        dem = dem_mod.fetch_dem_for_bbox(_scene_geo_bbox(sicd, amplitude.shape, origin=origin))
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
                origin=origin,
            )
    elif geoid is not None:
        raise ValueError(
            "geoid= requires dem=: the geoid correction adjusts DEM heights to "
            "ellipsoidal (HAE), so pass a DEM to terrain-orthorectify against."
        )
    else:
        gcps = _build_gcps(
            sicd,
            amplitude.shape,
            grid=gcp_grid,
            projection_type=projection_type,
            origin=origin,
        )

    post_warp = None
    reference_deg = None
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
        bounds=bbox,
        tags=conversion_tags(
            source=src,
            geocoded=True,
            decibels=decibels,
            calibration=calibration,
            noise_subtraction=_NOISE_PROVENANCE[noise_model] if noise_subtract else None,
            **_noise_tag_values(noise),
            **_speckle_tag_values(speckle),
            rtc_model=rtc_model if rtc else None,
            rtc_reference_deg=reference_deg,
            projection_type=projection_type,
            dem=dem,
            geoid=geoid,
            resampling=resampling,
        ),
    )
