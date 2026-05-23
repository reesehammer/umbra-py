"""Lightweight representations of Umbra STAC items.

We deliberately model items as plain dataclasses over the raw STAC JSON rather
than depending on a heavier STAC object library. This keeps the core install
small and makes the objects trivial to construct in tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .constants import METADATA_ASSET, PRODUCT_ASSETS
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
    is_geotiff = "tif" in name or "geotiff" in media

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
        """Return the download URL for a product type (``"GEC"``) or asset key."""
        key = self.asset_map.get(name, name)
        try:
            return self.assets[key]["href"]
        except KeyError as exc:
            available = ", ".join(self.available_assets) or "none"
            raise AssetNotFoundError(
                f"Item {self.id!r} has no asset {name!r}. Available: {available}."
            ) from exc

    def has_asset(self, name: str) -> bool:
        return name in self.asset_map or name in self.assets

    def intersects_bbox(self, bbox: BBox) -> bool:
        """Whether this item's footprint overlaps the given bounding box."""
        if self.bbox is None:
            return False
        return _bbox_overlaps(self.bbox, bbox)

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
