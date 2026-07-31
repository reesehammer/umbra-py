"""Self-contained interactive catalog explorer (the ``umbra demo`` command).

Every other visual surface in the toolkit emits a *one-shot* artifact: a map
of one search, a gallery of one set of thumbnails, a swipe of two passes. Change
the date range or the product filter and you re-run the CLI and open a new file.
This module produces the missing piece the demo-gap analysis
called the frontier — a **self-serve explorer**: one HTML
page over a whole slice of the catalog with *interactive* client-side filters
(date range, product type, polarization, free-text site search), marker
**clustering** so it scales past a Folium map's few-hundred-polygon ceiling, and
click-to-quicklook SAR imagery streamed on demand.

Design, deliberately in the repo's grain:

* **Static, single file, no server.** The page is self-contained HTML — Leaflet
  and Leaflet.markercluster from pinned CDNs, the catalog embedded as a JSON
  blob, all filtering in the browser. It opens from ``file://`` or any static
  host (GitHub Pages), exactly like ``umbra swipe`` / ``umbra gallery`` output.
  No FastAPI, no build toolchain — the productized server app is
  :mod:`umbra_py.serve`; this is the static front end delivered as an
  artifact.

* **Optionally server-backed for analysis (R4).** With ``server_url`` set to a
  running ``umbra serve`` instance, the sidebar gains an "Analyze this view"
  panel: its buttons POST the currently-filtered acquisitions to the server's
  ``/artifacts/change|timescan|swipe|stats`` endpoints and render the returned
  artifact in place — the "run this analysis here" affordance the demo-gap
  analysis called the last self-serve gap (R4). The
  server does the heavy raster work and caches every result; the page itself
  stays a static file (the panel is simply hidden when no ``server_url`` is
  configured, so the default build is unchanged).

  Three of those products are pictures; **"Quantify"** is the one that is
  numbers. It POSTs the same filtered view to ``POST /artifacts/stats`` and
  reads out :func:`~umbra_py.load.stack_stats`' measurement — how much the site
  moved between its first and last pass (mean decibels, the fraction of ground
  past the change threshold, and the area in km², since the endpoint stacks on
  the site's UTM grid), which block moved most and between which two passes, and
  the north-up ASCII heat-grid of signed change. So the self-serve loop can
  *measure*, not only look. The panel formats the server's numbers and computes
  none of its own, so the page and ``umbra stack --stats`` cannot disagree.

  Two **sparklines** carry the part a headline number cannot: one bar per
  consecutive pass-to-pass step, signed and zero-baselined, for the site as a
  whole (each pass's ``change_vs_previous``) and for the block that moved most
  (its ``series``, which is why the request asks for ``block_series``). A net
  first-to-last figure and a peak interval read identically for a corner that
  drifted a decibel every pass and one that jumped twelve once and held; the
  sequence is what tells them apart.

* **Instant thumbnail preview when server-backed (G6).** With ``server_url``
  set, clicking a scene *leads* its detail panel with a small SAR picture pulled
  from ``GET /artifacts/thumbnail/{id}.png`` — the baked quicklook thumbnail
  ``umbra index bake-thumbnails`` stored in the index, served straight from local
  bytes with no render (falling back to a live quicklook render for a scene not
  yet baked). This is the client wiring the G6 thumbnail bake left
  open: the primitive and the server endpoint shipped, so the detail panel now
  opens with a radar picture, not just metadata, and the heavier on-click
  "Get SAR image" COG overlay stays the deeper look. A scene with no baked
  thumbnail 404s and the element is dropped, so a metadata-only scene is never a
  broken image; without ``server_url`` the panel is unchanged.

* **Whole-archive mode over PMTiles.** The default page embeds the gathered
  slice as JSON, which is the right shape for a search result but caps the
  explorer at whatever fits in a download. Pass ``pmtiles_url`` and the page
  swaps its embedded-slice Leaflet cluster for a **MapLibre GL vector layer over
  a whole-catalog ``.pmtiles`` archive** (the one :mod:`umbra_py.pmtiles` /
  ``umbra tiles`` writes and ``publish-index.yml`` publishes): the browser
  range-reads only the tiles for the current view, so the *entire* archive is
  explorable from a page that stays a few kilobytes, and the same sidebar
  filters (free-text, date range, product and polarization chips) run as
  MapLibre filter expressions evaluated inside the tiles. This is the
  whole-catalog follow-on that collapses the showcase's separate
  whole-catalog *map* and interactive *explorer* into one page (``umbra showcase
  --unified``). The archive carries each acquisition twice — a centroid at every
  zoom and its clipped **footprint polygon** at the deeper ones — so the page
  draws coverage shape as you zoom in, filtered by the same expression as the
  markers and clickable to the same detail panel. The **on-click "Get SAR image"
  overlay works here too** — the last thing the embedded-slice mode had over
  the whole-archive one: the archive's features carry a reference to each
  acquisition's GEC COG and the bounds to place it (kept lean as a filename
  resolved against the ``stac_href`` the tiles already carry), and the page ships
  a MapLibre-placing build of the *same* geotiff.js driver, so any scene
  in the archive is one click from its actual radar picture. The last two fields
  the tiles withheld — polarizations and the per-product asset list — now ride
  along comma-joined, so the whole-archive explorer is a **superset** of the
  slice one: same detail panel, same facets, every acquisition.

* **Filterable by polarization (both modes).** The one facet the sidebar lacked
  was the one that decides whether an analysis is *valid* rather than merely
  what it shows: HH and VV image different scattering, so a change measurement
  across mixed polarizations reads a physics difference as change. ``POST
  /artifacts/stats`` refuses such a selection outright and tells the caller to
  narrow it — which, before this, the explorer had no control to do. A
  polarization chip row sits under the product chips in both modes, filtering
  the embedded slice in JavaScript and the whole archive as a MapLibre
  ``index-of`` test evaluated inside the tiles.

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

from .constants import ATTRIBUTION, POLARIZATIONS, PRODUCT_ASSETS
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


def _is_http_url(url: Any) -> bool:
    """True only for an ``http(s)`` URL — the schemes safe to make clickable.

    STAC hrefs come from remote JSON; anything else (a ``javascript:`` scheme in
    particular) must not reach an anchor's ``href``.
    """
    return isinstance(url, str) and url.startswith(("http://", "https://"))


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
    # The STAC href is assigned to an anchor's ``href`` DOM property client-side,
    # so a ``javascript:`` scheme from a hostile STAC document would be a
    # clickable script link. Only pass through ``http(s)`` URLs; the client
    # already omits the link when ``stac_href`` is absent. (No HTML-escaping
    # here -- this value travels as JSON and is set via a DOM property, not
    # parsed as HTML.)
    stac_href = item.href if _is_http_url(item.href) else None
    props: dict[str, Any] = {
        "id": item.id,
        # Prefer a baked reverse-geocoded label ("Reykjavík, Iceland") over the
        # task codename when the index has one (see CatalogIndex.bake_places); it
        # is what the detail panel shows and the free-text search matches on.
        "place": item.place or item.task,
        "product": item.product_type,
        "datetime": dt.isoformat() if dt else None,
        # A plain YYYY-MM-DD keeps the client's date-range compare a lexical
        # string comparison -- no Date parsing, no timezone surprises.
        "date": dt.date().isoformat() if dt else None,
        "platform": item.platform,
        "polarizations": list(item.polarizations),
        "assets": list(item.available_assets),
        "stac_href": stac_href,
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
    server_url: str | None = None,
    pmtiles_url: str | None = None,
    pmtiles_layer: str = "acquisitions",
) -> str:
    """Render items as a single self-contained interactive explorer page.

    Parameters
    ----------
    items:
        The acquisitions to explore. Any without a footprint or bbox are
        dropped (they cannot be mapped). **Ignored when ``pmtiles_url`` is set**
        — that mode draws every acquisition from the tiled archive instead, so
        the page carries no embedded slice at all.
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
    server_url:
        Base URL of a running ``umbra serve`` instance
        (e.g. ``"http://localhost:8000"``). When set, the sidebar gains an
        "Analyze this view" panel whose buttons POST the currently-filtered
        acquisitions to the server's ``/artifacts/change|timescan|swipe|stats``
        endpoints and show the returned artifact — the R4 "run this analysis
        here" affordance. Three of them render a picture;
        "Quantify" (``/artifacts/stats``) reads out the numeric measurement
        instead, sparklining the site's and its peak block's pass-to-pass
        history beside the headline figures. It also turns on the instant
        thumbnail preview in the detail panel (served from the endpoint's
        ``/artifacts/thumbnail/{id}.png`` baked thumbnail, the G6 bake).
        When ``None`` (default) the page stays fully static and
        self-contained, exactly as before.
    pmtiles_url:
        Location of a whole-catalog ``.pmtiles`` archive relative to the page
        (e.g. ``"catalog.pmtiles"``) or an absolute URL. When set the explorer
        draws **every** acquisition in that archive from vector tiles read by
        range request, instead of the embedded ``items`` slice: the page stays a
        few kilobytes whatever the catalog's size, and the sidebar filters run as
        MapLibre expressions over the tiles. Footprint outlines are drawn from
        the archive's :data:`umbra_py.pmtiles.FOOTPRINT_LAYER` polygons where it
        carries them (a centroids-only archive just shows the markers), and the
        on-click SAR overlay is offered for any feature whose tile carries a COG
        reference (:func:`umbra_py.pmtiles.build_pmtiles`'s ``cog_asset``, on by
        default; an archive tiled without it simply shows no button). ``items``
        and ``asset`` do not apply in this mode — the archive is the data source,
        and it fixes the product it references — but ``lazy_imagery``,
        ``percentile`` and ``server_url`` all do. ``None`` (default) keeps the
        embedded-slice Leaflet page unchanged.
    pmtiles_layer:
        Source-layer name of the archive's centroid points. Must match the one it
        was written with (:func:`umbra_py.pmtiles.build_pmtiles` defaults to
        ``"acquisitions"``, as does this).

    Returns the HTML as a string; use :func:`save_demo` to write it to disk.
    """
    if pmtiles_url:
        return _build_pmtiles_demo(
            pmtiles_url,
            layer=pmtiles_layer,
            title=title,
            subtitle=subtitle,
            server_url=server_url,
            lazy_imagery=lazy_imagery,
            percentile=percentile,
        )

    items = list(items)
    from .viz import _resolve_lazy_urls  # noqa: PLC0415

    lazy = _resolve_lazy_urls(items, lazy_imagery, asset)
    features = [f for f in (_demo_feature(i, lazy) for i in items) if f is not None]

    products = sorted({f["properties"]["product"] for f in features if f["properties"]["product"]})
    # Chips for the polarizations this slice actually contains, in the canonical
    # order rather than alphabetically, so the row reads the same on every page.
    present = {p for f in features for p in f["properties"]["polarizations"]}
    polarizations = [p for p in POLARIZATIONS if p in present]
    polarizations += sorted(present - set(POLARIZATIONS))
    dates = [f["properties"]["date"] for f in features if f["properties"]["date"]]
    date_min = min(dates) if dates else None
    date_max = max(dates) if dates else None

    config = {
        "title": title,
        "subtitle": subtitle,
        "attribution": ATTRIBUTION,
        "features": features,
        "products": products,
        "polarizations": polarizations,
        "dateMin": date_min,
        "dateMax": date_max,
        "lazyImagery": bool(lazy),
        # None keeps the page fully static; a URL turns on the "Analyze this
        # view" panel that POSTs to a running ``umbra serve`` instance. Trailing
        # slash is trimmed client-side, so either form is accepted.
        "serverUrl": server_url or None,
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
        script_links=_SCRIPT_LINKS,
        styles=_STYLES,
        config_json=config_json,
        app_js=_SHARED_JS + _APP_JS,
        driver_js=driver,
    )


def _build_pmtiles_demo(
    pmtiles_url: str,
    *,
    layer: str,
    title: str,
    subtitle: str | None,
    server_url: str | None,
    lazy_imagery: bool = True,
    percentile: tuple[float, float] = (2.0, 98.0),
) -> str:
    """Render the whole-archive explorer over a ``.pmtiles`` catalog.

    Same page, same sidebar, same server-backed panels — a MapLibre GL vector
    layer over the tiled archive in place of the embedded-slice Leaflet cluster.
    No item list is needed (or used): the browser range-reads the archive, so
    the generated HTML is the same handful of kilobytes for a catalog of any
    size.

    The product and polarization chips come from
    :data:`umbra_py.constants.PRODUCT_ASSETS` /
    :data:`umbra_py.constants.POLARIZATIONS` rather than from a scanned slice —
    both sets are closed and known, and deriving them from a *sample* of a
    catalog the page does not otherwise read would let a chip go missing for the
    whole archive. For the same reason the date inputs start empty (unbounded)
    instead of framing a sample's extent, which would silently hide most of the
    archive behind a default filter.

    The on-click "Get SAR image" overlay works here too: the archive's features
    carry a ``cog`` reference and its ``bounds`` (see
    :func:`umbra_py.pmtiles.build_pmtiles`'s ``cog_asset``), so the detail panel
    can hand the same shared geotiff.js driver an absolute URL — built here as a
    MapLibre-placing build of that one driver rather than a second one. An
    archive tiled without those properties simply shows no button, so an older
    ``.pmtiles`` keeps working unchanged.
    """
    from .pmtiles import (  # noqa: PLC0415
        FOOTPRINT_LAYER,
        MAPLIBRE_CSS,
        MAPLIBRE_JS,
        PMTILES_JS,
    )

    config = {
        "title": title,
        "subtitle": subtitle,
        "attribution": ATTRIBUTION,
        "products": list(PRODUCT_ASSETS),
        "polarizations": list(POLARIZATIONS),
        "serverUrl": server_url or None,
        "pmtilesUrl": pmtiles_url,
        "pmtilesLayer": layer,
        "pmtilesFootprintLayer": FOOTPRINT_LAYER,
        "lazyImagery": bool(lazy_imagery),
    }
    config_json = json.dumps(config, separators=(",", ":")).replace("</", "<\\/")

    from ._lazy_imagery import driver_script  # noqa: PLC0415

    driver = (
        driver_script(
            percentile_low=percentile[0],
            percentile_high=percentile[1],
            engine="maplibre",
        )
        if lazy_imagery
        else ""
    )

    return _PAGE_TEMPLATE.format(
        title=escape(title),
        head_links=f'<link rel="stylesheet" href="{MAPLIBRE_CSS}"/>',
        script_links=(
            f'<script src="{MAPLIBRE_JS}"></script>\n<script src="{PMTILES_JS}"></script>'
        ),
        styles=_STYLES,
        config_json=config_json,
        app_js=_SHARED_JS + _PMTILES_APP_JS,
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
.umbra-thumb {
  display: block; width: 100%; margin-bottom: 8px; border: 1px solid #ddd;
  border-radius: 3px; background: #f2f2f2;
}
#umbra-detail table { border-collapse: collapse; width: 100%; }
#umbra-detail th { text-align: left; padding: 2px 8px 2px 0; color: #555; vertical-align: top; }
#umbra-detail td { padding: 2px 0; word-break: break-word; }
.umbra-sar-btn {
  font: 12px/1.2 -apple-system, sans-serif; margin-top: 8px; padding: 4px 10px;
  border: 1px solid #888; border-radius: 3px; background: #f7f7f7; cursor: pointer;
}
#umbra-analyze { border-top: 1px solid #ddd; padding-top: 12px; }
.umbra-analyze-btns { display: flex; flex-wrap: wrap; gap: 6px; }
.umbra-analyze-btns button {
  font: 12px/1.2 -apple-system, sans-serif; padding: 4px 10px; cursor: pointer;
  border: 1px solid #2b6cb0; border-radius: 3px; background: #2b6cb0; color: #fff;
}
.umbra-analyze-btns button:disabled { background: #9db8d4; border-color: #9db8d4; cursor: default; }
.umbra-analyze-status { font-size: 12px; color: #444; margin: 8px 0 0; min-height: 16px; }
#umbra-analyze-result img { max-width: 100%; margin-top: 8px; border: 1px solid #ddd; }
#umbra-analyze-result a { font-size: 12px; }
.umbra-stats { font-size: 12px; margin-top: 8px; }
.umbra-stats p { margin: 0 0 4px; }
.umbra-stats .headline { font-weight: 600; }
.umbra-stats .brighter { color: #b45309; }
.umbra-stats .dimmer { color: #2b6cb0; }
.umbra-stats pre {
  font: 11px/1.35 ui-monospace, Menlo, Consolas, monospace; margin: 6px 0 4px;
  padding: 6px; background: #f7f7f7; border: 1px solid #ddd; border-radius: 3px;
  overflow-x: auto;
}
.umbra-stats .note { color: #666; }
.umbra-stats .umbra-spark {
  display: block; width: 100%; height: 42px; margin: 2px 0 4px;
  background: #fbfbfb; border: 1px solid #eee; border-radius: 3px;
}
.umbra-stats .umbra-spark .spark-up { fill: #b45309; }
.umbra-stats .umbra-spark .spark-down { fill: #2b6cb0; }
.umbra-stats .umbra-spark .spark-axis { stroke: #bbb; stroke-width: 1; }
"""

# Pieces both explorer modes use verbatim: the detail-panel row builder, the
# server-backed thumbnail preview (G6), and the whole "Analyze this view" panel
# (R4). They are map-engine agnostic -- they touch only the DOM and the
# `umbra serve` HTTP contract -- so the Leaflet embedded-slice app and the
# MapLibre whole-archive app drive the identical code rather than each carrying
# its own copy that could drift. Like every script here it is a *static* string:
# each app hands it the values it needs at call time.
_SHARED_JS = """
// One metadata row in the detail panel. textContent never parses HTML, so
// remote strings need no escaping.
window.umbraRow = function (label, value) {
  var tr = document.createElement('tr');
  var th = document.createElement('th'); th.textContent = label;
  var td = document.createElement('td'); td.textContent = (value == null ? '\\u2014' : value);
  tr.appendChild(th); tr.appendChild(td);
  return tr;
};

// A row of toggle chips for one facet, wired to `onToggle(value, active)`.
// Both facets both apps offer -- product type and polarization -- read the same
// way: a chip starts active and a value is "on" unless explicitly toggled off,
// so an untouched sidebar hides nothing. Returns the container so a Reset can
// re-activate every chip in it.
window.umbraChipRow = function (containerId, values, onToggle) {
  var box = document.getElementById(containerId);
  if (!box) return [];
  (values || []).forEach(function (value) {
    var chip = document.createElement('span');
    chip.className = 'umbra-chip active';
    chip.textContent = value;
    chip.addEventListener('click', function () {
      onToggle(value, chip.classList.toggle('active'));
    });
    box.appendChild(chip);
  });
  return box;
};

// An instant SAR picture of the selected scene, or null when the page was
// built without a server. `umbra serve` serves the baked quicklook thumbnail
// (umbra index bake-thumbnails) straight from the index -- an offline file read
// -- and falls back to a quicklook render for a scene not yet baked. The id is
// remote metadata, so it is URL-encoded into the path (the scheme is our own
// trusted server base, so no javascript:-style breakout is possible); an
// unbaked/unrenderable scene 404s and the onerror handler drops the element
// rather than showing a broken image.
window.umbraThumb = function (base, id) {
  if (!base || !id) return null;
  var thumb = document.createElement('img');
  thumb.className = 'umbra-thumb';
  thumb.alt = 'SAR quicklook thumbnail';
  thumb.onerror = function () {
    if (thumb.parentNode) thumb.parentNode.removeChild(thumb);
  };
  thumb.src = base + '/artifacts/thumbnail/' + encodeURIComponent(id) + '.png';
  return thumb;
};

// The on-click "Get SAR image" button, or null when the page is metadata-only
// or the scene has no resolvable COG. Built with DOM APIs (setAttribute /
// textContent never parse HTML, so remote strings need no escaping) and wired
// to the shared driver's umbraToggleSarImage contract -- which the page ships
// in whichever placing build its map engine needs, so this builder is the same
// on both. `bounds` is the driver's "south,west,north,east" data-bounds string.
window.umbraSarButton = function (id, url, bounds) {
  if (!id || !url || !bounds || !window.umbraToggleSarImage) return null;
  var btn = document.createElement('button');
  btn.type = 'button'; btn.className = 'umbra-sar-btn';
  btn.setAttribute('data-item-id', id);
  btn.setAttribute('data-asset-url', url);
  btn.setAttribute('data-bounds', bounds);
  btn.setAttribute('data-state', 'idle');
  btn.textContent = 'Get SAR image';
  btn.onclick = function () { window.umbraToggleSarImage(btn); };
  return btn;
};

// The "Analyze this view" panel (R4): POST the currently-filtered acquisitions
// to a running `umbra serve` and show the artifact it renders. `collect()`
// returns the records currently in view as {id, when} objects; the panel does
// the chronological sort, the cap and the request. Returns a handle whose
// viewChanged() the caller invokes whenever the view changes.
//
// Three of the four products are pictures; "Quantify" is the numeric one
// (POST /artifacts/stats), so a spec says how to read its response: `html`
// opens a page in a tab, `json` renders the measurement, and the default paints
// the returned PNG.
window.umbraAnalyzePanel = function (base, collect) {
  var analyzeBox = document.getElementById('umbra-analyze');
  var statusEl = document.getElementById('umbra-analyze-status');
  var resultEl = document.getElementById('umbra-analyze-result');
  var buttons = [
    { el: document.getElementById('umbra-btn-change'), kind: 'change',
      label: 'change', verb: 'Rendering change', min: 2, html: false },
    { el: document.getElementById('umbra-btn-timescan'), kind: 'timescan',
      label: 'timescan', verb: 'Rendering timescan', min: 3, html: false },
    { el: document.getElementById('umbra-btn-swipe'), kind: 'swipe',
      label: 'swipe', verb: 'Rendering swipe', min: 2, html: true },
    { el: document.getElementById('umbra-btn-stats'), kind: 'stats',
      label: 'measurement', verb: 'Measuring change', min: 2, json: true,
      // A scene-wide mean dilutes a change that moved one corner hard, so the
      // page always asks for the spatial breakdown too -- it is what turns
      // "the site changed" into "the northeast corner brightened". And with
      // `block_series` each block carries its whole pass-to-pass sequence, which
      // is what the sparklines below plot: the peak block's own history is how a
      // steady drift is told apart from a single step.
      body: { blocks: 3, block_series: true } }
  ];
  analyzeBox.style.display = '';
  var CAP = 120;

  function analysisIds() {
    // Ids of the filtered acquisitions in chronological order (the server
    // resolves them and picks each product's frames). Sampled down to CAP so
    // the POST body -- and the server-side id scan -- stay bounded while
    // keeping the temporal span (first and last always survive).
    var fs = (collect() || []).filter(function (r) { return r && r.id && r.when; });
    fs.sort(function (a, b) { return a.when < b.when ? -1 : (a.when > b.when ? 1 : 0); });
    var ids = fs.map(function (r) { return r.id; });
    if (ids.length <= CAP) return ids;
    var picked = [];
    for (var i = 0; i < CAP; i++) {
      picked.push(ids[Math.round(i * (ids.length - 1) / (CAP - 1))]);
    }
    // Adjacent samples can collapse to the same id; de-dup, order preserved.
    return picked.filter(function (v, i, a) { return a.indexOf(v) === i; });
  }

  function setBusy(busy) {
    buttons.forEach(function (b) { b.el.disabled = busy; });
  }

  function viewChanged() {
    var n = analysisIds().length;
    buttons.forEach(function (b) { b.el.disabled = n < b.min; });
  }

  // Every number below is one the server measured: the panel formats, it never
  // computes, so what the page says and what `stack_stats` says cannot drift.
  function signedDb(v) { return (v > 0 ? '+' : '') + v + ' dB'; }
  function percent(f) {
    return f == null ? '\\u2014' : (Math.round(f * 1000) / 10) + '%';
  }
  function day(stamp) { return String(stamp || '').slice(0, 10); }

  function statsLine(box, text, cls) {
    var p = document.createElement('p');
    p.textContent = text;
    if (cls) p.className = cls;
    box.appendChild(p);
    return p;
  }

  var SVG_NS = 'http://www.w3.org/2000/svg';
  var SPARK_W = 240, SPARK_H = 42;

  // One bar per consecutive pass-to-pass step, signed and zero-baselined. A net
  // figure and a peak interval cannot separate two genuinely different
  // histories -- a corner drifting a decibel every pass and one that jumped
  // twelve once and held read the same in both -- and the sequence that tells
  // them apart is exactly what `block_series` (and, scene-wide, each pass's
  // `change_vs_previous`) puts in the document. Every decibel drawn is the
  // server's; the only arithmetic here is the pixel scale.
  function sparkline(steps) {
    var n = steps.length;
    if (!n) return null;
    var scale = 0, i;
    for (i = 0; i < n; i++) scale = Math.max(scale, Math.abs(steps[i].mean_delta_db));
    var svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('class', 'umbra-spark');
    svg.setAttribute('viewBox', '0 0 ' + SPARK_W + ' ' + SPARK_H);
    svg.setAttribute('preserveAspectRatio', 'none');
    svg.setAttribute('role', 'img');
    var mid = SPARK_H / 2;
    var slot = SPARK_W / n;
    var width = Math.max(1, slot - (n > 1 ? 2 : 0));
    for (i = 0; i < n; i++) {
      var v = steps[i].mean_delta_db;
      // A bar that rounds to nothing would read as "no data" rather than "no
      // change", so every observed step keeps a visible sliver.
      var h = Math.max(scale ? Math.abs(v) / scale * (mid - 1) : 0, 0.75);
      var bar = document.createElementNS(SVG_NS, 'rect');
      bar.setAttribute('x', String(i * slot + (slot - width) / 2));
      bar.setAttribute('y', String(v >= 0 ? mid - h : mid));
      bar.setAttribute('width', String(width));
      bar.setAttribute('height', String(h));
      bar.setAttribute('class', v >= 0 ? 'spark-up' : 'spark-down');
      var tip = document.createElementNS(SVG_NS, 'title');
      tip.textContent = day(steps[i].from_datetime) + ' \\u2192 ' +
        day(steps[i].to_datetime) + ': ' + signedDb(v);
      bar.appendChild(tip);
      svg.appendChild(bar);
    }
    var axis = document.createElementNS(SVG_NS, 'line');
    axis.setAttribute('x1', '0'); axis.setAttribute('x2', String(SPARK_W));
    axis.setAttribute('y1', String(mid)); axis.setAttribute('y2', String(mid));
    axis.setAttribute('class', 'spark-axis');
    svg.appendChild(axis);
    return svg;
  }

  // The sparkline plus the caption that makes it readable: bars with no stated
  // scale are decoration. The largest step is picked to scale the drawing (and
  // named so the reader knows what full height means) -- the decibel figure
  // printed is the server's own, not a recomputation.
  function sparkSection(box, steps, lead) {
    var svg = sparkline(steps);
    if (!svg) return;
    var top = steps[0];
    for (var i = 1; i < steps.length; i++) {
      if (Math.abs(steps[i].mean_delta_db) > Math.abs(top.mean_delta_db)) top = steps[i];
    }
    var span = day(top.from_datetime) + ' \\u2192 ' + day(top.to_datetime);
    svg.setAttribute('aria-label', lead + ': ' + steps.length +
      ' pass-to-pass steps, largest ' + signedDb(top.mean_delta_db) + ' (' + span + ').');
    box.appendChild(svg);
    statsLine(box, lead + ', oldest first \\u2014 ' + steps.length + ' steps, ' +
      'each bar scaled to the largest, ' + signedDb(top.mean_delta_db) + ' (' + span +
      '). Up is brighter.', 'note');
  }

  // Each pass's change against the one before it -- the scene-wide sibling of a
  // block's `series`, and until now the part of the document the readout
  // fetched and never showed. Reshaped to the block series' fields so one
  // sparkline builder draws both; a pass with nothing to compare against (the
  // first, or ground the previous pass never saw) has no step.
  function sceneSteps(passes) {
    var steps = [];
    for (var i = 1; i < passes.length; i++) {
      var ch = passes[i].change_vs_previous;
      if (!ch) continue;
      steps.push({
        from_datetime: passes[i - 1].datetime,
        to_datetime: passes[i].datetime,
        mean_delta_db: ch.mean_delta_db
      });
    }
    return steps;
  }

  function blockAt(spatial, row, col) {
    var blocks = (spatial && spatial.blocks) || [];
    for (var i = 0; i < blocks.length; i++) {
      if (blocks[i].row === row && blocks[i].col === col) return blocks[i];
    }
    return null;
  }

  // The measurement, read out of the /artifacts/stats document: how much the
  // site moved first-to-last, where it moved most, and the north-up heat-grid
  // of signed change. Built with textContent, so remote strings never parse as
  // HTML; the CC-BY attribution the document carries rides along with it.
  function renderStats(doc, resultEl) {
    var box = document.createElement('div');
    box.className = 'umbra-stats';
    var passes = doc.passes || [];
    var span = passes.length
      ? ' (' + day(passes[0].datetime) + ' \\u2192 ' +
        day(passes[passes.length - 1].datetime) + ')'
      : '';
    var net = doc.net_change;
    if (!net) {
      statsLine(box, 'No ground was observed on both the first and last pass, ' +
        'so there is nothing to compare. Filter to passes over one site.', 'headline');
    } else {
      var dir = net.mean_delta_db >= 0 ? 'brighter' : 'dimmer';
      statsLine(box, signedDb(net.mean_delta_db) + ' mean across ' + passes.length +
        ' passes' + span + ' \\u2014 ' + dir + '.', 'headline ' + dir);
      var moved = percent(net.changed_fraction) + ' of the site moved \\u2265' +
        doc.change_threshold_db + ' dB';
      statsLine(box, net.changed_area_km2 == null
        ? moved + ' (no area \\u2014 the measurement grid is geographic).'
        : moved + ' (' + net.changed_area_km2 + ' km\\u00b2).');
    }
    // First-to-last is one number for a whole series; this is the series.
    sparkSection(box, sceneSteps(passes), 'Site mean, pass to pass');
    var spatial = doc.spatial;
    if (spatial && spatial.peak_block) {
      var pb = spatial.peak_block;
      var when = pb.peak_interval
        ? ', mostly between ' + day(pb.peak_interval.from_datetime) +
          ' and ' + day(pb.peak_interval.to_datetime)
        : '';
      statsLine(box, 'Moved most in the ' + pb.compass + ': ' +
        signedDb(pb.mean_delta_db) + ' (' + pb.direction + ')' + when + '.');
      // And that block's own history, which is what says whether "mostly
      // between" was the only thing that happened there or the loudest of many.
      var pbRecord = blockAt(spatial, pb.row, pb.col);
      if (pbRecord && pbRecord.series) {
        sparkSection(box, pbRecord.series, 'The ' + pb.compass + ' block, pass to pass');
      }
    }
    if (spatial && spatial.grid_text) {
      var pre = document.createElement('pre');
      pre.textContent = spatial.grid_text;
      box.appendChild(pre);
      statsLine(box, 'Signed dB change per block, north-up (' + spatial.grid_rows +
        '\\u00d7' + spatial.grid_cols + '); "." was never observed on both passes.',
        'note');
    }
    if (doc.caveats && doc.caveats.length) statsLine(box, doc.caveats[0], 'note');
    if (doc.attribution) statsLine(box, doc.attribution, 'note');
    resultEl.appendChild(box);
  }

  function runAnalysis(spec) {
    var ids = analysisIds();
    if (ids.length < spec.min) {
      statusEl.textContent = 'Need at least ' + spec.min +
        ' filtered acquisitions for ' + spec.label + ' (have ' + ids.length + ').';
      return;
    }
    setBusy(true);
    statusEl.textContent = spec.verb + ' over ' +
      ids.length + ' acquisitions\\u2026';
    resultEl.innerHTML = '';
    var payload = { ids: ids };
    if (spec.body) {
      for (var k in spec.body) {
        if (Object.prototype.hasOwnProperty.call(spec.body, k)) payload[k] = spec.body[k];
      }
    }
    fetch(base + '/artifacts/' + spec.kind, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    }).then(function (r) {
      if (!r.ok) {
        return r.json().then(function (e) {
          throw new Error((e && e.detail) || ('HTTP ' + r.status));
        }, function () { throw new Error('HTTP ' + r.status); });
      }
      return spec.json ? r.json() : r.blob();
    }).then(function (data) {
      statusEl.textContent = spec.label + ' ready.';
      if (spec.json) {
        renderStats(data, resultEl);
        return;
      }
      var url = URL.createObjectURL(data);
      if (spec.html) {
        // Swipe is a full interactive HTML page: open it in a new tab and
        // leave a link behind (popup blockers may swallow the auto-open).
        var a = document.createElement('a');
        a.href = url; a.target = '_blank'; a.rel = 'noopener';
        a.textContent = 'open ' + spec.kind + ' map \\u2197';
        resultEl.appendChild(a);
        window.open(url, '_blank', 'noopener');
      } else {
        var img = document.createElement('img');
        img.src = url; img.alt = spec.kind + ' composite over the filtered view';
        resultEl.appendChild(img);
      }
    }).catch(function (err) {
      statusEl.textContent = 'Failed: ' + (err && err.message ? err.message : err);
    }).then(function () { setBusy(false); viewChanged(); });
  }

  buttons.forEach(function (b) {
    b.el.addEventListener('click', function () { runAnalysis(b); });
  });
  return { viewChanged: viewChanged };
};
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
  // Base URL of a running `umbra serve` instance, if the page was built with
  // one. It backs both the instant thumbnail preview in the detail panel
  // (below) and the "Analyze this view" panel (further down). Null keeps the
  // page fully static.
  var serverBase = CFG.serverUrl ? String(CFG.serverUrl).replace(/\\/+$/, '') : null;
  // The features currently passing the filters -- the "view" the server-backed
  // analysis buttons act on. Kept in sync by render().
  var shownFeatures = [];

  // --- filter state ---
  var state = {
    text: '', start: CFG.dateMin || '', end: CFG.dateMax || '', products: {}, pols: {}
  };
  (CFG.products || []).forEach(function (p) { state.products[p] = true; });
  (CFG.polarizations || []).forEach(function (p) { state.pols[p] = true; });

  function passesFilter(props) {
    if (state.products && Object.keys(state.products).length) {
      // A product is "on" unless explicitly toggled off.
      if (props.product && state.products[props.product] === false) return false;
    }
    // Same "on unless toggled off" rule, but a scene can carry several
    // polarizations, so it survives if *any* of them is still on -- and a scene
    // with none listed is never filtered out by a facet it has no value for.
    var pols = props.polarizations || [];
    if (pols.length) {
      var keep = false;
      for (var pi = 0; pi < pols.length; pi++) {
        if (state.pols[pols[pi]] !== false) { keep = true; break; }
      }
      if (!keep) return false;
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
    shownFeatures = [];
    var markers = [];
    for (var i = 0; i < features.length; i++) {
      var f = features[i];
      if (!passesFilter(f.properties)) continue;
      shownFeatures.push(f);
      var m = markerFor(f);
      if (m) { markers.push(m); }
    }
    cluster.addLayers(markers);
    countEl.textContent = shownFeatures.length + ' of ' + features.length + ' acquisitions shown';
    if (typeof onViewChanged === 'function') onViewChanged();
  }

  // --- detail panel ---
  function showDetail(feature) {
    var p = feature.properties;
    var panel = document.getElementById('umbra-detail');
    panel.innerHTML = '';

    // Lead with an instant SAR picture of the selected scene when the page was
    // built with a server_url; without one the panel stays metadata-only and
    // the page is fully static.
    var thumb = window.umbraThumb(serverBase, p.id);
    if (thumb) panel.appendChild(thumb);

    var row = window.umbraRow;
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

    // On-click SAR overlay button (the shared builder; this side already has
    // the resolved COG URL and bbox embedded in the feature).
    if (CFG.lazyImagery && p.lazy_url && p.lazy_bounds) {
      var btn = window.umbraSarButton(p.id, p.lazy_url, p.lazy_bounds.join(','));
      if (btn) panel.appendChild(btn);
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

  var chipBox = window.umbraChipRow('umbra-products', CFG.products, function (prod, on) {
    state.products[prod] = on; render();
  });
  var polBox = window.umbraChipRow('umbra-polarizations', CFG.polarizations, function (pol, on) {
    state.pols[pol] = on; render();
  });

  document.getElementById('umbra-reset').addEventListener('click', function () {
    state.text = ''; textInput.value = '';
    state.start = CFG.dateMin || ''; startInput.value = CFG.dateMin || '';
    state.end = CFG.dateMax || ''; endInput.value = CFG.dateMax || '';
    (CFG.products || []).forEach(function (p) { state.products[p] = true; });
    (CFG.polarizations || []).forEach(function (p) { state.pols[p] = true; });
    [chipBox, polBox].forEach(function (box) {
      Array.prototype.forEach.call(box.children || [], function (c) { c.classList.add('active'); });
    });
    render();
  });

  // --- server-backed analysis (R4): POST the filtered view to `umbra serve` ---
  // Only wired when the page was built with a server_url; otherwise the panel
  // stays hidden and the page is fully static. The panel itself is shared with
  // the whole-archive PMTiles app; this side only supplies the records in view.
  var onViewChanged = null;
  if (serverBase) {
    var analyze = window.umbraAnalyzePanel(serverBase, function () {
      return shownFeatures.map(function (f) {
        return { id: f.properties.id, when: f.properties.datetime || f.properties.date };
      });
    });
    onViewChanged = analyze.viewChanged;
  }

  render();
  // Frame the full set on first load.
  var pts = features.map(function (f) { return f.properties.centroid; }).filter(Boolean);
  if (pts.length) { map.fitBounds(pts, { padding: [40, 40], maxZoom: 12 }); }
})();
"""

# The whole-archive application. Same sidebar, same detail panel, same
# server-backed analysis -- a MapLibre GL vector layer over the tiled catalog in
# place of the embedded-slice Leaflet cluster. The filters become MapLibre
# expressions evaluated inside the tiles, so filtering scales with the archive
# rather than with what fits in the page. Static string, like _APP_JS: the
# archive URL, layer name and server base arrive through window.UMBRA_DEMO.
_PMTILES_APP_JS = """
(function () {
  var CFG = window.UMBRA_DEMO || {};
  var LAYER = CFG.pmtilesLayer || 'acquisitions';
  var FOOTPRINT_LAYER = CFG.pmtilesFootprintLayer || 'footprints';
  var LAYER_ID = 'umbra-acq';
  var FILL_ID = 'umbra-footprint-fill';
  var OUTLINE_ID = 'umbra-footprint-line';

  var protocol = new pmtiles.Protocol();
  maplibregl.addProtocol('pmtiles', protocol.tile);

  var map = new maplibregl.Map({
    container: 'umbra-map',
    style: {
      version: 8,
      sources: {
        osm: {
          type: 'raster',
          tiles: ['https://a.tile.openstreetmap.org/{z}/{x}/{y}.png'],
          tileSize: 256,
          attribution: '&copy; OpenStreetMap contributors'
        },
        umbra: { type: 'vector', url: 'pmtiles://' + CFG.pmtilesUrl }
      },
      layers: [
        { id: 'osm', type: 'raster', source: 'osm' },
        // Coverage shape from the archive's footprint polygons (written at the
        // deeper zooms). A centroids-only archive has no features here, so these
        // draw nothing and the page is exactly the circles-only one it was.
        {
          id: FILL_ID,
          type: 'fill',
          source: 'umbra',
          'source-layer': FOOTPRINT_LAYER,
          paint: { 'fill-color': '#e6194b', 'fill-opacity': 0.15 }
        },
        {
          id: OUTLINE_ID,
          type: 'line',
          source: 'umbra',
          'source-layer': FOOTPRINT_LAYER,
          paint: { 'line-color': '#e6194b', 'line-width': 1.2, 'line-opacity': 0.9 }
        },
        {
          id: LAYER_ID,
          type: 'circle',
          source: 'umbra',
          'source-layer': LAYER,
          paint: {
            'circle-radius': ['interpolate', ['linear'], ['zoom'], 2, 2.5, 8, 5, 12, 7],
            'circle-color': '#e6194b',
            'circle-opacity': 0.75,
            'circle-stroke-width': 1,
            'circle-stroke-color': '#ffffff'
          }
        }
      ]
    },
    center: [0, 20],
    zoom: 1.4
  });
  map.addControl(new maplibregl.NavigationControl(), 'top-left');
  map.addControl(new maplibregl.AttributionControl({ customAttribution: CFG.attribution }));
  // Publish the map for the shared lazy-imagery driver, which resolves its
  // target by walking the DOM for a Folium map and then falling back here. The
  // MapLibre-placing build of that driver is what this page ships. The driver
  // slots each SAR overlay *under* this layer, so the footprints and markers
  // that opened it stay drawn and clickable on top of the imagery.
  window.umbraLazyMap = map;
  window.umbraOverlayBeforeId = FILL_ID;

  // Rebuild the acquisition's COG URL from what the tiles carry. `cog` is
  // normally the bare filename of a product sitting next to the item's STAC
  // sidecar in the public bucket (kept lean: the URL prefix would otherwise be
  // repeated in every tile at every zoom), so it is resolved against
  // `stac_href`; an already-absolute reference is used as-is. Only http(s)
  // survives -- these strings come from remote metadata, and the driver hands
  // whatever it gets to fetch().
  function cogUrl(p) {
    if (!p || !p.cog) return null;
    var cog = String(p.cog);
    if (/^https?:\\/\\//.test(cog)) return cog;
    var href = String(p.stac_href || '');
    if (!/^https?:\\/\\//.test(href) || cog.indexOf('/') !== -1) return null;
    return href.replace(/[^/]*$/, '') + cog;
  }

  var serverBase = CFG.serverUrl ? String(CFG.serverUrl).replace(/\\/+$/, '') : null;
  // The acquisitions currently drawn -- the "view" the analysis buttons act on.
  var shownFeatures = [];
  var state = { text: '', start: '', end: '', products: {}, pols: {} };
  (CFG.products || []).forEach(function (p) { state.products[p] = true; });
  (CFG.polarizations || []).forEach(function (p) { state.pols[p] = true; });

  // --- filters, as MapLibre expressions evaluated inside the vector tiles ---
  // The equivalent of the embedded-slice app's passesFilter(), except the
  // renderer applies it to the whole archive without the page ever holding it.
  function filterExpression() {
    var clauses = ['all'];
    (CFG.products || []).forEach(function (p) {
      // A product is "on" unless explicitly toggled off (matching the slice
      // app), so only the off ones become exclusions.
      if (state.products[p] === false) clauses.push(['!=', ['get', 'product'], p]);
    });
    // Polarization is the one facet a tiled value holds as a *list* -- comma
    // joined, since a tile property is a scalar -- so "keep the scenes still
    // offering an on polarization" is an `any` over substring tests rather than
    // an equality. No two-letter code can match across the comma, so index-of is
    // exact. The clause is only added once something is off (an all-on facet
    // filters nothing), and a scene with no `pol` at all stays visible, the same
    // rule the missing-date guard applies.
    var onPols = (CFG.polarizations || []).filter(function (p) {
      return state.pols[p] !== false;
    });
    if (onPols.length !== (CFG.polarizations || []).length) {
      var any = ['any', ['!', ['has', 'pol']]];
      onPols.forEach(function (p) {
        any.push(['>=', ['index-of', p, ['get', 'pol']], 0]);
      });
      clauses.push(any);
    }
    // `any` short-circuits, so the guard both keeps a date-less feature out of a
    // string comparison against null *and* keeps it visible -- the same "a
    // missing date never fails a date filter" rule passesFilter() applies in the
    // embedded-slice app.
    if (state.start) {
      clauses.push(['any', ['!', ['has', 'date']], ['>=', ['get', 'date'], state.start]]);
    }
    if (state.end) {
      clauses.push(['any', ['!', ['has', 'date']], ['<=', ['get', 'date'], state.end]]);
    }
    if (state.text) {
      clauses.push(['>=', ['index-of', state.text, ['downcase', ['concat',
        ['coalesce', ['get', 'place'], ''], ' ',
        ['coalesce', ['get', 'id'], '']]]], 0]);
    }
    return clauses.length === 1 ? null : clauses;
  }

  var countEl = document.getElementById('umbra-count');

  function applyFilter() {
    if (!map.getLayer(LAYER_ID)) return;
    var expr = filterExpression();
    // Every layer reads the same properties, so one expression filters the
    // markers and their outlines together -- a hidden scene must not leave its
    // footprint drawn.
    [LAYER_ID, FILL_ID, OUTLINE_ID].forEach(function (id) {
      if (map.getLayer(id)) map.setFilter(id, expr);
    });
    // The filter takes effect on the next frame, so read the view after it.
    map.once('render', scheduleRefresh);
  }

  // Recomputing the view is a query over everything drawn, so coalesce the
  // bursts of events a pan or a filter change produces into one pass.
  var refreshTimer = null;
  function scheduleRefresh() {
    if (refreshTimer) return;
    refreshTimer = setTimeout(function () { refreshTimer = null; refreshView(); }, 250);
  }

  // What is actually on screen after the renderer has applied the filter. There
  // is no whole-archive count to show -- the page never holds the archive --
  // so the honest number is "in view", and panning or zooming loads more.
  function refreshView() {
    if (!map.getLayer(LAYER_ID)) return;
    var seen = {};
    var out = [];
    var rendered = map.queryRenderedFeatures({ layers: [LAYER_ID] });
    for (var i = 0; i < rendered.length; i++) {
      var p = rendered[i].properties || {};
      if (!p.id || seen[p.id]) continue;
      seen[p.id] = true;
      out.push(p);
    }
    shownFeatures = out;
    countEl.textContent = out.length + ' acquisition' + (out.length === 1 ? '' : 's') +
      ' in view \\u2014 pan or zoom to load more of the archive';
    if (typeof onViewChanged === 'function') onViewChanged();
  }

  // --- detail panel ---
  // Vector tiles carry the acquisition's geometry and its lean metadata, which
  // is now every field the slice app's panel shows. The two list-valued ones
  // arrive comma-joined (a tile property is a scalar), so they are re-spaced for
  // reading rather than reformatted.
  function commas(value) {
    return value ? String(value).split(',').join(', ') : null;
  }

  function showDetail(p) {
    var panel = document.getElementById('umbra-detail');
    panel.innerHTML = '';

    var thumb = window.umbraThumb(serverBase, p.id);
    if (thumb) panel.appendChild(thumb);

    var row = window.umbraRow;
    var table = document.createElement('table');
    table.appendChild(row('ID', p.id));
    table.appendChild(row('Place', p.place));
    table.appendChild(row('Acquired', p.date));
    table.appendChild(row('Platform', p.platform));
    table.appendChild(row('Product', p.product));
    table.appendChild(row('Polarizations', commas(p.pol)));
    table.appendChild(row('Assets', commas(p.assets)));
    panel.appendChild(table);

    // The STAC href comes from remote metadata; only http(s) may reach an
    // anchor (a javascript: scheme would be a clickable script link).
    if (p.stac_href && /^https?:\\/\\//.test(String(p.stac_href))) {
      var a = document.createElement('a');
      a.href = p.stac_href; a.target = '_blank'; a.rel = 'noopener';
      a.textContent = 'open STAC item';
      var pw = document.createElement('p'); pw.style.marginTop = '8px';
      pw.appendChild(a); panel.appendChild(pw);
    }

    // On-click SAR overlay button: the same shared builder and the same
    // geotiff.js driver the slice app uses, over the COG the tiles reference.
    if (CFG.lazyImagery) {
      var btn = window.umbraSarButton(p.id, cogUrl(p), p.bounds);
      if (btn) panel.appendChild(btn);
    }
  }

  // A centroid and its footprint carry the same properties, so clicking either
  // opens the same detail -- and zoomed in, the polygon is the easier target.
  [LAYER_ID, FILL_ID].forEach(function (id) {
    map.on('click', id, function (e) {
      var f = e.features && e.features[0];
      if (f) showDetail(f.properties || {});
    });
    map.on('mouseenter', id, function () { map.getCanvas().style.cursor = 'pointer'; });
    map.on('mouseleave', id, function () { map.getCanvas().style.cursor = ''; });
  });

  // --- wire controls (the same sidebar ids the slice app uses) ---
  var textInput = document.getElementById('umbra-text');
  textInput.addEventListener('input', function () {
    state.text = textInput.value.trim().toLowerCase(); applyFilter();
  });
  var startInput = document.getElementById('umbra-start');
  var endInput = document.getElementById('umbra-end');
  startInput.addEventListener('change', function () {
    state.start = startInput.value; applyFilter();
  });
  endInput.addEventListener('change', function () {
    state.end = endInput.value; applyFilter();
  });

  var chipBox = window.umbraChipRow('umbra-products', CFG.products, function (prod, on) {
    state.products[prod] = on; applyFilter();
  });
  var polBox = window.umbraChipRow('umbra-polarizations', CFG.polarizations, function (pol, on) {
    state.pols[pol] = on; applyFilter();
  });

  document.getElementById('umbra-reset').addEventListener('click', function () {
    state.text = ''; textInput.value = '';
    state.start = ''; startInput.value = '';
    state.end = ''; endInput.value = '';
    (CFG.products || []).forEach(function (p) { state.products[p] = true; });
    (CFG.polarizations || []).forEach(function (p) { state.pols[p] = true; });
    [chipBox, polBox].forEach(function (box) {
      Array.prototype.forEach.call(box.children || [], function (c) { c.classList.add('active'); });
    });
    applyFilter();
  });

  // --- server-backed analysis (R4), the same shared panel the slice app uses ---
  var onViewChanged = null;
  if (serverBase) {
    var analyze = window.umbraAnalyzePanel(serverBase, function () {
      return shownFeatures.map(function (p) { return { id: p.id, when: p.date }; });
    });
    onViewChanged = analyze.viewChanged;
  }

  map.on('load', applyFilter);
  // `idle` means "everything has finished loading and rendering" and is the
  // natural moment to read the view -- but a basemap that never settles (a
  // blocked or flaky tile host) would starve it, leaving the count blank while
  // the acquisitions themselves draw fine. `moveend` and `sourcedata` cover
  // that: between them every pan, zoom and tile arrival is accounted for, and
  // the debounce keeps the overlap to one pass.
  map.on('idle', scheduleRefresh);
  map.on('moveend', scheduleRefresh);
  map.on('sourcedata', scheduleRefresh);
})();
"""

_PAGE_TEMPLATE = """<!DOCTYPE html>
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
    <div class="umbra-filter">
      <label title="HH and VV image different scattering">Polarization</label>
      <div class="umbra-chips" id="umbra-polarizations"></div>
    </div>
    <div id="umbra-count"></div>
    <button id="umbra-reset" type="button">Reset filters</button>
    <div id="umbra-analyze" style="display:none">
      <label>Analyze this view</label>
      <div class="umbra-analyze-btns">
        <button id="umbra-btn-change" type="button">Change</button>
        <button id="umbra-btn-timescan" type="button">Timescan</button>
        <button id="umbra-btn-swipe" type="button">Swipe</button>
        <button id="umbra-btn-stats" type="button"
                title="Measure the change in numbers">Quantify</button>
      </div>
      <p id="umbra-analyze-status" class="umbra-analyze-status"></p>
      <div id="umbra-analyze-result"></div>
    </div>
    <div id="umbra-detail"><p class="empty">Click a marker to see its metadata.</p></div>
  </aside>
  <div id="umbra-map"></div>
</div>
{script_links}
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
