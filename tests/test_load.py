"""Offline tests for analysis-ready loading (``umbra_py.load.to_xarray``).

These build a tiny real GeoTIFF on disk and point a synthetic ``UmbraItem`` at
it, so the COG read path is exercised end to end without any network access.
"""

from __future__ import annotations

import math

import pytest

from umbra_py.models import UmbraItem


def _make_geotiff(path, *, width=20, height=10):
    """Write a small north-up UTM GeoTIFF and return (path, src_bounds, crs)."""
    rasterio = pytest.importorskip("rasterio")
    np = pytest.importorskip("numpy")
    from rasterio.transform import from_origin

    # Ascending amplitudes 1..N so we can assert orientation, with a zero
    # (nodata-like, non-positive) pixel in the top-left corner.
    data = (np.arange(width * height, dtype="float32") + 1.0).reshape(height, width)
    data[0, 0] = 0.0

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


def _item_for(tif_path):
    item = UmbraItem(id="test-acq", properties={"datetime": "2024-02-08T12:00:00Z"})
    # asset_href derives public S3 URLs from STAC naming; for the test we point
    # it straight at the local file.
    item.asset_href = lambda asset="GEC": str(tif_path)  # type: ignore[method-assign]
    return item


def test_to_xarray_shape_orientation_and_attrs(tmp_path):
    pytest.importorskip("xarray")
    pytest.importorskip("numpy")
    from umbra_py import to_xarray

    tif, bounds, crs = _make_geotiff(tmp_path / "scene.tif")
    da = to_xarray(_item_for(tif), masked=False)

    assert da.dims == ("y", "x")
    assert da.shape == (10, 20)
    # x ascends west->east, y descends north->south (north-up raster).
    assert da["x"].values[0] < da["x"].values[-1]
    assert da["y"].values[0] > da["y"].values[-1]
    # Geo metadata round-trips.
    assert da.attrs["crs"] == crs.to_string()
    assert len(da.attrs["transform"]) == 6
    assert da.attrs["bounds"] == pytest.approx(tuple(bounds))
    assert da.attrs["item_id"] == "test-acq"
    assert da.attrs["units"] == "amplitude"
    assert "CC BY 4.0" in da.attrs["attribution"]
    # Cell centers, not edges: first x is half a pixel in from the left bound.
    assert da["x"].values[0] == pytest.approx(bounds.left + 5.0)


def test_masked_replaces_nonpositive_with_nan(tmp_path):
    pytest.importorskip("xarray")
    np = pytest.importorskip("numpy")
    from umbra_py import to_xarray

    tif, _, _ = _make_geotiff(tmp_path / "scene.tif")

    raw = to_xarray(_item_for(tif), masked=False)
    assert raw.values[0, 0] == 0.0

    masked = to_xarray(_item_for(tif), masked=True)
    assert math.isnan(masked.values[0, 0])
    assert not np.isnan(masked.values[0, 1])


def test_db_scaling(tmp_path):
    pytest.importorskip("xarray")
    pytest.importorskip("numpy")
    from umbra_py import to_xarray

    tif, _, _ = _make_geotiff(tmp_path / "scene.tif")
    da = to_xarray(_item_for(tif), db=True)

    assert da.attrs["units"] == "dB"
    assert da.name == "backscatter_db"
    # The non-positive corner can't be expressed in dB -> NaN.
    assert math.isnan(da.values[0, 0])
    # A known amplitude maps to 20*log10(amp).
    amp = to_xarray(_item_for(tif), masked=False).values[5, 5]
    assert da.values[5, 5] == pytest.approx(20.0 * math.log10(amp))


def test_max_size_decimates(tmp_path):
    pytest.importorskip("xarray")
    from umbra_py import to_xarray

    tif, _, _ = _make_geotiff(tmp_path / "scene.tif", width=40, height=20)
    da = to_xarray(_item_for(tif), max_size=10, masked=False)

    assert max(da.shape) <= 10
    # Aspect ratio is preserved (40x20 -> 10x5).
    assert da.shape == (5, 10)


def test_bbox_windows_a_subset(tmp_path):
    pytest.importorskip("xarray")
    from rasterio.warp import transform_bounds

    from umbra_py import to_xarray

    tif, bounds, crs = _make_geotiff(tmp_path / "scene.tif")

    # Full extent in EPSG:4326, then take roughly the western quarter.
    left, bottom, right, top = transform_bounds(crs, "EPSG:4326", *bounds)
    sub = (left, bottom, left + (right - left) / 4.0, top)

    full = to_xarray(_item_for(tif), masked=False)
    windowed = to_xarray(_item_for(tif), bbox=sub, masked=False)

    assert windowed.shape[1] < full.shape[1]
    assert windowed["x"].values[-1] < full["x"].values[-1]


def test_bbox_no_overlap_raises(tmp_path):
    pytest.importorskip("xarray")
    from umbra_py import to_xarray

    tif, _, _ = _make_geotiff(tmp_path / "scene.tif")
    with pytest.raises(ValueError, match="does not overlap"):
        to_xarray(_item_for(tif), bbox=(0.0, 0.0, 0.001, 0.001))


def test_to_geotiff_roundtrip(tmp_path):
    pytest.importorskip("xarray")
    rasterio = pytest.importorskip("rasterio")
    np = pytest.importorskip("numpy")
    from umbra_py import to_geotiff

    src_tif, _, crs = _make_geotiff(tmp_path / "scene.tif")
    out = to_geotiff(_item_for(src_tif), tmp_path / "out.tif")

    assert out.exists()
    with rasterio.open(out) as ds:
        assert ds.count == 1
        assert ds.dtypes[0] == "float32"
        assert ds.crs == crs
        assert (ds.width, ds.height) == (20, 10)
        data = ds.read(1)
        # The non-positive corner round-trips as NaN nodata.
        assert np.isnan(ds.nodata)
        assert np.isnan(data[0, 0])
        assert ds.tags()["item_id"] == "test-acq"


def test_cli_load_writes_geotiff(tmp_path, monkeypatch):
    pytest.importorskip("xarray")
    rasterio = pytest.importorskip("rasterio")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    src_tif, _, _ = _make_geotiff(tmp_path / "scene.tif")

    # `umbra load` fetches the STAC JSON then resolves the asset href; stub both
    # so the test stays offline and points at the local GeoTIFF.
    monkeypatch.setattr(cli_mod, "get_json", lambda url: {"id": "cli-acq", "assets": {}})
    monkeypatch.setattr(cli_mod.UmbraItem, "asset_href", lambda self, asset="GEC": str(src_tif))

    out = tmp_path / "clipped.tif"
    result = CliRunner().invoke(
        cli_mod.cli, ["load", "http://example.com/item.json", "--out", str(out), "--max-size", "8"]
    )

    assert result.exit_code == 0, result.output
    assert out.exists()
    with rasterio.open(out) as ds:
        assert ds.count == 1
        assert max(ds.width, ds.height) <= 8
