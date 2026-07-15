"""Offline tests for ML chip preparation (``umbra_py.chips``).

Like ``test_load.py`` these build a tiny real GeoTIFF on disk and point a
synthetic ``UmbraItem`` at it, so the whole tile-read + manifest path runs end
to end with no network access and no model call.
"""

from __future__ import annotations

import json
import math

import pytest

from umbra_py.models import UmbraItem


def _make_geotiff(path, *, width=20, height=20, nodata_corner=True):
    """Write a small north-up UTM GeoTIFF and return (path, bounds, crs)."""
    rasterio = pytest.importorskip("rasterio")
    np = pytest.importorskip("numpy")
    from rasterio.transform import from_origin

    data = (np.arange(width * height, dtype="float32") + 1.0).reshape(height, width)
    if nodata_corner:
        # A block of non-positive pixels in the top-left, so a corner chip is
        # partly invalid and min_valid can filter it.
        data[0:5, 0:5] = 0.0

    transform = from_origin(500000.0, 4000000.0, 10.0, 10.0)  # 10 m pixels
    profile = {
        "driver": "GTiff",
        "height": height,
        "width": width,
        "count": 1,
        "dtype": "float32",
        "crs": "EPSG:32633",
        "transform": transform,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data, 1)
        bounds = dst.bounds
        crs = dst.crs
    return path, bounds, crs


def _item_for(tif_path, **props):
    properties = {
        "datetime": "2024-02-08T12:00:00Z",
        "platform": "Umbra-08",
        "sar:polarizations": ["VV"],
        "sar:product_type": "GEC",
        "view:incidence_angle": 42.0,
        "sar:resolution_range": 0.5,
        "sar:resolution_azimuth": 0.5,
    }
    properties.update(props)
    item = UmbraItem(id="test-acq", properties=properties)
    item.asset_href = lambda asset="GEC": str(tif_path)  # type: ignore[method-assign]
    return item


def test_chip_item_grid_shape_and_count(tmp_path):
    pytest.importorskip("numpy")
    rasterio = pytest.importorskip("rasterio")
    from umbra_py.chips import chip_item

    tif, _, crs = _make_geotiff(tmp_path / "scene.tif", width=20, height=20, nodata_corner=False)
    records = chip_item(_item_for(tif), tmp_path / "chips", chip_size=10)

    # 20x20 raster / 10 px non-overlapping -> a 2x2 grid.
    assert len(records) == 4
    assert {(r.row, r.col) for r in records} == {(0, 0), (0, 1), (1, 0), (1, 1)}
    for rec in records:
        chip = tmp_path / "chips" / rec.path
        assert chip.exists()
        with rasterio.open(chip) as ds:
            assert (ds.width, ds.height) == (10, 10)
            assert ds.crs == crs
            assert ds.tags()["item_id"] == "test-acq"
            assert ds.tags()["attribution"].startswith("Contains Umbra")


def test_partial_edge_tiles_are_dropped(tmp_path):
    pytest.importorskip("numpy")
    from umbra_py.chips import chip_item

    # 25x25 with chip_size 10 -> only the 2x2 full-tile grid fits; the 5 px
    # right/bottom strips are dropped (fixed-size promise).
    tif, _, _ = _make_geotiff(tmp_path / "scene.tif", width=25, height=25, nodata_corner=False)
    records = chip_item(_item_for(tif), tmp_path / "chips", chip_size=10)
    assert len(records) == 4
    assert max(r.col for r in records) == 1
    assert max(r.row for r in records) == 1


def test_stride_overlaps_tiles(tmp_path):
    pytest.importorskip("numpy")
    from umbra_py.chips import chip_item

    tif, _, _ = _make_geotiff(tmp_path / "scene.tif", width=20, height=20, nodata_corner=False)
    # stride 5, chip 10 over 20 px -> origins 0,5,10 in each axis -> 3x3 = 9.
    records = chip_item(_item_for(tif), tmp_path / "chips", chip_size=10, stride=5)
    assert len(records) == 9


def test_min_valid_filters_nodata_corner(tmp_path):
    pytest.importorskip("numpy")
    from umbra_py.chips import chip_item

    tif, _, _ = _make_geotiff(tmp_path / "scene.tif", width=20, height=20, nodata_corner=True)

    # The top-left 10x10 chip is 25% zeros (5x5 of 100) -> valid_fraction 0.75.
    keep_all = chip_item(_item_for(tif), tmp_path / "a", chip_size=10, min_valid=0.0)
    assert len(keep_all) == 4
    corner = next(r for r in keep_all if (r.row, r.col) == (0, 0))
    assert corner.valid_fraction == pytest.approx(0.75)

    # Requiring >90% valid drops the corner chip but keeps the other three.
    filtered = chip_item(_item_for(tif), tmp_path / "b", chip_size=10, min_valid=0.9)
    assert len(filtered) == 3
    assert (0, 0) not in {(r.row, r.col) for r in filtered}


def test_record_carries_geo_and_acquisition_metadata(tmp_path):
    pytest.importorskip("numpy")
    from umbra_py.chips import chip_item

    tif, bounds, _ = _make_geotiff(tmp_path / "scene.tif", width=20, height=20, nodata_corner=False)
    records = chip_item(_item_for(tif), tmp_path / "chips", chip_size=10)
    rec = next(r for r in records if (r.row, r.col) == (0, 0))

    assert rec.item_id == "test-acq"
    assert rec.asset == "GEC"
    assert rec.window == [0, 0, 10, 10]
    assert rec.units == "amplitude"
    assert rec.datetime == "2024-02-08T12:00:00+00:00"
    assert rec.platform == "Umbra-08"
    assert rec.polarizations == ["VV"]
    assert rec.incidence_angle_deg == 42.0
    assert rec.resolution_range_m == 0.5
    assert rec.license == "CC-BY-4.0"
    assert len(rec.transform) == 6
    assert len(rec.bbox) == 4
    # Geographic bbox is lon/lat (EPSG:32633 zone 33N -> ~12-15 E, ~36 N).
    min_lon, min_lat, max_lon, max_lat = rec.bbox
    assert min_lon < max_lon and min_lat < max_lat
    assert -180 <= min_lon <= 180 and -90 <= min_lat <= 90


def test_db_scale_writes_decibels(tmp_path):
    pytest.importorskip("numpy")
    rasterio = pytest.importorskip("rasterio")
    np = pytest.importorskip("numpy")
    from umbra_py.chips import chip_item

    tif, _, _ = _make_geotiff(tmp_path / "scene.tif", width=20, height=20, nodata_corner=False)
    records = chip_item(_item_for(tif), tmp_path / "chips", chip_size=10, db=True)
    assert all(r.units == "dB" for r in records)

    rec = next(r for r in records if (r.row, r.col) == (0, 0))
    with rasterio.open(tmp_path / "chips" / rec.path) as ds:
        data = ds.read([1])[0]
    assert np.isfinite(data).all()
    # The top-left pixel is amplitude 1.0 (arange + 1) -> 20*log10(1) == 0 dB.
    assert data[0, 0] == pytest.approx(0.0)
    # A pixel of known amplitude maps to 20*log10(amp).
    assert data[0, 5] == pytest.approx(20.0 * math.log10(6.0))


def test_npy_format(tmp_path):
    pytest.importorskip("numpy")
    np = pytest.importorskip("numpy")
    from umbra_py.chips import chip_item

    tif, _, _ = _make_geotiff(tmp_path / "scene.tif", width=20, height=20, nodata_corner=False)
    records = chip_item(_item_for(tif), tmp_path / "chips", chip_size=10, fmt="npy")

    assert all(r.path.endswith(".npy") for r in records)
    arr = np.load(tmp_path / "chips" / records[0].path)
    assert arr.shape == (10, 10)
    assert arr.dtype == np.float32


def test_write_chips_writes_jsonl_manifest(tmp_path):
    pytest.importorskip("numpy")
    from umbra_py.chips import write_chips

    tif, _, _ = _make_geotiff(tmp_path / "scene.tif", width=20, height=20, nodata_corner=False)
    dataset = write_chips([_item_for(tif)], tmp_path / "ds", chip_size=10)

    assert dataset.chip_count == 4
    manifest = tmp_path / "ds" / "manifest.jsonl"
    assert manifest.exists()
    lines = manifest.read_text().strip().splitlines()
    assert len(lines) == 4
    first = json.loads(lines[0])
    assert first["item_id"] == "test-acq"
    assert first["attribution"].startswith("Contains Umbra")

    summary = dataset.to_dict()
    assert summary["chip_count"] == 4
    assert summary["item_count"] == 1
    assert summary["license"] == "CC-BY-4.0"


def test_write_chips_geojson_manifest(tmp_path):
    pytest.importorskip("numpy")
    from umbra_py.chips import write_chips

    tif, _, _ = _make_geotiff(tmp_path / "scene.tif", width=20, height=20, nodata_corner=False)
    dataset = write_chips([_item_for(tif)], tmp_path / "ds", chip_size=10, manifest="chips.geojson")

    fc = json.loads((tmp_path / "ds" / "chips.geojson").read_text())
    assert fc["type"] == "FeatureCollection"
    assert len(fc["features"]) == 4
    feat = fc["features"][0]
    assert feat["geometry"]["type"] == "Polygon"
    assert feat["properties"]["item_id"] == "test-acq"
    assert dataset.manifest_path.endswith("chips.geojson")


def test_write_chips_manifest_none_skips_file(tmp_path):
    pytest.importorskip("numpy")
    from umbra_py.chips import write_chips

    tif, _, _ = _make_geotiff(tmp_path / "scene.tif", width=20, height=20, nodata_corner=False)
    dataset = write_chips([_item_for(tif)], tmp_path / "ds", chip_size=10, manifest=None)

    assert dataset.manifest_path is None
    assert not (tmp_path / "ds" / "manifest.jsonl").exists()
    assert dataset.chip_count == 4


def test_invalid_params_raise(tmp_path):
    pytest.importorskip("numpy")
    from umbra_py.chips import chip_item

    tif, _, _ = _make_geotiff(tmp_path / "scene.tif", nodata_corner=False)
    with pytest.raises(ValueError, match="chip_size"):
        chip_item(_item_for(tif), tmp_path / "c", chip_size=0)
    with pytest.raises(ValueError, match="stride"):
        chip_item(_item_for(tif), tmp_path / "c", stride=0)
    with pytest.raises(ValueError, match="fmt"):
        chip_item(_item_for(tif), tmp_path / "c", fmt="jpeg")
    with pytest.raises(ValueError, match="min_valid"):
        chip_item(_item_for(tif), tmp_path / "c", min_valid=1.5)


def test_cli_chips_from_url(tmp_path, monkeypatch):
    pytest.importorskip("numpy")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    tif, _, _ = _make_geotiff(tmp_path / "scene.tif", width=20, height=20, nodata_corner=False)
    monkeypatch.setattr(cli_mod, "get_json", lambda url: {"id": "cli-acq", "assets": {}})
    monkeypatch.setattr(cli_mod.UmbraItem, "asset_href", lambda self, asset="GEC": str(tif))

    out = tmp_path / "ds"
    result = CliRunner().invoke(
        cli_mod.cli,
        [
            "chips",
            "http://example.com/item.json",
            "--out",
            str(out),
            "--chip-size",
            "10",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["chip_count"] == 4
    assert payload["items"] == ["cli-acq"]
    assert (out / "manifest.jsonl").exists()
