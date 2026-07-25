"""Tests for the whole-catalog PMTiles tiling (``umbra tiles``).

The generator is stdlib-only, so these run in a core install with no network
and no viz/load extras. The discipline mirrors ``test_export`` / the STAC
document tests: we *decode our own output* -- parse the PMTiles v3 header and
directory, and decode a Mapbox Vector Tile back into points and footprint
polygons -- and assert the catalog survives the round trip. The JavaScript
viewer runs in a browser and isn't reachable from pytest, so we stop at "the
page ships the right wiring".
"""

from __future__ import annotations

import gzip
import json
import struct
from typing import NamedTuple

import pytest
from click.testing import CliRunner

from umbra_py import pmtiles
from umbra_py.cli import cli
from umbra_py.models import UmbraItem

_HREF = "https://x.s3.amazonaws.com/sar-data/tasks/{task}/t1/a1/item.stac.v2.json"


def _item(item_id: str, lon: float, lat: float, task: str = "Site A") -> UmbraItem:
    """A minimal item with a footprint centered on ``(lon, lat)``."""
    d = 0.02
    doc = {
        "type": "Feature",
        "stac_version": "1.0.0",
        "id": item_id,
        "bbox": [lon - d, lat - d, lon + d, lat + d],
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [
                    [lon - d, lat - d],
                    [lon + d, lat - d],
                    [lon + d, lat + d],
                    [lon - d, lat + d],
                    [lon - d, lat - d],
                ]
            ],
        },
        "properties": {
            "datetime": "2024-05-04T00:00:00Z",
            "platform": "Umbra-08",
            "sar:product_type": "GEC",
            "sar:polarizations": ["VV"],
        },
        "assets": {},
    }
    return UmbraItem.from_dict(doc, href=_HREF.format(task=task))


# --- primitive round trips -----------------------------------------------
def _read_uvarint(buf: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return result, pos
        shift += 7


def test_uvarint_round_trips():
    for value in (0, 1, 127, 128, 300, 16384, 1_000_000):
        encoded = pmtiles._uvarint(value)
        decoded, pos = _read_uvarint(encoded, 0)
        assert decoded == value
        assert pos == len(encoded)


def test_zigzag_matches_the_spec():
    assert pmtiles._zigzag(0) == 0
    assert pmtiles._zigzag(-1) == 1
    assert pmtiles._zigzag(1) == 2
    assert pmtiles._zigzag(-2) == 3
    assert pmtiles._zigzag(2) == 4


def test_tileid_is_unique_and_ordered_within_a_zoom():
    # Every (x, y) at a zoom maps to a distinct id, and lower zooms sort first.
    ids_z2 = {pmtiles.zxy_to_tileid(2, x, y) for x in range(4) for y in range(4)}
    assert len(ids_z2) == 16
    # z0 tile precedes every z1 tile precedes every z2 tile.
    assert pmtiles.zxy_to_tileid(0, 0, 0) == 0
    assert min(ids_z2) > max(pmtiles.zxy_to_tileid(1, x, y) for x in range(2) for y in range(2))


# --- PMTiles container decoding ------------------------------------------
def _decode_directory(buf: bytes) -> list[tuple[int, int, int, int]]:
    """Decode a serialized directory into (tile_id, offset, length, run) rows."""
    pos = 0
    n, pos = _read_uvarint(buf, pos)
    tile_ids = []
    last = 0
    for _ in range(n):
        delta, pos = _read_uvarint(buf, pos)
        last += delta
        tile_ids.append(last)
    runs = []
    for _ in range(n):
        run, pos = _read_uvarint(buf, pos)
        runs.append(run)
    lengths = []
    for _ in range(n):
        length, pos = _read_uvarint(buf, pos)
        lengths.append(length)
    offsets = []
    for i in range(n):
        raw, pos = _read_uvarint(buf, pos)
        if raw == 0 and i > 0:
            offsets.append(offsets[i - 1] + lengths[i - 1])
        else:
            offsets.append(raw - 1)
    return list(zip(tile_ids, offsets, lengths, runs, strict=True))


def _parse_header(data: bytes) -> dict:
    assert data[:7] == b"PMTiles"
    assert data[7] == 3
    (
        root_off,
        root_len,
        meta_off,
        meta_len,
        _leaf_off,
        _leaf_len,
        data_off,
        data_len,
    ) = struct.unpack_from("<QQQQQQQQ", data, 8)
    num_addressed, num_entries, num_contents = struct.unpack_from("<QQQ", data, 72)
    clustered, internal_comp, tile_comp, tile_type, min_z, max_z = struct.unpack_from(
        "<BBBBBB", data, 96
    )
    return {
        "root_off": root_off,
        "root_len": root_len,
        "meta_off": meta_off,
        "meta_len": meta_len,
        "data_off": data_off,
        "data_len": data_len,
        "num_addressed": num_addressed,
        "num_entries": num_entries,
        "num_contents": num_contents,
        "clustered": clustered,
        "internal_comp": internal_comp,
        "tile_comp": tile_comp,
        "tile_type": tile_type,
        "min_zoom": min_z,
        "max_zoom": max_z,
    }


class _DecodedFeature(NamedTuple):
    """One decoded MVT feature: geometry type, its parts, and its properties."""

    geom_type: int
    parts: list[list[tuple[int, int]]]
    props: dict


def _decode_mvt(tile: bytes) -> dict[str, list[_DecodedFeature]]:
    """Decode an MVT into ``{layer_name: [feature, ...]}``.

    Just enough of the wire format to prove the features survived: layer names,
    keys, values, geometry types and the geometry command stream (so a polygon's
    rings come back as coordinates, not an opaque blob).
    """
    layers: dict[str, list[_DecodedFeature]] = {}
    pos = 0
    while pos < len(tile):
        key, pos = _read_uvarint(tile, pos)
        field, wire = key >> 3, key & 0x7
        if field == 3 and wire == 2:  # Tile.layers
            length, pos = _read_uvarint(tile, pos)
            name, feats = _decode_layer(tile[pos : pos + length])
            pos += length
            assert name not in layers, "a layer name must appear once per tile"
            layers[name] = feats
        else:  # pragma: no cover
            raise AssertionError("unexpected top-level field")
    return layers


def _decode_layer(buf: bytes) -> tuple[str, list[_DecodedFeature]]:
    name = ""
    keys: list[str] = []
    values: list[object] = []
    raw: list[tuple[int, list[list[tuple[int, int]]], list[int]]] = []
    pos = 0
    while pos < len(buf):
        key, pos = _read_uvarint(buf, pos)
        field, wire = key >> 3, key & 0x7
        if field == 1 and wire == 2:  # name
            length, pos = _read_uvarint(buf, pos)
            name = buf[pos : pos + length].decode("utf-8")
            pos += length
        elif field == 3 and wire == 2:  # keys
            length, pos = _read_uvarint(buf, pos)
            keys.append(buf[pos : pos + length].decode("utf-8"))
            pos += length
        elif field == 4 and wire == 2:  # values
            length, pos = _read_uvarint(buf, pos)
            values.append(_decode_value(buf[pos : pos + length]))
            pos += length
        elif field == 2 and wire == 2:  # features
            length, pos = _read_uvarint(buf, pos)
            raw.append(_decode_feature(buf[pos : pos + length]))
            pos += length
        elif wire == 0:
            _v, pos = _read_uvarint(buf, pos)
        elif wire == 2:
            length, pos = _read_uvarint(buf, pos)
            pos += length
        else:  # pragma: no cover
            raise AssertionError("unexpected layer field wire type")

    out = []
    for gtype, parts, tags in raw:
        props = {keys[tags[i]]: values[tags[i + 1]] for i in range(0, len(tags), 2)}
        out.append(_DecodedFeature(gtype, parts, props))
    return name, out


def _decode_mvt_points(tile: bytes, layer: str = "acquisitions") -> list[dict]:
    """The property dicts of ``layer``'s features, which must all be points."""
    feats = _decode_mvt(tile).get(layer, [])
    for feat in feats:
        assert feat.geom_type == 1, "feature geometry must be a point"
        assert feat.parts and len(feat.parts[0]) == 1
    return [feat.props for feat in feats]


def _decode_value(buf: bytes) -> object:
    pos = 0
    key, pos = _read_uvarint(buf, pos)
    field, wire = key >> 3, key & 0x7
    if field == 1 and wire == 2:  # string_value
        length, pos = _read_uvarint(buf, pos)
        return buf[pos : pos + length].decode("utf-8")
    if field == 6 and wire == 0:  # sint_value
        z, pos = _read_uvarint(buf, pos)
        return (z >> 1) ^ -(z & 1)
    raise AssertionError("unexpected value field")  # pragma: no cover


def _decode_feature(buf: bytes) -> tuple[int, list[list[tuple[int, int]]], list[int]]:
    pos = 0
    tags: list[int] = []
    gtype = 0
    geometry: list[int] = []
    while pos < len(buf):
        key, pos = _read_uvarint(buf, pos)
        field, wire = key >> 3, key & 0x7
        if field == 2 and wire == 2:  # tags (packed)
            length, pos = _read_uvarint(buf, pos)
            end = pos + length
            while pos < end:
                t, pos = _read_uvarint(buf, pos)
                tags.append(t)
        elif field == 3 and wire == 0:  # type
            gtype, pos = _read_uvarint(buf, pos)
        elif field == 4 and wire == 2:  # geometry (packed commands)
            length, pos = _read_uvarint(buf, pos)
            end = pos + length
            while pos < end:
                g, pos = _read_uvarint(buf, pos)
                geometry.append(g)
        elif wire == 0:
            _v, pos = _read_uvarint(buf, pos)
        else:  # pragma: no cover
            raise AssertionError("unexpected feature field")
    return gtype, _decode_geometry(geometry), tags


def _decode_geometry(cmds: list[int]) -> list[list[tuple[int, int]]]:
    """Walk an MVT command stream back into parts (rings, for a polygon)."""

    def unzigzag(value: int) -> int:
        return (value >> 1) ^ -(value & 1)

    parts: list[list[tuple[int, int]]] = []
    cx = cy = 0
    pos = 0
    while pos < len(cmds):
        cmd, count = cmds[pos] & 0x7, cmds[pos] >> 3
        pos += 1
        if cmd == 1:  # MoveTo starts a new part
            for _ in range(count):
                cx += unzigzag(cmds[pos])
                cy += unzigzag(cmds[pos + 1])
                pos += 2
                parts.append([(cx, cy)])
        elif cmd == 2:  # LineTo extends the current part
            for _ in range(count):
                cx += unzigzag(cmds[pos])
                cy += unzigzag(cmds[pos + 1])
                pos += 2
                parts[-1].append((cx, cy))
        elif cmd == 7:  # ClosePath: the ring's first point is implied
            assert count == 1
        else:  # pragma: no cover
            raise AssertionError(f"unexpected geometry command {cmd}")
    return parts


def _tile_bytes(archive: bytes, header: dict, tile_id: int) -> bytes | None:
    directory = gzip.decompress(
        archive[header["root_off"] : header["root_off"] + header["root_len"]]
    )
    for tid, off, length, _run in _decode_directory(directory):
        if tid == tile_id:
            start = header["data_off"] + off
            return gzip.decompress(archive[start : start + length])
    return None


def test_build_pmtiles_header_is_well_formed():
    items = [_item("a", -122.4, 37.8), _item("b", 2.35, 48.85)]
    archive = pmtiles.build_pmtiles(items, min_zoom=0, max_zoom=3)
    header = _parse_header(archive)
    assert header["tile_type"] == 1  # MVT
    assert header["tile_comp"] == 2 and header["internal_comp"] == 2  # gzip
    assert header["min_zoom"] == 0 and header["max_zoom"] == 3
    assert header["clustered"] == 1
    # Four zooms (0..3), two well-separated points => the z0 tile holds both,
    # deeper zooms split them, so there are addressed tiles at every level.
    assert header["num_entries"] == header["num_addressed"] >= 4
    # The file is exactly header + dir + metadata + data with no gaps.
    assert header["data_off"] + header["data_len"] == len(archive)


def test_metadata_advertises_the_vector_layer():
    archive = pmtiles.build_pmtiles([_item("a", 0.0, 0.0)], max_zoom=2)
    header = _parse_header(archive)
    meta = gzip.decompress(archive[header["meta_off"] : header["meta_off"] + header["meta_len"]])
    doc = json.loads(meta)
    assert doc["vector_layers"][0]["id"] == "acquisitions"
    assert "CC BY 4.0" in doc["attribution"]
    assert set(doc["vector_layers"][0]["fields"]) >= {"id", "place", "product", "date"}


def test_features_and_properties_survive_the_round_trip():
    items = [
        _item("scene-1", -122.4, 37.8, task="San Francisco"),
        _item("scene-2", -122.41, 37.79, task="San Francisco"),
    ]
    archive = pmtiles.build_pmtiles(items, min_zoom=0, max_zoom=4)
    header = _parse_header(archive)
    # At z0 the whole world is one tile, so both points land in it.
    z0 = _tile_bytes(archive, header, pmtiles.zxy_to_tileid(0, 0, 0))
    assert z0 is not None
    props = _decode_mvt_points(z0)
    assert len(props) == 2
    ids = {p["id"] for p in props}
    assert ids == {"scene-1", "scene-2"}
    one = next(p for p in props if p["id"] == "scene-1")
    assert one["place"] == "San Francisco"
    assert one["product"] == "GEC"
    assert one["date"] == "2024-05-04"


def test_points_separate_into_different_tiles_at_high_zoom():
    # Two far-apart points must not share a tile once zoomed in.
    items = [_item("west", -122.4, 37.8), _item("east", 139.7, 35.7)]
    archive = pmtiles.build_pmtiles(items, min_zoom=0, max_zoom=6)
    header = _parse_header(archive)
    # Find each point's z6 tile and confirm they differ and each holds one point.
    fx_w, fy_w = pmtiles._lonlat_to_tile_fraction(-122.4, 37.8, 6)
    fx_e, fy_e = pmtiles._lonlat_to_tile_fraction(139.7, 35.7, 6)
    tid_w = pmtiles.zxy_to_tileid(6, int(fx_w), int(fy_w))
    tid_e = pmtiles.zxy_to_tileid(6, int(fx_e), int(fy_e))
    assert tid_w != tid_e
    assert len(_decode_mvt_points(_tile_bytes(archive, header, tid_w))) == 1
    assert len(_decode_mvt_points(_tile_bytes(archive, header, tid_e))) == 1


def test_identical_tiles_are_deduplicated():
    # One point, many zooms: each zoom's single-point tile has identical content
    # only when the within-tile pixel position matches; regardless, num_contents
    # never exceeds num_entries and the archive stays internally consistent.
    archive = pmtiles.build_pmtiles([_item("solo", 10.0, 10.0)], min_zoom=0, max_zoom=8)
    header = _parse_header(archive)
    assert header["num_contents"] <= header["num_entries"]
    assert header["num_entries"] == 9  # one tile per zoom 0..8


# --- footprint polygons ---------------------------------------------------
def _tile_to_lonlat(zoom: int, tx: int, ty: int, px: int, py: int) -> tuple[float, float]:
    """Inverse of ``_lonlat_to_tile_fraction`` for a point in a tile's space."""
    import math

    n = 1 << zoom
    fx = tx + px / 4096.0
    fy = ty + py / 4096.0
    lon = fx / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * fy / n))))
    return lon, lat


def _find_tile(archive: bytes, header: dict, zoom: int, lon: float, lat: float) -> bytes:
    fx, fy = pmtiles._lonlat_to_tile_fraction(lon, lat, zoom)
    tile = _tile_bytes(archive, header, pmtiles.zxy_to_tileid(zoom, int(fx), int(fy)))
    assert tile is not None
    return tile


def test_footprint_polygon_round_trips_with_its_properties():
    lon, lat = -122.4, 37.8
    archive = pmtiles.build_pmtiles([_item("scene-1", lon, lat, task="San Francisco")], max_zoom=7)
    header = _parse_header(archive)
    layers = _decode_mvt(_find_tile(archive, header, 7, lon, lat))

    assert set(layers) == {"acquisitions", pmtiles.FOOTPRINT_LAYER}
    (footprint,) = layers[pmtiles.FOOTPRINT_LAYER]
    assert footprint.geom_type == 3  # POLYGON
    assert footprint.props["id"] == "scene-1"
    assert footprint.props["place"] == "San Francisco"  # same lean metadata as the point
    # One ring, the four corners of the item's square footprint (ClosePath means
    # the closing duplicate is not stored).
    (ring,) = footprint.parts
    assert len(ring) == 4
    # MVT requires an exterior ring to wind clockwise in the tile's y-down space.
    assert pmtiles._signed_area(ring) > 0
    # The ring projects back onto the footprint it came from (d = 0.02 degrees).
    corners = [_tile_to_lonlat(7, *_tile_xy(7, lon, lat), px, py) for px, py in ring]
    assert min(c[0] for c in corners) == pytest.approx(lon - 0.02, abs=0.002)
    assert max(c[0] for c in corners) == pytest.approx(lon + 0.02, abs=0.002)
    assert min(c[1] for c in corners) == pytest.approx(lat - 0.02, abs=0.002)
    assert max(c[1] for c in corners) == pytest.approx(lat + 0.02, abs=0.002)


def _tile_xy(zoom: int, lon: float, lat: float) -> tuple[int, int]:
    fx, fy = pmtiles._lonlat_to_tile_fraction(lon, lat, zoom)
    return int(fx), int(fy)


def test_footprints_start_at_their_min_zoom():
    # The low-zoom tiles every visitor loads first stay centroid-only: a
    # footprint is sub-pixel there.
    lon, lat = 10.0, 10.0
    archive = pmtiles.build_pmtiles([_item("solo", lon, lat)], min_zoom=0, max_zoom=7)
    header = _parse_header(archive)
    assert pmtiles.FOOTPRINT_MIN_ZOOM == 6
    for zoom in range(0, 6):
        assert set(_decode_mvt(_find_tile(archive, header, zoom, lon, lat))) == {"acquisitions"}
    for zoom in (6, 7):
        assert pmtiles.FOOTPRINT_LAYER in _decode_mvt(_find_tile(archive, header, zoom, lon, lat))


def test_footprints_can_be_switched_off():
    lon, lat = 10.0, 10.0
    items = [_item("solo", lon, lat)]
    plain = pmtiles.build_pmtiles(items, max_zoom=7, footprints=False)
    header = _parse_header(plain)
    assert set(_decode_mvt(_find_tile(plain, header, 7, lon, lat))) == {"acquisitions"}
    # ... and the archive is smaller than the footprint-carrying default.
    assert len(plain) < len(pmtiles.build_pmtiles(items, max_zoom=7))
    # The metadata advertises only the layer that is actually there.
    meta = json.loads(
        gzip.decompress(plain[header["meta_off"] : header["meta_off"] + header["meta_len"]])
    )
    assert [layer["id"] for layer in meta["vector_layers"]] == ["acquisitions"]


def test_metadata_advertises_the_footprint_layer():
    archive = pmtiles.build_pmtiles([_item("a", 0.0, 0.0)], max_zoom=7)
    header = _parse_header(archive)
    meta = json.loads(
        gzip.decompress(archive[header["meta_off"] : header["meta_off"] + header["meta_len"]])
    )
    layers = {layer["id"]: layer for layer in meta["vector_layers"]}
    assert set(layers) == {"acquisitions", "footprints"}
    assert layers["footprints"]["minzoom"] == pmtiles.FOOTPRINT_MIN_ZOOM
    assert layers["footprints"]["maxzoom"] == 7
    assert set(layers["footprints"]["fields"]) == set(layers["acquisitions"]["fields"])


def test_a_footprint_spanning_a_tile_seam_is_clipped_into_both_tiles():
    # lon 0 is a tile boundary at every zoom, so a footprint straddling it must
    # appear -- clipped -- in the tiles on either side.
    d = 0.4
    doc = {
        "type": "Feature",
        "stac_version": "1.0.0",
        "id": "straddler",
        "bbox": [-d, 40.0 - d, d, 40.0 + d],
        "geometry": None,  # exercise the bbox-rectangle fallback too
        "properties": {"datetime": "2024-05-04T00:00:00Z", "sar:product_type": "GEC"},
        "assets": {},
    }
    item = UmbraItem.from_dict(doc, href=_HREF.format(task="Seam"))
    archive = pmtiles.build_pmtiles([item], max_zoom=7)
    header = _parse_header(archive)

    west = _decode_mvt(_find_tile(archive, header, 7, -d / 2, 40.0))
    east = _decode_mvt(_find_tile(archive, header, 7, d / 2, 40.0))
    for layers in (west, east):
        (footprint,) = layers[pmtiles.FOOTPRINT_LAYER]
        (ring,) = footprint.parts
        assert len(ring) >= 4
        assert pmtiles._signed_area(ring) > 0
        # Clipped to the tile plus the documented buffer, never the whole span.
        assert all(-64 <= x <= 4096 + 64 for x, _y in ring)
    # The seam splits the footprint, so each side keeps only part of its width.
    west_x = [x for x, _y in west[pmtiles.FOOTPRINT_LAYER][0].parts[0]]
    assert max(west_x) >= 4096  # runs up to (and just past) the eastern edge
    east_x = [x for x, _y in east[pmtiles.FOOTPRINT_LAYER][0].parts[0]]
    assert min(east_x) <= 0  # ... and continues from the western one


def test_tile_polygons_wind_clockwise_whichever_way_the_input_runs():
    square = [(10.0, 10.0), (10.1, 10.0), (10.1, 10.1), (10.0, 10.1), (10.0, 10.0)]
    for ring in (square, list(reversed(square))):
        (polygons,) = pmtiles._tile_polygons([ring], 7).values()
        (encoded,) = polygons
        assert pmtiles._signed_area(encoded) > 0


def test_clipping_drops_a_ring_that_misses_the_tile_entirely():
    # A tile inside a footprint's *bounding box* need not be inside the footprint
    # (a diagonal one clears the corner tiles), so the clip has to come back empty
    # and that tile carry no polygon at all.
    outside = [(5000.0, 5000.0), (6000.0, 5000.0), (6000.0, 6000.0)]
    assert pmtiles._clip_ring(outside, -64.0, 4096.0 + 64.0) == []
    # A ring squeezed below one integer coordinate leaves nothing drawable.
    assert len(pmtiles._quantize_ring([(0.1, 0.1), (0.2, 0.1), (0.2, 0.2), (0.1, 0.1)])) < 3


def test_a_footprint_straddling_the_antimeridian_keeps_only_its_centroid():
    # The only way an Umbra footprint spans half the globe is a bbox wrapping the
    # antimeridian, where the lon/lat ring is not the footprint -- tiling it
    # would paint a world-wide row.
    doc = {
        "type": "Feature",
        "stac_version": "1.0.0",
        "id": "wrapped",
        "bbox": [-179.9, 10.0, 179.9, 10.2],
        "geometry": None,
        "properties": {"datetime": "2024-05-04T00:00:00Z", "sar:product_type": "GEC"},
        "assets": {},
    }
    item = UmbraItem.from_dict(doc, href=_HREF.format(task="Wrapped"))
    archive = pmtiles.build_pmtiles([item], max_zoom=7)
    header = _parse_header(archive)
    layers = _decode_mvt(_find_tile(archive, header, 7, 0.0, 10.1))
    assert [f.props["id"] for f in layers["acquisitions"]] == ["wrapped"]
    assert pmtiles.FOOTPRINT_LAYER not in layers


def test_build_raises_without_a_footprint():
    doc = {
        "type": "Feature",
        "stac_version": "1.0.0",
        "id": "no-bbox",
        "geometry": None,
        "properties": {"datetime": "2024-05-04T00:00:00Z"},
        "assets": {},
    }
    item = UmbraItem.from_dict(doc, href=_HREF.format(task="x"))
    with pytest.raises(ValueError, match="no items with a footprint"):
        pmtiles.build_pmtiles([item])


# --- viewer wiring --------------------------------------------------------
def test_build_viewer_points_at_the_archive_and_layer():
    html = pmtiles.build_viewer("catalog.pmtiles", title="My catalog")
    assert "<title>My catalog</title>" in html
    assert "pmtiles://" in html
    assert "maplibre-gl" in html
    assert '"catalog.pmtiles"' in html or '"pmtiles":"catalog.pmtiles"' in html
    # Mandatory attribution is wired into the map's attribution control.
    assert "CC BY 4.0" in html
    # The circle layer reads the same source-layer the archive writes.
    assert '"acquisitions"' in html or "acquisitions" in html


def test_build_viewer_draws_the_footprint_layer():
    html = pmtiles.build_viewer("catalog.pmtiles")
    assert f'"footprintLayer":"{pmtiles.FOOTPRINT_LAYER}"' in html
    # A translucent fill plus an outline over the footprint polygons, and the
    # fill is a click target alongside the centroid circles.
    assert "acq-fill" in html and "acq-outline" in html
    assert '["acq", "acq-fill"]' in html


# --- CLI ------------------------------------------------------------------
def test_cli_tiles_writes_archive_and_viewer(tmp_path, monkeypatch):
    items = [_item("a", -122.4, 37.8), _item("b", 2.35, 48.85)]
    monkeypatch.setattr("umbra_py.cli._gather_items", lambda **kwargs: items)

    out = tmp_path / "catalog.pmtiles"
    viewer = tmp_path / "viewer.html"
    result = CliRunner().invoke(
        cli,
        ["tiles", "--local", "--out", str(out), "--viewer", str(viewer), "--max-zoom", "3"],
    )
    assert result.exit_code == 0, result.output
    assert out.exists() and out.read_bytes()[:7] == b"PMTiles"
    assert viewer.exists()
    assert "catalog.pmtiles" in viewer.read_text()
    assert "Wrote PMTiles archive of 2 acquisition(s)" in result.output


def test_cli_tiles_no_footprints_writes_a_centroids_only_archive(tmp_path, monkeypatch):
    items = [_item("a", -122.4, 37.8)]
    monkeypatch.setattr("umbra_py.cli._gather_items", lambda **kwargs: items)

    def run(*extra: str) -> bytes:
        out = tmp_path / f"c{len(extra)}.pmtiles"
        result = CliRunner().invoke(
            cli, ["tiles", "--local", "--out", str(out), "--max-zoom", "7", *extra]
        )
        assert result.exit_code == 0, result.output
        return out.read_bytes()

    default, plain = run(), run("--no-footprints")
    for archive, expected in ((default, {"acquisitions", "footprints"}), (plain, {"acquisitions"})):
        header = _parse_header(archive)
        assert set(_decode_mvt(_find_tile(archive, header, 7, -122.4, 37.8))) == expected


def test_cli_tiles_rejects_bad_extension(tmp_path, monkeypatch):
    monkeypatch.setattr("umbra_py.cli._gather_items", lambda **kwargs: [_item("a", 0.0, 0.0)])
    result = CliRunner().invoke(cli, ["tiles", "--local", "--out", str(tmp_path / "x.mbtiles")])
    assert result.exit_code != 0
    assert "must be a .pmtiles file" in result.output


def test_cli_tiles_requires_out_without_fetch(monkeypatch):
    monkeypatch.setattr("umbra_py.cli._gather_items", lambda **kwargs: [_item("a", 0.0, 0.0)])
    result = CliRunner().invoke(cli, ["tiles", "--local"])
    assert result.exit_code != 0
    assert "--out is required unless --fetch is given" in result.output


# --- fetch (the consume side of the published basemap) --------------------
def _published_pmtiles() -> bytes:
    """Stand in for the catalog.pmtiles the publish workflow uploads."""
    return pmtiles.build_pmtiles([_item("a", -122.4, 37.8), _item("b", 2.35, 48.85)], max_zoom=3)


def test_default_pmtiles_path_env_override(tmp_path, monkeypatch):
    target = tmp_path / "custom.pmtiles"
    monkeypatch.setenv("UMBRA_PMTILES", str(target))
    assert pmtiles.default_pmtiles_path() == target


def test_default_pmtiles_path_sits_beside_the_index(tmp_path, monkeypatch):
    monkeypatch.delenv("UMBRA_PMTILES", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert pmtiles.default_pmtiles_path() == tmp_path / "umbra-py" / "catalog.pmtiles"


def test_fetch_prebuilt_pmtiles_downloads_the_archive(tmp_path):
    import responses

    payload = _published_pmtiles()
    url = "https://example.com/catalog-index/catalog.pmtiles"
    dest = tmp_path / "fetched" / "catalog.pmtiles"

    @responses.activate
    def run():
        responses.add(
            responses.GET,
            url,
            body=payload,
            status=200,
            headers={"Content-Length": str(len(payload))},
        )
        return pmtiles.fetch_prebuilt_pmtiles(dest, url=url)

    path = run()
    assert path == dest
    assert dest.read_bytes() == payload
    assert dest.read_bytes()[:7] == b"PMTiles"


def test_cli_tiles_fetch_writes_archive_and_viewer(tmp_path):
    import responses

    payload = _published_pmtiles()
    url = "https://example.com/catalog.pmtiles"
    out = tmp_path / "catalog.pmtiles"
    viewer = tmp_path / "map.html"

    @responses.activate
    def run():
        responses.add(
            responses.GET,
            url,
            body=payload,
            status=200,
            headers={"Content-Length": str(len(payload))},
        )
        return CliRunner().invoke(
            cli,
            ["tiles", "--fetch", "--url", url, "--out", str(out), "--viewer", str(viewer)],
        )

    result = run()
    assert result.exit_code == 0, result.output
    assert out.read_bytes() == payload
    assert "Fetched prebuilt PMTiles basemap" in result.output
    # The viewer points at the fetched file by name, not the remote URL.
    assert viewer.exists()
    assert "catalog.pmtiles" in viewer.read_text()


def test_cli_tiles_url_without_fetch_is_rejected(monkeypatch):
    monkeypatch.setattr("umbra_py.cli._gather_items", lambda **kwargs: [_item("a", 0.0, 0.0)])
    result = CliRunner().invoke(
        cli, ["tiles", "--local", "--out", "catalog.pmtiles", "--url", "https://x/y.pmtiles"]
    )
    assert result.exit_code != 0
    assert "--url only applies with --fetch" in result.output


# --- the COG reference each feature carries (the on-click "Get SAR image") ---


def _imaged_item(item_id: str = "imaged", lon: float = 0.0, lat: float = 0.0) -> UmbraItem:
    """An item whose GEC asset is resolvable, as the real catalog's are.

    Umbra publishes every asset with an empty ``href`` and a v1-style asset
    *key*; the public product is a sibling of the item's own sidecar, which is
    what :func:`umbra_py.pmtiles._cog_reference` collapses to a basename.
    """
    item = _item(item_id, lon, lat)
    item.assets = {"a1_MM.tif": {"href": "", "type": "image/tiff; application=geotiff"}}
    return item


def test_features_prefer_the_baked_place_label_over_the_task_codename():
    """A `umbra tiles --local` over a baked index (CatalogIndex.bake_places)
    should show the real place name, like the demo and the parquet export do."""
    item = _item("scene-1", -122.4, 37.8, task="Golden Gate Site")
    assert pmtiles._item_properties(item)["place"] == "Golden Gate Site"

    item.place = "San Francisco, California"
    assert pmtiles._item_properties(item)["place"] == "San Francisco, California"


def test_features_reference_the_cog_as_a_sidecar_relative_basename():
    """The URL prefix is identical for every acquisition and already present as
    ``stac_href``, so tiling it again -- in every tile, at every zoom -- would be
    pure bloat. Only the filename is stored; the page rebuilds the URL."""
    item = _imaged_item()
    props = pmtiles._item_properties(item, "GEC")

    assert props["cog"] == "a1_GEC.tif"
    assert "/" not in props["cog"]
    # ...and it really is the URL the deterministic layer resolves.
    assert item.href.rsplit("/", 1)[0] + "/" + props["cog"] == item.asset_href("GEC")


def test_features_carry_placement_bounds_in_the_drivers_own_order():
    """``bounds`` is handed to the shared geotiff.js driver verbatim, so it must
    be its ``data-bounds`` string: south,west,north,east."""
    props = pmtiles._item_properties(_imaged_item(lon=10.0, lat=20.0), "GEC")

    south, west, north, east = (float(v) for v in props["bounds"].split(","))
    min_lon, min_lat, max_lon, max_lat = _imaged_item(lon=10.0, lat=20.0).bbox
    assert (south, west, north, east) == (min_lat, min_lon, max_lat, max_lon)


def test_an_absolute_asset_href_is_kept_whole():
    """A product that is *not* a sibling of the sidecar cannot be rebuilt from
    the basename, so the full URL rides along instead of a wrong one."""
    item = _imaged_item()
    item.assets = {"a1_MM.tif": {"href": "https://other.example.com/deep/path/a1_GEC.tif"}}

    assert pmtiles._item_properties(item, "GEC")["cog"] == (
        "https://other.example.com/deep/path/a1_GEC.tif"
    )


def test_a_product_nested_under_the_sidecar_stays_absolute():
    """Only a *direct* sibling collapses to a basename. A product one directory
    deeper would be rebuilt at the wrong URL, so it keeps the full href."""
    item = _imaged_item()
    nested = item.href.rsplit("/", 1)[0] + "/products/a1_GEC.tif"
    item.assets = {"a1_MM.tif": {"href": nested}}

    assert pmtiles._item_properties(item, "GEC")["cog"] == nested


def test_an_item_with_no_bbox_has_no_placement_bounds():
    """The overlay is stretched to the footprint bbox, so without one there is
    nowhere to put it."""
    item = _imaged_item()
    item.bbox = None

    assert pmtiles._cog_bounds(item) is None
    assert "cog" not in pmtiles._item_properties(item, "GEC")


def test_an_item_with_no_gec_asset_tiles_no_cog_at_all():
    """A viewer keys its button on these properties, so a scene whose image
    cannot be resolved must carry neither -- a half-populated feature would
    offer a button that 404s."""
    props = pmtiles._item_properties(_item("bare", 0.0, 0.0), "GEC")
    assert "cog" not in props and "bounds" not in props


def test_an_asset_that_stays_s3_only_tiles_no_cog():
    """``s3://`` points into the *private* processing bucket and is not
    anonymously fetchable from a browser; with no public sidecar href and no
    task id to derive one from, there is nothing to offer."""
    item = _imaged_item()
    item.href = None
    item.properties.pop("umbra:task_id", None)
    item.assets = {"a1_MM.tif": {"href": "s3://private-bucket/a1_GEC.tif"}}

    props = pmtiles._item_properties(item, "GEC")
    assert "cog" not in props and "bounds" not in props


def test_cog_asset_none_tiles_metadata_only():
    props = pmtiles._item_properties(_imaged_item(), None)
    assert "cog" not in props and "bounds" not in props
    assert props["id"] == "imaged"  # ...but the lean metadata is untouched


def test_the_cog_reference_survives_the_tile_round_trip():
    """End to end: the property has to come back out of the encoded archive, in
    both layers, so clicking a centroid or its footprint offers the same image."""
    lon, lat = 5.0, 5.0
    archive = pmtiles.build_pmtiles([_imaged_item("round", lon, lat)], max_zoom=7)
    header = _parse_header(archive)
    layers = _decode_mvt(_find_tile(archive, header, 7, lon, lat))

    for layer in ("acquisitions", "footprints"):
        props = layers[layer][0].props
        assert props["cog"] == "a1_GEC.tif"
        assert props["bounds"]


def test_metadata_advertises_the_cog_fields():
    archive = pmtiles.build_pmtiles([_imaged_item()], max_zoom=7)
    header = _parse_header(archive)
    meta = json.loads(
        gzip.decompress(archive[header["meta_off"] : header["meta_off"] + header["meta_len"]])
    )
    fields = meta["vector_layers"][0]["fields"]
    assert {"cog", "bounds"} <= set(fields)

    lean = pmtiles.build_pmtiles([_imaged_item()], max_zoom=7, cog_asset=None)
    lean_header = _parse_header(lean)
    off, length = lean_header["meta_off"], lean_header["meta_len"]
    lean_meta = json.loads(gzip.decompress(lean[off : off + length]))
    assert not {"cog", "bounds"} & set(lean_meta["vector_layers"][0]["fields"])


def test_cli_tiles_no_cog_writes_a_leaner_archive(monkeypatch, tmp_path):
    monkeypatch.setattr("umbra_py.cli._gather_items", lambda **kwargs: [_imaged_item()])

    default = tmp_path / "with.pmtiles"
    lean = tmp_path / "without.pmtiles"
    for out, extra in ((default, []), (lean, ["--no-cog"])):
        result = CliRunner().invoke(
            cli, ["tiles", "--local", "--out", str(out), "--max-zoom", "7", *extra]
        )
        assert result.exit_code == 0, result.output

    assert len(lean.read_bytes()) < len(default.read_bytes())
