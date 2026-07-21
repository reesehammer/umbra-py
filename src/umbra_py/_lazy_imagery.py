"""Browser-side lazy-fetch SAR overlays.

The map's HTML carries a per-item ``Get SAR image`` button instead of a
pre-baked PNG. On the first click anywhere on the map, the page lazily
fetches `geotiff.js <https://geotiffjs.github.io/>`_ from a pinned CDN,
streams a low-resolution overview of the GEC cloud-optimized GeoTIFF
directly from the Umbra public bucket via HTTP range requests, applies
the same percentile stretch that :func:`umbra_py.viz._stretch_to_rgba`
performs in Python, paints the result to a ``<canvas>``, and drops it
on the map as a plain Leaflet ``L.imageOverlay``.

**Why bare geotiff.js and not georaster-layer-for-leaflet.** The
georaster bundle decodes COGs inside Webpack-generated Web Workers.
Chromium-family browsers refuse to spawn those worker chunks from a
``file://`` page ("'file:' URLs are treated as unique security
origins"), so a double-clicked map produced an opaque failure. geotiff.js
decodes on the main thread when you don't hand it a ``Pool``, so it has
no worker dependency and works whether the page is served over http(s)
**or** opened straight off disk. The COG bytes themselves come from S3
over HTTPS (CORS ``*``), which is allowed from a ``file://`` origin.

**Placement is a quick-look approximation.** Umbra GEC rasters are
geocoded but in a projected CRS (UTM). Rather than reproject in the
browser, the overlay is stretched to fill the item's STAC footprint
bounding box (the same lat/lon extent used to draw the polygon). Over a
few-km Umbra scene the skew from stretching a north-up UTM grid onto its
lat/lon bbox is small -- fine for "where/what does this look like"
exploration. For pixel-accurate overlays use the Python ``imagery=True``
path, which reprojects through GDAL's ``WarpedVRT``.

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
# every map a user has generated (CODEBASE_ANALYSIS 3.4). unpkg serves the
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


def driver_script(
    *,
    percentile_low: float,
    percentile_high: float,
) -> str:
    """Return the JS module that wires every button to the COG fetcher.

    Parameters
    ----------
    percentile_low, percentile_high:
        Contrast-stretch cuts, mirroring
        :func:`umbra_py.viz._stretch_to_rgba`'s defaults of ``(2, 98)``.

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
    return _DRIVER_TEMPLATE.format(
        plo=float(percentile_low),
        phi=float(percentile_high),
        max_dim=_MAX_RENDER_DIM,
        geotiff_url=json.dumps(GEOTIFF_JS),
        geotiff_sri=json.dumps(GEOTIFF_SRI),
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
    ``(min_lon, min_lat, max_lon, max_lat)`` -- the driver places the
    decoded overlay there. State (idle / loading / loaded) is reflected
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


# The flow:
#  1. First click loads geotiff.js once (dynamic <script>, no workers).
#  2. GeoTIFF.fromUrl(url) opens the COG (headers only at first).
#  3. pickOverview() chooses the smallest overview >= max_dim so the
#     read is a handful of range requests, not the full-res image.
#  4. readRasters() decodes that overview on the main thread.
#  5. Percentile stretch over the first band (invalid / non-positive /
#     nodata pixels -> transparent), matching _stretch_to_rgba.
#  6. Paint to a canvas, toDataURL, drop on the map as L.imageOverlay
#     placed at the item's STAC footprint bbox.
#  7. Cache the layer keyed by item id; second click removes it.
_DRIVER_TEMPLATE = """
(function() {{
  if (window.umbraToggleSarImage) {{ return; }}  // idempotent across re-renders
  var layers = {{}};  // item_id -> L.imageOverlay
  var libPromise = null;
  var GEOTIFF_URL = {geotiff_url};
  var GEOTIFF_SRI = {geotiff_sri};
  var MAX_DIM = {max_dim};

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
  // smaller than MAX_DIM, and is agnostic to IFD ordering).
  function pickOverview(tiff) {{
    return tiff.getImageCount().then(function(count) {{
      var chain = Promise.resolve();
      var chosen = null, chosenMax = Infinity;
      var fallback = null, fallbackMax = -1;
      for (var i = 0; i < count; i++) {{
        (function(idx) {{
          chain = chain.then(function() {{
            return tiff.getImage(idx);
          }}).then(function(img) {{
            var m = Math.max(img.getWidth(), img.getHeight());
            if (m >= MAX_DIM && m < chosenMax) {{ chosen = img; chosenMax = m; }}
            if (m > fallbackMax) {{ fallback = img; fallbackMax = m; }}
          }});
        }})(i);
      }}
      return chain.then(function() {{ return chosen || fallback; }});
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

  function rasterToDataURL(data, width, height, stretch, noData) {{
    var canvas = document.createElement('canvas');
    canvas.width = width;
    canvas.height = height;
    var ctx = canvas.getContext('2d');
    var img = ctx.createImageData(width, height);
    var span = (stretch.hi - stretch.lo) || 1;
    for (var i = 0; i < data.length; i++) {{
      var v = data[i];
      var o = i * 4;
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
    loadLib().then(function() {{
      return GeoTIFF.fromUrl(url);
    }}).then(function(tiff) {{
      return pickOverview(tiff);
    }}).then(function(image) {{
      noData = normalizeNoData(image.getGDALNoData());
      return image.readRasters();
    }}).then(function(rasters) {{
      var data = rasters[0];
      var stretch = computeStretch(data, noData);
      if (!stretch) {{
        button.disabled = false;
        button.textContent = 'No valid SAR pixels';
        button.setAttribute('data-state', 'error');
        return;
      }}
      var dataUrl = rasterToDataURL(data, rasters.width, rasters.height, stretch, noData);
      var layer = L.imageOverlay(dataUrl, bounds, {{ opacity: 1.0 }});
      layer.addTo(map);
      layers[id] = layer;
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
    var layer = layers[id];
    if (layer) {{
      var map = findMapForButton(button);
      if (map) {{ map.removeLayer(layer); }}
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
