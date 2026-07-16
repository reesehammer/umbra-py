"""Offline tests for SICD conversion (``umbra_py.convert``).

The geocoding core (:func:`_warp_gcps_to_cog`) is deliberately free of any
SICD/sarpy dependency, so it is exercised here with a plain amplitude array and
hand-built ground control points -- no NITF fixture, no network. The SICD-facing
functions are covered with a fake reader/projection injected in place of
``sarpy``'s ``open_complex`` (a local import that re-resolves the patched
attribute each call), so the read -> amplitude -> GCP -> warp plumbing runs end
to end offline.
"""

from __future__ import annotations

import math

import pytest

from umbra_py import convert

# --------------------------------------------------------------------------- #
# Pure helpers (no optional extras).
# --------------------------------------------------------------------------- #


def test_grid_indices_spans_endpoints_and_is_sorted_unique():
    idx = convert._grid_indices(100, 5)
    assert idx[0] == 0
    assert idx[-1] == 99
    assert idx == sorted(idx)
    assert len(set(idx)) == len(idx)


def test_grid_indices_clamps_count_to_image_size():
    # Asking for more points than pixels can't invent indices.
    assert convert._grid_indices(3, 10) == [0, 1, 2]
    # A degenerate single-pixel axis collapses to one index.
    assert convert._grid_indices(1, 5) == [0]
    # Fewer than two is bumped to the two endpoints.
    assert convert._grid_indices(10, 1) == [0, 9]


# --------------------------------------------------------------------------- #
# Fakes standing in for a real SICD reader / projection model.
# --------------------------------------------------------------------------- #


class _FakeSicd:
    """Minimal SICD projection model: an affine image(row,col) -> lon/lat map.

    ``hae_shift`` couples the projection height to the ground longitude (a
    stand-in for terrain layover: a point projected at a greater height lands
    further east), so the DEM-refinement loop has something to converge against.
    Default ``0.0`` keeps the height-independent flat-earth behaviour the older
    tests assert on.
    """

    def __init__(self, lon0=-100.0, lat0=40.0, dlon=0.01, dlat=0.01, skew=0.002, hae_shift=0.0):
        self.lon0, self.lat0, self.dlon, self.dlat, self.skew = lon0, lat0, dlon, dlat, skew
        self.hae_shift = hae_shift
        self.calls: list[tuple] = []

    def project_image_to_ground_geo(
        self, im_points, ordering="latlong", projection_type="HAE", hae0=None
    ):
        import numpy as np

        self.calls.append((ordering, projection_type))
        pts = np.asarray(im_points, dtype="float64")
        rows, cols = pts[:, 0], pts[:, 1]
        h = 0.0 if hae0 is None else float(hae0)
        lon = self.lon0 + cols * self.dlon + rows * self.skew + h * self.hae_shift
        lat = self.lat0 - rows * self.dlat + cols * self.skew
        hae = np.full_like(lon, h)
        return np.stack([lat, lon, hae], axis=1)


class _FakeReader:
    def __init__(self, complex_data, sicd):
        self._data = complex_data
        self._sicd = sicd

    def __getitem__(self, _key):  # reader[:, :]
        return self._data

    def get_sicds_as_tuple(self):
        return (self._sicd,)


def _fake_complex(rows=12, cols=24):
    np = pytest.importorskip("numpy")
    mag = (np.arange(rows * cols, dtype="float64") + 1.0).reshape(rows, cols)
    return mag * (1 + 0j)  # zero phase, so |z| == mag exactly


# --------------------------------------------------------------------------- #
# Amplitude detection.
# --------------------------------------------------------------------------- #


def test_amplitude_linear_and_decibel():
    np = pytest.importorskip("numpy")
    data = np.array([[3.0 + 4.0j]], dtype="complex64")  # |z| == 5
    lin = convert._amplitude(data, decibels=False)
    assert lin.dtype == np.dtype("float32")
    assert math.isclose(float(lin[0, 0]), 5.0, rel_tol=1e-5)

    db = convert._amplitude(data, decibels=True)
    assert math.isclose(float(db[0, 0]), 20.0 * math.log10(5.0), rel_tol=1e-5)


# --------------------------------------------------------------------------- #
# GCP construction from the projection model.
# --------------------------------------------------------------------------- #


def test_build_gcps_covers_corners_and_uses_projection_type():
    pytest.importorskip("numpy")
    pytest.importorskip("rasterio")

    sicd = _FakeSicd()
    rows, cols = 30, 40
    gcps = convert._build_gcps(sicd, (rows, cols), grid=4, projection_type="PLANE")

    assert len(gcps) == 16  # a full 4x4 lattice, no collapsed duplicates
    assert sicd.calls == [("latlong", "PLANE")]  # projection_type threaded through

    # Image-space corners are pinned.
    corners = {(g.row, g.col) for g in gcps}
    assert (0.0, 0.0) in corners
    assert (float(rows - 1), float(cols - 1)) in corners

    # x/y carry lon/lat (not lat/lon): the top-left maps to the model's origin.
    tl = next(g for g in gcps if g.row == 0.0 and g.col == 0.0)
    assert math.isclose(tl.x, sicd.lon0, abs_tol=1e-9)
    assert math.isclose(tl.y, sicd.lat0, abs_tol=1e-9)


# --------------------------------------------------------------------------- #
# The geocoding core: warp GCP-tagged amplitude onto a north-up EPSG:4326 COG.
# --------------------------------------------------------------------------- #


def _hand_gcps(rows, cols, *, lon0=-100.0, lat0=40.0, res=0.01):
    """A north-up (axis-aligned) GCP set so the warp is an identity placement."""
    from rasterio.control import GroundControlPoint

    def xy(r, c):
        return lon0 + c * res, lat0 - r * res

    gcps = []
    for r in (0, rows - 1):
        for c in (0, cols - 1):
            x, y = xy(r, c)
            gcps.append(GroundControlPoint(row=r, col=c, x=x, y=y, z=0.0))
    return gcps


def test_warp_gcps_to_cog_writes_geocoded_cog(tmp_path):
    rasterio = pytest.importorskip("rasterio")
    np = pytest.importorskip("numpy")

    rows, cols = 16, 32
    amp = (np.arange(rows * cols, dtype="float32") + 1.0).reshape(rows, cols)
    gcps = _hand_gcps(rows, cols, res=0.01)

    out = convert._warp_gcps_to_cog(
        amp, gcps, tmp_path / "geo.tif", resolution=0.01, resampling="nearest", nodata=float("nan")
    )
    assert out.exists()

    with rasterio.open(out) as ds:
        assert ds.crs.to_epsg() == 4326
        assert ds.count == 1
        assert ds.nodata is None or math.isnan(ds.nodata)
        # North-up: negative north-south pixel size, positive east-west.
        assert ds.transform.a > 0
        assert ds.transform.e < 0
        # Bounds match the GCP lon/lat extent (a couple of pixels of slack for
        # the ceil on width/height).
        assert ds.bounds.left == pytest.approx(-100.0, abs=0.02)
        assert ds.bounds.top == pytest.approx(40.0, abs=0.02)
        band = ds.read(1)
        finite = np.isfinite(band)
        assert finite.any()
        # The scene's brightness range survives the warp: the brightest output
        # pixel is close to the brightest input (resampling may miss the exact
        # corner node, so allow a small shortfall).
        assert float(np.nanmax(band)) >= 0.85 * float(amp.max())
        assert float(np.nanmax(band)) <= float(amp.max())


def test_warp_rejects_unknown_resampling(tmp_path):
    pytest.importorskip("rasterio")
    np = pytest.importorskip("numpy")
    amp = np.ones((4, 4), dtype="float32")
    gcps = _hand_gcps(4, 4)
    with pytest.raises(ValueError, match="resampling"):
        convert._warp_gcps_to_cog(
            amp, gcps, tmp_path / "x.tif", resolution=0.01, resampling="sinc", nodata=0.0
        )


def test_warp_rejects_degenerate_gcps(tmp_path):
    pytest.importorskip("rasterio")
    np = pytest.importorskip("numpy")
    from rasterio.control import GroundControlPoint

    amp = np.ones((4, 4), dtype="float32")
    # All GCPs at one lon/lat -> zero geographic extent.
    gcps = [GroundControlPoint(row=r, col=c, x=-100.0, y=40.0) for r in (0, 3) for c in (0, 3)]
    with pytest.raises(ValueError, match="degenerate"):
        convert._warp_gcps_to_cog(
            amp, gcps, tmp_path / "x.tif", resolution=0.01, resampling="nearest", nodata=0.0
        )


# --------------------------------------------------------------------------- #
# End-to-end SICD functions with a faked sarpy reader.
# --------------------------------------------------------------------------- #


def _patch_open_complex(monkeypatch, reader):
    # convert.py does `from sarpy.io.complex.converter import open_complex`
    # inside the function, so patching the source attribute is picked up.
    import sarpy.io.complex.converter as conv_mod

    monkeypatch.setattr(conv_mod, "open_complex", lambda _src: reader)


def test_sicd_to_amplitude_geotiff_is_ungeoreferenced(tmp_path, monkeypatch):
    rasterio = pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")
    np = pytest.importorskip("numpy")

    data = _fake_complex(8, 10)
    _patch_open_complex(monkeypatch, _FakeReader(data, _FakeSicd()))

    out = convert.sicd_to_amplitude_geotiff(
        tmp_path / "in.ntf", tmp_path / "amp.tif", decibels=False
    )
    with rasterio.open(out) as ds:
        assert ds.crs is None  # slant plane: no geolocation
        assert ds.width == 10 and ds.height == 8
        assert float(np.nanmax(ds.read(1))) == pytest.approx(float(np.abs(data).max()), rel=1e-5)


def test_sicd_to_geocoded_cog_end_to_end(tmp_path, monkeypatch):
    rasterio = pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")
    np = pytest.importorskip("numpy")

    data = _fake_complex(12, 24)
    sicd = _FakeSicd()
    _patch_open_complex(monkeypatch, _FakeReader(data, sicd))

    out = convert.sicd_to_geocoded_cog(
        tmp_path / "in.ntf",
        tmp_path / "geo.tif",
        decibels=True,
        gcp_grid=6,
        resampling="bilinear",
    )
    assert sicd.calls and sicd.calls[0][1] == "HAE"  # default flat-earth projection
    with rasterio.open(out) as ds:
        assert ds.crs.to_epsg() == 4326
        assert ds.transform.e < 0  # north-up
        assert np.isfinite(ds.read(1)).any()


def test_cli_convert_geocoded(tmp_path, monkeypatch):
    rasterio = pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    _patch_open_complex(monkeypatch, _FakeReader(_fake_complex(10, 12), _FakeSicd()))

    src = tmp_path / "scene.ntf"
    src.write_bytes(b"not-a-real-nitf")  # exists() check only; reader is faked
    out = tmp_path / "geo.tif"
    result = CliRunner().invoke(cli_mod.cli, ["convert", str(src), str(out)])

    assert result.exit_code == 0, result.output
    assert out.exists()
    with rasterio.open(out) as ds:
        assert ds.crs.to_epsg() == 4326


def test_cli_convert_slant_plane(tmp_path, monkeypatch):
    rasterio = pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    _patch_open_complex(monkeypatch, _FakeReader(_fake_complex(6, 8), _FakeSicd()))

    src = tmp_path / "scene.ntf"
    src.write_bytes(b"not-a-real-nitf")
    out = tmp_path / "amp.tif"
    result = CliRunner().invoke(cli_mod.cli, ["convert", str(src), str(out), "--slant-plane"])

    assert result.exit_code == 0, result.output
    with rasterio.open(out) as ds:
        assert ds.crs is None


# --------------------------------------------------------------------------- #
# DEM terrain orthorectification.
# --------------------------------------------------------------------------- #


def test_sicd_projector_batches_and_threads_height(monkeypatch):
    """The HAE projector groups equal-height points into one call, per point otherwise."""
    np = pytest.importorskip("numpy")

    sicd = _FakeSicd(hae_shift=1e-4)
    project = convert._sicd_projector(sicd, height_bin=1.0)
    im_points = np.array([[0, 0], [0, 1], [1, 0], [1, 1]], dtype="float64")

    # All points at the same height -> a single grouped projection call.
    lats, lons = project(im_points, np.zeros(4))
    assert len(sicd.calls) == 1
    assert lats.shape == (4,) and lons.shape == (4,)

    # Two distinct heights -> two calls, and the per-point height shifts lon east.
    sicd.calls.clear()
    lats2, lons2 = project(im_points, np.array([0.0, 0.0, 500.0, 500.0]))
    assert len(sicd.calls) == 2
    # The 500 m points (rows 2,3) are shifted by 500 * hae_shift relative to flat.
    assert lons2[2] == pytest.approx(lons[2] + 500.0 * 1e-4, abs=1e-9)
    assert lons2[0] == pytest.approx(lons[0], abs=1e-9)


def test_refine_gcps_with_dem_converges_to_terrain_surface():
    """Injected project+sample_height fixed-point iteration lands on the terrain."""
    np = pytest.importorskip("numpy")

    lon_ref = -100.0
    k = 1e-4  # horizontal metres->degrees coupling of projection height
    slope = 300.0  # terrain rises 300 m per degree of longitude east of lon_ref

    def project(im_points, haes):
        im_points = np.asarray(im_points, dtype="float64")
        haes = np.broadcast_to(np.asarray(haes, dtype="float64"), (im_points.shape[0],))
        lons = lon_ref + im_points[:, 1] * 0.01 + haes * k
        lats = 40.0 - im_points[:, 0] * 0.01
        return lats, lons

    def sample_height(lons, lats):
        return slope * (np.asarray(lons, dtype="float64") - lon_ref)

    im_points = np.array([[0, 0], [0, 5], [3, 9]], dtype="float64")
    lats, lons, haes = convert._refine_gcps_with_dem(
        im_points, project, sample_height, h0=0.0, tol=1e-6, max_iter=50
    )

    # Self-consistency: the height each point sits at equals the DEM there.
    assert np.allclose(haes, sample_height(lons, lats), atol=1e-3)
    # Closed-form fixed point h* = slope*col*0.01 / (1 - slope*k) for this linear DEM.
    expected = slope * im_points[:, 1] * 0.01 / (1.0 - slope * k)
    assert np.allclose(haes, expected, rtol=1e-4)


def test_refine_gcps_flat_dem_reduces_to_constant_height():
    np = pytest.importorskip("numpy")

    def project(im_points, haes):
        im_points = np.asarray(im_points, dtype="float64")
        haes = np.broadcast_to(np.asarray(haes, dtype="float64"), (im_points.shape[0],))
        return 40.0 - im_points[:, 0], -100.0 + im_points[:, 1]

    def sample_height(lons, lats):
        return np.full(np.shape(lons), 123.0)  # flat plateau

    im_points = np.array([[0, 0], [2, 2]], dtype="float64")
    _lats, _lons, haes = convert._refine_gcps_with_dem(im_points, project, sample_height, h0=0.0)
    assert np.allclose(haes, 123.0)


def test_refine_gcps_keeps_scene_height_off_dem():
    """Points where the DEM has no coverage (NaN) keep the seed height."""
    np = pytest.importorskip("numpy")

    def project(im_points, haes):
        im_points = np.asarray(im_points, dtype="float64")
        return 40.0 - im_points[:, 0], -100.0 + im_points[:, 1]

    def sample_height(lons, lats):
        vals = np.full(np.shape(lons), np.nan)  # DEM covers nothing
        return vals

    im_points = np.array([[0, 0], [1, 1]], dtype="float64")
    _lats, _lons, haes = convert._refine_gcps_with_dem(im_points, project, sample_height, h0=42.0)
    assert np.allclose(haes, 42.0)  # fell back to the scene reference height


def _write_dem(
    path,
    *,
    crs="EPSG:4326",
    bounds=(-100.6, 39.6, -99.4, 40.4),
    shape=(60, 60),
    kind="ramp",
    nodata=None,
    const=100.0,
):
    import numpy as np
    import rasterio
    from rasterio.transform import from_bounds

    left, bottom, right, top = bounds
    h, w = shape
    if kind == "ramp":
        row = np.linspace(0.0, 500.0, w, dtype="float32")
        data = np.broadcast_to(row, (h, w)).copy()
    else:
        data = np.full((h, w), float(const), dtype="float32")
    profile = {
        "driver": "GTiff",
        "height": h,
        "width": w,
        "count": 1,
        "dtype": "float32",
        "crs": crs,
        "transform": from_bounds(left, bottom, right, top, w, h),
    }
    if nodata is not None:
        profile["nodata"] = nodata
        data[0, 0] = nodata  # a nodata cell to exercise masking
    with rasterio.open(path, "w", **profile) as ds:
        ds.write(data, 1)
    return path


def test_dem_height_sampler_reads_ramp_and_masks_out_of_bounds(tmp_path):
    rasterio = pytest.importorskip("rasterio")
    np = pytest.importorskip("numpy")

    dem = _write_dem(tmp_path / "dem.tif", bounds=(-101.0, 39.0, -99.0, 41.0), shape=(100, 100))
    with rasterio.open(dem) as ds:
        sample = convert._dem_height_sampler(ds)
        # West edge ~0 m, east edge ~500 m; midpoint ~250 m.
        vals = sample(np.array([-100.99, -99.01, -100.0]), np.array([40.0, 40.0, 40.0]))
        assert vals[0] == pytest.approx(0.0, abs=15.0)
        assert vals[1] == pytest.approx(500.0, abs=15.0)
        assert vals[2] == pytest.approx(250.0, abs=15.0)
        # Outside the DEM extent -> NaN, not a bogus edge value.
        oob = sample(np.array([-105.0]), np.array([40.0]))
        assert np.isnan(oob[0])


def test_dem_height_sampler_masks_nodata_and_reprojects(tmp_path):
    rasterio = pytest.importorskip("rasterio")
    np = pytest.importorskip("numpy")

    # A constant DEM in a projected CRS (UTM 13N) exercises the reprojection
    # branch; a nodata cell exercises masking.
    dem = _write_dem(
        tmp_path / "dem_utm.tif",
        crs="EPSG:32613",
        bounds=(400000, 4420000, 500000, 4520000),
        shape=(40, 40),
        kind="const",
        const=77.0,
        nodata=-9999.0,
    )
    with rasterio.open(dem) as ds:
        sample = convert._dem_height_sampler(ds)
        # A lon/lat inside the UTM footprint (~ -105.5, 40.5) reprojects and reads 77.
        vals = sample(np.array([-105.5]), np.array([40.5]))
        assert vals[0] == pytest.approx(77.0, abs=1e-3)


def test_sicd_to_geocoded_cog_with_dem_shifts_geolocation(tmp_path, monkeypatch):
    rasterio = pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")
    np = pytest.importorskip("numpy")

    data = _fake_complex(12, 24)
    dem = _write_dem(tmp_path / "dem.tif")

    # Flat-earth run (no DEM).
    _patch_open_complex(monkeypatch, _FakeReader(data, _FakeSicd(hae_shift=1e-4)))
    flat = convert.sicd_to_geocoded_cog(
        tmp_path / "in.ntf", tmp_path / "flat.tif", gcp_grid=6, resampling="nearest"
    )

    # Terrain run: a strong height->lon coupling makes the ramp DEM move the scene.
    sicd = _FakeSicd(hae_shift=1e-4)
    _patch_open_complex(monkeypatch, _FakeReader(data, sicd))
    terr = convert.sicd_to_geocoded_cog(
        tmp_path / "in.ntf", tmp_path / "terr.tif", gcp_grid=6, resampling="nearest", dem=str(dem)
    )

    with rasterio.open(flat) as a, rasterio.open(terr) as b:
        assert a.crs.to_epsg() == 4326 and b.crs.to_epsg() == 4326
        assert b.transform.e < 0  # still north-up
        assert np.isfinite(b.read(1)).any()
        # The DEM moved the geolocation: the eastern extent shifts measurably.
        assert abs(a.bounds.right - b.bounds.right) > 1e-3
    # The refinement ran the HAE projection more than once (grouped iterations).
    assert sum(1 for c in sicd.calls if c[1] == "HAE") >= 2


def test_cli_convert_with_dem(tmp_path, monkeypatch):
    rasterio = pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    _patch_open_complex(monkeypatch, _FakeReader(_fake_complex(10, 12), _FakeSicd(hae_shift=1e-4)))
    dem = _write_dem(tmp_path / "dem.tif")

    src = tmp_path / "scene.ntf"
    src.write_bytes(b"not-a-real-nitf")
    out = tmp_path / "geo.tif"
    result = CliRunner().invoke(cli_mod.cli, ["convert", str(src), str(out), "--dem", str(dem)])

    assert result.exit_code == 0, result.output
    assert "terrain-orthorectified" in result.output
    with rasterio.open(out) as ds:
        assert ds.crs.to_epsg() == 4326


# --------------------------------------------------------------------------- #
# --dem auto: fetch the covering Copernicus DEM for the scene.
# --------------------------------------------------------------------------- #


def test_scene_geo_bbox_bounds_the_corner_projection():
    pytest.importorskip("numpy")
    sicd = _FakeSicd()  # lon0=-100, lat0=40, dlon=dlat=0.01, skew=0.002
    west, south, east, north = convert._scene_geo_bbox(sicd, (30, 40))
    # Longitude grows with column (east) and rows (skew); latitude drops with row.
    assert west == pytest.approx(sicd.lon0, abs=1e-9)  # top-left corner
    assert east > west and north > south
    assert -101.0 < west < east < -98.0
    assert 39.0 < south < north < 41.0


def test_sicd_to_geocoded_cog_dem_auto_fetches_covering_dem(tmp_path, monkeypatch):
    rasterio = pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")

    data = _fake_complex(12, 24)
    dem = _write_dem(tmp_path / "auto_dem.tif")
    sicd = _FakeSicd(hae_shift=1e-4)
    _patch_open_complex(monkeypatch, _FakeReader(data, sicd))

    seen = {}

    def fake_fetch(bbox, *args, **kwargs):
        seen["bbox"] = bbox
        return dem

    import umbra_py.dem as dem_mod

    monkeypatch.setattr(dem_mod, "fetch_dem_for_bbox", fake_fetch)

    out = convert.sicd_to_geocoded_cog(
        tmp_path / "in.ntf", tmp_path / "auto.tif", gcp_grid=6, resampling="nearest", dem="auto"
    )

    # The scene bbox (west, south, east, north) was handed to the fetcher.
    assert "bbox" in seen and len(seen["bbox"]) == 4
    assert seen["bbox"][0] < seen["bbox"][2] and seen["bbox"][1] < seen["bbox"][3]
    with rasterio.open(out) as ds:
        assert ds.crs.to_epsg() == 4326


def test_cli_convert_dem_auto(tmp_path, monkeypatch):
    rasterio = pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    _patch_open_complex(monkeypatch, _FakeReader(_fake_complex(10, 12), _FakeSicd(hae_shift=1e-4)))
    dem = _write_dem(tmp_path / "auto_dem.tif")

    import umbra_py.dem as dem_mod

    monkeypatch.setattr(dem_mod, "fetch_dem_for_bbox", lambda bbox, *a, **k: dem)

    src = tmp_path / "scene.ntf"
    src.write_bytes(b"not-a-real-nitf")
    out = tmp_path / "geo.tif"
    result = CliRunner().invoke(cli_mod.cli, ["convert", str(src), str(out), "--dem", "auto"])

    assert result.exit_code == 0, result.output
    assert "terrain-orthorectified" in result.output
    with rasterio.open(out) as ds:
        assert ds.crs.to_epsg() == 4326


def test_cli_convert_dem_missing_path_errors(tmp_path):
    pytest.importorskip("sarpy")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    src = tmp_path / "scene.ntf"
    src.write_bytes(b"not-a-real-nitf")
    missing = tmp_path / "nope.tif"
    result = CliRunner().invoke(
        cli_mod.cli, ["convert", str(src), str(tmp_path / "o.tif"), "--dem", str(missing)]
    )
    assert result.exit_code != 0
    assert "does not exist" in result.output
