"""``UmbraItem`` -> GeoJSON, the dependency-free half of ``viz``.

Everything here is standard library only: an item becomes a GeoJSON Feature
(its footprint polygon, or its bbox when the sidecar carries no geometry) and a
search result becomes a FeatureCollection you can open in QGIS, leafmap, Earth
Engine, geopandas or deck.gl. The rest of the package's map, raster and
composite renderers build on these primitives, and so does
``UmbraItem.__geo_interface__``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ..models import UmbraItem


def _geometry_for(item: UmbraItem) -> dict[str, Any] | None:
    """Return a 2D GeoJSON geometry for the item.

    Umbra footprints are often 3D polygons (lon, lat, height); strip the
    third coordinate so consumers that expect 2D (Folium, leaflet, most
    GIS tools) render them correctly.
    """
    geom = item.geometry
    if geom and geom.get("coordinates"):
        return {"type": geom.get("type", "Polygon"), "coordinates": _strip_z(geom["coordinates"])}
    if item.bbox is not None:
        minx, miny, maxx, maxy = item.bbox
        return {
            "type": "Polygon",
            "coordinates": [[[minx, miny], [maxx, miny], [maxx, maxy], [minx, maxy], [minx, miny]]],
        }
    return None


def _strip_z(coords: Any) -> Any:
    if (
        isinstance(coords, (list, tuple))
        and len(coords) >= 2
        and all(isinstance(v, (int, float)) for v in coords[:2])
    ):
        return [float(coords[0]), float(coords[1])]
    if isinstance(coords, (list, tuple)):
        return [_strip_z(c) for c in coords]
    return coords


def item_to_feature(item: UmbraItem) -> dict[str, Any]:
    """Convert one ``UmbraItem`` to a GeoJSON ``Feature`` dict.

    Properties include the compact metadata summary plus the item's STAC
    URL (``stac_href``) so downstream tools can link back to the source.
    """
    props = item.metadata_summary()
    props["stac_href"] = item.href
    geometry = _geometry_for(item)
    return {
        "type": "Feature",
        "id": item.id,
        "geometry": geometry,
        "bbox": list(item.bbox) if item.bbox else None,
        "properties": props,
    }


def items_to_featurecollection(items: Iterable[UmbraItem]) -> dict[str, Any]:
    """Convert items to a single GeoJSON ``FeatureCollection`` dict."""
    features = [item_to_feature(i) for i in items]
    bbox = _union_bbox(features)
    fc: dict[str, Any] = {"type": "FeatureCollection", "features": features}
    if bbox is not None:
        fc["bbox"] = list(bbox)
    return fc


def write_geojson(
    items: Iterable[UmbraItem],
    dest: str | os.PathLike,
    *,
    indent: int | None = 2,
) -> Path:
    """Write items as a GeoJSON FeatureCollection to ``dest``."""
    fc = items_to_featurecollection(items)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(fc, indent=indent))
    return dest


def _union_bbox(features: list[dict[str, Any]]) -> tuple[float, float, float, float] | None:
    boxes = [f["bbox"] for f in features if f.get("bbox")]
    if not boxes:
        return None
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def _centroid(item: UmbraItem) -> tuple[float, float] | None:
    """Return (lat, lon) center of an item's footprint, or None."""
    if item.bbox is None:
        return None
    minx, miny, maxx, maxy = item.bbox
    return ((miny + maxy) / 2.0, (minx + maxx) / 2.0)
