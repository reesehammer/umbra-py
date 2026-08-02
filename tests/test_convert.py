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

import json
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
    """A sarpy-shaped reader that honours slicing, so a clipped read is visible.

    ``reads`` records every key the converter asked for: whole-scene conversion
    asks for ``[:, :]``, a clipped one asks for the window it resolved, which is
    the difference the clip is *for*.
    """

    def __init__(self, complex_data, sicd):
        self._data = complex_data
        self._sicd = sicd
        self.reads: list[tuple] = []

    @property
    def data_size(self):
        return self._data.shape

    def __getitem__(self, key):  # reader[:, :] or reader[r0:r1, c0:c1]
        self.reads.append(key)
        return self._data[key]

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
    from umbra_py.exceptions import UnsupportedMeasurementError

    sicd = _FakeSicd(incidence=42.0, azimuth=170.0)
    assert convert._scene_look_geometry(sicd) == (42.0, 170.0)

    class _Bare:
        SCPCOA = None

    # A product that does not state its geometry is the same *kind* of refusal as
    # a product that carries no Radiometric block: a fact about the file, which is
    # what `--skip-unsupported` may skip and `umbra preflight --rtc` may find over
    # the wire. It stays a ValueError, so any caller that caught one still does.
    with pytest.raises(UnsupportedMeasurementError, match="SCPCOA") as excinfo:
        convert._scene_look_geometry(_Bare())
    assert isinstance(excinfo.value, ValueError)
    assert excinfo.value.hint and "--rtc" in excinfo.value.hint


def test_check_measurement_support_asks_the_geometry_only_when_rtc_is_asked_for():
    from umbra_py.exceptions import UnsupportedMeasurementError

    class _Bare:
        SCPCOA = None

    # Not asked for: nothing about the geometry is checked, so a product that
    # states none converts exactly as it did.
    convert._check_measurement_support(
        _Bare(), calibration=None, noise_subtract=False, noise_model="measured"
    )
    with pytest.raises(UnsupportedMeasurementError, match="SCPCOA"):
        convert._check_measurement_support(
            _Bare(), calibration=None, noise_subtract=False, noise_model="measured", rtc=True
        )
    # And a product that does state it passes the same check.
    convert._check_measurement_support(
        _FakeSicd(), calibration=None, noise_subtract=False, noise_model="measured", rtc=True
    )


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
# Noise-floor subtraction (SICD ``Radiometric.NoiseLevel``).
#
# Calibration makes a pixel physical; it does not make it the *ground*. A
# measured pixel is the echo plus the receiver's own thermal noise, and over a
# dark surface the second term dominates -- so these tests pin the arithmetic
# (subtraction in the power domain, before the multiplicative corrections, at
# the image coordinates the window came from) and the refusals (a product that
# cannot state its floor says so rather than having one subtracted for it).
# --------------------------------------------------------------------------- #


def _noise_level(level="ABSOLUTE", coefs=((0.0,),), *, poly=True):
    """A SICD ``Radiometric.NoiseLevel`` block: a level type and its NoisePoly."""
    block = type("_FakeNoiseLevel", (), {})()
    block.NoiseLevelType = level
    if poly:
        block.NoisePoly = _FakePoly(coefs)
    return block


def _radiometric_noise(level="ABSOLUTE", coefs=((0.0,),), *, poly=True, **polys):
    """A ``Radiometric`` block carrying a NoiseLevel (and any SF polynomials)."""
    block = _radiometric(**polys)
    block.NoiseLevel = _noise_level(level, coefs, poly=poly)
    return block


def test_noise_level_type_reports_what_the_product_declares():
    # No Radiometric block at all -- the shape of an Umbra open product.
    assert convert._noise_level_type(_FakeSicd()) is None
    # Radiometric, but nothing said about the floor.
    assert convert._noise_level_type(_FakeSicd(radiometric=_radiometric())) is None
    assert convert._noise_level_type(_FakeSicd(radiometric=_radiometric_noise())) == "ABSOLUTE"
    assert (
        convert._noise_level_type(_FakeSicd(radiometric=_radiometric_noise("relative")))
        == "RELATIVE"
    )


def test_noise_coefficients_missing_block_raises_and_says_why():
    with pytest.raises(ValueError, match="no Radiometric metadata"):
        convert._noise_coefficients(_FakeSicd())


def test_noise_coefficients_without_a_noise_level_raises():
    sicd = _FakeSicd(radiometric=_radiometric(SigmaZeroSFPoly=[[2.0]]))
    with pytest.raises(ValueError, match="no NoiseLevel block"):
        convert._noise_coefficients(sicd)


def test_noise_coefficients_refuses_a_relative_noise_level():
    # The whole point: a relative level says how the floor *varies*, not what it
    # is, so subtracting it would mean inventing the offset -- and the result
    # would look exactly like a real measurement.
    sicd = _FakeSicd(radiometric=_radiometric_noise("RELATIVE"))
    with pytest.raises(ValueError, match="relative noise level"):
        convert._noise_coefficients(sicd)


def test_noise_coefficients_absolute_level_without_a_poly_raises():
    sicd = _FakeSicd(radiometric=_radiometric_noise(poly=False))
    with pytest.raises(ValueError, match="no NoisePoly"):
        convert._noise_coefficients(sicd)


def test_noise_coefficients_rejects_an_empty_poly():
    pytest.importorskip("numpy")
    sicd = _FakeSicd(radiometric=_radiometric_noise(coefs=[]))
    with pytest.raises(ValueError, match="no coefficients"):
        convert._noise_coefficients(sicd)


def test_noise_power_converts_the_decibel_polynomial_to_linear_power():
    np = pytest.importorskip("numpy")
    # NoisePoly is quoted in dB, and noise adds in linear power -- getting this
    # conversion wrong subtracts a number in the wrong units entirely.
    noise = convert._noise_power(
        [[-20.0]], (3, 4), row_ss=1.0, col_ss=1.0, scp_row=0.0, scp_col=0.0
    )
    assert noise.shape == (3, 4)
    assert np.allclose(noise, 0.01)


def test_noise_power_tracks_the_across_swath_variation():
    np = pytest.importorskip("numpy")
    # A floor that rises across the image is the case a single scalar would
    # smear: evaluated in metres from the SCP, like the scale factors.
    coefs = [[-30.0, 2.0], [0.0, 0.0]]  # noise_db = -30 + 2*y
    noise = convert._noise_power(coefs, (2, 5), row_ss=1.0, col_ss=0.5, scp_row=0.0, scp_col=1.0)
    cols = (np.arange(5) - 1.0) * 0.5
    assert np.allclose(noise, 10.0 ** ((-30.0 + 2.0 * cols[None, :]) / 10.0))


def test_noise_power_rejects_a_non_finite_polynomial():
    np = pytest.importorskip("numpy")
    with pytest.raises(ValueError, match="non-finite"):
        convert._noise_power([[np.inf]], (2, 2), row_ss=1.0, col_ss=1.0, scp_row=0.0, scp_col=0.0)


def test_subtract_noise_is_a_power_domain_subtraction_in_both_scales():
    np = pytest.importorskip("numpy")
    noise = np.full((1, 2), 0.25)

    # Linear magnitude: power is the square, so sqrt(4 - 0.25) comes back.
    lin = np.array([[2.0, np.nan]], dtype="float32")
    out_lin, _ = convert._subtract_noise(lin, noise, decibels=False)
    assert out_lin[0, 0] == pytest.approx(math.sqrt(4.0 - 0.25), rel=1e-5)
    assert np.isnan(out_lin[0, 1])  # the warp's nodata stays nodata

    # Decibels: 20*log10(magnitude) IS 10*log10(power), so the same measurement.
    db = np.array([[20.0 * math.log10(2.0), np.nan]], dtype="float32")
    out_db, _ = convert._subtract_noise(db, noise, decibels=True)
    assert out_db[0, 0] == pytest.approx(10.0 * math.log10(4.0 - 0.25), rel=1e-5)
    assert np.isnan(out_db[0, 1])
    assert 20.0 * math.log10(float(out_lin[0, 0])) == pytest.approx(float(out_db[0, 0]), rel=1e-5)


def test_subtract_noise_floors_pixels_at_or_below_the_noise():
    np = pytest.importorskip("numpy")
    # A pixel the radar could not hear over its own noise is at the sensor's
    # sensitivity limit, which is a statement about the radar. Flooring it keeps
    # it dark and finite; letting it go negative would make sqrt/log undefined.
    noise = np.full((1, 3), 1.0)
    lin = np.array([[0.5, 1.0, 2.0]], dtype="float32")
    out, floored = convert._subtract_noise(lin, noise, decibels=False)
    assert np.all(np.isfinite(out))
    assert out[0, 0] == pytest.approx(math.sqrt(convert._NOISE_RESIDUAL_FLOOR))
    assert out[0, 1] == pytest.approx(math.sqrt(convert._NOISE_RESIDUAL_FLOOR))
    assert out[0, 2] == pytest.approx(math.sqrt(3.0), rel=1e-5)
    # The two floored pixels are exactly "how much of this image is the sensor",
    # which the output alone cannot answer: a floored pixel and a genuinely
    # floor-valued one are the same value once written.
    assert floored == pytest.approx(2 / 3)


def test_denoise_origin_matches_the_same_pixels_of_the_whole_scene():
    """A clipped read has its floor evaluated where the pixels came from."""
    np = pytest.importorskip("numpy")

    sicd = _FakeSicd(radiometric=_radiometric_noise(coefs=[[-20.0, 0.4], [0.2, 0.0]]))
    amp = (np.arange(20 * 24, dtype="float32") + 1.0).reshape(20, 24)

    whole, whole_noise = convert._denoise_amplitude(sicd, amp, decibels=True)
    row0, col0 = 6, 9
    window, window_noise = convert._denoise_amplitude(
        sicd, amp[row0 : row0 + 5, col0 : col0 + 7], decibels=True, origin=(row0, col0)
    )
    assert np.allclose(window, whole[row0 : row0 + 5, col0 : col0 + 7], atol=1e-5)
    # The measured floor is a polynomial across the image, so there is no single
    # level to report -- unlike the estimated one, which is exactly one number.
    # It assumes nothing about the scene either, so there is no margin to quote.
    assert whole_noise.floor_db is None and window_noise.floor_db is None
    assert whole_noise.margin_db is None and window_noise.margin_db is None


def test_sicd_to_amplitude_geotiff_subtracts_the_products_own_floor(tmp_path, monkeypatch):
    rasterio = pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")
    np = pytest.importorskip("numpy")

    data = _fake_complex(6, 8)
    _patch_open_complex(monkeypatch, _FakeReader(data, _FakeSicd()))
    plain = convert.sicd_to_amplitude_geotiff(tmp_path / "in.ntf", tmp_path / "plain.tif")

    noise_db = -3.0
    sicd = _FakeSicd(radiometric=_radiometric_noise(coefs=[[noise_db]]))
    _patch_open_complex(monkeypatch, _FakeReader(data, sicd))
    denoised = convert.sicd_to_amplitude_geotiff(
        tmp_path / "in.ntf", tmp_path / "denoised.tif", noise_subtract=True
    )

    with rasterio.open(plain) as a, rasterio.open(denoised) as b:
        va, vb = a.read(1), b.read(1)
        residual = np.clip(
            10.0 ** (va / 10.0) - 10.0 ** (noise_db / 10.0), convert._NOISE_RESIDUAL_FLOOR, None
        )
        assert np.allclose(vb, 10.0 * np.log10(residual), atol=1e-4)
        # It only ever takes brightness away.
        assert np.all(vb <= va + 1e-4)


def test_sicd_to_amplitude_geotiff_records_the_subtraction(tmp_path, monkeypatch):
    pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")

    sicd = _FakeSicd(radiometric=_radiometric_noise(coefs=[[-40.0]]))
    _patch_open_complex(monkeypatch, _FakeReader(_fake_complex(6, 6), sicd))
    out = convert.sicd_to_amplitude_geotiff(
        tmp_path / "in.ntf", tmp_path / "denoised.tif", noise_subtract=True
    )
    assert convert.read_conversion_tags(out)["noise_subtraction"] == "absolute"


def test_sicd_to_geocoded_cog_subtracts_the_floor_before_calibrating(tmp_path, monkeypatch):
    """Order matters: noise adds to raw power, so it comes off before the scale."""
    rasterio = pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")
    np = pytest.importorskip("numpy")

    data = _fake_complex(10, 12)
    noise_db, scale = -3.0, 100.0

    _patch_open_complex(monkeypatch, _FakeReader(data, _FakeSicd()))
    plain = convert.sicd_to_geocoded_cog(
        tmp_path / "in.ntf", tmp_path / "plain.tif", gcp_grid=6, resampling="nearest"
    )

    sicd = _FakeSicd(radiometric=_radiometric_noise(coefs=[[noise_db]], SigmaZeroSFPoly=[[scale]]))
    _patch_open_complex(monkeypatch, _FakeReader(data, sicd))
    both = convert.sicd_to_geocoded_cog(
        tmp_path / "in.ntf",
        tmp_path / "both.tif",
        gcp_grid=6,
        resampling="nearest",
        calibration="sigma0",
        noise_subtract=True,
    )

    with rasterio.open(plain) as a, rasterio.open(both) as b:
        va, vb = a.read(1), b.read(1)
        finite = np.isfinite(va) & np.isfinite(vb)
        assert finite.any()
        power = 10.0 ** (va[finite] / 10.0)
        noise = 10.0 ** (noise_db / 10.0)
        subtract_then_scale = 10.0 * np.log10(
            scale * np.clip(power - noise, convert._NOISE_RESIDUAL_FLOOR, None)
        )
        scale_then_subtract = 10.0 * np.log10(
            np.clip(scale * power - noise, convert._NOISE_RESIDUAL_FLOOR, None)
        )
        assert np.allclose(vb[finite], subtract_then_scale, atol=1e-3)
        # The wrong order is a different, plausible-looking answer.
        assert not np.allclose(vb[finite], scale_then_subtract, atol=1e-3)
        assert convert.read_conversion_tags(both)["noise_subtraction"] == "absolute"


def test_sicd_to_geocoded_cog_without_noise_metadata_raises(tmp_path, monkeypatch):
    pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")
    _patch_open_complex(monkeypatch, _FakeReader(_fake_complex(6, 6), _FakeSicd()))
    with pytest.raises(ValueError, match="no Radiometric metadata"):
        convert.sicd_to_geocoded_cog(
            tmp_path / "in.ntf", tmp_path / "out.tif", gcp_grid=4, noise_subtract=True
        )


def test_sicd_noise_level_reports_the_products_own_metadata(tmp_path, monkeypatch):
    pytest.importorskip("sarpy")

    _patch_open_complex(monkeypatch, _FakeReader(_fake_complex(4, 4), _FakeSicd()))
    assert convert.sicd_noise_level(tmp_path / "in.ntf") is None

    sicd = _FakeSicd(radiometric=_radiometric_noise("ABSOLUTE"))
    _patch_open_complex(monkeypatch, _FakeReader(_fake_complex(4, 4), sicd))
    assert convert.sicd_noise_level(tmp_path / "in.ntf") == "ABSOLUTE"


def test_cli_convert_subtract_noise(tmp_path, monkeypatch):
    rasterio = pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")
    np = pytest.importorskip("numpy")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    sicd = _FakeSicd(radiometric=_radiometric_noise(coefs=[[-6.0]]))
    _patch_open_complex(monkeypatch, _FakeReader(_fake_complex(10, 12), sicd))

    src = tmp_path / "scene.ntf"
    src.write_bytes(b"not-a-real-nitf")
    dst = tmp_path / "geo.tif"
    result = CliRunner().invoke(
        cli_mod.cli, ["convert", str(src), str(dst), "--subtract-noise", "--resampling", "nearest"]
    )

    assert result.exit_code == 0, result.output
    assert "noise-subtracted" in result.output
    with rasterio.open(dst) as ds:
        assert np.isfinite(ds.read(1)).any()
    assert convert.read_conversion_tags(dst)["noise_subtraction"] == "absolute"


def test_cli_convert_subtract_noise_unavailable_is_a_clean_error(tmp_path, monkeypatch):
    pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    # A relative noise level is the interesting refusal: the metadata is there,
    # it just cannot answer the question being asked of it.
    sicd = _FakeSicd(radiometric=_radiometric_noise("RELATIVE"))
    _patch_open_complex(monkeypatch, _FakeReader(_fake_complex(8, 8), sicd))

    src = tmp_path / "scene.ntf"
    src.write_bytes(b"not-a-real-nitf")
    result = CliRunner().invoke(
        cli_mod.cli, ["convert", str(src), str(tmp_path / "geo.tif"), "--subtract-noise"]
    )

    assert result.exit_code != 0
    assert "relative noise level" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


# --------------------------------------------------------------------------- #
# The estimated noise floor (``noise_model="estimated"``).
#
# The measured floor is the better number and Umbra's open products do not carry
# it -- they generally ship with no Radiometric block at all -- so the
# correction refused on exactly the archive this library exists for. These tests
# pin the estimator's arithmetic, the two ways it can be asked for something it
# cannot do, and the part that keeps it honest: an inferred floor is recorded,
# reported and refused as a *different* thing from a measured one.
# --------------------------------------------------------------------------- #


def test_estimate_noise_power_reads_the_low_tail_of_the_scenes_own_power():
    np = pytest.importorskip("numpy")

    # A scene that is 20% dark water at 1.0 power and 80% land at 100.0. The
    # fifth percentile lands inside the noise-dominated population, which is the
    # whole argument: the darkest surfaces record the receiver, not the ground.
    power = np.concatenate([np.full(200, 1.0), np.full(800, 100.0)])
    lin = np.sqrt(power).reshape(20, 50).astype("float32")

    floor, median = convert._estimate_noise_power(lin, decibels=False, percentile=5.0)
    assert floor == pytest.approx(1.0)
    # The median comes back beside the floor because the distance between them is
    # the estimator's own assumption -- that the dark tail is a *different*
    # population from ordinary backscatter -- made checkable.
    assert median == pytest.approx(100.0)

    # Same measurement in the decibel scale: 10*log10 of power IS 20*log10 of
    # magnitude, so the estimator has to undo whichever one it was handed.
    db = (10.0 * np.log10(power)).reshape(20, 50).astype("float32")
    assert convert._estimate_noise_power(db, decibels=True, percentile=5.0)[0] == pytest.approx(
        1.0, rel=1e-5
    )


def test_estimate_noise_power_ignores_the_warps_nodata():
    np = pytest.importorskip("numpy")

    values = np.array([[1.0, np.nan, 10.0, np.nan, 10.0]], dtype="float32")
    # Counting the NaNs would move the percentile off a shrinking population.
    assert convert._estimate_noise_power(values, decibels=False, percentile=50.0)[
        0
    ] == pytest.approx(100.0)


def test_estimate_noise_power_rejects_an_all_nodata_image():
    np = pytest.importorskip("numpy")

    with pytest.raises(ValueError, match="no pixel carries a finite value"):
        convert._estimate_noise_power(np.full((3, 3), np.nan), decibels=False)


@pytest.mark.parametrize("percentile", [0.0, 100.0, -5.0, 101.0])
def test_estimate_noise_power_rejects_a_percentile_outside_the_distribution(percentile):
    np = pytest.importorskip("numpy")

    with pytest.raises(ValueError, match="between 0 and 100"):
        convert._estimate_noise_power(np.ones((2, 2)), decibels=False, percentile=percentile)


def test_denoise_estimated_needs_no_radiometric_metadata_at_all():
    """The point of the model: it works on a product that states nothing."""
    np = pytest.importorskip("numpy")

    # _FakeSicd() with no Radiometric block is the shape of an Umbra open
    # product, and is exactly what the measured model refuses.
    sicd = _FakeSicd()
    amp = np.concatenate([np.full(100, 1.0), np.full(900, 10.0)]).reshape(25, 40).astype("float32")

    with pytest.raises(ValueError, match="no Radiometric metadata"):
        convert._denoise_amplitude(sicd, amp, decibels=False, model="measured")

    out, noise = convert._denoise_amplitude(sicd, amp, decibels=False, model="estimated")
    # Floor = the 5th percentile of power = 1.0 (10% of the scene sits there).
    assert noise.floor_db == pytest.approx(0.0)
    # 90% of the scene is at power 100, so the median sits 20 dB above the floor:
    # this scene plainly had dark ground to read, and says so.
    assert noise.margin_db == pytest.approx(20.0)
    # Only the dark population is driven to the residual floor.
    assert noise.floored_fraction == pytest.approx(0.1)
    assert np.all(out <= amp + 1e-4)  # it only ever takes brightness away
    assert out[0, 0] == pytest.approx(math.sqrt(convert._NOISE_RESIDUAL_FLOOR))
    assert out[-1, -1] == pytest.approx(math.sqrt(100.0 - 1.0), rel=1e-5)


def test_denoise_rejects_an_unknown_noise_model():
    np = pytest.importorskip("numpy")

    with pytest.raises(ValueError, match="Unknown noise_model"):
        convert._denoise_amplitude(_FakeSicd(), np.ones((2, 2)), decibels=False, model="guessed")


def test_geocoded_cog_estimated_floor_is_recorded_as_an_inference(tmp_path, monkeypatch):
    """A measured floor and an inferred one are not the same claim."""
    pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")

    # No Radiometric block: the measured model cannot run on this product.
    _patch_open_complex(monkeypatch, _FakeReader(_fake_complex(10, 12), _FakeSicd()))
    out = convert.sicd_to_geocoded_cog(
        tmp_path / "in.ntf",
        tmp_path / "estimated.tif",
        gcp_grid=6,
        resampling="nearest",
        noise_subtract=True,
        noise_model="estimated",
    )
    tags = convert.read_conversion_tags(out)
    # Deliberately NOT "absolute": load.MEASUREMENT_PROVENANCE_KEYS carries
    # noise_subtraction, so this value is what makes to_stack refuse a series
    # that differences an inferred floor against a measured one.
    assert tags["noise_subtraction"] == "estimated"
    # An inferred number nobody can read back is not reproducible.
    assert float(tags["noise_floor_db"]) == pytest.approx(
        10.0
        * math.log10(
            convert._estimate_noise_power(
                convert._amplitude(_fake_complex(10, 12), decibels=True), decibels=True
            )[0]
        ),
        rel=1e-4,
    )


def test_measured_floor_still_records_absolute_and_no_level(tmp_path, monkeypatch):
    """Rasters converted before noise_model= existed still compare equal."""
    pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")

    sicd = _FakeSicd(radiometric=_radiometric_noise(coefs=[[-40.0]]))
    _patch_open_complex(monkeypatch, _FakeReader(_fake_complex(8, 8), sicd))
    out = convert.sicd_to_geocoded_cog(
        tmp_path / "in.ntf",
        tmp_path / "measured.tif",
        gcp_grid=6,
        resampling="nearest",
        noise_subtract=True,
    )
    tags = convert.read_conversion_tags(out)
    assert tags["noise_subtraction"] == "absolute"
    # The measured floor is a polynomial across the image, so there is no single
    # level to quote and the tag stays off rather than reporting a mean.
    assert "noise_floor_db" not in tags


def test_conversion_tags_omit_the_floor_when_nothing_was_subtracted():
    tags = convert.conversion_tags(
        source="scene.ntf", geocoded=True, noise_subtraction=None, noise_floor_db=-31.4
    )
    assert tags["UMBRA_NOISE_SUBTRACTION"] == "none"
    assert "UMBRA_NOISE_FLOOR_DB" not in tags


def test_cli_convert_noise_model_estimated_works_without_noise_metadata(tmp_path, monkeypatch):
    rasterio = pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")
    np = pytest.importorskip("numpy")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    _patch_open_complex(monkeypatch, _FakeReader(_fake_complex(10, 12), _FakeSicd()))
    src = tmp_path / "scene.ntf"
    src.write_bytes(b"not-a-real-nitf")
    dst = tmp_path / "geo.tif"
    result = CliRunner().invoke(
        cli_mod.cli,
        [
            "convert",
            str(src),
            str(dst),
            "--subtract-noise",
            "--noise-model",
            "estimated",
            "--resampling",
            "nearest",
        ],
    )

    assert result.exit_code == 0, result.output
    # The output line names the two apart, as the tags and the stack refusal do.
    assert "noise-estimated" in result.output
    with rasterio.open(dst) as ds:
        assert np.isfinite(ds.read(1)).any()
    assert convert.read_conversion_tags(dst)["noise_subtraction"] == "estimated"


def test_cli_convert_defaults_to_the_measured_floor(tmp_path, monkeypatch):
    """--subtract-noise alone means exactly what it meant before this option."""
    pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    _patch_open_complex(monkeypatch, _FakeReader(_fake_complex(8, 8), _FakeSicd()))
    src = tmp_path / "scene.ntf"
    src.write_bytes(b"not-a-real-nitf")
    result = CliRunner().invoke(
        cli_mod.cli, ["convert", str(src), str(tmp_path / "geo.tif"), "--subtract-noise"]
    )

    assert result.exit_code != 0
    assert "no Radiometric metadata" in result.output


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


# --------------------------------------------------------------------------- #
# Clipping to an area of interest (bbox= / --clip-bbox).
# --------------------------------------------------------------------------- #


def _ground(sicd, row, col, *, hae=0.0):
    """Where the fake projection model puts one image point, as (lon, lat)."""
    lon = sicd.lon0 + col * sicd.dlon + row * sicd.skew + hae * sicd.hae_shift
    lat = sicd.lat0 - row * sicd.dlat + col * sicd.skew
    return lon, lat


def _bbox_around(sicd, row, col, *, pad=0.004):
    lon, lat = _ground(sicd, row, col)
    return (lon - pad, lat - pad, lon + pad, lat + pad)


def test_clip_window_is_a_padded_superset_of_the_area_of_interest():
    pytest.importorskip("numpy")

    sicd = _FakeSicd()
    rows, cols = 60, 80
    # A tiny area of interest well inside the scene: smaller than one lattice
    # cell, which is why the search works on cells rather than lattice points.
    bbox = _bbox_around(sicd, 30, 40, pad=0.002)
    row0, row1, col0, col1 = convert._clip_window(sicd, (rows, cols), bbox, grid=17)

    # A real window, strictly inside the scene, and much smaller than it.
    assert 0 <= row0 < row1 <= rows
    assert 0 <= col0 < col1 <= cols
    assert (row1 - row0) * (col1 - col0) < 0.5 * rows * cols
    # It contains the image point the area of interest was built around.
    assert row0 <= 30 < row1
    assert col0 <= 40 < col1


def test_clip_window_covers_every_image_point_inside_the_bbox():
    """The window is a superset: no pixel whose ground lands in the bbox is cut."""
    pytest.importorskip("numpy")

    sicd = _FakeSicd()
    rows, cols = 60, 80
    bbox = _bbox_around(sicd, 22, 55, pad=0.006)
    west, south, east, north = bbox
    row0, row1, col0, col1 = convert._clip_window(sicd, (rows, cols), bbox, grid=17)

    for row in range(rows):
        for col in range(cols):
            lon, lat = _ground(sicd, row, col)
            if west <= lon <= east and south <= lat <= north:
                assert row0 <= row < row1, (row, col)
                assert col0 <= col < col1, (row, col)


def test_clip_window_rejects_a_bbox_that_misses_the_scene():
    pytest.importorskip("numpy")

    sicd = _FakeSicd()
    with pytest.raises(ValueError, match="does not overlap the scene"):
        convert._clip_window(sicd, (40, 40), (10.0, 10.0, 10.5, 10.5))


def test_clip_window_rejects_a_degenerate_bbox():
    pytest.importorskip("numpy")

    sicd = _FakeSicd()
    with pytest.raises(ValueError, match="positive"):
        convert._clip_window(sicd, (40, 40), (-100.0, 40.0, -100.0, 40.0))


def test_build_gcps_origin_projects_scene_coordinates_but_labels_window_ones():
    pytest.importorskip("numpy")
    pytest.importorskip("rasterio")

    sicd = _FakeSicd()
    gcps = convert._build_gcps(sicd, (10, 10), grid=2, projection_type="HAE", origin=(20, 30))

    # Rows/cols index the *array* being warped, so they start at zero ...
    assert {(g.row, g.col) for g in gcps} == {(0.0, 0.0), (0.0, 9.0), (9.0, 0.0), (9.0, 9.0)}
    # ... while lon/lat come from where that pixel really is in the scene.
    top_left = next(g for g in gcps if g.row == 0.0 and g.col == 0.0)
    lon, lat = _ground(sicd, 20, 30)
    assert top_left.x == pytest.approx(lon)
    assert top_left.y == pytest.approx(lat)


def test_scene_geo_bbox_origin_describes_the_window_not_the_scene():
    pytest.importorskip("numpy")

    sicd = _FakeSicd()
    whole = convert._scene_geo_bbox(sicd, (60, 80))
    window = convert._scene_geo_bbox(sicd, (10, 10), origin=(25, 35))

    assert window[0] > whole[0] and window[2] < whole[2]
    assert window[1] > whole[1] and window[3] < whole[3]


def test_calibration_origin_matches_the_same_pixels_of_the_whole_scene():
    """A clipped read is calibrated at the image coordinates it came from."""
    np = pytest.importorskip("numpy")

    # A polynomial that varies across the image, so a wrong origin shows up.
    sicd = _FakeSicd(radiometric=_radiometric(SigmaZeroSFPoly=[[2.0, 0.05], [0.03, 0.0]]))
    amp = (np.arange(20 * 24, dtype="float32") + 1.0).reshape(20, 24)

    whole = convert._calibrate_amplitude(sicd, amp, kind="sigma0", decibels=True)
    row0, col0 = 6, 9
    window = convert._calibrate_amplitude(
        sicd,
        amp[row0 : row0 + 5, col0 : col0 + 7],
        kind="sigma0",
        decibels=True,
        origin=(row0, col0),
    )

    assert np.allclose(window, whole[row0 : row0 + 5, col0 : col0 + 7], atol=1e-5)


def test_warp_gcps_to_cog_bounds_crop_without_changing_the_pixel_size(tmp_path):
    rasterio = pytest.importorskip("rasterio")
    np = pytest.importorskip("numpy")

    rows, cols = 32, 32
    amp = (np.arange(rows * cols, dtype="float32") + 1.0).reshape(rows, cols)
    gcps = _hand_gcps(rows, cols, res=0.01)  # spans lon -100..-99.69, lat 39.69..40

    whole = convert._warp_gcps_to_cog(
        amp,
        gcps,
        tmp_path / "whole.tif",
        resolution=None,
        resampling="nearest",
        nodata=float("nan"),
    )
    clipped = convert._warp_gcps_to_cog(
        amp,
        gcps,
        tmp_path / "clip.tif",
        resolution=None,
        resampling="nearest",
        nodata=float("nan"),
        bounds=(-99.95, 39.85, -99.85, 39.95),
    )

    with rasterio.open(whole) as full, rasterio.open(clipped) as part:
        # Same ground sample distance -- clipping chooses ground, not sharpness.
        assert part.transform.a == pytest.approx(full.transform.a)
        assert part.width < full.width and part.height < full.height
        assert part.bounds.left == pytest.approx(-99.95, abs=1e-9)
        assert part.bounds.top == pytest.approx(39.95, abs=1e-9)
        # The requested window is honoured to within the ceil on width/height.
        assert part.bounds.right <= -99.85 + full.transform.a
        assert part.bounds.bottom >= 39.85 - full.transform.a


def test_warp_gcps_to_cog_rejects_bounds_off_the_control_points(tmp_path):
    pytest.importorskip("rasterio")
    np = pytest.importorskip("numpy")

    amp = np.ones((8, 8), dtype="float32")
    with pytest.raises(ValueError, match="does not overlap"):
        convert._warp_gcps_to_cog(
            amp,
            _hand_gcps(8, 8),
            tmp_path / "x.tif",
            resolution=0.01,
            resampling="nearest",
            nodata=0.0,
            bounds=(10.0, 10.0, 10.5, 10.5),
        )


def _marked_complex(rows, cols, *, mark, value=5000.0):
    """A flat scene with one bright block, so its ground position is findable."""
    np = pytest.importorskip("numpy")
    mag = np.ones((rows, cols), dtype="float64")
    r, c = mark
    mag[r : r + 3, c : c + 3] = value
    return mag * (1 + 0j)


def _bright_centroid(path):
    """Ground centroid (lon, lat) of the bright block in a geocoded raster.

    The centroid rather than the brightest pixel: the two rasters are sampled on
    grids with different origins, so which single cell wins is arbitrary at the
    sub-pixel level while the block's centre of mass is not.
    """
    rasterio = pytest.importorskip("rasterio")
    np = pytest.importorskip("numpy")
    with rasterio.open(path) as ds:
        band = ds.read(1)
        finite = np.isfinite(band)
        bright = finite & (band > 0.5 * float(np.nanmax(band[finite])))
        rows, cols = np.nonzero(bright)
        xs, ys = ds.xy(rows, cols)
        return float(np.mean(xs)), float(np.mean(ys))


def test_sicd_to_geocoded_cog_clip_reads_only_the_window_and_keeps_the_geolocation(
    tmp_path, monkeypatch
):
    rasterio = pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")
    pytest.importorskip("numpy")

    rows, cols = 60, 80
    mark = (31, 41)
    data = _marked_complex(rows, cols, mark=mark)
    sicd = _FakeSicd()

    whole_reader = _FakeReader(data, sicd)
    _patch_open_complex(monkeypatch, whole_reader)
    whole = convert.sicd_to_geocoded_cog(
        tmp_path / "in.ntf", tmp_path / "whole.tif", gcp_grid=9, resampling="nearest"
    )

    clip_reader = _FakeReader(data, _FakeSicd())
    _patch_open_complex(monkeypatch, clip_reader)
    bbox = _bbox_around(sicd, *mark, pad=0.02)
    clipped = convert.sicd_to_geocoded_cog(
        tmp_path / "in.ntf",
        tmp_path / "clip.tif",
        gcp_grid=9,
        resampling="nearest",
        bbox=bbox,
    )

    # The whole-scene run reads everything; the clipped one reads a sub-window.
    assert whole_reader.reads == [(slice(None), slice(None))]
    (read_rows, read_cols) = clip_reader.reads[0]
    assert read_rows.start > 0 or read_cols.start > 0
    read_pixels = (read_rows.stop - read_rows.start) * (read_cols.stop - read_cols.start)
    assert read_pixels < 0.5 * rows * cols

    with rasterio.open(whole) as full, rasterio.open(clipped) as part:
        assert part.width < full.width and part.height < full.height
        # Cropped to the request (a pixel of slack for the ceil on width/height).
        slack = 2 * part.transform.a
        assert part.bounds.left >= bbox[0] - slack
        assert part.bounds.right <= bbox[2] + slack
        assert part.bounds.bottom >= bbox[1] - slack
        assert part.bounds.top <= bbox[3] + slack

    # The clip moved no ground: the bright block lands in the same place in
    # both rasters, and where the projection model puts it. This is what an
    # off-by-``origin`` GCP set would break -- the clipped window would be
    # geocoded as if it were the whole scene, shifting it by the window offset
    # (here tens of pixels), which is exactly the failure the padding cannot
    # hide.
    with rasterio.open(whole) as full:
        pixel = full.transform.a
    true_lon, true_lat = _ground(sicd, mark[0] + 1, mark[1] + 1)  # block centre
    whole_lon, whole_lat = _bright_centroid(whole)
    clip_lon, clip_lat = _bright_centroid(clipped)
    assert clip_lon == pytest.approx(whole_lon, abs=0.5 * pixel)
    assert clip_lat == pytest.approx(whole_lat, abs=0.5 * pixel)
    assert clip_lon == pytest.approx(true_lon, abs=pixel)
    assert clip_lat == pytest.approx(true_lat, abs=pixel)


def test_sicd_to_geocoded_cog_clip_that_misses_the_scene_errors(tmp_path, monkeypatch):
    pytest.importorskip("sarpy")
    pytest.importorskip("numpy")

    _patch_open_complex(monkeypatch, _FakeReader(_fake_complex(20, 20), _FakeSicd()))
    with pytest.raises(ValueError, match="does not overlap the scene"):
        convert.sicd_to_geocoded_cog(
            tmp_path / "in.ntf", tmp_path / "out.tif", bbox=(10.0, 10.0, 10.5, 10.5)
        )


def test_cli_convert_clip_bbox(tmp_path, monkeypatch):
    rasterio = pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    sicd = _FakeSicd()
    _patch_open_complex(monkeypatch, _FakeReader(_fake_complex(40, 50), sicd))
    src = tmp_path / "scene.ntf"
    src.write_bytes(b"not-a-real-nitf")
    out = tmp_path / "geo.tif"
    west, south, east, north = _bbox_around(sicd, 20, 25, pad=0.02)

    result = CliRunner().invoke(
        cli_mod.cli,
        ["convert", str(src), str(out), "--clip-bbox", f"{west},{south},{east},{north}"],
    )

    assert result.exit_code == 0, result.output
    with rasterio.open(out) as ds:
        # Cropped to the request, to within the ceil on the output width.
        slack = ds.transform.a
        assert ds.bounds.left >= west - slack
        assert ds.bounds.right <= east + slack


def test_cli_convert_clip_bbox_is_refused_on_the_slant_plane(tmp_path, monkeypatch):
    pytest.importorskip("sarpy")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    _patch_open_complex(monkeypatch, _FakeReader(_fake_complex(8, 8), _FakeSicd()))
    src = tmp_path / "scene.ntf"
    src.write_bytes(b"not-a-real-nitf")

    result = CliRunner().invoke(
        cli_mod.cli,
        [
            "convert",
            str(src),
            str(tmp_path / "amp.tif"),
            "--slant-plane",
            "--clip-bbox",
            "-100.1,39.9,-99.9,40.1",
        ],
    )

    assert result.exit_code != 0
    assert "no map grid" in result.output


def test_clip_with_a_dem_searches_on_the_flat_earth_projection(tmp_path, monkeypatch):
    """A DEM places the pixels; it does not decide which ones are read."""
    pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")
    pytest.importorskip("numpy")

    dem = _write_dem(tmp_path / "dem.tif", kind="const")
    sicd = _FakeSicd(hae_shift=1e-4)
    reader = _FakeReader(_fake_complex(40, 50), sicd)
    _patch_open_complex(monkeypatch, reader)

    out = convert.sicd_to_geocoded_cog(
        tmp_path / "in.ntf",
        tmp_path / "geo.tif",
        gcp_grid=5,
        # "DEM" would need a sarpy elevation model; the window search must not
        # ask for one, so this also pins that the flat-earth path is used.
        projection_type="DEM",
        dem=str(dem),
        bbox=_bbox_around(sicd, 20, 25, pad=0.02),
    )

    assert out.exists()
    assert ("latlong", "HAE") in sicd.calls
    assert not any(kind == "DEM" for _, kind in sicd.calls)


# --------------------------------------------------------------------------- #
# What the subtraction did to the image. Both noise models have documented
# limits -- everything at the sensor's floor is clamped, and the estimated model
# assumes the scene contained dark ground to read -- and until these numbers were
# recorded, neither limit was visible on any particular scene.
# --------------------------------------------------------------------------- #


def test_subtract_noise_counts_the_floored_fraction_over_finite_pixels_only():
    np = pytest.importorskip("numpy")

    # Two of the four real pixels are at or under the floor; the warp's nodata is
    # neither floored nor measured, so counting it would shrink the answer toward
    # zero on exactly the clipped scenes where the fraction matters most.
    noise = np.full((1, 5), 1.0)
    lin = np.array([[0.5, 1.0, 2.0, 3.0, np.nan]], dtype="float32")
    _, floored = convert._subtract_noise(lin, noise, decibels=False)
    assert floored == pytest.approx(0.5)

    # An all-nodata window has no population to report a fraction of.
    _, none_finite = convert._subtract_noise(
        np.full((2, 2), np.nan, dtype="float32"), np.ones((2, 2)), decibels=False
    )
    assert none_finite == 0.0


def test_estimated_margin_separates_a_bimodal_scene_from_a_uniform_one():
    """The estimator's assumption, made checkable rather than only documented."""
    np = pytest.importorskip("numpy")

    sicd = _FakeSicd()  # no Radiometric block: only the estimated model can run
    # Dark water (power 1) under bright land (power 1000): the fifth percentile
    # lands in a population 30 dB below the median, which is the evidence that
    # the two are different populations at all.
    bimodal = np.sqrt(
        np.concatenate([np.full(100, 1.0), np.full(900, 1000.0)]).reshape(25, 40)
    ).astype("float32")
    _, dark_ground = convert._denoise_amplitude(sicd, bimodal, decibels=False, model="estimated")
    assert dark_ground.margin_db == pytest.approx(30.0)
    assert dark_ground.margin_db > convert.NOISE_MARGIN_WARN_DB

    # Uniformly bright imagery -- dense city, forest at high incidence -- has no
    # noise-dominated tail, so the fifth percentile IS ground and subtracting it
    # takes real backscatter off. The margin collapses, which is the only warning
    # the estimate can honestly give.
    uniform = np.full((25, 40), math.sqrt(1000.0), dtype="float32")
    _, no_dark_ground = convert._denoise_amplitude(sicd, uniform, decibels=False, model="estimated")
    assert no_dark_ground.margin_db == pytest.approx(0.0)
    assert no_dark_ground.margin_db < convert.NOISE_MARGIN_WARN_DB
    # And it shows in the other diagnostic too: the whole scene lands on the floor.
    assert no_dark_ground.floored_fraction == pytest.approx(1.0)


def test_margin_is_absent_rather_than_infinite_where_the_ratio_is_undefined():
    # A tag a reader parses as a float should not have to carry "-inf": an absent
    # number is a smaller lie than an infinite one.
    assert convert._margin_db(0.0, 1.0) is None
    assert convert._margin_db(1.0, 0.0) is None
    assert convert._margin_db(100.0, 1.0) == pytest.approx(20.0)


def test_geocoded_cog_records_what_the_estimated_subtraction_did(tmp_path, monkeypatch):
    pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")

    _patch_open_complex(monkeypatch, _FakeReader(_fake_complex(10, 12), _FakeSicd()))
    out = convert.sicd_to_geocoded_cog(
        tmp_path / "in.ntf",
        tmp_path / "estimated.tif",
        gcp_grid=6,
        resampling="nearest",
        noise_subtract=True,
        noise_model="estimated",
    )
    tags = convert.read_conversion_tags(out)
    # Counted in image space, over the window the correction actually saw --
    # not over the warped output, which has nodata the subtraction never met.
    assert 0.0 <= float(tags["noise_floored_fraction"]) <= 1.0
    assert float(tags["noise_floor_margin_db"]) > 0.0


def test_measured_floor_reports_the_floored_fraction_but_no_margin(tmp_path, monkeypatch):
    """The measured model assumes nothing about the scene, so it claims nothing."""
    pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")

    sicd = _FakeSicd(radiometric=_radiometric_noise(coefs=[[-40.0]]))
    _patch_open_complex(monkeypatch, _FakeReader(_fake_complex(8, 8), sicd))
    out = convert.sicd_to_geocoded_cog(
        tmp_path / "in.ntf",
        tmp_path / "measured.tif",
        gcp_grid=6,
        resampling="nearest",
        noise_subtract=True,
    )
    tags = convert.read_conversion_tags(out)
    # "How much of this image is at the sensor's limit" is a fact about either
    # floor, so it is reported for both.
    assert "noise_floored_fraction" in tags
    # The margin exists to check an inference. There is none to check here.
    assert "noise_floor_margin_db" not in tags


def test_conversion_tags_omit_the_diagnostics_when_nothing_was_subtracted():
    tags = convert.conversion_tags(
        source="scene.ntf",
        geocoded=True,
        noise_subtraction=None,
        noise_floored_fraction=0.42,
        noise_floor_margin_db=11.0,
    )
    assert tags["UMBRA_NOISE_SUBTRACTION"] == "none"
    assert "UMBRA_NOISE_FLOORED_FRACTION" not in tags
    assert "UMBRA_NOISE_FLOOR_MARGIN_DB" not in tags


def test_cli_convert_says_what_the_subtraction_did(tmp_path, monkeypatch):
    pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    _patch_open_complex(monkeypatch, _FakeReader(_fake_complex(10, 12), _FakeSicd()))
    src = tmp_path / "scene.ntf"
    src.write_bytes(b"not-a-real-nitf")
    result = CliRunner().invoke(
        cli_mod.cli,
        [
            "convert",
            str(src),
            str(tmp_path / "geo.tif"),
            "--subtract-noise",
            "--noise-model",
            "estimated",
            "--resampling",
            "nearest",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "at the sensor's limit" in result.output
    assert "below the scene median" in result.output


def test_cli_convert_advises_when_the_scene_had_no_dark_ground(tmp_path, monkeypatch):
    """An advisory, not a refusal: a uniform scene is legitimate."""
    np = pytest.importorskip("numpy")
    pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    # Uniform brightness: the fifth percentile is ground, so the subtraction is
    # taking real backscatter off and the margin collapses to nothing.
    flat = np.full((10, 12), 5.0 + 0j)
    _patch_open_complex(monkeypatch, _FakeReader(flat, _FakeSicd()))
    src = tmp_path / "scene.ntf"
    src.write_bytes(b"not-a-real-nitf")
    result = CliRunner().invoke(
        cli_mod.cli,
        [
            "convert",
            str(src),
            str(tmp_path / "flat.tif"),
            "--subtract-noise",
            "--noise-model",
            "estimated",
            "--resampling",
            "nearest",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "little dark ground" in result.output
    assert "--noise-model measured" in result.output


def test_cli_convert_stays_quiet_about_a_subtraction_that_did_not_run(tmp_path, monkeypatch):
    pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    _patch_open_complex(monkeypatch, _FakeReader(_fake_complex(10, 12), _FakeSicd()))
    src = tmp_path / "scene.ntf"
    src.write_bytes(b"not-a-real-nitf")
    result = CliRunner().invoke(
        cli_mod.cli, ["convert", str(src), str(tmp_path / "plain.tif"), "--resampling", "nearest"]
    )

    assert result.exit_code == 0, result.output
    assert "sensor's limit" not in result.output


# --------------------------------------------------------------------------- #
# The range-varying estimated floor (``noise_model="estimated-range"``).
#
# The constant estimate's first documented limit was that one scalar cannot
# follow the swath -- which is not a rounding error but the very artefact the
# subtraction exists to remove, since a receiver's sensitivity varies with range
# and a scalar therefore under-subtracts at one edge and over-subtracts at the
# other. These tests pin the fit that follows it, the two ways a real scene
# defeats a per-line read (a line with no dark ground, a line with almost no
# samples), and the part that keeps it honest: a fitted profile is recorded,
# reported and refused as a *third* thing, not as a better "estimated".
# --------------------------------------------------------------------------- #


def _range_varying_noise_scene(
    rows=64, cols=100, *, near_db=-30.0, far_db=-20.0, signal_db=-25.0, water=20
):
    """A scene whose *noise* varies across range and whose ground does not.

    The first ``water`` columns of every range line return nothing, so what is
    recorded there is the floor at that range; the rest carry a constant
    backscatter on top of it. That separation is what makes the two estimators
    distinguishable at all: any residual gradient left in the land population
    after the subtraction came from the floor model, not from the scene.
    """
    np = pytest.importorskip("numpy")

    floor = 10.0 ** (np.linspace(near_db, far_db, rows) / 10.0)[:, None]
    power = np.tile(floor, (1, cols))
    power[:, water:] += 10.0 ** (signal_db / 10.0)
    return np.sqrt(power).astype("float32"), floor[:, 0]


def test_estimate_noise_profile_follows_the_floor_across_range():
    np = pytest.importorskip("numpy")

    amp, floor = _range_varying_noise_scene()
    profile, median, level_db, spread_db = convert._estimate_noise_profile(amp, decibels=False)

    # One floor per range line, shaped to broadcast across that line's azimuth
    # samples -- SICD stores range along the rows, so a row is one range.
    assert profile.shape == (amp.shape[0], 1)
    assert np.allclose(10.0 * np.log10(profile[:, 0]), 10.0 * np.log10(floor), atol=1e-4)
    # The profile's median stands for it, and its swing is the number that says
    # whether there was anything here for a constant floor to have missed.
    assert level_db == pytest.approx(-25.0, abs=1e-4)
    assert spread_db == pytest.approx(10.0, abs=1e-4)
    assert median > 0.0


def test_estimate_noise_profile_reads_the_same_tail_in_either_scale():
    np = pytest.importorskip("numpy")

    amp, _ = _range_varying_noise_scene()
    linear = convert._estimate_noise_profile(amp, decibels=False)[0]
    decibel = convert._estimate_noise_profile(20.0 * np.log10(amp), decibels=True)[0]
    assert np.allclose(linear, decibel, rtol=1e-4)


def test_estimate_noise_profile_interpolates_across_lines_with_no_dark_ground():
    """The fit is what makes a per-line read usable on a real scene."""
    np = pytest.importorskip("numpy")

    amp, floor = _range_varying_noise_scene()
    # A band of range lines crossing nothing but city: their low tail is dim
    # *backscatter*, well above the receiver. Contamination can only push a
    # line's tail up, which is why the trim is one-sided -- these lines are
    # dropped and the curve carries over them.
    amp[20:32, :] = math.sqrt(10.0 ** (0.0 / 10.0))
    profile, _, _, spread_db = convert._estimate_noise_profile(amp, decibels=False)

    assert np.allclose(10.0 * np.log10(profile[:, 0]), 10.0 * np.log10(floor), atol=0.2)
    assert spread_db == pytest.approx(10.0, abs=0.2)


def test_estimate_noise_profile_drops_lines_with_too_few_samples_to_read():
    np = pytest.importorskip("numpy")

    amp, floor = _range_varying_noise_scene()
    # Mostly-nodata lines (the edge of a clipped window): a low-tail percentile
    # over a handful of pixels is not a floor, so they are dropped rather than
    # believed -- even though the few pixels they do carry are dark.
    amp[40:48, convert._NOISE_PROFILE_MIN_SAMPLES - 1 :] = np.nan
    amp[40:48, : convert._NOISE_PROFILE_MIN_SAMPLES - 1] = math.sqrt(1e-9)
    profile, _, _, _ = convert._estimate_noise_profile(amp, decibels=False)

    # Believing them would have dragged the curve down toward -90 dB.
    assert np.allclose(10.0 * np.log10(profile[:, 0]), 10.0 * np.log10(floor), atol=0.5)


def test_estimate_noise_profile_refuses_when_too_few_lines_qualify():
    """It names the model that needs only one line, rather than fitting noise."""
    np = pytest.importorskip("numpy")

    with pytest.raises(ValueError, match="too few to fit"):
        convert._estimate_noise_profile(np.ones((2, 40), dtype="float32"), decibels=False)

    # A wide scene whose every line is too short to read is the same refusal.
    with pytest.raises(ValueError, match="estimated"):
        convert._estimate_noise_profile(np.ones((40, 4), dtype="float32"), decibels=False)


def test_estimate_noise_profile_rejects_input_it_cannot_fit_across():
    np = pytest.importorskip("numpy")

    with pytest.raises(ValueError, match="between 0 and 100"):
        convert._estimate_noise_profile(np.ones((40, 40)), decibels=False, percentile=0.0)
    with pytest.raises(ValueError, match="needs a 2-D image"):
        convert._estimate_noise_profile(np.ones(40), decibels=False)
    with pytest.raises(ValueError, match="no pixel carries a finite value"):
        convert._estimate_noise_profile(np.full((40, 40), np.nan), decibels=False)


def test_estimated_range_removes_the_gradient_the_constant_floor_leaves():
    """The whole claim of the model, measured rather than asserted."""
    np = pytest.importorskip("numpy")

    amp, _ = _range_varying_noise_scene()
    sicd = _FakeSicd()  # no Radiometric block: only the inferred models can run

    fitted, profile = convert._denoise_amplitude(sicd, amp, decibels=False, model="estimated-range")
    constant, scalar = convert._denoise_amplitude(sicd, amp, decibels=False, model="estimated")

    def land_gradient_db(corrected):
        # The ground under the swath is constant by construction, so whatever
        # gradient survives in it across range came from the floor model.
        land = np.square(np.asarray(corrected, dtype="float64"))[:, 20:].mean(axis=1)
        return float(10.0 * np.log10(land.max() / land.min()))

    assert land_gradient_db(fitted) < 0.1
    # One scalar over a floor that spans 10 dB leaves most of that span behind.
    assert land_gradient_db(constant) > 5.0

    # And the fitted model says how much variation it found, which is exactly
    # what the constant model was missing. The scalar reports no spread: its
    # spread is zero by construction, so recording it would say nothing.
    assert profile.floor_spread_db == pytest.approx(10.0, abs=1e-4)
    assert scalar.floor_spread_db is None


def test_denoise_estimated_range_ignores_the_clip_origin_like_the_constant_one():
    """A clip's floor is fitted over the clip's own rows: it sees no others."""
    np = pytest.importorskip("numpy")

    amp, _ = _range_varying_noise_scene()
    at_origin = convert._denoise_amplitude(
        _FakeSicd(), amp, decibels=False, model="estimated-range"
    )
    displaced = convert._denoise_amplitude(
        _FakeSicd(), amp, decibels=False, origin=(4096, 512), model="estimated-range"
    )
    assert np.array_equal(at_origin[0], displaced[0])
    assert at_origin[1] == displaced[1]


def test_geocoded_cog_records_the_range_profile_as_its_own_third_thing(tmp_path, monkeypatch):
    pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")

    _patch_open_complex(monkeypatch, _FakeReader(_fake_complex(24, 32), _FakeSicd()))
    out = convert.sicd_to_geocoded_cog(
        tmp_path / "in.ntf",
        tmp_path / "profiled.tif",
        gcp_grid=6,
        resampling="nearest",
        noise_subtract=True,
        noise_model="estimated-range",
    )
    tags = convert.read_conversion_tags(out)

    # Deliberately neither "absolute" nor "estimated": noise_subtraction is in
    # load.MEASUREMENT_PROVENANCE_KEYS, so this value is what makes to_stack
    # refuse to difference a fitted profile against a constant guess.
    assert tags["noise_subtraction"] == "estimated-range"
    # This fixture's brightness ramps along the rows, so there is a real swing
    # across range for the fit to find and report.
    assert float(tags["noise_floor_spread_db"]) > 0.0
    # The level is reported like the constant estimate's, so an inferred number
    # is always readable back off the file that carries it.
    assert math.isfinite(float(tags["noise_floor_db"]))


def test_conversion_tags_omit_the_spread_when_the_floor_could_not_vary():
    # The constant estimate and the measured polynomial both leave it off: one
    # has no spread by construction, the other's variation is the product's own
    # metadata rather than something this module inferred.
    estimated = convert.conversion_tags(
        source="scene.ntf", geocoded=True, noise_subtraction="estimated", noise_floor_db=-31.4
    )
    assert "UMBRA_NOISE_FLOOR_SPREAD_DB" not in estimated
    nothing_subtracted = convert.conversion_tags(
        source="scene.ntf", geocoded=True, noise_subtraction=None, noise_floor_spread_db=9.0
    )
    assert "UMBRA_NOISE_FLOOR_SPREAD_DB" not in nothing_subtracted


def test_cli_convert_noise_model_estimated_range_reports_the_swing(tmp_path, monkeypatch):
    pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    _patch_open_complex(monkeypatch, _FakeReader(_fake_complex(24, 32), _FakeSicd()))
    src = tmp_path / "scene.ntf"
    src.write_bytes(b"not-a-real-nitf")
    dst = tmp_path / "geo.tif"
    result = CliRunner().invoke(
        cli_mod.cli,
        [
            "convert",
            str(src),
            str(dst),
            "--subtract-noise",
            "--noise-model",
            "estimated-range",
            "--resampling",
            "nearest",
        ],
    )

    assert result.exit_code == 0, result.output
    # The three models are named apart on the way out as they are in the tags.
    assert "noise-profiled" in result.output
    assert "across the swath" in result.output
    # The profile has no single level, so the line says which number it quotes.
    assert "Fitted floor of median" in result.output
    assert convert.read_conversion_tags(dst)["noise_subtraction"] == "estimated-range"


# --------------------------------------------------------------------------- #
# Scoring an inferred floor against a measured one (``compare_noise_models``).
#
# The two inferred models were shipped on an argument -- that a percentile of a
# scene's darkest pixels reads the receiver, low but consistently so, and that
# fitting it per range line recovers the across-swath shape a scalar cannot.
# Neither claim could be checked on the archive they exist for, because a
# product that states no floor states no truth either. These tests supply that
# truth synthetically: a SICD whose NoisePoly *is* the floor the pixels were
# built from, so the difference between the estimate and the fact is a number.
# --------------------------------------------------------------------------- #


def _noise_poly_coefficients(rows, *, near_db, far_db):
    """A NoisePoly stating the ramp ``_range_varying_noise_scene`` builds.

    The scene's floor is linear in decibels from ``near_db`` at row 0 to
    ``far_db`` at the last row, and a SICD NoisePoly is a polynomial in image
    coordinates measured in metres from the SCP -- so with ``row_ss=1`` and the
    SCP on row 0, the row coordinate *is* the row index and the ramp is a
    first-degree coefficient. Anything else would put the truth and the pixels
    on different grids, which is the one mistake this fixture cannot make.
    """
    return [[near_db], [(far_db - near_db) / (rows - 1)]]


def _measuring_sicd(rows, *, near_db=-30.0, far_db=-20.0):
    """A ``_FakeSicd`` whose stated noise floor matches the scene it is paired with."""
    return _FakeSicd(
        radiometric=_radiometric_noise(
            coefs=_noise_poly_coefficients(rows, near_db=near_db, far_db=far_db)
        ),
        row_ss=1.0,
        col_ss=1.0,
        scp_row=0.0,
        scp_col=0.0,
    )


def _speckled_noise_scene(
    rows=64, cols=100, *, near_db=-30.0, far_db=-20.0, signal_db=-10.0, water=20, seed=0
):
    """The same ramp, but with speckle -- which is what biases an estimator.

    ``_range_varying_noise_scene`` puts every dark pixel at exactly the floor,
    so a percentile of them *is* the floor. A real noise-only population is
    exponentially distributed around its mean power, so its fifth percentile
    sits well below that mean: the conservative bias both inferred models carry
    and neither can see. This fixture is that population.
    """
    np = pytest.importorskip("numpy")

    rng = np.random.default_rng(seed)
    floor = 10.0 ** (np.linspace(near_db, far_db, rows) / 10.0)[:, None]
    power = rng.exponential(np.tile(floor, (1, cols)))
    power[:, water:] += rng.exponential(10.0 ** (signal_db / 10.0), size=(rows, cols - water))
    return np.sqrt(power).astype("float32")


def _compare(monkeypatch, tmp_path, magnitude, sicd, **kwargs):
    """Run ``compare_noise_models`` over a magnitude raster and a paired SICD."""
    _patch_open_complex(monkeypatch, _FakeReader(magnitude * (1 + 0j), sicd))
    src = tmp_path / "scene.ntf"
    src.write_bytes(b"not-a-real-nitf")
    return convert.compare_noise_models(src, **kwargs)


def test_compare_noise_models_splits_the_error_into_offset_and_shape(tmp_path, monkeypatch):
    """The measurement the two inferred models were shipped without."""
    pytest.importorskip("numpy")
    pytest.importorskip("sarpy")

    rows = 64
    magnitude, _ = _range_varying_noise_scene(rows=rows)
    comparison = _compare(monkeypatch, tmp_path, magnitude, _measuring_sicd(rows))

    assert comparison.source == "scene.ntf"
    assert comparison.shape == magnitude.shape
    # The truth: a floor ramping 10 dB across the swath, median at its midpoint.
    assert comparison.measured_spread_db == pytest.approx(10.0, abs=1e-6)
    assert comparison.measured_floor_db == pytest.approx(-25.0, abs=1e-6)

    scored = {agreement.model: agreement for agreement in comparison.models}
    assert tuple(scored) == convert.INFERRED_NOISE_MODELS

    # Every dark pixel in this fixture sits exactly on the floor, so the fitted
    # profile reads each line's level exactly right and the only thing left for
    # it to get wrong is the shape.
    assert scored["estimated-range"].bias_db == pytest.approx(0.0, abs=0.05)
    # The constant estimate reads low even here, with no speckle to blame: a low
    # percentile pooled over the whole scene lands near the *near-range* end of a
    # floor that ramps, not at its middle. That offset is a second thing the
    # scalar model gets wrong on a varying floor, and it is separate from the
    # shape error below -- which is why the two numbers are reported apart.
    assert -4.0 < scored["estimated"].bias_db < -1.0

    # A constant floor against a linear ramp: what is left after granting the
    # offset is the ramp's own deviation about its midpoint, 10/sqrt(12) dB.
    assert scored["estimated"].shape_error_db == pytest.approx(10.0 / math.sqrt(12.0), abs=0.15)
    assert scored["estimated"].spread_db == 0.0
    # The fitted profile follows it: that residual is what the model buys, and
    # it recovers the swing the constant one had no way to represent.
    assert scored["estimated-range"].shape_error_db < 0.05
    assert scored["estimated-range"].spread_db == pytest.approx(10.0, abs=0.05)
    assert scored["estimated-range"].residual_db < scored["estimated"].residual_db / 10.0


def test_compare_noise_models_measures_the_conservative_bias_speckle_causes(tmp_path, monkeypatch):
    """The other half of the claim: both models read low, by about the same."""
    pytest.importorskip("numpy")
    pytest.importorskip("sarpy")

    rows = 64
    comparison = _compare(
        monkeypatch, tmp_path, _speckled_noise_scene(rows=rows), _measuring_sicd(rows)
    )
    scored = {agreement.model: agreement for agreement in comparison.models}

    # A fifth percentile of an exponentially distributed noise-only population
    # sits below that population's mean power -- 10*log10(-ln(0.95)/0.05) worth
    # of it once the tail is read over a 20%-dark line -- so both models
    # under-subtract, leaving a little of the receiver in rather than taking
    # ground out. Under-subtraction is the safe direction, and this is how far it
    # goes: a number the archive this correction exists for cannot supply.
    assert -8.0 < scored["estimated-range"].bias_db < -3.0
    assert scored["estimated"].bias_db < 0.0
    # Nearly the same offset on both, which is the claim that lets the profile
    # carry the shape while the level stays conservative.
    assert abs(scored["estimated"].bias_db - scored["estimated-range"].bias_db) < 2.0

    # And it *is* very nearly the same offset on every line, which is why it
    # moves the fitted curve down without bending it: the profile recovers the
    # swing through the speckle, and what is left is a fifth of a decibel.
    assert scored["estimated-range"].shape_error_db < 0.5
    assert scored["estimated-range"].shape_error_db < scored["estimated"].shape_error_db / 5.0
    assert scored["estimated-range"].spread_db == pytest.approx(10.0, abs=0.5)
    # The constant model's shape error is the ramp's own deviation about its
    # midpoint, exactly as in the noiseless case: speckle does not change what a
    # scalar cannot represent.
    assert scored["estimated"].shape_error_db == pytest.approx(10.0 / math.sqrt(12.0), abs=0.15)


def test_compare_noise_models_shows_the_estimate_compressing_over_dark_ground(
    tmp_path, monkeypatch
):
    """A limit of the estimator that only a measurement could have found.

    The inference reads a range line's low tail as its floor, which holds while
    the ground is well above that floor. Where backscatter sinks *toward* it --
    a scene whose land returns about what the receiver does at far range -- the
    tail stops being a separate population at the far edge before it does at the
    near edge, so the fitted ramp reads flatter than the real one. The subtracted
    floor is still conservative (the bias only deepens), but the swing the model
    reports understates the swing that was there.
    """
    pytest.importorskip("numpy")
    pytest.importorskip("sarpy")

    rows = 64
    comparison = _compare(
        monkeypatch,
        tmp_path,
        _speckled_noise_scene(rows=rows, signal_db=-25.0),
        _measuring_sicd(rows),
    )
    fitted = next(a for a in comparison.models if a.model == "estimated-range")

    assert comparison.measured_spread_db == pytest.approx(10.0, abs=1e-6)
    assert 6.0 < fitted.spread_db < 9.0  # under-read, and knowably so
    assert fitted.bias_db < -5.0
    # Still far better than a scalar, which is the decision this informs: the
    # profile is worth using here, it just should not be quoted as the truth.
    assert fitted.shape_error_db < 1.5


def test_compare_noise_models_reads_the_truth_at_the_clipped_windows_coordinates(
    tmp_path, monkeypatch
):
    """A clip's measured floor is the swath it covers, not the scene's average."""
    np = pytest.importorskip("numpy")
    pytest.importorskip("sarpy")

    rows, cols = 64, 100
    magnitude, _ = _range_varying_noise_scene(rows=rows, cols=cols)
    sicd = _measuring_sicd(rows)
    # The ground under the far end of the swath, where the stated floor is
    # highest -- taken as the bounding box of that image region's own corners,
    # and wide enough to keep the dark columns the estimate reads inside it.
    corners = [_ground(sicd, row, col) for row in (48, rows - 1) for col in (0, 30)]
    bbox = (
        min(lon for lon, _ in corners),
        min(lat for _, lat in corners),
        max(lon for lon, _ in corners),
        max(lat for _, lat in corners),
    )
    clipped = _compare(monkeypatch, tmp_path, magnitude, sicd, bbox=bbox)
    whole = _compare(monkeypatch, tmp_path, magnitude, sicd)

    assert clipped.shape[0] < whole.shape[0]
    # Evaluating the NoisePoly at the window's own image coordinates is the
    # whole point: read at the array's rows instead, and the truth would be the
    # near-edge floor while the pixels came from the far edge.
    assert clipped.measured_floor_db > whole.measured_floor_db + 2.0
    assert clipped.measured_spread_db < whole.measured_spread_db
    # And the estimator still scores well against it, on the window's own tail.
    fitted = next(a for a in clipped.models if a.model == "estimated-range")
    assert abs(fitted.bias_db) < 0.5
    assert np.isfinite(fitted.shape_error_db)


def test_compare_noise_models_refuses_a_product_with_no_truth_to_check_against(
    tmp_path, monkeypatch
):
    """No stated floor, nothing to score: the same refusal 'measured' makes."""
    pytest.importorskip("numpy")
    pytest.importorskip("sarpy")

    magnitude, _ = _range_varying_noise_scene(rows=32, cols=40)
    # An Umbra open product: no Radiometric block at all.
    with pytest.raises(ValueError, match="no Radiometric metadata"):
        _compare(monkeypatch, tmp_path, magnitude, _FakeSicd())
    # A relative level describes the floor's variation without stating it, so it
    # is not a truth either -- the same reason it cannot be subtracted.
    relative = _FakeSicd(radiometric=_radiometric_noise("RELATIVE"))
    with pytest.raises(ValueError, match="relative noise level"):
        _compare(monkeypatch, tmp_path, magnitude, relative)


def test_compare_noise_models_rejects_measured_as_a_candidate(tmp_path, monkeypatch):
    pytest.importorskip("numpy")
    pytest.importorskip("sarpy")

    magnitude, _ = _range_varying_noise_scene(rows=32, cols=40)
    with pytest.raises(ValueError, match="reference"):
        _compare(monkeypatch, tmp_path, magnitude, _measuring_sicd(32), models=("measured",))
    with pytest.raises(ValueError, match="at least one"):
        _compare(monkeypatch, tmp_path, magnitude, _measuring_sicd(32), models=())


def test_cli_convert_noise_check_prints_the_comparison(tmp_path, monkeypatch):
    pytest.importorskip("numpy")
    pytest.importorskip("sarpy")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    rows = 64
    magnitude, _ = _range_varying_noise_scene(rows=rows)
    _patch_open_complex(monkeypatch, _FakeReader(magnitude * (1 + 0j), _measuring_sicd(rows)))
    src = tmp_path / "scene.ntf"
    src.write_bytes(b"not-a-real-nitf")

    result = CliRunner().invoke(cli_mod.cli, ["convert", str(src), "--noise-check"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["measured_spread_db"] == pytest.approx(10.0, abs=1e-6)
    models = {entry["model"]: entry for entry in payload["models"]}
    assert set(models) == set(convert.INFERRED_NOISE_MODELS)
    assert models["estimated-range"]["shape_error_db"] < models["estimated"]["shape_error_db"]

    # It reads SRC and writes nothing, so a DST is a mistake worth naming.
    with_dst = CliRunner().invoke(
        cli_mod.cli, ["convert", str(src), str(tmp_path / "out.tif"), "--noise-check"]
    )
    assert with_dst.exit_code != 0
    assert "writes nothing" in with_dst.output

    # The two read-only modes read different things -- a converted raster's tags
    # and a SICD's pixels -- so asking for both is a question, not a request.
    both = CliRunner().invoke(cli_mod.cli, ["convert", str(src), "--noise-check", "--provenance"])
    assert both.exit_code != 0
    assert "pick one" in both.output


def test_cli_convert_noise_check_without_a_stated_floor_is_a_clean_error(tmp_path, monkeypatch):
    pytest.importorskip("numpy")
    pytest.importorskip("sarpy")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    magnitude, _ = _range_varying_noise_scene(rows=32, cols=40)
    _patch_open_complex(monkeypatch, _FakeReader(magnitude * (1 + 0j), _FakeSicd()))
    src = tmp_path / "scene.ntf"
    src.write_bytes(b"not-a-real-nitf")

    result = CliRunner().invoke(cli_mod.cli, ["convert", str(src), "--noise-check"])
    assert result.exit_code != 0
    # The refusal names what the product does carry, rather than inventing one.
    assert "no Radiometric metadata" in result.output


# --------------------------------------------------------------------------- #
# Speckle filtering.
#
# Speckle is multiplicative and is the same physics on every SAR image, so it
# can be *made* here rather than faked: a single-look intensity is exponentially
# distributed about its surface's true backscatter, which numpy draws directly.
# That makes every claim in the module checkable -- the ENL of single-look
# imagery is 1, a boxcar over N pixels reaches about N looks, and Lee keeps an
# edge the boxcar smears.
# --------------------------------------------------------------------------- #


def _speckle_field(rows=192, cols=192, *, bright=0.1, dark=0.01, seed=7):
    """A two-surface single-look scene: exponential speckle about a step in power.

    Returned as linear *power*, which is the domain the filters work in; the
    conversion-level tests turn it into magnitude or decibels as needed.
    """
    np = pytest.importorskip("numpy")

    rng = np.random.default_rng(seed)
    truth = np.where(np.arange(cols)[None, :] < cols // 2, bright, dark) * np.ones((rows, 1))
    return rng.exponential(truth), truth


def test_speckle_window_must_be_odd_and_at_least_three():
    for bad in (2, 4, 1, 0, -3):
        with pytest.raises(ValueError, match="odd integer"):
            convert._check_speckle_window(bad)
    assert convert._check_speckle_window(3) == 3
    assert convert._check_speckle_window(9) == 9


def test_unknown_speckle_filter_is_refused():
    np = pytest.importorskip("numpy")

    with pytest.raises(ValueError, match="Unknown speckle_filter"):
        convert._filter_speckle(np.ones((8, 8), dtype="float32"), decibels=False, name="frost")


def test_estimate_enl_reads_single_look_imagery_as_one_look():
    pytest.importorskip("numpy")

    power, _truth = _speckle_field()
    # The definition, measured: a single-look intensity's standard deviation
    # equals its mean, so mean^2/variance is 1 -- and it is 1 across a step in
    # brightness too, because the estimate is per block rather than per scene.
    assert convert._estimate_enl(power, block=16) == pytest.approx(1.0, abs=0.15)


def test_estimate_enl_is_none_without_a_block_to_read():
    np = pytest.importorskip("numpy")

    power, _truth = _speckle_field(rows=8, cols=8)
    assert convert._estimate_enl(power, block=16) is None
    # A constant raster has no speckle to count the looks of, which is an absent
    # answer rather than an infinite one.
    assert convert._estimate_enl(np.full((64, 64), 3.0), block=16) is None


def test_boxcar_reaches_about_the_looks_it_averaged():
    np = pytest.importorskip("numpy")

    # One surface, so the only variance in the scene is speckle: this is the
    # estimator's calibration rather than a test of how it copes with structure.
    power = np.random.default_rng(17).exponential(np.full((320, 320), 0.05))
    for window in (3, 5, 9):
        block = max(convert._ENL_BLOCK, convert._ENL_BLOCK_WINDOWS * window)
        enl = convert._estimate_enl(convert._boxcar_power(power, window), block=block)
        # The pixels of this synthetic scene are independent, so a window of N^2
        # of them averages N^2 independent looks -- which is the claim
        # ``_ENL_BLOCK_WINDOWS`` exists to keep true. A fixed 16-pixel block
        # instead reads 15-25 % high on the wider windows, because a block only
        # two windows across holds too few independent samples to divide by.
        assert enl == pytest.approx(window**2, rel=0.1)
    naive = convert._estimate_enl(convert._boxcar_power(power, 9), block=convert._ENL_BLOCK)
    assert naive > 1.1 * 81


def test_boxcar_preserves_the_mean_power_of_each_surface():
    np = pytest.importorskip("numpy")

    power, truth = _speckle_field()
    filtered = convert._boxcar_power(power, 5)
    # Averaging removes variance, not signal: well inside each surface the mean
    # is unchanged, which is what makes the filter a better *estimate* of the
    # same quantity rather than a different one.
    for cols in (slice(8, 88), slice(104, 184)):
        assert float(filtered[:, cols].mean()) == pytest.approx(
            float(truth[:, cols].mean()), rel=0.05
        )
    assert float(np.nanstd(filtered[:, 8:88])) < 0.3 * float(np.nanstd(power[:, 8:88]))


def test_lee_keeps_the_edge_the_boxcar_smears():
    np = pytest.importorskip("numpy")

    power, _truth = _speckle_field()
    edge = power.shape[1] // 2
    band = slice(edge - 8, edge), slice(edge, edge + 8)

    def step_db(arr):
        return 10.0 * math.log10(float(arr[:, band[0]].mean()) / float(arr[:, band[1]].mean()))

    boxcar = convert._boxcar_power(power, 9)
    lee = convert._lee_power(power, 9, looks=1.0)
    # The truth is a 10 dB step. Both filters average across it, but Lee only
    # averages where the window is no more variable than speckle alone explains,
    # so it holds more of the contrast -- the whole reason it exists.
    assert step_db(lee) > step_db(boxcar)
    assert step_db(lee) == pytest.approx(10.0, abs=3.5)
    # And it still removes speckle: the ENL rises well above single-look.
    block = convert._ENL_BLOCK_WINDOWS * 9
    assert convert._estimate_enl(lee, block=block) > 20.0
    # Homogeneous ground is smoothed as hard as the boxcar smooths it: the two
    # differ at structure, not everywhere.
    assert float(np.nanstd(lee[:, 8:80])) == pytest.approx(
        float(np.nanstd(boxcar[:, 8:80])), rel=0.4
    )


def test_lee_takes_its_looks_from_the_scene_but_never_below_single_look():
    np = pytest.importorskip("numpy")

    rng = np.random.default_rng(11)
    # Texture on top of speckle: every block is more variable than speckle alone,
    # so the ENL estimate reads *below* 1. No product has fewer looks than one,
    # so believing that read would tell the filter speckle is worse than it is --
    # licence to smooth structure away.
    textured = rng.exponential(0.05 * np.exp(rng.normal(0.0, 0.6, (128, 128))))
    assert convert._estimate_enl(textured, block=16) < 1.0

    _filtered, info = convert._filter_speckle(
        10.0 * np.log10(textured), decibels=True, name="lee", window=5
    )
    assert info.enl_before is not None and info.enl_before < 1.0
    assert info.looks == 1.0


def test_filter_speckle_reports_what_it_did_and_is_scale_independent():
    np = pytest.importorskip("numpy")

    power, _truth = _speckle_field()
    from_db, db_info = convert._filter_speckle(
        10.0 * np.log10(power), decibels=True, name="boxcar", window=5
    )
    from_linear, lin_info = convert._filter_speckle(
        np.sqrt(power), decibels=False, name="boxcar", window=5
    )
    # Filtering happens in the power domain whichever scale the raster arrived
    # in, so the two paths are the same measurement -- a mean of decibels would
    # be the geometric mean of the powers, biased low by ~2.5 dB on single-look
    # speckle.
    assert np.allclose(
        10.0 ** (from_db.astype("float64") / 10.0),
        np.square(from_linear.astype("float64")),
        rtol=1e-4,
    )
    for info in (db_info, lin_info):
        assert (info.filter, info.window, info.looks) == ("boxcar", 5, None)
        assert info.enl_before == pytest.approx(1.0, abs=0.15)
        assert info.enl_after == pytest.approx(25.0, rel=0.15)


def test_filter_speckle_keeps_nodata_and_averages_the_neighbours_it_has():
    np = pytest.importorskip("numpy")

    power, _truth = _speckle_field(rows=64, cols=64)
    holed = power.copy()
    holed[10, 10] = np.nan
    # Linear magnitude in, linear magnitude out; the filter squares it internally.
    filtered, _info = convert._filter_speckle(
        np.sqrt(holed), decibels=False, name="boxcar", window=3
    )

    # A nodata pixel stays nodata (it is not ground to average), and its
    # neighbours are averaged from the valid pixels they do have rather than
    # dragged toward zero.
    assert not np.isfinite(filtered[10, 10])
    assert np.isfinite(filtered[10, 11])
    window = holed[9:12, 10:13]
    assert float(filtered[10, 11]) ** 2 == pytest.approx(float(np.nanmean(window)), rel=1e-4)


def _speckled_sicd_scene(rows=96, cols=96):
    """A fake complex SICD whose magnitudes carry real single-look speckle."""
    np = pytest.importorskip("numpy")

    power, _truth = _speckle_field(rows=rows, cols=cols, seed=13)
    return np.sqrt(power) * (1 + 0j)


def test_sicd_to_geocoded_cog_records_the_speckle_filter(tmp_path, monkeypatch):
    rasterio = pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")
    pytest.importorskip("numpy")

    data = _speckled_sicd_scene()
    _patch_open_complex(monkeypatch, _FakeReader(data, _FakeSicd()))
    out = convert.sicd_to_geocoded_cog(
        tmp_path / "in.ntf",
        tmp_path / "geo.tif",
        resolution=0.01,
        resampling="nearest",
        speckle_filter="lee",
        speckle_window=5,
    )

    tags = convert.read_conversion_tags(out)
    assert tags["speckle_filter"] == "lee"
    assert tags["speckle_window"] == "5"
    # The diagnostics: what the filter started from and reached on this scene,
    # plus the looks it assumed, so a pixel value is reproducible.
    assert float(tags["speckle_enl_before"]) == pytest.approx(1.0, abs=0.2)
    assert float(tags["speckle_enl_after"]) > 5.0 * float(tags["speckle_enl_before"])
    assert float(tags["speckle_looks"]) == pytest.approx(1.0, abs=0.2)
    with rasterio.open(out) as ds:
        assert ds.crs.to_epsg() == 4326


def test_an_unfiltered_conversion_says_so_rather_than_omitting_the_key(tmp_path, monkeypatch):
    pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")
    pytest.importorskip("numpy")

    _patch_open_complex(monkeypatch, _FakeReader(_fake_complex(16, 16), _FakeSicd()))
    out = convert.sicd_to_amplitude_geotiff(tmp_path / "in.ntf", tmp_path / "amp.tif")
    tags = convert.read_conversion_tags(out)
    # ``"none"`` rather than a missing key, so a stack of unfiltered passes agrees
    # on it and nothing has to interpret an absence.
    assert tags["speckle_filter"] == "none"
    assert "speckle_window" not in tags
    assert "speckle_enl_after" not in tags


def test_slant_plane_speckle_filter_smooths_and_records(tmp_path, monkeypatch):
    rasterio = pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")
    np = pytest.importorskip("numpy")

    data = _speckled_sicd_scene()
    _patch_open_complex(monkeypatch, _FakeReader(data, _FakeSicd()))
    raw = convert.sicd_to_amplitude_geotiff(
        tmp_path / "in.ntf", tmp_path / "raw.tif", decibels=False
    )
    _patch_open_complex(monkeypatch, _FakeReader(data, _FakeSicd()))
    filtered = convert.sicd_to_amplitude_geotiff(
        tmp_path / "in.ntf",
        tmp_path / "filtered.tif",
        decibels=False,
        speckle_filter="boxcar",
        speckle_window=5,
    )

    with rasterio.open(raw) as ds:
        before = ds.read(1)
    with rasterio.open(filtered) as ds:
        after = ds.read(1)
    assert after.shape == before.shape  # the filter changes values, not geometry
    assert float(np.nanstd(after)) < float(np.nanstd(before))
    assert convert.read_conversion_tags(filtered)["speckle_filter"] == "boxcar"


def test_a_bad_speckle_window_is_refused_before_the_product_is_read(tmp_path, monkeypatch):
    pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")
    pytest.importorskip("numpy")

    reader = _FakeReader(_fake_complex(16, 16), _FakeSicd())
    _patch_open_complex(monkeypatch, reader)
    with pytest.raises(ValueError, match="odd integer"):
        convert.sicd_to_geocoded_cog(
            tmp_path / "in.ntf",
            tmp_path / "geo.tif",
            speckle_filter="boxcar",
            speckle_window=4,
        )
    # Nothing was read: an unusable window is worth finding out about without
    # first pulling a multi-gigabyte scene through the amplitude detection.
    assert reader.reads == []


def test_cli_convert_speckle_filter_reports_the_looks_it_reached(tmp_path, monkeypatch):
    pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")
    pytest.importorskip("numpy")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    _patch_open_complex(monkeypatch, _FakeReader(_speckled_sicd_scene(), _FakeSicd()))
    src = tmp_path / "scene.ntf"
    src.write_bytes(b"not-a-real-nitf")

    result = CliRunner().invoke(
        cli_mod.cli,
        [
            "convert",
            str(src),
            str(tmp_path / "geo.tif"),
            "--speckle-filter",
            "boxcar",
            "--speckle-window",
            "5",
            "--resolution",
            "0.01",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "boxcar-filtered geocoded COG" in result.output
    # The window says how many pixels were averaged; the ENL says how many
    # independent looks that was, which is the number worth printing.
    assert "Equivalent looks" in result.output
    assert "of 25 pixels averaged" in result.output


def test_cli_convert_rejects_an_even_speckle_window_as_a_parameter_error(tmp_path, monkeypatch):
    pytest.importorskip("sarpy")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    _patch_open_complex(monkeypatch, _FakeReader(_fake_complex(16, 16), _FakeSicd()))
    src = tmp_path / "scene.ntf"
    src.write_bytes(b"not-a-real-nitf")

    result = CliRunner().invoke(
        cli_mod.cli,
        [
            "convert",
            str(src),
            str(tmp_path / "geo.tif"),
            "--speckle-filter",
            "lee",
            "--speckle-window",
            "6",
        ],
    )
    assert result.exit_code != 0
    # A typo in a flag, reported as one -- not as a conversion failure.
    assert "--speckle-window" in result.output
    assert "odd integer" in result.output


def _tagged_raster(path, **tags):
    """A 1-pixel GeoTIFF carrying whatever conversion tags a test wants read back.

    ``_echo_speckle_report`` is a pure reader of the output's own tags, so its
    branches are exercised against handcrafted ones rather than by hunting for a
    synthetic scene that happens to produce each case.
    """
    rasterio = pytest.importorskip("rasterio")
    np = pytest.importorskip("numpy")
    from rasterio.transform import from_origin

    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=1,
        width=1,
        count=1,
        dtype="float32",
        transform=from_origin(0, 0, 1, 1),
    ) as ds:
        ds.write(np.ones((1, 1), dtype="float32"), 1)
        ds.update_tags(**convert.conversion_tags(source="scene.nitf", geocoded=True, **tags))
    return path


def test_speckle_report_says_when_a_window_bought_little(tmp_path, capsys):
    pytest.importorskip("rasterio")
    from umbra_py.cli.process import _echo_speckle_report

    _echo_speckle_report(
        _tagged_raster(
            tmp_path / "meagre.tif",
            speckle_filter="lee",
            speckle_window=5,
            speckle_enl_before=1.0,
            speckle_enl_after=1.2,
            speckle_looks=1.0,
        )
    )
    out = capsys.readouterr().out
    assert "Equivalent looks 1.0 -> 1.2, of 25 pixels averaged" in out
    # An advisory, and one that names both honest explanations: on imagery that
    # is textured everywhere 'lee' is *meant* to leave most pixels alone.
    assert "bought" in out and "textured almost everywhere" in out

    _echo_speckle_report(
        _tagged_raster(
            tmp_path / "ample.tif",
            speckle_filter="boxcar",
            speckle_window=5,
            speckle_enl_before=1.0,
            speckle_enl_after=18.0,
        )
    )
    ample = capsys.readouterr().out
    assert "Equivalent looks 1.0 -> 18.0" in ample
    assert "bought" not in ample


def test_speckle_report_says_when_there_was_no_block_to_measure(tmp_path, capsys):
    pytest.importorskip("rasterio")
    from umbra_py.cli.process import _echo_speckle_report

    # A raster smaller than one measuring block (or with no uniform block in it)
    # still got filtered; the missing number is the ENL gain, not the filter.
    _echo_speckle_report(
        _tagged_raster(tmp_path / "tiny.tif", speckle_filter="boxcar", speckle_window=3)
    )
    out = capsys.readouterr().out
    assert "No homogeneous block to measure looks in" in out
    assert "the filter still ran" in out


def test_cli_convert_slant_plane_names_the_filter_and_its_looks(tmp_path, monkeypatch):
    pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")
    pytest.importorskip("numpy")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod

    _patch_open_complex(monkeypatch, _FakeReader(_speckled_sicd_scene(), _FakeSicd()))
    src = tmp_path / "scene.ntf"
    src.write_bytes(b"not-a-real-nitf")

    result = CliRunner().invoke(
        cli_mod.cli,
        [
            "convert",
            str(src),
            str(tmp_path / "amp.tif"),
            "--slant-plane",
            "--speckle-filter",
            "lee",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "slant-plane lee-filtered amplitude GeoTIFF" in result.output
    # Lee's own speckle parameter is part of what made the pixels, so it is
    # printed as well as recorded.
    assert "Speckle taken as" in result.output
    assert "Equivalent looks" in result.output


def test_slant_plane_conversion_refuses_an_unknown_speckle_filter(tmp_path, monkeypatch):
    pytest.importorskip("sarpy")
    pytest.importorskip("numpy")

    reader = _FakeReader(_fake_complex(8, 8), _FakeSicd())
    _patch_open_complex(monkeypatch, reader)
    with pytest.raises(ValueError, match="Unknown speckle_filter"):
        convert.sicd_to_amplitude_geotiff(
            tmp_path / "in.ntf", tmp_path / "amp.tif", speckle_filter="frost"
        )
    assert reader.reads == []


# --------------------------------------------------------------------------- #
# A product that cannot support the measurement says so before its pixels are
# read (and says it with a type a caller can catch narrowly).
# --------------------------------------------------------------------------- #


def test_unsupported_measurement_is_still_a_value_error():
    """Naming the refusal is not a breaking change for anyone catching one."""
    from umbra_py.exceptions import UmbraError, UnsupportedMeasurementError

    exc = UnsupportedMeasurementError("no Radiometric block", hint="drop --calibrate")
    assert isinstance(exc, ValueError)
    assert isinstance(exc, UmbraError)
    assert exc.to_dict() == {
        "error": "UnsupportedMeasurementError",
        "message": "no Radiometric block",
        "hint": "drop --calibrate",
    }


def test_an_uncalibratable_product_is_refused_before_the_scene_is_read(tmp_path, monkeypatch):
    pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")
    pytest.importorskip("numpy")
    from umbra_py.exceptions import UnsupportedMeasurementError

    # The default fake scene carries no Radiometric block -- an Umbra open
    # product, and the case the refusal exists for.
    reader = _FakeReader(_fake_complex(8, 10), _FakeSicd())
    _patch_open_complex(monkeypatch, reader)

    with pytest.raises(UnsupportedMeasurementError, match="no Radiometric metadata"):
        convert.sicd_to_geocoded_cog(
            tmp_path / "in.ntf", tmp_path / "out.tif", calibration="sigma0"
        )

    # The point: no slice of the complex product was ever asked for. On a real
    # scene that is gigabytes of NITF pulled through amplitude detection to
    # learn something the header already knew.
    assert reader.reads == []


def test_a_measured_noise_floor_is_refused_before_the_scene_is_read(tmp_path, monkeypatch):
    pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")
    pytest.importorskip("numpy")
    from umbra_py.exceptions import UnsupportedMeasurementError

    reader = _FakeReader(_fake_complex(8, 10), _FakeSicd())
    _patch_open_complex(monkeypatch, reader)

    with pytest.raises(UnsupportedMeasurementError, match="noise floor cannot be subtracted"):
        convert.sicd_to_geocoded_cog(
            tmp_path / "in.ntf",
            tmp_path / "out.tif",
            noise_subtract=True,
            noise_model="measured",
        )
    assert reader.reads == []


def test_an_inferred_noise_floor_needs_no_metadata_and_still_reads(tmp_path, monkeypatch):
    """The check is scoped to the models that depend on the product saying so."""
    pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")
    pytest.importorskip("numpy")

    reader = _FakeReader(_fake_complex(8, 10), _FakeSicd())
    _patch_open_complex(monkeypatch, reader)

    convert.sicd_to_geocoded_cog(
        tmp_path / "in.ntf",
        tmp_path / "out.tif",
        noise_subtract=True,
        noise_model="estimated",
    )
    assert reader.reads  # the estimate reads the scene, which is where its floor is


def test_the_slant_plane_writer_refuses_before_the_read_too(tmp_path, monkeypatch):
    pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")
    pytest.importorskip("numpy")
    from umbra_py.exceptions import UnsupportedMeasurementError

    reader = _FakeReader(_fake_complex(8, 10), _FakeSicd())
    _patch_open_complex(monkeypatch, reader)

    with pytest.raises(UnsupportedMeasurementError, match="no Radiometric metadata"):
        convert.sicd_to_amplitude_geotiff(
            tmp_path / "in.ntf", tmp_path / "out.tif", calibration="beta0"
        )
    assert reader.reads == []


def test_a_malformed_request_stays_a_plain_value_error(tmp_path, monkeypatch):
    """An unknown calibration name is the caller's mistake, not the product's."""
    pytest.importorskip("rasterio")
    pytest.importorskip("sarpy")
    from umbra_py.exceptions import UnsupportedMeasurementError

    _patch_open_complex(monkeypatch, _FakeReader(_fake_complex(8, 10), _FakeSicd()))

    with pytest.raises(ValueError, match="Unknown calibration") as caught:
        convert.sicd_to_geocoded_cog(
            tmp_path / "in.ntf", tmp_path / "out.tif", calibration="nonesuch"
        )
    assert not isinstance(caught.value, UnsupportedMeasurementError)
