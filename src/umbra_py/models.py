"""Lightweight representations of Umbra STAC items.

We deliberately model items as plain dataclasses over the raw STAC JSON rather
than depending on a heavier STAC object library. This keeps the core install
small and makes the objects trivial to construct in tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote

from .constants import (
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

    Handles both the old explicit keys (``"GEC"``, ``"SICD"``, ...) and the
    newer filename-style keys (``..._SICD_MM.nitf``, ``..._MM.tif``).
    """
    name = f"{key} {asset.get('href', '')}".upper()
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


# Current Umbra STAC items publish every asset with href="". The asset *key*
# is the v1-style filename (e.g. "<base>_MM.tif"); the actual file on S3 lives
# at sar-data/tasks/<umbra:task_id>/<base>/<base>_<PRODUCT>.<ext>. The map
# below converts the v1 suffix to the on-disk suffix. Longest entries first so
# "_CSI_SIDD_MM" doesn't get eaten by the "_MM" rule.
_V1_TO_DISK_SUFFIX: tuple[tuple[str, str], ...] = (
    ("_CSI_SIDD_MM.nitf", "_CSI-SIDD.nitf"),
    ("_SICD_MM.nitf", "_SICD.nitf"),
    ("_SIDD_MM.nitf", "_SIDD.nitf"),
    ("_CSI_MM.tif", "_CSI.tif"),
    ("_MM.cphd", "_CPHD.cphd"),
    ("_MM.tif", "_GEC.tif"),
)


def _public_basename(key: str) -> str | None:
    """Public on-disk filename for an asset whose STAC key uses v1 naming.

    Maps the v1 suffix to the published suffix (e.g. ``..._MM.tif`` ->
    ``..._GEC.tif``). Returns ``None`` for keys matching no known suffix
    (sidecar metadata JSON, stray files).
    """
    for v1, disk in _V1_TO_DISK_SUFFIX:
        if key.endswith(v1):
            return key[: -len(v1)] + disk
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
        directory, so the task is the natural grouping for "the same place
        over time". We read the task component straight from the item's
        sidecar ``href`` (URL-decoded, e.g. ``"Centerfield, Utah"``), since
        that carries the human-friendly label; when no usable href is present
        we fall back to the ``umbra:task_id`` property. Returns ``None`` when
        neither is available.
        """
        href = self.href or ""
        marker = "/sar-data/tasks/"
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

        When several assets share a product type (e.g. a primary SIDD and a
        Color Sub-aperture SIDD), the non-CSI "primary" asset is preferred.
        """
        result: dict[str, str] = {}
        for key, asset in self.assets.items():
            canon = _classify_asset(key, asset)
            if canon is None:
                continue
            existing = result.get(canon)
            if existing is not None:
                new_is_csi = "CSI" in key.upper()
                old_is_csi = "CSI" in existing.upper()
                if new_is_csi and not old_is_csi:
                    continue  # keep the primary (non-CSI) asset
            result[canon] = key
        return result

    @property
    def available_assets(self) -> list[str]:
        """Canonical product types present on this item (e.g. GEC, SICD)."""
        present = self.asset_map
        return [name for name in PRODUCT_ASSETS if name in present]

    def asset_href(self, name: str) -> str:
        """Return the download URL for a product type (``"GEC"``) or asset key.

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
        key = self.asset_map.get(name, name)
        try:
            asset = self.assets[key]
        except KeyError as exc:
            available = ", ".join(self.available_assets) or "none"
            raise AssetNotFoundError(
                f"Item {self.id!r} has no asset {name!r}. Available: {available}."
            ) from exc
        href = asset.get("href") or ""
        if href.startswith(("http://", "https://")):
            return href
        public = self._public_asset_url(key)
        if public is not None:
            return public
        task_id = self.properties.get("umbra:task_id")
        if task_id:
            derived = _derive_data_url(key, task_id)
            if derived is not None:
                return derived
        return href

    def _public_asset_url(self, key: str) -> str | None:
        """Resolve an asset's public URL as a sibling of the item's sidecar.

        The downloadable products live next to the ``*.stac.v2.json`` sidecar
        in the public bucket, so given the item's own (public) ``href`` and
        the asset's v1-style key we can build the sibling URL directly. This
        handles named-task layouts that a ``umbra:task_id``-only
        reconstruction can't. Returns ``None`` when there's no usable sidecar
        href or the key isn't recognised.
        """
        if not self.href or not self.href.startswith(("http://", "https://")):
            return None
        basename = _public_basename(key)
        if basename is None:
            return None
        base_dir = self.href.rsplit("/", 1)[0]
        return f"{base_dir}/{basename}"

    def has_asset(self, name: str) -> bool:
        return name in self.asset_map or name in self.assets

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
        """
        rng, azi = self.resolution
        return {
            "id": self.id,
            "datetime": self.datetime.isoformat() if self.datetime else None,
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
