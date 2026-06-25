"""HTML/SVG rendering of items for Jupyter ``_repr_html_``.

This module is deliberately dependency-free: it builds strings only, so the
rich notebook representation of an :class:`~umbra_py.models.UmbraItem` works in
the *core* install with no extras. The SAR pixel thumbnails (which do need
``rasterio`` and a network read) are produced elsewhere and passed in as
ready-made data URIs; here we only lay them out.
"""

from __future__ import annotations

from html import escape
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .models import UmbraItem

# A compact, scoped stylesheet. Re-emitting it per card is harmless (identical
# rules) and keeps each rendering a self-contained fragment.
_STYLE = """
<style>
.umbra-card{display:inline-flex;gap:12px;align-items:flex-start;border:1px solid #d0d7de;
border-radius:8px;padding:10px;margin:4px;font-family:-apple-system,Segoe UI,sans-serif;
font-size:12px;background:#fff;color:#1f2328;max-width:520px;vertical-align:top}
.umbra-card .umbra-pic{flex:0 0 auto;width:128px;height:128px;display:flex;
align-items:center;justify-content:center;background:#0d1117;border-radius:6px;overflow:hidden}
.umbra-card .umbra-pic img{width:128px;height:128px;object-fit:cover}
.umbra-card table{border-collapse:collapse}
.umbra-card td{padding:1px 6px 1px 0;vertical-align:top}
.umbra-card td.k{color:#656d76;white-space:nowrap}
.umbra-card .umbra-id{font-weight:600;word-break:break-all}
.umbra-card a{color:#0969da;text-decoration:none}
.umbra-gallery{display:flex;flex-wrap:wrap;gap:0}
</style>
"""


def _esc(value: Any) -> str:
    return escape("" if value is None else str(value))


def _rings(coords: Any):
    """Yield each polygon ring as a list of ``(lon, lat)`` tuples.

    Handles Polygon and MultiPolygon coordinate nestings by recursing until a
    node looks like a ring (a list whose first element is a position).
    """
    if not isinstance(coords, (list, tuple)) or not coords:
        return
    first = coords[0]
    if (
        isinstance(first, (list, tuple))
        and len(first) >= 2
        and all(isinstance(v, (int, float)) for v in first[:2])
    ):
        yield [(float(p[0]), float(p[1])) for p in coords]
    else:
        for child in coords:
            yield from _rings(child)


def footprint_svg(item: UmbraItem, *, size: int = 128) -> str | None:
    """Draw an item's footprint polygon as a small standalone SVG.

    Returns ``None`` when the item has no usable geometry/bbox. The drawing is
    a schematic of the acquisition's ground footprint in lon/lat (north up),
    not a map — it gives instant spatial context with no tiles and no network.
    """
    geom = item.geometry
    bbox = item.bbox
    if not geom or bbox is None:
        return None
    min_lon, min_lat, max_lon, max_lat = bbox
    span_lon = max_lon - min_lon
    span_lat = max_lat - min_lat
    if span_lon <= 0 or span_lat <= 0:
        return None

    pad = 8
    inner = size - 2 * pad
    polys: list[str] = []
    for ring in _rings(geom.get("coordinates")):
        pts = []
        for lon, lat in ring:
            x = pad + (lon - min_lon) / span_lon * inner
            # SVG y grows downward; flip so north is up.
            y = pad + (max_lat - lat) / span_lat * inner
            pts.append(f"{x:.1f},{y:.1f}")
        if pts:
            polys.append(f'<polygon points="{" ".join(pts)}" />')
    if not polys:
        return None

    return (
        f'<svg viewBox="0 0 {size} {size}" width="{size}" height="{size}" '
        'xmlns="http://www.w3.org/2000/svg" '
        'style="background:#0d1117">'
        '<g fill="rgba(88,166,255,0.35)" stroke="#58a6ff" stroke-width="1.5" '
        'stroke-linejoin="round">' + "".join(polys) + "</g></svg>"
    )


def _metadata_rows(item: UmbraItem) -> str:
    info = item.metadata_summary()
    rng = info["resolution_range_m"]
    azi = info["resolution_azimuth_m"]

    def res(v: Any) -> str:
        return f"{v:.2f} m" if isinstance(v, (int, float)) else "?"

    inc = info["incidence_angle_deg"]
    inc_str = f"{inc:.1f}°" if isinstance(inc, (int, float)) else "—"
    rows = [
        ("acquired", info["datetime"] or "—"),
        ("platform", f"{info['platform'] or '—'} ({info['instrument_mode'] or '—'})"),
        ("product", info["product_type"] or "—"),
        ("pol", ", ".join(info["polarizations"]) or "—"),
        ("incidence", inc_str),
        ("resolution", f"{res(rng)} × {res(azi)}"),
        ("assets", ", ".join(info["available_assets"]) or "none"),
    ]
    out = "".join(f'<tr><td class="k">{_esc(k)}</td><td>{_esc(v)}</td></tr>' for k, v in rows)
    if item.href:
        out += (
            '<tr><td class="k">item</td>'
            f'<td><a href="{_esc(item.href)}" target="_blank">STAC JSON</a></td></tr>'
        )
    return out


def item_card_html(item: UmbraItem, *, thumbnail: str | None = None) -> str:
    """Render one item as an HTML card (thumbnail/footprint + metadata table).

    ``thumbnail`` is an optional ``data:image/png;base64,...`` URI of a SAR
    quicklook; when absent the card falls back to the footprint SVG (or, if
    the item has no geometry, an empty pane).
    """
    if thumbnail:
        pic = f'<img src="{_esc(thumbnail)}" alt="SAR quicklook" />'
    else:
        pic = footprint_svg(item) or ""
    return (
        f'{_STYLE}<div class="umbra-card">'
        f'<div class="umbra-pic">{pic}</div>'
        f'<table><tr><td class="k">id</td>'
        f'<td class="umbra-id">{_esc(item.id)}</td></tr>'
        f"{_metadata_rows(item)}</table></div>"
    )


def gallery_html(items, *, thumbnails: dict[int, str | None] | None = None) -> str:
    """Render a sequence of items as a wrapping gallery of cards.

    ``thumbnails`` optionally maps each item's index to a data-URI thumbnail.
    """
    thumbnails = thumbnails or {}
    cards = "".join(
        item_card_html(item, thumbnail=thumbnails.get(i)) for i, item in enumerate(items)
    )
    count = len(items)
    plural = "" if count == 1 else "s"
    header = (
        '<div style="font-family:sans-serif;font-size:12px;color:#656d76">'
        f"{count} item{plural}</div>"
    )
    return f'{_STYLE}{header}<div class="umbra-gallery">{cards}</div>'
