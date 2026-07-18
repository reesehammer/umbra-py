"""Offline tests for stac-geoparquet export (skipped without the export extra)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from umbra_py.exceptions import UmbraError
from umbra_py.export import _export_doc, export_geoparquet
from umbra_py.models import UmbraItem

_BUCKET = "https://s3.us-west-2.amazonaws.com/umbra-open-data-catalog"


def _make_item(item_id, bbox, *, geometry=True, links=None, href=None):
    """A minimal STAC item with a rectangular footprint polygon (by default)
    and one GEC asset — pyarrow cannot write an *empty* assets struct, and
    real Umbra items always carry assets."""
    min_lon, min_lat, max_lon, max_lat = bbox
    doc = {
        "type": "Feature",
        "stac_version": "1.0.0",
        "id": item_id,
        "properties": {"datetime": "2024-01-15T10:00:00Z"},
        "bbox": list(bbox),
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [
                    [min_lon, min_lat],
                    [max_lon, min_lat],
                    [max_lon, max_lat],
                    [min_lon, max_lat],
                    [min_lon, min_lat],
                ]
            ],
        }
        if geometry
        else None,
        "assets": {
            f"acq-{item_id}_GEC.tif": {
                "href": f"{_BUCKET}/sar-data/tasks/SiteA/acq-{item_id}/acq-{item_id}_GEC.tif",
                "type": "image/tiff; application=geotiff; profile=cloud-optimized",
            }
        },
    }
    if links is not None:
        doc["links"] = links
    if href is None:
        href = f"{_BUCKET}/sar-data/tasks/SiteA/acq-{item_id}/acq-{item_id}.stac.v2.json"
    return UmbraItem.from_dict(doc, href=href)


def _read_back(path):
    sga = pytest.importorskip("stac_geoparquet")
    pq = pytest.importorskip("pyarrow.parquet")
    return list(sga.arrow.stac_table_to_items(pq.read_table(path)))


def test_export_round_trip(tmp_path):
    pytest.importorskip("stac_geoparquet")
    items = [_make_item("a", (0, 0, 1, 1)), _make_item("b", (10, 10, 11, 11))]
    out = tmp_path / "catalog.parquet"

    written = export_geoparquet(items, out)

    assert written == 2
    assert out.exists()
    back = {d["id"]: d for d in _read_back(out)}
    assert set(back) == {"a", "b"}
    # Every row must carry a self link pointing back to its sidecar JSON.
    self_hrefs = {
        link["href"]
        for d in back.values()
        for link in d.get("links", [])
        if link.get("rel") == "self"
    }
    assert self_hrefs == {items[0].href, items[1].href}


def test_export_real_sample_item(tmp_path):
    pytest.importorskip("stac_geoparquet")
    doc = json.loads((Path(__file__).parent / "data" / "sample_item.json").read_text())
    item = UmbraItem.from_dict(doc, href=f"{_BUCKET}/sar-data/tasks/x/y/y.stac.v2.json")
    out = tmp_path / "sample.parquet"

    assert export_geoparquet([item], out) == 1
    [back] = _read_back(out)
    assert back["id"] == item.id


def test_export_skips_items_without_geometry(tmp_path):
    pytest.importorskip("stac_geoparquet")
    items = [_make_item("a", (0, 0, 1, 1)), _make_item("no-geom", (5, 5, 6, 6), geometry=False)]
    out = tmp_path / "catalog.parquet"

    assert export_geoparquet(items, out) == 1
    assert {d["id"] for d in _read_back(out)} == {"a"}


def test_export_nothing_exportable_raises(tmp_path):
    pytest.importorskip("stac_geoparquet")
    items = [_make_item("no-geom", (0, 0, 1, 1), geometry=False)]
    with pytest.raises(UmbraError, match="footprint"):
        export_geoparquet(items, tmp_path / "catalog.parquet")


def test_export_keeps_existing_self_link(tmp_path):
    pytest.importorskip("stac_geoparquet")
    existing = {"rel": "self", "href": "https://example.com/already-there.json"}
    item = _make_item("a", (0, 0, 1, 1), links=[existing])
    out = tmp_path / "catalog.parquet"

    export_geoparquet([item], out)

    [back] = _read_back(out)
    self_links = [link for link in back["links"] if link.get("rel") == "self"]
    assert len(self_links) == 1
    assert self_links[0]["href"] == existing["href"]
    # The injection above must not have mutated the item's raw document.
    assert item.raw["links"] == [existing]


def test_export_doc_injects_baked_place():
    """A baked `.place` (from `umbra index bake`) is carried into the exported
    STAC properties as `umbra:place`, without mutating the item's raw doc — so
    the published snapshot has a real place name and no consumer re-geocodes."""
    item = _make_item("a", (0, 0, 1, 1))
    item.place = "Reykjavík, Iceland"
    doc = _export_doc(item)
    assert doc["properties"]["umbra:place"] == "Reykjavík, Iceland"
    assert "umbra:place" not in (item.raw.get("properties") or {})


def test_export_doc_without_place_leaves_properties_untouched():
    item = _make_item("a", (0, 0, 1, 1))
    assert item.place is None
    assert "umbra:place" not in _export_doc(item).get("properties", {})


def test_export_doc_keeps_existing_place_property():
    """If the raw item already declares `umbra:place`, the baked label never
    overrides it (the source document wins)."""
    item = _make_item("a", (0, 0, 1, 1))
    item.raw["properties"]["umbra:place"] = "From the source"
    item.place = "Baked"
    assert _export_doc(item)["properties"]["umbra:place"] == "From the source"


def test_export_round_trip_carries_baked_place(tmp_path):
    """End to end: a baked `.place` reaches the published parquet as
    `umbra:place`, so a DuckDB / geopandas consumer reads a real place name."""
    pytest.importorskip("stac_geoparquet")
    item = _make_item("a", (0, 0, 1, 1))
    item.place = "Reykjavík, Iceland"
    out = tmp_path / "catalog.parquet"

    assert export_geoparquet([item], out) == 1
    [back] = _read_back(out)
    assert back["properties"]["umbra:place"] == "Reykjavík, Iceland"


def test_cli_index_export(tmp_path):
    pytest.importorskip("stac_geoparquet")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod
    from umbra_py.index import CatalogIndex

    db = tmp_path / "catalog.db"
    with CatalogIndex(db) as idx:
        idx.add(_make_item("a", (0, 0, 1, 1)))
        idx.add(_make_item("no-geom", (5, 5, 6, 6), geometry=False))
    out = tmp_path / "catalog.parquet"

    result = CliRunner().invoke(
        cli_mod.cli, ["index", "export", "--db", str(db), "--out", str(out)]
    )

    assert result.exit_code == 0, result.output
    assert "Exported 1 of 2 item(s)" in result.output
    assert "1 without a footprint skipped" in result.output
    assert out.exists()


def test_cli_index_export_missing_index_errors(tmp_path):
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    result = CliRunner().invoke(
        cli_mod.cli,
        ["index", "export", "--db", str(tmp_path / "missing.db"), "--out", str(tmp_path / "o.pq")],
    )
    assert result.exit_code != 0
    assert "No index" in result.output
