"""Stdlib-only polygon geometry for ``intersects`` search.

Umbra's open catalog has no search API -- the library *is* the search layer,
and its only spatial filter so far was a bounding box (:meth:`UmbraItem.
intersects_bbox`). A rectangle is coarse: a diagonal coast, a river basin, or
any drawn area of interest sweeps in a lot of empty ocean or neighbouring land.
This module adds a true polygon filter -- keep only acquisitions whose footprint
*intersects* a caller-supplied GeoJSON polygon -- so ``search`` matches the
standard STAC ``intersects`` parameter every geo tool already speaks.

It is deliberately dependency-free (no shapely): the whole test is a handful of
closed-form primitives -- a bounding-box reject, segment-crossing, and a
ray-cast point-in-polygon -- over plain ``(lon, lat)`` tuples, so it stays in
the library's pure-Python, offline-testable core rather than pulling a compiled
geometry stack into the base install.

Two simplifications, both documented and both *over*-inclusive (they can only
keep an item, never wrongly drop one -- the safe direction for a discovery
filter):

* Interior rings (polygon holes) are ignored; only exterior rings are tested.
  Umbra footprints are simple polygons without holes.
* Coordinates are treated as planar lon/lat. A polygon spanning the
  antimeridian is not split, exactly as the existing bbox filter doesn't.
"""

from __future__ import annotations

import json
from typing import Any

#: A single ``(lon, lat)`` position.
Position = tuple[float, float]
#: An ordered ring of positions (a polygon boundary).
Ring = list[Position]
#: The normalised form this module works in: a geometry is a list of exterior
#: rings (one per polygon; a MultiPolygon contributes several). Holes dropped.
Geometry = list[Ring]

BBox = tuple[float, float, float, float]


# --------------------------------------------------------------------------- #
# Parsing GeoJSON -> exterior rings.
# --------------------------------------------------------------------------- #


def _ring_positions(ring: Any) -> Ring:
    if not isinstance(ring, (list, tuple)):
        raise ValueError("a polygon ring must be a list of positions")
    out: Ring = []
    for pos in ring:
        if (
            isinstance(pos, (list, tuple))
            and len(pos) >= 2
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in pos[:2])
        ):
            out.append((float(pos[0]), float(pos[1])))
        else:
            raise ValueError("a polygon position must be [lon, lat] numbers")
    if len(out) < 3:
        raise ValueError("a polygon ring needs at least 3 positions")
    return out


def _polygon_exterior(coords: Any) -> Ring:
    # GeoJSON Polygon coordinates are ``[exterior, hole1, ...]``; take the
    # exterior ring and drop any holes.
    if not isinstance(coords, (list, tuple)) or not coords:
        raise ValueError("Polygon coordinates must be a non-empty list of rings")
    return _ring_positions(coords[0])


def _geometry_rings(geom: Any) -> Geometry:
    if not isinstance(geom, dict):
        raise ValueError("geometry must be a GeoJSON object")
    gtype = geom.get("type")
    if gtype == "Feature":
        return _geometry_rings(geom.get("geometry"))
    if gtype == "FeatureCollection":
        rings: Geometry = []
        for feat in geom.get("features") or []:
            rings.extend(_geometry_rings((feat or {}).get("geometry")))
        if not rings:
            raise ValueError("FeatureCollection has no polygon features")
        return rings
    if gtype == "GeometryCollection":
        rings = []
        for sub in geom.get("geometries") or []:
            rings.extend(_geometry_rings(sub))
        if not rings:
            raise ValueError("GeometryCollection has no polygon geometries")
        return rings
    coords = geom.get("coordinates")
    if gtype == "Polygon":
        return [_polygon_exterior(coords)]
    if gtype == "MultiPolygon":
        if not isinstance(coords, (list, tuple)) or not coords:
            raise ValueError("MultiPolygon coordinates must be a non-empty list of polygons")
        return [_polygon_exterior(poly) for poly in coords]
    raise ValueError(
        f"unsupported geometry type {gtype!r}; intersects needs a Polygon or "
        "MultiPolygon (optionally wrapped in a Feature / FeatureCollection)"
    )


def parse_geometry(value: dict | str) -> Geometry:
    """Parse a GeoJSON geometry into exterior rings for intersection testing.

    ``value`` is a GeoJSON ``dict`` (a ``Polygon`` / ``MultiPolygon`` geometry,
    or a ``Feature`` / ``FeatureCollection`` / ``GeometryCollection`` wrapping
    them) or a JSON string of one. Returns the normalised :data:`Geometry` (a
    list of exterior rings). Raises :class:`ValueError` on anything that isn't a
    usable polygon, so callers can validate at the boundary.
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"intersects is not valid JSON: {exc}") from exc
    return _geometry_rings(value)


def rings_from_geojson(geom: Any) -> Geometry | None:
    """Exterior rings of a GeoJSON geometry, or ``None`` if it isn't a polygon.

    The forgiving sibling of :func:`parse_geometry` for data already in hand (an
    item's own footprint): a non-polygon footprint (or a missing one) yields
    ``None`` so the caller can fall back to the bbox rectangle rather than raise.
    """
    try:
        return _geometry_rings(geom)
    except ValueError:
        return None


def bbox_ring(bbox: BBox) -> Ring:
    """The closed rectangular ring of a ``(min_lon, min_lat, max_lon, max_lat)``."""
    min_lon, min_lat, max_lon, max_lat = bbox
    return [
        (min_lon, min_lat),
        (max_lon, min_lat),
        (max_lon, max_lat),
        (min_lon, max_lat),
        (min_lon, min_lat),
    ]


def to_geojson(geometry: Geometry) -> dict | None:
    """A GeoJSON ``Polygon`` / ``MultiPolygon`` for the normalised rings.

    The inverse of :func:`parse_geometry` (holes already dropped), for handing
    the geometry to a STAC API's ``intersects``. Returns ``None`` if empty.
    """
    if not geometry:
        return None
    if len(geometry) == 1:
        return {"type": "Polygon", "coordinates": [[list(p) for p in geometry[0]]]}
    return {
        "type": "MultiPolygon",
        "coordinates": [[[list(p) for p in ring]] for ring in geometry],
    }


def geometry_bbox(geometry: Geometry) -> BBox | None:
    """Bounding box enclosing every ring, or ``None`` for an empty geometry.

    Used to derive the cheap bbox a STAC API can push down before the exact
    polygon test runs client-side.
    """
    lons = [p[0] for ring in geometry for p in ring]
    lats = [p[1] for ring in geometry for p in ring]
    if not lons or not lats:
        return None
    return (min(lons), min(lats), max(lons), max(lats))


# --------------------------------------------------------------------------- #
# Intersection primitives.
# --------------------------------------------------------------------------- #


def _ring_bbox(ring: Ring) -> BBox:
    lons = [p[0] for p in ring]
    lats = [p[1] for p in ring]
    return (min(lons), min(lats), max(lons), max(lats))


def _bbox_overlaps(a: BBox, b: BBox) -> bool:
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


def _orient(a: Position, b: Position, c: Position) -> float:
    """Signed area (cross product) of ``a->b`` and ``a->c``; sign is the turn."""
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _on_segment(a: Position, b: Position, c: Position) -> bool:
    """Whether collinear point ``c`` lies within segment ``a->b``'s extent."""
    return min(a[0], b[0]) <= c[0] <= max(a[0], b[0]) and min(a[1], b[1]) <= c[1] <= max(a[1], b[1])


def _segments_cross(p1: Position, p2: Position, p3: Position, p4: Position) -> bool:
    """Whether segment ``p1-p2`` intersects segment ``p3-p4`` (touching counts)."""
    d1 = _orient(p3, p4, p1)
    d2 = _orient(p3, p4, p2)
    d3 = _orient(p1, p2, p3)
    d4 = _orient(p1, p2, p4)
    if ((d1 > 0 and d2 < 0) or (d1 < 0 and d2 > 0)) and (
        (d3 > 0 and d4 < 0) or (d3 < 0 and d4 > 0)
    ):
        return True
    if d1 == 0 and _on_segment(p3, p4, p1):
        return True
    if d2 == 0 and _on_segment(p3, p4, p2):
        return True
    if d3 == 0 and _on_segment(p1, p2, p3):
        return True
    if d4 == 0 and _on_segment(p1, p2, p4):
        return True
    return False


def _point_in_ring(pt: Position, ring: Ring) -> bool:
    """Ray-cast test: is ``pt`` inside ``ring``? (Boundary is not guaranteed.)"""
    x, y = pt
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > y) != (yj > y):
            x_cross = xi + (y - yi) / (yj - yi) * (xj - xi)
            if x < x_cross:
                inside = not inside
        j = i
    return inside


def _rings_intersect(a: Ring, b: Ring) -> bool:
    if not _bbox_overlaps(_ring_bbox(a), _ring_bbox(b)):
        return False
    na, nb = len(a), len(b)
    for i in range(na):
        a1, a2 = a[i], a[(i + 1) % na]
        for k in range(nb):
            b1, b2 = b[k], b[(k + 1) % nb]
            if _segments_cross(a1, a2, b1, b2):
                return True
    # No edges cross: the rings are disjoint or one wholly contains the other.
    # Testing one vertex of each against the other settles containment.
    if _point_in_ring(a[0], b):
        return True
    if _point_in_ring(b[0], a):
        return True
    return False


def geometries_intersect(a: Geometry, b: Geometry) -> bool:
    """Whether any exterior ring of ``a`` intersects any exterior ring of ``b``."""
    for ring_a in a:
        for ring_b in b:
            if _rings_intersect(ring_a, ring_b):
                return True
    return False
