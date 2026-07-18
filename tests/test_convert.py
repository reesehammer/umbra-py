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
    ):
        self.lon0, self.lat0, self.dlon, self.dlat, self.skew = lon0, lat0, dlon, dlat, skew
        self.hae_shift = hae_shift
        self.calls: list[tuple] = []
        self.SCPCOA = self._SCPCOA(incidence, azimuth)

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
