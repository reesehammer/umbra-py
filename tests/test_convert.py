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

    class _SCPCOA:
        def __init__(self, incidence, azimuth):
            self.IncidenceAng = incidence
            self.AzimAng = azimuth

    class _Grid:
        class _Axis:
            def __init__(self, ss):
                self.SS = ss

        def __init__(self, row_ss, col_ss):
            self.Row = self._Axis(row_ss)
            self.Col = self._Axis(col_ss)

    class _ImageData:
        class _Pixel:
            def __init__(self, row, col):
                self.Row = row
                self.Col = col

        def __init__(self, scp_row, scp_col, first_row, first_col):
            self.SCPPixel = self._Pixel(scp_row, scp_col)
            self.FirstRow = first_row
            self.FirstCol = first_col

    def __init__(
        self,
        lon0=-100.0,
        lat0=40.0,
        dlon=0.01,
        dlat=0.01,
        skew=0.002,
        hae_shift=0.0,
        incidence=30.0,
        azimuth=100.0,
        radiometric=None,
        row_ss=0.5,
        col_ss=0.25,
        scp_row=5.0,
        scp_col=10.0,
        first_row=0,
        first_col=0,
    ):
        self.lon0, self.lat0, self.dlon, self.dlat, self.skew = lon0, lat0, dlon, dlat, skew
        self.hae_shift = hae_shift
        self.calls: list[tuple] = []
        self.SCPCOA = self._SCPCOA(incidence, azimuth)
        self.Grid = self._Grid(row_ss, col_ss)
        self.ImageData = self._ImageData(scp_row, scp_col, first_row, first_col)
        # Umbra's open products generally ship *without* a Radiometric block, so
        # "no calibration available" is the default a fake scene stands for.
        self.Radiometric = radiometric

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


def _write_steep_ramp_dem(path, *, bounds=(-100.6, 39.6, -99.4, 40.4), shape=(60, 60), top=20000.0):
    """A steep west-to-east elevation ramp (0..``top`` m), same footprint as the
    default ramp DEM. Used to make the second-order gamma facet-area term
    (``nz = cos(slope)``) measurable, where the gentle default ramp would not."""
    import numpy as np
    import rasterio
    from rasterio.transform import from_bounds

    left, bottom, right, top_lat = bounds
    h, w = shape
    row = np.linspace(0.0, float(top), w, dtype="float32")
    data = np.broadcast_to(row, (h, w)).copy()
    profile = {
        "driver": "GTiff",
        "height": h,
        "width": w,
        "count": 1,
        "dtype": "float32",
        "crs": "EPSG:4326",
        "transform": from_bounds(left, bottom, right, top_lat, w, h),
    }
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


# --------------------------------------------------------------------------- #
# --geoid: correct DEM (orthometric) heights to ellipsoidal (HAE) before projecting.
# --------------------------------------------------------------------------- #


def test_geoid_corrected_sampler_adds_undulation():
    np = pytest.importorskip("numpy")

    def dem_sample(lons, lats):
        return np.array([100.0, 200.0, np.nan])

    def geoid_sample(lons, lats):
        # Second point falls outside the undulation grid (NaN -> treated as 0).
        return np.array([30.0, np.nan, 30.0])

    sample = convert._geoid_corrected_sampler(dem_sample, geoid_sample)
    out = sample(np.array([0.0, 1.0, 2.0]), np.array([0.0, 0.0, 0.0]))
    # hae = orthometric + undulation; missing undulation contributes 0; a DEM NaN
    # stays NaN so the refinement still falls back to the scene height there.
    assert out[0] == pytest.approx(130.0)
    assert out[1] == pytest.approx(200.0)
    assert np.isnan(out[2])


def test_sicd_to_geocoded_cog_with_geoid_shifts_geolocation(tmp_path, monkeypatch):
    rasterio = pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")
    np = pytest.importorskip("numpy")

    data = _fake_complex(12, 24)
    dem = _write_dem(tmp_path / "dem.tif")
    # A constant +60 m undulation grid over the same footprint: every sampled DEM
    # height becomes 60 m higher in HAE, so the terrain projection moves.
    geoid = _write_dem(tmp_path / "geoid.tif", kind="const", const=60.0)

    # DEM only (heights read as-is, assumed ellipsoidal).
    _patch_open_complex(monkeypatch, _FakeReader(data, _FakeSicd(hae_shift=1e-4)))
    dem_only = convert.sicd_to_geocoded_cog(
        tmp_path / "in.ntf",
        tmp_path / "dem.out.tif",
        gcp_grid=6,
        resampling="nearest",
        dem=str(dem),
    )

    # DEM + geoid: undulation lifts every height, so the scene shifts.
    sicd = _FakeSicd(hae_shift=1e-4)
    _patch_open_complex(monkeypatch, _FakeReader(data, sicd))
    corrected = convert.sicd_to_geocoded_cog(
        tmp_path / "in.ntf",
        tmp_path / "geoid.out.tif",
        gcp_grid=6,
        resampling="nearest",
        dem=str(dem),
        geoid=str(geoid),
    )

    with rasterio.open(dem_only) as a, rasterio.open(corrected) as b:
        assert a.crs.to_epsg() == 4326 and b.crs.to_epsg() == 4326
        assert b.transform.e < 0  # still north-up
        assert np.isfinite(b.read(1)).any()
        # The undulation correction moved the geolocation measurably.
        assert abs(a.bounds.right - b.bounds.right) > 1e-3


def test_geoid_without_dem_raises(tmp_path, monkeypatch):
    pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")

    geoid = _write_dem(tmp_path / "geoid.tif", kind="const", const=30.0)
    _patch_open_complex(monkeypatch, _FakeReader(_fake_complex(6, 8), _FakeSicd()))
    with pytest.raises(ValueError, match="geoid= requires dem="):
        convert.sicd_to_geocoded_cog(tmp_path / "in.ntf", tmp_path / "out.tif", geoid=str(geoid))


def test_cli_convert_with_geoid(tmp_path, monkeypatch):
    rasterio = pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    _patch_open_complex(monkeypatch, _FakeReader(_fake_complex(10, 12), _FakeSicd(hae_shift=1e-4)))
    dem = _write_dem(tmp_path / "dem.tif")
    geoid = _write_dem(tmp_path / "geoid.tif", kind="const", const=45.0)

    src = tmp_path / "scene.ntf"
    src.write_bytes(b"not-a-real-nitf")
    out = tmp_path / "geo.tif"
    result = CliRunner().invoke(
        cli_mod.cli,
        ["convert", str(src), str(out), "--dem", str(dem), "--geoid", str(geoid)],
    )

    assert result.exit_code == 0, result.output
    assert "terrain-orthorectified" in result.output
    with rasterio.open(out) as ds:
        assert ds.crs.to_epsg() == 4326


def test_cli_convert_geoid_without_dem_errors(tmp_path):
    pytest.importorskip("sarpy")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    src = tmp_path / "scene.ntf"
    src.write_bytes(b"not-a-real-nitf")
    geoid = _write_dem(tmp_path / "geoid.tif", kind="const", const=30.0)
    result = CliRunner().invoke(
        cli_mod.cli, ["convert", str(src), str(tmp_path / "o.tif"), "--geoid", str(geoid)]
    )
    assert result.exit_code != 0
    assert "--geoid requires --dem" in result.output


def test_cli_convert_geoid_missing_path_errors(tmp_path):
    pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    src = tmp_path / "scene.ntf"
    src.write_bytes(b"not-a-real-nitf")
    dem = _write_dem(tmp_path / "dem.tif")
    missing = tmp_path / "nope.tif"
    result = CliRunner().invoke(
        cli_mod.cli,
        ["convert", str(src), str(tmp_path / "o.tif"), "--dem", str(dem), "--geoid", str(missing)],
    )
    assert result.exit_code != 0
    assert "does not exist" in result.output


# --------------------------------------------------------------------------- #
# --geoid auto: fetch a global EGM geoid grid for the scene.
# --------------------------------------------------------------------------- #


def test_sicd_to_geocoded_cog_geoid_auto_fetches_grid(tmp_path, monkeypatch):
    rasterio = pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")

    data = _fake_complex(12, 24)
    dem = _write_dem(tmp_path / "dem.tif")
    geoid = _write_dem(tmp_path / "auto_geoid.tif", kind="const", const=60.0)
    _patch_open_complex(monkeypatch, _FakeReader(data, _FakeSicd(hae_shift=1e-4)))

    seen = {}

    def fake_fetch(*args, **kwargs):
        seen["called"] = True
        return geoid

    import umbra_py.geoid as geoid_mod

    monkeypatch.setattr(geoid_mod, "fetch_geoid_grid", fake_fetch)

    out = convert.sicd_to_geocoded_cog(
        tmp_path / "in.ntf",
        tmp_path / "auto.tif",
        gcp_grid=6,
        resampling="nearest",
        dem=str(dem),
        geoid="auto",
    )

    assert seen.get("called") is True
    with rasterio.open(out) as ds:
        assert ds.crs.to_epsg() == 4326


def test_geoid_auto_without_dem_raises(tmp_path, monkeypatch):
    pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")

    _patch_open_complex(monkeypatch, _FakeReader(_fake_complex(6, 8), _FakeSicd()))
    with pytest.raises(ValueError, match="geoid= requires dem="):
        convert.sicd_to_geocoded_cog(tmp_path / "in.ntf", tmp_path / "out.tif", geoid="auto")


def test_cli_convert_geoid_auto(tmp_path, monkeypatch):
    rasterio = pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    _patch_open_complex(monkeypatch, _FakeReader(_fake_complex(10, 12), _FakeSicd(hae_shift=1e-4)))
    dem = _write_dem(tmp_path / "dem.tif")
    geoid = _write_dem(tmp_path / "auto_geoid.tif", kind="const", const=45.0)

    import umbra_py.geoid as geoid_mod

    monkeypatch.setattr(geoid_mod, "fetch_geoid_grid", lambda *a, **k: geoid)

    src = tmp_path / "scene.ntf"
    src.write_bytes(b"not-a-real-nitf")
    out = tmp_path / "geo.tif"
    result = CliRunner().invoke(
        cli_mod.cli, ["convert", str(src), str(out), "--dem", str(dem), "--geoid", "auto"]
    )

    assert result.exit_code == 0, result.output
    assert "terrain-orthorectified" in result.output
    with rasterio.open(out) as ds:
        assert ds.crs.to_epsg() == 4326


def test_cli_convert_geoid_auto_without_dem_errors(tmp_path):
    pytest.importorskip("sarpy")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    src = tmp_path / "scene.ntf"
    src.write_bytes(b"not-a-real-nitf")
    result = CliRunner().invoke(
        cli_mod.cli, ["convert", str(src), str(tmp_path / "o.tif"), "--geoid", "auto"]
    )
    assert result.exit_code != 0
    assert "--geoid requires --dem" in result.output


# --------------------------------------------------------------------------- #
# Radiometric terrain flattening (RTC).
#
# The physics core (terrain normals, look vector, cosine correction) is pure
# numpy with closed-form behaviour over a planar slope, so it is exercised here
# with hand-built arrays; the end-to-end path uses the faked reader + a real DEM.
# --------------------------------------------------------------------------- #

_DEG = 1.0 / convert._M_PER_DEG_LAT  # a degree step giving 1 m of ground spacing


def test_terrain_normals_flat_is_straight_up():
    np = pytest.importorskip("numpy")
    dem = np.full((8, 10), 123.0, dtype="float64")
    nx, ny, nz = convert._terrain_normals(dem, x_res_deg=_DEG, y_res_deg=_DEG, top_lat=0.0)
    assert np.allclose(nx, 0.0)
    assert np.allclose(ny, 0.0)
    assert np.allclose(nz, 1.0)


def test_terrain_normals_east_ramp_matches_closed_form():
    np = pytest.importorskip("numpy")
    # 1 m of ground spacing per column, DEM rising 0.5 m per metre eastward.
    dem = np.broadcast_to(0.5 * np.arange(12, dtype="float64"), (6, 12)).copy()
    nx, ny, nz = convert._terrain_normals(dem, x_res_deg=_DEG, y_res_deg=_DEG, top_lat=0.0)
    # Upward normal of z = 0.5*east is (-0.5, 0, 1) normalised: it leans downhill (west).
    mag = math.sqrt(0.5**2 + 1.0)
    assert np.allclose(nx, -0.5 / mag)
    assert np.allclose(ny, 0.0)
    assert np.allclose(nz, 1.0 / mag)


def test_cos_local_incidence_reduces_to_incidence_on_flat_ground():
    np = pytest.importorskip("numpy")
    dem = np.zeros((5, 5), dtype="float64")
    normals = convert._terrain_normals(dem, x_res_deg=_DEG, y_res_deg=_DEG, top_lat=0.0)
    look = convert._look_unit_vector(37.0, 100.0)
    cos_lia = convert._cos_local_incidence(normals, look)
    assert np.allclose(cos_lia, math.cos(math.radians(37.0)))


def test_cos_local_incidence_east_ramp_is_incidence_plus_slope():
    np = pytest.importorskip("numpy")
    # East-rising ramp faces west (away from a radar looking from the east), so a
    # radar at azimuth 90 sees a back-slope: LIA = incidence + slope angle.
    dem = np.broadcast_to(0.5 * np.arange(12, dtype="float64"), (6, 12)).copy()
    normals = convert._terrain_normals(dem, x_res_deg=_DEG, y_res_deg=_DEG, top_lat=0.0)
    look = convert._look_unit_vector(45.0, 90.0)  # look from due east
    cos_lia = convert._cos_local_incidence(normals, look)
    slope = math.degrees(math.atan(0.5))
    expected = math.cos(math.radians(45.0 + slope))
    assert np.allclose(cos_lia, expected)


def test_terrain_flatten_factor_clamps_nan_and_shadow():
    np = pytest.importorskip("numpy")
    cos_ref = math.cos(math.radians(30.0))
    cos_lia = np.array([cos_ref, np.nan, -0.5, 1e-9])
    factor = convert._terrain_flatten_factor(cos_lia, cos_ref=cos_ref)
    assert factor[0] == pytest.approx(1.0)  # equal to reference -> no change
    assert factor[1] == pytest.approx(1.0)  # DEM gap (NaN) -> no change
    # Shadow / near-zero cosine is floored, so the factor cannot run away.
    assert factor[2] == pytest.approx(convert._RTC_FACTOR_MAX)
    assert factor[3] == pytest.approx(convert._RTC_FACTOR_MAX)


def test_apply_terrain_flattening_db_and_linear_preserve_nan():
    np = pytest.importorskip("numpy")
    factor = np.array([[4.0, 1.0]], dtype="float64")

    db = np.array([[10.0, np.nan]], dtype="float32")
    out_db = convert._apply_terrain_flattening(db, factor, decibels=True)
    assert out_db[0, 0] == pytest.approx(10.0 + 10.0 * math.log10(4.0), rel=1e-5)
    assert np.isnan(out_db[0, 1])

    lin = np.array([[8.0, np.nan]], dtype="float32")
    out_lin = convert._apply_terrain_flattening(lin, factor, decibels=False)
    assert out_lin[0, 0] == pytest.approx(8.0 * math.sqrt(4.0), rel=1e-5)
    assert np.isnan(out_lin[0, 1])


def test_scene_look_geometry_reads_scpcoa_and_errors_when_absent():
    sicd = _FakeSicd(incidence=42.0, azimuth=170.0)
    assert convert._scene_look_geometry(sicd) == (42.0, 170.0)

    class _Bare:
        SCPCOA = None

    with pytest.raises(ValueError, match="SCPCOA"):
        convert._scene_look_geometry(_Bare())


def test_sicd_to_geocoded_cog_rtc_flat_dem_leaves_values_unchanged(tmp_path, monkeypatch):
    rasterio = pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")
    np = pytest.importorskip("numpy")

    data = _fake_complex(12, 24)
    dem = _write_dem(tmp_path / "flat_dem.tif", kind="const", const=100.0)

    # Terrain-geocode without RTC.
    _patch_open_complex(monkeypatch, _FakeReader(data, _FakeSicd()))
    plain = convert.sicd_to_geocoded_cog(
        tmp_path / "in.ntf", tmp_path / "plain.tif", gcp_grid=6, resampling="nearest", dem=str(dem)
    )
    # Same, with RTC: on flat terrain the local incidence equals the scene
    # incidence (the default reference), so every factor is 1 -> identical values.
    _patch_open_complex(monkeypatch, _FakeReader(data, _FakeSicd()))
    flattened = convert.sicd_to_geocoded_cog(
        tmp_path / "in.ntf",
        tmp_path / "rtc.tif",
        gcp_grid=6,
        resampling="nearest",
        dem=str(dem),
        rtc=True,
    )
    with rasterio.open(plain) as a, rasterio.open(flattened) as b:
        va, vb = a.read(1), b.read(1)
        both = np.isfinite(va) & np.isfinite(vb)
        assert both.any()
        assert np.allclose(va[both], vb[both], atol=1e-3)


def test_sicd_to_geocoded_cog_rtc_slope_changes_brightness(tmp_path, monkeypatch):
    rasterio = pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")
    np = pytest.importorskip("numpy")

    data = _fake_complex(12, 24)
    dem = _write_dem(tmp_path / "ramp_dem.tif")  # default east-west ramp 0..500 m

    _patch_open_complex(monkeypatch, _FakeReader(data, _FakeSicd()))
    plain = convert.sicd_to_geocoded_cog(
        tmp_path / "in.ntf", tmp_path / "plain.tif", gcp_grid=6, resampling="nearest", dem=str(dem)
    )
    _patch_open_complex(monkeypatch, _FakeReader(data, _FakeSicd()))
    flattened = convert.sicd_to_geocoded_cog(
        tmp_path / "in.ntf",
        tmp_path / "rtc.tif",
        gcp_grid=6,
        resampling="nearest",
        dem=str(dem),
        rtc=True,
    )
    with rasterio.open(plain) as a, rasterio.open(flattened) as b:
        va, vb = a.read(1), b.read(1)
        both = np.isfinite(va) & np.isfinite(vb)
        # The geocoding is identical (same DEM); only the flattening differs, and
        # over a real slope it must move the values measurably.
        assert both.any()
        assert not np.allclose(va[both], vb[both], atol=1e-3)


def test_sicd_to_geocoded_cog_rtc_reference_offsets_flat_scene(tmp_path, monkeypatch):
    rasterio = pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")
    np = pytest.importorskip("numpy")

    data = _fake_complex(12, 24)
    dem = _write_dem(tmp_path / "flat_dem.tif", kind="const", const=100.0)

    _patch_open_complex(monkeypatch, _FakeReader(data, _FakeSicd(incidence=30.0)))
    plain = convert.sicd_to_geocoded_cog(
        tmp_path / "in.ntf", tmp_path / "plain.tif", gcp_grid=6, resampling="nearest", dem=str(dem)
    )
    _patch_open_complex(monkeypatch, _FakeReader(data, _FakeSicd(incidence=30.0)))
    shifted = convert.sicd_to_geocoded_cog(
        tmp_path / "in.ntf",
        tmp_path / "rtc.tif",
        gcp_grid=6,
        resampling="nearest",
        dem=str(dem),
        rtc=True,
        rtc_reference_deg=50.0,
    )
    # Flat DEM: cos_lia == cos(30) everywhere, so a reference of 50 deg applies a
    # uniform dB offset of 10*log10(cos50/cos30).
    offset = 10.0 * math.log10(math.cos(math.radians(50.0)) / math.cos(math.radians(30.0)))
    with rasterio.open(plain) as a, rasterio.open(shifted) as b:
        va, vb = a.read(1), b.read(1)
        both = np.isfinite(va) & np.isfinite(vb)
        assert both.any()
        assert np.allclose(vb[both] - va[both], offset, atol=1e-3)


def test_sicd_to_geocoded_cog_rtc_without_dem_raises(tmp_path, monkeypatch):
    pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")
    _patch_open_complex(monkeypatch, _FakeReader(_fake_complex(6, 8), _FakeSicd()))
    with pytest.raises(ValueError, match="rtc= requires dem="):
        convert.sicd_to_geocoded_cog(tmp_path / "in.ntf", tmp_path / "out.tif", rtc=True)


def test_cli_convert_rtc(tmp_path, monkeypatch):
    rasterio = pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    _patch_open_complex(monkeypatch, _FakeReader(_fake_complex(10, 12), _FakeSicd()))
    dem = _write_dem(tmp_path / "dem.tif")

    src = tmp_path / "scene.ntf"
    src.write_bytes(b"not-a-real-nitf")
    out = tmp_path / "geo.tif"
    result = CliRunner().invoke(
        cli_mod.cli, ["convert", str(src), str(out), "--dem", str(dem), "--rtc"]
    )

    assert result.exit_code == 0, result.output
    assert "terrain-flattened" in result.output
    with rasterio.open(out) as ds:
        assert ds.crs.to_epsg() == 4326


def test_cli_convert_rtc_without_dem_errors(tmp_path):
    pytest.importorskip("sarpy")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    src = tmp_path / "scene.ntf"
    src.write_bytes(b"not-a-real-nitf")
    result = CliRunner().invoke(
        cli_mod.cli, ["convert", str(src), str(tmp_path / "o.tif"), "--rtc"]
    )
    assert result.exit_code != 0
    assert "--rtc requires --dem" in result.output


# --------------------------------------------------------------------------- #
# The projected-area / foreshortening RTC model (rtc_model="area"). Like the
# cosine core above, its geometry is a pure-numpy core with closed-form behaviour
# over a planar slope, so it is exercised here with hand-built arrays; the
# end-to-end path uses the faked reader + a real DEM.
# --------------------------------------------------------------------------- #


def test_range_local_incidence_flat_is_scene_incidence():
    np = pytest.importorskip("numpy")
    dem = np.zeros((5, 5), dtype="float64")
    normals = convert._terrain_normals(dem, x_res_deg=_DEG, y_res_deg=_DEG, top_lat=0.0)
    theta = convert._range_local_incidence(normals, incidence_deg=37.0, azimuth_deg=100.0)
    # Flat ground has no range tilt, so the local range incidence is the scene angle.
    assert np.allclose(theta, math.radians(37.0))


def test_range_local_incidence_range_ramp_is_incidence_plus_slope():
    np = pytest.importorskip("numpy")
    # East-rising ramp faces west (away from a radar looking from due east, so the
    # range direction is east): a back-slope, local range incidence = incidence +
    # slope angle -- the same closed form the cosine LIA reduces to when the slope
    # lies entirely in the range direction.
    dem = np.broadcast_to(0.5 * np.arange(12, dtype="float64"), (6, 12)).copy()
    normals = convert._terrain_normals(dem, x_res_deg=_DEG, y_res_deg=_DEG, top_lat=0.0)
    theta = convert._range_local_incidence(normals, incidence_deg=45.0, azimuth_deg=90.0)
    expected = math.radians(45.0) + math.atan(0.5)
    assert np.allclose(theta, expected)


def test_range_local_incidence_ignores_azimuth_direction_slope():
    np = pytest.importorskip("numpy")
    # An east-west ramp seen by a radar looking from due north (azimuth 0) is a
    # pure *azimuth*-direction slope: it does not foreshorten, so the area model
    # leaves the local range incidence at the scene angle -- exactly the case the
    # per-pixel cosine model wrongly "corrects".
    dem = np.broadcast_to(0.5 * np.arange(12, dtype="float64"), (6, 12)).copy()
    normals = convert._terrain_normals(dem, x_res_deg=_DEG, y_res_deg=_DEG, top_lat=0.0)
    theta = convert._range_local_incidence(normals, incidence_deg=40.0, azimuth_deg=0.0)
    assert np.allclose(theta, math.radians(40.0))
    # The cosine model, in contrast, does see a change on the same azimuth slope.
    look = convert._look_unit_vector(40.0, 0.0)
    cos_lia = convert._cos_local_incidence(normals, look)
    assert not np.allclose(cos_lia, math.cos(math.radians(40.0)))


def test_foreshortening_factor_flat_foreshortened_layover_and_gap():
    np = pytest.importorskip("numpy")
    sin_ref = math.sin(math.radians(30.0))
    theta = np.array([math.radians(30.0), math.radians(10.0), 0.0, -0.2, np.nan])
    factor = convert._foreshortening_factor(theta, sin_ref=sin_ref)
    assert factor[0] == pytest.approx(1.0)  # reference angle -> no change
    # A foreshortened (radar-facing) slope is darkened: factor below one.
    assert factor[1] == pytest.approx(math.sin(math.radians(10.0)) / sin_ref, rel=1e-6)
    assert factor[1] < 1.0
    # Layover (theta_local <= 0) is floored, so the factor cannot run away.
    assert factor[2] == pytest.approx(convert._RTC_FACTOR_MIN)
    assert factor[3] == pytest.approx(convert._RTC_FACTOR_MIN)
    # A DEM gap (NaN) leaves the pixel unchanged.
    assert factor[4] == pytest.approx(1.0)


def test_sicd_to_geocoded_cog_rtc_area_flat_dem_leaves_values_unchanged(tmp_path, monkeypatch):
    rasterio = pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")
    np = pytest.importorskip("numpy")

    data = _fake_complex(12, 24)
    dem = _write_dem(tmp_path / "flat_dem.tif", kind="const", const=100.0)

    _patch_open_complex(monkeypatch, _FakeReader(data, _FakeSicd()))
    plain = convert.sicd_to_geocoded_cog(
        tmp_path / "in.ntf", tmp_path / "plain.tif", gcp_grid=6, resampling="nearest", dem=str(dem)
    )
    # On flat terrain the local range incidence equals the scene incidence (the
    # default reference), so every area factor is 1 -> identical values.
    _patch_open_complex(monkeypatch, _FakeReader(data, _FakeSicd()))
    flattened = convert.sicd_to_geocoded_cog(
        tmp_path / "in.ntf",
        tmp_path / "rtc.tif",
        gcp_grid=6,
        resampling="nearest",
        dem=str(dem),
        rtc=True,
        rtc_model="area",
    )
    with rasterio.open(plain) as a, rasterio.open(flattened) as b:
        va, vb = a.read(1), b.read(1)
        both = np.isfinite(va) & np.isfinite(vb)
        assert both.any()
        assert np.allclose(va[both], vb[both], atol=1e-3)


def test_sicd_to_geocoded_cog_rtc_area_and_cosine_differ_over_slope(tmp_path, monkeypatch):
    rasterio = pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")
    np = pytest.importorskip("numpy")

    data = _fake_complex(12, 24)
    dem = _write_dem(tmp_path / "ramp_dem.tif")  # default east-west ramp 0..500 m

    _patch_open_complex(monkeypatch, _FakeReader(data, _FakeSicd()))
    cosine = convert.sicd_to_geocoded_cog(
        tmp_path / "in.ntf",
        tmp_path / "cosine.tif",
        gcp_grid=6,
        resampling="nearest",
        dem=str(dem),
        rtc=True,
        rtc_model="cosine",
    )
    _patch_open_complex(monkeypatch, _FakeReader(data, _FakeSicd()))
    area = convert.sicd_to_geocoded_cog(
        tmp_path / "in.ntf",
        tmp_path / "area.tif",
        gcp_grid=6,
        resampling="nearest",
        dem=str(dem),
        rtc=True,
        rtc_model="area",
    )
    with rasterio.open(cosine) as a, rasterio.open(area) as b:
        va, vb = a.read(1), b.read(1)
        both = np.isfinite(va) & np.isfinite(vb)
        # Same geocoding; the two flattening models must move a real slope by
        # measurably different amounts.
        assert both.any()
        assert not np.allclose(va[both], vb[both], atol=1e-3)


def test_sicd_to_geocoded_cog_rtc_invalid_model_raises(tmp_path, monkeypatch):
    pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")
    _patch_open_complex(monkeypatch, _FakeReader(_fake_complex(6, 8), _FakeSicd()))
    with pytest.raises(ValueError, match="Unknown rtc_model"):
        convert.sicd_to_geocoded_cog(
            tmp_path / "in.ntf",
            tmp_path / "out.tif",
            dem=str(tmp_path / "dem.tif"),
            rtc=True,
            rtc_model="bogus",
        )


# --------------------------------------------------------------------------- #
# The per-pixel facet-area / gamma-nought RTC model (rtc_model="gamma"). Its
# factor is the cosine factor scaled by the true tilted-facet-area term nz, so it
# is a pure-numpy core with closed-form behaviour over a planar slope, exercised
# here with hand-built arrays; the end-to-end path uses the faked reader + a real
# DEM, like the cosine and area models above.
# --------------------------------------------------------------------------- #


def test_facet_area_factor_flat_slope_gap_and_clamp():
    np = pytest.importorskip("numpy")
    cos_ref = math.cos(math.radians(35.0))
    # A flat facet (cos_lia == cos_ref, nz == 1) is left unchanged.
    flat = convert._facet_area_factor(np.array([cos_ref]), np.array([1.0]), cos_ref=cos_ref)
    assert flat[0] == pytest.approx(1.0)
    # A radar-facing, tilted facet (larger cos_lia, nz < 1): the gamma factor is
    # exactly the cosine factor scaled by the true-facet-area term nz, so it lies
    # below the ground-referenced cosine correction -- the extra darkening the
    # cosine model omits by ignoring the tilted facet's larger true area.
    cos_lia = np.array([0.95])
    nz = np.array([0.8])
    gamma = convert._facet_area_factor(cos_lia, nz, cos_ref=cos_ref)
    cosine = convert._terrain_flatten_factor(cos_lia, cos_ref=cos_ref)
    assert gamma[0] == pytest.approx(cosine[0] * 0.8, rel=1e-6)
    assert gamma[0] < cosine[0]
    # A DEM gap (non-finite cos_lia and/or nz) leaves the pixel unchanged.
    gap = convert._facet_area_factor(np.array([np.nan]), np.array([np.nan]), cos_ref=cos_ref)
    assert gap[0] == pytest.approx(1.0)
    # Shadow / near-zero cosine is floored and the factor clamped, so it cannot run
    # away.
    steep = convert._facet_area_factor(np.array([1e-6]), np.array([1.0]), cos_ref=cos_ref)
    assert steep[0] == pytest.approx(convert._RTC_FACTOR_MAX)


def test_sicd_to_geocoded_cog_rtc_gamma_flat_dem_leaves_values_unchanged(tmp_path, monkeypatch):
    rasterio = pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")
    np = pytest.importorskip("numpy")

    data = _fake_complex(12, 24)
    dem = _write_dem(tmp_path / "flat_dem.tif", kind="const", const=100.0)

    _patch_open_complex(monkeypatch, _FakeReader(data, _FakeSicd()))
    plain = convert.sicd_to_geocoded_cog(
        tmp_path / "in.ntf", tmp_path / "plain.tif", gcp_grid=6, resampling="nearest", dem=str(dem)
    )
    # On flat terrain nz == 1 and the local incidence equals the scene incidence
    # (the default reference), so every facet-area factor is 1 -> identical values.
    _patch_open_complex(monkeypatch, _FakeReader(data, _FakeSicd()))
    flattened = convert.sicd_to_geocoded_cog(
        tmp_path / "in.ntf",
        tmp_path / "rtc.tif",
        gcp_grid=6,
        resampling="nearest",
        dem=str(dem),
        rtc=True,
        rtc_model="gamma",
    )
    with rasterio.open(plain) as a, rasterio.open(flattened) as b:
        va, vb = a.read(1), b.read(1)
        both = np.isfinite(va) & np.isfinite(vb)
        assert both.any()
        assert np.allclose(va[both], vb[both], atol=1e-3)


def test_sicd_to_geocoded_cog_rtc_gamma_differs_from_cosine_and_area(tmp_path, monkeypatch):
    rasterio = pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")
    np = pytest.importorskip("numpy")

    data = _fake_complex(12, 24)
    # A steep east-west ramp (0..20 km over the footprint). The gamma model differs
    # from cosine only through the true-facet-area term nz = cos(slope), which is
    # second-order small, so a gentle slope would leave them indistinguishable; a
    # steep slope makes the nz term measurable while keeping the local incidence
    # well clear of the shadow/clamp regime.
    dem = _write_steep_ramp_dem(tmp_path / "steep_dem.tif")

    outs = {}
    for model in ("cosine", "area", "gamma"):
        _patch_open_complex(monkeypatch, _FakeReader(data, _FakeSicd()))
        outs[model] = convert.sicd_to_geocoded_cog(
            tmp_path / "in.ntf",
            tmp_path / f"{model}.tif",
            gcp_grid=6,
            resampling="nearest",
            dem=str(dem),
            rtc=True,
            rtc_model=model,
        )

    with (
        rasterio.open(outs["cosine"]) as c,
        rasterio.open(outs["area"]) as a,
        rasterio.open(outs["gamma"]) as g,
    ):
        vc, va, vg = c.read(1), a.read(1), g.read(1)
        both = np.isfinite(vc) & np.isfinite(va) & np.isfinite(vg)
        assert both.any()
        # Same geocoding; the gamma facet-area model must move a real slope by
        # measurably different amounts than either the cosine or the range-plane
        # area model (it adds the nz true-facet-area term neither carries).
        assert not np.allclose(vg[both], vc[both], atol=1e-3)
        assert not np.allclose(vg[both], va[both], atol=1e-3)


def test_cli_convert_rtc_gamma(tmp_path, monkeypatch):
    rasterio = pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    _patch_open_complex(monkeypatch, _FakeReader(_fake_complex(10, 12), _FakeSicd()))
    dem = _write_dem(tmp_path / "dem.tif")

    src = tmp_path / "scene.ntf"
    src.write_bytes(b"not-a-real-nitf")
    out = tmp_path / "geo.tif"
    result = CliRunner().invoke(
        cli_mod.cli,
        ["convert", str(src), str(out), "--dem", str(dem), "--rtc", "--rtc-model", "gamma"],
    )

    assert result.exit_code == 0, result.output
    assert "terrain-flattened" in result.output
    with rasterio.open(out) as ds:
        assert ds.crs.to_epsg() == 4326


# --------------------------------------------------------------------------- #
# The image-space illuminated-area RTC model (rtc_model="facet"). Unlike the
# three per-pixel models above it integrates: every facet is projected into the
# radar's own (slant range, azimuth) geometry and its illuminated area binned
# there, so terrain folded into one radar cell (layover) is normalised by the
# summed area of all of it. Over a planar slope it must reduce to the product of
# the area and gamma factors, which is what pins the arithmetic down here.
# --------------------------------------------------------------------------- #


def _range_ramp(slope: float, shape=(60, 60)):
    """A west-to-east elevation ramp rising ``slope`` metres per 1 m of ground."""
    import numpy as np

    h, w = shape
    return np.broadcast_to(np.arange(w, dtype="float64") * slope, (h, w)).copy()


def _facet_factor(dem, *, incidence_deg=40.0, azimuth_deg=90.0, reference_deg=None):
    normals = convert._terrain_normals(dem, x_res_deg=_DEG, y_res_deg=_DEG, top_lat=0.0)
    return convert._image_space_area_factor(
        dem,
        normals,
        x_res_deg=_DEG,
        y_res_deg=_DEG,
        top_lat=0.0,
        incidence_deg=incidence_deg,
        azimuth_deg=azimuth_deg,
        reference_deg=incidence_deg if reference_deg is None else reference_deg,
    )


def test_radar_coordinates_place_relief_toward_the_sensor():
    np = pytest.importorskip("numpy")
    # Radar to the east (azimuth 90): eastward ground is closer, so its slant
    # coordinate decreases along the row, and the azimuth coordinate is constant
    # along the range direction.
    flat = np.zeros((4, 6), dtype="float64")
    slant, along = convert._radar_coordinates(
        flat, x_res_deg=_DEG, y_res_deg=_DEG, top_lat=0.0, incidence_deg=40.0, azimuth_deg=90.0
    )
    assert np.all(np.diff(slant, axis=1) < 0)
    assert np.allclose(along - along[:, :1], 0.0)
    # A hill leans toward the sensor: same ground position, shorter slant range.
    hill = flat.copy()
    hill[2, 3] = 100.0
    raised, _ = convert._radar_coordinates(
        hill, x_res_deg=_DEG, y_res_deg=_DEG, top_lat=0.0, incidence_deg=40.0, azimuth_deg=90.0
    )
    assert raised[2, 3] < slant[2, 3]
    assert raised[2, 3] == pytest.approx(slant[2, 3] - 100.0 * math.cos(math.radians(40.0)))


def test_accumulate_radar_area_folds_coincident_facets():
    np = pytest.importorskip("numpy")
    # Two facets imaging into the same radar cell each read back the *sum* of
    # both areas -- the layover accumulation no per-pixel model can express --
    # while a facet on its own reads back only its own.
    slant = np.array([0.0, 0.0, 50.0])
    along = np.array([0.0, 0.0, 0.0])
    area = np.array([2.0, 3.0, 7.0])
    total = convert._accumulate_radar_area(slant, along, area, slant_bin=1.0, along_bin=1.0)
    assert total[0] == pytest.approx(5.0)
    assert total[1] == pytest.approx(5.0)
    assert total[2] == pytest.approx(7.0)


def test_accumulate_radar_area_coarsens_rather_than_exploding():
    np = pytest.importorskip("numpy")
    # Extreme relief stretches the slant-range axis, so bins sized for the ground
    # spacing would need far more cells than there are pixels. The bins coarsen
    # to stay inside the budget; two facets a million metres apart still land in
    # different cells, so each reads back its own area rather than the sum.
    slant = np.array([0.0, 1e6])
    along = np.array([0.0, 0.0])
    area = np.array([2.0, 3.0])
    total = convert._accumulate_radar_area(slant, along, area, slant_bin=1.0, along_bin=1.0)
    assert total[0] == pytest.approx(2.0, rel=0.05)
    assert total[1] == pytest.approx(3.0, rel=0.05)


def test_image_space_area_factor_flat_terrain_is_unchanged():
    np = pytest.importorskip("numpy")
    # The reference is the same integration over flat ground in the same
    # geometry, so the binning (and the scene edges) cancel exactly: flat terrain
    # at the scene incidence comes back at exactly one.
    factor = _facet_factor(np.zeros((40, 40), dtype="float64"))
    assert np.allclose(factor, 1.0)


def test_image_space_area_factor_ignores_azimuth_direction_slope():
    np = pytest.importorskip("numpy")
    # An east-west ramp seen from due north is a pure azimuth-direction slope: it
    # shears the radar geometry without compressing it and its facet-area and
    # projection terms cancel, so the integrated area per cell is unchanged.
    factor = _facet_factor(_range_ramp(0.35), azimuth_deg=0.0)
    inner = factor[12:-12, 12:-12]
    assert np.allclose(inner, 1.0, atol=1e-3)


@pytest.mark.parametrize("slope", [-0.35, -0.1, 0.1, 0.35])
def test_image_space_area_factor_planar_slope_is_area_times_gamma(slope):
    pytest.importorskip("numpy")
    # Over a planar range slope there is no folding, and the integration must
    # reduce to the closed form the two per-pixel models carry between them: the
    # range-compression factor (area) times the facet-area factor (gamma).
    inc, az = 40.0, 90.0
    dem = _range_ramp(slope)
    normals = convert._terrain_normals(dem, x_res_deg=_DEG, y_res_deg=_DEG, top_lat=0.0)
    theta = convert._range_local_incidence(normals, incidence_deg=inc, azimuth_deg=az)
    area = convert._foreshortening_factor(theta, sin_ref=math.sin(math.radians(inc)))
    cos_lia = convert._cos_local_incidence(normals, convert._look_unit_vector(inc, az))
    gamma = convert._facet_area_factor(cos_lia, normals[2], cos_ref=math.cos(math.radians(inc)))

    inner = (slice(12, -12), slice(12, -12))
    facet = _facet_factor(dem, incidence_deg=inc, azimuth_deg=az)[inner]
    assert facet.mean() == pytest.approx((area * gamma)[inner].mean(), rel=0.1)
    # An east-rising ramp leans away from an eastern radar (a back-slope), so it
    # is stretched over more radar cells and brightened; the sign must follow the
    # slope rather than be an artefact of the binning.
    assert bool(facet.mean() > 1.0) is (slope > 0)


def test_image_space_area_factor_suppresses_layover_the_per_pixel_models_miss():
    np = pytest.importorskip("numpy")
    # A steep radar-facing face whose returns fold onto the flat ground behind
    # it. The flat ground has no slope of its own, so every per-pixel model
    # leaves it alone; the integration sees the face's area land in the same
    # radar cells and suppresses both together.
    inc, az = 40.0, 90.0
    profile = np.zeros(60, dtype="float64")
    face = np.arange(10, dtype="float64")
    profile[20:30] = (9.0 - face) * 2.0  # descends eastward: faces the sensor
    dem = np.broadcast_to(profile, (60, 60)).copy()

    factor = _facet_factor(dem, incidence_deg=inc, azimuth_deg=az)
    normals = convert._terrain_normals(dem, x_res_deg=_DEG, y_res_deg=_DEG, top_lat=0.0)
    cos_lia = convert._cos_local_incidence(normals, convert._look_unit_vector(inc, az))
    gamma = convert._facet_area_factor(cos_lia, normals[2], cos_ref=math.cos(math.radians(inc)))

    behind = (slice(20, 40), slice(32, 42))  # flat ground east of the face
    assert np.allclose(gamma[behind], 1.0)  # per-pixel: nothing to correct
    assert factor[behind].max() < 0.9  # integrated: shared with the folded face


def test_image_space_area_factor_reference_angle_offsets_a_flat_scene():
    np = pytest.importorskip("numpy")
    # Like the other models, a reference angle other than the scene incidence
    # re-references flat terrain by a constant -- here tan(scene)/tan(reference),
    # the composition of the two per-pixel models' reference ratios.
    factor = _facet_factor(
        np.zeros((30, 30), dtype="float64"), incidence_deg=40.0, reference_deg=30.0
    )
    expected = math.tan(math.radians(40.0)) / math.tan(math.radians(30.0))
    assert np.allclose(factor, expected)


def test_sicd_to_geocoded_cog_rtc_facet_flat_dem_leaves_values_unchanged(tmp_path, monkeypatch):
    rasterio = pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")
    np = pytest.importorskip("numpy")

    data = _fake_complex(12, 24)
    dem = _write_dem(tmp_path / "flat_dem.tif", kind="const", const=100.0)

    _patch_open_complex(monkeypatch, _FakeReader(data, _FakeSicd()))
    plain = convert.sicd_to_geocoded_cog(
        tmp_path / "in.ntf", tmp_path / "plain.tif", gcp_grid=6, resampling="nearest", dem=str(dem)
    )
    _patch_open_complex(monkeypatch, _FakeReader(data, _FakeSicd()))
    flattened = convert.sicd_to_geocoded_cog(
        tmp_path / "in.ntf",
        tmp_path / "rtc.tif",
        gcp_grid=6,
        resampling="nearest",
        dem=str(dem),
        rtc=True,
        rtc_model="facet",
    )
    with rasterio.open(plain) as a, rasterio.open(flattened) as b:
        va, vb = a.read(1), b.read(1)
        both = np.isfinite(va) & np.isfinite(vb)
        assert both.any()
        assert np.allclose(va[both], vb[both], atol=1e-3)


def test_sicd_to_geocoded_cog_rtc_facet_differs_from_the_per_pixel_models(tmp_path, monkeypatch):
    rasterio = pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")
    np = pytest.importorskip("numpy")

    data = _fake_complex(12, 24)
    dem = _write_steep_ramp_dem(tmp_path / "steep_dem.tif")

    outs = {}
    for model in ("cosine", "area", "gamma", "facet"):
        _patch_open_complex(monkeypatch, _FakeReader(data, _FakeSicd()))
        outs[model] = convert.sicd_to_geocoded_cog(
            tmp_path / "in.ntf",
            tmp_path / f"{model}.tif",
            gcp_grid=6,
            resampling="nearest",
            dem=str(dem),
            rtc=True,
            rtc_model=model,
        )

    with (
        rasterio.open(outs["cosine"]) as c,
        rasterio.open(outs["area"]) as a,
        rasterio.open(outs["gamma"]) as g,
        rasterio.open(outs["facet"]) as f,
    ):
        vc, va, vg, vf = c.read(1), a.read(1), g.read(1), f.read(1)
        both = np.isfinite(vc) & np.isfinite(va) & np.isfinite(vg) & np.isfinite(vf)
        assert both.any()
        # The integration is a different measurement from all three per-pixel
        # models, not a rescaling of any of them.
        for other in (vc, va, vg):
            assert not np.allclose(vf[both], other[both], atol=1e-3)


def test_cli_convert_rtc_facet(tmp_path, monkeypatch):
    rasterio = pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    _patch_open_complex(monkeypatch, _FakeReader(_fake_complex(10, 12), _FakeSicd()))
    dem = _write_dem(tmp_path / "dem.tif")

    src = tmp_path / "scene.ntf"
    src.write_bytes(b"not-a-real-nitf")
    out = tmp_path / "geo.tif"
    result = CliRunner().invoke(
        cli_mod.cli,
        ["convert", str(src), str(out), "--dem", str(dem), "--rtc", "--rtc-model", "facet"],
    )

    assert result.exit_code == 0, result.output
    assert "terrain-flattened" in result.output
    with rasterio.open(out) as ds:
        assert ds.crs.to_epsg() == 4326


def test_cli_convert_rtc_area(tmp_path, monkeypatch):
    rasterio = pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    _patch_open_complex(monkeypatch, _FakeReader(_fake_complex(10, 12), _FakeSicd()))
    dem = _write_dem(tmp_path / "dem.tif")

    src = tmp_path / "scene.ntf"
    src.write_bytes(b"not-a-real-nitf")
    out = tmp_path / "geo.tif"
    result = CliRunner().invoke(
        cli_mod.cli,
        ["convert", str(src), str(out), "--dem", str(dem), "--rtc", "--rtc-model", "area"],
    )

    assert result.exit_code == 0, result.output
    assert "terrain-flattened" in result.output
    with rasterio.open(out) as ds:
        assert ds.crs.to_epsg() == 4326


# --------------------------------------------------------------------------- #
# Radiometric calibration (SICD ``Radiometric`` scale-factor polynomials).
#
# Terrain flattening removes the geometry from a pixel's brightness but leaves
# it in the product's own arbitrary units. Calibration is what makes the number
# physical, and it is only ever as real as the metadata behind it -- so the
# tests pin both halves: the arithmetic (a power-domain scale evaluated in
# SICD's image coordinates, composing with RTC) and the refusal (a product with
# no scale factor says so instead of emitting a calibrated-looking number).
# --------------------------------------------------------------------------- #


class _FakePoly:
    """Stand-in for a sarpy ``Poly2DType``: just its coefficient array."""

    def __init__(self, coefs):
        self.Coefs = coefs


def _radiometric(**polys):
    """A SICD ``Radiometric`` block carrying only the named SF polynomials."""

    block = type("_FakeRadiometric", (), {})()
    for name, coefs in polys.items():
        setattr(block, name, _FakePoly(coefs))
    return block


def test_available_calibrations_reports_only_what_the_metadata_carries():
    # The default fake scene has no Radiometric block at all -- the uncalibrated
    # case Umbra's open products are in.
    assert convert._available_calibrations(_FakeSicd()) == ()

    sicd = _FakeSicd(radiometric=_radiometric(SigmaZeroSFPoly=[[2.0]], RCSSFPoly=[[3.0]]))
    assert convert._available_calibrations(sicd) == ("sigma0", "rcs")


def test_calibration_coefficients_missing_block_raises_and_says_why():
    with pytest.raises(ValueError, match="no Radiometric metadata"):
        convert._calibration_coefficients(_FakeSicd(), "sigma0")


def test_calibration_coefficients_missing_poly_names_what_is_available():
    sicd = _FakeSicd(radiometric=_radiometric(SigmaZeroSFPoly=[[2.0]]))
    with pytest.raises(ValueError, match="available: sigma0"):
        convert._calibration_coefficients(sicd, "gamma0")


def test_calibration_coefficients_rejects_an_unknown_kind():
    with pytest.raises(ValueError, match="Unknown calibration"):
        convert._calibration_coefficients(_FakeSicd(), "sigma_nought")


def test_calibration_coefficients_rejects_an_empty_poly():
    pytest.importorskip("numpy")
    sicd = _FakeSicd(radiometric=_radiometric(SigmaZeroSFPoly=[]))
    with pytest.raises(ValueError, match="no coefficients"):
        convert._calibration_coefficients(sicd, "sigma0")


def test_calibration_coefficients_accepts_a_bare_array_or_scalar():
    np = pytest.importorskip("numpy")
    # sarpy hands back a Poly2DType, but a bare array (or scalar) is enough for
    # the pure core, which is what keeps it testable without sarpy.
    sicd = _FakeSicd(radiometric=type("_R", (), {"BetaZeroSFPoly": 7.0})())
    coefs = convert._calibration_coefficients(sicd, "beta0")
    assert coefs.shape == (1, 1)
    assert np.allclose(coefs, 7.0)


def test_image_grid_geometry_reads_spacings_scp_and_chip_origin():
    sicd = _FakeSicd(row_ss=0.5, col_ss=0.25, scp_row=5.0, scp_col=10.0, first_row=3, first_col=4)
    assert convert._image_grid_geometry(sicd) == {
        "row_ss": 0.5,
        "col_ss": 0.25,
        "scp_row": 5.0,
        "scp_col": 10.0,
        "first_row": 3.0,
        "first_col": 4.0,
    }


def test_image_grid_geometry_raises_without_the_grid_the_polys_live_in():
    sicd = _FakeSicd()
    sicd.Grid = None
    with pytest.raises(ValueError, match="Grid.Row.SS"):
        convert._image_grid_geometry(sicd)


def test_calibration_scale_constant_poly_is_a_flat_scale():
    np = pytest.importorskip("numpy")
    scale = convert._calibration_scale(
        [[4.0]], (3, 5), row_ss=0.5, col_ss=0.25, scp_row=1.0, scp_col=2.0
    )
    assert scale.shape == (3, 5)
    assert np.allclose(scale, 4.0)


def test_calibration_scale_is_evaluated_in_metres_from_the_scp():
    np = pytest.importorskip("numpy")
    # sf(x, y) = 4 + 2*x + 1*y, with x/y the row/col offsets from the SCP in
    # metres -- so the coefficient matrix is [[4, 1], [2, 0]].
    coefs = [[4.0, 1.0], [2.0, 0.0]]
    scale = convert._calibration_scale(
        coefs, (3, 4), row_ss=0.5, col_ss=0.25, scp_row=1.0, scp_col=2.0
    )
    rows = (np.arange(3) - 1.0) * 0.5
    cols = (np.arange(4) - 2.0) * 0.25
    expected = 4.0 + 2.0 * rows[:, None] + 1.0 * cols[None, :]
    assert np.allclose(scale, expected)


def test_calibration_scale_offsets_a_chipped_image_by_its_origin():
    np = pytest.importorskip("numpy")
    # A chip's SCP is quoted against the *full* grid, so FirstRow/FirstCol move
    # the whole evaluation -- getting this wrong tilts the calibration.
    coefs = [[1.0, 0.0], [2.0, 0.0]]
    full = convert._calibration_scale(
        coefs, (6, 2), row_ss=1.0, col_ss=1.0, scp_row=0.0, scp_col=0.0
    )
    chip = convert._calibration_scale(
        coefs, (3, 2), row_ss=1.0, col_ss=1.0, scp_row=0.0, scp_col=0.0, first_row=3
    )
    assert np.allclose(chip, full[3:])


def test_calibration_scale_rejects_a_non_positive_scale_factor():
    pytest.importorskip("numpy")
    # A scale factor is a positive power ratio by construction; a polynomial
    # that goes non-positive over the image is broken metadata, and repairing it
    # silently would hand back a calibrated-looking number.
    coefs = [[1.0, 0.0], [1.0, 0.0]]  # sf = 1 + x, negative for x < -1
    with pytest.raises(ValueError, match="non-positive or non-finite"):
        convert._calibration_scale(coefs, (10, 2), row_ss=1.0, col_ss=1.0, scp_row=9.0, scp_col=0.0)


def test_apply_calibration_matches_the_power_domain_convention():
    np = pytest.importorskip("numpy")
    scale = np.array([[100.0, 100.0]], dtype="float64")

    db = np.array([[10.0, np.nan]], dtype="float32")
    out_db = convert._apply_calibration(db, scale, decibels=True)
    assert out_db[0, 0] == pytest.approx(10.0 + 20.0, rel=1e-5)  # 10*log10(100)
    assert np.isnan(out_db[0, 1])

    lin = np.array([[3.0, np.nan]], dtype="float32")
    out_lin = convert._apply_calibration(lin, scale, decibels=False)
    # Linear output is calibrated *amplitude*: square it for the coefficient.
    assert out_lin[0, 0] == pytest.approx(3.0 * 10.0, rel=1e-5)
    assert np.isnan(out_lin[0, 1])

    # The two paths are the same measurement in different units: calibrating a
    # linear raster and then taking its decibels gives what calibrating the
    # decibel raster gave.
    same_scene_db = np.array([[20.0 * math.log10(3.0)]], dtype="float32")
    assert 20.0 * math.log10(float(out_lin[0, 0])) == pytest.approx(
        float(convert._apply_calibration(same_scene_db, scale[:, :1], decibels=True)[0, 0]),
        rel=1e-5,
    )


def test_sicd_to_amplitude_geotiff_calibration_scales_the_slant_plane(tmp_path, monkeypatch):
    rasterio = pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")
    np = pytest.importorskip("numpy")

    data = _fake_complex(6, 8)
    _patch_open_complex(monkeypatch, _FakeReader(data, _FakeSicd()))
    plain = convert.sicd_to_amplitude_geotiff(tmp_path / "in.ntf", tmp_path / "plain.tif")

    sicd = _FakeSicd(radiometric=_radiometric(SigmaZeroSFPoly=[[100.0]]))
    _patch_open_complex(monkeypatch, _FakeReader(data, sicd))
    calibrated = convert.sicd_to_amplitude_geotiff(
        tmp_path / "in.ntf", tmp_path / "cal.tif", calibration="sigma0"
    )

    with rasterio.open(plain) as a, rasterio.open(calibrated) as b:
        assert np.allclose(b.read(1), a.read(1) + 20.0, atol=1e-4)


def test_sicd_to_amplitude_geotiff_rejects_an_unknown_calibration(tmp_path, monkeypatch):
    pytest.importorskip("sarpy")
    _patch_open_complex(monkeypatch, _FakeReader(_fake_complex(4, 4), _FakeSicd()))
    with pytest.raises(ValueError, match="Unknown calibration"):
        convert.sicd_to_amplitude_geotiff(
            tmp_path / "in.ntf", tmp_path / "out.tif", calibration="sigma"
        )


def test_sicd_to_geocoded_cog_calibration_offsets_the_scene(tmp_path, monkeypatch):
    rasterio = pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")
    np = pytest.importorskip("numpy")

    data = _fake_complex(10, 12)
    _patch_open_complex(monkeypatch, _FakeReader(data, _FakeSicd()))
    plain = convert.sicd_to_geocoded_cog(
        tmp_path / "in.ntf", tmp_path / "plain.tif", gcp_grid=6, resampling="nearest"
    )

    sicd = _FakeSicd(radiometric=_radiometric(GammaZeroSFPoly=[[0.01]]))
    _patch_open_complex(monkeypatch, _FakeReader(data, sicd))
    calibrated = convert.sicd_to_geocoded_cog(
        tmp_path / "in.ntf",
        tmp_path / "cal.tif",
        gcp_grid=6,
        resampling="nearest",
        calibration="gamma0",
    )

    with rasterio.open(plain) as a, rasterio.open(calibrated) as b:
        va, vb = a.read(1), b.read(1)
        both = np.isfinite(va) & np.isfinite(vb)
        assert both.any()
        # 10*log10(0.01) == -20 dB, applied before the warp.
        assert np.allclose(vb[both], va[both] - 20.0, atol=1e-4)


def test_sicd_to_geocoded_cog_calibration_composes_with_rtc(tmp_path, monkeypatch):
    rasterio = pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")
    np = pytest.importorskip("numpy")

    data = _fake_complex(10, 12)
    dem = _write_dem(tmp_path / "dem.tif")

    _patch_open_complex(monkeypatch, _FakeReader(data, _FakeSicd()))
    flattened = convert.sicd_to_geocoded_cog(
        tmp_path / "in.ntf",
        tmp_path / "rtc.tif",
        gcp_grid=6,
        resampling="nearest",
        dem=str(dem),
        rtc=True,
        rtc_model="facet",
    )

    sicd = _FakeSicd(radiometric=_radiometric(GammaZeroSFPoly=[[1000.0]]))
    _patch_open_complex(monkeypatch, _FakeReader(data, sicd))
    both_ways = convert.sicd_to_geocoded_cog(
        tmp_path / "in.ntf",
        tmp_path / "rtc_cal.tif",
        gcp_grid=6,
        resampling="nearest",
        dem=str(dem),
        rtc=True,
        rtc_model="facet",
        calibration="gamma0",
    )

    with rasterio.open(flattened) as a, rasterio.open(both_ways) as b:
        va, vb = a.read(1), b.read(1)
        finite = np.isfinite(va) & np.isfinite(vb)
        assert finite.any()
        # Both are power-domain factors, so terrain-flattened gamma-nought is
        # exactly the flattened scene plus the calibration offset (30 dB).
        assert np.allclose(vb[finite], va[finite] + 30.0, atol=1e-4)


def test_sicd_to_geocoded_cog_uncalibrated_product_raises(tmp_path, monkeypatch):
    pytest.importorskip("sarpy")
    _patch_open_complex(monkeypatch, _FakeReader(_fake_complex(6, 6), _FakeSicd()))
    with pytest.raises(ValueError, match="no Radiometric metadata"):
        convert.sicd_to_geocoded_cog(
            tmp_path / "in.ntf", tmp_path / "out.tif", gcp_grid=4, calibration="sigma0"
        )


def test_sicd_to_geocoded_cog_rejects_an_unknown_calibration_before_reading(tmp_path):
    pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")
    # Validated ahead of the file read, like the rtc_model check beside it.
    with pytest.raises(ValueError, match="Unknown calibration"):
        convert.sicd_to_geocoded_cog(
            tmp_path / "missing.ntf", tmp_path / "out.tif", calibration="gamma"
        )


def test_sicd_calibration_types_reports_the_products_own_metadata(tmp_path, monkeypatch):
    pytest.importorskip("sarpy")

    _patch_open_complex(monkeypatch, _FakeReader(_fake_complex(4, 4), _FakeSicd()))
    assert convert.sicd_calibration_types(tmp_path / "in.ntf") == ()

    sicd = _FakeSicd(radiometric=_radiometric(SigmaZeroSFPoly=[[1.0]], GammaZeroSFPoly=[[1.0]]))
    _patch_open_complex(monkeypatch, _FakeReader(_fake_complex(4, 4), sicd))
    assert convert.sicd_calibration_types(tmp_path / "in.ntf") == ("sigma0", "gamma0")


def test_cli_convert_calibrate(tmp_path, monkeypatch):
    rasterio = pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    sicd = _FakeSicd(radiometric=_radiometric(SigmaZeroSFPoly=[[10.0]]))
    _patch_open_complex(monkeypatch, _FakeReader(_fake_complex(10, 12), sicd))

    src = tmp_path / "scene.ntf"
    src.write_bytes(b"not-a-real-nitf")
    out = tmp_path / "geo.tif"
    result = CliRunner().invoke(
        cli_mod.cli, ["convert", str(src), str(out), "--calibrate", "sigma0"]
    )

    assert result.exit_code == 0, result.output
    assert "sigma0-calibrated" in result.output
    with rasterio.open(out) as ds:
        assert ds.crs.to_epsg() == 4326


def test_cli_convert_calibrate_slant_plane(tmp_path, monkeypatch):
    pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    sicd = _FakeSicd(radiometric=_radiometric(RCSSFPoly=[[10.0]]))
    _patch_open_complex(monkeypatch, _FakeReader(_fake_complex(6, 6), sicd))

    src = tmp_path / "scene.ntf"
    src.write_bytes(b"not-a-real-nitf")
    result = CliRunner().invoke(
        cli_mod.cli,
        ["convert", str(src), str(tmp_path / "amp.tif"), "--slant-plane", "--calibrate", "rcs"],
    )

    assert result.exit_code == 0, result.output
    assert "rcs-calibrated" in result.output


def test_cli_convert_calibrate_unavailable_is_a_clean_error(tmp_path, monkeypatch):
    pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    # The default fake scene carries no Radiometric block -- the shape of an
    # Umbra open product -- so the CLI must explain, not traceback.
    _patch_open_complex(monkeypatch, _FakeReader(_fake_complex(8, 8), _FakeSicd()))

    src = tmp_path / "scene.ntf"
    src.write_bytes(b"not-a-real-nitf")
    result = CliRunner().invoke(
        cli_mod.cli, ["convert", str(src), str(tmp_path / "geo.tif"), "--calibrate", "sigma0"]
    )

    assert result.exit_code != 0
    assert "Radiometric" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


# --------------------------------------------------------------------------- #
# Conversion provenance (what the pixel values mean, recorded in the raster).
# --------------------------------------------------------------------------- #


def test_conversion_tags_record_every_processing_choice():
    tags = convert.conversion_tags(
        source="/home/analyst/scenes/scene_SICD.nitf",
        geocoded=True,
        decibels=True,
        calibration="gamma0",
        rtc_model="facet",
        rtc_reference_deg=31.25,
        projection_type="HAE",
        dem="/cache/glo30_mosaic.tif",
        geoid="/cache/us_nga_egm96_15.tif",
        resampling="bilinear",
    )

    assert tags["UMBRA_CALIBRATION"] == "gamma0"
    assert tags["UMBRA_RTC_MODEL"] == "facet"
    # The *resolved* reference angle, not the request (None means "scene angle").
    assert tags["UMBRA_RTC_REFERENCE_DEG"] == "31.25"
    assert tags["UMBRA_SCALE"] == "decibels"
    assert tags["UMBRA_UNITS"] == "dB (gamma0)"
    assert tags["UMBRA_CONVERSION"] == "geocoded"
    assert tags["UMBRA_PROJECTION"] == "DEM"  # a DEM supersedes projection_type
    assert tags["UMBRA_DEM"] == "glo30_mosaic.tif"
    assert tags["UMBRA_GEOID"] == "us_nga_egm96_15.tif"
    assert tags["UMBRA_RESAMPLING"] == "bilinear"
    # Only the file name: the local directory is not provenance, and travels.
    assert tags["UMBRA_SOURCE"] == "scene_SICD.nitf"
    assert "analyst" not in "".join(tags.values())
    # The licence survives this transformation like every other one.
    assert tags["UMBRA_LICENSE"] == "CC-BY-4.0"
    assert "CC BY 4.0" in tags["UMBRA_ATTRIBUTION"]
    assert all(key.startswith("UMBRA_") for key in tags)
    assert all(isinstance(value, str) for value in tags.values())


def test_conversion_tags_report_the_steps_that_did_not_run():
    tags = convert.conversion_tags(source="scene.nitf", geocoded=True, resampling="cubic")

    # "none" rather than a missing key, so an absent tag never has to be read
    # as "unknown" *or* "not applied".
    assert tags["UMBRA_CALIBRATION"] == "none"
    assert tags["UMBRA_RTC_MODEL"] == "none"
    assert tags["UMBRA_DEM"] == "none"
    assert tags["UMBRA_GEOID"] == "none"
    assert tags["UMBRA_PROJECTION"] == "HAE"  # the flat-earth default
    assert tags["UMBRA_UNITS"] == "dB (relative amplitude)"
    # No flattening ran, so there is no reference angle to record.
    assert "UMBRA_RTC_REFERENCE_DEG" not in tags


def test_conversion_tags_omit_geocoding_keys_for_the_slant_plane():
    tags = convert.conversion_tags(
        source="scene.nitf", geocoded=False, decibels=False, calibration="sigma0"
    )

    assert tags["UMBRA_CONVERSION"] == "slant-plane"
    assert tags["UMBRA_SCALE"] == "linear"
    # Linear + calibrated is the amplitude whose *square* is the coefficient.
    assert tags["UMBRA_UNITS"] == "amplitude (sqrt sigma0)"
    for key in ("UMBRA_PROJECTION", "UMBRA_DEM", "UMBRA_GEOID", "UMBRA_RESAMPLING"):
        assert key not in tags


def test_warp_gcps_to_cog_carries_tags_through_the_cog_copy(tmp_path):
    pytest.importorskip("rasterio")
    np = pytest.importorskip("numpy")

    rows, cols = 12, 16
    amp = np.ones((rows, cols), dtype="float32")
    tags = convert.conversion_tags(source="scene.nitf", geocoded=True, calibration="beta0")

    out = convert._warp_gcps_to_cog(
        amp,
        _hand_gcps(rows, cols),
        tmp_path / "geo.tif",
        resolution=0.01,
        resampling="nearest",
        nodata=float("nan"),
        tags=tags,
    )

    # The COG driver copies the dataset through a MemoryFile, so the tags have
    # to survive that copy to reach the file a user actually gets.
    assert convert.read_conversion_tags(out)["calibration"] == "beta0"


def test_sicd_to_geocoded_cog_records_how_it_was_made(tmp_path, monkeypatch):
    pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")

    dem = _write_dem(tmp_path / "flat_dem.tif", kind="const", const=100.0)
    sicd = _FakeSicd(radiometric=_radiometric(GammaZeroSFPoly=[[0.01]]), incidence=37.5)
    _patch_open_complex(monkeypatch, _FakeReader(_fake_complex(10, 12), sicd))

    out = convert.sicd_to_geocoded_cog(
        tmp_path / "scene_SICD.nitf",
        tmp_path / "geo.tif",
        gcp_grid=6,
        resampling="nearest",
        dem=str(dem),
        rtc=True,
        rtc_model="gamma",
        calibration="gamma0",
    )

    recorded = convert.read_conversion_tags(out)
    assert recorded["source"] == "scene_SICD.nitf"
    assert recorded["calibration"] == "gamma0"
    assert recorded["rtc_model"] == "gamma"
    # No reference angle was asked for, so the scene incidence is what ran.
    assert recorded["rtc_reference_deg"] == "37.5"
    assert recorded["dem"] == "flat_dem.tif"
    assert recorded["projection"] == "DEM"
    assert recorded["units"] == "dB (gamma0)"
    assert recorded["software"].startswith("umbra-py ")


def test_sicd_to_amplitude_geotiff_records_the_slant_plane_conversion(tmp_path, monkeypatch):
    pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")

    _patch_open_complex(monkeypatch, _FakeReader(_fake_complex(8, 10), _FakeSicd()))
    out = convert.sicd_to_amplitude_geotiff(
        tmp_path / "scene_SICD.nitf", tmp_path / "amp.tif", decibels=False
    )

    recorded = convert.read_conversion_tags(out)
    assert recorded["conversion"] == "slant-plane"
    assert recorded["scale"] == "linear"
    assert recorded["calibration"] == "none"
    assert "projection" not in recorded


def test_read_conversion_tags_is_empty_for_a_foreign_raster(tmp_path):
    pytest.importorskip("rasterio")

    dem = _write_dem(tmp_path / "dem.tif", kind="const")
    assert convert.read_conversion_tags(dem) == {}


def test_conversion_provenance_parses_a_tag_set_without_reopening_the_raster():
    # The parsing half read_conversion_tags delegates to, so a caller that
    # already holds the dataset (to_stack, reading every source's record while
    # it resolves the shared grid) does not pay a second round of range reads.
    tags = convert.conversion_tags(
        source="/private/path/scene.nitf", geocoded=True, calibration="gamma0"
    )
    parsed = convert.conversion_provenance({**tags, "AREA_OR_POINT": "Area"})

    assert parsed["calibration"] == "gamma0"
    assert parsed["source"] == "scene.nitf"
    # Foreign tags are not provenance, and nothing keeps the UMBRA_ prefix.
    assert "area_or_point" not in parsed
    assert not any(key.startswith(convert.PROVENANCE_TAG_PREFIX) for key in parsed)
    assert convert.conversion_provenance({"AREA_OR_POINT": "Area"}) == {}


def test_cli_convert_provenance_prints_json(tmp_path, monkeypatch):
    pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")
    import json

    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    _patch_open_complex(monkeypatch, _FakeReader(_fake_complex(8, 10), _FakeSicd()))
    src = tmp_path / "scene.ntf"
    src.write_bytes(b"not-a-real-nitf")
    out = tmp_path / "geo.tif"
    runner = CliRunner()
    assert runner.invoke(cli_mod.cli, ["convert", str(src), str(out)]).exit_code == 0

    result = runner.invoke(cli_mod.cli, ["convert", str(out), "--provenance"])

    assert result.exit_code == 0, result.output
    recorded = json.loads(result.output)
    assert recorded["conversion"] == "geocoded"
    assert recorded["calibration"] == "none"
    assert recorded["license"] == "CC-BY-4.0"


def test_cli_convert_provenance_rejects_a_destination(tmp_path, monkeypatch):
    pytest.importorskip("rasterio")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    src = tmp_path / "scene.tif"
    src.write_bytes(b"not-a-real-tif")
    result = CliRunner().invoke(
        cli_mod.cli, ["convert", str(src), str(tmp_path / "out.tif"), "--provenance"]
    )

    assert result.exit_code != 0
    assert "writes nothing" in result.output


def test_cli_convert_provenance_on_an_unconverted_raster_errors(tmp_path):
    pytest.importorskip("rasterio")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    dem = _write_dem(tmp_path / "dem.tif", kind="const")
    result = CliRunner().invoke(cli_mod.cli, ["convert", str(dem), "--provenance"])

    assert result.exit_code != 0
    assert "no umbra-py conversion provenance" in result.output


def test_cli_convert_without_a_destination_errors(tmp_path):
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    src = tmp_path / "scene.ntf"
    src.write_bytes(b"not-a-real-nitf")
    result = CliRunner().invoke(cli_mod.cli, ["convert", str(src)])

    assert result.exit_code != 0
    assert "DST" in result.output
