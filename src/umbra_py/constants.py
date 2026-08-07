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

#: Canopy is Umbra's authenticated commercial product. Unlike the open-data
#: bucket -- a *static* STAC catalog with no search endpoint -- Canopy exposes a
#: real STAC API ``/search`` over Umbra's full commercial archive. Passing a
#: ``token`` to :class:`umbra_py.UmbraCatalog` searches this endpoint (the same
#: ``search()`` interface, the same :class:`~umbra_py.UmbraItem` results) instead
#: of crawling the open bucket, so a user onboarded on the free data is already
#: holding the tool they'd use as a paying customer. Requires a Canopy account
#: and token; see https://docs.canopy.umbra.space/.
CANOPY_ARCHIVE_URL = "https://api.canopy.umbra.space/archive/search"

#: Environment variable the CLI reads a Canopy token from when ``--token`` is
#: not passed explicitly, so a token never has to appear in shell history.
CANOPY_TOKEN_ENV = "UMBRA_CANOPY_TOKEN"

#: OpenRouter (https://openrouter.ai) is an OpenAI-compatible model gateway: one
#: key reaches models from many providers over the same ``/chat/completions``
#: shape umbra-py's ``describe`` / ``change --narrate`` / ``ask`` already speak.
#: So it needs no new HTTP client -- only its host, a vision-capable default
#: model, and the two (optional) ranking headers OpenRouter asks callers to
#: send. A user sets ``OPENROUTER_API_KEY`` and the AI features route here;
#: ``OPENROUTER_BASE_URL`` overrides the host. Recognised *before* the generic
#: ``OPENAI_API_KEY`` so an explicit OpenRouter opt-in wins over a stray one.
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
#: OpenRouter's model ids are ``provider/model``; this vision-capable default is
#: the cheap one, overridable via ``UMBRA_NARRATE_MODEL`` / ``UMBRA_DESCRIBE_MODEL``
#: / ``UMBRA_ASK_MODEL`` (or ``--model``) -- e.g. ``anthropic/claude-3.5-sonnet``.
OPENROUTER_DEFAULT_MODEL = "openai/gpt-4o-mini"
#: The identifying headers OpenRouter uses to attribute traffic. Optional on
#: OpenRouter and ignored by other OpenAI-compatible hosts, so always safe to send.
OPENROUTER_HEADERS = {
    "HTTP-Referer": "https://github.com/reesehammer/umbra-py",
    "X-Title": "umbra-py",
}

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

#: One-line, plain-language explanation of each product type. The docstring on
#: :data:`PRODUCT_ASSETS` above documents these for humans reading the source;
#: this table is the machine-readable version a language model (or an
#: :meth:`umbra_py.UmbraItem.to_llm_context` card) pulls in so it can reason
#: about which product to ask for without external SAR literacy.
PRODUCT_TYPE_EXPLANATIONS: dict[str, str] = {
    "GEC": (
        "Geocoded Ellipsoid Corrected image: a cloud-optimized GeoTIFF, "
        "map-projected and analysis-ready. The easiest product to use and the "
        "usual starting point."
    ),
    "CSI": (
        "Color Sub-aperture Image: a quick-look RGB GeoTIFF that colorizes "
        "sub-aperture/frequency content. Good for eyeballing a scene, not for "
        "radiometric measurement."
    ),
    "SIDD": (
        "Sensor Independent Derived Data: a geocoded, detected (amplitude) "
        "image in NITF. Map-projected like GEC but in the standard NGA format."
    ),
    "SICD": (
        "Sensor Independent Complex Data: full complex data in the slant "
        "plane (not map-projected). Needed for interferometry and advanced "
        "processing; not a display image."
    ),
    "CPHD": (
        "Compensated Phase History Data: the raw signal phase history, the "
        "least-processed product. For signal-level work, not for viewing."
    ),
}

#: Canonical SAR polarization codes, transmit-then-receive. Umbra's open
#: products are single-polarization per acquisition, so an item usually exposes
#: exactly one of these on ``sar:polarizations``. Like :data:`PRODUCT_ASSETS`
#: this is a *closed, known* set, which is what lets a whole-archive surface
#: (the ``umbra demo --pmtiles`` explorer) offer the full facet without first
#: scanning a sample of the catalog and risking a missing chip.
POLARIZATIONS = ("VV", "VH", "HH", "HV")

#: The caveat that must travel with an item's polarizations whenever a model
#: might reason about change detection. Two acquisitions in different
#: polarizations image different scattering physics and must not be differenced.
POLARIZATION_CAVEAT = (
    "Polarizations are not interchangeable: an HH scene and a VV scene of the "
    "same place measure different scattering and must not be differenced for "
    "change detection. Compare like polarization with like."
)

#: Canonical name for the per-acquisition metadata sidecar JSON.
METADATA_ASSET = "metadata"
ALL_ASSETS = (*PRODUCT_ASSETS, METADATA_ASSET)

#: GitHub repository that hosts this project and its rolling catalog snapshot.
GITHUB_REPO = "reesehammer/umbra-py"

#: Rolling GitHub release tag carrying the weekly-rebuilt catalog snapshots
#: (see ``.github/workflows/publish-index.yml``).
CATALOG_INDEX_RELEASE = "catalog-index"

#: Name of the prebuilt SQLite index asset on the ``catalog-index`` release.
CATALOG_DB_ASSET = "catalog.db"

#: Name of the prebuilt whole-catalog PMTiles basemap asset on the same release
#: (the vector tile pyramid ``umbra tiles`` builds -- published so a fresh
#: install gets a whole-catalog map with no local tiling step).
CATALOG_PMTILES_ASSET = "catalog.pmtiles"

#: Name of the prebuilt thumbnail sidecar asset on the same release (the baked
#: per-acquisition quicklook PNGs ``umbra index bake-thumbnails`` renders --
#: published as a *separate* file rather than inside ``catalog.db`` so the index
#: every ``--local`` search fetches stays small, and the pixels are opt-in.
CATALOG_THUMBS_ASSET = "catalog.thumbs.db"

#: Name of the prebuilt scene-embedding sidecar asset on the same release (the
#: per-acquisition quicklook vectors ``umbra embed build`` produces -- published
#: so a fresh install gets visual similarity search over the archive with no
#: rebuild and no embedding key). Unlike ``catalog.db`` / ``catalog.pmtiles`` this
#: artifact is model-derived and model-specific, so the embedding model is
#: recorded prominently on the release (see ``.github/workflows/publish-index.yml``).
CATALOG_EMBED_ASSET = "catalog.embed.db"

#: Stable download URL for the prebuilt SQLite index. GitHub redirects this
#: ``/releases/download/<tag>/<asset>`` path to the current asset, so it always
#: points at the latest weekly snapshot without an API call.
CATALOG_INDEX_DB_URL = (
    f"https://github.com/{GITHUB_REPO}/releases/download/{CATALOG_INDEX_RELEASE}/{CATALOG_DB_ASSET}"
)

#: Stable download URL for the prebuilt whole-catalog PMTiles basemap (same
#: rolling-release redirect discipline as :data:`CATALOG_INDEX_DB_URL`).
CATALOG_INDEX_PMTILES_URL = (
    f"https://github.com/{GITHUB_REPO}/releases/download/"
    f"{CATALOG_INDEX_RELEASE}/{CATALOG_PMTILES_ASSET}"
)

#: Stable download URL for the prebuilt thumbnail sidecar (same rolling-release
#: redirect discipline as :data:`CATALOG_INDEX_DB_URL`).
CATALOG_INDEX_THUMBS_URL = (
    f"https://github.com/{GITHUB_REPO}/releases/download/"
    f"{CATALOG_INDEX_RELEASE}/{CATALOG_THUMBS_ASSET}"
)

#: Stable download URL for the prebuilt scene-embedding sidecar (same
#: rolling-release redirect discipline as :data:`CATALOG_INDEX_DB_URL`).
CATALOG_INDEX_EMBED_URL = (
    f"https://github.com/{GITHUB_REPO}/releases/download/"
    f"{CATALOG_INDEX_RELEASE}/{CATALOG_EMBED_ASSET}"
)

#: License Umbra applies to all open data.
DATA_LICENSE = "CC-BY-4.0"

#: Suggested attribution string for derived products.
ATTRIBUTION = "Contains Umbra open data, licensed under CC BY 4.0."

#: Provenance note that must travel with any model-generated interpretation of
#: the data (a scene description, a change narration). It marks the text as an
#: AI reading of the imagery -- not a measurement -- so a downstream reader never
#: mistakes the narration for ground truth. The same license discipline the
#: library applies to GeoTIFF tags and xarray attrs, extended to model outputs.
AI_PROVENANCE = (
    "AI-generated interpretation of SAR imagery. Descriptions are a model's "
    "reading of the scene, not verified measurements, and may be wrong; verify "
    "against the source data before relying on them."
)
