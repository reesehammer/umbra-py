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

The implementation here is intentionally a couple of JS string
templates rather than a Jinja template module: the templates are short,
they only ever land inside a single ``<script>`` block at the bottom
of the map, and keeping them inline keeps the rendering surface
visible from Python.
"""

from __future__ import annotations

import html
from typing import Any

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


def driver_script(
    *,
    map_var: str,
    percentile_low: float,
    percentile_high: float,
    sample_cap: int,
) -> str:
    """Return the JS module that wires every button to the COG fetcher.

    Parameters
    ----------
    map_var:
        Name of the global JS variable that holds the Folium ``L.Map``
        instance. Folium picks a per-render random name like
        ``map_a1b2c3``; the caller is responsible for resolving it
        (e.g. via ``m.get_name()``).
    percentile_low, percentile_high:
        Contrast-stretch cuts, mirroring
        :func:`umbra_py.viz._stretch_to_rgba`'s defaults of ``(2, 98)``.
    sample_cap:
        Maximum number of pixel samples to use when computing the
        percentile cut in the browser. SAR overviews are typically a
        few million pixels; sampling keeps the math sub-second on
        modest hardware.

    The returned snippet embeds the CDN URLs (pinned at module level)
    so a single ``<script>`` block injection from the Python side is
    enough -- no extra ``<head>`` plumbing.
    """
    return _DRIVER_TEMPLATE.format(
        map_var=map_var,
        plo=percentile_low,
        phi=percentile_high,
        sample_cap=sample_cap,
        georaster_url=GEORASTER_JS,
        georaster_layer_url=GEORASTER_LAYER_JS,
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
        f'<div class="umbra-sar-fetch" style="margin-top:6px">'
        f'<button type="button" '
        f'class="umbra-sar-btn" '
        f'data-item-id="{html.escape(item_id, quote=True)}" '
        f'data-asset-url="{html.escape(asset_url, quote=True)}" '
        f'data-state="idle" '
        f'onclick="umbraToggleSarImage(this)" '
        f'style="font:12px/1.2 -apple-system,sans-serif;padding:4px 10px;'
        f"border:1px solid #888;border-radius:3px;background:#f7f7f7;"
        f'cursor:pointer">{html.escape(label)}</button>'
        f"</div>"
    )


def _verbatim_url_set(*urls: str) -> dict[str, Any]:
    # Kept as a function for tests to call when they want the set of URLs
    # the driver injects, without parsing JS out of the template.
    return {url: True for url in urls}


# The template stays small on purpose. The flow:
#  1. First click anywhere on the map kicks off `loadLibs()`, which
#     dynamically inserts the two CDN <script> tags into <head>. They
#     run AFTER Leaflet (already loaded), so georaster-layer-for-leaflet
#     finds L.GridLayer when it tries to extend it.
#  2. Once both scripts have fired their `onload`, parseGeoraster(url)
#     streams the COG via HTTP range requests.
#  3. Sample the pixel values to compute percentile cuts.
#  4. Build a GeoRasterLayer whose pixelValuesToColorFn does the stretch
#     and emits transparent for invalid / non-positive pixels (matching
#     _stretch_to_rgba in Python).
#  5. Add to the map; cache the layer keyed by item id.
#  6. Second click on the same button removes the layer.
_DRIVER_TEMPLATE = """
(function() {{
  if (window.umbraToggleSarImage) {{ return; }}  // idempotent across re-renders
  var layers = {{}};  // item_id -> L.GeoRasterLayer
  var libsPromise = null;
  var GEORASTER_URL = {georaster_url!r};
  var GEORASTER_LAYER_URL = {georaster_layer_url!r};

  function findMap() {{ return window['{map_var}']; }}

  function injectScript(src) {{
    return new Promise(function(resolve, reject) {{
      var existing = document.querySelector('script[src="' + src + '"]');
      if (existing) {{
        if (existing.dataset.umbraLoaded === '1') {{ resolve(); return; }}
        existing.addEventListener('load', function() {{ resolve(); }});
        existing.addEventListener('error', function() {{
          reject(new Error('Failed to load ' + src));
        }});
        return;
      }}
      var s = document.createElement('script');
      s.src = src;
      s.async = false;  // preserve insertion order, just in case
      s.onload = function() {{ s.dataset.umbraLoaded = '1'; resolve(); }};
      s.onerror = function() {{ reject(new Error('Failed to load ' + src)); }};
      document.head.appendChild(s);
    }});
  }}

  function loadLibs() {{
    if (libsPromise) return libsPromise;
    // georaster-layer-for-leaflet extends L.GridLayer at evaluation
    // time, so by the time we get here Leaflet must already be on the
    // page -- which it is, because Folium loads it in <head> during
    // initial page parse. The georaster bundle itself has no Leaflet
    // dependency, so the two can load in parallel.
    libsPromise = Promise.all([
      injectScript(GEORASTER_URL),
      injectScript(GEORASTER_LAYER_URL)
    ]).then(function() {{
      if (typeof parseGeoraster === 'undefined' ||
          typeof GeoRasterLayer === 'undefined') {{
        throw new Error(
          'CDN libs loaded but expected globals (parseGeoraster, ' +
          'GeoRasterLayer) are missing. Has a CDN URL drifted?');
      }}
    }});
    return libsPromise;
  }}

  function percentile(samples, p) {{
    var sorted = samples.slice().sort(function(a, b) {{ return a - b; }});
    var idx = Math.max(0, Math.min(sorted.length - 1,
      Math.floor((p / 100.0) * (sorted.length - 1))));
    return sorted[idx];
  }}

  function computeStretch(georaster) {{
    var values = georaster.values && georaster.values[0];
    if (!values) {{ return null; }}
    var samples = [];
    var step = Math.max(1, Math.floor(
      (values.length * (values[0] ? values[0].length : 1)) / {sample_cap}));
    var counter = 0;
    for (var i = 0; i < values.length; i++) {{
      var row = values[i];
      for (var j = 0; j < row.length; j++) {{
        if (counter++ % step !== 0) continue;
        var v = row[j];
        if (isFinite(v) && v > 0 && v !== georaster.noDataValue) {{
          samples.push(v);
        }}
      }}
    }}
    if (samples.length === 0) return null;
    var lo = percentile(samples, {plo});
    var hi = percentile(samples, {phi});
    if (hi <= lo) hi = lo + 1;
    return {{ lo: lo, hi: hi }};
  }}

  function loadCogAsLayer(button) {{
    var url = button.getAttribute('data-asset-url');
    var id = button.getAttribute('data-item-id');
    button.disabled = true;
    button.textContent = 'Loading SAR image…';
    button.setAttribute('data-state', 'loading');
    loadLibs().then(function() {{
      return parseGeoraster(url);
    }}).then(function(georaster) {{
      var stretch = computeStretch(georaster);
      if (!stretch) {{
        button.disabled = false;
        button.textContent = 'No valid SAR pixels';
        button.setAttribute('data-state', 'error');
        return;
      }}
      var layer = new GeoRasterLayer({{
        georaster: georaster,
        opacity: 1.0,
        pixelValuesToColorFn: function(values) {{
          var v = values[0];
          if (!isFinite(v) || v <= 0 || v === georaster.noDataValue) {{
            return null;  // transparent
          }}
          var s = Math.max(0, Math.min(255,
            Math.floor((v - stretch.lo) / (stretch.hi - stretch.lo) * 255)));
          return 'rgb(' + s + ',' + s + ',' + s + ')';
        }},
        resolution: 256
      }});
      var map = findMap();
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
      var map = findMap();
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
