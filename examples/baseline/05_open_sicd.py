"""Open a SICD file and write an amplitude GeoTIFF for inspection.

Why this is here
----------------
Umbra's ``GEC`` asset is a cloud-optimized GeoTIFF you can drop straight into
QGIS / rioxarray / Earth Engine. The phase-preserving products are harder:

* ``SICD`` — Sensor Independent Complex Data, NITF container, slant-plane
  complex pixels. The native format for InSAR / coherence / phase analysis.
* ``CPHD`` — Compensated Phase History Data, raw signal before image
  formation.

Neither is opened by ``rasterio`` / GDAL out of the box on most systems.
Researchers reach for `sarpy` (https://github.com/ngageoint/sarpy), the
reference reader/writer for complex SAR formats.

A SICD file can be several gigabytes. ``reader[:, :]`` loads the entire
complex array into memory; for a quick look you usually decimate first
(``reader[::8, ::8]``) and convert to a real-valued amplitude image.

The output here is *not* geocoded — it lives in the slant plane and has no
spatial reference. Geocoding a SICD is a real piece of work (orthorectifying
against a DEM); umbra-py's roadmap calls it out as out of scope for v0.1 for
exactly that reason.

Requires::

    pip install sarpy rasterio numpy

Run::

    python 05_open_sicd.py path/to/foo_SICD.nitf  output.tif
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin
from sarpy.io.complex.converter import open_complex


def sicd_to_amplitude_geotiff(
    src: Path,
    dst: Path,
    *,
    decimate: int = 8,
    decibels: bool = True,
) -> Path:
    """Read ``src`` (a SICD NITF), build an amplitude image, write to ``dst``.

    ``decimate=8`` reads every 8th sample in each axis — a 64x reduction in
    data, fine for visual inspection. Drop to ``decimate=1`` only if you
    actually need full-resolution magnitude and your machine has the RAM.
    """
    reader = open_complex(str(src))

    # SICD files are usually a single image segment. For multi-segment files
    # you would loop over reader.get_image_size() per segment.
    complex_data = reader[::decimate, ::decimate]
    amplitude = np.abs(complex_data).astype("float32")

    if decibels:
        # 20*log10 magnitude, clipped to avoid log(0).
        amplitude = 20.0 * np.log10(np.clip(amplitude, 1e-6, None))

    rows, cols = amplitude.shape
    dst.parent.mkdir(parents=True, exist_ok=True)
    profile = {
        "driver": "GTiff",
        "height": rows,
        "width": cols,
        "count": 1,
        "dtype": "float32",
        "compress": "deflate",
        "tiled": True,
        # Identity transform: this is slant-plane, not geocoded.
        "transform": from_origin(0, 0, 1, 1),
    }
    with rasterio.open(dst, "w", **profile) as out:
        out.write(amplitude, 1)
    return dst


def main() -> None:
    if len(sys.argv) != 3:
        print("usage: python 05_open_sicd.py <input.nitf> <output.tif>")
        sys.exit(2)

    src = Path(sys.argv[1])
    dst = Path(sys.argv[2])
    out = sicd_to_amplitude_geotiff(src, dst)
    print(f"wrote {out} (slant-plane amplitude in dB, decimated 8x)")


if __name__ == "__main__":
    main()
