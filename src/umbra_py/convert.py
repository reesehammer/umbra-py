"""Format conversion helpers (optional, requires the ``convert`` extra).

Umbra's ``GEC`` asset is already a geocoded cloud-optimized GeoTIFF and needs no
conversion. The complex products (``SICD``/``CPHD``) live in the radar slant
plane; full geocoding of those is out of scope for v1. This module provides a
well-defined first step: extracting a detected amplitude image from a SICD into
a GeoTIFF for quick inspection.

Install with: ``pip install "umbra-py[convert]"``
"""

from __future__ import annotations

import os
from pathlib import Path

from .exceptions import MissingDependencyError


def _require(module: str):
    try:
        return __import__(module)
    except ImportError as exc:  # pragma: no cover - exercised only without extra
        raise MissingDependencyError(
            f"'{module}' is required for conversion. "
            'Install the extra with: pip install "umbra-py[convert]"'
        ) from exc


def sicd_to_amplitude_geotiff(
    src: str | os.PathLike,
    dst: str | os.PathLike,
    *,
    decibels: bool = True,
) -> Path:
    """Read a SICD (complex) image and write its detected amplitude as a GeoTIFF.

    This is an inspection-quality product in the slant plane: the output is
    *not* geocoded. For geocoded imagery use the item's ``GEC`` asset directly.

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
    np = _require("numpy")
    import rasterio  # noqa: PLC0415
    from rasterio.transform import from_origin  # noqa: PLC0415
    from sarpy.io.complex.converter import open_complex  # noqa: PLC0415

    reader = open_complex(str(src))
    complex_data = reader[:, :]
    amplitude = np.abs(complex_data).astype("float32")
    if decibels:
        amplitude = 20.0 * np.log10(np.clip(amplitude, 1e-6, None))

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
