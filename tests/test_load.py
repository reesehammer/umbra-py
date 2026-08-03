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


def _without_estimates(summary):
    """A summary stripped of everything the two walks don't report identically.

    The percentiles, because ``windowed=True`` estimates them from a histogram;
    and ``looks`` (with the ``detection`` floor derived from it, and the caveat
    quoting both), because it is a median over 16-cell measuring blocks cut from
    whatever array is in hand -- so a window narrower than a block finds none
    where a whole slice finds several. Everything left is a count or a sum, and
    those are exact either way.
    """
    import json

    trimmed = json.loads(json.dumps(summary))
    for record in trimmed["passes"]:
        for key in ("median", "p5", "p95", "looks"):
            record.pop(key)
    trimmed.pop("quantile_method", None)
    trimmed.pop("quantile_bin_db", None)
    trimmed.pop("detection", None)
    trimmed["caveats"] = [
        c for c in trimmed["caveats"] if "window by window" not in c and "Speckle alone" not in c
    ]
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

    assert _without_estimates(windowed) == _without_estimates(whole)
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

    assert _without_estimates(windowed) == _without_estimates(whole)
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
    assert _without_estimates(windowed) == _without_estimates(
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

    assert _without_estimates(windowed) == _without_estimates(stack_stats(cube, blocks=2))


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


def _tag_scene_omitting(path, tag, **settings):
    """Stamp provenance with one tag left out -- an older umbra-py's record."""
    rasterio = pytest.importorskip("rasterio")
    from umbra_py.convert import conversion_tags

    tags = conversion_tags(source=f"{path.name}.nitf", geocoded=True, **settings)
    del tags[tag]
    with rasterio.open(path, "r+") as ds:
        ds.update_tags(**tags)
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


def test_to_stack_refuses_to_mix_a_noise_subtracted_pass_with_a_raw_one(tmp_path):
    pytest.importorskip("xarray")
    pytest.importorskip("numpy")
    from umbra_py import to_stack

    # Subtracting the sensor's own floor from one pass and not the next puts
    # that floor on the time axis: over a dark cell it is most of the value, so
    # the "change" would be the correction rather than the ground.
    settings = {"calibration": "sigma0"}
    items = _converted_scenes(
        tmp_path,
        {**settings, "noise_subtraction": "absolute"},
        settings,
    )
    with pytest.raises(ValueError, match="noise_subtraction disagrees") as exc:
        to_stack(items, max_size=32)
    assert "'absolute' (acq-1)" in str(exc.value)
    assert "'none' (acq-2)" in str(exc.value)


def test_to_stack_reads_a_record_that_predates_a_step_as_not_having_run_it(tmp_path):
    pytest.importorskip("xarray")
    pytest.importorskip("numpy")
    from umbra_py import to_stack

    # A raster converted by an older umbra-py has no tag for a step that did not
    # exist yet -- and did not run it either. Reading that as "none" rather than
    # as "(unrecorded)" is what keeps a new key from retroactively splitting a
    # series that agrees; the sentinel stays for a raster with no record at all.
    older = _stack_item(
        _tag_scene_omitting(
            _stack_scene(tmp_path / "older.tif", value=2.0),
            "UMBRA_NOISE_SUBTRACTION",
            calibration="sigma0",
        ),
        "acq-old",
        "2024-01-08T12:00:00Z",
    )
    newer = _converted_scenes(tmp_path, {"calibration": "sigma0"})

    cube = to_stack([older, *newer], max_size=32)
    assert cube.attrs["provenance"]["calibration"] == "sigma0"
    # The key only survives into the cube's record when every source carries it.
    assert "noise_subtraction" not in cube.attrs["provenance"]


def test_stack_stats_says_when_the_noise_floor_was_subtracted(tmp_path):
    pytest.importorskip("xarray")
    pytest.importorskip("numpy")
    from umbra_py import stack_stats, to_stack

    settings = {"calibration": "sigma0", "noise_subtraction": "absolute"}
    stats = stack_stats(to_stack(_converted_scenes(tmp_path, settings, settings), max_size=32))
    assert "thermal-noise floor was subtracted" in " ".join(stats["caveats"])

    # And says nothing when it wasn't: the default summary is unchanged.
    plain = stack_stats(to_stack(_three_scenes(tmp_path), max_size=32))
    assert "thermal-noise floor" not in " ".join(plain["caveats"])
    # A measured floor is quoted without the estimator's caveats attached.
    assert "estimated from each scene" not in " ".join(stats["caveats"])


def test_to_stack_refuses_an_inferred_floor_differenced_against_a_measured_one(tmp_path):
    pytest.importorskip("xarray")
    pytest.importorskip("numpy")
    from umbra_py import to_stack

    # Both passes had *a* floor taken off, so the coarse "was noise subtracted?"
    # question agrees -- but one number was read from the product and the other
    # inferred from the image, and the gap between them lands on the time axis.
    settings = {"calibration": "sigma0"}
    items = _converted_scenes(
        tmp_path,
        {**settings, "noise_subtraction": "absolute"},
        {**settings, "noise_subtraction": "estimated"},
    )
    with pytest.raises(ValueError, match="noise_subtraction disagrees") as exc:
        to_stack(items, max_size=32)
    assert "'absolute' (acq-1)" in str(exc.value)
    assert "'estimated' (acq-2)" in str(exc.value)


def test_to_stack_refuses_a_fitted_profile_differenced_against_a_constant_guess(tmp_path):
    pytest.importorskip("xarray")
    pytest.importorskip("numpy")
    from umbra_py import to_stack

    # Both floors were inferred from the pixels rather than read, so "was it
    # estimated?" agrees -- but one is a scalar and the other follows the swath,
    # and over a dark cell near the edge of a scene that difference is most of
    # the value. It is a third provenance value for exactly this reason.
    settings = {"calibration": "sigma0"}
    items = _converted_scenes(
        tmp_path,
        {**settings, "noise_subtraction": "estimated"},
        {**settings, "noise_subtraction": "estimated-range"},
    )
    with pytest.raises(ValueError, match="noise_subtraction disagrees") as exc:
        to_stack(items, max_size=32)
    assert "'estimated' (acq-1)" in str(exc.value)
    assert "'estimated-range' (acq-2)" in str(exc.value)


def test_stack_stats_says_a_fitted_floor_followed_the_swath_but_was_still_inferred(tmp_path):
    pytest.importorskip("xarray")
    pytest.importorskip("numpy")
    from umbra_py import stack_stats, to_stack

    settings = {"calibration": "sigma0", "noise_subtraction": "estimated-range"}
    stats = stack_stats(to_stack(_converted_scenes(tmp_path, settings, settings), max_size=32))
    caveats = " ".join(stats["caveats"])
    assert "thermal-noise floor was subtracted" in caveats
    # The constant model's first limit is gone and its second is not, so reusing
    # that model's wording would have understated one and overstated the other.
    assert "fitted across range so it follows the swath" in caveats
    assert "it cannot follow the across-swath variation" not in caveats


def test_stack_stats_says_when_the_subtracted_floor_was_only_estimated(tmp_path):
    pytest.importorskip("xarray")
    pytest.importorskip("numpy")
    from umbra_py import stack_stats, to_stack

    settings = {"calibration": "sigma0", "noise_subtraction": "estimated"}
    stats = stack_stats(to_stack(_converted_scenes(tmp_path, settings, settings), max_size=32))
    caveats = " ".join(stats["caveats"])
    # Both: the floor came off *and* the number that came off was inferred. A
    # summary that said only the first would let an estimate be quoted with a
    # measurement's confidence.
    assert "thermal-noise floor was subtracted" in caveats
    assert "estimated from each scene's own darkest pixels" in caveats


def test_to_stack_refuses_a_speckle_filtered_pass_against_an_unfiltered_one(tmp_path):
    pytest.importorskip("xarray")
    pytest.importorskip("numpy")
    from umbra_py import to_stack

    # A filtered pass reports the average of the ground its window covered, so
    # the difference against an unfiltered pass is the smoothing -- strongest
    # exactly where the ground has the most structure, which is where a change
    # measurement is being read.
    settings = {"calibration": "sigma0"}
    items = _converted_scenes(
        tmp_path,
        {**settings, "speckle_filter": "lee", "speckle_window": 5},
        settings,
    )
    with pytest.raises(ValueError, match="speckle_filter disagrees") as exc:
        to_stack(items, max_size=32)
    assert "'lee' (acq-1)" in str(exc.value)
    assert "'none' (acq-2)" in str(exc.value)


def test_to_stack_refuses_two_speckle_windows(tmp_path):
    pytest.importorskip("xarray")
    pytest.importorskip("numpy")
    from umbra_py import to_stack

    # Same filter, different window: both passes are smoothed, so "was it
    # filtered?" agrees -- but they resolve different ground, and the difference
    # between a 3-pixel and a 9-pixel average is not change.
    settings = {"calibration": "sigma0", "speckle_filter": "boxcar"}
    items = _converted_scenes(
        tmp_path,
        {**settings, "speckle_window": 3},
        {**settings, "speckle_window": 9},
    )
    with pytest.raises(ValueError, match="speckle_window disagrees") as exc:
        to_stack(items, max_size=32)
    assert "'3' (acq-1)" in str(exc.value)
    assert "'9' (acq-2)" in str(exc.value)


def test_to_stack_allows_the_speckle_diagnostics_to_differ(tmp_path):
    pytest.importorskip("xarray")
    pytest.importorskip("numpy")
    from umbra_py import to_stack

    # What each scene's own ENL was, and the looks a Lee filter read off it, are
    # measurements *of that scene*: no two real passes agree on them, so refusing
    # over them would end every series.
    settings = {"calibration": "gamma0", "speckle_filter": "lee", "speckle_window": 5}
    cube = to_stack(
        _converted_scenes(
            tmp_path,
            {
                **settings,
                "speckle_enl_before": 1.1,
                "speckle_enl_after": 18.0,
                "speckle_looks": 1.1,
            },
            {
                **settings,
                "speckle_enl_before": 0.9,
                "speckle_enl_after": 21.0,
                "speckle_looks": 1.0,
            },
        ),
        max_size=32,
    )
    prov = cube.attrs["provenance"]
    assert prov["speckle_filter"] == "lee"
    assert prov["speckle_window"] == "5"
    # Carried only where they agree, so a diagnostic that differs is dropped
    # rather than attributed to the cube.
    assert "speckle_enl_after" not in prov


def test_stack_stats_says_the_series_was_speckle_filtered(tmp_path):
    pytest.importorskip("xarray")
    pytest.importorskip("numpy")
    from umbra_py import stack_stats, to_stack

    settings = {"calibration": "gamma0", "speckle_filter": "boxcar", "speckle_window": 7}
    stats = stack_stats(to_stack(_converted_scenes(tmp_path, settings, settings), max_size=32))
    caveats = " ".join(stats["caveats"])
    # Both halves of the trade, because which one matters depends on the block
    # size a reader is quoting: better estimates, coarser measurements.
    assert "speckle-filtered (boxcar, 7x7 window" in caveats
    assert "less noisy estimate" in caveats
    assert "resolution is that of a 7-wide window" in caveats


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


def test_to_stack_does_not_refuse_over_the_subtractions_own_diagnostics(tmp_path):
    """How much a pass was floored is not what its pixel values mean."""
    pytest.importorskip("xarray")
    pytest.importorskip("numpy")
    from umbra_py import to_stack

    # Two passes of one site, converted identically -- and each carrying its own
    # measured floored fraction and margin, because those describe the scene the
    # correction met rather than the correction. A refusal here would make the
    # diagnostics unusable: no two real passes agree on them, so recording them
    # would have ended every series.
    settings = {"calibration": "sigma0", "noise_subtraction": "estimated"}
    items = _converted_scenes(
        tmp_path,
        {**settings, "noise_floored_fraction": 0.02, "noise_floor_margin_db": 14.0},
        {**settings, "noise_floored_fraction": 0.31, "noise_floor_margin_db": 7.5},
    )
    cube = to_stack(items, max_size=32)

    prov = cube.attrs["provenance"]
    assert prov["noise_subtraction"] == "estimated"
    # Carried only where the sources agree, and here they do not -- so the cube
    # says nothing rather than quoting one pass's number for the whole series.
    assert "noise_floored_fraction" not in prov
    assert "noise_floor_margin_db" not in prov


# --- Filtering speckle on the cube's own grid (``to_stack(speckle_filter=...)``)
#
# The conversion pipeline filters in the radar's image space, so it reaches only
# the complex archive. These pin the same averaging one step down the chain,
# where it reaches the published GEC rasters: what it removes, what it costs,
# what it records, and the two things it refuses to do.


def _edge_scene(path, *, dark=1.0, bright=10.0, width=40, height=40):
    """A scene split down the middle, with multiplicative speckle on both halves.

    The fixture a filter comparison needs: a step no averaging should blur *and*
    a grain every averaging should remove, so "smoothed the field" and "smoothed
    the edge" are separable outcomes rather than one number.
    """
    rasterio = pytest.importorskip("rasterio")
    np = pytest.importorskip("numpy")
    from rasterio.transform import from_origin

    rng = np.random.default_rng(11)
    surface = np.where(np.arange(width) < width // 2, dark, bright)
    values = np.broadcast_to(surface, (height, width)) * rng.gamma(4.0, 0.25, (height, width))
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=height,
        width=width,
        count=1,
        dtype="float32",
        crs="EPSG:32633",
        transform=from_origin(500000.0, 4000000.0, 10.0, 10.0),
    ) as dst:
        dst.write(values.astype("float32"), 1)
    return path


def _edge_scenes(tmp_path, count=2):
    """``count`` passes of the same split scene, one per month."""
    return [
        _stack_item(_edge_scene(tmp_path / f"e{n}.tif"), f"acq-{n}", f"2024-0{n}-08T12:00:00Z")
        for n in range(1, count + 1)
    ]


def test_to_stack_speckle_filter_averages_each_pass_down(tmp_path):
    """The point of the filter: less scatter per cell, same surface underneath."""
    pytest.importorskip("xarray")
    np = pytest.importorskip("numpy")
    from umbra_py import to_stack

    items = _spread_scenes(tmp_path, count=2)
    kwargs = {"max_size": 40, "crs": "utm"}
    plain = to_stack(items, **kwargs)
    filtered = to_stack(items, speckle_filter="boxcar", speckle_window=5, **kwargs)

    assert filtered.shape == plain.shape
    for i in range(plain.shape[0]):
        assert np.nanstd(filtered.values[i]) < 0.5 * np.nanstd(plain.values[i])
        # Averaging happens in power, so the mean of the power is preserved --
        # which is the whole reason the filter does not work in decibels, where
        # the same average would be a geometric mean and read systematically low.
        assert np.nanmean(np.square(filtered.values[i])) == pytest.approx(
            np.nanmean(np.square(plain.values[i])), rel=0.05
        )


def test_to_stack_lee_keeps_the_edge_boxcar_averages_across(tmp_path):
    """The difference between the two filters, measured rather than described."""
    pytest.importorskip("xarray")
    np = pytest.importorskip("numpy")
    from umbra_py import to_stack

    items = _edge_scenes(tmp_path)
    kwargs = {"max_size": 40, "crs": "utm", "speckle_window": 5}
    boxcar = to_stack(items, speckle_filter="boxcar", **kwargs).values[0]
    lee = to_stack(items, speckle_filter="lee", **kwargs).values[0]

    # The step is halfway across, so column edge-1 is dark ground and column
    # edge is bright ground. A boxcar window straddling them averages the two
    # together; Lee sees a window more variable than speckle alone explains and
    # leaves those cells nearer their own side.
    edge = boxcar.shape[1] // 2
    boxcar_step = np.nanmean(boxcar[:, edge]) - np.nanmean(boxcar[:, edge - 1])
    lee_step = np.nanmean(lee[:, edge]) - np.nanmean(lee[:, edge - 1])
    # Lee holds about 1.9x of the step boxcar keeps here; the factor depends on
    # the contrast and the window, so the assertion is the direction with room
    # rather than the measured number.
    assert lee_step > 1.5 * boxcar_step

    # Away from the edge both are doing the job they exist for.
    interior = np.s_[:, 2 : edge - 3]
    plain = to_stack(items, max_size=40, crs="utm").values[0]
    assert np.nanstd(lee[interior]) < np.nanstd(plain[interior])
    assert np.nanstd(boxcar[interior]) < np.nanstd(plain[interior])


def test_to_stack_speckle_filter_records_itself_on_untagged_products(tmp_path):
    """A published GEC carries no provenance; a cube that filtered one now does."""
    pytest.importorskip("xarray")
    pytest.importorskip("numpy")
    from umbra_py import stack_stats, to_stack

    cube = to_stack(_spread_scenes(tmp_path, count=2), max_size=32, speckle_filter="lee")

    # The keys `umbra convert` writes, because they describe the raster rather
    # than who made it -- which is what keeps the refusal below working.
    assert cube.attrs["provenance"] == {"speckle_filter": "lee", "speckle_window": "5"}
    caveats = " ".join(stack_stats(cube)["caveats"])
    assert "speckle-filtered (lee, 5x5 window)" in caveats
    assert "resolution is that of a 5-wide window" in caveats
    # Still Umbra's published products otherwise: filtering says nothing about
    # calibration, so the relative-decibels caveat stands.
    assert "not radiometrically calibrated" in caveats


def test_stack_to_geotiff_writes_the_filter_it_applied(tmp_path):
    """The written file says what its values are, so re-reading it is checked."""
    pytest.importorskip("xarray")
    pytest.importorskip("numpy")
    from umbra_py import read_conversion_tags, stack_to_geotiff, to_stack

    dest = stack_to_geotiff(
        _spread_scenes(tmp_path, count=2),
        tmp_path / "filtered.tif",
        max_size=32,
        speckle_filter="boxcar",
        speckle_window=7,
    )
    tags = read_conversion_tags(dest)
    assert tags["speckle_filter"] == "boxcar"
    assert tags["speckle_window"] == "7"

    # And the record is consumed, not just written: that raster stacked against
    # an unfiltered product is the smoothing differenced against the ground. The
    # key the refusal names is the *first* the two disagree on, which here is the
    # earliest one in MEASUREMENT_PROVENANCE_KEYS -- a raster umbra-py wrote
    # carries a record and a published GEC carries none, so they part company
    # before the filter is reached.
    written = _stack_item(dest, "acq-filtered", "2024-05-08T12:00:00Z")
    with pytest.raises(ValueError, match="Refusing to stack rasters whose") as exc:
        to_stack([written, *_spread_scenes(tmp_path, count=1)], max_size=32)
    assert "(unrecorded)" in str(exc.value)


def test_to_stack_speckle_filter_matches_between_the_eager_and_lazy_paths(tmp_path):
    """Deferring a pass's read must not change what the filter did to it."""
    pytest.importorskip("xarray")
    np = pytest.importorskip("numpy")
    pytest.importorskip("dask.array")
    from umbra_py import to_stack

    items = _spread_scenes(tmp_path, count=2)
    kwargs = {"max_size": 32, "crs": "utm", "speckle_filter": "lee"}
    eager = to_stack(items, **kwargs)
    lazy = to_stack(items, lazy=True, **kwargs)

    assert np.array_equal(eager.values, lazy.compute().values, equal_nan=True)


def test_to_stack_refuses_to_filter_an_already_filtered_series(tmp_path):
    """Two averagings leave a resolution neither window names."""
    pytest.importorskip("xarray")
    pytest.importorskip("numpy")
    from umbra_py import to_stack

    settings = {"calibration": "sigma0", "speckle_filter": "boxcar", "speckle_window": 3}
    items = _converted_scenes(tmp_path, settings, settings)

    with pytest.raises(ValueError, match="already filtered") as exc:
        to_stack(items, max_size=32, speckle_filter="lee", speckle_window=5)
    assert "boxcar, 3x3 window" in str(exc.value)
    # Without the request the sources' own filter is carried, as before.
    assert to_stack(items, max_size=32).attrs["provenance"]["speckle_filter"] == "boxcar"


@pytest.mark.parametrize("name", ["boxcar", "lee"])
def test_to_stack_speckle_filter_on_a_chunked_cube_matches_the_unchunked_one(tmp_path, name):
    """The halo claim, which is what lets a cube be filtered window by window.

    A filter window centred near a chunk edge needs cells the neighbouring chunk
    holds. Read the chunk alone and those cells are missing, so its edge averages
    a truncated window and two neighbouring windows disagree about the ground
    they share -- a seam, right where a change measurement would read it as
    change. Reading each window with a half-window halo and cropping after the
    filter makes it the whole-pass filter's own answer, which is what this
    asserts, on a grid whose last window is a partial one.

    "Own answer" to within one ``float32`` ulp rather than bit-for-bit: the
    filters sum a window out of a summed-area table, and a table accumulated over
    a 12-cell read reaches a given window total by a different order of additions
    than one accumulated over the pass. That moves the last bit of a few cells
    and nothing else -- the windows themselves are the same cells.
    """
    pytest.importorskip("xarray")
    np = pytest.importorskip("numpy")

    items = _spread_scenes(tmp_path, count=2)
    kwargs = {"max_size": 24, "crs": "utm", "speckle_filter": name, "speckle_window": 5}
    whole = _dask_cube(items, **kwargs)
    # 24 is not a multiple of 10, so the last window of each row is partial and
    # its halo is clamped at the pass edge exactly as the whole-pass filter is.
    windowed = _dask_cube(items, chunk_size=10, **kwargs)

    assert windowed.chunks[2] == (10, 10, 4)
    assert windowed.attrs == whole.attrs
    np.testing.assert_allclose(
        np.asarray(windowed.compute().values),
        np.asarray(whole.compute().values),
        rtol=float(np.finfo("float32").eps),
        atol=1e-5,
    )


def test_to_stack_chunked_lee_reads_its_speckle_parameter_once_per_pass(tmp_path):
    """A window's variability is judged against the *pass*, not against itself.

    ``lee`` compares each window against what speckle alone would explain, and
    that is a property of the product's processing. Read per chunk it would
    differ across one pass, so half a scene would be smoothed harder than the
    half beside it.
    """
    pytest.importorskip("xarray")
    pytest.importorskip("numpy")
    from umbra_py import load as load_mod

    calls = []
    original = load_mod._pass_looks

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            load_mod,
            "_pass_looks",
            lambda url, grid, **kw: (calls.append(url), original(url, grid, **kw))[1],
        )
        cube = _dask_cube(
            _spread_scenes(tmp_path, count=2),
            max_size=24,
            chunk_size=10,
            speckle_filter="lee",
        )
        assert calls == []  # deferred like every other read
        cube.compute()

    # Nine windows per pass, two passes -- and one looks read for each pass.
    assert len(calls) == 2
    assert len(set(calls)) == 2

    # ``boxcar`` needs no such parameter, so it costs no such read.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(load_mod, "_pass_looks", lambda *a, **kw: pytest.fail("boxcar read looks"))
        _dask_cube(
            _spread_scenes(tmp_path, count=2),
            max_size=24,
            chunk_size=10,
            speckle_filter="boxcar",
        ).compute()


def test_the_looks_sample_grid_spreads_and_collapses():
    """A pass wider than one sample window is read at several places; one no
    wider is read once, whole -- which is why a small cube's ``lee`` parameter is
    exactly the one the unchunked path reads off the slab."""
    from umbra_py.load import _sample_starts

    assert _sample_starts(400, 512, 3) == [0]  # narrower than a window: read whole
    assert _sample_starts(512, 512, 3) == [0]  # exactly one window: still once
    assert _sample_starts(1024, 512, 3) == [0, 256, 512]
    # De-duplicated where the spread would repeat an origin.
    assert _sample_starts(514, 512, 3) == [0, 1, 2]
    assert _sample_starts(2048, 512, 1) == [0]


def test_to_stack_chunked_filter_reads_the_halo_and_returns_the_window(tmp_path):
    """The read is grown by half a window; what lands in the cube is not."""
    pytest.importorskip("xarray")
    pytest.importorskip("numpy")
    from umbra_py import load as load_mod

    shapes = []
    original = load_mod._open_slab

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            load_mod,
            "_open_slab",
            lambda url, grid, **kw: (
                shapes.append(((grid.width, grid.height), kw.get("crop"))),
                original(url, grid, **kw),
            )[1],
        )
        cube = _dask_cube(
            _spread_scenes(tmp_path, count=2),
            max_size=24,
            chunk_size=10,
            speckle_filter="boxcar",
            speckle_window=5,
        )
        block = cube.data.blocks[0, 0, 0].compute()

    assert block.shape[1:] == (10, 10)
    # The top-left window has no ground above or left of it, so its halo grows on
    # two sides only: 12 cells read, the 10 asked for cropped out of them.
    assert shapes == [((12, 12), (0, 10, 0, 10))]


def test_to_stack_checks_the_filter_at_the_call_not_at_compute(tmp_path):
    """A misspelt filter or an even window fails where it was asked for."""
    pytest.importorskip("xarray")
    pytest.importorskip("numpy")
    from umbra_py import to_stack

    items = _spread_scenes(tmp_path, count=2)
    with pytest.raises(ValueError, match="Unknown speckle_filter 'median'"):
        to_stack(items, max_size=32, speckle_filter="median")
    with pytest.raises(ValueError, match="speckle_window must be an odd integer"):
        to_stack(items, max_size=32, speckle_filter="boxcar", speckle_window=4)


def test_cli_stack_speckle_filter_reaches_the_cube_and_the_manifest(tmp_path, monkeypatch):
    pytest.importorskip("xarray")
    rasterio = pytest.importorskip("rasterio")
    np = pytest.importorskip("numpy")
    import json

    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    paths = {
        "one": _speckle_scene(tmp_path / "one.tif", scale=1.0),
        "two": _speckle_scene(tmp_path / "two.tif", scale=2.0),
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
            "32",
            "--speckle-filter",
            "boxcar",
            "--speckle-window",
            "5",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    manifest = json.loads(result.output)
    assert manifest["parameters"]["speckle_filter"] == "boxcar"
    assert manifest["parameters"]["speckle_window"] == 5
    with rasterio.open(out) as ds:
        assert ds.tags()["UMBRA_SPECKLE_FILTER"] == "boxcar"
        assert ds.tags()["UMBRA_SPECKLE_WINDOW"] == "5"
        assert np.nanstd(ds.read(1)) > 0


# --- The provenance preflight (`stack_provenance` / `umbra stack --provenance`)
#
# The refusal above is correct and was only ever discoverable by hitting it, and
# its advice ("use only the acquisitions that share one") named a subset it could
# not identify. These check that asking first gives the same verdict, and says
# which acquisitions those are.


def _linked(item, href):
    """The same test item, carrying the item-JSON URL `umbra stack` takes."""
    item.href = href
    return item


def test_stack_provenance_reports_a_series_that_agrees(tmp_path):
    pytest.importorskip("rasterio")
    from umbra_py import stack_provenance

    settings = {"calibration": "sigma0", "rtc_model": "gamma"}
    report = stack_provenance(_converted_scenes(tmp_path, settings, settings))

    assert report.agrees
    assert report.refusal is None
    assert len(report.groups) == 1
    assert report.groups[0].item_ids == ("acq-1", "acq-2")
    # Verbatim what to_stack would carry, so the two answers cannot drift.
    assert report.shared["calibration"] == "sigma0"
    assert report.shared["rtc_model"] == "gamma"


def test_stack_provenance_agrees_on_a_series_of_published_products(tmp_path):
    pytest.importorskip("rasterio")
    from umbra_py import stack_provenance

    # The ordinary case: untagged GECs agree on being untagged, so one group,
    # no refusal, and nothing claimed about what their pixels are.
    report = stack_provenance(_three_scenes(tmp_path))
    assert report.agrees
    assert len(report.groups) == 1
    assert report.shared == {}
    assert report.groups[0].record["calibration"] == "(unrecorded)"


def test_stack_provenance_groups_a_mixed_selection_and_names_the_largest(tmp_path):
    pytest.importorskip("rasterio")
    from umbra_py import stack_provenance

    gamma = {"calibration": "gamma0"}
    items = _converted_scenes(tmp_path, gamma, {"calibration": "sigma0"}, gamma)
    report = stack_provenance(items)

    assert not report.agrees
    assert [len(g.item_ids) for g in report.groups] == [2, 1]
    assert report.largest.item_ids == ("acq-1", "acq-3")
    assert report.largest.record["calibration"] == "gamma0"
    # Nothing is carried from a selection that disagrees.
    assert report.shared == {}


def test_stack_provenance_gives_the_refusal_the_stack_itself_would(tmp_path):
    pytest.importorskip("xarray")
    pytest.importorskip("numpy")
    from umbra_py import stack_provenance, to_stack

    items = _converted_scenes(tmp_path, {"calibration": "gamma0"}, {"calibration": "sigma0"})
    report = stack_provenance(items)

    with pytest.raises(ValueError) as exc:
        to_stack(items, max_size=32)
    # The same function produced both, which is what stops a preflight becoming
    # a second opinion about what stacks.
    assert report.refusal == str(exc.value)


def test_stack_provenance_clears_the_subset_it_named(tmp_path):
    pytest.importorskip("xarray")
    pytest.importorskip("numpy")
    from umbra_py import stack_provenance, to_stack

    gamma = {"calibration": "gamma0"}
    items = _converted_scenes(tmp_path, gamma, {"calibration": "sigma0"}, gamma)
    report = stack_provenance(items)

    keep = {i.id: i for i in items}
    subset = [keep[item_id] for item_id in report.largest.item_ids]
    # The advice is actionable rather than abstract: stacking what it named works.
    cube = to_stack(subset, max_size=32)
    assert cube.attrs["provenance"]["calibration"] == "gamma0"


def test_stack_provenance_splits_on_every_measurement_key(tmp_path):
    pytest.importorskip("rasterio")
    from umbra_py import stack_provenance

    # Two passes calibrated identically but filtered differently: the smoothing
    # would read as change, so they are two conversions, not one.
    base = {"calibration": "sigma0"}
    report = stack_provenance(
        _converted_scenes(
            tmp_path,
            {**base, "speckle_filter": "lee", "speckle_window": 5},
            base,
        )
    )
    assert not report.agrees
    assert "speckle_filter disagrees" in report.refusal
    assert len(report.groups) == 2


def test_stack_provenance_carries_the_urls_to_re_run_on(tmp_path):
    pytest.importorskip("rasterio")
    from umbra_py import stack_provenance

    gamma = {"calibration": "gamma0"}
    items = _converted_scenes(tmp_path, gamma, {"calibration": "sigma0"}, gamma)
    for item in items:
        _linked(item, f"https://example.com/{item.id}.json")

    report = stack_provenance(items)
    assert report.largest.hrefs == (
        "https://example.com/acq-1.json",
        "https://example.com/acq-3.json",
    )


def test_stack_provenance_leaves_an_unreadable_source_undecided(tmp_path):
    pytest.importorskip("rasterio")
    from umbra_py import stack_provenance

    settings = {"calibration": "sigma0"}
    items = _converted_scenes(tmp_path, settings, settings)
    items.append(_stack_item(tmp_path / "never-written.tif", "acq-9", "2024-09-08T12:00:00Z"))

    report = stack_provenance(items)
    # A failed read is not a product saying its pixels are something else, so it
    # does not make the series mixed -- it makes the answer incomplete.
    assert report.agrees
    assert [g.item_ids for g in report.groups] == [("acq-1", "acq-2")]
    assert [u.item_id for u in report.unreadable] == ["acq-9"]
    assert "never-written.tif" in report.unreadable[0].error


def test_stack_provenance_reports_an_item_with_no_such_asset(tmp_path):
    pytest.importorskip("rasterio")
    from umbra_py import stack_provenance

    settings = {"calibration": "sigma0"}
    items = _converted_scenes(tmp_path, settings, settings)
    # An item that lists no GEC at all, and one that lists it with an href
    # nothing can resolve: two ways to have no product, one place to report it.
    items.append(UmbraItem(id="acq-8", properties={"datetime": "2024-08-08T12:00:00Z"}))
    items.append(
        UmbraItem(
            id="acq-9",
            properties={"datetime": "2024-09-08T12:00:00Z"},
            assets={"GEC": {"href": ""}},
        )
    )

    report = stack_provenance(items)
    assert report.agrees
    assert [u.item_id for u in report.unreadable] == ["acq-8", "acq-9"]
    assert all(u.href is None for u in report.unreadable)
    assert "no asset 'GEC'" in report.unreadable[0].error
    assert "no resolvable URL" in report.unreadable[1].error


def test_stack_provenance_reads_no_pixels(tmp_path, monkeypatch):
    pytest.importorskip("rasterio")
    import rasterio

    from umbra_py import stack_provenance

    reads = []
    original = rasterio.DatasetReader.read

    def _spy(self, *args, **kwargs):
        reads.append(self.name)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(rasterio.DatasetReader, "read", _spy)
    settings = {"calibration": "sigma0"}
    stack_provenance(_converted_scenes(tmp_path, settings, settings))

    # The whole claim: it costs the headers a stack pays for anyway and saves
    # the grid, the warp and every pixel after them.
    assert reads == []


def test_stack_provenance_refuses_a_selection_that_is_not_a_time_series(tmp_path):
    pytest.importorskip("rasterio")
    from umbra_py import stack_provenance

    with pytest.raises(ValueError, match="needs at least one acquisition"):
        stack_provenance([])

    undated = UmbraItem(id="acq-0", properties={})
    with pytest.raises(ValueError, match="no datetime"):
        stack_provenance([undated])


def test_stack_provenance_to_dict_is_json_safe(tmp_path):
    pytest.importorskip("rasterio")
    import json

    from umbra_py import stack_provenance

    items = _converted_scenes(tmp_path, {"calibration": "gamma0"}, {"calibration": "sigma0"})
    payload = json.loads(json.dumps(stack_provenance(items).to_dict()))

    assert payload["asset"] == "GEC"
    assert payload["agrees"] is False
    assert [g["count"] for g in payload["groups"]] == [1, 1]
    assert "calibration disagrees" in payload["refusal"]
    # Nothing is claimed to be shared by a selection that disagrees.
    assert "shared" not in payload


def _stack_cli_stubs(monkeypatch, paths):
    """Serve `umbra stack` two STAC item URLs backed by local rasters."""
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
    return list(stac)


def test_cli_stack_provenance_reports_a_mix_before_stacking(tmp_path, monkeypatch):
    pytest.importorskip("rasterio")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    paths = {
        "one": _tag_scene(_stack_scene(tmp_path / "one.tif"), calibration="gamma0"),
        "two": _tag_scene(_stack_scene(tmp_path / "two.tif"), calibration="sigma0"),
    }
    urls = _stack_cli_stubs(monkeypatch, paths)

    result = CliRunner().invoke(cli_mod.cli, ["stack", *urls, "--provenance"])
    assert result.exit_code == 0, result.output
    assert "2 conversions" in result.output
    assert "calibration=gamma0" in result.output
    assert "calibration=sigma0" in result.output
    # The subset is a command, not a diagnosis.
    assert "Re-run on those alone:" in result.output
    assert "umbra stack 'http://example.com/one.json'" in result.output


def test_cli_stack_provenance_says_a_series_agrees(tmp_path, monkeypatch):
    pytest.importorskip("rasterio")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    paths = {
        "one": _stack_scene(tmp_path / "one.tif"),
        "two": _stack_scene(tmp_path / "two.tif"),
    }
    urls = _stack_cli_stubs(monkeypatch, paths)

    result = CliRunner().invoke(cli_mod.cli, ["stack", *urls, "--provenance"])
    assert result.exit_code == 0, result.output
    assert "2 acquisition(s) agree" in result.output
    # The common case reads as what it is rather than as seven "none"s.
    assert "no umbra-py conversion" in result.output


def test_cli_stack_provenance_json(tmp_path, monkeypatch):
    pytest.importorskip("rasterio")
    import json

    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    paths = {
        "one": _tag_scene(_stack_scene(tmp_path / "one.tif"), calibration="gamma0"),
        "two": _tag_scene(_stack_scene(tmp_path / "two.tif"), calibration="gamma0"),
    }
    urls = _stack_cli_stubs(monkeypatch, paths)

    result = CliRunner().invoke(cli_mod.cli, ["stack", *urls, "--provenance", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["agrees"] is True
    assert payload["shared"]["calibration"] == "gamma0"
    assert payload["groups"][0]["hrefs"] == urls


def test_cli_stack_provenance_is_asked_instead_of_the_work_not_beside_it(tmp_path, monkeypatch):
    pytest.importorskip("rasterio")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    paths = {
        "one": _stack_scene(tmp_path / "one.tif"),
        "two": _stack_scene(tmp_path / "two.tif"),
    }
    urls = _stack_cli_stubs(monkeypatch, paths)

    refused = CliRunner().invoke(
        cli_mod.cli, ["stack", *urls, "--provenance", "--out", str(tmp_path / "cube.tif")]
    )
    assert refused.exit_code != 0
    assert "reads the sources and writes nothing" in refused.output

    # And the command still insists on being asked for *something*.
    bare = CliRunner().invoke(cli_mod.cli, ["stack", *urls])
    assert bare.exit_code != 0
    assert "--provenance to check the sources agree first" in bare.output


# --- The speckle detection floor (``stack_stats``'s ``detection`` block) ------


def _speckled_scene(path, seed, *, looks=1.0, mean=1.0, size=256, bright=None):
    """A speckled amplitude scene: ``Gamma(looks, mean/looks)`` intensity, rooted.

    This is what a SAR image of uniform ground *is* -- the interference pattern,
    not a constant with noise on it -- so a cube of two of these is unchanged
    ground, and every cell the change detector flags in one is a false alarm.
    ``bright`` brightens the lower-right quadrant by that many decibels, which is
    change that did happen.
    """
    rasterio = pytest.importorskip("rasterio")
    np = pytest.importorskip("numpy")
    from rasterio.transform import from_origin

    rng = np.random.default_rng(seed)
    surface = np.full((size, size), float(mean))
    if bright is not None:
        surface[size // 2 :, size // 2 :] *= 10.0 ** (bright / 10.0)
    intensity = rng.gamma(looks, surface / looks)
    profile = {
        "driver": "GTiff",
        "height": size,
        "width": size,
        "count": 1,
        "dtype": "float32",
        "crs": "EPSG:32633",
        "transform": from_origin(500000.0, 4000000.0, 10.0, 10.0),
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(np.sqrt(intensity).astype("float32"), 1)
    return path


def _speckled_pair(tmp_path, *, looks=1.0, bright=None, size=256):
    """Two passes over the same ground, independently speckled."""
    return [
        _stack_item(
            _speckled_scene(tmp_path / f"sp{n}.tif", seed=n, looks=looks, size=size, bright=bright),
            f"acq-{n}",
            f"2024-0{n}-08T12:00:00Z",
        )
        for n in (1, 2)
    ]


def test_the_speckle_false_alarm_rate_matches_simulated_speckle():
    """The claim the whole floor rests on, checked against the physics it models."""
    np = pytest.importorskip("numpy")
    from umbra_py.load import _speckle_change_sigma_db, _speckle_false_alarm

    rng = np.random.default_rng(11)
    for looks in (1.0, 2.0, 4.0):
        delta_db = 10.0 * np.log10(
            rng.gamma(looks, 1.0 / looks, 400_000) / rng.gamma(looks, 1.0 / looks, 400_000)
        )
        assert _speckle_change_sigma_db(looks) == pytest.approx(float(delta_db.std()), rel=0.01)
        for threshold in (1.0, 3.0, 6.0):
            simulated = float(np.mean(np.abs(delta_db) > threshold))
            assert _speckle_false_alarm(threshold, looks) == pytest.approx(simulated, abs=0.005)


def test_the_reported_threshold_is_the_one_its_own_false_alarm_rate_answers_for():
    """Bisected against the same function, so the pair cannot disagree."""
    from umbra_py.load import (
        DETECTION_FALSE_ALARM_TARGET,
        _detection_threshold_db,
        _speckle_false_alarm,
    )

    for looks in (1.0, 1.7, 6.0, 40.0):
        threshold = _detection_threshold_db(DETECTION_FALSE_ALARM_TARGET, looks)
        assert _speckle_false_alarm(threshold, looks) == pytest.approx(
            DETECTION_FALSE_ALARM_TARGET, abs=1e-6
        )
    # More looks is a lower bar: averaging speckle down is what buys sensitivity.
    thresholds = [_detection_threshold_db(0.05, n) for n in (1.0, 4.0, 16.0)]
    assert thresholds == sorted(thresholds, reverse=True)


def test_stack_stats_predicts_the_false_alarms_unchanged_ground_produces(tmp_path):
    """The end-to-end claim: on ground that did not change, every flag is a false one.

    Two independent single-look realisations of the *same* surface, so the true
    changed fraction is zero and whatever ``net_change`` reports is exactly what
    the detection floor exists to predict. The two numbers have to agree.
    """
    pytest.importorskip("xarray")
    from umbra_py import stack_stats, to_stack

    stats = stack_stats(to_stack(_speckled_pair(tmp_path), max_size=256, crs="utm"))

    detection = stats["detection"]
    assert detection["looks"] == pytest.approx(1.0, abs=0.15)
    assert detection["cell_sigma_db"] == pytest.approx(7.9, abs=0.6)
    assert detection["false_alarm_fraction"] == pytest.approx(
        stats["net_change"]["changed_fraction"], abs=0.05
    )
    # A 3 dB threshold on single-look imagery is mostly counting interference, and
    # holding the false alarms to 5 % takes a far wider bar than the default.
    assert detection["false_alarm_fraction"] > 0.5
    assert detection["target_threshold_db"] > 12.0
    assert all(record["looks"] == pytest.approx(1.0, abs=0.2) for record in stats["passes"])


def test_stack_stats_says_when_the_change_does_not_clear_the_speckle_floor(tmp_path):
    """Unchanged ground earns the finding; genuinely changed ground does not."""
    pytest.importorskip("xarray")
    from umbra_py import stack_stats, to_stack

    def caveats(items):
        return stack_stats(to_stack(items, max_size=256, crs="utm"))["caveats"]

    flat = tmp_path / "flat"
    flat.mkdir()
    unchanged = caveats(_speckled_pair(flat))
    assert any("Speckle alone moves an unchanged cell" in c for c in unchanged)
    assert any("does not stand clear of that speckle floor" in c for c in unchanged)

    # The whole scene 25 dB brighter is change no amount of interference accounts
    # for, so the floor is context rather than the headline. It takes that much on
    # single-look imagery: at one look the floor already flags two cells in three,
    # which is the finding rather than a defect of the test.
    changed = caveats(
        [
            _stack_item(
                _speckled_scene(tmp_path / "a.tif", seed=1),
                "acq-1",
                "2024-01-08T12:00:00Z",
            ),
            _stack_item(
                _speckled_scene(tmp_path / "b.tif", seed=2, mean=10.0**2.5),
                "acq-2",
                "2024-02-08T12:00:00Z",
            ),
        ]
    )
    assert any("Speckle alone moves an unchanged cell" in c for c in changed)
    assert not any("does not stand clear of that speckle floor" in c for c in changed)


def test_speckle_filtering_lowers_the_floor_it_is_measured_against(tmp_path):
    """What the filter bought, in the units of the answer rather than of the window."""
    pytest.importorskip("xarray")
    from umbra_py import stack_stats, to_stack

    items = _speckled_pair(tmp_path)
    plain = stack_stats(to_stack(items, max_size=256, crs="utm"))["detection"]
    filtered = stack_stats(
        to_stack(items, max_size=256, crs="utm", speckle_filter="boxcar", speckle_window=5)
    )["detection"]

    assert filtered["looks"] > plain["looks"]
    assert filtered["cell_sigma_db"] < plain["cell_sigma_db"]
    assert filtered["false_alarm_fraction"] < plain["false_alarm_fraction"]
    assert filtered["target_threshold_db"] < plain["target_threshold_db"]


def test_detection_is_absent_rather_than_null_when_there_is_nothing_to_weigh(tmp_path):
    """A single pass has no comparison; a cube under one block has no reading."""
    pytest.importorskip("xarray")
    from umbra_py import stack_stats, to_stack

    items = _speckled_pair(tmp_path)
    single = stack_stats(to_stack(items[:1], max_size=256, crs="utm"))
    assert "detection" not in single
    assert single["passes"][0]["looks"] is not None

    tiny = stack_stats(to_stack(items, max_size=8, crs="utm"))
    assert "detection" not in tiny
    assert all(record["looks"] is None for record in tiny["passes"])
