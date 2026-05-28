"""Visualization helpers for Umbra search results.

This module turns ``UmbraItem`` objects into:

- **GeoJSON features** (zero dependencies) — open them in QGIS, leafmap,
  Earth Engine, geopandas, deck.gl, or anywhere else that reads GeoJSON.
- **Interactive Folium maps** (requires the ``viz`` extra) — drop-in HTML
  for notebooks or sharing, with one polygon per acquisition and a popup
  showing each item's metadata and an "open" link.
- **SAR image overlays** on top of those maps (requires ``viz`` + rasterio):
  ``image_overlay`` and ``footprint_map(..., imagery=True)`` stream a
  downsampled preview of the GEC asset via HTTP range requests and
  composite it onto the basemap. Self-contained — the resulting HTML
  embeds the image as a base64 PNG, no tile server required.

The first surface is the important one: Umbra acquisitions are points on
the planet, and being able to *see* where a search landed before
downloading multi-gigabyte SAR files is the difference between exploring
the archive and giving up.

Install the optional dependency for the interactive map with::

    pip install "umbra-py[viz]"
"""

from __future__ import annotations

import html
import json
import os
import warnings
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .exceptions import AssetNotFoundError, MissingDependencyError
from .models import UmbraItem


def _require(module: str):
    try:
        return __import__(module)
    except ImportError as exc:  # pragma: no cover - only without extra
        raise MissingDependencyError(
            f"'{module}' is required for interactive maps. "
            'Install the extra with: pip install "umbra-py[viz]"'
        ) from exc


def _geometry_for(item: UmbraItem) -> dict[str, Any] | None:
    """Return a 2D GeoJSON geometry for the item.

    Umbra footprints are often 3D polygons (lon, lat, height); strip the
    third coordinate so consumers that expect 2D (Folium, leaflet, most
    GIS tools) render them correctly.
    """
    geom = item.geometry
    if geom and geom.get("coordinates"):
        return {"type": geom.get("type", "Polygon"), "coordinates": _strip_z(geom["coordinates"])}
    if item.bbox is not None:
        minx, miny, maxx, maxy = item.bbox
        return {
            "type": "Polygon",
            "coordinates": [[[minx, miny], [maxx, miny], [maxx, maxy], [minx, maxy], [minx, miny]]],
        }
    return None


def _strip_z(coords: Any) -> Any:
    if (
        isinstance(coords, (list, tuple))
        and len(coords) >= 2
        and all(isinstance(v, (int, float)) for v in coords[:2])
    ):
        return [float(coords[0]), float(coords[1])]
    if isinstance(coords, (list, tuple)):
        return [_strip_z(c) for c in coords]
    return coords


def item_to_feature(item: UmbraItem) -> dict[str, Any]:
    """Convert one ``UmbraItem`` to a GeoJSON ``Feature`` dict.

    Properties include the compact metadata summary plus the item's STAC
    URL (``stac_href``) so downstream tools can link back to the source.
    """
    props = item.metadata_summary()
    props["stac_href"] = item.href
    geometry = _geometry_for(item)
    return {
        "type": "Feature",
        "id": item.id,
        "geometry": geometry,
        "bbox": list(item.bbox) if item.bbox else None,
        "properties": props,
    }


def items_to_featurecollection(items: Iterable[UmbraItem]) -> dict[str, Any]:
    """Convert items to a single GeoJSON ``FeatureCollection`` dict."""
    features = [item_to_feature(i) for i in items]
    bbox = _union_bbox(features)
    fc: dict[str, Any] = {"type": "FeatureCollection", "features": features}
    if bbox is not None:
        fc["bbox"] = list(bbox)
    return fc


def write_geojson(
    items: Iterable[UmbraItem],
    dest: str | os.PathLike,
    *,
    indent: int | None = 2,
) -> Path:
    """Write items as a GeoJSON FeatureCollection to ``dest``."""
    fc = items_to_featurecollection(items)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(fc, indent=indent))
    return dest


def _union_bbox(features: list[dict[str, Any]]) -> tuple[float, float, float, float] | None:
    boxes = [f["bbox"] for f in features if f.get("bbox")]
    if not boxes:
        return None
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def _popup_html(
    item: UmbraItem,
    *,
    location: str | None = None,
    lazy_imagery_url: str | None = None,
) -> str:
    info = item.metadata_summary()
    rng, azi = info["resolution_range_m"], info["resolution_azimuth_m"]

    def fmt(v: Any, suffix: str = "") -> str:
        if v is None:
            return "&mdash;"
        if isinstance(v, float):
            return f"{v:.2f}{suffix}"
        return f"{v}{suffix}"

    rows = [
        ("ID", info["id"]),
        ("Acquired", info["datetime"] or "&mdash;"),
        ("Platform", fmt(info["platform"])),
        ("Mode", fmt(info["instrument_mode"])),
        ("Product", fmt(info["product_type"])),
        ("Polarizations", ", ".join(info["polarizations"]) or "&mdash;"),
        ("Incidence", fmt(info["incidence_angle_deg"], "&deg;")),
        ("Resolution (rng × azi)", f"{fmt(rng, ' m')} × {fmt(azi, ' m')}"),
        ("Assets", ", ".join(info["available_assets"]) or "&mdash;"),
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
    link = (
        f"<p style='margin-top:6px'><a href='{item.href}' target='_blank'>open STAC item</a></p>"
        if item.href
        else ""
    )
    button = ""
    if lazy_imagery_url:
        from ._lazy_imagery import popup_button_html  # noqa: PLC0415

        button = popup_button_html(item_id=item.id, asset_url=lazy_imagery_url)
    return (
        f"<table style='font-family:sans-serif;font-size:12px'>{body}</table>"
        f"{desc_html}{button}{link}"
    )


def _centroid(item: UmbraItem) -> tuple[float, float] | None:
    """Return (lat, lon) center of an item's footprint, or None."""
    if item.bbox is None:
        return None
    minx, miny, maxx, maxy = item.bbox
    return ((miny + maxy) / 2.0, (minx + maxx) / 2.0)


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
    from ._http import default_session  # noqa: PLC0415

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
        from ._http import default_session  # noqa: PLC0415

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
) -> dict[str, str]:
    """Return ``{item_id: cog_url}`` for items that can be lazily fetched.

    When ``enabled`` is False we short-circuit to an empty dict so the
    caller doesn't have to repeat that check. Items missing the asset
    or whose ``asset_href`` returns the empty string (no populated
    href and no ``umbra:task_id`` to derive one) are silently dropped
    -- the popup still renders for them, just without the button.
    """
    if not enabled:
        return {}
    urls: dict[str, str] = {}
    for item in items:
        try:
            href = item.asset_href(asset)
        except AssetNotFoundError:
            continue
        if href:
            urls[item.id] = href
    return urls


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
    from ._lazy_imagery import driver_script  # noqa: PLC0415

    folium_map.get_root().script.add_child(
        folium.Element(
            driver_script(
                percentile_low=percentile[0],
                percentile_high=percentile[1],
            )
        )
    )


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
        geocode_session = _require_session_for_geocoding()
        for item, _ in features:
            center_ll = _centroid(item)
            if center_ll is None:
                continue
            label = _reverse_geocode(
                center_ll[0],
                center_ll[1],
                zoom=geocode_zoom,
                session=geocode_session,
            )
            if label:
                locations[item.id] = label

    # Resolve per-item COG URLs for the lazy-fetch button. Items whose
    # asset_href can't be resolved (no populated href, no task_id) get
    # no button -- the popup still works for everything else.
    lazy_urls = _resolve_lazy_urls((i for i, _ in features), lazy_imagery, lazy_imagery_asset)

    for item, geometry in features:
        loc = locations.get(item.id)
        lazy_url = lazy_urls.get(item.id)
        folium.GeoJson(
            {"type": "Feature", "geometry": geometry, "properties": {}},
            style_function=lambda _f, c=color, w=weight, fo=fill_opacity: {
                "color": c,
                "weight": w,
                "fillOpacity": fo,
            },
            tooltip=item.id,
            popup=folium.Popup(
                _popup_html(item, location=loc, lazy_imagery_url=lazy_url),
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
                    _popup_html(item, location=loc, lazy_imagery_url=lazy_url),
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


def _stretch_to_rgba(data: Any, *, percentile: tuple[float, float] = (2.0, 98.0)) -> Any:
    """Convert a 2D array of SAR amplitudes to an RGBA uint8 image.

    SAR data has enormous dynamic range; a straight 0-255 scaling looks
    almost black. We compute the low/high cut on positive, finite values
    only, clip the rest to that range, and rescale. Pixels that were
    invalid (NaN / nodata / non-positive) become fully transparent so the
    basemap shows through scene edges.
    """
    np = _require("numpy")
    arr = np.asarray(data)
    invalid = ~np.isfinite(arr) | (arr <= 0)
    valid = arr[~invalid]
    if valid.size == 0:
        raise ValueError("Image has no valid pixels to stretch.")
    lo, hi = np.percentile(valid, percentile)
    if hi <= lo:
        hi = lo + 1.0
    # Replace invalid pixels with lo before the uint8 cast so NaN values
    # don't trigger numpy's "invalid value encountered in cast" warning;
    # they're set fully transparent below regardless.
    safe = np.where(invalid, lo, arr)
    scaled = np.clip((safe - lo) / (hi - lo) * 255.0, 0, 255).astype("uint8")
    rgba = np.zeros((arr.shape[0], arr.shape[1], 4), dtype="uint8")
    rgba[..., 0] = scaled
    rgba[..., 1] = scaled
    rgba[..., 2] = scaled
    rgba[..., 3] = np.where(invalid, 0, 255).astype("uint8")
    return rgba


def image_overlay(
    item: UmbraItem,
    *,
    asset: str = "GEC",
    max_size: int = 1024,
    percentile: tuple[float, float] = (2.0, 98.0),
    opacity: float = 1.0,
):
    """Build a Folium ``ImageOverlay`` of an item's SAR image.

    Reads a downsampled preview of the cloud-optimized GeoTIFF via HTTP
    range requests (only the bytes for the requested resolution are
    fetched), applies a percentile contrast stretch for SAR amplitude,
    reprojects to lat/lon if necessary, and embeds the result as a base64
    PNG so the resulting map stays a single self-contained HTML file.

    Requires the ``viz`` extra (which pulls in rasterio + numpy; Pillow
    comes transitively via matplotlib).
    """
    folium = _require("folium")
    rasterio = _require("rasterio")
    _require("numpy")
    _require("PIL")

    import base64  # noqa: PLC0415
    import io  # noqa: PLC0415

    from PIL import Image  # noqa: PLC0415
    from rasterio.enums import Resampling  # noqa: PLC0415
    from rasterio.vrt import WarpedVRT  # noqa: PLC0415

    url = item.asset_href(asset)
    if not url:
        raise AssetNotFoundError(
            f"Item {item.id!r} has no resolvable URL for asset {asset!r} "
            "(asset href is empty and no umbra:task_id available to derive one)."
        )
    with rasterio.open(f"/vsicurl/{url}") as src:
        epsg = src.crs.to_epsg() if src.crs else None
        wrap = WarpedVRT(src, crs="EPSG:4326") if epsg != 4326 else None
        ds = wrap if wrap is not None else src
        try:
            scale = max(max(ds.width, ds.height) / max_size, 1.0)
            out_w = max(int(ds.width / scale), 1)
            out_h = max(int(ds.height / scale), 1)
            data = ds.read(1, out_shape=(out_h, out_w), resampling=Resampling.average)
            bounds = ds.bounds
        finally:
            if wrap is not None:
                wrap.close()

    rgba = _stretch_to_rgba(data, percentile=percentile)
    buf = io.BytesIO()
    Image.fromarray(rgba, mode="RGBA").save(buf, format="PNG")
    data_uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

    return folium.raster_layers.ImageOverlay(
        image=data_uri,
        bounds=[[bounds.bottom, bounds.left], [bounds.top, bounds.right]],
        opacity=opacity,
    )


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
        geocode_session = _require_session_for_geocoding()
        for item in plottable:
            center_ll = _centroid(item)
            if center_ll is None:
                continue
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
        features.append(
            {
                "type": "Feature",
                "geometry": geoms[item.id],
                "properties": {
                    "times": [item.datetime.isoformat()],
                    "popup": _popup_html(
                        item,
                        location=locations.get(item.id),
                        lazy_imagery_url=lazy_urls.get(item.id),
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
