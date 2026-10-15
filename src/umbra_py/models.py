"""Lightweight representations of Umbra STAC items.

We deliberately model items as plain dataclasses over the raw STAC JSON rather
than depending on a heavier STAC object library. This keeps the core install
small and makes the objects trivial to construct in tests.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote

from .constants import (
    ASSET_FILENAME_FIELD,
    ATTRIBUTION,
    DATA_LICENSE,
    METADATA_ASSET,
    POLARIZATION_CAVEAT,
    PRODUCT_ASSETS,
    PRODUCT_TYPE_EXPLANATIONS,
    S3_BUCKET,
    S3_REGION,
)
from .exceptions import AssetNotFoundError

# (min_lon, min_lat, max_lon, max_lat)
BBox = tuple[float, float, float, float]


def _classify_asset(key: str, asset: dict[str, Any]) -> str | None:
    """Map a STAC asset to a canonical Umbra product type (or ``None``).

    Handles explicit keys (``"GEC"``, ``"gec"``), filename-style keys
    (``..._SICD_MM.nitf``, ``..._MM.tif``), and asset schema v2 keys whose
    filename lives on :data:`~umbra_py.constants.ASSET_FILENAME_FIELD`.
    """
    filename = asset.get(ASSET_FILENAME_FIELD) or ""
    name = f"{key} {filename} {asset.get('href', '')}".upper()
    media = (asset.get("type") or "").lower()
    # ``name`` is upper-cased, so the substring must be too: a lowercase "tif"
    # can never match and would leave the branch dead, dropping a GeoTIFF whose
    # media type is a plain ``image/tiff`` (no "geotiff" profile substring).
    is_geotiff = "TIF" in name or "geotiff" in media

    if "CPHD" in name:
        return "CPHD"
    if "SICD" in name:
        return "SICD"
    if "SIDD" in name:
        return "SIDD"
    if "CSI" in name and is_geotiff:
        return "CSI"
    if "METADATA" in name:
        return METADATA_ASSET
    if is_geotiff:  # remaining geocoded GeoTIFF is the GEC product
        return "GEC"
    return None


#: Stable asset keys emitted by :func:`_normalize_assets`, in dict order.
#:
#: Drawn from the filenames on the published ``umbra-open-data.parquet``
#: (catalog-index snapshot, 2026-10): every one of ~79k asset fields was one
#: of these filename shapes.
#:
#: - ``gec`` — ``*_GEC.tif``, and legacy bare ``<stem>.tif``
#: - ``csi`` — ``*_CSI.tif`` (color sub-aperture GeoTIFF)
#: - ``sidd`` — ``*_SIDD.nitf``
#: - ``csi_sidd`` — ``*_CSI-SIDD.nitf`` (color sub-aperture SIDD; its own key
#:   so it does not collide with ``sidd``)
#: - ``sicd`` — ``*_SICD.nitf``
#: - ``cphd`` — ``*_CPHD.cphd``, and legacy bare ``<stem>.cphd``
#: - ``metadata`` — ``*_METADATA.json`` (Umbra extended metadata)
#: - ``stac_metadata`` — ``*_STAC-METADATA.json``
#: - ``di_gif`` — ``*_DI-GIF.gif``, the detected-image GIF preview (the only
#:   thumbnail-like product file in the open archive)
#:
#: A file that matches none of these collapses to ``file`` or ``file_<ext>``
#: so a new suffix cannot reopen one struct field per filename. A second file
#: of the same key is ``<key>_2``, ``<key>_3``, … (see :func:`_normalize_assets`).
ASSET_KEYS: tuple[str, ...] = (
    "gec",
    "csi",
    "sidd",
    "csi_sidd",
    "sicd",
    "cphd",
    "metadata",
    "stac_metadata",
    "di_gif",
)

# Filename token that marks the primary file for a key. Checked on the
# upper-cased filename. ``_METADATA.`` does not match ``_STAC-METADATA.json``
# (the character before METADATA there is ``-``).
_PRIMARY_MARK: dict[str, str] = {
    "gec": "_GEC.",
    "csi": "_CSI.",
    "sidd": "_SIDD.",
    "csi_sidd": "CSI-SIDD",
    "sicd": "_SICD.",
    "cphd": "_CPHD.",
    "metadata": "_METADATA.",
    "stac_metadata": "STAC-METADATA",
    "di_gif": "DI-GIF",
}

_COLLISION_KEY_RE = re.compile(r"^(?P<base>.+)_(?P<n>[0-9]+)$")


def _looks_like_filename(value: str) -> bool:
    """A single path segment with an extension, not a URL."""
    if not value or "://" in value or "/" in value or "\\" in value:
        return False
    return "." in value


def _asset_filename(key: str, asset: dict[str, Any]) -> str:
    """On-disk filename for an asset, wherever schema v1 or v2 stored it.

    Prefers ``umbra-py:filename``, then a filename-shaped key, then the href
    basename, then a filename-shaped title. A stable key such as ``gec`` with
    none of those falls back to the key itself.
    """
    named = asset.get(ASSET_FILENAME_FIELD)
    if isinstance(named, str) and named.strip():
        return named.strip()
    if _looks_like_filename(key):
        return key
    href = asset.get("href") or ""
    if isinstance(href, str) and href:
        base = unquote(href.rsplit("/", 1)[-1].split("?", 1)[0])
        if _looks_like_filename(base):
            return base
    title = asset.get("title")
    if isinstance(title, str) and _looks_like_filename(title):
        return title
    return key


def _known_base_key(name: str) -> bool:
    """Whether ``name`` is a stable key or the ``file`` / ``file_<ext>`` fallback."""
    if name in ASSET_KEYS or name == "file":
        return True
    return name.startswith("file_") and name.removeprefix("file_").isalnum()


def _asset_base_key(filename: str, asset: dict[str, Any]) -> str:
    """Stable product key for one file, before collision suffixes."""
    name = filename.upper()
    media = (asset.get("type") or "").lower()
    is_geotiff = "TIF" in name or "geotiff" in media
    if "CSI-SIDD" in name or "CSI_SIDD" in name:
        return "csi_sidd"
    if "STAC-METADATA" in name or "STAC_METADATA" in name:
        return "stac_metadata"
    if "DI-GIF" in name or "DI_GIF" in name or name.endswith(".GIF"):
        return "di_gif"
    if "CPHD" in name or name.endswith(".CPHD"):
        return "cphd"
    if "SICD" in name:
        return "sicd"
    if "SIDD" in name:
        return "sidd"
    if "CSI" in name and is_geotiff:
        return "csi"
    if "METADATA" in name:
        return "metadata"
    if is_geotiff or name.endswith(".TIFF"):
        return "gec"
    # Idempotent: a key that is already ``gec`` or ``gec_2`` with no filename.
    lowered = filename.lower()
    match = _COLLISION_KEY_RE.fullmatch(lowered)
    base = match.group("base") if match else lowered
    if _known_base_key(base):
        return base
    if "." in filename:
        ext = filename.rsplit(".", 1)[-1].lower()
        if ext.isalnum():
            return f"file_{ext}"
    return "file"


def _asset_key_rank(key: str) -> int:
    """Sort rank of a normalized key. The unsuffixed key ranks first (1)."""
    match = _COLLISION_KEY_RE.fullmatch(key)
    if match and _known_base_key(match.group("base")):
        return int(match.group("n"))
    return 1


def _normalize_assets(assets: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Rewrite asset keys to asset schema v2.

    Each file keeps its bytes-address (``href``) and gains
    ``umbra-py:filename`` plus, when it had no title, a ``title`` of that
    filename. An existing title that is not the old key is left alone.

    Two files of the same product become ``gec`` and ``gec_2`` (then
    ``gec_3``, …). Order is deterministic: a filename carrying the canonical
    token (``_GEC.tif``, ``_CPHD.cphd``, …) takes the unsuffixed key; the
    rest follow in filename order. Re-applying this to its own output is a
    no-op.
    """
    if not assets:
        return {}
    grouped: dict[str, list[tuple]] = {}
    for index, (key, asset) in enumerate(assets.items()):
        if not isinstance(asset, dict):
            continue
        text_key = str(key)
        filename = _asset_filename(text_key, asset)
        base = _asset_base_key(filename, asset)
        upper = filename.upper()
        mark = _PRIMARY_MARK.get(base)
        preferred = 0 if mark and mark in upper else 1
        grouped.setdefault(base, []).append(
            ((preferred, filename, index), filename, text_key, asset)
        )
    order = [name for name in ASSET_KEYS if name in grouped]
    order.extend(sorted(name for name in grouped if name not in ASSET_KEYS))
    out: dict[str, dict[str, Any]] = {}
    for base in order:
        rows = sorted(grouped[base], key=lambda row: row[0])
        for nth, (_, filename, old_key, asset) in enumerate(rows, start=1):
            new_key = base if nth == 1 else f"{base}_{nth}"
            stamped = dict(asset)
            stamped[ASSET_FILENAME_FIELD] = filename
            title = stamped.get("title")
            # Fill a missing title. A v1 key that *is* the filename and was
            # copied into ``title`` is rewritten to that same filename; a
            # human title such as ``"GEC"`` is left alone.
            title_missing = not isinstance(title, str) or not title.strip()
            title_is_old_filename = title == old_key and _looks_like_filename(old_key)
            if title_missing or title_is_old_filename:
                stamped["title"] = filename
            out[new_key] = stamped
    return out


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    # STAC datetimes are RFC 3339; normalise the trailing "Z" for fromisoformat.
    text = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _iso_utc(value: datetime | None) -> str | None:
    """UTC RFC 3339 form of ``value`` (``Z`` suffix), or ``None``.

    Naive values are read as UTC. Microseconds are kept when the instant has
    them, so a STAC ``created`` of ``...28.483435Z`` round-trips unchanged.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _bbox_from_geometry(geometry: dict | None) -> BBox | None:
    if not geometry:
        return None
    coords = geometry.get("coordinates")
    if not coords:
        return None
    lons: list[float] = []
    lats: list[float] = []

    def walk(node: Any) -> None:
        # A position is a list whose first two entries are numbers (lon, lat).
        if (
            isinstance(node, (list, tuple))
            and len(node) >= 2
            and all(isinstance(v, (int, float)) for v in node[:2])
        ):
            lons.append(float(node[0]))
            lats.append(float(node[1]))
            return
        if isinstance(node, (list, tuple)):
            for child in node:
                walk(child)

    walk(coords)
    if not lons or not lats:
        return None
    return (min(lons), min(lats), max(lons), max(lats))


def _bbox_overlaps(a: BBox, b: BBox) -> bool:
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


# Current Umbra STAC items publish every asset with href="" or an s3:// URL
# into a private bucket. The asset *key* is the v1-style filename (e.g.
# "<base>_MM.tif"); the public file sits next to the sidecar as
# <sidecar-stem>_<PRODUCT>.<ext> under sar-data/tasks/ or sar-data/task-data/.
# The map below converts the v1 suffix to the on-disk suffix. Longest entries
# first so "_CSI_SIDD_MM" doesn't get eaten by the "_MM" rule.
_V1_TO_DISK_SUFFIX: tuple[tuple[str, str], ...] = (
    ("_CSI_SIDD_MM.nitf", "_CSI-SIDD.nitf"),
    ("_SICD_MM.nitf", "_SICD.nitf"),
    ("_SIDD_MM.nitf", "_SIDD.nitf"),
    ("_CSI_MM.tif", "_CSI.tif"),
    ("_MM.cphd", "_CPHD.cphd"),
    ("_MM.tif", "_GEC.tif"),
)


# Acquisition directories and sidecar stems look like
# ``2026-02-20-03-03-38_UMBRA-07``. Same pattern catalog.py uses to identify
# an acquisition component; kept here so models does not import catalog.
_ACQ_STEM_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-")


def _sidecar_acquisition_stem(href: str) -> str | None:
    """Acquisition stem from a ``*.stac.v2.json`` sidecar URL, if it looks like one.

    Published products share that stem (``<stem>_CPHD.cphd`` next to
    ``<stem>.stac.v2.json``). A sidecar named something else (``item.stac.v2.json``)
    is not an acquisition id -- fall back to the asset key.
    """
    name = unquote(href.rsplit("/", 1)[-1].split("?", 1)[0])
    marker = ".stac.v2.json"
    if not name.endswith(marker):
        return None
    stem = name[: -len(marker)]
    if stem and _ACQ_STEM_RE.match(stem):
        return stem
    return None


def _disk_suffix(key: str) -> str | None:
    """On-disk product suffix for a STAC asset key.

    Accepts both v1 keys (``..._MM.cphd`` → ``_CPHD.cphd``) and already-
    public names (``..._CPHD.cphd`` → ``_CPHD.cphd``). Returns ``None``
    for sidecar metadata and other unrecognised files.
    """
    for v1, disk in _V1_TO_DISK_SUFFIX:
        if key.endswith(v1):
            return disk
    for _, disk in _V1_TO_DISK_SUFFIX:
        if key.endswith(disk):
            return disk
    return None


def _public_basename(key: str) -> str | None:
    """Public on-disk filename for an asset whose STAC key uses v1 naming.

    Maps the v1 suffix to the published suffix (e.g. ``..._MM.tif`` ->
    ``..._GEC.tif``). Returns ``None`` for keys matching no known suffix
    (sidecar metadata JSON, stray files).
    """
    for v1, disk in _V1_TO_DISK_SUFFIX:
        if key.endswith(v1):
            return key[: -len(v1)] + disk
    suffix = _disk_suffix(key)
    if suffix is not None and key.endswith(suffix):
        return key
    return None


def _derive_data_url(key: str, task_id: str) -> str | None:
    """Reconstruct the public-bucket URL for an asset whose STAC href is empty.

    Returns ``None`` when ``key`` doesn't end in any of the recognised v1
    suffixes (e.g. sidecar metadata files), so the caller can fall back to
    the original empty href rather than building a wrong URL.
    """
    for v1, disk in _V1_TO_DISK_SUFFIX:
        if key.endswith(v1):
            base = key[: -len(v1)]
            return (
                f"https://s3.{S3_REGION}.amazonaws.com/{S3_BUCKET}"
                f"/sar-data/tasks/{task_id}/{base}/{base}{disk}"
            )
    return None


@dataclass
class UmbraItem:
    """A single Umbra SAR acquisition, parsed from a STAC item."""

    id: str
    properties: dict[str, Any] = field(default_factory=dict)
    assets: dict[str, dict[str, Any]] = field(default_factory=dict)
    geometry: dict[str, Any] | None = None
    bbox: BBox | None = None
    href: str | None = None  # URL of the item JSON, when known
    raw: dict[str, Any] = field(default_factory=dict)
    #: Human-readable place label for the acquisition's location (e.g.
    #: ``"Reykjavík, Iceland"``), reverse-geocoded from the footprint centroid.
    #: It is *not* parsed from the STAC document -- ``CatalogIndex`` bakes it
    #: once at build time and populates it on the items it yields (see
    #: :meth:`umbra_py.CatalogIndex.bake_places`); it is ``None`` for an item
    #: read live or from an index with no baked label. Distinct from
    #: :attr:`task`, which is the Umbra campaign codename, not a geographic name.
    place: str | None = None
    #: Set by :class:`~umbra_py.CatalogIndex` when the acquisition time is after
    #: ``properties.created`` (or after the index ``built_at`` when created is
    #: missing). The row is kept and the datetime is not rewritten; latest-date
    #: and recency paths skip it. ``False`` for an item that did not come from
    #: an index build that flagged it.
    datetime_suspect: bool = False
    #: When the sidecar object was last written in the public bucket (its S3
    #: ``LastModified``), taken from the ``ListObjectsV2`` page the crawl
    #: already fetches. Not a STAC field: the open-bucket walk sets it, and
    #: :class:`~umbra_py.CatalogIndex` restores it from the ``published``
    #: column. ``None`` for a Canopy item, a hand-built item, or a row indexed
    #: before that column existed. This is when the object appeared in the
    #: public bucket, not when the collect was tasked.
    published: datetime | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any], href: str | None = None) -> UmbraItem:
        """Build an item from a STAC feature dictionary."""
        geometry = data.get("geometry")
        bbox = data.get("bbox")
        if bbox is not None and len(bbox) >= 4:
            parsed_bbox: BBox | None = (
                float(bbox[0]),
                float(bbox[1]),
                float(bbox[-2]),
                float(bbox[-1]),
            )
        else:
            parsed_bbox = _bbox_from_geometry(geometry)

        self_href = href
        if self_href is None:
            for link in data.get("links", []):
                if link.get("rel") == "self":
                    self_href = link.get("href")
                    break

        return cls(
            id=data.get("id", ""),
            properties=data.get("properties", {}),
            assets=data.get("assets", {}),
            geometry=geometry,
            bbox=parsed_bbox,
            href=self_href,
            raw=data,
        )

    # -- convenience accessors -------------------------------------------------

    # Declared before ``datetime``: that property's name shadows the class
    # for every later annotation in this body.
    @property
    def created(self) -> datetime | None:
        """When Umbra generated this STAC item (``properties.created``).

        That is the sidecar's generation time, not tasking delivery and not
        when the object appeared in the public bucket (:attr:`published`).
        Legacy ``_METADATA.json`` collects do not carry ``created``.
        """
        value = self.properties.get("created")
        return _parse_datetime(value if isinstance(value, str) else None)

    @property
    def datetime(self) -> datetime | None:
        return _parse_datetime(
            self.properties.get("datetime") or self.properties.get("start_datetime")
        )

    @property
    def platform(self) -> str | None:
        return self.properties.get("platform")

    @property
    def product_type(self) -> str | None:
        return self.properties.get("sar:product_type")

    @property
    def polarizations(self) -> list[str]:
        return list(self.properties.get("sar:polarizations", []))

    @property
    def instrument_mode(self) -> str | None:
        return self.properties.get("sar:instrument_mode")

    @property
    def incidence_angle(self) -> float | None:
        return self.properties.get("view:incidence_angle")

    @property
    def resolution(self) -> tuple[float | None, float | None]:
        """(range, azimuth) resolution in metres."""
        return (
            self.properties.get("sar:resolution_range"),
            self.properties.get("sar:resolution_azimuth"),
        )

    @property
    def description(self) -> str | None:
        """Free-text description of the acquisition, when the STAC item has one.

        Checks the item's top-level ``description`` (STAC convention) and
        ``properties.description``, then falls back to the description on
        the primary image asset (GEC) so popups can surface whatever
        human-readable blurb the catalog provides.
        """
        top = self.raw.get("description") if self.raw else None
        if top:
            return str(top)
        prop = self.properties.get("description")
        if prop:
            return str(prop)
        gec_key = self.asset_map.get("GEC")
        if gec_key:
            asset_desc = self.assets.get(gec_key, {}).get("description")
            if asset_desc:
                return str(asset_desc)
        return None

    @property
    def task(self) -> str | None:
        """The Umbra task (AOI campaign) this acquisition belongs to.

        Umbra files every pass of a site under one ``sar-data/tasks/<task>/``
        or ``sar-data/task-data/<task-id>/`` directory, so the task is the
        natural grouping for "the same place over time". We read the task
        component straight from the item's sidecar ``href`` (URL-decoded,
        e.g. ``"Centerfield, Utah"``), since that carries the human-friendly
        label on named campaigns; UUID collects keep the task id. When no
        usable href is present we fall back to the ``umbra:task_id``
        property. Returns ``None`` when neither is available.
        """
        href = self.href or ""
        for marker in ("/sar-data/tasks/", "/sar-data/task-data/", "/open-data/"):
            idx = href.find(marker)
            if idx != -1:
                rest = href[idx + len(marker) :]
                first = rest.split("/", 1)[0]
                if first:
                    return unquote(first)
        task_id = self.properties.get("umbra:task_id")
        return str(task_id) if task_id else None

    @property
    def asset_map(self) -> dict[str, str]:
        """Map canonical product type -> actual STAC asset key.

        When several assets share a product type (a primary SIDD and a
        ``csi_sidd``, or ``gec`` and ``gec_2``), the unsuffixed non-CSI asset
        is preferred. CSI is read from the key and the filename, so a
        normalized ``csi_sidd`` key still loses to ``sidd``.
        """
        result: dict[str, str] = {}
        for key, asset in self.assets.items():
            canon = _classify_asset(key, asset)
            if canon is None:
                continue
            existing = result.get(canon)
            if existing is not None:
                if _asset_key_rank(key) > _asset_key_rank(existing):
                    continue
                if _asset_key_rank(key) == _asset_key_rank(existing):
                    new_is_csi = "CSI" in f"{key} {_asset_filename(key, asset)}".upper()
                    old_asset = self.assets[existing]
                    old_name = f"{existing} {_asset_filename(existing, old_asset)}"
                    if new_is_csi and "CSI" not in old_name.upper():
                        continue  # keep the primary (non-CSI) asset
            result[canon] = key
        return result

    @property
    def available_assets(self) -> list[str]:
        """Canonical product types present on this item (e.g. GEC, SICD)."""
        present = self.asset_map
        return [name for name in PRODUCT_ASSETS if name in present]

    def asset_href(self, name: str) -> str:
        """Return the download URL for one asset.

        ``name`` is a product type in any case (``"GEC"``), a schema v2 key
        (``"gec"``, ``"gec_2"``, ``"csi_sidd"``, ``"file_tif"``), or the
        original filename (``umbra-py:filename``). ``"GEC"`` and ``"gec"``
        are the primary file; ``"gec_2"`` is the next file of that product.
        A name that is not on the item raises :class:`AssetNotFoundError`
        listing the v2 keys. Stored keys may still be a filename or
        ``"GEC"``; selection uses the same v2 key space the API serves.

        Umbra's published ``*.stac.v2.json`` sidecars reference assets either
        with ``href=""`` or with an ``s3://`` URL into a *private* processing
        bucket -- neither is anonymously fetchable. In both cases the public
        copy sits next to the sidecar in the open-data bucket, so we
        reconstruct a public HTTPS URL: first relative to the item's own
        sidecar ``href`` (which correctly handles named-task layouts like
        ``tasks/<name>/<task_id>/<acq>/``), then falling back to deriving from
        ``umbra:task_id``. Hrefs already pointing at ``http(s)`` are returned
        unchanged.
        """
        key, asset = self._resolve_asset(name)
        href = asset.get("href") or ""
        if href.startswith(("http://", "https://")):
            return href
        public = self._public_asset_url(key, asset)
        if public is not None:
            return public
        task_id = self.properties.get("umbra:task_id")
        if task_id:
            derived = _derive_data_url(_asset_filename(key, asset), task_id)
            if derived is not None:
                return derived
        return href

    def _public_asset_url(self, key: str, asset: dict[str, Any] | None = None) -> str | None:
        """Resolve an asset's public URL as a sibling of the item's sidecar.

        The downloadable products live next to the ``*.stac.v2.json`` sidecar
        in the public bucket and share that sidecar's acquisition stem
        (``<stem>_CPHD.cphd`` next to ``<stem>.stac.v2.json``). STAC asset
        keys sometimes carry a *processing* timestamp that is not the
        on-disk name, so we take the suffix from the filename (the v1 key,
        ``umbra-py:filename``, or an already-public name) and the stem from
        the sidecar. That also handles named-task
        and ``task-data/`` layouts that a ``umbra:task_id``-only
        reconstruction can't. Returns ``None`` when there's no usable sidecar
        href or the key isn't recognised.
        """
        if not self.href or not self.href.startswith(("http://", "https://")):
            return None
        # Schema v2 keys are ``gec`` / ``cphd``; the on-disk suffix still comes
        # from the filename (v1 ``*_MM.cphd`` or already-public ``*_CPHD.cphd``).
        # ``asset`` is the resolved body: the stored key may still be a filename
        # while ``key`` is already the v2 name.
        body = asset if asset is not None else (self.assets.get(key) or {})
        filename = _asset_filename(key, body)
        suffix = _disk_suffix(filename)
        if suffix is None:
            return None
        base_dir = self.href.rsplit("/", 1)[0]
        stem = _sidecar_acquisition_stem(self.href)
        if stem is not None:
            return f"{base_dir}/{stem}{suffix}"
        basename = _public_basename(filename)
        if basename is None:
            return None
        return f"{base_dir}/{basename}"

    def _resolve_asset(self, name: str) -> tuple[str, dict[str, Any]]:
        """The ``(v2 key, asset)`` for a product type, stable key, or filename.

        Selection uses the schema v2 key space, including when this item still
        stores a filename or an uppercase ``"GEC"`` key. ``"GEC"`` / ``"gec"``
        / ``"Gec"`` select the unsuffixed ``gec`` asset. ``"gec_2"`` selects
        that collision suffix. A product type that is not itself a key
        (``"SIDD"`` when the only file is ``csi_sidd``) uses :attr:`asset_map`.
        A product filename matches ``umbra-py:filename``. A miss lists the v2
        keys, not the uppercase product types.
        """
        normalized = _normalize_assets(self.assets)
        text = name.strip()
        folded = {key.lower(): key for key in normalized}
        key = folded.get(text.lower())
        if key is not None:
            return key, normalized[key]
        if text.upper() in PRODUCT_ASSETS:
            picked = self._normalized_for_storage_key(normalized, self.asset_map.get(text.upper()))
            if picked is not None:
                return picked
        want = unquote(text.replace("\\", "/").split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1])
        if want:
            for candidate, body in normalized.items():
                filename = body.get(ASSET_FILENAME_FIELD)
                if candidate == want or filename == want:
                    return candidate, body
        available = ", ".join(normalized) or "none"
        raise AssetNotFoundError(f"Item {self.id!r} has no asset {name!r}. Available: {available}.")

    def _normalized_for_storage_key(
        self,
        normalized: dict[str, dict[str, Any]],
        storage_key: str | None,
    ) -> tuple[str, dict[str, Any]] | None:
        """The v2 entry for a key that still lives in :attr:`assets`."""
        if storage_key is None:
            return None
        body = self.assets.get(storage_key)
        if not isinstance(body, dict):
            return None
        filename = _asset_filename(storage_key, body)
        for candidate, asset in normalized.items():
            if asset.get(ASSET_FILENAME_FIELD) == filename:
                return candidate, asset
        return None

    def has_asset(self, name: str) -> bool:
        try:
            self._resolve_asset(name)
        except AssetNotFoundError:
            return False
        return True

    def intersects_bbox(self, bbox: BBox) -> bool:
        """Whether this item's footprint overlaps the given bounding box."""
        if self.bbox is None:
            return False
        return _bbox_overlaps(self.bbox, bbox)

    def intersects_polygon(self, geometry: Any) -> bool:
        """Whether this item's footprint intersects the given polygon geometry.

        ``geometry`` is the exterior-ring form returned by
        :func:`umbra_py._geometry.parse_geometry` (a list of rings). This uses
        the item's *actual* footprint polygon when it has one -- a tighter test
        than the bbox rectangle :meth:`intersects_bbox` uses -- and falls back
        to the footprint bbox when the geometry is missing or not a polygon (and
        matches nothing when neither is available).
        """
        from ._geometry import bbox_ring, geometries_intersect, rings_from_geojson

        item_rings = rings_from_geojson(self.geometry)
        if item_rings is None:
            if self.bbox is None:
                return False
            item_rings = [bbox_ring(self.bbox)]
        return geometries_intersect(item_rings, geometry)

    def matches_filters(
        self,
        *,
        polarizations: list[str] | None = None,
        min_incidence: float | None = None,
        max_incidence: float | None = None,
        max_resolution: float | None = None,
    ) -> bool:
        """Whether this item satisfies the SAR acquisition-property filters.

        These are the first-class radar-discovery filters an analyst reaches for
        beyond geography and date -- polarization, incidence angle and spatial
        resolution -- read from the STAC ``sar:``/``view:`` properties the item
        already parses (:attr:`polarizations`, :attr:`incidence_angle`,
        :attr:`resolution`). Every argument is optional; a ``None``/empty one is
        no constraint, so a call with no arguments always returns ``True``.

        - ``polarizations``: keep the item if it exposes *at least one* of the
          requested polarizations (case-insensitive), e.g. ``["VV"]``. This is
          the filter that keeps a change composite comparing like with like --
          HH and VV image different scattering physics (see
          :data:`~umbra_py.constants.POLARIZATION_CAVEAT`).
        - ``min_incidence`` / ``max_incidence``: inclusive bounds on the view
          incidence angle in degrees.
        - ``max_resolution``: keep the item only if it is *at least this fine* --
          both the range and azimuth resolution must be ``<= max_resolution``
          metres.

        Each filter is a **hard predicate**: when it is set, an item that lacks
        the corresponding metadata is *excluded* (an absent value can't be
        confirmed to match), matching the STAC Query-extension convention that a
        property predicate does not select items missing that property. This is
        deliberately unlike the geometric :meth:`intersects_bbox` /
        :meth:`intersects_polygon` filters, which fall back to a coarser datum
        when a footprint is missing: those have a fallback, these categorical /
        numeric properties do not, so "present and satisfies" is the honest test.
        No model is called and no new dependency is used -- pure metadata
        comparison, so it is fully deterministic and offline-testable.
        """
        if polarizations:
            wanted = {p.upper() for p in polarizations}
            have = {p.upper() for p in self.polarizations}
            if not (wanted & have):
                return False
        if min_incidence is not None or max_incidence is not None:
            inc = self.incidence_angle
            if inc is None:
                return False
            if min_incidence is not None and inc < min_incidence:
                return False
            if max_incidence is not None and inc > max_incidence:
                return False
        if max_resolution is not None:
            rng, azi = self.resolution
            if rng is None or azi is None:
                return False
            if rng > max_resolution or azi > max_resolution:
                return False
        return True

    def metadata_summary(self) -> dict[str, Any]:
        """A compact, human-friendly subset of the item's metadata."""
        rng, azi = self.resolution
        return {
            "id": self.id,
            "datetime": self.datetime.isoformat() if self.datetime else None,
            "platform": self.platform,
            "product_type": self.product_type,
            "instrument_mode": self.instrument_mode,
            "polarizations": self.polarizations,
            "incidence_angle_deg": self.incidence_angle,
            "resolution_range_m": rng,
            "resolution_azimuth_m": azi,
            "bbox": self.bbox,
            "available_assets": self.available_assets,
        }

    def to_llm_context(self) -> dict[str, Any]:
        """A compact, explanation-rich context card for prompting a model.

        Like :meth:`metadata_summary` but tuned for a language model rather
        than a human: every present product type carries a one-line
        explanation, the polarizations carry the change-detection caveat, and
        the mandatory CC-BY attribution line travels with the data. The
        differences from ``metadata_summary`` are exactly the things a model
        needs spelled out and a human already knows — so an agent can reason
        about the scene, and cite the right product, with no external SAR
        literacy. Deterministic and offline (no network, no model call).

        Published as ``docs/schemas/item-context.schema.json`` -- the same card
        ``umbra info --json`` prints, the agent tools return, and a watch delta
        carries one of per new acquisition.
        """
        rng, azi = self.resolution
        return {
            "id": self.id,
            "datetime": self.datetime.isoformat() if self.datetime else None,
            "datetime_suspect": self.datetime_suspect,
            # Prefer the baked reverse-geocoded label (e.g. "Reykjavík,
            # Iceland") a `CatalogIndex` search yields on `.place`; fall back to
            # the task codename so an item from a live walk still carries a name.
            "place": self.place or self.task,
            "bbox": list(self.bbox) if self.bbox else None,
            "platform": self.platform,
            "instrument_mode": self.instrument_mode,
            "incidence_angle_deg": self.incidence_angle,
            "resolution_range_m": rng,
            "resolution_azimuth_m": azi,
            "polarizations": self.polarizations,
            "polarization_caveat": POLARIZATION_CAVEAT,
            "products": [
                {
                    "type": name,
                    "explanation": PRODUCT_TYPE_EXPLANATIONS.get(name, ""),
                    "url": self.asset_href(name),
                }
                for name in self.available_assets
            ],
            "stac_href": self.href,
            "license": DATA_LICENSE,
            "attribution": ATTRIBUTION,
        }

    def to_geojson(self) -> dict[str, Any]:
        """Return a GeoJSON ``Feature`` representing this item.

        Convenience wrapper around :func:`umbra_py.viz.item_to_feature` so
        users can call ``item.to_geojson()`` directly. The third coordinate
        of 3D footprints is stripped so the feature renders cleanly in
        standard 2D GIS tools.
        """
        from .viz import item_to_feature  # noqa: PLC0415

        return item_to_feature(self)

    @property
    def __geo_interface__(self) -> dict[str, Any]:
        """GeoJSON ``Feature`` mapping for the Python geo-interface protocol.

        Lets geopandas / shapely / leafmap ingest an item with zero glue
        (``shapely.geometry.shape(item)``,
        ``gpd.GeoDataFrame.from_features([item])``) — and agent-written
        analysis code "just works" on the first try. Delegates to
        :meth:`to_geojson`, so the 2D-footprint behaviour is identical.
        """
        return self.to_geojson()

    def summary(self) -> str:
        """A one-paragraph readable description for the CLI / notebooks."""
        info = self.metadata_summary()
        dt = info["datetime"] or "unknown time"
        res = info["resolution_range_m"]
        res_str = f"{res:.2f} m" if isinstance(res, (int, float)) else "?"
        pol = ", ".join(info["polarizations"]) or "?"
        return (
            f"{self.id}\n"
            f"  acquired : {dt}\n"
            f"  platform : {info['platform']} ({info['instrument_mode']})\n"
            f"  product  : {info['product_type']}  pol={pol}  res~{res_str}\n"
            f"  assets   : {', '.join(info['available_assets']) or 'none'}"
        )

    def _repr_html_(self) -> str:
        """Rich HTML card for Jupyter: footprint sketch + metadata table.

        Pure-stdlib and offline so it works in the core install and never
        triggers a network read just from displaying an item. For the SAR
        pixels, use :func:`umbra_py.quicklook` or an
        :class:`ItemCollection` with ``thumbnails=True``.
        """
        from ._html import item_card_html  # noqa: PLC0415

        return item_card_html(self)


class ItemCollection(list):
    """A list of :class:`UmbraItem` that renders as a gallery in notebooks.

    Behaves exactly like a ``list`` (it *is* one), so existing code that
    iterates search results is unaffected. The extra is a Jupyter
    ``_repr_html_`` that lays the items out as a grid of metadata cards::

        from umbra_py import UmbraCatalog, ItemCollection
        results = ItemCollection(UmbraCatalog().search(area="rome", limit=8))
        results  # -> gallery of cards (offline, no extras needed)

    Pass ``thumbnails=True`` to stream a small SAR quicklook for each item
    (needs the ``viz`` extra). Thumbnails are fetched lazily when the
    collection is displayed; any item that can't be previewed falls back to
    its footprint card, so displaying the collection never raises.
    """

    def __init__(
        self,
        items=(),
        *,
        thumbnails: bool = False,
        max_size: int = 256,
        db: bool = True,
    ) -> None:
        super().__init__(items)
        self._thumbnails = thumbnails
        self._max_size = max_size
        self._db = db

    @property
    def __geo_interface__(self) -> dict[str, Any]:
        """GeoJSON ``FeatureCollection`` for the Python geo-interface protocol.

        Mirrors :attr:`UmbraItem.__geo_interface__` at the collection level, so
        ``gpd.GeoDataFrame.from_features(results)`` ingests a whole search with
        zero code. Delegates to :func:`umbra_py.viz.items_to_featurecollection`.
        """
        from .viz import items_to_featurecollection  # noqa: PLC0415

        return items_to_featurecollection(self)

    def _repr_html_(self) -> str:
        from ._html import gallery_html  # noqa: PLC0415

        thumbs: dict[int, str | None] = {}
        if self._thumbnails:
            from .viz import _thumbnail_data_uri  # noqa: PLC0415

            for i, item in enumerate(self):
                # A repr must never raise (Jupyter would show a traceback
                # instead of the object), and a thumbnail read can fail many
                # ways -- no previewable asset, network, missing extra. On any
                # failure we drop back to the item's footprint card.
                try:
                    thumbs[i] = _thumbnail_data_uri(item, max_size=self._max_size, db=self._db)
                except Exception:
                    thumbs[i] = None
        return gallery_html(self, thumbnails=thumbs)
