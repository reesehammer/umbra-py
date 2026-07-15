"""Self-contained interactive catalog explorer (the ``umbra demo`` command).

Every other visual surface in the toolkit emits a *one-shot* artifact: a map
of one search, a gallery of one set of thumbnails, a swipe of two passes. Change
the date range or the product filter and you re-run the CLI and open a new file.
This module produces the missing piece the demo-gap analysis
(:doc:`DEMO_APP_GAPS`) calls the frontier — a **self-serve explorer**: one HTML
page over a whole slice of the catalog with *interactive* client-side filters
(date range, product type, free-text site search), marker **clustering** so it
scales past a Folium map's few-hundred-polygon ceiling, and click-to-quicklook
SAR imagery streamed on demand.

Design, deliberately in the repo's grain:

* **Static, single file, no server.** The page is self-contained HTML — Leaflet
  and Leaflet.markercluster from pinned CDNs, the catalog embedded as a JSON
  blob, all filtering in the browser. It opens from ``file://`` or any static
  host (GitHub Pages), exactly like ``umbra swipe`` / ``umbra gallery`` output.
  No FastAPI, no build toolchain — the productized server app is
  ``DEMO_APP_GAPS.md`` Path B; this is Path A's front end delivered as an
  artifact.

* **Reads the fast index.** Like the other visual commands it routes through the
  shared ``_gather_items`` helper, so ``--local`` answers from a prebuilt index
  (``umbra index fetch`` / ``umbra index build``) in milliseconds instead of
  re-walking S3 — the "no multi-minute walk in the user's critical path"
  requirement a demo needs.

* **Reuses the proven COG driver.** The per-item "Get SAR image" button drives
  the same browser-side geotiff.js fetcher as ``umbra map --lazy-imagery`` (see
  :mod:`umbra_py._lazy_imagery`); the only addition there is a
  ``window.umbraLazyMap`` fallback so the same driver resolves a plain Leaflet
  map on this non-Folium page. The HTML stays small regardless of item count —
  you pay the COG fetch only for scenes you click.

The catalog data is injected as a JSON global (``window.UMBRA_DEMO``) and the
application JavaScript is a *static* string that reads it, so there is no
Python-side string interpolation into executable JS — the one place remote
metadata meets the page, it arrives as JSON (with ``</`` neutralised against a
``</script>`` break-out) and is placed into the DOM with ``textContent`` /
``setAttribute``, never parsed as HTML.

Needs **no extra**: the page is generated with the standard library, and the
map (Leaflet) and the on-click COG decode (geotiff.js) run browser-side from
pinned CDNs, so the generator runs in a core install and is fully
offline-testable. (Contrast ``umbra gallery``, which streams thumbnails through
``rasterio`` in Python and so needs the ``viz`` extra.)
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from html import escape
from pathlib import Path
from typing import Any

from .constants import ATTRIBUTION
from .models import UmbraItem

# Pinned CDN assets. Bumped deliberately -- an unpinned CDN can regress a
# generated page without warning (the same discipline _lazy_imagery applies to
# geotiff.js). Leaflet 1.9.4 and Leaflet.markercluster 1.5.3 are the current
# stable releases and the versions Folium itself ships against.
LEAFLET_CSS = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
LEAFLET_JS = "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
MARKERCLUSTER_CSS = "https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.css"
MARKERCLUSTER_CSS_DEFAULT = (
    "https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.Default.css"
)
MARKERCLUSTER_JS = "https://unpkg.com/leaflet.markercluster@1.5.3/dist/leaflet.markercluster.js"


def _lazy_bounds_for(bbox: tuple[float, float, float, float]) -> list[float]:
    """Return an item bbox as ``[south, west, north, east]``.

    Matches :func:`umbra_py._lazy_imagery.popup_button_html`'s ``data-bounds``
    order (``"min_lat,min_lon,max_lat,max_lon"``) so the shared driver places
    the overlay identically. ``bbox`` is the STAC
    ``(min_lon, min_lat, max_lon, max_lat)`` order.
    """
    min_lon, min_lat, max_lon, max_lat = bbox
    return [min_lat, min_lon, max_lat, max_lon]


def _demo_feature(
    item: UmbraItem,
    lazy: dict[str, tuple[str, tuple[float, float, float, float]]],
) -> dict[str, Any] | None:
    """Build the compact GeoJSON-ish feature the front end consumes.

    Returns ``None`` for an item with no footprint *and* no bbox — it can be
    neither placed on the map nor clustered, so it has nothing to contribute to
    an explorer. Properties are the exact facets the client filters and renders
    on: place, product type, date, platform, polarizations, assets, the STAC
    link, and (when resolvable) the GEC COG URL + placement bounds for the
    on-click overlay.
    """
    from .viz import _centroid, _geometry_for  # noqa: PLC0415

    centroid = _centroid(item)
    geometry = _geometry_for(item)
    if centroid is None and geometry is None:
        return None
    dt = item.datetime
    lazy_entry = lazy.get(item.id)
    props: dict[str, Any] = {
        "id": item.id,
        "place": item.task,
        "product": item.product_type,
        "datetime": dt.isoformat() if dt else None,
        # A plain YYYY-MM-DD keeps the client's date-range compare a lexical
        # string comparison -- no Date parsing, no timezone surprises.
        "date": dt.date().isoformat() if dt else None,
        "platform": item.platform,
        "polarizations": list(item.polarizations),
        "assets": list(item.available_assets),
        "stac_href": item.href,
        "centroid": list(centroid) if centroid else None,
        "lazy_url": lazy_entry[0] if lazy_entry else None,
        "lazy_bounds": _lazy_bounds_for(lazy_entry[1]) if lazy_entry else None,
    }
    return {"type": "Feature", "id": item.id, "geometry": geometry, "properties": props}


def build_demo(
    items: Iterable[UmbraItem],
    *,
    title: str = "Umbra open-data explorer",
    subtitle: str | None = None,
    asset: str = "GEC",
    lazy_imagery: bool = True,
    percentile: tuple[float, float] = (2.0, 98.0),
) -> str:
    """Render items as a single self-contained interactive explorer page.

    Parameters
    ----------
    items:
        The acquisitions to explore. Any without a footprint or bbox are
        dropped (they cannot be mapped).
    title, subtitle:
        Header text; ``subtitle`` is a good place for the search terms that
        produced the page.
    asset:
        Product whose cloud-optimized GeoTIFF the "Get SAR image" button
        streams on click (``"GEC"``, the detected-amplitude COG, is the
        sensible default; ``"CSI"`` also works).
    lazy_imagery:
        When True (default) each item with a resolvable ``asset`` COG gets the
        on-click SAR overlay button. Set False for a metadata-only explorer.
    percentile:
        Contrast-stretch cuts handed to the shared COG driver, mirroring
        :func:`umbra_py.viz._stretch_to_rgba`'s ``(2, 98)`` default.

    Returns the HTML as a string; use :func:`save_demo` to write it to disk.
    """
    items = list(items)
    from .viz import _resolve_lazy_urls  # noqa: PLC0415

    lazy = _resolve_lazy_urls(items, lazy_imagery, asset)
    features = [f for f in (_demo_feature(i, lazy) for i in items) if f is not None]

    products = sorted({f["properties"]["product"] for f in features if f["properties"]["product"]})
    dates = [f["properties"]["date"] for f in features if f["properties"]["date"]]
    date_min = min(dates) if dates else None
    date_max = max(dates) if dates else None

    config = {
        "title": title,
        "subtitle": subtitle,
        "attribution": ATTRIBUTION,
        "features": features,
        "products": products,
        "dateMin": date_min,
        "dateMax": date_max,
        "lazyImagery": bool(lazy),
    }
    # json.dumps then neutralise any "</" so a place name containing the literal
    # "</script>" cannot break out of the embedded data block.
    config_json = json.dumps(config, separators=(",", ":")).replace("</", "<\\/")

    from ._lazy_imagery import driver_script  # noqa: PLC0415

    driver = (
        driver_script(percentile_low=percentile[0], percentile_high=percentile[1]) if lazy else ""
    )

    return _PAGE_TEMPLATE.format(
        title=escape(title),
        head_links=_HEAD_LINKS,
        styles=_STYLES,
        config_json=config_json,
        app_js=_APP_JS,
        driver_js=driver,
    )


def save_demo(items: Iterable[UmbraItem], dest: str | os.PathLike, **kwargs: Any) -> Path:
    """Render an interactive explorer and write it to ``dest`` as standalone HTML.

    See :func:`build_demo` for the rendering options.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(build_demo(items, **kwargs))
    return dest


_HEAD_LINKS = (
    f'<link rel="stylesheet" href="{LEAFLET_CSS}"/>\n'
    f'<link rel="stylesheet" href="{MARKERCLUSTER_CSS}"/>\n'
    f'<link rel="stylesheet" href="{MARKERCLUSTER_CSS_DEFAULT}"/>'
)

_SCRIPT_LINKS = f'<script src="{LEAFLET_JS}"></script>\n<script src="{MARKERCLUSTER_JS}"></script>'

_STYLES = """
*, *::before, *::after { box-sizing: border-box; }
html, body { margin: 0; height: 100%; }
#umbra-app {
  display: flex; height: 100vh; width: 100vw;
  font: 14px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  color: #1a1a1a;
}
#umbra-sidebar {
  width: 320px; flex: 0 0 320px; overflow-y: auto;
  background: #fafafa; border-right: 1px solid #ddd; padding: 16px;
}
#umbra-map { flex: 1 1 auto; height: 100%; }
#umbra-sidebar h1 { font-size: 18px; margin: 0 0 4px; }
#umbra-sidebar .subtitle { color: #666; font-size: 12px; margin: 0 0 12px; }
.umbra-attr { color: #888; font-size: 11px; margin: 4px 0 16px; }
.umbra-filter { margin-bottom: 16px; }
.umbra-filter label { display: block; font-weight: 600; font-size: 12px; margin-bottom: 4px; }
.umbra-filter input[type=text], .umbra-filter input[type=date] {
  width: 100%; padding: 5px 7px; border: 1px solid #bbb; border-radius: 4px; font: inherit;
}
.umbra-dates { display: flex; gap: 8px; }
.umbra-dates > div { flex: 1; }
.umbra-chips { display: flex; flex-wrap: wrap; gap: 6px; }
.umbra-chip {
  cursor: pointer; user-select: none; padding: 3px 10px; border-radius: 12px;
  border: 1px solid #bbb; background: #fff; font-size: 12px;
}
.umbra-chip.active { background: #2b6cb0; border-color: #2b6cb0; color: #fff; }
#umbra-count { font-size: 12px; color: #444; margin-bottom: 12px; }
#umbra-reset {
  cursor: pointer; font: inherit; font-size: 12px; padding: 5px 12px;
  border: 1px solid #bbb; border-radius: 4px; background: #fff;
}
#umbra-detail {
  margin-top: 16px; padding-top: 12px; border-top: 1px solid #ddd; font-size: 12px;
}
#umbra-detail .empty { color: #999; }
#umbra-detail table { border-collapse: collapse; width: 100%; }
#umbra-detail th { text-align: left; padding: 2px 8px 2px 0; color: #555; vertical-align: top; }
#umbra-detail td { padding: 2px 0; word-break: break-word; }
.umbra-sar-btn {
  font: 12px/1.2 -apple-system, sans-serif; margin-top: 8px; padding: 4px 10px;
  border: 1px solid #888; border-radius: 3px; background: #f7f7f7; cursor: pointer;
}
"""

# The application. A *static* string (no Python interpolation): every dynamic
# value arrives through window.UMBRA_DEMO. It builds the map, a clustered marker
# layer over item centroids (the scale answer -- thousands of points instead of
# thousands of DOM polygons), the faceted filter controls, and a detail panel
# that draws the selected item's footprint and, for a lazy-imagery page, a
# "Get SAR image" button wired to the shared geotiff.js driver.
_APP_JS = """
(function () {
  var CFG = window.UMBRA_DEMO || { features: [] };
  var features = CFG.features || [];

  var map = L.map('umbra-map', { preferCanvas: true }).setView([20, 0], 2);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '&copy; OpenStreetMap contributors'
  }).addTo(map);
  // Publish the map so the shared lazy-imagery driver (which walks the DOM for a
  // Folium map, then falls back to this global) can find it on this plain page.
  window.umbraLazyMap = map;

  var cluster = L.markerClusterGroup({ chunkedLoading: true });
  map.addLayer(cluster);
  var selectedFootprint = null;

  // --- filter state ---
  var state = { text: '', start: CFG.dateMin || '', end: CFG.dateMax || '', products: {} };
  (CFG.products || []).forEach(function (p) { state.products[p] = true; });

  function passesFilter(props) {
    if (state.products && Object.keys(state.products).length) {
      // A product is "on" unless explicitly toggled off.
      if (props.product && state.products[props.product] === false) return false;
    }
    if (state.start && props.date && props.date < state.start) return false;
    if (state.end && props.date && props.date > state.end) return false;
    if (state.text) {
      var hay = ((props.place || '') + ' ' + (props.id || '')).toLowerCase();
      if (hay.indexOf(state.text) === -1) return false;
    }
    return true;
  }

  function markerFor(feature) {
    var c = feature.properties.centroid;
    if (!c) return null;
    var m = L.marker([c[0], c[1]]);
    m.on('click', function () { showDetail(feature); });
    return m;
  }

  var countEl = document.getElementById('umbra-count');

  function render() {
    cluster.clearLayers();
    var shown = 0;
    var markers = [];
    for (var i = 0; i < features.length; i++) {
      var f = features[i];
      if (!passesFilter(f.properties)) continue;
      var m = markerFor(f);
      if (m) { markers.push(m); shown++; }
    }
    cluster.addLayers(markers);
    countEl.textContent = shown + ' of ' + features.length + ' acquisitions shown';
  }

  // --- detail panel ---
  function row(label, value) {
    var tr = document.createElement('tr');
    var th = document.createElement('th'); th.textContent = label;
    var td = document.createElement('td'); td.textContent = (value == null ? '\\u2014' : value);
    tr.appendChild(th); tr.appendChild(td);
    return tr;
  }

  function showDetail(feature) {
    var p = feature.properties;
    var panel = document.getElementById('umbra-detail');
    panel.innerHTML = '';
    var table = document.createElement('table');
    table.appendChild(row('ID', p.id));
    table.appendChild(row('Place', p.place));
    table.appendChild(row('Acquired', p.datetime));
    table.appendChild(row('Platform', p.platform));
    table.appendChild(row('Product', p.product));
    table.appendChild(row('Polarizations', (p.polarizations || []).join(', ')));
    table.appendChild(row('Assets', (p.assets || []).join(', ')));
    panel.appendChild(table);

    if (p.stac_href) {
      var a = document.createElement('a');
      a.href = p.stac_href; a.target = '_blank'; a.rel = 'noopener';
      a.textContent = 'open STAC item';
      var pw = document.createElement('p'); pw.style.marginTop = '8px';
      pw.appendChild(a); panel.appendChild(pw);
    }

    // On-click SAR overlay button: built with DOM APIs (setAttribute /
    // textContent never parse HTML, so no escaping of remote strings is
    // needed) and wired to the shared driver's umbraToggleSarImage contract.
    if (CFG.lazyImagery && p.lazy_url && p.lazy_bounds && window.umbraToggleSarImage) {
      var btn = document.createElement('button');
      btn.type = 'button'; btn.className = 'umbra-sar-btn';
      btn.setAttribute('data-item-id', p.id);
      btn.setAttribute('data-asset-url', p.lazy_url);
      btn.setAttribute('data-bounds', p.lazy_bounds.join(','));
      btn.setAttribute('data-state', 'idle');
      btn.textContent = 'Get SAR image';
      btn.onclick = function () { window.umbraToggleSarImage(btn); };
      panel.appendChild(btn);
    }

    // Draw the selected item's footprint so the point gets geographic context.
    if (selectedFootprint) { map.removeLayer(selectedFootprint); selectedFootprint = null; }
    if (feature.geometry) {
      selectedFootprint = L.geoJSON(feature, {
        style: { color: '#2b6cb0', weight: 2, fillOpacity: 0.08 }
      }).addTo(map);
    }
    if (p.centroid) { map.panTo([p.centroid[0], p.centroid[1]]); }
  }

  // --- wire controls ---
  var textInput = document.getElementById('umbra-text');
  textInput.addEventListener('input', function () {
    state.text = textInput.value.trim().toLowerCase(); render();
  });
  var startInput = document.getElementById('umbra-start');
  var endInput = document.getElementById('umbra-end');
  if (CFG.dateMin) {
    startInput.value = CFG.dateMin; startInput.min = CFG.dateMin; startInput.max = CFG.dateMax;
  }
  if (CFG.dateMax) {
    endInput.value = CFG.dateMax; endInput.min = CFG.dateMin; endInput.max = CFG.dateMax;
  }
  startInput.addEventListener('change', function () { state.start = startInput.value; render(); });
  endInput.addEventListener('change', function () { state.end = endInput.value; render(); });

  var chipBox = document.getElementById('umbra-products');
  (CFG.products || []).forEach(function (prod) {
    var chip = document.createElement('span');
    chip.className = 'umbra-chip active';
    chip.textContent = prod;
    chip.addEventListener('click', function () {
      var on = chip.classList.toggle('active');
      state.products[prod] = on;
      render();
    });
    chipBox.appendChild(chip);
  });

  document.getElementById('umbra-reset').addEventListener('click', function () {
    state.text = ''; textInput.value = '';
    state.start = CFG.dateMin || ''; startInput.value = CFG.dateMin || '';
    state.end = CFG.dateMax || ''; endInput.value = CFG.dateMax || '';
    (CFG.products || []).forEach(function (p) { state.products[p] = true; });
    Array.prototype.forEach.call(chipBox.children, function (c) { c.classList.add('active'); });
    render();
  });

  render();
  // Frame the full set on first load.
  var pts = features.map(function (f) { return f.properties.centroid; }).filter(Boolean);
  if (pts.length) { map.fitBounds(pts, { padding: [40, 40], maxZoom: 12 }); }
})();
"""

_PAGE_TEMPLATE = (
    """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{title}</title>
{head_links}
<style>{styles}</style>
</head>
<body>
<div id="umbra-app">
  <aside id="umbra-sidebar">
    <div id="umbra-header"></div>
    <div class="umbra-filter">
      <label for="umbra-text">Search site / id</label>
      <input type="text" id="umbra-text" placeholder="e.g. Centerfield"/>
    </div>
    <div class="umbra-filter">
      <label>Date range</label>
      <div class="umbra-dates">
        <div><input type="date" id="umbra-start"/></div>
        <div><input type="date" id="umbra-end"/></div>
      </div>
    </div>
    <div class="umbra-filter">
      <label>Product type</label>
      <div class="umbra-chips" id="umbra-products"></div>
    </div>
    <div id="umbra-count"></div>
    <button id="umbra-reset" type="button">Reset filters</button>
    <div id="umbra-detail"><p class="empty">Click a marker to see its metadata.</p></div>
  </aside>
  <div id="umbra-map"></div>
</div>
"""
    + _SCRIPT_LINKS
    + """
<script id="umbra-data" type="application/json"></script>
<script>window.UMBRA_DEMO = {config_json};</script>
<script>
(function () {{
  var CFG = window.UMBRA_DEMO || {{}};
  var h = document.getElementById('umbra-header');
  var title = document.createElement('h1'); title.textContent = CFG.title || 'Umbra explorer';
  h.appendChild(title);
  if (CFG.subtitle) {{
    var sub = document.createElement('p'); sub.className = 'subtitle';
    sub.textContent = CFG.subtitle; h.appendChild(sub);
  }}
  var attr = document.createElement('p'); attr.className = 'umbra-attr';
  attr.textContent = CFG.attribution || ''; h.appendChild(attr);
}})();
</script>
<script>{app_js}</script>
<script>{driver_js}</script>
</body>
</html>
"""
)
