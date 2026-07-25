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
        data = ds.read([1])[0]  # list index avoids rasterio's NumPy 2.5 in-place reshape
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


# --- Time-series stacking (``to_stack`` / ``stack_to_geotiff`` / ``umbra stack``) ---
#
# Each scene is a constant-valued UTM raster, optionally shifted east, so a
# co-registered slice is trivially checkable: inside the shared footprint every
# cell of slice N must read that scene's fill value, and outside it must be NaN.


def _stack_scene(path, *, x_offset=0.0, value=1.0, width=40, height=40):
    """Write a constant-valued north-up UTM GeoTIFF shifted ``x_offset`` metres east."""
    rasterio = pytest.importorskip("rasterio")
    np = pytest.importorskip("numpy")
    from rasterio.transform import from_origin

    profile = {
        "driver": "GTiff",
        "height": height,
        "width": width,
        "count": 1,
        "dtype": "float32",
        "crs": "EPSG:32633",
        "transform": from_origin(500000.0 + x_offset, 4000000.0, 10.0, 10.0),
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(np.full((height, width), float(value), dtype="float32"), 1)
    return path


def _stack_item(tif, item_id, when):
    item = UmbraItem(id=item_id, properties={"datetime": when})
    item.asset_href = lambda asset="GEC", _p=str(tif): _p  # type: ignore[method-assign]
    return item


def _three_scenes(tmp_path):
    """Three same-footprint passes with fills 2/4/8, returned newest-first."""
    return [
        _stack_item(
            _stack_scene(tmp_path / f"s{n}.tif", value=v), f"acq-{n}", f"2024-0{n}-08T12:00:00Z"
        )
        for n, v in ((3, 8.0), (2, 4.0), (1, 2.0))
    ]


def test_to_stack_orders_by_time_and_carries_provenance(tmp_path):
    pytest.importorskip("xarray")
    np = pytest.importorskip("numpy")
    from umbra_py import to_stack

    cube = to_stack(_three_scenes(tmp_path), max_size=32)

    assert cube.dims == ("time", "y", "x")
    assert cube.shape[0] == 3
    # Input was newest-first; the cube is oldest-first.
    assert list(cube["item_id"].values) == ["acq-1", "acq-2", "acq-3"]
    assert list(cube["time"].values) == sorted(cube["time"].values)
    # y descends (north-up), x ascends.
    assert cube["y"].values[0] > cube["y"].values[-1]
    assert cube["x"].values[0] < cube["x"].values[-1]
    assert cube.attrs["crs"] == "EPSG:4326"
    assert cube.attrs["extent"] == "intersection"
    assert cube.attrs["units"] == "amplitude"
    assert "CC BY 4.0" in cube.attrs["attribution"]
    # Same footprint every pass: each slice is its own fill value throughout.
    for i, value in enumerate((2.0, 4.0, 8.0)):
        assert np.nanmin(cube.values[i]) == pytest.approx(value)
        assert np.nanmax(cube.values[i]) == pytest.approx(value)


def test_to_stack_intersection_keeps_only_shared_ground(tmp_path):
    """Offset passes align onto the overlap, and every cell has a full series."""
    pytest.importorskip("xarray")
    np = pytest.importorskip("numpy")
    from umbra_py import to_stack

    items = [
        _stack_item(_stack_scene(tmp_path / "a.tif", value=2.0), "a", "2024-01-01T00:00:00Z"),
        _stack_item(
            _stack_scene(tmp_path / "b.tif", x_offset=200.0, value=8.0),
            "b",
            "2024-02-01T00:00:00Z",
        ),
    ]
    cube = to_stack(items, max_size=32)

    # The scenes are 400 m wide and offset by 200 m, so the intersection is
    # half as wide as either -- and no cell is NaN in either slice.
    assert not np.isnan(cube.values).any()
    assert cube.values[0] == pytest.approx(2.0)
    assert cube.values[1] == pytest.approx(8.0)
    left, _, right, _ = cube.attrs["bounds"]
    single = to_stack([items[0]], max_size=32)
    assert right - left < (single.attrs["bounds"][2] - single.attrs["bounds"][0])


def test_to_stack_union_pads_each_slice_with_nan(tmp_path):
    pytest.importorskip("xarray")
    np = pytest.importorskip("numpy")
    from umbra_py import to_stack

    items = [
        _stack_item(_stack_scene(tmp_path / "a.tif", value=2.0), "a", "2024-01-01T00:00:00Z"),
        _stack_item(
            _stack_scene(tmp_path / "b.tif", x_offset=200.0, value=8.0),
            "b",
            "2024-02-01T00:00:00Z",
        ),
    ]
    inter = to_stack(items, max_size=32, extent="intersection")
    cube = to_stack(items, max_size=32, extent="union")

    assert cube.attrs["extent"] == "union"
    # Union spans both footprints, so it is wider than the intersection...
    ul, _, ur, _ = cube.attrs["bounds"]
    il, _, ir, _ = inter.attrs["bounds"]
    assert (ur - ul) > (ir - il)
    # ...and each slice reads NaN over ground it never covered: the far west
    # column belongs to `a` alone, the far east column to `b` alone.
    assert np.isnan(cube.values[1, :, 0]).all()
    assert np.isnan(cube.values[0, :, -1]).all()
    # Where a slice does have data it is still that scene's fill value.
    assert np.nanmax(cube.values[0]) == pytest.approx(2.0)
    assert np.nanmax(cube.values[1]) == pytest.approx(8.0)


def test_to_stack_non_overlapping_footprints_raise(tmp_path):
    pytest.importorskip("xarray")
    from umbra_py import to_stack

    items = [
        _stack_item(_stack_scene(tmp_path / "a.tif"), "a", "2024-01-01T00:00:00Z"),
        _stack_item(
            _stack_scene(tmp_path / "b.tif", x_offset=200_000.0), "b", "2024-02-01T00:00:00Z"
        ),
    ]
    with pytest.raises(ValueError, match="do not all overlap"):
        to_stack(items, max_size=32)
    # union has no such requirement.
    assert to_stack(items, max_size=32, extent="union").shape[0] == 2


def test_to_stack_rejects_undated_and_empty(tmp_path):
    pytest.importorskip("xarray")
    from umbra_py import to_stack

    tif = _stack_scene(tmp_path / "a.tif")
    dated = _stack_item(tif, "dated", "2024-01-01T00:00:00Z")
    undated = UmbraItem(id="no-date", properties={})
    undated.asset_href = lambda asset="GEC": str(tif)  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="no datetime"):
        to_stack([dated, undated])
    with pytest.raises(ValueError, match="at least one acquisition"):
        to_stack([])


def test_to_stack_bad_extent_and_bbox(tmp_path):
    pytest.importorskip("xarray")
    from umbra_py import to_stack

    items = _three_scenes(tmp_path)
    with pytest.raises(ValueError, match="extent must be one of"):
        to_stack(items, extent="everything")
    with pytest.raises(ValueError, match="does not overlap"):
        to_stack(items, bbox=(0.0, 0.0, 0.001, 0.001))


def test_to_stack_db_scale(tmp_path):
    pytest.importorskip("xarray")
    from umbra_py import to_stack

    cube = to_stack(_three_scenes(tmp_path), max_size=32, db=True)

    assert cube.name == "backscatter_db"
    assert cube.attrs["units"] == "dB"
    assert cube.values[0] == pytest.approx(20.0 * math.log10(2.0), abs=1e-4)


def test_to_stack_utm_grid_has_uniform_metre_cells(tmp_path):
    """crs="utm" resolves the site's zone and lays down equal-area cells."""
    pytest.importorskip("xarray")
    np = pytest.importorskip("numpy")
    from umbra_py import to_stack

    cube = to_stack(_three_scenes(tmp_path), max_size=32, crs="utm")

    # The fixtures sit at 500000E/4000000N in EPSG:32633, i.e. zone 33 north.
    assert cube.attrs["crs"] == "EPSG:32633"
    # Coordinates are metres in that zone, not degrees...
    assert cube["x"].values[0] == pytest.approx(500_000.0, abs=100.0)
    assert cube["y"].values[0] == pytest.approx(4_000_000.0, abs=100.0)
    # ...and every cell covers the same ground, which is the point: uniform
    # spacing on both axes, and near-square cells (the aspect is derived from
    # the extent, so x and y resolution agree to within a rounding step).
    dx, dy = np.diff(cube["x"].values), np.diff(cube["y"].values)
    assert dx == pytest.approx(dx[0])
    assert dy == pytest.approx(dy[0])
    assert dx[0] == pytest.approx(-dy[0], rel=0.05)
    # The values themselves survive the different warp.
    for i, value in enumerate((2.0, 4.0, 8.0)):
        assert np.nanmax(cube.values[i]) == pytest.approx(value)


def test_to_stack_accepts_an_explicit_crs(tmp_path):
    pytest.importorskip("xarray")
    np = pytest.importorskip("numpy")
    from umbra_py import to_stack

    cube = to_stack(_three_scenes(tmp_path), max_size=32, crs="EPSG:3857")

    assert cube.attrs["crs"] == "EPSG:3857"
    assert cube["y"].values[0] > cube["y"].values[-1]
    assert np.nanmax(cube.values[0]) == pytest.approx(2.0)


def test_to_stack_rejects_an_unknown_crs(tmp_path):
    pytest.importorskip("xarray")
    from umbra_py import to_stack

    with pytest.raises(ValueError, match="not a CRS"):
        to_stack(_three_scenes(tmp_path), max_size=32, crs="EPSG:not-a-code")


def test_to_stack_clip_bbox_stays_lonlat_under_a_projected_crs(tmp_path):
    """--clip-bbox / bbox= is lon/lat whatever CRS the cube is built in."""
    pytest.importorskip("xarray")
    rasterio = pytest.importorskip("rasterio")
    from rasterio.warp import transform_bounds

    from umbra_py import to_stack

    items = _three_scenes(tmp_path)
    with rasterio.open(str(tmp_path / "s1.tif")) as ds:
        left, bottom, right, top = transform_bounds(ds.crs, "EPSG:4326", *ds.bounds)

    full = to_stack(items, max_size=32, crs="utm")
    west = to_stack(items, max_size=32, crs="utm", bbox=(left, bottom, (left + right) / 2, top))

    assert west.attrs["crs"] == "EPSG:32633"
    fl, _, fr, _ = full.attrs["bounds"]
    wl, _, wr, _ = west.attrs["bounds"]
    assert (wr - wl) < (fr - fl) / 1.5  # clipped to roughly the western half
    # A lon/lat window that misses the site is still reported in lon/lat.
    with pytest.raises(ValueError, match=r"bbox \(0.0, 0.0"):
        to_stack(items, max_size=32, crs="utm", bbox=(0.0, 0.0, 0.001, 0.001))


def test_utm_epsg_picks_the_zone_and_hemisphere():
    from umbra_py.load import _utm_epsg

    assert _utm_epsg(15.0, 36.1) == "EPSG:32633"  # zone 33 north
    assert _utm_epsg(15.0, -36.1) == "EPSG:32733"  # same zone, south
    assert _utm_epsg(-122.4, 37.8) == "EPSG:32610"  # San Francisco, zone 10
    assert _utm_epsg(180.0, 0.0) == "EPSG:32601"  # the wrap lands back in zone 1


def test_stack_to_geotiff_writes_a_band_per_date(tmp_path):
    pytest.importorskip("xarray")
    rasterio = pytest.importorskip("rasterio")
    np = pytest.importorskip("numpy")
    from umbra_py import stack_to_geotiff

    out = stack_to_geotiff(_three_scenes(tmp_path), tmp_path / "cube.tif", max_size=32)

    assert out.exists()
    with rasterio.open(out) as ds:
        assert ds.count == 3
        assert ds.dtypes[0] == "float32"
        assert ds.crs.to_epsg() == 4326
        assert np.isnan(ds.nodata)
        # Bands are oldest-first and self-describing.
        assert [d.split()[-1] for d in ds.descriptions] == ["acq-1", "acq-2", "acq-3"]
        assert ds.descriptions[0].startswith("2024-01-08")
        assert ds.tags()["item_ids"] == "acq-1,acq-2,acq-3"
        assert "CC BY 4.0" in ds.tags()["attribution"]
        assert ds.read([2])[0] == pytest.approx(4.0)


def test_cli_stack_writes_datacube(tmp_path, monkeypatch):
    pytest.importorskip("xarray")
    rasterio = pytest.importorskip("rasterio")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    paths = {
        "one": _stack_scene(tmp_path / "one.tif", value=2.0),
        "two": _stack_scene(tmp_path / "two.tif", value=8.0),
    }
    stac = {
        f"http://example.com/{name}.json": {
            "id": name,
            "properties": {"datetime": f"2024-0{n}-08T12:00:00Z"},
            "assets": {},
        }
        for n, name in enumerate(paths, start=1)
    }
    monkeypatch.setattr(cli_mod, "get_json", lambda url: stac[url])
    monkeypatch.setattr(
        cli_mod.UmbraItem, "asset_href", lambda self, asset="GEC": str(paths[self.id])
    )

    out = tmp_path / "cube.tif"
    result = CliRunner().invoke(
        cli_mod.cli,
        [
            "stack",
            "http://example.com/one.json",
            "http://example.com/two.json",
            "--out",
            str(out),
            "--max-size",
            "16",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "2-band datacube" in result.output
    with rasterio.open(out) as ds:
        assert ds.count == 2
        assert max(ds.width, ds.height) <= 16
        assert ds.read([1])[0] == pytest.approx(2.0)
        assert ds.read([2])[0] == pytest.approx(8.0)


def test_cli_stack_crs_writes_a_projected_cube(tmp_path, monkeypatch):
    pytest.importorskip("xarray")
    rasterio = pytest.importorskip("rasterio")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    paths = {
        "one": _stack_scene(tmp_path / "one.tif", value=2.0),
        "two": _stack_scene(tmp_path / "two.tif", value=8.0),
    }
    stac = {
        f"http://example.com/{name}.json": {
            "id": name,
            "properties": {"datetime": f"2024-0{n}-08T12:00:00Z"},
            "assets": {},
        }
        for n, name in enumerate(paths, start=1)
    }
    monkeypatch.setattr(cli_mod, "get_json", lambda url: stac[url])
    monkeypatch.setattr(
        cli_mod.UmbraItem, "asset_href", lambda self, asset="GEC": str(paths[self.id])
    )

    out = tmp_path / "cube.tif"
    result = CliRunner().invoke(
        cli_mod.cli,
        [
            "stack",
            "http://example.com/one.json",
            "http://example.com/two.json",
            "--out",
            str(out),
            "--max-size",
            "16",
            "--crs",
            "utm",
        ],
    )

    assert result.exit_code == 0, result.output
    with rasterio.open(out) as ds:
        assert ds.crs.to_epsg() == 32633
        # The tag records the resolved zone, so the file says what "utm" meant.
        assert ds.tags()["crs"] == "EPSG:32633"
        assert ds.read([1])[0] == pytest.approx(2.0)


def test_cli_stack_needs_two_urls(tmp_path):
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    result = CliRunner().invoke(
        cli_mod.cli, ["stack", "http://example.com/one.json", "--out", str(tmp_path / "c.tif")]
    )
    assert result.exit_code != 0
    assert "2 or more item URLs" in result.output


def _step_scene(path, *, pixel_m, width, height):
    """A scene whose west half reads 2.0 and east half 8.0, on a ``pixel_m`` grid.

    Two of these at different source resolutions cover identical ground, so a
    co-registered stack must put the step at the same output column in both --
    which a constant-valued scene cannot detect.
    """
    rasterio = pytest.importorskip("rasterio")
    np = pytest.importorskip("numpy")
    from rasterio.transform import from_origin

    data = np.full((height, width), 2.0, dtype="float32")
    data[:, width // 2 :] = 8.0
    profile = {
        "driver": "GTiff",
        "height": height,
        "width": width,
        "count": 1,
        "dtype": "float32",
        "crs": "EPSG:32633",
        "transform": from_origin(500000.0, 4000000.0, pixel_m, pixel_m),
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data, 1)
    return path


def test_to_stack_slices_are_pixel_aligned_across_source_grids(tmp_path):
    pytest.importorskip("xarray")
    np = pytest.importorskip("numpy")
    from umbra_py import to_stack

    coarse = _step_scene(tmp_path / "coarse.tif", pixel_m=10.0, width=40, height=40)
    fine = _step_scene(tmp_path / "fine.tif", pixel_m=5.0, width=80, height=80)
    cube = to_stack(
        [
            _stack_item(coarse, "coarse", "2024-01-01T00:00:00Z"),
            _stack_item(fine, "fine", "2024-02-01T00:00:00Z"),
        ],
        max_size=64,
    )

    # First column at (or past) the step, per slice, along a mid-row.
    row = cube.shape[1] // 2
    steps = [int(np.argmax(cube.values[i, row, :] > 5.0)) for i in range(2)]
    assert steps[0] == steps[1], f"step edge lands on different columns: {steps}"
    assert 0 < steps[0] < cube.shape[2]


def test_cli_stack_json_manifest(tmp_path, monkeypatch):
    pytest.importorskip("xarray")
    pytest.importorskip("rasterio")
    import json

    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    paths = {
        "one": _stack_scene(tmp_path / "one.tif", value=2.0),
        "two": _stack_scene(tmp_path / "two.tif", value=8.0),
    }
    stac = {
        f"http://example.com/{name}.json": {
            "id": name,
            "properties": {"datetime": f"2024-0{n}-08T12:00:00Z"},
            "assets": {},
        }
        for n, name in enumerate(paths, start=1)
    }
    monkeypatch.setattr(cli_mod, "get_json", lambda url: stac[url])
    monkeypatch.setattr(
        cli_mod.UmbraItem, "asset_href", lambda self, asset="GEC": str(paths[self.id])
    )

    out = tmp_path / "cube.tif"
    result = CliRunner().invoke(
        cli_mod.cli,
        [
            "stack",
            "http://example.com/one.json",
            "http://example.com/two.json",
            "--out",
            str(out),
            "--max-size",
            "16",
            "--extent",
            "union",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    manifest = json.loads(result.stdout)
    assert manifest["output"] == str(out)
    assert manifest["items_used"] == ["one", "two"]
    assert manifest["parameters"]["extent"] == "union"
    assert manifest["parameters"]["max_size"] == 16


# --- Time-series statistics (``stack_stats`` / ``umbra stack --stats``) ---
#
# The fixtures are constant-valued scenes with fills 2/4/8, so every doubling is
# exactly 20*log10(2) = 6.0206 dB of brightening -- the deltas are checkable by
# hand rather than by golden value.


_DOUBLING_DB = 6.0206


def test_stack_stats_measures_each_pass_and_the_net_change(tmp_path):
    pytest.importorskip("xarray")
    from umbra_py import stack_stats, to_stack

    stats = stack_stats(to_stack(_three_scenes(tmp_path), max_size=32))

    assert stats["count"] == 3
    assert stats["units"] == "amplitude"
    assert stats["change_threshold_db"] == 3.0
    assert [p["item_id"] for p in stats["passes"]] == ["acq-1", "acq-2", "acq-3"]
    assert stats["passes"][0]["datetime"] == "2024-01-08T12:00:00Z"
    # Constant scenes: the distribution is the fill value with no spread.
    assert stats["passes"][1]["mean"] == pytest.approx(4.0)
    assert stats["passes"][1]["median"] == pytest.approx(4.0)
    assert stats["passes"][1]["std"] == pytest.approx(0.0)
    assert stats["passes"][0]["valid_fraction"] == 1.0
    # The first pass has nothing to compare against; each later one doubled.
    assert stats["passes"][0]["change_vs_previous"] is None
    for record in stats["passes"][1:]:
        change = record["change_vs_previous"]
        assert change["mean_delta_db"] == pytest.approx(_DOUBLING_DB, abs=0.01)
        assert change["brightened_fraction"] == 1.0
        assert change["dimmed_fraction"] == 0.0
        assert change["changed_fraction"] == 1.0
    # Net change is first-to-last: two doublings, i.e. 2 -> 8.
    assert stats["net_change"]["mean_delta_db"] == pytest.approx(2 * _DOUBLING_DB, abs=0.01)
    assert stats["license"] == "CC-BY-4.0"
    assert any("not radiometrically calibrated" in c for c in stats["caveats"])


def test_stack_stats_reports_change_in_db_from_a_db_cube_too(tmp_path):
    """The deltas are scale-invariant; only the per-pass distribution changes."""
    pytest.importorskip("xarray")
    from umbra_py import stack_stats, to_stack

    items = _three_scenes(tmp_path)
    linear = stack_stats(to_stack(items, max_size=32))
    decibel = stack_stats(to_stack(items, max_size=32, db=True))

    assert decibel["units"] == "dB"
    assert decibel["passes"][1]["mean"] == pytest.approx(_DOUBLING_DB * 2, abs=0.01)  # 4.0 in dB
    for a, b in zip(linear["passes"][1:], decibel["passes"][1:], strict=True):
        assert a["change_vs_previous"]["mean_delta_db"] == pytest.approx(
            b["change_vs_previous"]["mean_delta_db"], abs=0.01
        )


def test_stack_stats_omits_area_on_a_geographic_grid(tmp_path):
    pytest.importorskip("xarray")
    from umbra_py import stack_stats, to_stack

    stats = stack_stats(to_stack(_three_scenes(tmp_path), max_size=32))

    assert stats["grid"]["crs"] == "EPSG:4326"
    assert stats["grid"]["cell_area_m2"] is None
    assert stats["net_change"]["changed_area_km2"] is None
    assert any("equal-area" in c and "crs='utm'" in c for c in stats["caveats"])


def test_stack_stats_measures_area_on_a_projected_grid(tmp_path):
    """`crs="utm"` makes cells equal-area, so a changed-cell count is an area."""
    pytest.importorskip("xarray")
    from umbra_py import stack_stats, to_stack

    stats = stack_stats(to_stack(_three_scenes(tmp_path), max_size=32, crs="utm"))

    grid = stats["grid"]
    assert grid["crs"] == "EPSG:32633"
    xres, yres = grid["cell_size"]
    assert grid["cell_area_m2"] == pytest.approx(xres * yres)
    # Every cell changed, so the changed area is the whole 400 m x 400 m scene.
    change = stats["net_change"]
    assert change["changed_fraction"] == 1.0
    assert change["changed_area_km2"] == pytest.approx(
        grid["width"] * grid["height"] * grid["cell_area_m2"] / 1e6, rel=1e-6
    )
    assert change["changed_area_km2"] == pytest.approx(0.16, rel=0.02)
    assert not any("equal-area" in c for c in stats["caveats"])


def test_stack_stats_threshold_gates_what_counts_as_changed(tmp_path):
    pytest.importorskip("xarray")
    from umbra_py import stack_stats, to_stack

    cube = to_stack(_three_scenes(tmp_path), max_size=32)
    # One doubling is ~6 dB and two are ~12, so a 10 dB threshold counts no
    # pass-to-pass change but still counts the cumulative one -- and the mean
    # delta is unchanged either way: the threshold gates counting, not measuring.
    strict = stack_stats(cube, change_threshold_db=10.0)

    assert strict["change_threshold_db"] == 10.0
    step = strict["passes"][1]["change_vs_previous"]
    assert step["changed_fraction"] == 0.0
    assert step["mean_delta_db"] == pytest.approx(_DOUBLING_DB, abs=0.01)
    assert strict["net_change"]["changed_fraction"] == 1.0
    assert strict["net_change"]["mean_delta_db"] == pytest.approx(2 * _DOUBLING_DB, abs=0.01)


def test_stack_stats_compares_only_ground_both_passes_cover(tmp_path):
    """Under `extent="union"` the NaN padding must not read as change."""
    pytest.importorskip("xarray")
    from umbra_py import stack_stats, to_stack

    items = [
        _stack_item(_stack_scene(tmp_path / "a.tif", value=2.0), "a", "2024-01-01T00:00:00Z"),
        _stack_item(
            _stack_scene(tmp_path / "b.tif", x_offset=200.0, value=8.0),
            "b",
            "2024-02-01T00:00:00Z",
        ),
    ]
    stats = stack_stats(to_stack(items, max_size=32, extent="union"))

    grid_cells = stats["grid"]["width"] * stats["grid"]["height"]
    # Each pass covers about two thirds of the union; the overlap is smaller still.
    assert stats["passes"][0]["valid_fraction"] < 1.0
    assert 0 < stats["net_change"]["compared_cells"] < grid_cells
    assert stats["net_change"]["mean_delta_db"] == pytest.approx(2 * _DOUBLING_DB, abs=0.01)


def test_stack_stats_rejects_a_non_cube(tmp_path):
    pytest.importorskip("xarray")
    from umbra_py import stack_stats, to_stack

    cube = to_stack(_three_scenes(tmp_path), max_size=32)
    with pytest.raises(ValueError, match=r"\(time, y, x\) cube"):
        stack_stats(cube.isel(time=0))


def _stack_cli_env(tmp_path, monkeypatch, values=(2.0, 8.0)):
    """Two STAC URLs resolving to local constant-valued scenes, for CLI tests."""
    from umbra_py import cli as cli_mod

    names = ("one", "two")
    paths = {
        name: _stack_scene(tmp_path / f"{name}.tif", value=value)
        for name, value in zip(names, values, strict=True)
    }
    stac = {
        f"http://example.com/{name}.json": {
            "id": name,
            "properties": {"datetime": f"2024-0{n}-08T12:00:00Z"},
            "assets": {},
        }
        for n, name in enumerate(names, start=1)
    }
    monkeypatch.setattr(cli_mod, "get_json", lambda url: stac[url])
    monkeypatch.setattr(
        cli_mod.UmbraItem, "asset_href", lambda self, asset="GEC": str(paths[self.id])
    )
    return list(stac)


def test_cli_stack_stats_measures_without_writing_a_file(tmp_path, monkeypatch):
    pytest.importorskip("xarray")
    pytest.importorskip("rasterio")
    import json

    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    urls = _stack_cli_env(tmp_path, monkeypatch)
    result = CliRunner().invoke(
        cli_mod.cli, ["stack", *urls, "--stats", "--max-size", "16", "--crs", "utm"]
    )

    assert result.exit_code == 0, result.output
    # No --out: stdout carries the statistics object alone.
    stats = json.loads(result.stdout)
    assert stats["count"] == 2
    assert stats["grid"]["crs"] == "EPSG:32633"
    assert stats["net_change"]["mean_delta_db"] == pytest.approx(2 * _DOUBLING_DB, abs=0.01)
    assert stats["net_change"]["changed_area_km2"] is not None
    assert not list(tmp_path.glob("*cube*"))


def test_cli_stack_stats_alongside_the_file_and_in_the_manifest(tmp_path, monkeypatch):
    pytest.importorskip("xarray")
    rasterio = pytest.importorskip("rasterio")
    import json

    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    urls = _stack_cli_env(tmp_path, monkeypatch)
    out = tmp_path / "cube.tif"
    runner = CliRunner()
    result = runner.invoke(
        cli_mod.cli, ["stack", *urls, "--out", str(out), "--stats", "--max-size", "16"]
    )

    assert result.exit_code == 0, result.output
    # The file is still written -- and the human note moves to stderr so the
    # statistics stay a single parseable object on stdout.
    with rasterio.open(out) as ds:
        assert ds.count == 2
    assert "2-band datacube" in result.stderr
    assert json.loads(result.stdout)["count"] == 2

    # Under --json the statistics ride inside the render manifest instead, so
    # stdout is one object either way.
    out2 = tmp_path / "cube2.tif"
    manifest = json.loads(
        runner.invoke(
            cli_mod.cli,
            ["stack", *urls, "--out", str(out2), "--stats", "--max-size", "16", "--json"],
        ).stdout
    )
    assert manifest["output"] == str(out2)
    assert manifest["stats"]["count"] == 2
    assert manifest["stats"]["passes"][1]["change_vs_previous"]["mean_delta_db"] == pytest.approx(
        2 * _DOUBLING_DB, abs=0.01
    )


def test_cli_stack_needs_an_output_or_stats(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    urls = _stack_cli_env(tmp_path, monkeypatch)
    result = CliRunner().invoke(cli_mod.cli, ["stack", *urls])
    assert result.exit_code != 0
    assert "--out to write the datacube, --stats to measure it" in result.output


def test_cli_stack_stats_keeps_stdout_json_in_search_mode(tmp_path, monkeypatch):
    """The search-mode "Selected N of M" note must not precede the JSON."""
    pytest.importorskip("xarray")
    pytest.importorskip("rasterio")
    import json

    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    paths = {
        "one": _stack_scene(tmp_path / "one.tif", value=2.0),
        "two": _stack_scene(tmp_path / "two.tif", value=8.0),
    }
    found = [
        UmbraItem(id=name, properties={"datetime": f"2024-0{n}-08T12:00:00Z"})
        for n, name in enumerate(paths, start=1)
    ]
    monkeypatch.setattr(
        cli_mod.UmbraItem, "asset_href", lambda self, asset="GEC": str(paths[self.id])
    )
    monkeypatch.setattr(cli_mod, "_gather_items", lambda **kwargs: found)

    result = CliRunner().invoke(
        cli_mod.cli, ["stack", "--area", "SiteA", "--stats", "--max-size", "16"]
    )

    assert result.exit_code == 0, result.output
    assert "Selected 2 of 2" in result.stderr
    assert json.loads(result.stdout)["count"] == 2
