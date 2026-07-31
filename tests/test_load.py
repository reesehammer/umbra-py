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
    monkeypatch.setattr(
        "umbra_py.cli._shared.get_json", lambda url: {"id": "cli-acq", "assets": {}}
    )
    monkeypatch.setattr(
        "umbra_py.cli._shared.UmbraItem.asset_href", lambda self, asset="GEC": str(src_tif)
    )

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
    monkeypatch.setattr("umbra_py.cli._shared.get_json", lambda url: stac[url])
    monkeypatch.setattr(
        "umbra_py.cli._shared.UmbraItem.asset_href", lambda self, asset="GEC": str(paths[self.id])
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
    monkeypatch.setattr("umbra_py.cli._shared.get_json", lambda url: stac[url])
    monkeypatch.setattr(
        "umbra_py.cli._shared.UmbraItem.asset_href", lambda self, asset="GEC": str(paths[self.id])
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
    monkeypatch.setattr("umbra_py.cli._shared.get_json", lambda url: stac[url])
    monkeypatch.setattr(
        "umbra_py.cli._shared.UmbraItem.asset_href", lambda self, asset="GEC": str(paths[self.id])
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


# --- The spatial breakdown (``stack_stats(blocks=N)`` / ``umbra stack --blocks``) ---
#
# One corner brightens on the last pass only, so the answer is checkable by hand
# in both axes: on a 4x4 grid the northeast block reads +12.04 dB (two doublings)
# net, every other block reads 0.0, and the interval that moved is the last one.


def _corner_scene(path, *, base=2.0, corner=2.0, width=40, height=40):
    """A scene reading ``base`` everywhere but its northeast sixteenth.

    The hot patch is exactly one cell of a 4x4 block grid, so a breakdown at that
    resolution isolates it and a scene-wide mean dilutes it sixteen-fold.
    """
    rasterio = pytest.importorskip("rasterio")
    np = pytest.importorskip("numpy")
    from rasterio.transform import from_origin

    data = np.full((height, width), float(base), dtype="float32")
    data[: height // 4, width * 3 // 4 :] = float(corner)
    profile = {
        "driver": "GTiff",
        "height": height,
        "width": width,
        "count": 1,
        "dtype": "float32",
        "crs": "EPSG:32633",
        "transform": from_origin(500000.0, 4000000.0, 10.0, 10.0),
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data, 1)
    return path


def _corner_series(tmp_path):
    """Three passes; only the northeast corner, and only on the last, changes."""
    return [
        _stack_item(_corner_scene(tmp_path / "p1.tif"), "acq-1", "2024-01-08T12:00:00Z"),
        _stack_item(_corner_scene(tmp_path / "p2.tif"), "acq-2", "2024-02-08T12:00:00Z"),
        _stack_item(
            _corner_scene(tmp_path / "p3.tif", corner=8.0), "acq-3", "2024-03-08T12:00:00Z"
        ),
    ]


def _block(spatial, row, col):
    return next(b for b in spatial["blocks"] if (b["row"], b["col"]) == (row, col))


def test_stack_stats_blocks_locate_change_in_space_and_in_time(tmp_path):
    pytest.importorskip("xarray")
    from umbra_py import stack_stats, to_stack

    cube = to_stack(_corner_series(tmp_path), max_size=32, crs="utm")
    spatial = stack_stats(cube, blocks=4)["spatial"]

    assert (spatial["grid_rows"], spatial["grid_cols"]) == (4, 4)
    assert len(spatial["blocks"]) == 16
    assert spatial["bounds_crs"] == "EPSG:32633"

    # Where: the northeast corner block moved two doublings, the southwest none.
    northeast = _block(spatial, 0, 3)
    southwest = _block(spatial, 3, 0)
    assert northeast["compass"] == "northeast"
    assert southwest["compass"] == "southwest"
    assert northeast["net_change"]["mean_delta_db"] == pytest.approx(2 * _DOUBLING_DB, abs=0.01)
    assert northeast["net_change"]["changed_fraction"] == 1.0
    assert southwest["net_change"]["mean_delta_db"] == pytest.approx(0.0, abs=0.01)
    assert southwest["net_change"]["changed_fraction"] == 0.0

    # When: the change landed on the last interval, not the first.
    assert northeast["peak_interval"]["from_item_id"] == "acq-2"
    assert northeast["peak_interval"]["to_item_id"] == "acq-3"
    assert northeast["peak_interval"]["to_datetime"] == "2024-03-08T12:00:00Z"
    assert northeast["peak_interval"]["mean_delta_db"] == pytest.approx(2 * _DOUBLING_DB, abs=0.01)
    assert northeast["peak_interval"]["changed_area_km2"] > 0

    # And the headline points at that block without the caller scanning the grid.
    peak = spatial["peak_block"]
    assert (peak["row"], peak["col"], peak["compass"]) == (0, 3, "northeast")
    assert peak["direction"] == "brighter"

    # Scene-wide, the same change is a sixteenth of the ground and all but
    # disappears -- which is the reason to ask for the breakdown at all.
    assert stack_stats(cube)["net_change"]["mean_delta_db"] == pytest.approx(
        2 * _DOUBLING_DB / 16, abs=0.05
    )


def test_stack_stats_blocks_carry_locatable_geometry(tmp_path):
    """A block is only useful if you can put it back on the ground."""
    pytest.importorskip("xarray")
    from umbra_py import stack_stats, to_stack

    spatial = stack_stats(to_stack(_corner_series(tmp_path), max_size=32, crs="utm"), blocks=4)[
        "spatial"
    ]
    northeast = _block(spatial, 0, 3)
    southwest = _block(spatial, 3, 0)

    # Bounds are in the cube's own (projected) CRS, north-up and non-overlapping.
    left, bottom, right, top = northeast["bounds"]
    assert left < right and bottom < top
    assert bottom >= southwest["bounds"][3]
    assert left >= southwest["bounds"][2]

    # The lon/lat centre is what a map or a geocoder needs, so it comes back in
    # degrees whatever the grid's CRS -- north and east of the opposite corner.
    lon, lat = northeast["center_lonlat"]
    assert -180 <= lon <= 180 and -90 <= lat <= 90
    assert lat > southwest["center_lonlat"][1]
    assert lon > southwest["center_lonlat"][0]

    # The ASCII grid is north-up, one row per block row, and marks the hot corner.
    lines = spatial["grid_text"].splitlines()
    assert len(lines) == 4
    assert "+12.0" in lines[0]
    assert "+12.0" not in lines[-1]


def test_stack_stats_block_series_keeps_every_interval_the_peak_was_picked_from(tmp_path):
    """A peak says how hard a block moved; the series says what its history looked like."""
    pytest.importorskip("xarray")
    from umbra_py import stack_stats, to_stack

    cube = to_stack(_corner_series(tmp_path), max_size=32, crs="utm")
    spatial = stack_stats(cube, blocks=4, block_series=True)["spatial"]
    northeast = _block(spatial, 0, 3)

    # Three passes -> two consecutive intervals, oldest first, chained end to end.
    series = northeast["series"]
    assert [(s["from_item_id"], s["to_item_id"]) for s in series] == [
        ("acq-1", "acq-2"),
        ("acq-2", "acq-3"),
    ]
    assert [s["from_datetime"] for s in series] == [
        "2024-01-08T12:00:00Z",
        "2024-02-08T12:00:00Z",
    ]

    # This corner jumped once and held, so the shape is flat-then-step -- the
    # distinction a single peak interval throws away.
    assert series[0]["mean_delta_db"] == pytest.approx(0.0, abs=0.01)
    assert series[1]["mean_delta_db"] == pytest.approx(2 * _DOUBLING_DB, abs=0.01)

    # The peak is a member of the series, not a separately-computed number.
    assert northeast["peak_interval"] == max(series, key=lambda s: abs(s["mean_delta_db"]))

    # Every block carries its own series, and a quiet one is flat throughout.
    assert all("series" in b for b in spatial["blocks"])
    assert all(
        s["mean_delta_db"] == pytest.approx(0.0, abs=0.01) for s in _block(spatial, 3, 0)["series"]
    )


def test_stack_stats_block_series_is_opt_in_and_needs_a_grid(tmp_path):
    pytest.importorskip("xarray")
    from umbra_py import stack_stats, to_stack

    cube = to_stack(_corner_series(tmp_path), max_size=32, crs="utm")

    # Off by default: the breakdown is unchanged for callers that didn't ask.
    assert all("series" not in b for b in stack_stats(cube, blocks=4)["spatial"]["blocks"])

    # The series hangs on a block, so asking without one is refused, not dropped.
    with pytest.raises(ValueError, match="block_series needs a blocks grid"):
        stack_stats(cube, block_series=True)


def test_stack_stats_block_series_reports_no_intervals_for_unobserved_ground(tmp_path):
    """A block with nothing to compare gets an empty series, not a fake zero."""
    pytest.importorskip("xarray")
    from umbra_py import stack_stats, to_stack

    items = [
        _stack_item(_stack_scene(tmp_path / "a.tif", value=2.0), "a", "2024-01-01T00:00:00Z"),
        _stack_item(
            _stack_scene(tmp_path / "b.tif", x_offset=300.0, value=8.0),
            "b",
            "2024-02-01T00:00:00Z",
        ),
    ]
    spatial = stack_stats(
        to_stack(items, max_size=32, extent="union"), blocks=4, block_series=True
    )["spatial"]

    unobserved = [b for b in spatial["blocks"] if b["net_change"] is None]
    assert unobserved
    assert all(b["series"] == [] for b in unobserved)


def test_stack_stats_skips_the_breakdown_by_default(tmp_path):
    pytest.importorskip("xarray")
    from umbra_py import stack_stats, to_stack

    cube = to_stack(_three_scenes(tmp_path), max_size=32)
    assert "spatial" not in stack_stats(cube)
    assert "spatial" not in stack_stats(cube, blocks=0)


def test_stack_stats_blocks_never_read_unobserved_ground_as_change(tmp_path):
    """Blocks outside the overlap have nothing to compare, not a change of zero."""
    pytest.importorskip("xarray")
    from umbra_py import stack_stats, to_stack

    items = [
        _stack_item(_stack_scene(tmp_path / "a.tif", value=2.0), "a", "2024-01-01T00:00:00Z"),
        _stack_item(
            _stack_scene(tmp_path / "b.tif", x_offset=300.0, value=8.0),
            "b",
            "2024-02-01T00:00:00Z",
        ),
    ]
    spatial = stack_stats(to_stack(items, max_size=32, extent="union"), blocks=4)["spatial"]

    unobserved = [b for b in spatial["blocks"] if b["net_change"] is None]
    assert unobserved, "the west and east edges are imaged on one pass only"
    assert all(b["peak_interval"] is None for b in unobserved)
    # An unimaged block is a gap in the map, not a zero.
    assert "   ." in spatial["grid_text"]


def test_stack_stats_rejects_a_non_cube(tmp_path):
    pytest.importorskip("xarray")
    from umbra_py import stack_stats, to_stack

    cube = to_stack(_three_scenes(tmp_path), max_size=32)
    with pytest.raises(ValueError, match=r"\(time, y, x\) cube"):
        stack_stats(cube.isel(time=0))


def _stack_cli_env(tmp_path, monkeypatch, values=(2.0, 8.0)):
    """Two STAC URLs resolving to local constant-valued scenes, for CLI tests."""

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
    monkeypatch.setattr("umbra_py.cli._shared.get_json", lambda url: stac[url])
    monkeypatch.setattr(
        "umbra_py.cli._shared.UmbraItem.asset_href", lambda self, asset="GEC": str(paths[self.id])
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


def test_cli_stack_blocks_implies_stats_and_adds_the_breakdown(tmp_path, monkeypatch):
    pytest.importorskip("xarray")
    pytest.importorskip("rasterio")
    import json

    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    urls = _stack_cli_env(tmp_path, monkeypatch)
    # No --stats and no --out: --blocks alone measures the site spatially.
    result = CliRunner().invoke(
        cli_mod.cli, ["stack", *urls, "--blocks", "4", "--max-size", "16", "--crs", "utm"]
    )

    assert result.exit_code == 0, result.output
    spatial = json.loads(result.stdout)["spatial"]
    assert (spatial["grid_rows"], len(spatial["blocks"])) == (4, 16)
    assert spatial["peak_block"]["compass"]
    assert len(spatial["grid_text"].splitlines()) == 4

    bad = CliRunner().invoke(cli_mod.cli, ["stack", *urls, "--blocks", "-1"])
    assert bad.exit_code != 0
    assert "--blocks must be 0" in bad.output


def test_cli_stack_block_series_adds_the_sequence_and_needs_a_grid(tmp_path, monkeypatch):
    pytest.importorskip("xarray")
    pytest.importorskip("rasterio")
    import json

    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    urls = _stack_cli_env(tmp_path, monkeypatch)
    result = CliRunner().invoke(
        cli_mod.cli,
        ["stack", *urls, "--blocks", "2", "--block-series", "--max-size", "16", "--crs", "utm"],
    )

    assert result.exit_code == 0, result.output
    blocks = json.loads(result.stdout)["spatial"]["blocks"]
    # Two passes -> one interval per block, and it is the one the peak names.
    assert all(len(b["series"]) == 1 for b in blocks)
    assert all(b["series"][0] == b["peak_interval"] for b in blocks)

    bad = CliRunner().invoke(cli_mod.cli, ["stack", *urls, "--block-series", "--max-size", "16"])
    assert bad.exit_code != 0
    assert "--block-series needs --blocks" in bad.output


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
        "umbra_py.cli._shared.UmbraItem.asset_href", lambda self, asset="GEC": str(paths[self.id])
    )
    monkeypatch.setattr("umbra_py.cli._shared._gather_items", lambda **kwargs: found)

    result = CliRunner().invoke(
        cli_mod.cli, ["stack", "--area", "SiteA", "--stats", "--max-size", "16"]
    )

    assert result.exit_code == 0, result.output
    assert "Selected 2 of 2" in result.stderr
    assert json.loads(result.stdout)["count"] == 2


# --- Lazy (dask-backed) stacking (``to_stack(lazy=True)`` / ``umbra stack --lazy``) ---
#
# The claim under test is an equivalence plus a deferral: a lazy cube holds the
# identical numbers, and holds none of them until something asks.


def _dask_cube(items, **kwargs):
    """A lazy cube, skipping the test when the optional dask extra is absent."""
    pytest.importorskip("dask.array")
    from umbra_py import to_stack

    return to_stack(items, lazy=True, **kwargs)


def test_to_stack_lazy_defers_every_read_until_compute(tmp_path, monkeypatch):
    pytest.importorskip("xarray")
    pytest.importorskip("dask.array")
    from umbra_py import load as load_mod

    reads = []
    original = load_mod._open_slab
    monkeypatch.setattr(
        load_mod,
        "_open_slab",
        lambda url, grid, **kw: (reads.append(url), original(url, grid, **kw))[1],
    )

    cube = _dask_cube(_three_scenes(tmp_path), max_size=32)

    # The grid is resolved eagerly (it needs every footprint), the pixels are not.
    assert cube.shape[0] == 3
    assert reads == []
    # One chunk per acquisition: the unit a reduction can walk one at a time.
    assert cube.chunks[0] == (1, 1, 1)

    loaded = cube.compute()

    # Each source read exactly once, by the task that owns its chunk.
    assert len(reads) == len(set(reads)) == 3
    assert float(loaded.isel(time=1).mean()) == pytest.approx(4.0)


def test_to_stack_lazy_matches_the_eager_cube(tmp_path):
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
    for extent in ("intersection", "union"):
        eager = to_stack(items, max_size=24, extent=extent, db=True)
        lazy = _dask_cube(items, max_size=24, extent=extent, db=True)

        assert lazy.dims == eager.dims
        assert lazy.attrs == eager.attrs
        assert list(lazy["item_id"].values) == list(eager["item_id"].values)
        # Identical, NaN padding included -- the deferral changes when the bytes
        # are fetched, never what they are.
        np.testing.assert_array_equal(np.asarray(lazy.compute().values), np.asarray(eager.values))


def test_to_stack_lazy_still_validates_the_grid_eagerly(tmp_path):
    """A grid that cannot exist is an error now, not a surprise at compute time."""
    pytest.importorskip("xarray")
    pytest.importorskip("dask.array")

    items = [
        _stack_item(_stack_scene(tmp_path / "a.tif"), "a", "2024-01-01T00:00:00Z"),
        _stack_item(
            _stack_scene(tmp_path / "b.tif", x_offset=200_000.0), "b", "2024-02-01T00:00:00Z"
        ),
    ]
    with pytest.raises(ValueError, match="do not all overlap"):
        _dask_cube(items, max_size=16)


def test_to_stack_lazy_without_dask_names_the_extra(tmp_path, monkeypatch):
    pytest.importorskip("xarray")
    import sys

    from umbra_py import to_stack
    from umbra_py.exceptions import MissingDependencyError

    # Absent for the duration of the call, however it is installed here.
    monkeypatch.setitem(sys.modules, "dask", None)

    with pytest.raises(MissingDependencyError) as excinfo:
        to_stack(_three_scenes(tmp_path), max_size=16, lazy=True)

    assert "umbra-py[dask]" in str(excinfo.value)


def test_stack_stats_measures_a_lazy_cube_identically(tmp_path):
    pytest.importorskip("xarray")
    pytest.importorskip("dask.array")
    from umbra_py import stack_stats, to_stack

    items = _three_scenes(tmp_path)
    kwargs = {"max_size": 24, "crs": "utm"}
    stats_kwargs = {"blocks": 2, "block_series": True}

    eager = stack_stats(to_stack(items, **kwargs), **stats_kwargs)
    lazy = stack_stats(_dask_cube(items, **kwargs), **stats_kwargs)

    # Every number, down to the per-block series and the ASCII heat-grid.
    assert lazy == eager
    assert lazy["spatial"]["blocks"][0]["series"]


def test_stack_stats_reads_one_pass_at_a_time(tmp_path, monkeypatch):
    """Peak memory follows the grid, not the length of the series."""
    pytest.importorskip("xarray")
    pytest.importorskip("dask.array")
    from umbra_py import load as load_mod
    from umbra_py import stack_stats

    computed = []
    original = load_mod._pass_slabs
    monkeypatch.setattr(
        load_mod,
        "_pass_slabs",
        lambda np, cube, index, **kw: (computed.append(index), original(np, cube, index, **kw))[1],
    )

    stack_stats(_dask_cube(_three_scenes(tmp_path), max_size=24), blocks=2)

    # One materialised slice per pass -- never the whole cube at once.
    assert computed == [0, 1, 2]


def test_stack_to_geotiff_lazy_writes_the_same_file(tmp_path):
    pytest.importorskip("xarray")
    rasterio = pytest.importorskip("rasterio")
    pytest.importorskip("dask.array")
    np = pytest.importorskip("numpy")
    from umbra_py import stack_to_geotiff

    items = _three_scenes(tmp_path)
    eager = stack_to_geotiff(items, tmp_path / "eager.tif", max_size=24)
    lazy = stack_to_geotiff(items, tmp_path / "lazy.tif", max_size=24, lazy=True)

    with rasterio.open(eager) as a, rasterio.open(lazy) as b:
        assert a.count == b.count == 3
        assert a.descriptions == b.descriptions
        assert a.tags()["item_ids"] == b.tags()["item_ids"]
        np.testing.assert_array_equal(a.read(), b.read())


# --- Windowed chunking within a pass (``to_stack(chunk_size=...)``) ---
#
# One chunk per acquisition makes a whole slice the smallest unit of work; these
# pin the claim that windowing changes only *what is resident*, never the values.


def _ramp_scene(path, *, width=40, height=40):
    """A scene whose every pixel differs, so a seam at a window edge would show."""
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
        "transform": from_origin(500000.0, 4000000.0, 10.0, 10.0),
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write((np.arange(width * height, dtype="float32") + 1.0).reshape(height, width), 1)
    return path


def _ramp_scenes(tmp_path):
    return [
        _stack_item(_ramp_scene(tmp_path / f"r{n}.tif"), f"acq-{n}", f"2024-0{n}-08T12:00:00Z")
        for n in (1, 2)
    ]


def test_to_stack_chunk_size_windows_each_pass(tmp_path):
    pytest.importorskip("xarray")
    from umbra_py import to_stack

    cube = _dask_cube(_three_scenes(tmp_path), max_size=32, chunk_size=16)

    # Same cube, cut differently: one chunk per pass, and windows inside it.
    assert cube.shape == to_stack(_three_scenes(tmp_path), max_size=32).shape
    assert cube.chunks[0] == (1, 1, 1)
    assert max(cube.chunks[1]) <= 16 and max(cube.chunks[2]) <= 16
    assert sum(cube.chunks[1]) == cube.shape[1]
    assert sum(cube.chunks[2]) == cube.shape[2] == 32


def test_to_stack_chunk_size_matches_the_unchunked_cube(tmp_path):
    """A window edge is not a seam: the numbers are the whole-slab read's."""
    pytest.importorskip("xarray")
    np = pytest.importorskip("numpy")

    items = _ramp_scenes(tmp_path)
    whole = _dask_cube(items, max_size=24, db=True)
    # 24 is not a multiple of 10, so the last window is a partial one.
    windowed = _dask_cube(items, max_size=24, db=True, chunk_size=10)

    assert windowed.attrs == whole.attrs
    # The grid is 24 columns wide, so the last window of a row is a partial one.
    assert windowed.chunks[2] == (10, 10, 4)
    np.testing.assert_array_equal(
        np.asarray(windowed.compute().values), np.asarray(whole.compute().values)
    )


def test_to_stack_chunk_size_reads_one_window_at_a_time(tmp_path):
    """The unit of work is the window, and only the windows asked for are read."""
    pytest.importorskip("xarray")
    from umbra_py import load as load_mod

    shapes = []
    original = load_mod._open_slab

    def record(url, grid, **kw):
        shapes.append((grid.width, grid.height))
        return original(url, grid, **kw)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(load_mod, "_open_slab", record)
        cube = _dask_cube(_three_scenes(tmp_path), max_size=32, chunk_size=16)
        assert shapes == []
        # One pass, one window of it -- one task out of the cube's several.
        chunk = cube.data.blocks[0, 0, 0].compute()

    assert chunk.shape[1:] == (16, 16)
    # Only the window that was asked for was opened and read.
    assert shapes == [(16, 16)]


def test_to_stack_chunk_size_needs_lazy(tmp_path):
    pytest.importorskip("xarray")
    from umbra_py import to_stack

    with pytest.raises(ValueError, match="chunk_size needs lazy=True"):
        to_stack(_three_scenes(tmp_path), max_size=16, chunk_size=8)


def test_to_stack_rejects_a_nonpositive_chunk_size(tmp_path):
    pytest.importorskip("xarray")
    pytest.importorskip("dask.array")
    from umbra_py import to_stack

    with pytest.raises(ValueError, match="positive pixel count"):
        to_stack(_three_scenes(tmp_path), max_size=16, lazy=True, chunk_size=0)


def test_stack_to_geotiff_chunked_writes_window_by_window(tmp_path):
    """The file is the same one; the writer just never holds a whole band."""
    pytest.importorskip("xarray")
    rasterio = pytest.importorskip("rasterio")
    np = pytest.importorskip("numpy")
    pytest.importorskip("dask.array")
    from umbra_py import stack_to_geotiff

    items = _ramp_scenes(tmp_path)
    whole = stack_to_geotiff(items, tmp_path / "whole.tif", max_size=24, lazy=True)
    windowed = stack_to_geotiff(
        items, tmp_path / "windowed.tif", max_size=24, lazy=True, chunk_size=10
    )

    with rasterio.open(whole) as a, rasterio.open(windowed) as b:
        assert a.count == b.count == 2
        assert a.descriptions == b.descriptions
        np.testing.assert_array_equal(a.read(), b.read())


def test_cube_windows_covers_the_grid_exactly(tmp_path):
    pytest.importorskip("xarray")
    from umbra_py import load as load_mod

    cube = _dask_cube(_three_scenes(tmp_path), max_size=24, chunk_size=10)
    windows = load_mod._cube_windows(cube)

    assert windows[0] == ((0, 10), (0, 10))
    # The grid is 24 columns wide, so a row ends on a 4-wide partial window.
    assert windows[-1] == ((10, 20), (20, 24))
    covered = sum((r1 - r0) * (c1 - c0) for (r0, r1), (c0, c1) in windows)
    assert covered == cube.shape[1] * cube.shape[2]
    # An eager cube has no chunks, so it keeps the single whole-band write.
    from umbra_py import to_stack

    eager = to_stack(_three_scenes(tmp_path), max_size=24)
    assert load_mod._cube_windows(eager) == [((0, eager.shape[1]), (0, eager.shape[2]))]


# --- Measuring window by window (``stack_stats(windowed=True)``) ---
#
# The writer already streams a chunked cube; these pin the same claim for the
# reduction: what is resident is a window, and the only number that moves is a
# percentile (which needs the whole distribution and so is estimated).


def _speckle_scene(path, *, scale=1.0, width=40, height=40):
    """A scene with a SAR-like amplitude distribution, brightened by ``scale``.

    Lognormal rather than a ramp because a percentile is what is being checked:
    backscatter clusters within a dozen decibels, where a fixed-width dB bin
    holds many cells, and a ramp spanning five *orders of magnitude* would put
    neighbouring samples further apart than the bins themselves.
    """
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
        "transform": from_origin(500000.0, 4000000.0, 10.0, 10.0),
    }
    values = np.random.default_rng(7).lognormal(2.0, 0.8, (height, width)) * scale
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(values.astype("float32"), 1)
    return path


def _spread_scenes(tmp_path, count=3):
    """Passes that both *differ across the scene* and *change over time*.

    A percentile means nothing on a constant scene and a change means nothing on
    two identical ones, so the windowed walk needs a fixture with both: one
    speckle pattern doubling each pass, i.e. a spread distribution moving
    6.02 dB a step.
    """
    return [
        _stack_item(
            _speckle_scene(tmp_path / f"w{n}.tif", scale=2.0 ** (n - 1)),
            f"acq-{n}",
            f"2024-0{n}-08T12:00:00Z",
        )
        for n in range(1, count + 1)
    ]


def _without_quantiles(summary):
    """A summary stripped of everything ``windowed=True`` reports approximately."""
    import json

    trimmed = json.loads(json.dumps(summary))
    for record in trimmed["passes"]:
        for key in ("median", "p5", "p95"):
            record.pop(key)
    trimmed.pop("quantile_method", None)
    trimmed.pop("quantile_bin_db", None)
    trimmed["caveats"] = [c for c in trimmed["caveats"] if "window by window" not in c]
    return trimmed


def test_stack_stats_windowed_matches_the_whole_slice_walk(tmp_path):
    """Counts, means, spreads and every change number are sums, so they are exact."""
    pytest.importorskip("xarray")
    pytest.importorskip("dask.array")
    from umbra_py import stack_stats, to_stack

    items = _spread_scenes(tmp_path)
    kwargs = {"max_size": 24, "crs": "utm"}
    # 24 / 3 blocks puts a block edge on column 8; a 7-wide window puts a window
    # edge on 7 and 14 -- so no block is a whole number of windows.
    whole = stack_stats(to_stack(items, **kwargs), blocks=3, block_series=True)
    windowed = stack_stats(
        to_stack(items, lazy=True, chunk_size=7, **kwargs),
        blocks=3,
        block_series=True,
        windowed=True,
    )

    assert _without_quantiles(windowed) == _without_quantiles(whole)
    # ...and the percentiles land about one histogram bin from the exact value
    # (a bin, plus wherever the neighbouring cells sit inside it).
    from umbra_py.load import _QUANTILE_BIN_DB

    for estimate, exact in zip(windowed["passes"], whole["passes"], strict=True):
        for key in ("median", "p5", "p95"):
            offset_db = 20 * math.log10(estimate[key] / exact[key])
            assert offset_db == pytest.approx(0.0, abs=2 * _QUANTILE_BIN_DB)


def test_stack_stats_windowed_matches_across_unobserved_ground_too(tmp_path):
    """A window edge is not a footprint edge: union padding still isn't change."""
    pytest.importorskip("xarray")
    pytest.importorskip("dask.array")
    from umbra_py import stack_stats, to_stack

    items = [
        _stack_item(_stack_scene(tmp_path / "a.tif", value=2.0), "a", "2024-01-01T00:00:00Z"),
        _stack_item(
            _stack_scene(tmp_path / "b.tif", x_offset=300.0, value=8.0),
            "b",
            "2024-02-01T00:00:00Z",
        ),
    ]
    kwargs = {"max_size": 32, "extent": "union"}
    whole = stack_stats(to_stack(items, **kwargs), blocks=4)
    windowed = stack_stats(
        to_stack(items, lazy=True, chunk_size=6, **kwargs), blocks=4, windowed=True
    )

    assert _without_quantiles(windowed) == _without_quantiles(whole)
    # The blocks only one pass covers stay gaps rather than becoming zeros.
    assert any(b["net_change"] is None for b in windowed["spatial"]["blocks"])


def test_stack_stats_windowed_reports_nothing_for_a_pass_with_no_valid_cell(tmp_path):
    """No observation is not a distribution of zero, however the cube was walked."""
    pytest.importorskip("xarray")
    pytest.importorskip("dask.array")
    from umbra_py import stack_stats, to_stack

    items = [
        # Zero amplitude is masked as "no observation", so this pass is all NaN.
        _stack_item(_stack_scene(tmp_path / "blank.tif", value=0.0), "a", "2024-01-01T00:00:00Z"),
        _stack_item(_stack_scene(tmp_path / "seen.tif", value=4.0), "b", "2024-02-01T00:00:00Z"),
    ]
    windowed = stack_stats(to_stack(items, max_size=24, lazy=True, chunk_size=8), windowed=True)

    blank = windowed["passes"][0]
    assert blank["valid_cells"] == 0
    assert all(blank[key] is None for key in ("mean", "median", "std", "p5", "p95"))
    assert windowed["net_change"] is None
    assert _without_quantiles(windowed) == _without_quantiles(
        stack_stats(to_stack(items, max_size=24))
    )


def test_stack_stats_windowed_holds_one_window_not_one_slice(tmp_path):
    """Peak memory follows the chunk size, not the grid."""
    pytest.importorskip("xarray")
    pytest.importorskip("dask.array")
    from umbra_py import load as load_mod
    from umbra_py import stack_stats

    shapes = []
    original = load_mod._pass_slabs

    def record(np, cube, index, **kw):
        slabs = original(np, cube, index, **kw)
        shapes.append(slabs[0].shape)
        return slabs

    cube = _dask_cube(_spread_scenes(tmp_path), max_size=24, chunk_size=8)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(load_mod, "_pass_slabs", record)
        stack_stats(cube, windowed=True)

    # Every window of every pass, and never anything bigger than a window --
    # the whole (height, 24) slice this walk replaced is never materialised.
    windows = load_mod._cube_windows(cube)
    assert len(shapes) == len(windows) * cube.shape[0]
    assert max(max(shape) for shape in shapes) == 8


def test_stack_stats_windowed_says_which_numbers_are_estimates(tmp_path):
    pytest.importorskip("xarray")
    pytest.importorskip("dask.array")
    from umbra_py import stack_stats, to_stack
    from umbra_py.load import _QUANTILE_BIN_DB

    items = _spread_scenes(tmp_path)
    windowed = stack_stats(to_stack(items, max_size=24, lazy=True, chunk_size=8), windowed=True)

    assert windowed["quantile_method"] == "histogram"
    assert windowed["quantile_bin_db"] == _QUANTILE_BIN_DB
    assert any("histogram estimates" in c for c in windowed["caveats"])
    # A summary whose numbers are all exact says nothing, and is byte-identical
    # to the one this mode did not exist for.
    exact = stack_stats(to_stack(items, max_size=24))
    assert "quantile_method" not in exact
    assert not any("histogram estimates" in c for c in exact["caveats"])


def test_stack_stats_windowed_on_an_unchunked_cube_is_one_window(tmp_path):
    """Nothing to stream: the walk degenerates to the whole-slice read."""
    pytest.importorskip("xarray")
    from umbra_py import stack_stats, to_stack

    cube = to_stack(_spread_scenes(tmp_path), max_size=24, crs="utm")
    windowed = stack_stats(cube, windowed=True, blocks=2)

    assert _without_quantiles(windowed) == _without_quantiles(stack_stats(cube, blocks=2))


def test_cli_stack_stats_windowed_measures_a_chunked_cube(tmp_path, monkeypatch):
    pytest.importorskip("xarray")
    pytest.importorskip("dask.array")
    import json

    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    paths = {
        f"acq-{n}": _speckle_scene(tmp_path / f"c{n}.tif", scale=2.0 ** (n - 1)) for n in (1, 2)
    }
    stac = {
        f"http://example.com/{name}.json": {
            "id": name,
            "properties": {"datetime": f"2024-0{n}-08T12:00:00Z"},
            "assets": {},
        }
        for n, name in enumerate(paths, start=1)
    }
    monkeypatch.setattr("umbra_py.cli._shared.get_json", lambda url: stac[url])
    monkeypatch.setattr(
        "umbra_py.cli._shared.UmbraItem.asset_href", lambda self, asset="GEC": str(paths[self.id])
    )

    result = CliRunner().invoke(
        cli_mod.cli,
        [
            "stack",
            "http://example.com/acq-1.json",
            "http://example.com/acq-2.json",
            "--stats-windowed",
            "--lazy",
            "--chunk-size",
            "8",
            "--max-size",
            "24",
        ],
    )

    assert result.exit_code == 0, result.output
    # --stats-windowed implies --stats, so the reduction is printed with no --out.
    summary = json.loads(result.stdout)
    assert summary["count"] == 2
    assert summary["quantile_method"] == "histogram"
    assert summary["passes"][1]["change_vs_previous"]["mean_delta_db"] == pytest.approx(
        _DOUBLING_DB, abs=0.01
    )


def test_cli_stack_chunk_size_writes_the_datacube(tmp_path, monkeypatch):
    pytest.importorskip("xarray")
    rasterio = pytest.importorskip("rasterio")
    pytest.importorskip("dask.array")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    urls = _stack_cli_env(tmp_path, monkeypatch)
    out = tmp_path / "cube.tif"

    result = CliRunner().invoke(
        cli_mod.cli,
        ["stack", *urls, "--out", str(out), "--lazy", "--chunk-size", "8", "--max-size", "16"],
    )

    assert result.exit_code == 0, result.output
    with rasterio.open(out) as ds:
        assert ds.count == 2
        assert ds.read([2])[0] == pytest.approx(8.0)

    # Without --lazy the window size would bound nothing, so it is refused.
    refused = CliRunner().invoke(
        cli_mod.cli,
        ["stack", *urls, "--out", str(out), "--chunk-size", "8", "--max-size", "16"],
    )
    assert refused.exit_code != 0
    assert "--chunk-size needs --lazy" in refused.output


def test_cli_stack_lazy_writes_the_datacube(tmp_path, monkeypatch):
    pytest.importorskip("xarray")
    rasterio = pytest.importorskip("rasterio")
    pytest.importorskip("dask.array")
    import json

    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    urls = _stack_cli_env(tmp_path, monkeypatch)
    out = tmp_path / "cube.tif"

    result = CliRunner().invoke(
        cli_mod.cli,
        ["stack", *urls, "--out", str(out), "--lazy", "--stats", "--max-size", "16", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["stats"]["count"] == 2
    with rasterio.open(out) as ds:
        assert ds.count == 2
        assert ds.read([2])[0] == pytest.approx(8.0)


# --- Conversion provenance on the measurement chain -------------------------
#
# ``umbra convert`` stamps UMBRA_* GeoTIFF tags saying what a pixel value is
# (calibration, RTC model, amplitude scale). These check the consuming half:
# a loaded array carries the record, a cube refuses to mix two of them, and a
# raster written back out keeps it.


def _tag_scene(path, **settings):
    """Stamp ``umbra convert``-style provenance onto an existing test scene."""
    rasterio = pytest.importorskip("rasterio")
    from umbra_py.convert import conversion_tags

    with rasterio.open(path, "r+") as ds:
        ds.update_tags(**conversion_tags(source=f"{path.name}.nitf", geocoded=True, **settings))
    return path


def _converted_scenes(tmp_path, *per_scene):
    """Same-footprint passes, each converted with its own settings dict."""
    return [
        _stack_item(
            _tag_scene(_stack_scene(tmp_path / f"c{n}.tif", value=2.0**n), **settings),
            f"acq-{n}",
            f"2024-0{n}-08T12:00:00Z",
        )
        for n, settings in enumerate(per_scene, start=1)
    ]


def test_to_xarray_surfaces_the_conversion_provenance(tmp_path):
    pytest.importorskip("xarray")
    from umbra_py import to_xarray

    plain, _, _ = _make_geotiff(tmp_path / "scene.tif")
    # A published Umbra product carries no such tags: absent, not empty, so the
    # key never reads as "nothing was done to this".
    assert "provenance" not in to_xarray(_item_for(plain)).attrs

    converted = _tag_scene(
        _stack_scene(tmp_path / "converted.tif"), calibration="gamma0", rtc_model="facet"
    )
    prov = to_xarray(_item_for(converted)).attrs["provenance"]
    assert prov["calibration"] == "gamma0"
    assert prov["rtc_model"] == "facet"
    assert prov["units"] == "dB (gamma0)"
    # Exactly what the file itself says, so the two answers cannot drift.
    from umbra_py import read_conversion_tags

    assert prov == read_conversion_tags(converted)


def test_to_stack_carries_a_provenance_its_sources_agree_on(tmp_path):
    pytest.importorskip("xarray")
    pytest.importorskip("numpy")
    from umbra_py import to_stack

    settings = {"calibration": "sigma0", "rtc_model": "gamma"}
    cube = to_stack(_converted_scenes(tmp_path, settings, settings), max_size=32)

    assert cube.attrs["provenance"]["calibration"] == "sigma0"
    assert cube.attrs["provenance"]["rtc_model"] == "gamma"


def test_to_stack_omits_provenance_for_untagged_products(tmp_path):
    pytest.importorskip("xarray")
    pytest.importorskip("numpy")
    from umbra_py import to_stack

    # The ordinary case -- every published GEC is untagged, and a series of them
    # agrees on that, so nothing is refused and nothing is claimed.
    cube = to_stack(_three_scenes(tmp_path), max_size=32)
    assert "provenance" not in cube.attrs


def test_to_stack_refuses_to_mix_two_calibrations(tmp_path):
    pytest.importorskip("xarray")
    pytest.importorskip("numpy")
    from umbra_py import to_stack

    items = _converted_scenes(tmp_path, {"calibration": "gamma0"}, {"calibration": "sigma0"})
    with pytest.raises(ValueError, match="calibration disagrees") as exc:
        to_stack(items, max_size=32)

    message = str(exc.value)
    # Both sides are named, with the acquisition standing for each.
    assert "'gamma0' (acq-1)" in message
    assert "'sigma0' (acq-2)" in message
    assert "umbra convert --provenance" in message


def test_to_stack_refuses_a_converted_raster_mixed_with_an_untagged_one(tmp_path):
    pytest.importorskip("xarray")
    pytest.importorskip("numpy")
    from umbra_py import to_stack

    calibrated = _converted_scenes(tmp_path, {"calibration": "gamma0"})
    raw = [_stack_item(_stack_scene(tmp_path / "raw.tif"), "acq-9", "2024-09-08T12:00:00Z")]

    with pytest.raises(ValueError, match="calibration disagrees") as exc:
        to_stack(calibrated + raw, max_size=32)
    assert "'(unrecorded)' (acq-9)" in str(exc.value)


def test_to_stack_refuses_to_mix_flattened_and_unflattened_passes(tmp_path):
    pytest.importorskip("xarray")
    pytest.importorskip("numpy")
    from umbra_py import to_stack

    items = _converted_scenes(tmp_path, {"rtc_model": "facet"}, {})
    with pytest.raises(ValueError, match="rtc_model disagrees"):
        to_stack(items, max_size=32)


def test_to_stack_allows_the_per_scene_keys_to_differ(tmp_path):
    pytest.importorskip("xarray")
    pytest.importorskip("numpy")
    from umbra_py import to_stack

    # Each pass resolves its own reference incidence angle and names its own
    # source file; neither says the pixel values mean different things.
    cube = to_stack(
        _converted_scenes(
            tmp_path,
            {"calibration": "gamma0", "rtc_model": "facet", "rtc_reference_deg": 31.0},
            {"calibration": "gamma0", "rtc_model": "facet", "rtc_reference_deg": 44.5},
        ),
        max_size=32,
    )

    prov = cube.attrs["provenance"]
    assert prov["calibration"] == "gamma0"
    # Only the keys every source agreed on survive into the cube's record.
    assert "rtc_reference_deg" not in prov
    assert "source" not in prov


def test_stack_stats_reports_the_provenance_and_drops_the_relative_caveat(tmp_path):
    pytest.importorskip("xarray")
    pytest.importorskip("numpy")
    from umbra_py import stack_stats, to_stack

    settings = {"calibration": "gamma0", "rtc_model": "facet"}
    stats = stack_stats(to_stack(_converted_scenes(tmp_path, settings, settings), max_size=32))

    assert stats["provenance"]["calibration"] == "gamma0"
    caveats = " ".join(stats["caveats"])
    assert "gamma0 radiometric calibration" in caveats
    assert "not radiometrically calibrated" not in caveats
    assert "facet model" in caveats


def test_stack_stats_keeps_the_relative_caveat_for_published_products(tmp_path):
    pytest.importorskip("xarray")
    pytest.importorskip("numpy")
    from umbra_py import stack_stats, to_stack

    stats = stack_stats(to_stack(_three_scenes(tmp_path), max_size=32))

    assert "provenance" not in stats
    assert "not radiometrically calibrated" in " ".join(stats["caveats"])


def test_written_rasters_carry_the_provenance_forward(tmp_path):
    pytest.importorskip("xarray")
    pytest.importorskip("numpy")
    from umbra_py import read_conversion_tags, stack_to_geotiff, to_geotiff

    settings = {"calibration": "beta0", "rtc_model": "cosine"}
    items = _converted_scenes(tmp_path, settings, settings)

    cube_tif = stack_to_geotiff(items, tmp_path / "cube.tif", max_size=32)
    assert read_conversion_tags(cube_tif)["calibration"] == "beta0"
    assert read_conversion_tags(cube_tif)["rtc_model"] == "cosine"

    scene_tif = to_geotiff(items[0], tmp_path / "scene_out.tif", max_size=32)
    assert read_conversion_tags(scene_tif)["calibration"] == "beta0"

    # A derivative of an untagged product stays untagged rather than claiming a
    # conversion that never happened.
    plain = to_geotiff(_three_scenes(tmp_path)[0], tmp_path / "plain.tif", max_size=32)
    assert read_conversion_tags(plain) == {}
