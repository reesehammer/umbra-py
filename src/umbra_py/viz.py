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
- **Standalone SAR quicklooks** (requires ``viz`` + rasterio):
  ``quicklook`` / ``save_quicklook`` turn one acquisition into a plain
  image file — no map, no GIS, no full download — with optional decibel
  scaling and matplotlib pseudo-color for the radiometrically-correct
  SAR look.
- **Multi-temporal change composites** (requires ``viz`` + rasterio):
  ``change_composite`` / ``save_change_composite`` co-register 2–3
  acquisitions of the same site onto a shared grid and color-code them by
  date, so unchanged ground stays gray while anything that appeared or
  vanished between passes lights up — SAR's signature change-detection
  view, with no manual co-registration.

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
    lazy_imagery_bounds: tuple[float, float, float, float] | None = None,
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
    if lazy_imagery_url and lazy_imagery_bounds is not None:
        from ._lazy_imagery import popup_button_html  # noqa: PLC0415

        button = popup_button_html(
            item_id=item.id,
            asset_url=lazy_imagery_url,
            bounds=lazy_imagery_bounds,
        )
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


def _normalize_band(
    data: Any,
    *,
    percentile: tuple[float, float] = (2.0, 98.0),
    db: bool = False,
) -> tuple[Any, Any]:
    """Percentile-stretch a 2D SAR amplitude band to ``[0, 1]`` + a mask.

    Returns ``(norm, invalid)``: ``norm`` is a float64 array in ``[0, 1]``
    (invalid pixels clamped to 0) and ``invalid`` is a boolean mask of the
    pixels that were NaN / nodata / non-positive.

    SAR data has enormous dynamic range; a straight 0-255 scaling looks
    almost black. We compute the low/high cut on positive, finite values
    only, clip the rest to that range, and rescale. When ``db`` is True the
    amplitudes are first converted to decibels (``20*log10(amplitude)``)
    before the percentile stretch -- the radiometrically-meaningful view:
    the log compresses the huge dynamic range so terrain texture and urban
    structure that a linear amplitude stretch crushes into near-black
    become visible.

    Shared by the grayscale/pseudo-color quicklook path
    (:func:`_stretch_to_rgba`) and the multi-temporal change composite
    (:func:`_compose_change_rgba`).
    """
    np = _require("numpy")
    # float64 so the log and the rescale don't lose precision on integer
    # amplitude rasters.
    arr = np.asarray(data, dtype="float64")
    invalid = ~np.isfinite(arr) | (arr <= 0)
    if invalid.all():
        raise ValueError("Image has no valid pixels to stretch.")
    if db:
        # amplitude -> decibels; only defined for the positive pixels we
        # already flagged as valid. Invalid pixels become NaN and are
        # masked out of the percentile below.
        with np.errstate(divide="ignore", invalid="ignore"):
            arr = np.where(invalid, np.nan, 20.0 * np.log10(arr))
    valid = arr[~invalid]
    lo, hi = np.percentile(valid, percentile)
    if hi <= lo:
        hi = lo + 1.0
    # Replace invalid pixels with lo before scaling so NaN values don't
    # trigger numpy warnings; they're set fully transparent by callers.
    safe = np.where(invalid, lo, arr)
    norm = np.clip((safe - lo) / (hi - lo), 0.0, 1.0)
    return norm, invalid


def _stretch_to_rgba(
    data: Any,
    *,
    percentile: tuple[float, float] = (2.0, 98.0),
    db: bool = False,
    colormap: str | None = None,
) -> Any:
    """Convert a 2D array of SAR amplitudes to an RGBA uint8 image.

    Pixels that were invalid (NaN / nodata / non-positive) become fully
    transparent so the basemap shows through scene edges. See
    :func:`_normalize_band` for the percentile-stretch and optional dB
    rationale.

    When ``colormap`` names a matplotlib colormap (e.g. ``"viridis"``,
    ``"magma"``) the stretched values are mapped through it for a
    pseudo-colored quicklook instead of grayscale; this needs matplotlib
    (already in the ``viz`` extra).
    """
    np = _require("numpy")
    norm, invalid = _normalize_band(data, percentile=percentile, db=db)
    alpha = np.where(invalid, 0, 255).astype("uint8")

    if colormap:
        rgb = _apply_colormap(norm, colormap)
    else:
        gray = (norm * 255.0).astype("uint8")
        rgb = np.stack([gray, gray, gray], axis=-1)
    return np.dstack([rgb, alpha])


def _apply_colormap(norm: Any, name: str) -> Any:
    """Map a [0,1]-normalised 2D array through a matplotlib colormap.

    Returns an ``(H, W, 3)`` uint8 RGB array. Raised separately from
    ``_stretch_to_rgba`` so the numpy-only grayscale path doesn't import
    matplotlib.
    """
    _require("matplotlib")
    from matplotlib import colormaps  # noqa: PLC0415

    cmap = colormaps[name]
    rgb = cmap(norm)[..., :3]  # drop the colormap's own alpha channel
    return (rgb * 255.0).astype("uint8")


def _read_sar_band(
    item: UmbraItem,
    asset: str,
    max_size: int,
    *,
    reproject_to_4326: bool = False,
) -> tuple[Any, Any]:
    """Read a downsampled band 1 of an item's SAR GeoTIFF via range requests.

    Returns ``(data, bounds)`` where ``data`` is a 2D numpy array and
    ``bounds`` is the dataset's geographic bounds. Only the bytes for the
    requested resolution are fetched (the asset is a cloud-optimized
    GeoTIFF read through GDAL's ``/vsicurl/`` driver). When
    ``reproject_to_4326`` is True the raster is warped to lon/lat so it
    can be placed on a web map; for a standalone quicklook the native
    projection is read as-is (no warp distortion).
    """
    rasterio = _require("rasterio")
    _require("numpy")
    from rasterio.enums import Resampling  # noqa: PLC0415
    from rasterio.vrt import WarpedVRT  # noqa: PLC0415

    url = item.asset_href(asset)
    if not url:
        raise AssetNotFoundError(
            f"Item {item.id!r} has no resolvable URL for asset {asset!r} "
            "(asset href is empty and no umbra:task_id available to derive one)."
        )
    with rasterio.open(f"/vsicurl/{url}") as src:
        if reproject_to_4326:
            epsg = src.crs.to_epsg() if src.crs else None
            wrap = WarpedVRT(src, crs="EPSG:4326") if epsg != 4326 else None
        else:
            wrap = None
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
    return data, bounds


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
    _require("PIL")

    import base64  # noqa: PLC0415
    import io  # noqa: PLC0415

    from PIL import Image  # noqa: PLC0415

    data, bounds = _read_sar_band(item, asset, max_size, reproject_to_4326=True)

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


def quicklook(
    item: UmbraItem,
    *,
    asset: str = "GEC",
    max_size: int = 2048,
    percentile: tuple[float, float] = (2.0, 98.0),
    db: bool = False,
    colormap: str | None = None,
):
    """Render a standalone SAR quicklook image of an item.

    Reads a downsampled preview of the cloud-optimized GeoTIFF via HTTP
    range requests (only the bytes for the requested resolution are
    fetched — no multi-gigabyte download), applies a percentile contrast
    stretch tuned for SAR's dynamic range, and returns a ``PIL.Image`` you
    can ``.save("scene.png")`` or display in a notebook.

    This is the lowest-friction way to *see* an Umbra acquisition: no map,
    no GIS, no full download. Unlike :func:`image_overlay`, the raster is
    read in its native (already geocoded, north-up) projection rather than
    warped to lon/lat — a faithful look at the pixels rather than a
    map-placeable overlay.

    ``db`` switches to a decibel (log-amplitude) stretch, the
    radiometrically-correct way to view SAR — it reveals terrain texture
    and urban structure that the default linear stretch crushes toward
    black. ``colormap`` names a matplotlib colormap (``"viridis"``,
    ``"magma"``, ...) for a pseudo-colored quicklook instead of grayscale.

    ``asset`` defaults to ``"GEC"``, the detected single-band image; that
    and ``"CSI"`` are the sensible targets (the complex SICD/CPHD products
    aren't amplitude rasters). Requires the ``viz`` extra.
    """
    _require("PIL")
    from PIL import Image  # noqa: PLC0415

    data, _ = _read_sar_band(item, asset, max_size, reproject_to_4326=False)
    rgba = _stretch_to_rgba(data, percentile=percentile, db=db, colormap=colormap)
    return Image.fromarray(rgba, mode="RGBA")


def save_quicklook(
    item: UmbraItem,
    dest: str | os.PathLike,
    **kwargs,
) -> Path:
    """Render an item's SAR quicklook and write it to ``dest`` as an image.

    The output format follows ``dest``'s extension (``.png``, ``.jpg``,
    ...), per Pillow. See :func:`quicklook` for the rendering options.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    image = quicklook(item, **kwargs)
    if dest.suffix.lower() in (".jpg", ".jpeg"):
        # JPEG has no alpha channel; flatten transparent (invalid) pixels
        # onto black so the save doesn't error.
        image = image.convert("RGB")
    image.save(str(dest))
    return dest


def _compose_change_rgba(
    bands: list[Any],
    *,
    percentile: tuple[float, float] = (2.0, 98.0),
    db: bool = False,
) -> Any:
    """Stack 2-3 co-registered SAR bands into an RGBA change composite.

    Each band is percentile-stretched independently, then assigned to a
    color channel by acquisition order:

    - **Two dates** map to ``R = t1, G = t2, B = t1``. An unchanged pixel
      (``t1 == t2``) lands on the gray diagonal; a pixel that brightened
      only in the later pass shows **green**; one that dimmed shows
      **magenta**. This is the classic two-date SAR change product.
    - **Three dates** map straight to ``R/G/B`` -- a temporal-RGB where
      stationary scene stays gray and anything that changed between passes
      is tinted by *when* it was bright.

    All bands must already share a pixel grid (use :func:`_coregister_bands`).
    A pixel invalid in *any* band is made transparent, so the composite
    only colors ground seen on every pass.
    """
    np = _require("numpy")
    n = len(bands)
    if n not in (2, 3):
        raise ValueError(f"change composite needs 2 or 3 bands, got {n}.")
    shape = np.asarray(bands[0]).shape
    if any(np.asarray(b).shape != shape for b in bands):
        raise ValueError("all bands must share the same shape; co-register first.")

    norms: list[Any] = []
    invalid = np.zeros(shape, dtype=bool)
    for band in bands:
        norm, inv = _normalize_band(band, percentile=percentile, db=db)
        norms.append(norm)
        invalid |= inv

    order = (0, 1, 0) if n == 2 else (0, 1, 2)
    rgb = np.stack([(norms[i] * 255.0).astype("uint8") for i in order], axis=-1)
    alpha = np.where(invalid, 0, 255).astype("uint8")
    return np.dstack([rgb, alpha])


def _coregister_bands(
    items: list[UmbraItem],
    asset: str,
    max_size: int,
) -> tuple[list[Any], tuple[float, float, float, float]]:
    """Read each item's SAR band onto one shared EPSG:4326 grid.

    Returns ``(bands, bounds)`` where ``bands`` is a list of 2D arrays --
    one per item, all the same shape and pixel-aligned -- and ``bounds`` is
    the geographic intersection ``(left, bottom, right, top)`` they cover.
    Each source cloud-optimized GeoTIFF is read at a downsampled resolution
    via range requests and warped to lon/lat so the same output pixel
    refers to the same ground location across dates -- the prerequisite for
    an honest change comparison.

    Raises ``ValueError`` when the footprints don't overlap (nothing to
    compare).
    """
    rasterio = _require("rasterio")
    _require("numpy")
    from rasterio.enums import Resampling  # noqa: PLC0415
    from rasterio.transform import from_bounds  # noqa: PLC0415
    from rasterio.vrt import WarpedVRT  # noqa: PLC0415
    from rasterio.warp import transform_bounds  # noqa: PLC0415

    datasets: list[Any] = []
    try:
        for item in items:
            url = item.asset_href(asset)
            if not url:
                raise AssetNotFoundError(
                    f"Item {item.id!r} has no resolvable URL for asset {asset!r}."
                )
            datasets.append(rasterio.open(f"/vsicurl/{url}"))

        # Intersection of every footprint, in lon/lat.
        wgs: list[tuple[float, float, float, float]] = []
        for ds in datasets:
            b = ds.bounds
            if ds.crs and ds.crs.to_epsg() != 4326:
                b = transform_bounds(ds.crs, "EPSG:4326", *b)
            wgs.append(tuple(b))
        left = max(b[0] for b in wgs)
        bottom = max(b[1] for b in wgs)
        right = min(b[2] for b in wgs)
        top = min(b[3] for b in wgs)
        if left >= right or bottom >= top:
            raise ValueError(
                "Footprints do not overlap, so there's nothing to compare. "
                "Change detection needs acquisitions of the same area "
                "(e.g. items from one Umbra task)."
            )

        # Output grid: max_size on the longer side, aspect from the
        # intersection's lon/lat extent. Same lat/lon-stretch quick-look
        # approximation image_overlay uses -- fine at the scene scale,
        # mildly distorted toward the poles.
        w_deg, h_deg = right - left, top - bottom
        if w_deg >= h_deg:
            out_w = max_size
            out_h = max(int(round(max_size * h_deg / w_deg)), 1)
        else:
            out_h = max_size
            out_w = max(int(round(max_size * w_deg / h_deg)), 1)
        transform = from_bounds(left, bottom, right, top, out_w, out_h)

        bands: list[Any] = []
        for ds in datasets:
            with WarpedVRT(
                ds,
                crs="EPSG:4326",
                transform=transform,
                width=out_w,
                height=out_h,
                resampling=Resampling.average,
            ) as vrt:
                bands.append(vrt.read(1))
    finally:
        for ds in datasets:
            ds.close()
    return bands, (left, bottom, right, top)


def change_composite(
    items: Iterable[UmbraItem],
    *,
    asset: str = "GEC",
    max_size: int = 2048,
    percentile: tuple[float, float] = (2.0, 98.0),
    db: bool = False,
):
    """Render a multi-temporal SAR change composite of 2-3 acquisitions.

    SAR's killer app is change detection: the radar backscatter of a fixed
    scene is remarkably stable between passes, so anything that *did* change
    -- a ship that arrived, a field that flooded, a building that went up --
    jumps out against the static background. This function turns 2 or 3
    acquisitions of the same site into a single color image where unchanged
    ground stays gray and change is tinted by *when* it happened.

    Pass the items in **chronological order**. The bands are co-registered
    onto a shared lon/lat grid (so the same pixel is the same place on every
    date), each is percentile-stretched, and they're assigned to color
    channels:

    - **Two dates:** **green** = backscatter that appeared in the later pass
      (new/brighter), **magenta** = backscatter that vanished (gone/dimmer),
      gray/white = unchanged.
    - **Three dates:** a temporal-RGB (earliest=red, middle=green,
      latest=blue); a moving bright target leaves a red→green→blue trail.

    Only the area imaged on *every* pass is colored; pixels missing from any
    acquisition are transparent. ``db`` switches to a decibel stretch (the
    radiometrically-correct SAR view). ``asset`` defaults to ``"GEC"`` (the
    detected amplitude GeoTIFF); ``"CSI"`` also works. Only a downsampled
    overview of each cloud-optimized GeoTIFF is fetched (range requests, no
    full download). Returns a ``PIL.Image``. Requires the ``viz`` extra.
    """
    _require("PIL")
    from PIL import Image  # noqa: PLC0415

    items = list(items)
    if len(items) not in (2, 3):
        raise ValueError(f"change_composite needs 2 or 3 acquisitions, got {len(items)}.")

    bands, _ = _coregister_bands(items, asset, max_size)
    rgba = _compose_change_rgba(bands, percentile=percentile, db=db)
    return Image.fromarray(rgba, mode="RGBA")


def save_change_composite(
    items: Iterable[UmbraItem],
    dest: str | os.PathLike,
    **kwargs,
) -> Path:
    """Render a SAR change composite and write it to ``dest`` as an image.

    The output format follows ``dest``'s extension (``.png``, ``.jpg``,
    ...), per Pillow. See :func:`change_composite` for the rendering
    options and color semantics.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    image = change_composite(items, **kwargs)
    if dest.suffix.lower() in (".jpg", ".jpeg"):
        # JPEG has no alpha channel; flatten transparent (un-imaged) pixels
        # onto black so the save doesn't error.
        image = image.convert("RGB")
    image.save(str(dest))
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
        lazy_url, lazy_bounds = lazy_urls.get(item.id, (None, None))
        features.append(
            {
                "type": "Feature",
                "geometry": geoms[item.id],
                "properties": {
                    "times": [item.datetime.isoformat()],
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
