"""Browser-side lazy-fetch SAR overlays.

The map's HTML carries a per-item ``Get SAR image`` button instead of a
pre-baked PNG. On click, the page lazily loads
``georaster-layer-for-leaflet`` (which pulls in ``geotiff.js``) from a
CDN, streams the GEC cloud-optimized GeoTIFF directly from the Umbra
public bucket via HTTP range requests, applies the same percentile
stretch that :func:`umbra_py.viz._stretch_to_rgba` performs in Python,
and adds the result as a :class:`L.GeoRasterLayer` on the running
Folium map.

This lets a 200-item map weigh ~30 KB instead of hundreds of MB — the
user only pays the fetch cost for the items they actually want to see.
The Umbra bucket already serves permissive CORS headers (``*`` origin,
``GET``/``HEAD`` methods, ``range`` headers) on every object, which is
what makes browser-direct streaming possible.

The implementation here is intentionally a couple of JS string
templates rather than a Jinja template module: the templates are short,
they only ever land inside a ``<script>`` block at the bottom of the
map, and keeping them inline keeps the rendering surface visible from
Python.
"""

from __future__ import annotations

import html
import json
from typing import Any

# Pinned to the latest releases at the time of writing. Bump these
# deliberately -- COG decoding in the browser is a moving target and an
# unpinned CDN URL can regress without warning.
_GEORASTER_JS = "https://unpkg.com/georaster@1.6.0/dist/georaster.browser.bundle.min.js"
_GEORASTER_LAYER_JS = (
    "https://unpkg.com/georaster-layer-for-leaflet@3.10.0/dist/georaster-layer-for-leaflet.min.js"
)


def cdn_script_tags() -> str:
    """The ``<script>`` tags that pull the COG-loading libraries.

    Injected once into the map's ``<head>``. The browser still won't
    fetch the bytes until the first ``Get SAR image`` click triggers
    ``parseGeoraster``, so the cost of a map that nobody clicks is two
    tiny HTTP HEADs (handled by the browser's normal script cache).
    """
    return f'<script src="{_GEORASTER_JS}"></script>\n<script src="{_GEORASTER_LAYER_JS}"></script>'


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
    """
    return _DRIVER_TEMPLATE.format(
        map_var=map_var,
        plo=percentile_low,
        phi=percentile_high,
        sample_cap=sample_cap,
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


def feature_url_map(
    pairs: list[tuple[str, str]],
) -> str:
    """Serialize the ``{item_id: asset_url}`` map for the driver.

    The driver script reads this at load time so it has a canonical
    list even if the user dismisses the popup before clicking. Kept as
    JSON to dodge JS-injection concerns from item IDs or filenames.
    """
    obj: dict[str, Any] = {pid: url for pid, url in pairs}
    return json.dumps(obj, separators=(",", ":"))


# The template stays small on purpose. The flow:
#  1. Click → loading state.
#  2. parseGeoraster(url) streams the COG via HTTP range requests.
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
  function findMap() {{ return window['{map_var}']; }}

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
    if (typeof parseGeoraster === 'undefined' ||
        typeof GeoRasterLayer === 'undefined') {{
      button.disabled = false;
      button.textContent = 'SAR libs unavailable';
      button.setAttribute('data-state', 'error');
      return;
    }}
    parseGeoraster(url).then(function(georaster) {{
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
