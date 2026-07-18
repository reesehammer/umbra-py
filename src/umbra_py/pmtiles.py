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
  ``export GeoJSON → tile with tippecanoe`` — an external binary. Because the
  catalog geometry we tile is *points* (one centroid per acquisition), the whole
  encoder — the `Mapbox Vector Tile
  <https://github.com/mapbox/vector-tile-spec>`_ protobuf and the `PMTiles v3
  <https://github.com/protomaps/PMTiles/blob/main/spec/v3/spec.md>`_ container —
  fits in the standard library (``struct``, ``gzip``, varint arithmetic). So the
  generator runs in a core install and is fully offline-testable by decoding its
  own output, the same discipline :mod:`umbra_py.export` and the STAC document
  builders hold.

* **The viewer is an interactive whole-catalog explorer.** :func:`build_viewer`
  emits a self-contained MapLibre GL page that points at a ``.pmtiles`` URL via
  the pinned ``pmtiles`` protocol plugin; it renders the *whole* catalog as a
  scalable circle layer with the same OpenStreetMap basemap and mandatory CC-BY
  attribution the Leaflet demo uses. It reads the archive's own ``umbra:*``
  metadata (the distinct product types and the date range :func:`build_pmtiles`
  records) and, when present, shows a filter panel — free-text site/id search,
  product-type toggles, a date-range pair — that narrows the visible
  acquisitions client-side via MapLibre ``setFilter``. That gives ``umbra demo``'s
  filter-and-click experience at *whole-catalog* scale, which the demo's
  inline-JSON page cannot hold at once. The two are complementary: ``demo`` for a
  gathered slice with click-to-quicklook SAR and server-backed analysis;
  ``tiles`` for the fast, zoom-anywhere, filterable whole-archive view.

The catalog rows the tiles carry are points with a lean set of string
properties (id, place, product, date, platform) — enough to style and label a
marker; the full metadata still lives one STAC link away.
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
from typing import Any

from .constants import ATTRIBUTION, CATALOG_INDEX_PMTILES_URL
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


def _encode_mvt(features: list[tuple[int, int, dict[str, Any]]], layer_name: str) -> bytes:
    """Encode a single vector tile holding point ``features``.

    Each feature is ``(px, py, properties)`` with ``px``/``py`` already in the
    tile's ``[0, extent]`` integer coordinate space. Returns the uncompressed
    protobuf bytes of a one-layer ``Tile`` message.
    """
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
    for fid, (px, py, props) in enumerate(features):
        tags: list[int] = []
        for name, value in props.items():
            if value is None:
                continue
            tags.append(intern_key(name))
            tags.append(intern_value(value))
        # Point geometry: one MoveTo (command 1, count 1), then the zigzagged
        # delta from the cursor origin (0, 0).
        geometry = [(1 & 0x7) | (1 << 3), _zigzag(px), _zigzag(py)]

        body = bytearray()
        body += _uvarint((1 << 3) | 0) + _uvarint(fid)  # id
        if tags:
            packed_tags = b"".join(_uvarint(t) for t in tags)
            body += _uvarint((2 << 3) | 2) + _uvarint(len(packed_tags)) + packed_tags
        body += _uvarint((3 << 3) | 0) + _uvarint(1)  # type = POINT
        packed_geom = b"".join(_uvarint(g) for g in geometry)
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

    tile = bytearray()
    tile += _uvarint((3 << 3) | 2) + _uvarint(len(layer)) + bytes(layer)  # layers
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


def _item_properties(item: UmbraItem) -> dict[str, Any]:
    """The lean string properties each tiled point carries (id, place, ...)."""
    dt = item.datetime
    props: dict[str, Any] = {
        "id": item.id,
        # Prefer a baked reverse-geocoded label ("Reykjavík, Iceland") over the
        # Umbra task codename when the index has one (see
        # CatalogIndex.bake_places) -- the same preference every other read
        # surface holds (map / serve / export / to_llm_context / demo), so the
        # tiled whole-catalog view labels a point with a real place name too, not
        # a campaign codename, and the viewer's free-text search matches on it.
        "place": item.place or item.task,
        "product": item.product_type,
        "date": dt.date().isoformat() if dt else None,
        "platform": item.platform,
        "stac_href": item.href,
    }
    return {k: v for k, v in props.items() if v is not None}


def build_pmtiles(
    items: Iterable[UmbraItem],
    *,
    min_zoom: int = 0,
    max_zoom: int = 9,
    layer_name: str = "acquisitions",
    name: str = "Umbra open-data catalog",
    description: str | None = None,
) -> bytes:
    """Build a single-file PMTiles archive of the catalog's acquisition centroids.

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
        The vector-tile source-layer name. The viewer's style references it, so
        keep it in sync with :func:`build_viewer` (both default to
        ``"acquisitions"``).
    name, description:
        Metadata recorded in the archive (surfaced by PMTiles-aware tooling).

    Returns the ``.pmtiles`` file as bytes; use :func:`write_pmtiles` to save it.
    Raises ``ValueError`` if no item has a footprint (an empty pyramid).
    """
    if min_zoom < 0 or max_zoom < min_zoom or max_zoom > 26:
        raise ValueError("require 0 <= min_zoom <= max_zoom <= 26")

    points = [(pt, _item_properties(i)) for i in items if (pt := _item_point(i)) is not None]
    if not points:
        raise ValueError("no items with a footprint to tile")

    lons = [lon for (lon, _lat), _p in points]
    lats = [lat for (_lon, lat), _p in points]
    bounds = (min(lons), min(lats), max(lons), max(lats))
    center = ((bounds[0] + bounds[2]) / 2.0, (bounds[1] + bounds[3]) / 2.0)

    # Filter facets for the viewer's interactive controls. The viewer reads these
    # from the archive's metadata at runtime (build_viewer) so a static,
    # zoom-anywhere map becomes a filterable *explorer* -- search a site, toggle a
    # product type, bound the date range -- without the viewer having to hold the
    # whole item list (the point of tiling). They are derived from the same points
    # being tiled, so they can never drift from the data. Absent facets (an older
    # archive) simply leave the panel hidden, so the viewer is unchanged there.
    facet_products = sorted({p["product"] for (_pt, p) in points if p.get("product")})
    facet_dates = [p["date"] for (_pt, p) in points if p.get("date")]
    facet_date_min = min(facet_dates) if facet_dates else None
    facet_date_max = max(facet_dates) if facet_dates else None

    # Bucket every point into the tile that holds it at each zoom.
    tiles: dict[tuple[int, int, int], list[tuple[int, int, dict[str, Any]]]] = {}
    for (lon, lat), props in points:
        for zoom in range(min_zoom, max_zoom + 1):
            fx, fy = _lonlat_to_tile_fraction(lon, lat, zoom)
            n = 1 << zoom
            tx = min(int(fx), n - 1)
            ty = min(int(fy), n - 1)
            px = min(max(int(round((fx - tx) * _EXTENT)), 0), _EXTENT)
            py = min(max(int(round((fy - ty) * _EXTENT)), 0), _EXTENT)
            tiles.setdefault((zoom, tx, ty), []).append((px, py, props))

    # Encode each tile, compress, and deduplicate identical contents. Walk tiles
    # in Hilbert (tile_id) order so the data section stays clustered.
    ordered = sorted(tiles.items(), key=lambda kv: zxy_to_tileid(*kv[0]))
    data = bytearray()
    entries: list[tuple[int, int, int, int]] = []
    seen: dict[bytes, tuple[int, int]] = {}
    for (zoom, tx, ty), feats in ordered:
        blob = _gzip(_encode_mvt(feats, layer_name))
        digest = hashlib.sha256(blob).digest()
        if digest in seen:
            offset, length = seen[digest]
        else:
            offset, length = len(data), len(blob)
            data += blob
            seen[digest] = (offset, length)
        entries.append((zxy_to_tileid(zoom, tx, ty), offset, length, 1))

    directory = _gzip(_serialize_directory(entries))
    metadata = _gzip(
        json.dumps(
            {
                "name": name,
                "description": description or f"{name} — acquisition footprint centroids.",
                "attribution": ATTRIBUTION,
                "type": "overlay",
                # Namespaced facets the MapLibre viewer turns into filter controls
                # (see build_viewer). Kept out of vector_layers.fields, which is a
                # field *type* map, not a value list.
                "umbra:products": facet_products,
                "umbra:date_min": facet_date_min,
                "umbra:date_max": facet_date_max,
                "vector_layers": [
                    {
                        "id": layer_name,
                        "description": "One point per Umbra open-data acquisition.",
                        "minzoom": min_zoom,
                        "maxzoom": max_zoom,
                        "fields": {
                            "id": "String",
                            "place": "String",
                            "product": "String",
                            "date": "String",
                            "platform": "String",
                            "stac_href": "String",
                        },
                    }
                ],
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
    acquisition as a circle over an OpenStreetMap basemap, with a click popup and
    the mandatory CC-BY attribution. ``layer_name`` must match the archive's
    source-layer (:func:`build_pmtiles`' default is ``"acquisitions"``).

    The page is an **interactive explorer**, not just a static map: it reads the
    archive's own metadata (:func:`build_pmtiles` records the distinct product
    types and the date range under ``umbra:*`` keys) and, when they are present,
    shows a filter panel — a free-text site/id search, product-type toggles, and
    a date-range pair — that narrows the visible acquisitions client-side via
    MapLibre ``setFilter`` without touching the tiles. This scales the
    ``umbra demo`` explorer's filter-and-click experience to the *whole*
    catalog (which the demo's inline-JSON page cannot hold at once), on the
    surface that is already tiled. An archive without those facets (one built
    before they were recorded) simply leaves the panel hidden, so an older
    ``.pmtiles`` renders exactly as before.
    """
    from html import escape

    config = json.dumps(
        {
            "pmtiles": pmtiles_url,
            "layer": layer_name,
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
// A PMTiles instance registered with the protocol so the same archive backs
// both the vector source (via the pmtiles:// URL) and the metadata read below
// that builds the filter controls.
const archive = new pmtiles.PMTiles(CFG.pmtiles);
protocol.add(archive);

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

// --- interactive filter panel -------------------------------------------
// The archive records the distinct product types and the date range under
// umbra:* metadata keys (build_pmtiles); when they are present, build a small
// filter panel that narrows the visible circles client-side with setFilter, so
// the whole-catalog view becomes an explorer. All filtering happens over the
// tiles already fetched -- no re-query, no held item list. An archive without
// the facets (built before they were recorded) leaves the panel hidden.
const fstate = { text: "", products: null, start: "", end: "" };

function buildFilterExpr() {
  const clauses = ["all"];
  if (fstate.text) {
    // Case-insensitive substring match on place OR id. `in` tests substring
    // when its second argument is a string; `coalesce`+`downcase` guard a
    // missing/absent property.
    clauses.push([
      "any",
      ["in", fstate.text, ["downcase", ["coalesce", ["get", "place"], ""]]],
      ["in", fstate.text, ["downcase", ["coalesce", ["get", "id"], ""]]],
    ]);
  }
  if (fstate.products && fstate.products.length) {
    clauses.push(["in", ["coalesce", ["get", "product"], ""], ["literal", fstate.products]]);
  }
  // Date bounds are lexical on the YYYY-MM-DD string. Keep points with no date
  // (mirroring the demo explorer) rather than dropping them under a bound.
  if (fstate.start) {
    clauses.push(["any", ["!", ["has", "date"]], [">=", ["get", "date"], fstate.start]]);
  }
  if (fstate.end) {
    clauses.push(["any", ["!", ["has", "date"]], ["<=", ["get", "date"], fstate.end]]);
  }
  return clauses.length > 1 ? clauses : null;
}

function applyFilter() {
  map.setFilter("acq", buildFilterExpr());
}

function buildFilterPanel(products, dateMin, dateMax) {
  const panel = document.getElementById("filter");
  if (!panel) return;

  const search = document.createElement("input");
  search.type = "text";
  search.placeholder = "Search site / id";
  search.className = "umbra-f-input";
  search.addEventListener("input", function () {
    fstate.text = search.value.trim().toLowerCase();
    applyFilter();
  });
  const searchWrap = document.createElement("div");
  searchWrap.className = "umbra-f-row";
  searchWrap.appendChild(search);
  panel.appendChild(searchWrap);

  if (products && products.length) {
    fstate.products = products.slice();
    const chips = document.createElement("div");
    chips.className = "umbra-f-chips";
    products.forEach(function (prod) {
      const chip = document.createElement("span");
      chip.className = "umbra-f-chip active";
      chip.textContent = prod;
      chip.addEventListener("click", function () {
        const on = chip.classList.toggle("active");
        if (on) {
          if (fstate.products.indexOf(prod) === -1) fstate.products.push(prod);
        } else {
          fstate.products = fstate.products.filter(function (p) { return p !== prod; });
        }
        applyFilter();
      });
      chips.appendChild(chip);
    });
    panel.appendChild(chips);
  }

  if (dateMin && dateMax) {
    const dates = document.createElement("div");
    dates.className = "umbra-f-row umbra-f-dates";
    const from = document.createElement("input");
    from.type = "date";
    from.min = dateMin; from.max = dateMax;
    const to = document.createElement("input");
    to.type = "date";
    to.min = dateMin; to.max = dateMax;
    from.addEventListener("change", function () { fstate.start = from.value; applyFilter(); });
    to.addEventListener("change", function () { fstate.end = to.value; applyFilter(); });
    dates.appendChild(from);
    dates.appendChild(to);
    panel.appendChild(dates);
  }

  panel.style.display = "";
}

archive.getMetadata().then(function (meta) {
  meta = meta || {};
  const products = meta["umbra:products"] || [];
  const dateMin = meta["umbra:date_min"] || null;
  const dateMax = meta["umbra:date_max"] || null;
  if (products.length || (dateMin && dateMax)) {
    buildFilterPanel(products, dateMin, dateMax);
  }
}).catch(function () { /* no metadata -> no filter panel, map still works */ });

map.on("click", "acq", (e) => {
  const f = e.features && e.features[0];
  if (!f) return;
  const p = f.properties || {};
  const div = document.createElement("div");
  div.className = "umbra-popup";
  const order = ["place", "product", "date", "platform", "id"];
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

map.on("mouseenter", "acq", () => (map.getCanvas().style.cursor = "pointer"));
map.on("mouseleave", "acq", () => (map.getCanvas().style.cursor = ""));
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
  #filter {{
    position: absolute; top: 10px; right: 10px; z-index: 1; display: none;
    width: 220px; max-width: calc(100vw - 20px); padding: 12px;
    background: rgba(255, 255, 255, 0.94); border: 1px solid #ccc; border-radius: 6px;
    box-shadow: 0 1px 4px rgba(0, 0, 0, 0.2);
    font: 13px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }}
  #filter .umbra-f-row {{ margin-bottom: 8px; }}
  #filter .umbra-f-input, #filter .umbra-f-dates input {{
    width: 100%; padding: 5px 7px; border: 1px solid #bbb; border-radius: 4px; font: inherit;
  }}
  #filter .umbra-f-dates {{ display: flex; gap: 6px; }}
  #filter .umbra-f-dates input {{ flex: 1; min-width: 0; }}
  #filter .umbra-f-chips {{ display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 8px; }}
  #filter .umbra-f-chip {{
    cursor: pointer; user-select: none; padding: 2px 9px; border-radius: 11px;
    border: 1px solid #bbb; background: #fff; font-size: 12px;
  }}
  #filter .umbra-f-chip.active {{ background: #2b6cb0; border-color: #2b6cb0; color: #fff; }}
</style>
</head>
<body>
<div id="map"></div>
<div id="filter"></div>
<script src="{maplibre_js}"></script>
<script src="{pmtiles_js}"></script>
<script>window.UMBRA_TILES = {config_json};</script>
<script>{viewer_js}</script>
</body>
</html>
"""
