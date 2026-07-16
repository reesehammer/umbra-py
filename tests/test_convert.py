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
    """Minimal SICD projection model: an affine image(row,col) -> lon/lat map."""

    def __init__(self, lon0=-100.0, lat0=40.0, dlon=0.01, dlat=0.01, skew=0.002):
        self.lon0, self.lat0, self.dlon, self.dlat, self.skew = lon0, lat0, dlon, dlat, skew
        self.calls: list[tuple] = []

    def project_image_to_ground_geo(self, im_points, ordering="latlong", projection_type="HAE"):
        import numpy as np

        self.calls.append((ordering, projection_type))
        pts = np.asarray(im_points, dtype="float64")
        rows, cols = pts[:, 0], pts[:, 1]
        lon = self.lon0 + cols * self.dlon + rows * self.skew
        lat = self.lat0 - rows * self.dlat + cols * self.skew
        hae = np.zeros_like(lon)
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
