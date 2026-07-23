"""Interactive full-resolution web viewer for a single Umbra SAR scene.

Every other rendering surface in the toolkit collapses a scene to a *fixed*
picture: ``quicklook`` writes one downsampled PNG, the map overlays bake a
low-res preview into HTML. That throws away the resolution that makes Umbra
special -- a GEC scene is ~25 cm imagery, tens of thousands of pixels on a
side. This module lets you actually *explore* it.

:func:`view` starts a tiny local tile server and opens a Leaflet page in the
browser. As you pan and zoom, the page requests standard slippy-map tiles
(``/tiles/{z}/{x}/{y}.png``); the server reads just the matching window out of
the remote cloud-optimized GeoTIFF via GDAL's ``/vsicurl/`` driver (HTTP range
requests against the public bucket) and warps it into the Web-Mercator tile
grid. Only the tiles in view -- at the COG overview level that matches the
current zoom -- are ever fetched, so you roam a multi-gigabyte scene at native
resolution without downloading it.

**One global contrast stretch.** SAR amplitude has enormous dynamic range, so
each tile must be stretched -- but a per-tile percentile stretch would give
neighbouring tiles different contrast and visible seams. So the percentile cuts
are computed *once* from a whole-scene overview at startup (see
:func:`umbra_py.viz._amplitude_cuts`) and applied to every tile, exactly the
stretch the static :func:`umbra_py.viz.quicklook` would pick for the scene.

**Pixel-accurate placement.** Unlike the browser-side lazy overlay (which
stretches a north-up UTM raster onto its lat/lon bbox as a quick-look
approximation), tiles are warped through GDAL into true Web Mercator, so the
imagery lines up with the OpenStreetMap basemap.

Requires the ``viz`` extra (``pip install "umbra-py[viz]"``).
"""

from __future__ import annotations

import math
import re
import threading
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from ._html import safe_href
from .exceptions import AssetNotFoundError
from .models import UmbraItem
from .viz import _amplitude_cuts, _require, _stretch_to_rgba

#: Slippy-map tiles are 256x256, the web-mapping standard Leaflet expects.
TILE_SIZE = 256

#: Web-Mercator (EPSG:3857) half-extent in metres: ``pi * 6378137``. The
#: projection is square and spans ``[-R, R]`` on both axes.
_MERC_ORIGIN = math.pi * 6378137.0


def _open_path(url: str) -> str:
    """Path to hand ``rasterio.open``: stream remote COGs, open local files directly.

    Umbra's public assets are ``https`` cloud-optimized GeoTIFFs, which GDAL
    reads with range requests via the ``/vsicurl/`` driver; a plain local path
    (used in tests, or for an already-downloaded file) is opened as-is. Mirrors
    :func:`umbra_py.load._open_path`.
    """
    if url.startswith(("http://", "https://")):
        return f"/vsicurl/{url}"
    return url


def _proj_env_options() -> dict[str, str]:
    """GDAL/PROJ options that force PROJ to find its database.

    A recurring failure (especially on macOS) is a stale ``PROJ_LIB`` /
    ``PROJ_DATA`` in the shell -- left by a Homebrew or conda GDAL -- that
    shadows the ``proj.db`` rasterio ships in its own wheel. GDAL then prints
    ``PROJ: Cannot find proj.db`` and the tile reprojection to Web Mercator
    fails, so tiles land wrong or blank. Pointing ``PROJ_DATA`` at rasterio's
    bundled data for our own raster ops makes the viewer self-contained
    regardless of the ambient environment. Returns an empty dict when the
    bundled data can't be located (leave the environment untouched).
    """
    import os.path  # noqa: PLC0415

    rasterio = _require("rasterio")
    bundled = os.path.join(os.path.dirname(rasterio.__file__), "proj_data")
    if os.path.exists(os.path.join(bundled, "proj.db")):
        return {"PROJ_DATA": bundled}
    return {}


def _tile_bounds_3857(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """Web-Mercator bounds ``(minx, miny, maxx, maxy)`` of XYZ tile ``z/x/y``.

    Standard (non-TMS) tiling: ``y`` increases southward from the top-left
    origin at ``(-R, R)``.
    """
    n = 2**z
    span = 2 * _MERC_ORIGIN / n
    minx = -_MERC_ORIGIN + x * span
    maxx = -_MERC_ORIGIN + (x + 1) * span
    maxy = _MERC_ORIGIN - y * span
    miny = _MERC_ORIGIN - (y + 1) * span
    return minx, miny, maxx, maxy


class SceneTiler:
    """Render Web-Mercator tiles of one Umbra SAR scene on demand.

    Constructed with the item to view; opening the scene reads a whole-scene
    overview to fix the global contrast stretch and to learn the footprint.
    :meth:`tile` then renders any ``z/x/y`` tile by warping the matching window
    of the cloud-optimized GeoTIFF into the tile grid and applying that stretch.

    The source dataset is opened lazily *per thread* (rasterio readers aren't
    safe to share across threads), so the threaded tile server can render tiles
    concurrently; GDAL's process-wide ``/vsicurl/`` block cache means the COG
    header isn't refetched for each thread.
    """

    def __init__(
        self,
        item: UmbraItem,
        *,
        asset: str = "GEC",
        db: bool = False,
        colormap: str | None = None,
        percentile: tuple[float, float] = (2.0, 98.0),
        sample_size: int = 1024,
    ) -> None:
        rasterio = _require("rasterio")
        np = _require("numpy")
        from rasterio.enums import Resampling  # noqa: PLC0415
        from rasterio.warp import transform_bounds  # noqa: PLC0415

        url = item.asset_href(asset)
        if not url:
            raise AssetNotFoundError(
                f"Item {item.id!r} has no resolvable URL for asset {asset!r} "
                "(asset href is empty and no umbra:task_id available to derive one)."
            )

        self.item = item
        self.asset = asset
        self.db = db
        self.colormap = colormap
        self._path = _open_path(url)
        self._local = threading.local()
        self._env_opts = _proj_env_options()

        # Read a downsampled whole-scene overview once: it fixes the global
        # contrast stretch shared by every tile, and gives us the lon/lat
        # footprint for the page's initial view. Only a COG overview's worth of
        # bytes is fetched, not the full scene.
        with rasterio.Env(**self._env_opts), rasterio.open(self._path) as src:
            self._nodata = src.nodata
            if src.crs is None:
                raise ValueError(f"Item {item.id!r} asset {asset!r} has no CRS to map.")
            self.bounds_4326 = transform_bounds(src.crs, "EPSG:4326", *src.bounds)
            # Cache the footprint in Web Mercator now, under this Env, so the
            # per-tile intersection test never has to reproject on a worker
            # thread (and can't trip over a mis-set PROJ path there).
            self._b3857 = transform_bounds("EPSG:4326", "EPSG:3857", *self.bounds_4326)
            scale = max(max(src.width, src.height) / sample_size, 1.0)
            out_w = max(int(src.width / scale), 1)
            out_h = max(int(src.height / scale), 1)
            sample = src.read([1], out_shape=(1, out_h, out_w), resampling=Resampling.average)[
                0
            ].astype("float64")

        if self._nodata is not None:
            sample = np.where(sample == self._nodata, np.nan, sample)
        self.cuts = _amplitude_cuts(sample, percentile=percentile, db=db)

    def _dataset(self):
        """The calling thread's open rasterio dataset (opened on first use)."""
        rasterio = _require("rasterio")
        ds = getattr(self._local, "ds", None)
        if ds is None:
            ds = rasterio.open(self._path)
            self._local.ds = ds
        return ds

    def tile(self, z: int, x: int, y: int) -> bytes | None:
        """Render tile ``z/x/y`` as PNG bytes, or ``None`` if it has no data.

        Tiles that don't intersect the scene footprint -- which Leaflet will
        ask for at the edges and at low zoom -- short-circuit to ``None`` so
        the server answers 404 without warping empty ground. A tile that
        intersects but lands entirely on nodata pixels also returns ``None``.
        """
        rasterio = _require("rasterio")
        np = _require("numpy")
        from affine import Affine  # noqa: PLC0415
        from rasterio.enums import Resampling  # noqa: PLC0415
        from rasterio.vrt import WarpedVRT  # noqa: PLC0415

        minx, miny, maxx, maxy = _tile_bounds_3857(z, x, y)
        src = self._dataset()
        if not self._intersects_3857(minx, miny, maxx, maxy):
            return None

        # Warp the source straight into this tile's exact grid. Handing GDAL a
        # coarse target transform makes it read the matching COG overview, so a
        # tile is a few range requests rather than a full-res read. The Env
        # forces PROJ's data path (see _proj_env_options) so the reprojection
        # doesn't fail on a machine with a stale PROJ_LIB/PROJ_DATA.
        dst_transform = Affine(
            (maxx - minx) / TILE_SIZE, 0.0, minx, 0.0, (miny - maxy) / TILE_SIZE, maxy
        )
        with (
            rasterio.Env(**self._env_opts),
            WarpedVRT(
                src,
                crs="EPSG:3857",
                transform=dst_transform,
                width=TILE_SIZE,
                height=TILE_SIZE,
                resampling=Resampling.bilinear,
            ) as vrt,
        ):
            data = vrt.read([1], out_shape=(1, TILE_SIZE, TILE_SIZE))[0].astype("float64")

        if self._nodata is not None:
            data = np.where(data == self._nodata, np.nan, data)
        # Off-scene pixels warp in as 0; the stretch already treats <= 0 and
        # non-finite as transparent. Skip the encode for a fully-empty tile.
        if not np.isfinite(data).any() or (np.nan_to_num(data) <= 0).all():
            return None

        rgba = _stretch_to_rgba(data, db=self.db, colormap=self.colormap, cuts=self.cuts)
        return _encode_png(rgba)

    def _intersects_3857(self, minx: float, miny: float, maxx: float, maxy: float) -> bool:
        """Whether a Web-Mercator box overlaps the scene footprint."""
        w, s, e, n = self._b3857  # precomputed in __init__ under the PROJ Env
        return not (maxx <= w or minx >= e or maxy <= s or miny >= n)


def _encode_png(rgba: Any) -> bytes:
    """Encode an ``(H, W, 4)`` uint8 array as PNG bytes."""
    _require("PIL")
    import io  # noqa: PLC0415

    from PIL import Image  # noqa: PLC0415

    buf = io.BytesIO()
    Image.fromarray(rgba, mode="RGBA").save(buf, format="PNG")
    return buf.getvalue()


_TILE_RE = re.compile(r"^/tiles/(\d+)/(\d+)/(\d+)\.png$")
# A 1x1 transparent PNG, used as Leaflet's errorTileUrl so 404s (off-scene
# tiles) don't flash a broken-image icon.
_BLANK_TILE = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


class _ViewerHandler(BaseHTTPRequestHandler):
    """Serve the viewer page and on-demand SAR tiles for one scene."""

    # The handler always runs under a ``_ViewerServer`` (created below), which
    # carries the ``tiler`` and ``index_html`` the request paths read; declare
    # the narrower type so those attribute reads type-check.
    server: _ViewerServer

    # Quiet by default: a pan/zoom session fires hundreds of tile requests and
    # logging each would bury the one line the user needs (the URL).
    def log_message(self, *args: Any) -> None:  # noqa: D401
        pass

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        # Panning fires many tile requests and the browser routinely cancels
        # the ones that scroll out of view before we finish writing, closing
        # the socket under us. That's normal, not an error -- swallow the
        # resulting connection errors so they don't spew a traceback per pan.
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            if status == 200 and content_type == "image/png":
                self.send_header("Cache-Control", "max-age=3600")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", self.server.index_html.encode())
            return
        match = _TILE_RE.match(path)
        if match:
            z, x, y = (int(g) for g in match.groups())
            try:
                png = self.server.tiler.tile(z, x, y)
            except Exception:  # noqa: BLE001 - one bad tile must not kill the server
                self._send(500, "text/plain", b"tile render error")
                return
            if png is None:
                self._send(404, "text/plain", b"")
            else:
                self._send(200, "image/png", png)
            return
        self._send(404, "text/plain", b"not found")


class _ViewerServer(ThreadingHTTPServer):
    """A threaded HTTP server carrying the scene tiler and page for the handler."""

    daemon_threads = True
    allow_reuse_address = True
    tiler: SceneTiler
    index_html: str


def _viewer_html(tiler: SceneTiler) -> str:
    """The single-page Leaflet viewer wired to this scene's tile endpoint."""
    import json  # noqa: PLC0415

    info = tiler.item.metadata_summary()
    w, s, e, n = tiler.bounds_4326
    bounds_js = json.dumps([[s, w], [n, e]])
    scale = "dB" if tiler.db else "linear"
    rng, azi = info["resolution_range_m"], info["resolution_azimuth_m"]

    def _res(v: Any) -> str:
        return f"{v:.2f} m" if isinstance(v, (int, float)) else "?"

    # ``id``/``datetime``/``platform``/``href`` come from remote STAC JSON, so
    # they are HTML-escaped before landing in the page; ``href`` is additionally
    # scheme-checked so a hostile document can't inject a ``javascript:`` link.
    href = safe_href(tiler.item.href)
    stac_link = f'<a href="{href}" target="_blank" rel="noopener">STAC item</a>' if href else ""
    meta_rows = "".join(
        f"<tr><td>{k}</td><td>{v}</td></tr>"
        for k, v in [
            ("ID", escape(str(info["id"]))),
            ("Acquired", escape(info["datetime"]) if info["datetime"] else "&mdash;"),
            ("Platform", escape(info["platform"]) if info["platform"] else "&mdash;"),
            ("Product", f"{escape(str(tiler.asset))} &middot; {scale}"),
            ("Resolution", f"{_res(rng)} &times; {_res(azi)}"),
        ]
    )
    title = f"Umbra SAR &mdash; {escape(str(info['id']))}"
    return _HTML_TEMPLATE.format(
        title=title,
        bounds=bounds_js,
        meta_rows=meta_rows,
        stac_link=stac_link,
        blank_tile=_BLANK_TILE,
    )


# Pinned Leaflet build, mirroring the lazy-imagery module's CDN-pinning
# rationale: an unpinned URL can regress the page without warning.
_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  html,body{{margin:0;height:100%}}
  #map{{position:absolute;inset:0}}
  .panel{{position:absolute;top:10px;right:10px;z-index:1000;background:rgba(13,17,23,.88);
    color:#e6edf3;font:12px/1.4 -apple-system,Segoe UI,sans-serif;padding:10px 12px;
    border:1px solid #30363d;border-radius:8px;max-width:300px}}
  .panel h1{{margin:0 0 6px;font-size:13px;font-weight:600}}
  .panel table{{border-collapse:collapse}}
  .panel td{{padding:1px 6px 1px 0;vertical-align:top}}
  .panel td:first-child{{color:#8b949e;white-space:nowrap}}
  .panel .tid{{word-break:break-all}}
  .panel a{{color:#58a6ff;text-decoration:none}}
  .panel label{{display:block;margin-top:8px;color:#8b949e}}
  .panel input[type=range]{{width:100%}}
  .panel .foot{{margin-top:8px;color:#6e7681}}
</style>
</head>
<body>
<div id="map"></div>
<div class="panel">
  <h1>Umbra SAR viewer</h1>
  <table class="tid">{meta_rows}</table>
  <label>SAR opacity <input id="op" type="range" min="0" max="1" step="0.05" value="1"></label>
  <div class="foot">{stac_link}<br>
    &copy; Umbra (CC BY 4.0) &middot; basemap &copy; OpenStreetMap</div>
</div>
<script>
  var bounds = L.latLngBounds({bounds});
  var map = L.map('map', {{ maxZoom: 22 }});
  L.tileLayer('https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
    maxNativeZoom: 19, maxZoom: 22,
    attribution: '&copy; OpenStreetMap contributors'
  }}).addTo(map);
  var sar = L.tileLayer('tiles/{{z}}/{{x}}/{{y}}.png', {{
    maxNativeZoom: 22, maxZoom: 22, bounds: bounds,
    errorTileUrl: '{blank_tile}', attribution: 'Umbra SAR'
  }}).addTo(map);
  map.fitBounds(bounds);
  document.getElementById('op').addEventListener('input', function(e) {{
    sar.setOpacity(parseFloat(e.target.value));
  }});
</script>
</body>
</html>
"""


def make_viewer_server(
    item: UmbraItem,
    *,
    asset: str = "GEC",
    host: str = "127.0.0.1",
    port: int = 0,
    db: bool = False,
    colormap: str | None = None,
    percentile: tuple[float, float] = (2.0, 98.0),
    sample_size: int = 1024,
) -> tuple[ThreadingHTTPServer, str]:
    """Build (but don't start) a local tile server for one Umbra scene.

    Opens the scene -- reading a whole-scene overview to fix the global
    contrast stretch and footprint -- and returns ``(server, url)``. Call
    ``server.serve_forever()`` to run it (or use :func:`view`, which does that
    for you and opens a browser). Returning the unstarted server keeps this
    testable and embeddable; ``port=0`` lets the OS pick a free port, read back
    from ``url``.

    See :func:`view` for the parameter meanings. Requires the ``viz`` extra.
    """
    tiler = SceneTiler(
        item,
        asset=asset,
        db=db,
        colormap=colormap,
        percentile=percentile,
        sample_size=sample_size,
    )
    httpd = _ViewerServer((host, port), _ViewerHandler)
    httpd.tiler = tiler
    httpd.index_html = _viewer_html(tiler)
    bound_host, bound_port = httpd.server_address[:2]
    # server_address is typed loosely (host may be bytes); a bound TCP host is a
    # string, so decode defensively for the URL rather than formatting bytes.
    if isinstance(bound_host, bytes):
        bound_host = bound_host.decode()
    url = f"http://{bound_host}:{bound_port}/"
    return httpd, url


def view(
    item: UmbraItem,
    *,
    asset: str = "GEC",
    host: str = "127.0.0.1",
    port: int = 0,
    db: bool = False,
    colormap: str | None = None,
    percentile: tuple[float, float] = (2.0, 98.0),
    open_browser: bool = True,
) -> None:
    """Serve an interactive full-resolution viewer for one Umbra SAR scene.

    Starts a local tile server and (by default) opens it in your browser, then
    blocks until interrupted (Ctrl-C). Pan and zoom to roam the scene at native
    resolution: only the tiles in view are streamed from the remote
    cloud-optimized GeoTIFF via HTTP range requests, warped into the
    Web-Mercator map grid -- no full download.

    Parameters
    ----------
    item:
        The acquisition to view.
    asset:
        Which product to render. ``"GEC"`` (the detected, geocoded GeoTIFF) is
        the sensible default; ``"CSI"`` also works. The complex ``SICD`` /
        ``CPHD`` products aren't amplitude rasters.
    host, port:
        Where to bind the local server. ``port=0`` (default) picks a free port.
    db:
        Use a decibel (log-amplitude) stretch -- the radiometrically-correct
        SAR look that reveals texture the default linear stretch crushes toward
        black.
    colormap:
        Matplotlib colormap name for a pseudo-colored view (e.g. ``"viridis"``,
        ``"magma"``). Default is grayscale.
    percentile:
        ``(low, high)`` percentile cut for the global contrast stretch,
        computed once over a whole-scene overview and shared by every tile.
    open_browser:
        Open the viewer URL in the default browser on start.

    Requires the ``viz`` extra (``pip install "umbra-py[viz]"``).
    """
    httpd, url = make_viewer_server(
        item,
        asset=asset,
        host=host,
        port=port,
        db=db,
        colormap=colormap,
        percentile=percentile,
    )
    if open_browser:
        import webbrowser  # noqa: PLC0415

        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        # serve_forever has already returned, so close the socket directly --
        # shutdown() is for stopping the loop from another thread and would
        # deadlock here.
        httpd.server_close()
