"""Browser-side lazy-fetch SAR overlays.

The map's HTML carries a per-item ``Get SAR image`` button instead of a
pre-baked PNG. On the first click anywhere on the map, the page lazily
fetches `geotiff.js <https://geotiffjs.github.io/>`_ from a pinned CDN,
streams a low-resolution overview of the GEC cloud-optimized GeoTIFF
directly from the Umbra public bucket via HTTP range requests, applies
the same percentile stretch that :func:`umbra_py.viz._stretch_to_rgba`
performs in Python, resamples it north-up onto a ``<canvas>`` using the
raster's own georeferencing, and drops it on the map as an image
overlay -- a Leaflet ``L.imageOverlay`` on a Folium or ``umbra demo`` page, a
MapLibre ``image`` source plus ``raster`` layer on the whole-archive
PMTiles explorer (``driver_script(engine=...)``). Only those two lines
differ between the engines; the fetch, decode, stretch and button state
machine are one implementation.

**Why bare geotiff.js and not georaster-layer-for-leaflet.** The
georaster bundle decodes COGs inside Webpack-generated Web Workers.
Chromium-family browsers refuse to spawn those worker chunks from a
``file://`` page ("'file:' URLs are treated as unique security
origins"), so a double-clicked map produced an opaque failure. geotiff.js
decodes on the main thread when you don't hand it a ``Pool``, so it has
no worker dependency and works whether the page is served over http(s)
**or** opened straight off disk. The COG bytes themselves come from S3
over HTTPS (CORS ``*``), which is allowed from a ``file://`` origin.

**Placement comes from the raster, not the footprint.** Umbra GEC
rasters are geocoded but *not* north-up: the pixel grid is rotated to
the collect geometry, so its four corners are the acquisition's
footprint polygon and the angle differs for every acquisition.
Stretching such a grid onto its lat/lon bounding box does not skew it
slightly -- it spins the whole scene off the map. So the driver reads
the file's own georeferencing (a GeoTIFF ``ModelTransformation`` plus
the CRS geokey), resamples the decoded overview onto a north-up
lat/lon grid, and places *that* at the grid's envelope. The item's
STAC footprint bbox (``data-bounds``) stays as the fallback for a file
whose georeferencing the driver can't read.

Umbra publishes GECs both in WGS84 geographic and in WGS84 UTM zones,
so those are the two CRSs the driver inverts -- the UTM inverse is a
few lines of Snyder series rather than a second CDN dependency. The
resampling is nearest-neighbour onto an affine fit through the grid
corners, which is exact for a geographic raster and stays within a
couple of metres for a UTM one over a scene this size. For a
pixel-accurate overlay use the Python ``imagery=True`` path, which
reprojects through GDAL's ``WarpedVRT``.

A 200-item map weighs ~30 KB and pays *nothing* for the CDN until
somebody clicks a button.

The implementation here is intentionally a JS string template rather
than a Jinja template module: it's short, it lands inside a single
``<script>`` block at the bottom of the map, and keeping it inline
keeps the rendering surface visible from Python.
"""

from __future__ import annotations

import html
import json

# Pinned to a specific version to keep release behavior reproducible.
# Bump deliberately -- COG decoding in the browser is a moving target
# and an unpinned CDN URL can regress without warning. The UMD bundle
# publishes the `GeoTIFF` global; `dist-browser/geotiff.js` is the path
# the package's own `unpkg` field points at.
GEOTIFF_JS = "https://unpkg.com/geotiff@3.0.5/dist-browser/geotiff.js"

# Subresource Integrity digest for the exact bytes at `GEOTIFF_JS`. The
# browser refuses to run the fetched script unless its hash matches, so a
# compromised CDN or a hijacked package release can't inject code into
# every map a user has generated. unpkg serves the
# published npm tarball verbatim, so the digest is reproducible from the
# registry without touching the (egress-restricted) CDN host:
#
#   v=3.0.5
#   curl -sSL "https://registry.npmjs.org/geotiff/-/geotiff-$v.tgz" | \
#     tar xzO package/dist-browser/geotiff.js | \
#     openssl dgst -sha384 -binary | openssl base64 -A | \
#     sed 's/^/sha384-/'
#
# Recompute and update this whenever `GEOTIFF_JS`'s version is bumped --
# a stale digest blocks the load entirely (the driver's onerror path then
# surfaces a clean "Fetch failed" rather than silently running nothing).
GEOTIFF_SRI = "sha384-QchpYxK+DqZYCChtK4SebrECTZEIQ0ahLhme9vwraN6KNxOGwtS66BG72wo1HQDN"

# Largest overview dimension we render at. geotiff.js picks the smallest
# COG overview whose longest side is >= this, so the fetch stays a few
# range requests rather than the full-res image.
_MAX_RENDER_DIM = 1024

# Ceiling on the north-up canvas the rotated overview is resampled onto.
# A rotated grid's lat/lon envelope is bigger than the grid itself (up to
# ~2x per side at 45 degrees), so without a cap a diagonal scene would
# allocate several times the overview's pixels.
_MAX_OUTPUT_DIM = 2 * _MAX_RENDER_DIM


def driver_script(
    *,
    percentile_low: float,
    percentile_high: float,
    engine: str = "leaflet",
) -> str:
    """Return the JS module that wires every button to the COG fetcher.

    Parameters
    ----------
    percentile_low, percentile_high:
        Contrast-stretch cuts, mirroring
        :func:`umbra_py.viz._stretch_to_rgba`'s defaults of ``(2, 98)``.
    engine:
        Which map library places the decoded overlay: ``"leaflet"`` (the
        default, for Folium maps and the embedded-slice ``umbra demo``
        page) or ``"maplibre"`` (for the whole-archive PMTiles explorer).
        Everything above the placement -- the CDN load, the range-read,
        the overview pick, the percentile stretch, the north-up resample
        and the button state machine -- is identical; only the two lines
        that add and remove the overlay differ, so the two pages share
        one driver rather than one each.

    The returned snippet embeds the CDN URL (pinned at module level) as
    a JSON-encoded JS string literal, so a future bump to a URL with
    quotes or non-ASCII characters can't break the template. It also
    carries the pinned Subresource Integrity digest (``GEOTIFF_SRI``) and
    loads the ``<script>`` with ``crossorigin="anonymous"`` so the
    browser verifies the fetched bytes before executing them. The driver
    resolves the running Folium map at click time by walking the
    button's DOM ancestry to the enclosing ``.folium-map`` element --
    robust against Jupyter cell reruns and multi-map pages, where a
    single bound ``map_var`` closure would go stale.
    """
    try:
        overlay_ops = _OVERLAY_OPS[engine]
    except KeyError:
        supported = ", ".join(sorted(_OVERLAY_OPS))
        raise ValueError(f"Unknown map engine {engine!r}. Supported: {supported}.") from None
    return _DRIVER_TEMPLATE.format(
        plo=float(percentile_low),
        phi=float(percentile_high),
        max_dim=_MAX_RENDER_DIM,
        max_out_dim=_MAX_OUTPUT_DIM,
        geotiff_url=json.dumps(GEOTIFF_JS),
        geotiff_sri=json.dumps(GEOTIFF_SRI),
        georef_ops=_GEOREF_OPS,
        overlay_ops=overlay_ops,
    )


def popup_button_html(
    *,
    item_id: str,
    asset_url: str,
    bounds: tuple[float, float, float, float],
    label: str = "Get SAR image",
) -> str:
    """Render the per-item button shown inside the polygon's popup.

    ``bounds`` is the item's lat/lon footprint as
    ``(min_lon, min_lat, max_lon, max_lat)`` -- the driver's fallback
    placement, used only when the COG itself carries no georeferencing
    it can read. State (idle / loading / loaded) is reflected
    by swapping ``data-state`` and the visible text; the button is keyed
    by ``item_id`` so the driver can find the same DOM node on a
    "Remove image" click.
    """
    min_lon, min_lat, max_lon, max_lat = bounds
    # data-bounds is "south,west,north,east" to match Leaflet's
    # [[south, west], [north, east]] LatLngBounds convention.
    bounds_attr = f"{min_lat},{min_lon},{max_lat},{max_lon}"
    return (
        '<div class="umbra-sar-fetch" style="margin-top:6px">'
        '<button type="button" '
        'class="umbra-sar-btn" '
        f'data-item-id="{html.escape(item_id, quote=True)}" '
        f'data-asset-url="{html.escape(asset_url, quote=True)}" '
        f'data-bounds="{bounds_attr}" '
        'data-state="idle" '
        'onclick="umbraToggleSarImage(this)" '
        'style="font:12px/1.2 -apple-system,sans-serif;padding:4px 10px;'
        "border:1px solid #888;border-radius:3px;background:#f7f7f7;"
        f'cursor:pointer">{html.escape(label)}</button>'
        "</div>"
    )


# The two map-library-specific lines, substituted into the driver as
# `{overlay_ops}`. Both define the same pair of functions, so the driver body
# above them is engine-agnostic: `addOverlay` returns an opaque handle that
# `removeOverlay` later takes back.
#
# These are substituted *values*, not template text, so their braces are
# single (unlike `_DRIVER_TEMPLATE`'s, which are doubled for `str.format`).
_LEAFLET_OVERLAY_OPS = """
  function addOverlay(map, id, dataUrl, bounds) {
    var layer = L.imageOverlay(dataUrl, bounds, { opacity: 1.0 });
    layer.addTo(map);
    return layer;
  }

  function removeOverlay(map, id, handle) {
    map.removeLayer(handle);
  }
"""

# MapLibre GL has no `imageOverlay`; the equivalent is an `image` source
# (a data URL plus its four corner coordinates) drawn by a `raster` layer.
# Source/layer ids are derived from the acquisition id, which comes from
# remote metadata -- so it is sanitized to `[A-Za-z0-9_-]` before being used
# as a style id (MapLibre keys its style objects by these strings).
_MAPLIBRE_OVERLAY_OPS = """
  function overlayIds(id) {
    var safe = String(id).replace(/[^A-Za-z0-9_-]/g, '_');
    return { source: 'umbra-sar-src-' + safe, layer: 'umbra-sar-lyr-' + safe };
  }

  function addOverlay(map, id, dataUrl, bounds) {
    // bounds arrive as Leaflet's [[south, west], [north, east]]; an image
    // source wants its corners as [lon, lat], clockwise from the top left.
    var south = bounds[0][0], west = bounds[0][1];
    var north = bounds[1][0], east = bounds[1][1];
    var ids = overlayIds(id);
    removeOverlay(map, id, ids);  // idempotent: a stale style entry would throw
    map.addSource(ids.source, {
      type: 'image',
      url: dataUrl,
      coordinates: [[west, north], [east, north], [east, south], [west, south]]
    });
    // MapLibre stacks layers in insertion order, so without a `beforeId` the
    // image would bury the markers and footprints that opened it. A page that
    // publishes `window.umbraOverlayBeforeId` gets the overlay slotted under
    // that layer (Leaflet's pane order gives this for free); one that does not
    // just gets it on top, as before.
    var beforeId = window.umbraOverlayBeforeId;
    map.addLayer({
      id: ids.layer,
      type: 'raster',
      source: ids.source,
      paint: { 'raster-opacity': 1.0, 'raster-fade-duration': 0 }
    }, (beforeId && map.getLayer(beforeId)) ? beforeId : undefined);
    return ids;
  }

  function removeOverlay(map, id, handle) {
    var ids = handle || overlayIds(id);
    if (map.getLayer(ids.layer)) { map.removeLayer(ids.layer); }
    if (map.getSource(ids.source)) { map.removeSource(ids.source); }
  }
"""

#: Map engine -> the overlay add/remove pair the driver is built with.
_OVERLAY_OPS: dict[str, str] = {
    "leaflet": _LEAFLET_OVERLAY_OPS,
    "maplibre": _MAPLIBRE_OVERLAY_OPS,
}


# The georeferencing half of the driver, substituted as `{georef_ops}`. It is
# kept as its own chunk -- like the overlay ops above -- because it is the part
# of the driver that is pure arithmetic and therefore the part that can be
# exercised outside a browser (`tests/test_lazy_imagery.py` runs it under node).
#
# These are substituted *values*, not template text, so their braces are single
# (unlike `_DRIVER_TEMPLATE`'s, which are doubled for `str.format`).
_GEOREF_OPS = """
  var WGS84_A = 6378137.0;
  var WGS84_F = 1.0 / 298.257223563;
  var UTM_K0 = 0.9996;

  // Inverse UTM -> WGS84 lon/lat as the Snyder series (USGS PP 1395 section 8),
  // good to well under a millimetre inside a zone. Written out rather than
  // pulled from proj4js: UTM and plain WGS84 are the only two CRSs Umbra's GEC
  // products use, and the map's whole CDN budget is spent on geotiff.js.
  function utmToLonLat(easting, northing, zone, south) {
    var e2 = WGS84_F * (2.0 - WGS84_F);
    var ep2 = e2 / (1.0 - e2);
    var x = easting - 500000.0;
    var y = south ? northing - 10000000.0 : northing;
    var mu = (y / UTM_K0)
      / (WGS84_A * (1 - e2 / 4 - 3 * e2 * e2 / 64 - 5 * e2 * e2 * e2 / 256));
    var e1 = (1 - Math.sqrt(1 - e2)) / (1 + Math.sqrt(1 - e2));
    var e1_2 = e1 * e1, e1_3 = e1_2 * e1, e1_4 = e1_3 * e1;
    var phi = mu
      + (3 * e1 / 2 - 27 * e1_3 / 32) * Math.sin(2 * mu)
      + (21 * e1_2 / 16 - 55 * e1_4 / 32) * Math.sin(4 * mu)
      + (151 * e1_3 / 96) * Math.sin(6 * mu)
      + (1097 * e1_4 / 512) * Math.sin(8 * mu);
    var sinPhi = Math.sin(phi), cosPhi = Math.cos(phi), tanPhi = Math.tan(phi);
    var c1 = ep2 * cosPhi * cosPhi;
    var t1 = tanPhi * tanPhi;
    var n1 = WGS84_A / Math.sqrt(1 - e2 * sinPhi * sinPhi);
    var r1 = WGS84_A * (1 - e2) / Math.pow(1 - e2 * sinPhi * sinPhi, 1.5);
    var d = x / (n1 * UTM_K0);
    var d2 = d * d, d3 = d2 * d, d4 = d3 * d, d5 = d4 * d, d6 = d5 * d;
    var lat = phi - (n1 * tanPhi / r1) * (d2 / 2
      - (5 + 3 * t1 + 10 * c1 - 4 * c1 * c1 - 9 * ep2) * d4 / 24
      + (61 + 90 * t1 + 298 * c1 + 45 * t1 * t1 - 252 * ep2 - 3 * c1 * c1) * d6 / 720);
    var lon = (d - (1 + 2 * t1 + c1) * d3 / 6
      + (5 - 2 * c1 + 28 * t1 - 3 * c1 * c1 + 8 * ep2 + 24 * t1 * t1) * d5 / 120) / cosPhi;
    return [(zone * 6 - 183) + lon * 180 / Math.PI, lat * 180 / Math.PI];
  }

  // Pixel (column, row) -> the file's model coordinates, as the six affine
  // terms. A rotated raster carries them as a ModelTransformation matrix; a
  // north-up one as a tiepoint plus a pixel scale. Resolves to null when it
  // has neither.
  //
  // Promise-returning because geotiff.js resolves tag values *lazily*: the
  // object `getFileDirectory()` hands back does not carry the tags as plain
  // properties, and reading one (`fd.ModelTransformation`) yields undefined
  // whether the tag is absent or merely unread. `hasTag`/`loadValue` are the
  // accessors that actually answer, and `loadValue` may need another range
  // request -- so the affine can only be had asynchronously.
  function geoTransformOf(image) {
    var fd = image.getFileDirectory();
    if (!fd || typeof fd.hasTag !== 'function') { return Promise.resolve(null); }
    if (fd.hasTag('ModelTransformation')) {
      return fd.loadValue('ModelTransformation').then(function (m) {
        if (!m || m.length < 16) { return null; }
        return { a: m[0], b: m[1], c: m[3], d: m[4], e: m[5], f: m[7] };
      });
    }
    if (fd.hasTag('ModelTiepoint') && fd.hasTag('ModelPixelScale')) {
      return Promise.all([
        fd.loadValue('ModelTiepoint'), fd.loadValue('ModelPixelScale')
      ]).then(function (v) {
        var tie = v[0], scale = v[1];
        if (!tie || tie.length < 6 || !scale || scale.length < 2) { return null; }
        return {
          a: scale[0], b: 0, c: tie[3] - tie[0] * scale[0],
          d: 0, e: -scale[1], f: tie[4] + tie[1] * scale[1]
        };
      });
    }
    return Promise.resolve(null);
  }

  // Model coordinates -> [lon, lat] for the CRS the file declares. Null for
  // anything else, which sends the caller back to the STAC footprint bbox.
  function modelToLonLat(image) {
    var keys = image.getGeoKeys() || {};
    var code = keys.ProjectedCSTypeGeoKey || keys.GeographicTypeGeoKey;
    if (code === 4326) { return function (x, y) { return [x, y]; }; }
    if (code >= 32601 && code <= 32660) {
      return function (x, y) { return utmToLonLat(x, y, code - 32600, false); };
    }
    if (code >= 32701 && code <= 32760) {
      return function (x, y) { return utmToLonLat(x, y, code - 32700, true); };
    }
    return null;
  }

  // Everything needed to paint `image` (an overview of the full-res `base`)
  // north-up: the lat/lon box to place the canvas at, the canvas size, and the
  // source pixel behind each output pixel. Resolves to null when `base` carries
  // no georeferencing this driver can read, which sends the caller back to the
  // item's STAC footprint bbox.
  //
  // The pixel -> lon/lat map is taken as affine, least-squares fitted through
  // the four grid corners. For a WGS84-geographic GEC that is exact (its model
  // coordinates *are* lon/lat); for a UTM one it absorbs the projection's
  // curvature to a couple of metres over a scene a few km across, well inside
  // one pixel at the resolution an overview renders at.
  function rasterGeoreference(base, image, maxOut) {
    var toLonLat = modelToLonLat(base);
    if (!toLonLat) { return Promise.resolve(null); }
    return geoTransformOf(base).then(function (t) {
      return t ? placeGrid(t, toLonLat, base, image, maxOut) : null;
    });
  }

  function placeGrid(t, toLonLat, base, image, maxOut) {
    var w = image.getWidth(), h = image.getHeight();
    // Overview IFDs carry no geo tags of their own, so the full-res affine is
    // rescaled by however much this overview shrank the grid.
    var sx = base.getWidth() / w, sy = base.getHeight() / h;
    function corner(u, v) {
      return toLonLat(t.c + t.a * u * sx + t.b * v * sy,
                      t.f + t.d * u * sx + t.e * v * sy);
    }
    var c00 = corner(0, 0), cW0 = corner(w, 0), c0H = corner(0, h), cWH = corner(w, h);
    // Per-column and per-row steps in lon/lat, averaged over both edges (which
    // is the least-squares fit for a four-corner quad).
    var du = [((cW0[0] - c00[0]) + (cWH[0] - c0H[0])) / (2 * w),
              ((cW0[1] - c00[1]) + (cWH[1] - c0H[1])) / (2 * w)];
    var dv = [((c0H[0] - c00[0]) + (cWH[0] - cW0[0])) / (2 * h),
              ((c0H[1] - c00[1]) + (cWH[1] - cW0[1])) / (2 * h)];
    var mid = [(c00[0] + cW0[0] + c0H[0] + cWH[0]) / 4,
               (c00[1] + cW0[1] + c0H[1] + cWH[1]) / 4];
    var org = [mid[0] - du[0] * w / 2 - dv[0] * h / 2,
               mid[1] - du[1] * w / 2 - dv[1] * h / 2];
    var det = du[0] * dv[1] - dv[0] * du[1];
    if (!isFinite(det) || det === 0) { return null; }

    var west = Math.min(c00[0], cW0[0], c0H[0], cWH[0]);
    var east = Math.max(c00[0], cW0[0], c0H[0], cWH[0]);
    var south = Math.min(c00[1], cW0[1], c0H[1], cWH[1]);
    var north = Math.max(c00[1], cW0[1], c0H[1], cWH[1]);
    // Sample the north-up grid at the source's own scale: the *coarser* of the
    // two per-axis steps, so a rotated grid is not blown up along its diagonal
    // (a north-up one comes out pixel for pixel, since one step is then zero).
    var outW = Math.min(maxOut, Math.max(1, Math.round(
      (east - west) / Math.max(Math.abs(du[0]), Math.abs(dv[0])))));
    var outH = Math.min(maxOut, Math.max(1, Math.round(
      (north - south) / Math.max(Math.abs(du[1]), Math.abs(dv[1])))));
    var lonPer = (east - west) / outW, latPer = (north - south) / outH;

    return {
      bounds: [[south, west], [north, east]],
      width: outW,
      height: outH,
      sourceIndex: function (ox, oy) {
        var dLon = west + (ox + 0.5) * lonPer - org[0];
        var dLat = north - (oy + 0.5) * latPer - org[1];
        var u = (dLon * dv[1] - dLat * dv[0]) / det;
        var v = (dLat * du[0] - dLon * du[1]) / det;
        if (u < 0 || v < 0) { return -1; }
        var col = u | 0, row = v | 0;
        if (col >= w || row >= h) { return -1; }
        return row * w + col;
      }
    };
  }
"""


# The flow:
#  1. First click loads geotiff.js once (dynamic <script>, no workers).
#  2. GeoTIFF.fromUrl(url) opens the COG (headers only at first).
#  3. pickOverview() chooses the smallest overview >= max_dim so the
#     read is a handful of range requests, not the full-res image.
#  4. readRasters() decodes that overview on the main thread.
#  5. Percentile stretch over the first band (invalid / non-positive /
#     nodata pixels -> transparent), matching _stretch_to_rgba.
#  6. Resample onto a north-up lat/lon canvas using the raster's own
#     georeferencing (see `{georef_ops}`) -- a GEC's grid is rotated to
#     the collect geometry -- then toDataURL and drop it on the map at
#     that canvas's envelope via the engine's addOverlay (see
#     `{overlay_ops}`). A file with unreadable georeferencing falls back
#     to painting the source grid onto the item's STAC footprint bbox.
#  7. Cache the overlay handle keyed by item id; second click removes it.
_DRIVER_TEMPLATE = """
(function() {{
  if (window.umbraToggleSarImage) {{ return; }}  // idempotent across re-renders
  var layers = {{}};  // item_id -> the engine's overlay handle
  var libPromise = null;
  var GEOTIFF_URL = {geotiff_url};
  var GEOTIFF_SRI = {geotiff_sri};
  var MAX_DIM = {max_dim};
  var MAX_OUT_DIM = {max_out_dim};

  // Resolve the Folium map by walking up from the clicked button to the
  // enclosing `.folium-map` div, then looking up its id on `window`
  // (Folium publishes every map by id). Robust against Jupyter cell
  // reruns and multi-map pages -- the IIFE installs `umbraToggleSarImage`
  // once but each click resolves the right map fresh.
  function findMapForButton(button) {{
    var el = button;
    while (el && (!el.classList || !el.classList.contains('folium-map'))) {{
      el = el.parentElement;
    }}
    if (el && el.id && window[el.id]) {{ return window[el.id]; }}
    // Fallback for non-Folium host pages (e.g. the `umbra demo` explorer):
    // a plain Leaflet page publishes its single map as `window.umbraLazyMap`,
    // so the same COG-fetch driver drives it unchanged. Folium pages never set
    // it, so their DOM-walk resolution above is untouched.
    return window.umbraLazyMap || null;
  }}
{georef_ops}{overlay_ops}
  function loadLib() {{
    if (libPromise) return libPromise;
    libPromise = new Promise(function(resolve, reject) {{
      var s = document.createElement('script');
      s.src = GEOTIFF_URL;
      // Subresource Integrity: the browser hashes the fetched bytes and
      // refuses to execute them unless they match GEOTIFF_SRI, so a
      // compromised CDN can't run arbitrary script in the map. SRI
      // requires a CORS fetch, hence crossorigin='anonymous' (unpkg/S3
      // serve Access-Control-Allow-Origin: *, so this works from file://
      // too). A digest mismatch fires onerror below -> clean 'Fetch failed'.
      if (GEOTIFF_SRI) {{ s.integrity = GEOTIFF_SRI; s.crossOrigin = 'anonymous'; }}
      s.async = false;
      s.onload = resolve;
      s.onerror = function() {{ reject(new Error('Failed to load ' + GEOTIFF_URL)); }};
      document.head.appendChild(s);
    }}).then(function() {{
      if (typeof GeoTIFF === 'undefined' || typeof GeoTIFF.fromUrl !== 'function') {{
        throw new Error('geotiff.js loaded but GeoTIFF.fromUrl is missing. '
          + 'Has the CDN URL drifted?');
      }}
    }});
    return libPromise;
  }}

  function pickPercentile(sorted, p) {{
    var idx = Math.max(0, Math.min(sorted.length - 1,
      Math.floor((p / 100.0) * (sorted.length - 1))));
    return sorted[idx];
  }}

  function normalizeNoData(raw) {{
    // GDAL_NODATA is stored as a string; coerce so the equality check
    // downstream catches it. Returns null when absent / unparseable.
    if (raw === undefined || raw === null) return null;
    var n = Number(raw);
    return isFinite(n) ? n : null;
  }}

  // Smallest overview whose longest side is >= MAX_DIM, else the
  // largest image available (handles COGs whose overviews are all
  // smaller than MAX_DIM, and is agnostic to IFD ordering). The full-res
  // image comes back alongside it: overview IFDs carry no geo tags, so it
  // is the one that says where the raster sits on the ground.
  function pickOverview(tiff) {{
    return tiff.getImageCount().then(function(count) {{
      var chain = Promise.resolve();
      var base = null;
      var chosen = null, chosenMax = Infinity;
      var fallback = null, fallbackMax = -1;
      for (var i = 0; i < count; i++) {{
        (function(idx) {{
          chain = chain.then(function() {{
            return tiff.getImage(idx);
          }}).then(function(img) {{
            if (idx === 0) {{ base = img; }}
            var m = Math.max(img.getWidth(), img.getHeight());
            if (m >= MAX_DIM && m < chosenMax) {{ chosen = img; chosenMax = m; }}
            if (m > fallbackMax) {{ fallback = img; fallbackMax = m; }}
          }});
        }})(i);
      }}
      return chain.then(function() {{
        return {{ base: base, image: chosen || fallback }};
      }});
    }});
  }}

  function computeStretch(data, noData) {{
    var samples = [];
    for (var i = 0; i < data.length; i++) {{
      var v = data[i];
      if (isFinite(v) && v > 0 && (noData === null || v !== noData)) {{
        samples.push(v);
      }}
    }}
    if (samples.length === 0) return null;
    samples.sort(function(a, b) {{ return a - b; }});
    var lo = pickPercentile(samples, {plo});
    var hi = pickPercentile(samples, {phi});
    if (hi <= lo) {{
      // Degenerate sample (one valid pixel, or all pixels equal). A
      // flat `lo + 1` fallback blacks out normalized-amplitude rasters
      // whose values are <<1; use a relative epsilon so uniform scenes
      // render mid-gray instead.
      var delta = Math.max(Math.abs(lo), 1) * 1e-3;
      lo = lo - delta;
      hi = lo + 2 * delta;
    }}
    return {{ lo: lo, hi: hi }};
  }}

  // With `geo`, the canvas is the north-up lat/lon grid rasterGeoreference()
  // describes and each output pixel is pulled back through the raster's own
  // affine; without it, the source grid is painted as-is and the caller places
  // it on the item's STAC bbox, as this driver did before it read geo tags.
  function rasterToDataURL(data, width, height, stretch, noData, geo) {{
    var outW = geo ? geo.width : width;
    var outH = geo ? geo.height : height;
    var canvas = document.createElement('canvas');
    canvas.width = outW;
    canvas.height = outH;
    var ctx = canvas.getContext('2d');
    var img = ctx.createImageData(outW, outH);
    var span = (stretch.hi - stretch.lo) || 1;
    for (var oy = 0; oy < outH; oy++) {{
      for (var ox = 0; ox < outW; ox++) {{
        var o = (oy * outW + ox) * 4;
        var i = geo ? geo.sourceIndex(ox, oy) : (oy * width + ox);
        var v = i < 0 ? NaN : data[i];
        if (!isFinite(v) || v <= 0 || (noData !== null && v === noData)) {{
          img.data[o + 3] = 0;  // transparent
          continue;
        }}
        var s = Math.max(0, Math.min(255,
          Math.floor((v - stretch.lo) / span * 255)));
        img.data[o] = s;
        img.data[o + 1] = s;
        img.data[o + 2] = s;
        img.data[o + 3] = 255;
      }}
    }}
    ctx.putImageData(img, 0, 0);
    return canvas.toDataURL('image/png');
  }}

  function parseBounds(button) {{
    // "south,west,north,east" -> [[south, west], [north, east]]
    var parts = (button.getAttribute('data-bounds') || '').split(',').map(Number);
    if (parts.length !== 4 || parts.some(function(n) {{ return !isFinite(n); }})) {{
      return null;
    }}
    return [[parts[0], parts[1]], [parts[2], parts[3]]];
  }}

  function loadCogAsLayer(button) {{
    var url = button.getAttribute('data-asset-url');
    var id = button.getAttribute('data-item-id');
    var bounds = parseBounds(button);
    var map = findMapForButton(button);
    if (!map || !bounds) {{
      button.textContent = 'Map not ready';
      button.setAttribute('data-state', 'error');
      return;
    }}
    button.disabled = true;
    button.textContent = 'Loading SAR image…';
    button.setAttribute('data-state', 'loading');
    var noData = null;
    // Where the raster says it sits, or null for a file whose georeferencing
    // we can't read -- then `bounds` (the STAC footprint bbox) still applies.
    var geo = null;
    var overview = null;
    loadLib().then(function() {{
      return GeoTIFF.fromUrl(url);
    }}).then(function(tiff) {{
      return pickOverview(tiff);
    }}).then(function(picked) {{
      overview = picked.image;
      noData = normalizeNoData(overview.getGDALNoData());
      // Reading the affine can cost another range request, so this step is
      // awaited rather than assigned -- see geoTransformOf.
      return rasterGeoreference(picked.base, overview, MAX_OUT_DIM);
    }}).then(function(placement) {{
      geo = placement;
      return overview.readRasters();
    }}).then(function(rasters) {{
      var data = rasters[0];
      var stretch = computeStretch(data, noData);
      if (!stretch) {{
        button.disabled = false;
        button.textContent = 'No valid SAR pixels';
        button.setAttribute('data-state', 'error');
        return;
      }}
      var dataUrl = rasterToDataURL(data, rasters.width, rasters.height, stretch, noData, geo);
      layers[id] = addOverlay(map, id, dataUrl, geo ? geo.bounds : bounds);
      button.disabled = false;
      button.textContent = 'Remove SAR image';
      button.setAttribute('data-state', 'loaded');
    }}).catch(function(err) {{
      button.disabled = false;
      button.textContent = 'Fetch failed';
      button.setAttribute('data-state', 'error');
      button.title = String(err);
      console.error('[umbra-py lazy SAR]', err);
    }});
  }}

  function removeLayer(button) {{
    var id = button.getAttribute('data-item-id');
    var handle = layers[id];
    if (handle) {{
      var map = findMapForButton(button);
      if (map) {{ removeOverlay(map, id, handle); }}
      delete layers[id];
    }}
    button.textContent = 'Get SAR image';
    button.setAttribute('data-state', 'idle');
  }}

  window.umbraToggleSarImage = function(button) {{
    var state = button.getAttribute('data-state');
    if (state === 'loaded') {{ removeLayer(button); }}
    else if (state !== 'loading') {{ loadCogAsLayer(button); }}
  }};
}})();
"""
