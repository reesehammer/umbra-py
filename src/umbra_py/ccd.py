"""Coherent change detection (CCD) for Umbra ``SICD`` pairs.

The amplitude change products in :mod:`umbra_py.viz` (``change_composite``,
``timescan_composite``) compare how *bright* a scene is between passes. CCD
asks a different, sharper question: did the ground itself stay physically
undisturbed? Two complex SAR images of the same scene, collected from the same
geometry, have a near-identical speckle phase pattern -- unless something at the
surface moved. The normalized complex cross-correlation of the two images, the
**coherence** ``|gamma|`` in ``[0, 1]``, measures exactly that:

- ``|gamma| -> 1`` : the surface is unchanged between passes (high coherence).
- ``|gamma| -> 0`` : the surface decorrelated -- a vehicle drove through, earth
  was turned, foliage or water moved (low coherence), *or* the return was too
  weak to be coherent (shadow, smooth water, noise).

That decorrelation reveals sub-resolution disturbance -- tire tracks, footpaths,
freshly dug soil -- that leaves no signature in amplitude at all. It is the one
SAR product a general-purpose GIS pipeline cannot reproduce, because it needs
the preserved phase that only the complex ``SICD`` product carries.

Scope (v1)
----------
Coherence is estimated in the image (slant) plane, not geocoded. The pair is
assumed to come from the **same collection geometry** (an Umbra repeat-pass of
one site); the two images are co-registered by correcting a single global
sub-pixel translation -- the dominant misregistration for a small, high-
resolution scene. Spatially varying misregistration (terrain parallax, large
rotations) is not modeled. Coherence is sensitive to even a fraction of a pixel
of misregistration, so the sub-pixel step is not optional -- it is done by
default.

Install with: ``pip install "umbra-py[convert,viz]"`` (``convert`` reads the
SICD via ``sarpy``; ``viz`` renders the map).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .exceptions import MissingDependencyError


def _require(module: str):
    try:
        return __import__(module)
    except ImportError as exc:  # pragma: no cover - exercised only without extras
        raise MissingDependencyError(
            f"'{module}' is required for coherent change detection. Install the "
            'extras with: pip install "umbra-py[convert,viz]"'
        ) from exc


def _box_sum(a: Any, window: int) -> Any:
    """Sum each pixel's ``window x window`` neighbourhood, same output shape.

    An integral-image (summed-area table) so the cost is independent of the
    window size. Windows are clipped at the image edge; since the coherence
    ratio applies the *same* clipped window to its numerator and denominator,
    the edge bias cancels and ``|gamma|`` stays in ``[0, 1]`` by
    Cauchy-Schwarz. Works on real or complex input.
    """
    np = _require("numpy")
    a = np.asarray(a)
    r = window // 2
    h, w = a.shape
    ii = np.pad(np.cumsum(np.cumsum(a, axis=0), axis=1), ((1, 0), (1, 0)))
    i = np.arange(h)
    j = np.arange(w)
    r0 = np.clip(i - r, 0, h)[:, None]
    r1 = np.clip(i + r + 1, 0, h)[:, None]
    c0 = np.clip(j - r, 0, w)[None, :]
    c1 = np.clip(j + r + 1, 0, w)[None, :]
    return ii[r1, c1] - ii[r0, c1] - ii[r1, c0] + ii[r0, c0]


def _upsampled_dft(data: Any, region_size: int, upsample_factor: int, offsets: Any) -> Any:
    """Matrix-multiply DFT of ``data`` over a small, finely-sampled region.

    Evaluates the 2D DFT only on a ``region_size x region_size`` patch at
    ``1 / upsample_factor`` pixel spacing around ``offsets`` -- the kernel of
    the Guizar-Sicairos sub-pixel registration, refining the integer
    cross-correlation peak without a full upsampled FFT.
    """
    np = _require("numpy")
    im2pi = 1j * 2 * np.pi
    for n_items, offset in zip(data.shape[::-1], offsets[::-1], strict=True):
        kernel = (np.arange(region_size) - offset)[:, None] * np.fft.fftfreq(
            n_items, upsample_factor
        )
        kernel = np.exp(-im2pi * kernel)
        data = np.tensordot(kernel, data, axes=(1, -1))
    return data


def _register_shift(reference: Any, moving: Any, upsample: int) -> tuple[float, float]:
    """Estimate the sub-pixel shift aligning ``moving`` onto ``reference``.

    Phase cross-correlation: the cross-power spectrum's peak gives the integer
    offset, then an upsampled DFT around that peak refines it to
    ``1 / upsample`` of a pixel. Returns ``(dy, dx)`` to feed
    :func:`_fourier_shift`.
    """
    np = _require("numpy")
    src_freq = np.fft.fft2(reference)
    target_freq = np.fft.fft2(moving)
    shape = np.array(src_freq.shape)
    image_product = src_freq * target_freq.conj()
    correlation = np.fft.ifft2(image_product)
    maxima = np.array(np.unravel_index(np.argmax(np.abs(correlation)), correlation.shape), float)
    midpoint = np.fix(shape / 2)
    maxima[maxima > midpoint] -= shape[maxima > midpoint]
    if upsample <= 1:
        return float(maxima[0]), float(maxima[1])

    upsample = float(upsample)
    shift = np.round(maxima * upsample) / upsample
    region = int(np.ceil(upsample * 1.5))
    dftshift = np.fix(region / 2.0)
    offsets = dftshift - shift * upsample
    upsampled = _upsampled_dft(image_product.conj(), region, upsample, offsets).conj()
    peak = np.array(np.unravel_index(np.argmax(np.abs(upsampled)), upsampled.shape), float)
    shift = shift + (peak - dftshift) / upsample
    return float(shift[0]), float(shift[1])


def _fourier_shift(arr: Any, shift: tuple[float, float]) -> Any:
    """Translate a complex image by a sub-pixel ``(dy, dx)`` via a phase ramp.

    Multiplying the spectrum by a linear phase is an exact, band-limited
    resampling that preserves the complex phase -- the right interpolator for
    coregistering SAR before a coherence estimate.
    """
    np = _require("numpy")
    dy, dx = shift
    ny, nx = arr.shape
    fy = np.fft.fftfreq(ny).reshape(-1, 1)
    fx = np.fft.fftfreq(nx).reshape(1, -1)
    ramp = np.exp(-2j * np.pi * (fy * dy + fx * dx))
    return np.fft.ifft2(np.fft.fft2(arr) * ramp)


def coherence(
    reference: Any,
    secondary: Any,
    *,
    window: int = 5,
    upsample: int = 10,
    register: bool = True,
) -> Any:
    """Estimate the coherence magnitude ``|gamma|`` between two complex images.

    ``reference`` and ``secondary`` are complex 2D arrays (e.g. read from a
    ``SICD`` pair of the same site). They are co-registered by a global
    sub-pixel translation (unless ``register`` is False), then coherence is
    estimated over a ``window x window`` boxcar::

        |gamma| = | sum(ref . conj(sec)) | / sqrt( sum|ref|^2 . sum|sec|^2 )

    Returns a ``float32`` array in ``[0, 1]`` on ``reference``'s grid: bright /
    near 1 where the surface is unchanged, dark / near 0 where it decorrelated
    (physical change, or an incoherent weak return). If the inputs differ in
    size they are cropped to their common top-left overlap; pass already-
    co-registered, equally sized arrays with ``register=False`` to skip the
    alignment step.
    """
    np = _require("numpy")
    ref = np.asarray(reference)
    sec = np.asarray(secondary)
    if ref.ndim != 2 or sec.ndim != 2:
        raise ValueError("coherence expects two 2D complex images.")
    if window < 1 or window % 2 == 0:
        raise ValueError(f"window must be a positive odd integer, got {window}.")
    if ref.shape != sec.shape:
        h = min(ref.shape[0], sec.shape[0])
        w = min(ref.shape[1], sec.shape[1])
        if h == 0 or w == 0:
            raise ValueError("images have no overlapping extent to compare.")
        ref = ref[:h, :w]
        sec = sec[:h, :w]

    ref = ref.astype(np.complex128, copy=False)
    sec = sec.astype(np.complex128, copy=False)
    if register:
        sec = _fourier_shift(sec, _register_shift(np.abs(ref), np.abs(sec), upsample))

    numerator = np.abs(_box_sum(ref * np.conj(sec), window))
    power = _box_sum(np.abs(ref) ** 2, window) * _box_sum(np.abs(sec) ** 2, window)
    with np.errstate(divide="ignore", invalid="ignore"):
        gamma = np.where(power > 0, numerator / np.sqrt(power), 0.0)
    return np.clip(gamma, 0.0, 1.0).astype("float32")


def _read_sicd(src: str | os.PathLike) -> Any:
    """Read a complex ``SICD`` NITF into a 2D array via ``sarpy``."""
    _require("sarpy")
    np = _require("numpy")
    from sarpy.io.complex.converter import open_complex  # noqa: PLC0415

    reader = open_complex(str(src))
    return np.asarray(reader[:, :])


def coherent_change(
    reference: str | os.PathLike,
    secondary: str | os.PathLike,
    *,
    window: int = 5,
    upsample: int = 10,
) -> Any:
    """Read two ``SICD`` files and return their coherence map.

    Convenience wrapper over :func:`coherence` that handles the ``sarpy`` read.
    The whole complex image of each is read into memory (SICDs are large; this
    matches ``sicd_to_amplitude_geotiff``), so expect the cost to scale with
    scene size. Returns a ``float32`` ``[0, 1]`` array; see :func:`coherence`
    for the interpretation.
    """
    ref = _read_sicd(reference)
    sec = _read_sicd(secondary)
    return coherence(ref, sec, window=window, upsample=upsample)


def _downsample(coh: Any, max_size: int) -> Any:
    """Area-resize a coherence map so its longer side is at most ``max_size``."""
    np = _require("numpy")
    _require("PIL")
    from PIL import Image  # noqa: PLC0415

    h, w = coh.shape
    scale = max(max(h, w) / max_size, 1.0)
    if scale <= 1.0:
        return coh
    img = Image.fromarray(coh.astype("float32"), mode="F")
    out = img.resize((max(int(w / scale), 1), max(int(h / scale), 1)), Image.BILINEAR)
    return np.asarray(out)


def ccd_image(coh: Any, *, colormap: str | None = None, invert: bool = False):
    """Render a coherence map to a ``PIL.Image``.

    By default coherence maps to brightness -- stable ground is bright, change
    is dark. ``invert`` flips that so disturbed ground stands out bright.
    ``colormap`` (any matplotlib name, e.g. ``"viridis"``) pseudo-colours the
    map instead of grayscale.
    """
    np = _require("numpy")
    _require("PIL")
    from PIL import Image  # noqa: PLC0415

    disp = np.clip(np.asarray(coh, dtype="float32"), 0.0, 1.0)
    if invert:
        disp = 1.0 - disp
    if colormap:
        from .viz import _apply_colormap  # noqa: PLC0415

        return Image.fromarray(_apply_colormap(disp, colormap), mode="RGB")
    return Image.fromarray((disp * 255.0).astype("uint8"), mode="L")


def save_ccd(
    reference: str | os.PathLike,
    secondary: str | os.PathLike,
    dest: str | os.PathLike,
    *,
    window: int = 5,
    upsample: int = 10,
    colormap: str | None = None,
    invert: bool = False,
    max_size: int | None = 2048,
) -> Path:
    """Compute a SICD-pair coherent change map and write it as an image.

    The output format follows ``dest``'s extension (``.png``, ``.jpg``, ...).
    ``max_size`` caps the written image's longer side (the coherence is still
    estimated at full resolution, then resized for display). See
    :func:`coherent_change` and :func:`ccd_image` for the options.
    """
    coh = coherent_change(reference, secondary, window=window, upsample=upsample)
    if max_size is not None:
        coh = _downsample(coh, max_size)
    image = ccd_image(coh, colormap=colormap, invert=invert)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    image.save(str(dest))
    return dest
