"""Interactive Folium maps: footprints, timelines and before/after swipes.

The map half of ``viz`` (requires the ``viz`` extra). Search results become
drop-in HTML for a notebook or a link: one polygon per acquisition with a
metadata popup, an optional streamed SAR overlay (eagerly composited, or
fetched lazily in the browser), a timeline view of one site's passes, and the
draggable before/after swipe -- the interactive cousin of a change composite.
Every map carries the CC-BY data credit in Leaflet's attribution control.

Reverse geocoding of footprint centroids lives here too, rate-limited and
cached to stay inside Nominatim's usage policy; a baked ``UmbraItem.place``
label is always preferred over a live lookup.
"""

from __future__ import annotations

import html
import json
import os
import warnings
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ..constants import ATTRIBUTION
from ..exceptions import AssetNotFoundError
from ..models import UmbraItem
from ._deps import _require
from .composites import _coregister_bands
from .geojson import _centroid, _geometry_for, _union_bbox, item_to_feature
from .raster import _rgba_overlay, _stretch_to_rgba, image_overlay


def _popup_html(
    item: UmbraItem,
    *,
    location: str | None = None,
    lazy_imagery_url: str | None = None,
    lazy_imagery_bounds: tuple[float, float, float, float] | None = None,
) -> str:
    info = item.metadata_summary()
    rng, azi = info["resolution_range_m"], info["resolution_azimuth_m"]

    def fmt(v: Any, suffix: str = "") -> str:
        # These values originate from remote STAC JSON, so a string value is
        # HTML-escaped before it reaches the popup. The ``&mdash;``/``&deg;``/
        # unit literals are code-controlled and intentionally left as markup.
        if v is None:
            return "&mdash;"
        if isinstance(v, float):
            return f"{v:.2f}{suffix}"
        return f"{html.escape(str(v))}{suffix}"

    rows = [
        ("ID", html.escape(str(info["id"]))),
        ("Acquired", html.escape(info["datetime"]) if info["datetime"] else "&mdash;"),
        ("Platform", fmt(info["platform"])),
        ("Mode", fmt(info["instrument_mode"])),
        ("Product", fmt(info["product_type"])),
        ("Polarizations", html.escape(", ".join(info["polarizations"])) or "&mdash;"),
        ("Incidence", fmt(info["incidence_angle_deg"], "&deg;")),
        ("Resolution (rng × azi)", f"{fmt(rng, ' m')} × {fmt(azi, ' m')}"),
        ("Assets", html.escape(", ".join(info["available_assets"])) or "&mdash;"),
    ]
    if location:
        # Slot "Location" right under the acquisition time so the popup
        # reads "what / when / where" before drilling into instrument
        # detail.
        rows.insert(2, ("Location", html.escape(location)))
    body = "".join(
        f"<tr><th style='text-align:left;padding-right:8px'>{k}</th><td>{v}</td></tr>"
        for k, v in rows
    )
    desc = item.description
    desc_html = f"<p style='margin:6px 0 0;max-width:380px'>{html.escape(desc)}</p>" if desc else ""
    from .._html import safe_href  # noqa: PLC0415

    href = safe_href(item.href)
    link = (
        f"<p style='margin-top:6px'><a href='{href}' target='_blank' "
        "rel='noopener'>open STAC item</a></p>"
        if href
        else ""
    )
    button = ""
    if lazy_imagery_url and lazy_imagery_bounds is not None:
        from .._lazy_imagery import popup_button_html  # noqa: PLC0415

        button = popup_button_html(
            item_id=item.id,
            asset_url=lazy_imagery_url,
            bounds=lazy_imagery_bounds,
        )
    return (
        f"<table style='font-family:sans-serif;font-size:12px'>{body}</table>"
        f"{desc_html}{button}{link}"
    )


# OpenStreetMap's Nominatim service is the canonical free reverse-geocoder.
# Its usage policy caps absolute traffic at one request per second and
# requires a descriptive User-Agent. Both are honored below.
_NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"


_GEOCODE_MIN_INTERVAL = 1.05  # seconds; small margin over Nominatim's 1 req/s


_GEOCODE_CACHE: dict[tuple[int, int, int], str | None] = {}


_LAST_GEOCODE_AT = 0.0


def _require_session_for_geocoding() -> Any:
    """Build the shared HTTP session used for a batch of geocode calls.

    Split into its own helper so tests can patch out the session creation
    without monkey-patching ``_http``.
    """
    from .._http import default_session  # noqa: PLC0415

    return default_session()


def _reverse_geocode(
    lat: float,
    lon: float,
    *,
    zoom: int = 10,
    session: Any = None,
    timeout: float = 10.0,
) -> str | None:
    """Resolve ``(lat, lon)`` to a human-readable place name.

    Calls OpenStreetMap's Nominatim reverse-geocoding endpoint and returns
    the ``display_name`` (e.g. ``"Reykjavík, Iceland"``) or ``None`` if
    the service is unreachable, returns malformed JSON, or has no record
    for the coordinate. Failures never raise — the label is decorative
    and missing it should not break a map render.

    Results are cached in-process at ~1 km granularity, and the function
    self-throttles to ≤1 request per second to comply with Nominatim's
    usage policy. ``zoom`` controls the address granularity: 3 = country,
    8 = county, 10 = city, 14 = suburb, 18 = building.
    """
    requests = _require("requests")
    # ~1 km at the equator; nearby revisits collapse into one HTTP call.
    cache_key = (round(lat * 100), round(lon * 100), zoom)
    if cache_key in _GEOCODE_CACHE:
        return _GEOCODE_CACHE[cache_key]

    global _LAST_GEOCODE_AT
    import time  # noqa: PLC0415

    elapsed = time.monotonic() - _LAST_GEOCODE_AT
    if elapsed < _GEOCODE_MIN_INTERVAL:
        time.sleep(_GEOCODE_MIN_INTERVAL - elapsed)

    if session is None:
        from .._http import default_session  # noqa: PLC0415

        session = default_session()

    label: str | None = None
    try:
        resp = session.get(
            _NOMINATIM_REVERSE_URL,
            params={
                "lat": f"{lat:.6f}",
                "lon": f"{lon:.6f}",
                "format": "jsonv2",
                "zoom": zoom,
                "addressdetails": 0,
            },
            timeout=timeout,
            headers={"Accept-Language": "en"},
        )
        resp.raise_for_status()
        payload = resp.json()
    except (requests.RequestException, ValueError):
        # Network hiccup, HTTP error, or non-JSON body -- leave label None
        # and cache the miss so we don't hammer Nominatim on every retry.
        payload = None
    finally:
        _LAST_GEOCODE_AT = time.monotonic()

    if isinstance(payload, dict):
        raw = payload.get("display_name") or payload.get("name")
        if isinstance(raw, str) and raw.strip():
            label = raw.strip()
    _GEOCODE_CACHE[cache_key] = label
    return label


def _legend_html(total: int, with_imagery: int | None, color: str) -> str:
    """Small fixed-position legend pinned to the top-right of the map."""
    if with_imagery is None:
        body = (
            f"<div style='display:flex;align-items:center;gap:6px'>"
            f"<span style='display:inline-block;width:10px;height:10px;"
            f"border-radius:50%;border:2px solid {color};background:white'></span>"
            f"<span>{total} footprint{'s' if total != 1 else ''}</span>"
            f"</div>"
        )
    else:
        without = total - with_imagery
        body = (
            f"<div style='display:flex;align-items:center;gap:6px;margin-bottom:3px'>"
            f"<span style='display:inline-block;width:10px;height:10px;"
            f"border-radius:50%;background:{color};border:2px solid {color}'></span>"
            f"<span>{with_imagery} with SAR imagery</span></div>"
            f"<div style='display:flex;align-items:center;gap:6px'>"
            f"<span style='display:inline-block;width:10px;height:10px;"
            f"border-radius:50%;border:2px solid {color};background:white'></span>"
            f"<span>{without} footprint only</span></div>"
        )
    return (
        "<div style='position:fixed;top:12px;right:12px;z-index:1000;"
        "background:rgba(255,255,255,0.95);padding:8px 12px;border:1px solid #ccc;"
        "border-radius:4px;font:12px/1.4 -apple-system,sans-serif;"
        "box-shadow:0 1px 3px rgba(0,0,0,0.2)'>"
        f"<div style='font-weight:600;margin-bottom:5px'>Umbra footprints</div>{body}</div>"
    )


def _resolve_lazy_urls(
    items: Iterable[UmbraItem],
    enabled: bool,
    asset: str,
) -> dict[str, tuple[str, tuple[float, float, float, float]]]:
    """Return ``{item_id: (cog_url, bbox)}`` for lazily-fetchable items.

    ``bbox`` is the item's lat/lon footprint, used to place the overlay
    in the browser. When ``enabled`` is False we short-circuit to an
    empty dict so the caller doesn't have to repeat that check. Items
    are silently dropped (popup still renders, just without the button)
    when they lack a bbox to place the overlay, or when ``asset_href``
    can't be resolved -- missing asset, or an empty href with no
    ``umbra:task_id`` to derive one.
    """
    if not enabled:
        return {}
    resolved: dict[str, tuple[str, tuple[float, float, float, float]]] = {}
    for item in items:
        if item.bbox is None:
            continue
        try:
            href = item.asset_href(asset)
        except AssetNotFoundError:
            continue
        if href:
            resolved[item.id] = (href, item.bbox)
    return resolved


def _install_lazy_imagery(
    folium_map: Any,
    percentile: tuple[float, float],
) -> None:
    """Inject the per-page button driver into the map's HTML.

    The driver injects its own CDN ``<script>`` tags on first click
    (see ``_lazy_imagery`` for the rationale -- short version: doing
    it from ``<head>`` races against Folium's Leaflet bundle, and
    georaster-layer-for-leaflet needs ``L.GridLayer`` defined before
    it evaluates). The driver finds the running map by DOM-traversal
    from each clicked button, so it stays correct across Jupyter
    cell reruns and multi-map pages.
    """
    folium = _require("folium")
    from .._lazy_imagery import driver_script  # noqa: PLC0415

    folium_map.get_root().script.add_child(
        folium.Element(
            driver_script(
                percentile_low=percentile[0],
                percentile_high=percentile[1],
            )
        )
    )


# Umbra's open data is CC-BY-4.0, which requires the data credit be shown
# wherever the data is used. A Folium map's default basemap only carries the
# *tile* provider's attribution (OpenStreetMap); the Umbra footprints and SAR
# overlays drawn on top are the licensed data, and their notice was missing.
# Register it with Leaflet's attribution control so it sits beside the OSM
# credit -- the standard place a web map shows its data sources -- rather than
# only inside per-marker popups a viewer has to click to reveal. Emitted as a
# MacroElement (the same runtime-script mechanism as _image_overlay_swipe_shim),
# so the notice is baked into the saved HTML and is asserted offline. ATTRIBUTION
# is a fixed package constant (no untrusted input), JSON-encoded into the call.
_ATTRIBUTION_JS = (
    "{% macro script(this, kwargs) %}\n"
    "{{ this._parent.get_name() }}.attributionControl.addAttribution("
    + json.dumps(ATTRIBUTION)
    + ");\n"
    "{% endmacro %}"
)


def _add_attribution(folium_map: Any) -> None:
    """Add the mandatory Umbra CC-BY data credit to a map's attribution control.

    The default Folium basemap credits only the tile provider; this adds the
    Umbra open-data licence notice (:data:`umbra_py.constants.ATTRIBUTION`)
    alongside it, satisfying CC-BY-4.0's attribution requirement on the
    generated map itself (``umbra map`` / ``--timeline`` / ``umbra swipe``).
    """
    from branca.element import MacroElement  # noqa: PLC0415
    from jinja2 import Template  # noqa: PLC0415

    el = MacroElement()
    el._name = "UmbraAttribution"
    el._template = Template(_ATTRIBUTION_JS)
    el.add_to(folium_map)


def footprint_map(
    items: Iterable[UmbraItem],
    *,
    tiles: str = "OpenStreetMap",
    color: str = "#ff5500",
    weight: int = 2,
    fill_opacity: float = 0.15,
    zoom_start: int | None = None,
    imagery: bool = False,
    imagery_kwargs: dict[str, Any] | None = None,
    geocode: bool = False,
    geocode_zoom: int = 10,
    lazy_imagery: bool = False,
    lazy_imagery_asset: str = "GEC",
    lazy_imagery_percentile: tuple[float, float] = (2.0, 98.0),
):
    """Build an interactive Folium map of one or more Umbra acquisitions.

    The map auto-fits the union of footprints and renders each item as a
    polygon with a metadata popup. Items without a geometry or bbox are
    silently skipped.

    When ``imagery=True``, each item's GEC asset is streamed (via HTTP
    range requests against the cloud-optimized GeoTIFF) and overlaid on
    the basemap. Items lacking a GEC asset are skipped silently; this
    needs ``rasterio`` (already in the ``viz`` extra). Pass per-overlay
    options via ``imagery_kwargs`` (e.g. ``{"max_size": 2048}``).

    When ``geocode=True``, each footprint's centroid is reverse-geocoded
    via OpenStreetMap Nominatim and the resulting place name is shown in
    the popup. The call is throttled to ≤1 req/s per Nominatim's usage
    policy and cached, so a 100-item map takes ~100 s of wall time on
    first render; rerunning is fast. ``geocode_zoom`` controls
    granularity (3 = country, 10 = city, 18 = building); see
    https://nominatim.org/release-docs/develop/api/Reverse/ for the full
    table. Off by default so library users don't make surprise network
    calls.

    When ``lazy_imagery=True``, each popup gets a "Get SAR image" button
    that streams the cloud-optimized GeoTIFF directly in the browser
    (via ``georaster-layer-for-leaflet`` + ``geotiff.js`` from a CDN)
    instead of pre-baking a PNG into the HTML. The map stays small no
    matter how many items it carries; users pay the fetch cost only for
    items they click. Requires the Umbra bucket's permissive CORS (it
    has it). ``lazy_imagery_asset`` selects the asset key (default
    ``"GEC"``); ``lazy_imagery_percentile`` controls the in-browser
    contrast stretch (default ``(2.0, 98.0)`` matches the Python
    overlay path). Mutually exclusive with ``imagery=True`` — eager and
    lazy imagery on the same item would compete for the same Leaflet
    layer slot.

    Requires the ``viz`` extra (``pip install "umbra-py[viz]"``). Returns
    a ``folium.Map`` you can ``.save("out.html")`` or display in Jupyter.
    """
    folium = _require("folium")

    if imagery and lazy_imagery:
        raise ValueError(
            "imagery=True and lazy_imagery=True can't be combined: both "
            "would add a SAR raster for each item. Pick one."
        )

    items = list(items)
    features = [(i, _geometry_for(i)) for i in items]
    features = [(i, g) for i, g in features if g is not None]

    bbox = _union_bbox([item_to_feature(i) for i, _ in features])
    if bbox is not None:
        center = ((bbox[1] + bbox[3]) / 2, (bbox[0] + bbox[2]) / 2)
    else:
        center = (0.0, 0.0)

    m = folium.Map(location=center, tiles=tiles, zoom_start=zoom_start or 2)
    _add_attribution(m)

    rendered_imagery: set[str] = set()
    if imagery:
        ik = imagery_kwargs or {}
        for item, _ in features:
            try:
                image_overlay(item, **ik).add_to(m)
                rendered_imagery.add(item.id)
            except (AssetNotFoundError, OSError, ValueError) as exc:
                # Skip items whose imagery we can't fetch/decode -- the
                # footprint polygon still renders below. Common causes:
                # the item lacks a GEC asset, the bucket returns 404 for
                # a referenced file, or the image has no valid pixels.
                # RasterioIOError subclasses OSError.
                warnings.warn(
                    f"Skipping SAR overlay for {item.id!r}: {exc}",
                    stacklevel=2,
                )

    # Resolve geocoded labels up front so we can reuse the same string in
    # both the polygon popup and the centroid-marker popup without paying
    # for the Nominatim call twice.
    locations: dict[str, str] = {}
    if geocode:
        # Prefer a baked `item.place` (a `CatalogIndex` search yields it for
        # free after `umbra index bake`); only fall back to a live Nominatim
        # call for items without one, and build the session lazily so a
        # fully-baked render never touches the network.
        geocode_session = None
        for item, _ in features:
            if item.place:
                locations[item.id] = item.place
                continue
            center_ll = _centroid(item)
            if center_ll is None:
                continue
            if geocode_session is None:
                geocode_session = _require_session_for_geocoding()
            label = _reverse_geocode(
                center_ll[0],
                center_ll[1],
                zoom=geocode_zoom,
                session=geocode_session,
            )
            if label:
                locations[item.id] = label

    # Resolve per-item COG URLs + footprint bounds for the lazy-fetch
    # button. Items whose asset_href can't be resolved, or that lack a
    # bbox to place the overlay, get no button -- the popup still works
    # for everything else.
    lazy_urls = _resolve_lazy_urls((i for i, _ in features), lazy_imagery, lazy_imagery_asset)

    for item, geometry in features:
        loc = locations.get(item.id)
        lazy_url, lazy_bounds = lazy_urls.get(item.id, (None, None))
        folium.GeoJson(
            {"type": "Feature", "geometry": geometry, "properties": {}},
            style_function=lambda _f, c=color, w=weight, fo=fill_opacity: {
                "color": c,
                "weight": w,
                "fillOpacity": fo,
            },
            tooltip=item.id,
            popup=folium.Popup(
                _popup_html(
                    item,
                    location=loc,
                    lazy_imagery_url=lazy_url,
                    lazy_imagery_bounds=lazy_bounds,
                ),
                max_width=420,
            ),
        ).add_to(m)

        # Always-visible centroid marker so a single tiny footprint is
        # findable when the polygon shrinks below a pixel at world zoom.
        center_ll = _centroid(item)
        if center_ll is not None:
            has_img = item.id in rendered_imagery
            folium.CircleMarker(
                location=center_ll,
                radius=6,
                color=color,
                weight=2,
                fill=True,
                fill_color=color if has_img else "white",
                fill_opacity=0.9 if has_img else 0.7,
                tooltip=item.id,
                popup=folium.Popup(
                    _popup_html(
                        item,
                        location=loc,
                        lazy_imagery_url=lazy_url,
                        lazy_imagery_bounds=lazy_bounds,
                    ),
                    max_width=420,
                ),
            ).add_to(m)

    if lazy_imagery and lazy_urls:
        _install_lazy_imagery(m, lazy_imagery_percentile)

    if features:
        m.get_root().html.add_child(
            folium.Element(
                _legend_html(
                    total=len(features),
                    with_imagery=len(rendered_imagery) if imagery else None,
                    color=color,
                )
            )
        )

    if bbox is not None and len(features) > 0:
        # Folium expects [[south, west], [north, east]].
        m.fit_bounds([[bbox[1], bbox[0]], [bbox[3], bbox[2]]])

    return m


def save_footprint_map(
    items: Iterable[UmbraItem],
    dest: str | os.PathLike,
    **kwargs,
) -> Path:
    """Build a footprint map and write it to ``dest`` as standalone HTML."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    footprint_map(items, **kwargs).save(str(dest))
    return dest


def timeline_map(
    items: Iterable[UmbraItem],
    *,
    tiles: str = "OpenStreetMap",
    color: str = "#ff5500",
    weight: int = 2,
    fill_opacity: float = 0.35,
    zoom_start: int = 2,
    period: str = "P1D",
    duration: str | None = None,
    auto_play: bool = True,
    loop: bool = False,
    transition_time: int = 400,
    geocode: bool = False,
    geocode_zoom: int = 10,
    lazy_imagery: bool = False,
    lazy_imagery_asset: str = "GEC",
    lazy_imagery_percentile: tuple[float, float] = (2.0, 98.0),
):
    """Build an animated timeline map of Umbra acquisitions.

    Each item is rendered as a polygon stamped with its acquisition
    datetime. Folium's ``TimestampedGeoJson`` plugin draws a play
    button and a time slider underneath the map: scrubbing through it
    reveals how Umbra's coverage accumulates across the requested
    window. Items without a datetime or geometry are skipped (they
    can't be placed on a time axis).

    This is a different lens on the same data ``footprint_map``
    handles. The static map answers "what areas does this search
    cover?"; the timeline map answers "when did Umbra image each of
    them?". Use it to spot revisit cadence over a tasked site, the
    sparsity vs. density of the archive across months, or the
    geographic footprint of a single day's collection.

    Parameters
    ----------
    items:
        Items to plot. Order is irrelevant; the plugin sorts by time.
    tiles, color, weight, fill_opacity, zoom_start:
        Same meaning as in :func:`footprint_map`.
    period:
        ISO 8601 duration string for the slider's tick interval (e.g.
        ``"PT1H"`` for hourly, ``"P1D"`` for daily, ``"P7D"`` for
        weekly). Default ``"P1D"``.
    duration:
        How long each footprint stays visible after its timestamp
        (ISO 8601 duration). ``None`` (default) keeps footprints on
        the map once revealed -- so the animation accumulates coverage.
        Pass e.g. ``"P1D"`` for a "show each day's acquisitions then
        fade" look.
    auto_play:
        Start the animation when the page loads.
    loop:
        Restart from the beginning when the slider reaches the end.
    transition_time:
        Milliseconds between slider ticks during playback. Lower =
        faster animation.
    geocode, geocode_zoom:
        Same semantics as :func:`footprint_map` -- reverse-geocode each
        footprint's centroid via OpenStreetMap Nominatim and surface
        the resulting place name in the popup. Throttled to ~1 req/s
        and cached, so a 100-item timeline takes ~100 s on first
        render. Off by default to avoid surprise network traffic.
    lazy_imagery, lazy_imagery_asset, lazy_imagery_percentile:
        Same semantics as :func:`footprint_map`. Each popup gets a
        "Get SAR image" button that streams the GEC cloud-optimized
        GeoTIFF in the browser on click, so a 200-item timeline stays
        ~30 KB instead of hundreds of MB. Pairs naturally with the
        animation: scrub to the moment you care about, click the
        polygon, see the actual SAR.

    Returns the underlying ``folium.Map``; ``.save("file.html")`` it
    or display it in Jupyter. Requires the ``viz`` extra.
    """
    folium = _require("folium")
    from folium.plugins import TimestampedGeoJson  # noqa: PLC0415

    items = list(items)
    plottable: list[UmbraItem] = []
    geoms: dict[str, dict[str, Any]] = {}
    for item in items:
        geom = _geometry_for(item)
        if geom is None or item.datetime is None:
            continue
        plottable.append(item)
        geoms[item.id] = geom

    # Resolve geocoded labels before the popup HTML is baked into the
    # TimestampedGeoJson feature properties -- the plugin renders the
    # popup string verbatim, so the location row has to be present at
    # generation time.
    locations: dict[str, str] = {}
    if geocode:
        # Prefer a baked `item.place` (see `footprint_map`); geocode only the
        # items without one, and create the session lazily so a fully-baked
        # render never makes a network call.
        geocode_session = None
        for item in plottable:
            if item.place:
                locations[item.id] = item.place
                continue
            center_ll = _centroid(item)
            if center_ll is None:
                continue
            if geocode_session is None:
                geocode_session = _require_session_for_geocoding()
            label = _reverse_geocode(
                center_ll[0],
                center_ll[1],
                zoom=geocode_zoom,
                session=geocode_session,
            )
            if label:
                locations[item.id] = label

    lazy_urls = _resolve_lazy_urls(plottable, lazy_imagery, lazy_imagery_asset)

    features: list[dict[str, Any]] = []
    bbox_inputs: list[dict[str, Any]] = []
    for item in plottable:
        # `plottable` holds only items whose datetime is not None (filtered
        # above), so this is always set -- the assert documents the invariant
        # and narrows the type for the checker.
        dt = item.datetime
        assert dt is not None
        lazy_url, lazy_bounds = lazy_urls.get(item.id, (None, None))
        features.append(
            {
                "type": "Feature",
                "geometry": geoms[item.id],
                "properties": {
                    "times": [dt.isoformat()],
                    "popup": _popup_html(
                        item,
                        location=locations.get(item.id),
                        lazy_imagery_url=lazy_url,
                        lazy_imagery_bounds=lazy_bounds,
                    ),
                    "id": item.id,
                    "style": {
                        "color": color,
                        "weight": weight,
                        "fillColor": color,
                        "fillOpacity": fill_opacity,
                    },
                    "icon": "circle",
                    "iconstyle": {
                        "fillColor": color,
                        "fillOpacity": 0.85,
                        "stroke": "true",
                        "color": color,
                        "radius": 6,
                    },
                },
            }
        )
        bbox_inputs.append(item_to_feature(item))

    bbox = _union_bbox(bbox_inputs)
    if bbox is not None:
        center = ((bbox[1] + bbox[3]) / 2, (bbox[0] + bbox[2]) / 2)
    else:
        center = (0.0, 0.0)

    m = folium.Map(location=center, tiles=tiles, zoom_start=zoom_start)
    _add_attribution(m)

    if features:
        TimestampedGeoJson(
            {"type": "FeatureCollection", "features": features},
            period=period,
            duration=duration,
            auto_play=auto_play,
            loop=loop,
            transition_time=transition_time,
            add_last_point=False,
            date_options="YYYY-MM-DD HH:mm UTC",
            time_slider_drag_update=True,
        ).add_to(m)

    if lazy_imagery and lazy_urls:
        _install_lazy_imagery(m, lazy_imagery_percentile)

    if bbox is not None:
        m.fit_bounds([[bbox[1], bbox[0]], [bbox[3], bbox[2]]])

    return m


def save_timeline_map(
    items: Iterable[UmbraItem],
    dest: str | os.PathLike,
    **kwargs,
) -> Path:
    """Build a timeline map and write it to ``dest`` as standalone HTML."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    timeline_map(items, **kwargs).save(str(dest))
    return dest


# leaflet-side-by-side clips each layer via ``layer.getContainer()`` (which
# GridLayer/TileLayer has but ImageOverlay does not) using a rectangle in the
# map's *layer-point* coordinate space. That space is the coordinate origin of
# a Leaflet pane -- not of the overlay's <img>, which is translate()-d to the
# image's position, so clipping the <img> directly is offset by that
# translation. So we point getContainer at the overlay's pane instead, and the
# swipe map puts each overlay in its own pane so the two clips stay
# independent. Emitted as a map child right before the control so it runs
# after Leaflet loads (head) and before ``L.control.sideBySide`` reads it.
_SWIPE_SHIM_JS = (
    "{% macro script(this, kwargs) %}\n"
    "L.ImageOverlay.prototype.getContainer = function() { return this.getPane(); };\n"
    "{% endmacro %}"
)


def _image_overlay_swipe_shim():
    """A Folium element that aliases ``ImageOverlay.getContainer`` at runtime."""
    from branca.element import MacroElement  # noqa: PLC0415
    from jinja2 import Template  # noqa: PLC0415

    shim = MacroElement()
    shim._name = "ImageOverlaySwipeShim"
    shim._template = Template(_SWIPE_SHIM_JS)
    return shim


def swipe_map(
    before: UmbraItem,
    after: UmbraItem,
    *,
    asset: str = "GEC",
    max_size: int = 1024,
    percentile: tuple[float, float] = (2.0, 98.0),
    db: bool = False,
    tiles: str = "OpenStreetMap",
):
    """Build an interactive before/after *swipe* map of two SAR passes.

    Where :func:`change_composite` bakes the comparison into one colored
    still and :func:`change_animation` flips between dates, this renders a
    draggable divider: the ``before`` acquisition fills the left of the
    slider, ``after`` the right, and dragging the handle wipes one over the
    other across the *same* ground. SAR's backscatter is stable between
    passes, so anything that changed -- a ship that docked, a field that
    flooded, a building that rose -- snaps in and out as you sweep the seam.
    It is the most direct way to *feel* change in the archive, and the whole
    thing is a single self-contained HTML file.

    The two acquisitions are **co-registered** onto one shared lon/lat grid
    -- their footprint intersection, read at a downsampled resolution via
    HTTP range requests against the cloud-optimized GeoTIFFs (no full
    download) -- so both overlays cover the *identical* ground at the
    *identical* pixel scale. That alignment is what makes the swipe honest:
    each pass would otherwise warp to a differently-rotated bounding box, and
    the seam would compare different ground. Pass the two items in
    chronological order. ``db`` selects the decibel stretch (the
    radiometrically-correct SAR look); ``asset`` defaults to ``"GEC"`` (the
    detected GeoTIFF), which along with ``"CSI"`` is the sensible target.

    Raises ``ValueError`` if the two footprints don't overlap (nothing to
    compare). Requires the ``viz`` extra
    (``pip install "umbra-py[viz]"``). Returns a ``folium.Map`` you can
    ``.save("swipe.html")`` or display in Jupyter.
    """
    folium = _require("folium")
    from folium.map import CustomPane  # noqa: PLC0415
    from folium.plugins import SideBySideLayers  # noqa: PLC0415

    bands, bounds, _ = _coregister_bands([before, after], asset, max_size)
    left_rgba = _stretch_to_rgba(bands[0], percentile=percentile, db=db)
    right_rgba = _stretch_to_rgba(bands[1], percentile=percentile, db=db)

    bleft, bbottom, bright, btop = bounds
    center = ((bbottom + btop) / 2, (bleft + bright) / 2)

    m = folium.Map(location=center, tiles=tiles, zoom_start=2)
    _add_attribution(m)
    # One full-map pane per overlay so the side-by-side control can clip each
    # independently in layer-point space (see _SWIPE_SHIM_JS). Panes must be
    # created before the overlays that reference them.
    CustomPane("sbsBefore", z_index=625).add_to(m)
    CustomPane("sbsAfter", z_index=626).add_to(m)
    left = _rgba_overlay(left_rgba, bounds, pane="sbsBefore")
    right = _rgba_overlay(right_rgba, bounds, pane="sbsAfter")
    left.add_to(m)
    right.add_to(m)
    _image_overlay_swipe_shim().add_to(m)
    SideBySideLayers(layer_left=left, layer_right=right).add_to(m)
    m.fit_bounds([[bbottom, bleft], [btop, bright]])
    return m


def save_swipe_map(
    before: UmbraItem,
    after: UmbraItem,
    dest: str | os.PathLike,
    **kwargs,
) -> Path:
    """Build a before/after swipe map and write it to ``dest`` as HTML."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    swipe_map(before, after, **kwargs).save(str(dest))
    return dest
