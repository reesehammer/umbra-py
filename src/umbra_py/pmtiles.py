"""Whole-catalog vector tiling: a stdlib-only PMTiles writer (``umbra tiles``).

Every other map surface in the toolkit embeds its features *in the page*:
``umbra map`` writes one Folium polygon per item, and ``umbra demo`` ships the
gathered slice as an inline JSON blob its clustered Leaflet layer reads. Both
are excellent up to a few hundred–few thousand acquisitions; both hit the same
wall at the *whole acquisition set* the demo-gap analysis
(:doc:`DEMO_APP_GAPS`, Path A step 3) names as the last open gap — thousands of
DOM markers, or a multi-megabyte JSON blob, that no browser wants to hold at
once.

The standard answer for "a whole catalog on one map that stays fast" is a
**vector tile pyramid**: the catalog is pre-cut into small tiles keyed by
``(z, x, y)``, and the map fetches only the handful covering the current view at
the current zoom. `PMTiles <https://docs.protomaps.com/pmtiles/>`_ packages that
pyramid as a *single* file — no tile server, no thousands of small files — so it
drops straight onto GitHub Pages or into an S3 bucket beside the catalog and is
read by range requests, exactly the static-hosting grain the rest of this
project keeps.

Deliberately in the repo's grain:

* **No extra, no tippecanoe.** The demo-gap doc sketched this step as
  ``export GeoJSON → tile with tippecanoe`` — an external binary. The whole
  encoder — the `Mapbox Vector Tile
  <https://github.com/mapbox/vector-tile-spec>`_ protobuf and the `PMTiles v3
  <https://github.com/protomaps/PMTiles/blob/main/spec/v3/spec.md>`_ container —
  fits in the standard library (``struct``, ``gzip``, varint arithmetic),
  polygon clipping included. So the generator runs in a core install and is
  fully offline-testable by decoding its own output, the same discipline
  :mod:`umbra_py.export` and the STAC document builders hold.

* **The viewer is a static sibling, not a rewrite of the demo.** :func:`build_viewer`
  emits a self-contained MapLibre GL page that points at a ``.pmtiles`` URL via
  the pinned ``pmtiles`` protocol plugin; it renders the *whole* catalog as a
  scalable circle layer with the same OpenStreetMap basemap and mandatory CC-BY
  attribution the Leaflet demo uses. Keeping it separate leaves the proven
  ``umbra demo`` page untouched — the two are complementary: ``demo`` for the
  interactive, filter-and-click slice; ``tiles`` for the fast, zoom-anywhere
  whole-archive view. The archive this module writes is also what the explorer
  reads in its whole-archive mode (``umbra demo --pmtiles`` /
  :func:`umbra_py.demo.build_demo`'s ``pmtiles_url``), which gets the filters and
  the zoom-anywhere reach at once; :func:`build_viewer` stays the minimal,
  no-sidebar view of the same data.

Each acquisition is tiled **twice**: as a centroid point in the
``acquisitions`` layer (what a whole-world view needs — one marker per scene at
any zoom), and, from :data:`FOOTPRINT_MIN_ZOOM` up, as its clipped footprint
polygon in the ``footprints`` layer, so zooming in shows *coverage shape*
rather than a dot. The polygon layer starts partway down the pyramid on
purpose: at world zooms a 5–25 km footprint is smaller than a pixel, and the
low-zoom tiles are the ones every visitor loads first. Both layers carry the
same lean set of string properties (id, place, product, date, platform, the
comma-joined ``pol`` and ``assets``) — enough to style, label, filter and click
a feature; the full metadata still lives one STAC link away.

They also carry a **reference to the acquisition's GEC cloud-optimized GeoTIFF**
(``cog``, plus the ``bounds`` to place it), which is what lets a viewer over the
archive stream the actual radar picture on click rather than stopping at
metadata — the last capability the embedded-slice ``umbra demo`` had over the
whole-archive one (``DEMO_APP_GAPS.md`` Path A). It stays lean because the
product sits *next to* the item's STAC sidecar in the public bucket, so what is
tiled is the bare filename and the page rebuilds the URL against the
``stac_href`` it already carries. Pass ``cog_asset=None`` for a metadata-only
archive.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import struct
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, NamedTuple

from ._geometry import Ring, bbox_ring, rings_from_geojson
from .constants import ATTRIBUTION, CATALOG_INDEX_PMTILES_URL
from .exceptions import AssetNotFoundError
from .models import UmbraItem

# --- PMTiles v3 header constants -----------------------------------------
_MAGIC = b"PMTiles"
_VERSION = 3
_COMPRESSION_NONE = 1
_COMPRESSION_GZIP = 2
_TILETYPE_MVT = 1
# MVT tile extent: the integer coordinate space inside each tile. 4096 is the
# spec's near-universal default (the value tippecanoe and MapLibre assume).
_EXTENT = 4096
# How far outside its tile a clipped polygon may keep coordinates. MVT allows
# out-of-extent geometry, and that margin is what puts a footprint's clipped
# edge *under* the neighbouring tile's seam instead of drawn on it. 64/4096
# matches the buffer tippecanoe writes by default.
_TILE_BUFFER = 64.0

#: Source-layer name of the acquisition-footprint polygons (the point centroids
#: live in ``build_pmtiles``' ``layer_name``, default ``"acquisitions"``).
FOOTPRINT_LAYER = "footprints"
#: Lowest zoom at which footprint polygons are written. Below this a footprint
#: is sub-pixel, and these are the tiles every visitor loads first.
FOOTPRINT_MIN_ZOOM = 6

#: Product whose COG each tiled feature references by default, so a viewer can
#: stream the picture on click. GEC is the detected-amplitude, map-projected
#: GeoTIFF -- the one product that is both cloud-optimized and directly
#: displayable, and the same default ``umbra map``/``umbra demo`` use.
DEFAULT_COG_ASSET = "GEC"

# Pinned CDN assets for the viewer. An unpinned CDN can regress a generated page
# without warning, the same discipline the Leaflet demo and _lazy_imagery apply.
MAPLIBRE_JS = "https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js"
MAPLIBRE_CSS = "https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css"
PMTILES_JS = "https://unpkg.com/pmtiles@3.2.1/dist/pmtiles.js"


# --- varint / zigzag primitives (protobuf + PMTiles directory share these) ---
def _uvarint(value: int) -> bytes:
    """Encode a non-negative integer as an unsigned LEB128 varint."""
    if value < 0:
        raise ValueError("uvarint cannot encode a negative value")
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _zigzag(value: int) -> int:
    """Map a signed integer to an unsigned one (protobuf/MVT parameter encoding)."""
    return (value << 1) ^ (value >> 63) if value < 0 else value << 1


# --- Web Mercator projection ---------------------------------------------
def _lonlat_to_tile_fraction(lon: float, lat: float, zoom: int) -> tuple[float, float]:
    """Project ``(lon, lat)`` to fractional tile coordinates at ``zoom``.

    Returns ``(fx, fy)`` where the integer parts are the tile column/row and the
    fractional parts locate the point inside that tile (Web Mercator / the XYZ
    slippy-map convention every vector-tile client uses).
    """
    lat = max(min(lat, 85.05112878), -85.05112878)
    n = 1 << zoom
    fx = (lon + 180.0) / 360.0 * n
    siny = math.sin(math.radians(lat))
    fy = (0.5 - math.log((1 + siny) / (1 - siny)) / (4 * math.pi)) * n
    return fx, fy


# --- Hilbert-curve tile id (PMTiles orders tiles along a Hilbert curve) ---
def zxy_to_tileid(zoom: int, x: int, y: int) -> int:
    """Return the 64-bit PMTiles tile id for a ``(zoom, x, y)`` tile.

    PMTiles orders tiles along a Hilbert space-filling curve so that
    geographically near tiles are near in the file (good range-read locality).
    The id is the count of all tiles at lower zooms plus the tile's Hilbert
    index within its zoom.
    """
    if zoom < 0 or zoom > 26:
        raise ValueError("zoom out of range for a PMTiles tile id")
    n = 1 << zoom
    if not (0 <= x < n and 0 <= y < n):
        raise ValueError("tile x/y out of range for its zoom")
    # Tiles at zooms 0..zoom-1 number (4**zoom - 1) / 3.
    acc = ((1 << (zoom * 2)) - 1) // 3
    d = 0
    tx, ty = x, y
    s = n >> 1
    while s > 0:
        rx = 1 if (tx & s) > 0 else 0
        ry = 1 if (ty & s) > 0 else 0
        d += s * s * ((3 * rx) ^ ry)
        # Rotate the quadrant.
        if ry == 0:
            if rx == 1:
                tx = s - 1 - tx
                ty = s - 1 - ty
            tx, ty = ty, tx
        s >>= 1
    return acc + d


# --- Mapbox Vector Tile encoding (points only) ---------------------------
def _encode_value(value: Any) -> bytes:
    """Encode one MVT ``Value`` message body (the field, not the wrapping tag)."""
    if isinstance(value, bool):
        # bool before int: bool is a subclass of int in Python.
        return _uvarint((7 << 3) | 0) + _uvarint(1 if value else 0)
    if isinstance(value, str):
        raw = value.encode("utf-8")
        return _uvarint((1 << 3) | 2) + _uvarint(len(raw)) + raw
    if isinstance(value, int):
        # sint_value (field 6): zigzag varint, handles negatives compactly.
        return _uvarint((6 << 3) | 0) + _uvarint(_zigzag(value))
    if isinstance(value, float):
        # double_value (field 3): 64-bit little-endian IEEE-754.
        return _uvarint((3 << 3) | 1) + struct.pack("<d", value)
    raise TypeError(f"unsupported MVT value type: {type(value).__name__}")


class _Feature(NamedTuple):
    """One tile feature: a geometry type, its parts, and its properties.

    ``parts`` is a list of point sequences in the tile's integer coordinate
    space: one single-point part for a POINT, one part per (already clipped and
    clockwise-wound) exterior ring for a POLYGON.
    """

    geom_type: int  # MVT GeomType: 1 = POINT, 3 = POLYGON
    parts: list[list[tuple[int, int]]]
    props: dict[str, Any]


def _encode_geometry(feature: _Feature) -> list[int]:
    """The MVT command/parameter integers for one feature's geometry.

    The cursor starts at ``(0, 0)`` for every feature and every point is written
    as a zigzagged delta from it, so a part's first point is a MoveTo and the
    rest one LineTo run; a polygon part ends with ClosePath (which re-states the
    ring's first point, so the closing duplicate must already be dropped).
    """
    cmds: list[int] = []
    cx = cy = 0
    for part in feature.parts:
        fx, fy = part[0]
        cmds += [1 | (1 << 3), _zigzag(fx - cx), _zigzag(fy - cy)]  # MoveTo, count 1
        cx, cy = fx, fy
        rest = part[1:]
        if rest:
            cmds.append(2 | (len(rest) << 3))  # LineTo, count len(rest)
            for px, py in rest:
                cmds += [_zigzag(px - cx), _zigzag(py - cy)]
                cx, cy = px, py
        if feature.geom_type == 3:
            cmds.append(7 | (1 << 3))  # ClosePath, count 1
    return cmds


def _encode_layer(layer_name: str, features: list[_Feature]) -> bytes:
    """Encode one MVT ``Layer`` message body (the field body, not its tag)."""
    keys: list[str] = []
    key_index: dict[str, int] = {}
    values: list[bytes] = []
    value_index: dict[tuple[str, Any], int] = {}

    def intern_key(name: str) -> int:
        if name not in key_index:
            key_index[name] = len(keys)
            keys.append(name)
        return key_index[name]

    def intern_value(value: Any) -> int:
        vkey = (type(value).__name__, value)
        if vkey not in value_index:
            value_index[vkey] = len(values)
            values.append(_encode_value(value))
        return value_index[vkey]

    feature_msgs: list[bytes] = []
    for fid, feature in enumerate(features):
        tags: list[int] = []
        for name, value in feature.props.items():
            if value is None:
                continue
            tags.append(intern_key(name))
            tags.append(intern_value(value))

        body = bytearray()
        body += _uvarint((1 << 3) | 0) + _uvarint(fid)  # id
        if tags:
            packed_tags = b"".join(_uvarint(t) for t in tags)
            body += _uvarint((2 << 3) | 2) + _uvarint(len(packed_tags)) + packed_tags
        body += _uvarint((3 << 3) | 0) + _uvarint(feature.geom_type)  # type
        packed_geom = b"".join(_uvarint(g) for g in _encode_geometry(feature))
        body += _uvarint((4 << 3) | 2) + _uvarint(len(packed_geom)) + packed_geom
        feature_msgs.append(bytes(body))

    layer = bytearray()
    layer += _uvarint((15 << 3) | 0) + _uvarint(2)  # version = 2
    name_raw = layer_name.encode("utf-8")
    layer += _uvarint((1 << 3) | 2) + _uvarint(len(name_raw)) + name_raw
    for feat in feature_msgs:
        layer += _uvarint((2 << 3) | 2) + _uvarint(len(feat)) + feat
    for name in keys:
        raw = name.encode("utf-8")
        layer += _uvarint((3 << 3) | 2) + _uvarint(len(raw)) + raw
    for val in values:
        layer += _uvarint((4 << 3) | 2) + _uvarint(len(val)) + val
    layer += _uvarint((5 << 3) | 0) + _uvarint(_EXTENT)  # extent
    return bytes(layer)


def _encode_mvt(layers: list[tuple[str, list[_Feature]]]) -> bytes:
    """Encode one vector tile from ``(layer_name, features)`` pairs.

    Empty layers are skipped, so a tile that only carries footprints (or only
    centroids) holds just the one layer. Returns the uncompressed protobuf bytes
    of the ``Tile`` message.
    """
    tile = bytearray()
    for layer_name, features in layers:
        if not features:
            continue
        body = _encode_layer(layer_name, features)
        tile += _uvarint((3 << 3) | 2) + _uvarint(len(body)) + body  # layers
    return bytes(tile)


# --- PMTiles directory serialization -------------------------------------
def _serialize_directory(entries: list[tuple[int, int, int, int]]) -> bytes:
    """Serialize directory ``entries`` (``tile_id, offset, length, run_length``).

    ``entries`` must be sorted by ``tile_id``. Follows the PMTiles v3 columnar
    layout: counts, then delta-encoded ids, run lengths, lengths, and offsets
    (with 0 meaning "immediately after the previous entry").
    """
    buf = bytearray()
    buf += _uvarint(len(entries))
    last_id = 0
    for tile_id, _off, _length, _run in entries:
        buf += _uvarint(tile_id - last_id)
        last_id = tile_id
    for _id, _off, _length, run in entries:
        buf += _uvarint(run)
    for _id, _off, length, _run in entries:
        buf += _uvarint(length)
    for i, (_id, off, _length, _run) in enumerate(entries):
        if i > 0:
            prev_id, prev_off, prev_len, _prev_run = entries[i - 1]
            if off == prev_off + prev_len:
                buf += _uvarint(0)
                continue
        buf += _uvarint(off + 1)
    return bytes(buf)


def _pack_header(
    *,
    root_dir_offset: int,
    root_dir_length: int,
    metadata_offset: int,
    metadata_length: int,
    leaf_offset: int,
    leaf_length: int,
    tile_data_offset: int,
    tile_data_length: int,
    num_addressed: int,
    num_entries: int,
    num_contents: int,
    clustered: int,
    min_zoom: int,
    max_zoom: int,
    bounds: tuple[float, float, float, float],
    center: tuple[float, float],
    center_zoom: int,
) -> bytes:
    """Pack the 127-byte PMTiles v3 header."""

    def e7(value: float) -> int:
        return int(round(value * 1e7))

    min_lon, min_lat, max_lon, max_lat = bounds
    center_lon, center_lat = center
    header = struct.pack(
        "<7sB",
        _MAGIC,
        _VERSION,
    )
    header += struct.pack(
        "<QQQQQQQQ",
        root_dir_offset,
        root_dir_length,
        metadata_offset,
        metadata_length,
        leaf_offset,
        leaf_length,
        tile_data_offset,
        tile_data_length,
    )
    header += struct.pack("<QQQ", num_addressed, num_entries, num_contents)
    header += struct.pack(
        "<BBBBBB",
        clustered,
        _COMPRESSION_GZIP,  # internal (directory + metadata) compression
        _COMPRESSION_GZIP,  # tile compression
        _TILETYPE_MVT,
        min_zoom,
        max_zoom,
    )
    header += struct.pack("<iiii", e7(min_lon), e7(min_lat), e7(max_lon), e7(max_lat))
    header += struct.pack("<B", center_zoom)
    header += struct.pack("<ii", e7(center_lon), e7(center_lat))
    assert len(header) == 127, f"PMTiles header must be 127 bytes, got {len(header)}"
    return header


def _gzip(data: bytes) -> bytes:
    """Deterministic gzip (fixed mtime) so identical input yields identical bytes."""
    return gzip.compress(data, mtime=0)


def _item_point(item: UmbraItem) -> tuple[float, float] | None:
    """Return an item's ``(lon, lat)`` centroid, or None if it has no footprint."""
    if item.bbox is None:
        return None
    min_lon, min_lat, max_lon, max_lat = item.bbox
    return ((min_lon + max_lon) / 2.0, (min_lat + max_lat) / 2.0)


def _item_rings(item: UmbraItem) -> list[Ring] | None:
    """An item's exterior footprint rings in lon/lat, or None if unusable.

    Prefers the item's own polygon and falls back to the bbox rectangle — the
    same order :meth:`UmbraItem.intersects_polygon` uses. A ring spanning more
    than half the globe is dropped: an Umbra footprint is at most tens of
    kilometres across, so such a span means the ring straddles the antimeridian,
    where the lon/lat path is not the footprint (and would tile a world-wide row
    of it). The centroid point is still written for those items.
    """
    rings = rings_from_geojson(item.geometry)
    if rings is None:
        rings = [bbox_ring(item.bbox)] if item.bbox is not None else []
    usable = [r for r in rings if max(p[0] for p in r) - min(p[0] for p in r) <= 180.0]
    return usable or None


def _open_ring(ring: Ring) -> Ring:
    """Drop a closed ring's repeated final position (MVT's ClosePath implies it)."""
    return list(ring[:-1]) if len(ring) > 1 and ring[0] == ring[-1] else list(ring)


def _clip_edge(ring: Ring, axis: int, bound: float, keep_ge: bool) -> Ring:
    """Sutherland–Hodgman clip of ``ring`` against one axis-aligned half-plane."""

    def inside(pt: tuple[float, float]) -> bool:
        return pt[axis] >= bound if keep_ge else pt[axis] <= bound

    out: Ring = []
    for i, cur in enumerate(ring):
        nxt = ring[(i + 1) % len(ring)]
        cur_in, nxt_in = inside(cur), inside(nxt)
        if cur_in:
            out.append(cur)
        if cur_in != nxt_in:
            t = (bound - cur[axis]) / (nxt[axis] - cur[axis])
            out.append((cur[0] + (nxt[0] - cur[0]) * t, cur[1] + (nxt[1] - cur[1]) * t))
    return out


def _clip_ring(ring: Ring, lo: float, hi: float) -> Ring:
    """Clip ``ring`` to the square ``[lo, hi]`` in both axes (empty if outside).

    Sutherland–Hodgman against a convex box: exact for the convex quadrilaterals
    SAR footprints are, and for a concave ring it can only add a degenerate edge
    along the box (which renders identically), never lose covered area.
    """
    clipped = ring
    for axis, bound, keep_ge in ((0, lo, True), (0, hi, False), (1, lo, True), (1, hi, False)):
        clipped = _clip_edge(clipped, axis, bound, keep_ge)
        if not clipped:
            return []
    return clipped


def _quantize_ring(ring: Ring) -> list[tuple[int, int]]:
    """Round a ring to integer tile coordinates, dropping repeated positions."""
    out: list[tuple[int, int]] = []
    for x, y in ring:
        pt = (int(round(x)), int(round(y)))
        if not out or pt != out[-1]:
            out.append(pt)
    if len(out) > 1 and out[0] == out[-1]:
        out.pop()
    return out


def _signed_area(ring: list[tuple[int, int]]) -> float:
    """Twice the shoelace area; positive means clockwise in MVT's y-down space."""
    total = 0
    for i, (x1, y1) in enumerate(ring):
        x2, y2 = ring[(i + 1) % len(ring)]
        total += x1 * y2 - x2 * y1
    return total / 2.0


def _tile_polygons(
    rings: list[Ring], zoom: int
) -> dict[tuple[int, int], list[list[tuple[int, int]]]]:
    """Clip footprint ``rings`` into every tile they touch at ``zoom``.

    Returns ``{(tile_x, tile_y): [ring, ...]}`` with each ring in that tile's
    integer coordinate space (buffered past the extent, per :data:`_TILE_BUFFER`)
    and wound clockwise, as the MVT spec requires of an exterior ring.
    """
    n = 1 << zoom
    margin = _TILE_BUFFER / _EXTENT  # the buffer, in tiles
    out: dict[tuple[int, int], list[list[tuple[int, int]]]] = {}
    for ring in rings:
        projected = [_lonlat_to_tile_fraction(lon, lat, zoom) for lon, lat in _open_ring(ring)]
        xs = [p[0] for p in projected]
        ys = [p[1] for p in projected]
        x0 = max(int(math.floor(min(xs) - margin)), 0)
        x1 = min(int(math.floor(max(xs) + margin)), n - 1)
        y0 = max(int(math.floor(min(ys) - margin)), 0)
        y1 = min(int(math.floor(max(ys) + margin)), n - 1)
        for tx in range(x0, x1 + 1):
            for ty in range(y0, y1 + 1):
                local = [((fx - tx) * _EXTENT, (fy - ty) * _EXTENT) for fx, fy in projected]
                quantized = _quantize_ring(_clip_ring(local, -_TILE_BUFFER, _EXTENT + _TILE_BUFFER))
                if len(quantized) < 3:
                    continue  # clipped away, or collapsed below a drawable ring
                if _signed_area(quantized) < 0:
                    quantized.reverse()
                out.setdefault((tx, ty), []).append(quantized)
    return out


def _cog_reference(item: UmbraItem, asset: str) -> str | None:
    """The tiled, page-resolvable reference to an item's ``asset`` COG.

    The published products sit *next to* the item's ``*.stac.v2.json`` sidecar
    in the open-data bucket, so for the overwhelmingly common case the COG URL
    is ``dirname(stac_href) + "/" + basename`` — and since every tiled feature
    already carries ``stac_href``, storing the bare **basename** costs ~30 bytes
    a feature instead of ~180 for a full URL repeated at every zoom. When the
    resolved href is *not* a sibling of the sidecar (an item that publishes an
    absolute asset href elsewhere), the full ``https`` URL is stored instead and
    the page uses it as-is. Returns ``None`` when the asset is missing or its
    URL cannot be resolved to something anonymously fetchable.
    """
    try:
        href = item.asset_href(asset)
    except AssetNotFoundError:
        return None
    if not href.startswith(("http://", "https://")):
        return None  # an empty or s3:// href is not fetchable from a browser
    sibling_prefix = (
        f"{item.href.rsplit('/', 1)[0]}/"
        if item.href and item.href.startswith(("http://", "https://"))
        else None
    )
    if sibling_prefix and href.startswith(sibling_prefix):
        basename = href[len(sibling_prefix) :]
        # Only a *direct* sibling collapses to a basename; anything with a
        # further path segment must stay absolute or the page would rebuild the
        # wrong URL.
        if "/" not in basename:
            return basename
    return href


def _cog_bounds(item: UmbraItem) -> str | None:
    """Placement bounds for the on-click overlay, as ``"S,W,N,E"``.

    The string form (rather than four numeric fields) is deliberate: it is the
    exact ``data-bounds`` payload
    :func:`umbra_py._lazy_imagery.popup_button_html` writes, so the page hands
    it to the shared driver untouched. Coordinates are rounded to 5 decimals
    (~1 m) — the overlay is a bbox-stretched quick look, so more precision would
    only inflate every tile.
    """
    if item.bbox is None:
        return None
    min_lon, min_lat, max_lon, max_lat = item.bbox
    return ",".join(
        f"{v:.5f}".rstrip("0").rstrip(".") for v in (min_lat, min_lon, max_lat, max_lon)
    )


def _item_properties(item: UmbraItem, cog_asset: str | None = None) -> dict[str, Any]:
    """The lean string properties each tiled point carries (id, place, ...).

    Two of them are *lists* in the item and comma-joined strings here — ``pol``
    (:attr:`~umbra_py.models.UmbraItem.polarizations`) and ``assets``
    (:attr:`~umbra_py.models.UmbraItem.available_assets`) — because a vector
    tile's property values are scalars. Comma-joined is enough for both readers:
    the explorer's polarization filter is a substring test compiled to a
    MapLibre ``index-of`` expression, and no two-letter polarization code can
    match across a separator. ``pol`` is the facet that matters most for
    correctness rather than display: differencing an HH pass against a VV one
    puts a scattering difference on the time axis where it reads as change (see
    :data:`~umbra_py.constants.POLARIZATION_CAVEAT`), and ``POST
    /artifacts/stats`` refuses such a selection outright, so the whole-archive
    explorer needs the field in the tiles to be able to narrow to one.

    With ``cog_asset`` set, two more ride along — ``cog`` (the asset's COG,
    basename-relative to ``stac_href`` where it is a sibling) and ``bounds``
    (its footprint as ``"S,W,N,E"``) — which is what lets the whole-archive
    explorer offer the same on-click "Get SAR image" overlay as the
    embedded-slice page. Absent for an item whose asset cannot be resolved, so
    the page simply omits the button for it.
    """
    dt = item.datetime
    props: dict[str, Any] = {
        "id": item.id,
        # The baked reverse-geocoded label when the index carries one (see
        # CatalogIndex.bake_places), else the task codename -- the same
        # preference umbra demo and the stac-geoparquet export make, so a
        # `umbra tiles --local` over a baked index shows real place names.
        "place": item.place or item.task,
        "product": item.product_type,
        "date": dt.date().isoformat() if dt else None,
        "platform": item.platform,
        # Empty joins fall out below with the other Nones, so an item with no
        # polarization metadata (or no products) carries no key at all rather
        # than an empty string -- the filter's "has" guard keys off exactly that.
        "pol": ",".join(item.polarizations) or None,
        "assets": ",".join(item.available_assets) or None,
        "stac_href": item.href,
    }
    if cog_asset:
        cog = _cog_reference(item, cog_asset)
        bounds = _cog_bounds(item)
        if cog and bounds:
            props["cog"] = cog
            props["bounds"] = bounds
    return {k: v for k, v in props.items() if v is not None}


def build_pmtiles(
    items: Iterable[UmbraItem],
    *,
    min_zoom: int = 0,
    max_zoom: int = 9,
    layer_name: str = "acquisitions",
    footprints: bool = True,
    footprint_min_zoom: int = FOOTPRINT_MIN_ZOOM,
    cog_asset: str | None = DEFAULT_COG_ASSET,
    name: str = "Umbra open-data catalog",
    description: str | None = None,
) -> bytes:
    """Build a single-file PMTiles archive of the catalog's acquisitions.

    Parameters
    ----------
    items:
        Acquisitions to tile. Any without a footprint bbox are skipped (a point
        cannot be placed for them).
    min_zoom, max_zoom:
        Zoom range to generate. Each item is written into one tile at every zoom
        in ``[min_zoom, max_zoom]`` so the map has a point to draw at any scale.
        The default ``0..9`` covers world view down to city scale, which is where
        SAR sites read individually; raise ``max_zoom`` for denser sites.
    layer_name:
        The vector-tile source-layer name of the centroid points. The viewer's
        style references it, so keep it in sync with :func:`build_viewer` (both
        default to ``"acquisitions"``).
    footprints:
        Also tile each acquisition's footprint *polygon* into the
        :data:`FOOTPRINT_LAYER` layer, clipped to each tile it touches, so a
        zoomed-in map shows coverage shape rather than a marker. Pass ``False``
        for a centroids-only archive (smaller, what earlier versions wrote).
    footprint_min_zoom:
        Lowest zoom carrying footprint polygons (default
        :data:`FOOTPRINT_MIN_ZOOM`). Below it a footprint is sub-pixel, so the
        polygons would only inflate the low-zoom tiles a viewer loads first.
    cog_asset:
        Product whose cloud-optimized GeoTIFF each feature references, so a
        viewer can stream it on click (default :data:`DEFAULT_COG_ASSET`, the
        detected-amplitude GEC; ``"CSI"`` also works). This is what gives
        ``umbra demo --pmtiles`` the same on-click "Get SAR image" overlay as
        the embedded-slice explorer. Pass ``None`` for a metadata-only archive
        with no image references.
    name, description:
        Metadata recorded in the archive (surfaced by PMTiles-aware tooling).

    Returns the ``.pmtiles`` file as bytes; use :func:`write_pmtiles` to save it.
    Raises ``ValueError`` if no item has a footprint (an empty pyramid).
    """
    if min_zoom < 0 or max_zoom < min_zoom or max_zoom > 26:
        raise ValueError("require 0 <= min_zoom <= max_zoom <= 26")

    points: list[tuple[tuple[float, float], dict[str, Any]]] = []
    outlines: list[tuple[list[Ring], dict[str, Any]]] = []
    for item in items:
        point = _item_point(item)
        if point is None:
            continue
        props = _item_properties(item, cog_asset)
        points.append((point, props))
        if footprints and (rings := _item_rings(item)) is not None:
            outlines.append((rings, props))
    if not points:
        raise ValueError("no items with a footprint to tile")

    lons = [lon for (lon, _lat), _p in points]
    lats = [lat for (_lon, lat), _p in points]
    bounds = (min(lons), min(lats), max(lons), max(lats))
    center = ((bounds[0] + bounds[2]) / 2.0, (bounds[1] + bounds[3]) / 2.0)

    # Bucket every point into the tile that holds it at each zoom.
    tiles: dict[tuple[int, int, int], dict[str, list[_Feature]]] = {}
    for (lon, lat), props in points:
        for zoom in range(min_zoom, max_zoom + 1):
            fx, fy = _lonlat_to_tile_fraction(lon, lat, zoom)
            n = 1 << zoom
            tx = min(int(fx), n - 1)
            ty = min(int(fy), n - 1)
            px = min(max(int(round((fx - tx) * _EXTENT)), 0), _EXTENT)
            py = min(max(int(round((fy - ty) * _EXTENT)), 0), _EXTENT)
            layers = tiles.setdefault((zoom, tx, ty), {})
            layers.setdefault(layer_name, []).append(_Feature(1, [[(px, py)]], props))

    # Clip each footprint into every tile it touches, from footprint_min_zoom up.
    footprint_zoom = max(min_zoom, footprint_min_zoom)
    has_footprints = bool(outlines) and footprint_zoom <= max_zoom
    for rings, props in outlines:
        for zoom in range(footprint_zoom, max_zoom + 1):
            for (tx, ty), polygons in _tile_polygons(rings, zoom).items():
                layers = tiles.setdefault((zoom, tx, ty), {})
                layers.setdefault(FOOTPRINT_LAYER, []).append(_Feature(3, polygons, props))

    # Encode each tile, compress, and deduplicate identical contents. Walk tiles
    # in Hilbert (tile_id) order so the data section stays clustered.
    ordered = sorted(tiles.items(), key=lambda kv: zxy_to_tileid(*kv[0]))
    data = bytearray()
    entries: list[tuple[int, int, int, int]] = []
    seen: dict[bytes, tuple[int, int]] = {}
    for (zoom, tx, ty), feats in ordered:
        # A fixed layer order keeps identical content byte-identical (so the
        # dedup below catches it) regardless of which layer a tile saw first.
        blob = _gzip(
            _encode_mvt(
                [
                    (layer_name, feats.get(layer_name, [])),
                    (FOOTPRINT_LAYER, feats.get(FOOTPRINT_LAYER, [])),
                ]
            )
        )
        digest = hashlib.sha256(blob).digest()
        if digest in seen:
            offset, length = seen[digest]
        else:
            offset, length = len(data), len(blob)
            data += blob
            seen[digest] = (offset, length)
        entries.append((zxy_to_tileid(zoom, tx, ty), offset, length, 1))

    directory = _gzip(_serialize_directory(entries))
    fields = {
        "id": "String",
        "place": "String",
        "product": "String",
        "date": "String",
        "platform": "String",
        "pol": "String",
        "assets": "String",
        "stac_href": "String",
    }
    if cog_asset:
        # Declared even if some items resolved no COG -- the field describes the
        # layer's schema, and a viewer keys its button off the per-feature value.
        fields["cog"] = "String"
        fields["bounds"] = "String"
    vector_layers = [
        {
            "id": layer_name,
            "description": "One point per Umbra open-data acquisition.",
            "minzoom": min_zoom,
            "maxzoom": max_zoom,
            "fields": dict(fields),
        }
    ]
    if has_footprints:
        vector_layers.append(
            {
                "id": FOOTPRINT_LAYER,
                "description": "One clipped footprint polygon per Umbra open-data acquisition.",
                "minzoom": footprint_zoom,
                "maxzoom": max_zoom,
                "fields": dict(fields),
            }
        )
    metadata = _gzip(
        json.dumps(
            {
                "name": name,
                "description": description
                or f"{name} — acquisition centroids"
                + (" and footprints." if has_footprints else "."),
                "attribution": ATTRIBUTION,
                "type": "overlay",
                "vector_layers": vector_layers,
            },
            separators=(",", ":"),
        ).encode("utf-8")
    )

    root_dir_offset = 127
    metadata_offset = root_dir_offset + len(directory)
    leaf_offset = metadata_offset + len(metadata)
    tile_data_offset = leaf_offset  # no leaf directories
    header = _pack_header(
        root_dir_offset=root_dir_offset,
        root_dir_length=len(directory),
        metadata_offset=metadata_offset,
        metadata_length=len(metadata),
        leaf_offset=leaf_offset,
        leaf_length=0,
        tile_data_offset=tile_data_offset,
        tile_data_length=len(data),
        num_addressed=len(entries),
        num_entries=len(entries),
        num_contents=len(seen),
        clustered=1,
        min_zoom=min_zoom,
        max_zoom=max_zoom,
        bounds=bounds,
        center=center,
        center_zoom=min_zoom,
    )
    return bytes(header + directory + metadata + data)


def write_pmtiles(items: Iterable[UmbraItem], dest: str | os.PathLike, **kwargs: Any) -> Path:
    """Build a PMTiles archive of ``items`` and write it to ``dest``.

    See :func:`build_pmtiles` for the tiling options.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(build_pmtiles(items, **kwargs))
    return dest


def default_pmtiles_path() -> Path:
    """Default location for the prebuilt whole-catalog PMTiles basemap.

    A sibling of :func:`umbra_py.index.default_index_path` (``catalog.pmtiles``
    beside ``catalog.db`` in the same cache dir), honouring
    ``$UMBRA_PMTILES`` and then ``$XDG_CACHE_HOME`` so the searchable index and
    its visual basemap live together and move together.
    """
    override = os.environ.get("UMBRA_PMTILES")
    if override:
        return Path(override)
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(base) / "umbra-py" / "catalog.pmtiles"


def fetch_prebuilt_pmtiles(
    dest: str | os.PathLike | None = None,
    *,
    url: str | None = None,
    progress: Callable[[int, int | None], None] | None = None,
) -> Path:
    """Download the published whole-catalog PMTiles basemap.

    The weekly index workflow ships a ``catalog.pmtiles`` on the rolling
    ``catalog-index`` release alongside ``catalog.db``, so a fresh install gets a
    fast, zoom-anywhere map of the *whole* archive with no local tiling step --
    the visual sibling of :meth:`umbra_py.index.CatalogIndex.from_release`. This
    fetches that archive straight to ``dest`` (default:
    :func:`default_pmtiles_path`) and returns its path. Re-run any time to
    refresh; the download is resume-safe and always overwrites the existing file.
    ``url`` overrides the release asset location (e.g. to pull from a fork or a
    mirror). Pair it with :func:`build_viewer` / :func:`save_viewer` for a
    ready-to-open MapLibre GL page over the fetched file.
    """
    from .download import download_url  # local dependency; keep the import cheap

    target = Path(dest) if dest is not None else default_pmtiles_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    download_url(url or CATALOG_INDEX_PMTILES_URL, target, overwrite=True, progress=progress)
    return target


# --- MapLibre GL viewer ---------------------------------------------------
def build_viewer(
    pmtiles_url: str,
    *,
    title: str = "Umbra open-data catalog",
    layer_name: str = "acquisitions",
) -> str:
    """Render a self-contained MapLibre GL page over a ``.pmtiles`` catalog.

    ``pmtiles_url`` is the location of the archive relative to the page (e.g.
    ``"catalog.pmtiles"``) or an absolute URL; the page reads it by range
    request via the pinned ``pmtiles`` protocol plugin and draws every
    acquisition as a circle over an OpenStreetMap basemap — plus its footprint
    outline where the archive carries one (:data:`FOOTPRINT_LAYER`, written from
    :data:`FOOTPRINT_MIN_ZOOM` up) — with a click popup and the mandatory CC-BY
    attribution. ``layer_name`` must match the archive's point source-layer
    (:func:`build_pmtiles`' default is ``"acquisitions"``); a centroids-only
    archive simply draws no outlines.
    """
    from html import escape

    config = json.dumps(
        {
            "pmtiles": pmtiles_url,
            "layer": layer_name,
            "footprintLayer": FOOTPRINT_LAYER,
            "attribution": ATTRIBUTION,
        },
        separators=(",", ":"),
    ).replace("</", "<\\/")
    return _VIEWER_TEMPLATE.format(
        title=escape(title),
        maplibre_css=MAPLIBRE_CSS,
        maplibre_js=MAPLIBRE_JS,
        pmtiles_js=PMTILES_JS,
        config_json=config,
        viewer_js=_VIEWER_JS,
    )


def save_viewer(pmtiles_url: str, dest: str | os.PathLike, **kwargs: Any) -> Path:
    """Render a MapLibre viewer for ``pmtiles_url`` and write it to ``dest``."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(build_viewer(pmtiles_url, **kwargs))
    return dest


_VIEWER_JS = r"""
const CFG = window.UMBRA_TILES;
const protocol = new pmtiles.Protocol();
maplibregl.addProtocol("pmtiles", protocol.tile);

const map = new maplibregl.Map({
  container: "map",
  style: {
    version: 8,
    sources: {
      osm: {
        type: "raster",
        tiles: ["https://a.tile.openstreetmap.org/{z}/{x}/{y}.png"],
        tileSize: 256,
        attribution: "&copy; OpenStreetMap contributors",
      },
      umbra: { type: "vector", url: "pmtiles://" + CFG.pmtiles },
    },
    layers: [
      { id: "osm", type: "raster", source: "osm" },
      // Coverage shape, from the footprint polygons the archive carries at the
      // deeper zooms. An archive written without them simply has no features
      // here, so these two layers draw nothing and the circles stand alone.
      {
        id: "acq-fill",
        type: "fill",
        source: "umbra",
        "source-layer": CFG.footprintLayer,
        paint: { "fill-color": "#e6194b", "fill-opacity": 0.15 },
      },
      {
        id: "acq-outline",
        type: "line",
        source: "umbra",
        "source-layer": CFG.footprintLayer,
        paint: { "line-color": "#e6194b", "line-width": 1.2, "line-opacity": 0.9 },
      },
      {
        id: "acq",
        type: "circle",
        source: "umbra",
        "source-layer": CFG.layer,
        paint: {
          "circle-radius": ["interpolate", ["linear"], ["zoom"], 2, 2.5, 8, 5, 12, 7],
          "circle-color": "#e6194b",
          "circle-opacity": 0.75,
          "circle-stroke-width": 1,
          "circle-stroke-color": "#ffffff",
        },
      },
    ],
  },
  center: [0, 20],
  zoom: 1.4,
});

map.addControl(new maplibregl.NavigationControl(), "top-left");
map.addControl(new maplibregl.AttributionControl({ customAttribution: CFG.attribution }));

const CLICKABLE = ["acq", "acq-fill"];

// One handler over both layers: a centroid sits on top of its own footprint, so
// per-layer handlers would open two identical popups on the same click.
map.on("click", (e) => {
  const layers = CLICKABLE.filter((l) => map.getLayer(l));
  const f = map.queryRenderedFeatures(e.point, { layers })[0];
  if (!f) return;
  const p = f.properties || {};
  const div = document.createElement("div");
  div.className = "umbra-popup";
  const order = ["place", "product", "pol", "date", "platform", "id"];
  for (const key of order) {
    if (p[key] == null) continue;
    const row = document.createElement("div");
    const k = document.createElement("b");
    k.textContent = key + ": ";
    row.appendChild(k);
    row.appendChild(document.createTextNode(String(p[key])));
    div.appendChild(row);
  }
  if (p.stac_href) {
    const a = document.createElement("a");
    a.href = p.stac_href;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    a.textContent = "Open STAC item";
    div.appendChild(a);
  }
  new maplibregl.Popup().setLngLat(e.lngLat).setDOMContent(div).addTo(map);
});

// The centroid and its footprint carry the same properties, so hovering either
// promises the same popup -- and at high zoom the polygon is the easier target.
for (const id of CLICKABLE) {
  map.on("mouseenter", id, () => (map.getCanvas().style.cursor = "pointer"));
  map.on("mouseleave", id, () => (map.getCanvas().style.cursor = ""));
}
"""

_VIEWER_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{title}</title>
<link rel="stylesheet" href="{maplibre_css}"/>
<style>
  html, body {{ margin: 0; height: 100%; }}
  #map {{ position: absolute; inset: 0; }}
  .umbra-popup {{
    font: 13px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }}
  .umbra-popup a {{ display: inline-block; margin-top: 6px; }}
</style>
</head>
<body>
<div id="map"></div>
<script src="{maplibre_js}"></script>
<script src="{pmtiles_js}"></script>
<script>window.UMBRA_TILES = {config_json};</script>
<script>{viewer_js}</script>
</body>
</html>
"""
