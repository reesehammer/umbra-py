"""Endpoints and well-known values for Umbra's open SAR data program.

Umbra publishes its open data under the AWS Open Data program. The data is
hosted in a public S3 bucket and indexed by a *static* STAC catalog (a tree of
``catalog.json`` files), not a STAC API search endpoint. See
https://registry.opendata.aws/umbra-open-data/ and
https://umbra.space/open-data/.
"""

from __future__ import annotations

#: Public, anonymously readable S3 bucket holding all Umbra open data.
S3_BUCKET = "umbra-open-data-catalog"

#: AWS region the bucket lives in.
S3_REGION = "us-west-2"

#: Root of the static STAC catalog. Traversal starts here.
DEFAULT_STAC_ROOT = f"https://s3.{S3_REGION}.amazonaws.com/{S3_BUCKET}/stac/catalog.json"

#: Canonical Umbra product types, ordered from most processed / easiest to use
#: (GEC, a cloud-optimized GeoTIFF) to most raw (CPHD). Different catalog
#: generations name their STAC assets differently (e.g. an explicit ``"GEC"``
#: key vs. a filename like ``..._MM.tif``), so :class:`umbra_py.models.UmbraItem`
#: classifies each asset into one of these rather than matching keys exactly.
#:
#: - ``GEC``  : Geocoded Ellipsoid Corrected image, a cloud-optimized GeoTIFF.
#: - ``CSI``  : Color Sub-aperture Image, a quick-look RGB GeoTIFF.
#: - ``SIDD`` : Sensor Independent Derived Data, a geocoded detected NITF image.
#: - ``SICD`` : Sensor Independent Complex Data, full complex data in slant plane.
#: - ``CPHD`` : Compensated Phase History Data, the raw signal phase history.
PRODUCT_ASSETS = ("GEC", "CSI", "SIDD", "SICD", "CPHD")

#: Canonical name for the per-acquisition metadata sidecar JSON.
METADATA_ASSET = "metadata"
ALL_ASSETS = (*PRODUCT_ASSETS, METADATA_ASSET)

#: License Umbra applies to all open data.
DATA_LICENSE = "CC-BY-4.0"

#: Suggested attribution string for derived products.
ATTRIBUTION = "Contains Umbra open data, licensed under CC BY 4.0."
