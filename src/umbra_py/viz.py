"""Visualization helpers for Umbra search results.

This module turns ``UmbraItem`` objects into:

- **GeoJSON features** (zero dependencies) — open them in QGIS, leafmap,
  Earth Engine, geopandas, deck.gl, or anywhere else that reads GeoJSON.
- **Interactive Folium maps** (requires the ``viz`` extra) — drop-in HTML
  for notebooks or sharing, with one polygon per acquisition and a popup
  showing each item's metadata and an "open" link.

The first surface is the important one: Umbra acquisitions are points on
the planet, and being able to *see* where a search landed before
downloading multi-gigabyte SAR files is the difference between exploring
the archive and giving up.

Install the optional dependency for the interactive map with::

    pip install "umbra-py[viz]"
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .exceptions import MissingDependencyError
from .models import UmbraItem


def _require(module: str):
    try:
        return __import__(module)
    except ImportError as exc:  # pragma: no cover - only without extra
        raise MissingDependencyError(
            f"'{module}' is required for interactive maps. "
            'Install the extra with: pip install "umbra-py[viz]"'
        ) from exc


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


def _popup_html(item: UmbraItem) -> str:
    info = item.metadata_summary()
    rng, azi = info["resolution_range_m"], info["resolution_azimuth_m"]

    def fmt(v: Any, suffix: str = "") -> str:
        if v is None:
            return "&mdash;"
        if isinstance(v, float):
            return f"{v:.2f}{suffix}"
        return f"{v}{suffix}"

    rows = [
        ("ID", info["id"]),
        ("Acquired", info["datetime"] or "&mdash;"),
        ("Platform", fmt(info["platform"])),
        ("Mode", fmt(info["instrument_mode"])),
        ("Product", fmt(info["product_type"])),
        ("Polarizations", ", ".join(info["polarizations"]) or "&mdash;"),
        ("Incidence", fmt(info["incidence_angle_deg"], "&deg;")),
        ("Resolution (rng × azi)", f"{fmt(rng, ' m')} × {fmt(azi, ' m')}"),
        ("Assets", ", ".join(info["available_assets"]) or "&mdash;"),
    ]
    body = "".join(
        f"<tr><th style='text-align:left;padding-right:8px'>{k}</th><td>{v}</td></tr>"
        for k, v in rows
    )
    link = (
        f"<p style='margin-top:6px'><a href='{item.href}' target='_blank'>open STAC item</a></p>"
        if item.href
        else ""
    )
    return f"<table style='font-family:sans-serif;font-size:12px'>{body}</table>{link}"


def footprint_map(
    items: Iterable[UmbraItem],
    *,
    tiles: str = "OpenStreetMap",
    color: str = "#ff5500",
    weight: int = 2,
    fill_opacity: float = 0.15,
    zoom_start: int | None = None,
):
    """Build an interactive Folium map of one or more Umbra acquisitions.

    The map auto-fits the union of footprints and renders each item as a
    polygon with a metadata popup. Items without a geometry or bbox are
    silently skipped.

    Requires the ``viz`` extra (``pip install "umbra-py[viz]"``). Returns
    a ``folium.Map`` you can ``.save("out.html")`` or display in Jupyter.
    """
    folium = _require("folium")

    items = list(items)
    features = [(i, _geometry_for(i)) for i in items]
    features = [(i, g) for i, g in features if g is not None]

    bbox = _union_bbox([item_to_feature(i) for i, _ in features])
    if bbox is not None:
        center = ((bbox[1] + bbox[3]) / 2, (bbox[0] + bbox[2]) / 2)
    else:
        center = (0.0, 0.0)

    m = folium.Map(location=center, tiles=tiles, zoom_start=zoom_start or 2)

    for item, geometry in features:
        folium.GeoJson(
            {"type": "Feature", "geometry": geometry, "properties": {}},
            style_function=lambda _f, c=color, w=weight, fo=fill_opacity: {
                "color": c,
                "weight": w,
                "fillOpacity": fo,
            },
            tooltip=item.id,
            popup=folium.Popup(_popup_html(item), max_width=420),
        ).add_to(m)

    if bbox is not None and len(features) > 0:
        # Folium expects [[south, west], [north, east]].
        m.fit_bounds([[bbox[1], bbox[0]], [bbox[3], bbox[2]]])

    return m


def save_footprint_map(
    items: Iterable[UmbraItem],
    dest: str | os.PathLike,
    **kwargs,
) -> Path:
    """Build a footprint map and write it to ``dest`` as standalone HTML."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    footprint_map(items, **kwargs).save(str(dest))
    return dest
