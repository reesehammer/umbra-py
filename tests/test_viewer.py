"""Offline tests for the interactive tile viewer (``umbra_py.viewer``).

Like ``test_load.py``, these build a tiny real GeoTIFF on disk and point a
synthetic ``UmbraItem`` at it, so the tile-render path is exercised end to end
without any network access.
"""

from __future__ import annotations

import math
import threading
import urllib.request

import pytest

from umbra_py.models import UmbraItem


def _make_geotiff(path, *, width=64, height=48):
    """Write a small north-up UTM GeoTIFF and return (path, bounds, crs)."""
    rasterio = pytest.importorskip("rasterio")
    np = pytest.importorskip("numpy")
    from rasterio.transform import from_origin

    # Positive amplitudes so the SAR stretch has valid pixels, with a zero
    # (nodata-like) corner to exercise the transparency path.
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
        "nodata": 0.0,
    }
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data, 1)
        bounds = dst.bounds
        crs = dst.crs
    return path, bounds, crs


def _item_for(tif_path):
    item = UmbraItem(id="test-acq", properties={"datetime": "2024-02-08T12:00:00Z"})
    item.asset_href = lambda asset="GEC": str(tif_path)  # type: ignore[method-assign]
    return item


def _tile_for_lonlat(lon, lat, z):
    """Standard XYZ tile index containing ``(lon, lat)`` at zoom ``z``."""
    n = 2**z
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
    return x, y


def test_tiler_computes_cuts_and_bounds(tmp_path):
    pytest.importorskip("rasterio")
    from rasterio.warp import transform_bounds

    from umbra_py.viewer import SceneTiler

    tif, bounds, crs = _make_geotiff(tmp_path / "scene.tif")
    tiler = SceneTiler(_item_for(tif))

    lo, hi = tiler.cuts
    assert hi > lo  # a real stretch range
    expected = transform_bounds(crs, "EPSG:4326", *bounds)
    assert tiler.bounds_4326 == pytest.approx(expected)


def test_tile_renders_png_in_scene(tmp_path):
    pytest.importorskip("rasterio")
    pytest.importorskip("PIL")
    import io

    from PIL import Image

    tif, bounds, crs = _make_geotiff(tmp_path / "scene.tif")
    from rasterio.warp import transform_bounds

    from umbra_py.viewer import SceneTiler

    tiler = SceneTiler(_item_for(tif))
    w, s, e, n = transform_bounds(crs, "EPSG:4326", *bounds)
    # A zoom coarse enough that the ~600 m scene fits inside a single tile, so
    # the tile has both opaque (scene) and transparent (off-scene) pixels.
    z = 14
    x, y = _tile_for_lonlat((w + e) / 2, (s + n) / 2, z)

    png = tiler.tile(z, x, y)
    assert png is not None
    img = Image.open(io.BytesIO(png))
    assert img.size == (256, 256)
    assert img.mode == "RGBA"
    # Some pixels are opaque (the scene) and some transparent (off-scene + the
    # nodata corner), so the alpha channel is genuinely mixed.
    alpha = img.split()[3].getextrema()
    assert alpha[0] == 0 and alpha[1] == 255


def test_tile_off_scene_returns_none(tmp_path):
    pytest.importorskip("rasterio")
    from rasterio.warp import transform_bounds

    from umbra_py.viewer import SceneTiler

    tif, bounds, crs = _make_geotiff(tmp_path / "scene.tif")
    w, s, e, n = transform_bounds(crs, "EPSG:4326", *bounds)
    tiler = SceneTiler(_item_for(tif))

    # 10 degrees east of the footprint -- nowhere near the scene.
    z = 12
    x, y = _tile_for_lonlat(e + 10.0, (s + n) / 2, z)
    assert tiler.tile(z, x, y) is None


def test_make_viewer_server_serves_page_and_tile(tmp_path):
    pytest.importorskip("rasterio")
    from rasterio.warp import transform_bounds

    from umbra_py.viewer import make_viewer_server

    tif, bounds, crs = _make_geotiff(tmp_path / "scene.tif")
    httpd, url = make_viewer_server(_item_for(tif))

    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            assert resp.status == 200
            page = resp.read().decode()
        assert "test-acq" in page
        assert "tiles/{z}/{x}/{y}.png" in page

        w, s, e, n = transform_bounds(crs, "EPSG:4326", *bounds)
        z = 17
        x, y = _tile_for_lonlat((w + e) / 2, (s + n) / 2, z)
        with urllib.request.urlopen(f"{url}tiles/{z}/{x}/{y}.png", timeout=5) as resp:
            assert resp.status == 200
            assert resp.headers["Content-Type"] == "image/png"
            assert resp.read()[:8] == b"\x89PNG\r\n\x1a\n"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_proj_env_options_points_at_bundled_db():
    pytest.importorskip("rasterio")
    import os

    from umbra_py.viewer import _proj_env_options

    opts = _proj_env_options()
    # When rasterio ships bundled PROJ data (wheels do), we force PROJ_DATA at
    # it so a stale shell PROJ_LIB/PROJ_DATA can't break reprojection.
    if opts:
        assert os.path.exists(os.path.join(opts["PROJ_DATA"], "proj.db"))


def test_send_swallows_client_disconnect():
    """A browser that cancels a tile mid-write must not crash the handler."""
    from umbra_py.viewer import _ViewerHandler

    handler = _ViewerHandler.__new__(_ViewerHandler)
    handler.command = "GET"
    handler.send_response = lambda *a, **k: None
    handler.send_header = lambda *a, **k: None
    handler.end_headers = lambda: None

    class _Boom:
        def write(self, _body):
            raise BrokenPipeError

    handler.wfile = _Boom()
    # Should return quietly rather than propagate the BrokenPipeError.
    handler._send(200, "image/png", b"payload")


def test_cli_view_boots_and_stops(tmp_path, monkeypatch):
    pytest.importorskip("rasterio")
    from click.testing import CliRunner

    from umbra_py import cli as cli_mod
    from umbra_py.viewer import _ViewerServer

    src_tif, _, _ = _make_geotiff(tmp_path / "scene.tif")
    monkeypatch.setattr(cli_mod, "get_json", lambda url: {"id": "cli-acq", "assets": {}})
    monkeypatch.setattr(cli_mod.UmbraItem, "asset_href", lambda self, asset="GEC": str(src_tif))

    opened = {}
    monkeypatch.setattr("webbrowser.open", lambda u: opened.setdefault("url", u))
    # Don't actually block: return immediately as if Ctrl-C was pressed.
    monkeypatch.setattr(_ViewerServer, "serve_forever", lambda self: None)

    result = CliRunner().invoke(cli_mod.cli, ["view", "http://example.com/item.json"])
    assert result.exit_code == 0, result.output
    assert "Serving SAR viewer" in result.output
    assert opened.get("url", "").startswith("http://127.0.0.1:")
