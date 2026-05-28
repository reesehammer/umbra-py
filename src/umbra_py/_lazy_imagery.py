"""Browser-side lazy-fetch SAR overlays.

The map's HTML carries a per-item ``Get SAR image`` button instead of a
pre-baked PNG. On the first click anywhere on the map, the page lazily
fetches ``georaster-layer-for-leaflet`` (and the ``georaster`` /
``geotiff.js`` chain it pulls in) by appending ``<script>`` tags from
the driver, streams the GEC cloud-optimized GeoTIFF directly from the
Umbra public bucket via HTTP range requests, applies the same
percentile stretch that :func:`umbra_py.viz._stretch_to_rgba` performs
in Python, and adds the result as a :class:`L.GeoRasterLayer` on the
running Folium map.

Two reasons we inject the CDN scripts from the driver instead of from
the map's ``<head>``:

1. **Ordering.** ``georaster-layer-for-leaflet`` extends
   ``L.GridLayer`` at script-evaluation time, so it *must* run after
   Leaflet. Folium pulls Leaflet itself into the page head, and we
   don't get a hook in between -- so a naive ``<head>`` injection
   races and the layer ends up broken
   (``Cannot read properties of undefined (reading 'GridLayer')``).
2. **Cost.** A 200-item map weighs ~30 KB and pays *nothing* for the
   CDN until somebody actually clicks a button. Pages nobody clicks
   stay free.

The Umbra bucket already serves permissive CORS headers (``*`` origin,
``GET``/``HEAD`` methods, ``range`` headers) on every object, which is
what makes the browser-direct streaming possible.

**file:// origin limitation.** The georaster bundle uses Webpack
worker chunks, which Chromium-family browsers refuse to spawn from
``file://`` pages ("'file:' URLs are treated as unique security
origins"). The driver detects ``file:`` at click time and surfaces a
clear "open via http(s)" message instead of letting the click silently
hang. For interactive use, serve the directory with
``python3 -m http.server`` and open ``http://localhost:8000/<map>.html``.

The implementation here is intentionally a JS string template rather
than a Jinja template module: it's short, it lands inside a single
``<script>`` block at the bottom of the map, and keeping it inline
keeps the rendering surface visible from Python.
"""

from __future__ import annotations

import html
import json

# Pinned to specific versions to keep release behavior reproducible.
# Bump deliberately -- COG decoding in the browser is a moving target
# and an unpinned CDN URL can regress without warning.
#
# Both URLs target the *exact* file the package's `unpkg` /`browser`
# field in package.json points at -- naive guesses like
# `dist/<pkg>.min.js` 404 on georaster-layer-for-leaflet, where the
# real bundle lives several directories deep.
GEORASTER_JS = "https://unpkg.com/georaster@1.6.0/dist/georaster.browser.bundle.min.js"
GEORASTER_LAYER_JS = (
    "https://unpkg.com/georaster-layer-for-leaflet@3.10.0/"
    "dist/v3/webpack/bundle/georaster-layer-for-leaflet.min.js"
)

# Sample window for the in-browser percentile stretch. Hardcoded because
# nobody tunes it -- 316 x 316 ~= 100k samples, percentile sort is
# sub-second on modest hardware. The COG only ships the bytes for the
# overview level that matches this resolution.
_SAMPLE_DIM = 316


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

    The returned snippet embeds the CDN URLs (pinned at module level)
    as JS string literals via ``json.dumps`` so any future URL with
    quotes or non-ASCII characters stays a valid JS string. The driver
    resolves the running Folium map at click time by walking the
    button's DOM ancestry to the enclosing ``.folium-map`` element and
    looking up ``window[<that-div-id>]`` -- robust against Jupyter cell
    reruns and multi-map pages, where a single bound ``map_var``
    closure would go stale.
    """
    return _DRIVER_TEMPLATE.format(
        plo=float(percentile_low),
        phi=float(percentile_high),
        sample_dim=_SAMPLE_DIM,
        georaster_url=json.dumps(GEORASTER_JS),
        georaster_layer_url=json.dumps(GEORASTER_LAYER_JS),
    )


def popup_button_html(
    *,
    item_id: str,
    asset_url: str,
    label: str = "Get SAR image",
) -> str:
    """Render the per-item button shown inside the polygon's popup.

    The button is the entire UI surface for the lazy-fetch flow: no
    extra controls, no separate panel. State (idle / loading / loaded)
    is reflected by swapping ``data-state`` and the visible text. The
    button is keyed by ``item_id`` so the driver can find the same
    DOM node on a "Remove image" click.
    """
    return (
        '<div class="umbra-sar-fetch" style="margin-top:6px">'
        '<button type="button" '
        'class="umbra-sar-btn" '
        f'data-item-id="{html.escape(item_id, quote=True)}" '
        f'data-asset-url="{html.escape(asset_url, quote=True)}" '
        'data-state="idle" '
        'onclick="umbraToggleSarImage(this)" '
        'style="font:12px/1.2 -apple-system,sans-serif;padding:4px 10px;'
        "border:1px solid #888;border-radius:3px;background:#f7f7f7;"
        f'cursor:pointer">{html.escape(label)}</button>'
        "</div>"
    )


# The template stays small on purpose. The flow:
#  1. First click anywhere on the map kicks off `loadLibs()`, which
#     dynamically inserts the two CDN <script> tags into <head>. They
#     run AFTER Leaflet (already loaded), so georaster-layer-for-leaflet
#     finds L.GridLayer when it tries to extend it.
#  2. Once both scripts have fired their `onload`, parseGeoraster(url)
#     opens the COG (only the headers are fetched at this point).
#  3. fetchSample() pulls a downsampled view of the whole raster via
#     georaster.getValues -- HTTP range requests against the right
#     overview level, no full read. For COGs `georaster.values` is
#     null/undefined, so the naive iterate-`values[0]` path returns
#     no samples and fires "No valid SAR pixels".
#  4. Sample the returned pixel values to compute percentile cuts.
#  5. Build a GeoRasterLayer whose pixelValuesToColorFn does the stretch
#     and emits transparent for invalid / non-positive pixels (matching
#     _stretch_to_rgba in Python).
#  6. Add to the map; cache the layer keyed by item id.
#  7. Second click on the same button removes the layer.
_DRIVER_TEMPLATE = """
(function() {{
  if (window.umbraToggleSarImage) {{ return; }}  // idempotent across re-renders
  var layers = {{}};  // item_id -> L.GeoRasterLayer
  var libsPromise = null;
  var GEORASTER_URL = {georaster_url};
  var GEORASTER_LAYER_URL = {georaster_layer_url};

  // Resolve the Folium map by walking up from the clicked button to
  // the enclosing `.folium-map` div, then looking up its id on `window`
  // (Folium publishes every map by id). Robust against Jupyter cell
  // reruns and multi-map pages -- the IIFE installs `umbraToggleSarImage`
  // once but each click resolves the right map fresh.
  function findMapForButton(button) {{
    var el = button;
    while (el && (!el.classList || !el.classList.contains('folium-map'))) {{
      el = el.parentElement;
    }}
    return (el && el.id) ? window[el.id] : null;
  }}

  function loadLibs() {{
    if (libsPromise) return libsPromise;
    // loadLibs is gated by `libsPromise`, so injection happens at most
    // once per page lifetime -- no need for the inject-helper to dedup
    // existing <script> tags. georaster-layer-for-leaflet extends
    // L.GridLayer at evaluation time; by the time we run here Leaflet
    // is already on the page (Folium loads it in <head> during initial
    // parse). The georaster bundle has no Leaflet dependency, so the
    // two scripts can load in parallel.
    libsPromise = new Promise(function(resolve, reject) {{
      var pending = 2;
      function done() {{ if (--pending === 0) resolve(); }}
      [GEORASTER_URL, GEORASTER_LAYER_URL].forEach(function(src) {{
        var s = document.createElement('script');
        s.src = src;
        s.async = false;
        s.onload = done;
        s.onerror = function() {{ reject(new Error('Failed to load ' + src)); }};
        document.head.appendChild(s);
      }});
    }}).then(function() {{
      if (typeof parseGeoraster === 'undefined' ||
          typeof GeoRasterLayer === 'undefined') {{
        throw new Error(
          'CDN libs loaded but expected globals (parseGeoraster, ' +
          'GeoRasterLayer) are missing. Has a CDN URL drifted?');
      }}
    }});
    return libsPromise;
  }}

  function pickPercentile(sorted, p) {{
    var idx = Math.max(0, Math.min(sorted.length - 1,
      Math.floor((p / 100.0) * (sorted.length - 1))));
    return sorted[idx];
  }}

  function normalizeNoData(raw) {{
    // COGs sometimes declare noDataValue as a string; coerce to a
    // number so the equality check downstream catches it.
    if (raw === undefined || raw === null) return null;
    var n = Number(raw);
    return isFinite(n) ? n : null;
  }}

  function fetchSample(georaster) {{
    // For COGs, georaster.values is null and pixels have to be fetched
    // on demand via getValues, which range-reads the appropriate
    // overview level. For small in-memory rasters, georaster.values is
    // already populated; use it directly to dodge the round trip.
    if (georaster.values && georaster.values[0] && georaster.values[0].length) {{
      return Promise.resolve(georaster.values);
    }}
    if (typeof georaster.getValues !== 'function') {{
      return Promise.reject(new Error(
        'georaster source exposes neither preloaded values nor getValues()'));
    }}
    return georaster.getValues({{
      left: georaster.xmin,
      right: georaster.xmax,
      bottom: georaster.ymin,
      top: georaster.ymax,
      width: {sample_dim},
      height: {sample_dim},
      resampleMethod: 'nearest'
    }});
  }}

  function computeStretchFromValues(values, noDataValue) {{
    if (!values || !values[0]) return null;
    var band = values[0];
    var samples = [];
    for (var i = 0; i < band.length; i++) {{
      var row = band[i];
      if (!row) continue;
      for (var j = 0; j < row.length; j++) {{
        var v = row[j];
        if (isFinite(v) && v > 0 && (noDataValue === null || v !== noDataValue)) {{
          samples.push(v);
        }}
      }}
    }}
    if (samples.length === 0) return null;
    // Sort once in place, pick both percentile cuts off the same
    // sorted array.
    samples.sort(function(a, b) {{ return a - b; }});
    var lo = pickPercentile(samples, {plo});
    var hi = pickPercentile(samples, {phi});
    if (hi <= lo) {{
      // Degenerate sample (e.g. one valid pixel, or all pixels equal).
      // The previous absolute `lo + 1` fallback bricked normalized
      // amplitude rasters whose values were <<1. Use a tiny range
      // centered on the value so the image renders as mid-gray rather
      // than solid black.
      var delta = Math.max(Math.abs(lo), 1) * 1e-3;
      lo = lo - delta;
      hi = lo + 2 * delta;
    }}
    return {{ lo: lo, hi: hi }};
  }}

  function loadCogAsLayer(button) {{
    var url = button.getAttribute('data-asset-url');
    var id = button.getAttribute('data-item-id');
    if (window.location && window.location.protocol === 'file:') {{
      // georaster's worker chunks can't spawn from file:// origins
      // ("unique security origin"). Tell the user how to fix it
      // instead of letting parseGeoraster hang or fail opaquely.
      var file = (window.location.pathname || '').split('/').pop() || '';
      button.disabled = false;
      button.textContent = 'Open via http://';
      button.title =
        "Lazy SAR overlays need an http(s) origin. Open a terminal " +
        "in this folder, run `python3 -m http.server`, then visit " +
        "http://localhost:8000/" + file;
      button.setAttribute('data-state', 'error');
      console.warn(
        '[umbra-py lazy SAR] file:// origin: open via a local web server. '
        + 'Try `python3 -m http.server` and visit '
        + 'http://localhost:8000/' + file);
      return;
    }}
    button.disabled = true;
    button.textContent = 'Loading SAR image…';
    button.setAttribute('data-state', 'loading');
    var grRef = null;
    loadLibs().then(function() {{
      return parseGeoraster(url);
    }}).then(function(georaster) {{
      grRef = georaster;
      return fetchSample(georaster);
    }}).then(function(sampleValues) {{
      var noData = normalizeNoData(grRef.noDataValue);
      var stretch = computeStretchFromValues(sampleValues, noData);
      if (!stretch) {{
        button.disabled = false;
        button.textContent = 'No valid SAR pixels';
        button.setAttribute('data-state', 'error');
        return;
      }}
      var layer = new GeoRasterLayer({{
        georaster: grRef,
        opacity: 1.0,
        pixelValuesToColorFn: function(values) {{
          var v = values[0];
          if (!isFinite(v) || v <= 0 || (noData !== null && v === noData)) {{
            return null;  // transparent
          }}
          var s = Math.max(0, Math.min(255,
            Math.floor((v - stretch.lo) / (stretch.hi - stretch.lo) * 255)));
          return 'rgb(' + s + ',' + s + ',' + s + ')';
        }},
        resolution: 256
      }});
      var map = findMapForButton(button);
      if (!map) {{
        button.disabled = false;
        button.textContent = 'Map not ready';
        button.setAttribute('data-state', 'error');
        return;
      }}
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
