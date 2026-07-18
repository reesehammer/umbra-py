"""Tests for the whole-catalog PMTiles tiling (``umbra tiles``).

The generator is stdlib-only, so these run in a core install with no network
and no viz/load extras. The discipline mirrors ``test_export`` / the STAC
document tests: we *decode our own output* -- parse the PMTiles v3 header and
directory, and decode a Mapbox Vector Tile back into points -- and assert the
catalog survives the round trip. The JavaScript viewer runs in a browser and
isn't reachable from pytest, so we stop at "the page ships the right wiring".
"""

from __future__ import annotations

import gzip
import struct

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


def _decode_mvt_points(tile: bytes) -> list[dict]:
    """Decode a one-layer point MVT into a list of property dicts.

    Just enough of the wire format to prove the features and their string
    properties survived: layer name, keys, string values, and one point per
    feature.
    """
    pos = 0
    layer_bytes = None
    while pos < len(tile):
        key, pos = _read_uvarint(tile, pos)
        field, wire = key >> 3, key & 0x7
        if field == 3 and wire == 2:  # Tile.layers
            length, pos = _read_uvarint(tile, pos)
            layer_bytes = tile[pos : pos + length]
            pos += length
        else:  # pragma: no cover - single-layer tiles only
            raise AssertionError("unexpected top-level field")
    assert layer_bytes is not None

    keys: list[str] = []
    values: list[object] = []
    feature_tags: list[list[int]] = []
    pos = 0
    while pos < len(layer_bytes):
        key, pos = _read_uvarint(layer_bytes, pos)
        field, wire = key >> 3, key & 0x7
        if field == 3 and wire == 2:  # keys
            length, pos = _read_uvarint(layer_bytes, pos)
            keys.append(layer_bytes[pos : pos + length].decode("utf-8"))
            pos += length
        elif field == 4 and wire == 2:  # values
            length, pos = _read_uvarint(layer_bytes, pos)
            values.append(_decode_value(layer_bytes[pos : pos + length]))
            pos += length
        elif field == 2 and wire == 2:  # features
            length, pos = _read_uvarint(layer_bytes, pos)
            feature_tags.append(_decode_feature_tags(layer_bytes[pos : pos + length]))
            pos += length
        elif wire == 0:
            _v, pos = _read_uvarint(layer_bytes, pos)
        elif wire == 2:
            length, pos = _read_uvarint(layer_bytes, pos)
            pos += length
        else:  # pragma: no cover
            raise AssertionError("unexpected layer field wire type")

    out = []
    for tags in feature_tags:
        props = {}
        for i in range(0, len(tags), 2):
            props[keys[tags[i]]] = values[tags[i + 1]]
        out.append(props)
    return out


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


def _decode_feature_tags(buf: bytes) -> list[int]:
    pos = 0
    tags: list[int] = []
    saw_point = False
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
            saw_point = gtype == 1
        elif field == 4 and wire == 2:  # geometry
            length, pos = _read_uvarint(buf, pos)
            pos += length
        elif wire == 0:
            _v, pos = _read_uvarint(buf, pos)
        else:  # pragma: no cover
            raise AssertionError("unexpected feature field")
    assert saw_point, "feature geometry must be a point"
    return tags


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
    import json

    doc = json.loads(meta)
    assert doc["vector_layers"][0]["id"] == "acquisitions"
    assert "CC BY 4.0" in doc["attribution"]
    assert set(doc["vector_layers"][0]["fields"]) >= {"id", "place", "product", "date"}


def _metadata_doc(archive: bytes) -> dict:
    import json

    header = _parse_header(archive)
    meta = gzip.decompress(archive[header["meta_off"] : header["meta_off"] + header["meta_len"]])
    return json.loads(meta)


def test_metadata_records_filter_facets():
    # The viewer builds its filter controls from these, so they must reflect the
    # tiled items exactly (distinct products, and the [min, max] date span).
    items = [
        _item("a", 0.0, 0.0, task="Site A"),
        _item("b", 1.0, 1.0, task="Site B"),
    ]
    # Give one item a different product and a later date so the facets are real.
    items[1].properties["sar:product_type"] = "SICD"
    items[1].properties["datetime"] = "2025-01-15T00:00:00Z"

    doc = _metadata_doc(pmtiles.build_pmtiles(items, max_zoom=2))
    assert doc["umbra:products"] == ["GEC", "SICD"]  # sorted, distinct
    assert doc["umbra:date_min"] == "2024-05-04"
    assert doc["umbra:date_max"] == "2025-01-15"


def test_tiled_point_prefers_the_baked_place_label():
    # Every other read surface prefers the baked reverse-geocoded label over the
    # task codename (CatalogIndex.bake_places); the tiled catalog must match, so
    # the whole-catalog view labels a point "Reykjavík, Iceland", not "Site A".
    item = _item("scene-1", -21.9, 64.1, task="Iceland_Nov-2025")
    item.place = "Reykjavík, Iceland"
    archive = pmtiles.build_pmtiles([item], min_zoom=0, max_zoom=2)
    header = _parse_header(archive)
    props = _decode_mvt_points(_tile_bytes(archive, header, pmtiles.zxy_to_tileid(0, 0, 0)))
    assert props[0]["place"] == "Reykjavík, Iceland"
    # And it flows into the facet-free metadata path without disturbing products.
    assert _metadata_doc(archive)["umbra:products"] == ["GEC"]


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
    import pytest

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


def test_build_viewer_ships_the_interactive_filter_panel():
    html = pmtiles.build_viewer("catalog.pmtiles")
    # The filter panel container and its controls are in the page.
    assert 'id="filter"' in html
    assert "umbra-f-chip" in html
    assert "Search site / id" in html
    # It reads the archive's facets at runtime and narrows the layer via
    # setFilter -- the wiring that makes it an explorer, not a static map.
    assert "getMetadata" in html
    assert 'setFilter("acq"' in html
    assert "umbra:products" in html
    assert "umbra:date_min" in html


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
