"""Offline tests for polygon ``intersects`` search.

Covers the stdlib geometry core (:mod:`umbra_py._geometry`), the item-level
:meth:`UmbraItem.intersects_polygon`, and the ``intersects`` filter threaded
through every search surface: the live :class:`UmbraCatalog` walk, the SQLite
:class:`CatalogIndex`, the ``umbra search --intersects`` CLI, the STAC API
``/search`` endpoint, and the ``search_catalog`` MCP tool. The serve/MCP
sections importorskip their extras, so the core CI job still runs the rest.
"""

from __future__ import annotations

import json

import pytest

from umbra_py._geometry import (
    bbox_ring,
    geometries_intersect,
    geometry_bbox,
    parse_geometry,
    rings_from_geojson,
    to_geojson,
)
from umbra_py.catalog import UmbraCatalog
from umbra_py.index import CatalogIndex
from umbra_py.models import UmbraItem

_BUCKET = "https://s3.us-west-2.amazonaws.com/umbra-open-data-catalog"


def _poly(*coords) -> dict:
    """A GeoJSON Polygon from ``(lon, lat)`` pairs (auto-closed)."""
    ring = [list(c) for c in coords]
    if ring[0] != ring[-1]:
        ring.append(ring[0])
    return {"type": "Polygon", "coordinates": [ring]}


# A 0..2 square, and reference geometries around it.
UNIT_SQUARE = parse_geometry(_poly((0, 0), (2, 0), (2, 2), (0, 2)))


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def test_parse_polygon_and_multipolygon():
    poly = parse_geometry(_poly((0, 0), (1, 0), (1, 1), (0, 1)))
    assert len(poly) == 1
    multi = parse_geometry(
        {
            "type": "MultiPolygon",
            "coordinates": [
                [[[0, 0], [1, 0], [1, 1], [0, 0]]],
                [[[5, 5], [6, 5], [6, 6], [5, 5]]],
            ],
        }
    )
    assert len(multi) == 2


def test_parse_feature_and_collection_unwrap():
    feat = parse_geometry(
        {"type": "Feature", "geometry": _poly((0, 0), (1, 0), (1, 1), (0, 1)), "properties": {}}
    )
    assert len(feat) == 1
    fc = parse_geometry(
        {
            "type": "FeatureCollection",
            "features": [
                {"type": "Feature", "geometry": _poly((0, 0), (1, 0), (1, 1), (0, 1))},
                {"type": "Feature", "geometry": _poly((5, 5), (6, 5), (6, 6), (5, 6))},
            ],
        }
    )
    assert len(fc) == 2


def test_parse_from_json_string():
    poly = parse_geometry(json.dumps(_poly((0, 0), (1, 0), (1, 1), (0, 1))))
    assert len(poly) == 1


def test_parse_drops_holes():
    with_hole = {
        "type": "Polygon",
        "coordinates": [
            [[0, 0], [10, 0], [10, 10], [0, 10], [0, 0]],
            [[4, 4], [6, 4], [6, 6], [4, 6], [4, 4]],
        ],
    }
    rings = parse_geometry(with_hole)
    assert len(rings) == 1  # only the exterior survives


@pytest.mark.parametrize(
    "bad",
    [
        {"type": "Point", "coordinates": [0, 0]},
        {"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
        {"type": "Polygon", "coordinates": [[[0, 0], [1, 1]]]},  # <3 positions
        {"type": "Polygon", "coordinates": []},
        "not json at all {",
        {"type": "FeatureCollection", "features": []},
        123,
    ],
)
def test_parse_rejects_non_polygon(bad):
    with pytest.raises(ValueError):
        parse_geometry(bad)


def test_rings_from_geojson_forgiving():
    assert rings_from_geojson({"type": "Point", "coordinates": [0, 0]}) is None
    assert rings_from_geojson(None) is None
    assert rings_from_geojson(_poly((0, 0), (1, 0), (1, 1), (0, 1))) is not None


def test_to_geojson_roundtrip():
    assert to_geojson(UNIT_SQUARE)["type"] == "Polygon"
    multi = parse_geometry(
        {
            "type": "MultiPolygon",
            "coordinates": [[[[0, 0], [1, 0], [1, 1], [0, 0]]], [[[5, 5], [6, 5], [6, 6], [5, 5]]]],
        }
    )
    assert to_geojson(multi)["type"] == "MultiPolygon"
    assert to_geojson([]) is None


def test_geometry_bbox():
    assert geometry_bbox(UNIT_SQUARE) == (0.0, 0.0, 2.0, 2.0)
    assert geometry_bbox([]) is None


def test_bbox_ring_is_closed():
    ring = bbox_ring((0, 0, 1, 1))
    assert ring[0] == ring[-1]
    assert len(ring) == 5


# --------------------------------------------------------------------------- #
# Intersection geometry
# --------------------------------------------------------------------------- #


def test_intersect_contained():
    inner = parse_geometry(_poly((0.5, 0.5), (1, 0.5), (1, 1), (0.5, 1)))
    assert geometries_intersect(UNIT_SQUARE, inner)
    # containment is symmetric
    assert geometries_intersect(inner, UNIT_SQUARE)


def test_intersect_overlapping_edges():
    overlap = parse_geometry(_poly((1, 1), (3, 1), (3, 3), (1, 3)))
    assert geometries_intersect(UNIT_SQUARE, overlap)


def test_disjoint_does_not_intersect():
    far = parse_geometry(_poly((5, 5), (6, 5), (6, 6), (5, 6)))
    assert not geometries_intersect(UNIT_SQUARE, far)


def test_bbox_overlap_without_polygon_overlap():
    # Two triangles whose bounding boxes overlap but whose bodies do not: a
    # coarse bbox filter would wrongly match, the polygon test must not.
    tri_a = parse_geometry(_poly((0, 0), (4, 0), (0, 4)))
    tri_b = parse_geometry(_poly((4, 4), (1, 4), (4, 1)))
    # Their bboxes clearly overlap...
    assert geometry_bbox(tri_a) == (0.0, 0.0, 4.0, 4.0)
    assert geometry_bbox(tri_b) == (1.0, 1.0, 4.0, 4.0)
    # ...but the triangles are on opposite sides of the diagonal.
    assert not geometries_intersect(tri_a, tri_b)


def test_touching_edge_counts_as_intersecting():
    # Shares the x=2 edge segment; a boundary touch is inclusive (never-drop).
    right = parse_geometry(_poly((2, 0), (4, 0), (4, 2), (2, 2)))
    assert geometries_intersect(UNIT_SQUARE, right)


# --------------------------------------------------------------------------- #
# UmbraItem.intersects_polygon
# --------------------------------------------------------------------------- #


def _item(item_id, geometry=None, bbox=None) -> UmbraItem:
    return UmbraItem.from_dict(
        {
            "id": item_id,
            "geometry": geometry,
            "bbox": list(bbox) if bbox else None,
            "properties": {},
            "assets": {},
        }
    )


def test_item_uses_true_footprint():
    # A diagonal-triangle footprint inside a bbox that would over-match.
    tri = _poly((0, 0), (4, 0), (0, 4))
    item = _item("t", geometry=tri)
    # A query square in the far triangle corner: inside the bbox, outside the body.
    corner = parse_geometry(_poly((3, 3), (4, 3), (4, 4), (3, 4)))
    assert not item.intersects_polygon(corner)
    near = parse_geometry(_poly((0, 0), (1, 0), (1, 1), (0, 1)))
    assert item.intersects_polygon(near)


def test_item_falls_back_to_bbox_when_no_geometry():
    item = _item("b", geometry=None, bbox=(0, 0, 1, 1))
    assert item.intersects_polygon(UNIT_SQUARE)
    far = parse_geometry(_poly((5, 5), (6, 5), (6, 6), (5, 6)))
    assert not item.intersects_polygon(far)


def test_item_without_geometry_or_bbox_matches_nothing():
    item = _item("n", geometry=None, bbox=None)
    assert not item.intersects_polygon(UNIT_SQUARE)


# --------------------------------------------------------------------------- #
# Catalog live walk
# --------------------------------------------------------------------------- #


def _sidecar(item_id, dt, bbox):
    return {
        "id": item_id,
        "bbox": list(bbox),
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [
                    [bbox[0], bbox[1]],
                    [bbox[2], bbox[1]],
                    [bbox[2], bbox[3]],
                    [bbox[0], bbox[3]],
                    [bbox[0], bbox[1]],
                ]
            ],
        },
        "properties": {"datetime": dt, "sar:product_type": "GEC"},
        "assets": {"GEC": {"href": "s3://umbra-internal/private/foo_GEC.tif"}},
    }


@pytest.fixture
def fake_bucket(monkeypatch):
    top = ["sar-data/tasks/A/"]
    keys = {
        "sar-data/tasks/A/": [
            "sar-data/tasks/A/u/2024-01-15-10-00-00_UMBRA-04/2024-01-15-10-00-00_UMBRA-04.stac.v2.json",
            "sar-data/tasks/A/u/2024-01-15-10-00-00_UMBRA-04/2024-01-15-10-00-00_UMBRA-04_GEC.tif",
            "sar-data/tasks/A/u/2024-02-10-12-00-00_UMBRA-09/2024-02-10-12-00-00_UMBRA-09.stac.v2.json",
            "sar-data/tasks/A/u/2024-02-10-12-00-00_UMBRA-09/2024-02-10-12-00-00_UMBRA-09_GEC.tif",
        ],
    }
    sidecars = {
        "2024-01-15-10-00-00_UMBRA-04": _sidecar("a", "2024-01-15T10:00:00Z", (0, 0, 1, 1)),
        "2024-02-10-12-00-00_UMBRA-09": _sidecar("b", "2024-02-10T12:00:00Z", (10, 10, 11, 11)),
    }
    monkeypatch.setattr(
        UmbraCatalog,
        "_list_prefix",
        lambda self, p: (top, []) if p == "sar-data/tasks/" else (_ for _ in ()).throw(KeyError(p)),
    )
    monkeypatch.setattr(UmbraCatalog, "_stream_keys", lambda self, p: iter(keys[p]))

    def fake_get(self, url):
        for stem, doc in sidecars.items():
            if url.endswith(f"{stem}.stac.v2.json"):
                return doc
        raise KeyError(url)

    monkeypatch.setattr(UmbraCatalog, "_get", fake_get)
    return UmbraCatalog()


def test_catalog_intersects_filters(fake_bucket):
    geom = parse_geometry(_poly((0.2, 0.2), (0.8, 0.2), (0.8, 0.8), (0.2, 0.8)))
    items = list(fake_bucket.search(start="2024-01-01", end="2024-12-31", intersects=geom))
    assert [i.id for i in items] == ["a"]


def test_catalog_intersects_none_returns_all(fake_bucket):
    items = list(fake_bucket.search(start="2024-01-01", end="2024-12-31"))
    assert sorted(i.id for i in items) == ["a", "b"]


# --------------------------------------------------------------------------- #
# Index
# --------------------------------------------------------------------------- #


def _idx_item(task, acq, item_id, dt, bbox):
    base = f"{_BUCKET}/sar-data/tasks/{task}/{acq}/{acq}"
    doc = {
        "id": item_id,
        "properties": {"datetime": dt, "sar:product_type": "GEC"},
        "bbox": list(bbox),
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [
                    [bbox[0], bbox[1]],
                    [bbox[2], bbox[1]],
                    [bbox[2], bbox[3]],
                    [bbox[0], bbox[3]],
                    [bbox[0], bbox[1]],
                ]
            ],
        },
        "assets": {
            f"{acq}_GEC.tif": {"href": f"{base}_GEC.tif", "type": "image/tiff; application=geotiff"}
        },
    }
    return UmbraItem.from_dict(doc, href=f"{base}.stac.v2.json")


@pytest.fixture
def built_index(tmp_path):
    db = tmp_path / "catalog.db"
    with CatalogIndex(db) as idx:
        idx.add(
            _idx_item(
                "A", "2024-01-15-10-00-00_UMBRA-04", "a", "2024-01-15T10:00:00Z", (0, 0, 1, 1)
            )
        )
        idx.add(
            _idx_item(
                "B", "2024-02-10-12-00-00_UMBRA-09", "b", "2024-02-10T12:00:00Z", (10, 10, 11, 11)
            )
        )
    return db


def test_index_intersects_filters(built_index):
    geom = parse_geometry(_poly((0.2, 0.2), (0.8, 0.2), (0.8, 0.8), (0.2, 0.8)))
    with CatalogIndex(built_index) as idx:
        items = list(idx.search(intersects=geom))
    assert [i.id for i in items] == ["a"]


def test_index_and_catalog_intersects_agree(built_index, fake_bucket):
    geom = parse_geometry(_poly((0.2, 0.2), (0.8, 0.2), (0.8, 0.8), (0.2, 0.8)))
    with CatalogIndex(built_index) as idx:
        idx_ids = [i.id for i in idx.search(intersects=geom)]
    cat_ids = [
        i.id for i in fake_bucket.search(start="2024-01-01", end="2024-12-31", intersects=geom)
    ]
    assert idx_ids == cat_ids == ["a"]


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def test_cli_search_intersects(built_index):
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    runner = CliRunner()
    geom = json.dumps(_poly((0.2, 0.2), (0.8, 0.2), (0.8, 0.8), (0.2, 0.8)))
    result = runner.invoke(
        cli_mod.cli, ["search", "--local", "--db", str(built_index), "--intersects", geom]
    )
    assert result.exit_code == 0, result.output
    assert "1 item(s)." in result.output


def test_cli_search_intersects_from_file(tmp_path, built_index):
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    aoi = tmp_path / "aoi.geojson"
    aoi.write_text(json.dumps(_poly((0.2, 0.2), (0.8, 0.2), (0.8, 0.8), (0.2, 0.8))))
    runner = CliRunner()
    result = runner.invoke(
        cli_mod.cli, ["search", "--local", "--db", str(built_index), "--intersects", str(aoi)]
    )
    assert result.exit_code == 0, result.output
    assert "1 item(s)." in result.output


def test_cli_search_intersects_conflicts_with_bbox(built_index):
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    runner = CliRunner()
    result = runner.invoke(
        cli_mod.cli,
        [
            "search",
            "--local",
            "--db",
            str(built_index),
            "--intersects",
            json.dumps(_poly((0, 0), (1, 0), (1, 1), (0, 1))),
            "--bbox",
            "0,0,1,1",
        ],
    )
    assert result.exit_code != 0
    assert "not both" in result.output


def test_cli_search_intersects_bad_geometry(built_index):
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    runner = CliRunner()
    result = runner.invoke(
        cli_mod.cli,
        ["search", "--local", "--db", str(built_index), "--intersects", "{not valid"],
    )
    assert result.exit_code != 0


# --------------------------------------------------------------------------- #
# STAC API (serve)
# --------------------------------------------------------------------------- #


def test_serve_intersects_get_and_post(built_index):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from umbra_py import serve

    client = TestClient(serve.build_app(built_index))
    geom = _poly((0.2, 0.2), (0.8, 0.2), (0.8, 0.8), (0.2, 0.8))

    # POST body with a GeoJSON geometry (STAC standard).
    resp = client.post("/search", json={"intersects": geom})
    assert resp.status_code == 200
    ids = [f["id"] for f in resp.json()["features"]]
    assert ids == ["a"]

    # GET with intersects as a JSON string.
    resp = client.get("/search", params={"intersects": json.dumps(geom)})
    assert resp.status_code == 200
    assert [f["id"] for f in resp.json()["features"]] == ["a"]


def test_serve_intersects_bbox_mutually_exclusive(built_index):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from umbra_py import serve

    client = TestClient(serve.build_app(built_index))
    geom = _poly((0, 0), (1, 0), (1, 1), (0, 1))
    resp = client.post("/search", json={"intersects": geom, "bbox": [0, 0, 1, 1]})
    assert resp.status_code == 400
    resp = client.get("/search", params={"intersects": json.dumps(geom), "bbox": "0,0,1,1"})
    assert resp.status_code == 400


def test_serve_intersects_bad_geometry_is_400(built_index):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from umbra_py import serve

    client = TestClient(serve.build_app(built_index))
    resp = client.post("/search", json={"intersects": {"type": "Point", "coordinates": [0, 0]}})
    assert resp.status_code == 400


# --------------------------------------------------------------------------- #
# MCP tool
# --------------------------------------------------------------------------- #


def test_mcp_search_catalog_intersects(built_index, monkeypatch):
    pytest.importorskip("mcp")
    from umbra_py import mcp_server as ms

    monkeypatch.setenv("UMBRA_INDEX_DB", str(built_index))
    geom = _poly((0.2, 0.2), (0.8, 0.2), (0.8, 0.8), (0.2, 0.8))
    out = ms.search_catalog(intersects=geom, local=True)
    assert out["count"] == 1
    assert out["items"][0]["id"] == "a"


def test_mcp_search_catalog_intersects_conflicts(built_index):
    pytest.importorskip("mcp")
    from umbra_py import mcp_server as ms

    with pytest.raises(ValueError):
        ms.search_catalog(intersects=_poly((0, 0), (1, 0), (1, 1), (0, 1)), bbox=[0, 0, 1, 1])
